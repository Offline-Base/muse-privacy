"""The relay's storage: one SQLite file. On Modal it lives on a Volume and one container owns it.

Kept: device ids with the SHA-256 of their secrets, pairing codes and client IPs only as HMACs
under PAIRING_PEPPER, OAuth clients, and grants and tokens (tokens only as SHA-256). No MCP body
is ever stored. Every method is synchronous and fast; the app serializes writes with one lock and
calls `persist` after a write so the Volume keeps it.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (device_id TEXT PRIMARY KEY, secret_sha256 TEXT NOT NULL, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL, expires_at REAL NOT NULL, uses_left INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS codes_by_device ON codes (device_id);
CREATE TABLE IF NOT EXISTS hits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, reset_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS clients (client_id TEXT PRIMARY KEY, data TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS consents (handle TEXT PRIMARY KEY, cookie_sha256 TEXT NOT NULL, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS auth_codes (code_sha256 TEXT PRIMARY KEY, data TEXT NOT NULL, expires_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS grants (grant_id TEXT PRIMARY KEY, device_id TEXT NOT NULL, client_id TEXT NOT NULL, read_only INTEGER NOT NULL, created_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS grants_by_device ON grants (device_id);
CREATE TABLE IF NOT EXISTS tokens (token_sha256 TEXT PRIMARY KEY, kind TEXT NOT NULL, grant_id TEXT NOT NULL, expires_at REAL NOT NULL, replaced_at REAL);
CREATE INDEX IF NOT EXISTS tokens_by_grant ON tokens (grant_id);
"""


class Store:
    def __init__(self, path: str | Path, persist: Callable[[], None] | None = None, clock: Callable[[], float] = time.time):
        self.db = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self.db.execute("PRAGMA journal_mode=DELETE")
        self.db.executescript(SCHEMA)
        self._persist = persist or (lambda: None)
        self.now = clock

    def persist(self) -> None:
        self._persist()

    def _one(self, sql: str, *args):
        return self.db.execute(sql, args).fetchone()

    # Devices
    def add_device(self, device_id: str, secret_sha256: str) -> bool:
        try:
            self.db.execute("INSERT INTO devices VALUES (?, ?, ?)", (device_id, secret_sha256, self.now()))
        except sqlite3.IntegrityError:
            return False
        return True

    def device_secret_hash(self, device_id: str) -> str | None:
        row = self._one("SELECT secret_sha256 FROM devices WHERE device_id = ?", device_id)
        return row[0] if row else None

    # Rate limits
    def sweep(self) -> None:
        now = self.now()
        self.db.execute("DELETE FROM codes WHERE expires_at <= ?", (now,))
        self.db.execute("DELETE FROM hits WHERE reset_at <= ?", (now,))
        self.db.execute("DELETE FROM consents WHERE expires_at <= ?", (now,))
        self.db.execute("DELETE FROM auth_codes WHERE expires_at <= ?", (now,))
        self.db.execute("DELETE FROM tokens WHERE expires_at <= ?", (now,))

    def hit_count(self, key: str) -> int:
        row = self._one("SELECT count FROM hits WHERE key = ? AND reset_at > ?", key, self.now())
        return row[0] if row else 0

    def bump(self, key: str, window_seconds: float) -> None:
        self.db.execute(
            "INSERT INTO hits (key, count, reset_at) VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET count = count + 1",
            (key, self.now() + window_seconds),
        )

    # Pairing codes
    def issue_code(self, device_id: str, code_hash: str, ttl_seconds: float, uses: int) -> bool:
        if self._one("SELECT 1 FROM codes WHERE code_hash = ?", code_hash):
            return False
        self.db.execute("DELETE FROM codes WHERE device_id = ?", (device_id,))
        self.db.execute(
            "INSERT INTO codes (code_hash, device_id, expires_at, uses_left) VALUES (?, ?, ?, ?)",
            (code_hash, device_id, self.now() + ttl_seconds, max(1, int(uses))),
        )
        return True

    def code(self, code_hash: str):
        return self._one("SELECT code_hash, device_id, expires_at, uses_left FROM codes WHERE code_hash = ?", code_hash)

    def use_code(self, code_hash: str, uses_left: int) -> None:
        if uses_left > 1:
            self.db.execute("UPDATE codes SET uses_left = uses_left - 1 WHERE code_hash = ?", (code_hash,))
        else:
            self.db.execute("DELETE FROM codes WHERE code_hash = ?", (code_hash,))

    # OAuth clients
    def put_client(self, client_id: str, data: dict) -> None:
        self.db.execute(
            "INSERT INTO clients VALUES (?, ?, ?) ON CONFLICT(client_id) DO UPDATE SET data = excluded.data, fetched_at = excluded.fetched_at",
            (client_id, json.dumps(data), self.now()),
        )

    def client(self, client_id: str) -> tuple[dict, float] | None:
        row = self._one("SELECT data, fetched_at FROM clients WHERE client_id = ?", client_id)
        return (json.loads(row[0]), row[1]) if row else None

    # Consent transactions (the link page)
    def put_consent(self, handle: str, cookie_sha256: str, data: dict, ttl_seconds: float) -> None:
        self.db.execute("INSERT INTO consents VALUES (?, ?, ?, ?)", (handle, cookie_sha256, json.dumps(data), self.now() + ttl_seconds))

    def consent(self, handle: str) -> tuple[str, dict] | None:
        row = self._one("SELECT cookie_sha256, data FROM consents WHERE handle = ? AND expires_at > ?", handle, self.now())
        return (row[0], json.loads(row[1])) if row else None

    def drop_consent(self, handle: str) -> None:
        self.db.execute("DELETE FROM consents WHERE handle = ?", (handle,))

    # Authorization codes
    def put_auth_code(self, code_sha256: str, data: dict, ttl_seconds: float) -> None:
        self.db.execute("INSERT INTO auth_codes VALUES (?, ?, ?)", (code_sha256, json.dumps(data), self.now() + ttl_seconds))

    def take_auth_code(self, code_sha256: str) -> dict | None:
        row = self._one("SELECT data FROM auth_codes WHERE code_sha256 = ? AND expires_at > ?", code_sha256, self.now())
        self.db.execute("DELETE FROM auth_codes WHERE code_sha256 = ?", (code_sha256,))
        return json.loads(row[0]) if row else None

    # Grants and tokens
    def add_grant(self, grant_id: str, device_id: str, client_id: str, read_only: bool) -> None:
        self.db.execute("INSERT INTO grants VALUES (?, ?, ?, ?, ?)", (grant_id, device_id, client_id, int(read_only), self.now()))

    def grant(self, grant_id: str):
        return self._one("SELECT grant_id, device_id, client_id, read_only FROM grants WHERE grant_id = ?", grant_id)

    def add_token(self, token_sha256: str, kind: str, grant_id: str, ttl_seconds: float) -> None:
        self.db.execute("INSERT INTO tokens VALUES (?, ?, ?, ?, NULL)", (token_sha256, kind, grant_id, self.now() + ttl_seconds))

    def token(self, token_sha256: str, kind: str):
        return self._one(
            "SELECT token_sha256, grant_id, expires_at, replaced_at FROM tokens WHERE token_sha256 = ? AND kind = ? AND expires_at > ?",
            token_sha256,
            kind,
            self.now(),
        )

    def mark_replaced(self, token_sha256: str) -> None:
        self.db.execute("UPDATE tokens SET replaced_at = ? WHERE token_sha256 = ? AND replaced_at IS NULL", (self.now(), token_sha256))

    def drop_token(self, token_sha256: str) -> str | None:
        row = self._one("SELECT grant_id FROM tokens WHERE token_sha256 = ?", token_sha256)
        self.db.execute("DELETE FROM tokens WHERE token_sha256 = ?", (token_sha256,))
        return row[0] if row else None

    def revoke_grant(self, grant_id: str) -> None:
        self.db.execute("DELETE FROM tokens WHERE grant_id = ?", (grant_id,))
        self.db.execute("DELETE FROM grants WHERE grant_id = ?", (grant_id,))

    def revoke_device(self, device_id: str) -> int:
        ids = [row[0] for row in self.db.execute("SELECT grant_id FROM grants WHERE device_id = ?", (device_id,))]
        for grant_id in ids:
            self.revoke_grant(grant_id)
        return len(ids)
