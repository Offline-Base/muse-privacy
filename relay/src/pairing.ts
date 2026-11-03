import { DurableObject } from "cloudflare:workers";
import { hmacHex, sameText } from "./crypto";
import type { Env } from "./env";

/**
 * Pairing codes and attempt counters, in one Durable Object so that "single use" and the
 * attempt limits hold even when two requests race (KV cannot promise either).
 *
 * Stored: the HMAC of each live code, the device it links, and its expiry; and per-window hit
 * counts keyed by the HMAC of the client IP or of the code tried. Nothing else. The HMAC key is
 * the PAIRING_PEPPER Worker secret, so a copy of this storage alone cannot be brute-forced back
 * to live codes or IP addresses.
 */

/** No 0, O, 1 or I. 32 symbols, so `byte & 31` picks each one with equal chance. */
export const ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ";
export const CODE_LENGTH = 8;
export const CODE_TTL_SECONDS = 600;
/**
 * A review code, for a connector directory's reviewers (POST /device/pair with {"review": true}):
 * it links up to REVIEW_USES Muse accounts to the device within REVIEW_TTL_SECONDS. Meant only for
 * a device that holds invented data. Like any code, a newer code for the device replaces it, and
 * /device/unlink revokes every link it made.
 */
export const REVIEW_TTL_SECONDS = 14 * 24 * 3600;
export const REVIEW_USES = 20;
/** At most this many tries of one code value, from any IP, in each window. */
export const MAX_ATTEMPTS_PER_CODE = 5;
/** At most this many tries from one IP in each window. A school or office NAT shares one IP. */
export const MAX_ATTEMPTS_PER_IP = 20;
export const WINDOW_MS = 10 * 60 * 1000;
/** The single gate instance's name. */
export const GATE = "gate";
const MIN_PEPPER_LENGTH = 32;

/** The PAIRING_PEPPER secret, or null when it is missing or too short (then the relay will not pair). */
export function pepper(env: Env): string | null {
  const value = env.PAIRING_PEPPER;
  return typeof value === "string" && value.length >= MIN_PEPPER_LENGTH ? value : null;
}

/** What the gate keeps for a pairing code: HMAC-SHA256 under the pepper. */
export const codeKey = (key: string, code: string) => hmacHex(key, `code:${code}`);
/** What the gate keeps for a client IP: HMAC-SHA256 under the pepper. */
export const ipKey = (key: string, ip: string) => hmacHex(key, `ip:${ip}`);

export function newCode(): string {
  return [...crypto.getRandomValues(new Uint8Array(CODE_LENGTH))].map((byte) => ALPHABET[byte & 31]).join("");
}

/** "abcd 2345", "ABCD-2345" and "ABCD2345" are the same code. Anything else is null. */
export function normalizeCode(input: string): string | null {
  const code = input.toUpperCase().replace(/[\s-]/g, "");
  if (code.length !== CODE_LENGTH || [...code].some((char) => !ALPHABET.includes(char))) return null;
  return code;
}

export type Redeemed = { ok: true; deviceId: string } | { ok: false; reason: "limited" | "wrong" };

export class PairingGate extends DurableObject<Env> {
  constructor(ctx: DurableObjectState, env: Env) {
    super(ctx, env);
    ctx.blockConcurrencyWhile(async () => {
      const sql = ctx.storage.sql;
      sql.exec(
        "CREATE TABLE IF NOT EXISTS codes (code_hash TEXT PRIMARY KEY, device_id TEXT NOT NULL, expires_at INTEGER NOT NULL)",
      );
      sql.exec("CREATE INDEX IF NOT EXISTS codes_by_device ON codes (device_id)");
      // Added for review codes; a table from before keeps working with one use per code.
      const columns = sql.exec<{ name: string }>("PRAGMA table_info(codes)").toArray().map((row) => row.name);
      if (!columns.includes("uses_left")) sql.exec("ALTER TABLE codes ADD COLUMN uses_left INTEGER NOT NULL DEFAULT 1");
      sql.exec("CREATE TABLE IF NOT EXISTS hits (key TEXT PRIMARY KEY, count INTEGER NOT NULL, reset_at INTEGER NOT NULL)");
    });
  }

  private sweep(now: number): void {
    this.ctx.storage.sql.exec("DELETE FROM codes WHERE expires_at <= ?", now);
    this.ctx.storage.sql.exec("DELETE FROM hits WHERE reset_at <= ?", now);
  }

  private count(key: string): number {
    const row = this.ctx.storage.sql.exec<{ count: number }>("SELECT count FROM hits WHERE key = ?", key).toArray()[0];
    return row?.count ?? 0;
  }

  private bump(key: string, now: number): void {
    this.ctx.storage.sql.exec(
      "INSERT INTO hits (key, count, reset_at) VALUES (?, 1, ?) ON CONFLICT(key) DO UPDATE SET count = count + 1",
      key,
      now + WINDOW_MS,
    );
  }

  /** Count one use of `key`. False when `limit` uses already landed in this window. */
  async take(key: string, limit: number): Promise<boolean> {
    const now = Date.now();
    this.sweep(now);
    if (this.count(key) >= limit) return false;
    this.bump(key, now);
    return true;
  }

  /**
   * Store a device's new code (by hash), good for `uses` links within `ttlSeconds`. Only the newest
   * code per device works. False on a collision.
   */
  async issue(deviceId: string, codeHash: string, ttlSeconds = CODE_TTL_SECONDS, uses = 1): Promise<boolean> {
    const now = Date.now();
    this.sweep(now);
    if (this.ctx.storage.sql.exec("SELECT 1 FROM codes WHERE code_hash = ?", codeHash).toArray().length) return false;
    this.ctx.storage.sql.exec("DELETE FROM codes WHERE device_id = ?", deviceId);
    this.ctx.storage.sql.exec(
      "INSERT INTO codes (code_hash, device_id, expires_at, uses_left) VALUES (?, ?, ?, ?)",
      codeHash,
      deviceId,
      now + ttlSeconds * 1000,
      Math.max(1, Math.floor(uses)),
    );
    return true;
  }

  /**
   * One pairing attempt from `ipHash` with the code whose hash is `codeHash` (null when the
   * input was not even shaped like a code). Counts against the IP and the code value, then uses
   * one use of the code if it is live (a one-time code is then gone). SQL calls are synchronous, so the whole check runs as one step.
   */
  async redeem(ipHash: string, codeHash: string | null): Promise<Redeemed> {
    const now = Date.now();
    this.sweep(now);
    const limits: [string, number][] = [[`ip:${ipHash}`, MAX_ATTEMPTS_PER_IP]];
    if (codeHash) limits.push([`code:${codeHash}`, MAX_ATTEMPTS_PER_CODE]);
    if (limits.some(([key, limit]) => this.count(key) >= limit)) return { ok: false, reason: "limited" };
    for (const [key] of limits) this.bump(key, now);
    if (!codeHash) return { ok: false, reason: "wrong" };

    const row = this.ctx.storage.sql
      .exec<{ code_hash: string; device_id: string; expires_at: number; uses_left: number }>(
        "SELECT code_hash, device_id, expires_at, uses_left FROM codes WHERE code_hash = ?",
        codeHash,
      )
      .toArray()[0];
    // The lookup key is a hash of the guess, so its timing says nothing about the live code;
    // the stored hash is still compared in constant time before it counts.
    if (!row || row.expires_at <= now || !sameText(row.code_hash, codeHash)) return { ok: false, reason: "wrong" };
    if (row.uses_left > 1) this.ctx.storage.sql.exec("UPDATE codes SET uses_left = uses_left - 1 WHERE code_hash = ?", codeHash);
    else this.ctx.storage.sql.exec("DELETE FROM codes WHERE code_hash = ?", codeHash);
    return { ok: true, deviceId: row.device_id };
  }
}
