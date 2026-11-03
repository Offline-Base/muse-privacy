"""The relay as an ASGI app. Same endpoints and behavior as relay/ (docs/CONTRACTS.md, "Relay"):

  POST /device/register   new device id + secret, returned once (only the secret's hash is kept)
  POST /device/pair       a pairing code for the device (bearer secret); {"review": true} for a review code
  POST /device/unlink     revoke every link bound to the device (bearer secret)
  WS   /device/connect    the device's WebSocket (bearer secret); a new connection replaces the old one
  GET/POST /authorize     the Link your device page: pairing code, then Full access or Read only
  POST/DELETE /mcp        MCP requests (OAuth bearer), forwarded to the device as frames
  /oauth/token, /oauth/register, /.well-known/*   OAuth 2.1 with PKCE, dynamic client registration
                          and Client ID Metadata Documents

Bodies are never stored or logged.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import socket
import time
from dataclasses import dataclass
from urllib.parse import parse_qs, urlencode, urlsplit

import httpx
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response

from . import gates
from .hub import Hub, Reply
from .pages import CSP, access_from, message_page, pairing_page
from .store import Store

log = logging.getLogger("relay")

SCOPE = "mcp"
SCOPES = (SCOPE, "offline_access")
ACCESS_TTL = 3600
REFRESH_TTL = 30 * 24 * 3600
REFRESH_GRACE = 60  # a refresh token just replaced still works this long, for a client that retried
AUTH_CODE_TTL = 600
CONSENT_TTL = 600
CODE_TTL_SECONDS = 600
REVIEW_TTL_SECONDS = 14 * 24 * 3600
REVIEW_USES = 20
ALPHABET = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
CODE_LENGTH = 8
MAX_ATTEMPTS_PER_CODE = 5
MAX_ATTEMPTS_PER_IP = 20
REGISTRATIONS_PER_IP = 10
CLIENT_REGISTRATIONS_PER_IP = 30
WINDOW_SECONDS = 600
MIN_PEPPER_LENGTH = 32
MAX_BODY_BYTES = 4 * 1024 * 1024
CIMD_CACHE_SECONDS = 3600
CIMD_MAX_BYTES = 64 * 1024
CIMD_DEADLINE_SECONDS = 5.0
CIMD_FAILURE_CACHE_SECONDS = 60
AUTHORIZE_PER_IP = 60
COMMIT_EVERY_SECONDS = 2.0
MAX_SOCKET_AGE_SECONDS = 600  # an idle device socket is recycled so it lands on the newest container after a deploy
UNAUTHORIZED_CLOSE_CODE = 4401  # the device secret is wrong or revoked: relay_client stops (exit 3)
RECYCLE_CLOSE_CODE = 1012  # "service restart": the device reconnects (4000 would make it stop for good)
FORWARDED = ("content-type", "accept", "mcp-session-id", "mcp-protocol-version", "mcp-method", "mcp-name")
NULL_BODY_STATUS = {204, 205, 304}
DEVICE_ID_LENGTH = len("dev_") + 32

WRONG_CODE = "That code did not work. A code works once and lasts 10 minutes, so make a new one on your device and try again."
TOO_MANY = "Too many tries. Wait 10 minutes, then try again."
EXPIRED_TITLE = "This link did not work"
EXPIRED_TEXT = "Start linking again from your app. This page may have expired or been used already, or this browser blocked its cookie."
NOT_SET_UP_TITLE = "This relay is not set up yet"
NOT_SET_UP_TEXT = "Its pairing secret is missing, so no device can be linked. Whoever runs the relay needs to set PAIRING_PEPPER."
BAD_REQUEST_TITLE = "This link request did not work"


@dataclass
class Settings:
    pepper: str | None
    origin: str | None = None  # the public https origin; derived from the request when None
    request_timeout: float = 60.0


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def b64url(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def same(a: str, b: str) -> bool:
    return hmac.compare_digest(a.encode(), b.encode())


def new_code() -> str:
    return "".join(ALPHABET[byte & 31] for byte in secrets.token_bytes(CODE_LENGTH))


def normalize_code(value: str) -> str | None:
    code = value.upper().replace(" ", "").replace("-", "")
    if len(code) != CODE_LENGTH or any(char not in ALPHABET for char in code):
        return None
    return code


def _is_loopback(host: str) -> bool:
    return host in ("127.0.0.1", "localhost", "::1", "[::1]")


def valid_redirect(uri: str) -> bool:
    if not isinstance(uri, str) or len(uri) > 2000 or "#" in uri:
        return False
    parts = urlsplit(uri)
    if parts.scheme == "https" and parts.hostname:
        return True
    return parts.scheme == "http" and _is_loopback(parts.hostname or "")


def redirect_matches(registered: list[str], given: str) -> bool:
    if given in registered:
        return True
    g = urlsplit(given)
    if g.scheme != "http" or not _is_loopback(g.hostname or ""):
        return False
    # RFC 8252 7.3: a loopback redirect may use any port.
    for uri in registered:
        r = urlsplit(uri)
        if r.scheme == "http" and r.hostname == g.hostname and r.path == g.path and r.query == g.query:
            return True
    return False


def create_app(store: Store, settings: Settings, hub: Hub | None = None, http: httpx.AsyncClient | None = None) -> FastAPI:
    async def commit_loop() -> None:
        while True:
            await asyncio.sleep(COMMIT_EVERY_SECONDS)
            await commit()

    async def commit() -> None:
        if not store.dirty:
            return
        try:
            await asyncio.to_thread(store.persist)
        except Exception as exc:  # the next round tries again; the change is already in SQLite
            store.mark_dirty()
            log.warning("saving relay state failed (%s)", type(exc).__name__)

    @contextlib.asynccontextmanager
    async def lifespan(_app):
        task = asyncio.create_task(commit_loop())
        try:
            yield
        finally:
            task.cancel()
            await commit()

    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
    hub = hub or Hub(settings.request_timeout)
    lock = asyncio.Lock()
    app.state.hub = hub
    app.state.store = store
    cimd_failures: dict[str, float] = {}

    async def persist() -> None:
        """Writes are saved by the background commit loop, a couple of seconds later."""
        store.mark_dirty()

    def pepper() -> str | None:
        value = settings.pepper
        return value if isinstance(value, str) and len(value) >= MIN_PEPPER_LENGTH else None

    def keyed(kind: str, value: str) -> str:
        return hmac.new(pepper().encode(), f"{kind}:{value}".encode(), hashlib.sha256).hexdigest()

    def origin_of(request: Request | WebSocket) -> str:
        if settings.origin:
            return settings.origin.rstrip("/")
        host = request.headers.get("host", "localhost")
        proto = request.headers.get("x-forwarded-proto") or request.url.scheme
        if proto in ("ws", "wss"):
            proto = "https" if proto == "wss" else "http"
        return f"{proto}://{host}"

    def client_ip(request: Request) -> str:
        # Modal hands the app the caller's address and drops any X-Forwarded-For it sent, so the
        # connection's own address is the one that cannot be spoofed.
        return request.client.host if request.client else "unknown"

    def json_reply(body, status: int = 200, headers: dict | None = None) -> JSONResponse:
        return JSONResponse(body, status_code=status, headers={"cache-control": "no-store", **(headers or {})})

    def html_reply(body: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(
            body,
            status_code=status,
            headers={
                "content-security-policy": CSP,
                "x-frame-options": "DENY",
                "referrer-policy": "no-referrer",
                "cache-control": "no-store",
            },
        )

    def oauth_error(error: str, description: str, status: int = 400) -> JSONResponse:
        return json_reply({"error": error, "error_description": description}, status)

    async def take(key: str, limit: int) -> bool:
        async with lock:
            store.sweep()
            if store.hit_count(key) >= limit:
                return False
            store.bump(key, WINDOW_SECONDS)
        await persist()
        return True

    def device_from_secret(request: Request | WebSocket) -> str | None:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return None
        secret = header[7:].strip()
        device_id, _, rest = secret.partition(".")
        if not (device_id.startswith("dev_") and len(device_id) == DEVICE_ID_LENGTH and len(rest) == 43):
            return None
        stored = store.device_secret_hash(device_id)
        return device_id if stored and same(stored, sha256_hex(secret)) else None

    # ---- OAuth clients -------------------------------------------------------------------

    async def public_host(host: str) -> bool:
        try:
            infos = await asyncio.to_thread(socket.getaddrinfo, host, 443, proto=socket.IPPROTO_TCP)
        except OSError:
            return False
        addresses = {info[4][0] for info in infos}
        return bool(addresses) and all(ipaddress.ip_address(a.split("%")[0]).is_global for a in addresses)

    async def fetch_cimd(url: str) -> dict | None:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname or not await public_host(parts.hostname):
            return None
        client = http or httpx.AsyncClient(timeout=CIMD_DEADLINE_SECONDS, follow_redirects=False)

        async def read() -> bytes | None:
            async with client.stream("GET", url, headers={"accept": "application/json"}) as response:
                if response.status_code != 200:
                    return None
                stream = response.extensions.get("network_stream")
                peer = stream.get_extra_info("server_addr") if stream is not None else None
                # DNS could have changed since the check above: the address actually used must be public too.
                if peer and not ipaddress.ip_address(str(peer[0]).split("%")[0]).is_global:
                    return None
                chunks, size = [], 0
                async for chunk in response.aiter_bytes():
                    size += len(chunk)
                    if size > CIMD_MAX_BYTES:
                        return None
                    chunks.append(chunk)
                return b"".join(chunks)

        try:
            raw = await asyncio.wait_for(read(), CIMD_DEADLINE_SECONDS)
            doc = json.loads(raw) if raw is not None else None
        except (TimeoutError, httpx.HTTPError, ValueError, OSError):
            return None
        finally:
            if http is None:
                await client.aclose()
        if not isinstance(doc, dict) or doc.get("client_id") != url:
            return None
        uris = doc.get("redirect_uris")
        if not isinstance(uris, list) or not uris or not all(valid_redirect(u) for u in uris):
            return None
        name = doc.get("client_name")
        return {
            "client_name": name if isinstance(name, str) and name.strip() else parts.hostname,
            "redirect_uris": uris,
            "auth": "none",
            "domain": parts.hostname,
            "cimd": True,
        }

    async def get_client(client_id: str) -> dict | None:
        if not client_id or len(client_id) > 2000:
            return None
        found = store.client(client_id)
        if client_id.startswith("https://"):
            if found and time.time() - found[1] < CIMD_CACHE_SECONDS:
                return found[0]
            failed_at = cimd_failures.get(client_id)
            if failed_at is not None and time.time() - failed_at < CIMD_FAILURE_CACHE_SECONDS:
                return None
            doc = await fetch_cimd(client_id)
            if doc is None:
                if len(cimd_failures) > 1000:
                    cimd_failures.clear()
                cimd_failures[client_id] = time.time()
                return None
            async with lock:
                store.put_client(client_id, doc)
            await persist()
            return doc
        return found[0] if found else None

    # ---- Discovery -----------------------------------------------------------------------

    @app.get("/.well-known/oauth-authorization-server")
    @app.get("/.well-known/openid-configuration")
    async def as_metadata(request: Request):
        o = origin_of(request)
        return json_reply(
            {
                "issuer": o,
                "authorization_endpoint": f"{o}/authorize",
                "token_endpoint": f"{o}/oauth/token",
                "protected_resources": [f"{o}/mcp"],
                "registration_endpoint": f"{o}/oauth/register",
                "scopes_supported": list(SCOPES),
                "response_types_supported": ["code"],
                "response_modes_supported": ["query"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["client_secret_basic", "client_secret_post", "none"],
                "revocation_endpoint": f"{o}/oauth/token",
                "code_challenge_methods_supported": ["S256"],
                "authorization_response_iss_parameter_supported": True,
                "client_id_metadata_document_supported": True,
            }
        )

    @app.get("/.well-known/oauth-protected-resource")
    @app.get("/.well-known/oauth-protected-resource/mcp")
    async def pr_metadata(request: Request):
        o = origin_of(request)
        return json_reply(
            {
                "resource": f"{o}/mcp",
                "authorization_servers": [o],
                "scopes_supported": [SCOPE],
                "bearer_methods_supported": ["header"],
                "resource_name": "Muse Privacy",
            }
        )

    @app.get("/health")
    async def health():
        return json_reply({"ok": True})

    @app.get("/")
    async def root(request: Request):
        return PlainTextResponse(
            f"Muse Privacy relay. Add {origin_of(request)}/mcp to Muse as a connector, then type the pairing code from your device.\n"
        )

    # ---- Dynamic client registration -----------------------------------------------------

    @app.post("/oauth/register")
    async def register_client(request: Request):
        if pepper() is None:
            return json_reply({"error": "relay is not set up: PAIRING_PEPPER is missing"}, 503)
        if not await take(f"oauth-register:{keyed('ip', client_ip(request))}", CLIENT_REGISTRATIONS_PER_IP):
            return json_reply({"error": "too many registrations, try again in 10 minutes"}, 429, {"retry-after": "600"})
        try:
            body = await request.json()
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return oauth_error("invalid_client_metadata", "Send a JSON object.")
        uris = body.get("redirect_uris")
        if not isinstance(uris, list) or not uris or len(uris) > 10 or not all(valid_redirect(u) for u in uris):
            return oauth_error("invalid_redirect_uri", "redirect_uris must be https URLs or http loopback URLs.")
        method = body.get("token_endpoint_auth_method", "client_secret_basic")
        if method not in ("none", "client_secret_basic", "client_secret_post"):
            return oauth_error("invalid_client_metadata", "Unsupported token_endpoint_auth_method.")
        name = body.get("client_name")
        name = name.strip()[:100] if isinstance(name, str) and name.strip() else "An app"
        grant_types = body.get("grant_types") or ["authorization_code"]
        if not isinstance(grant_types, list) or not set(grant_types) <= {"authorization_code", "refresh_token"}:
            return oauth_error("invalid_client_metadata", "Unsupported grant_types.")
        client_id = secrets.token_urlsafe(12)
        now = int(time.time())
        record = {"client_name": name, "redirect_uris": uris, "auth": method, "domain": None, "cimd": False}
        answer = {
            "client_id": client_id,
            "redirect_uris": uris,
            "client_name": name,
            "grant_types": grant_types,
            "response_types": ["code"],
            "token_endpoint_auth_method": method,
            "client_id_issued_at": now,
        }
        if method != "none":
            secret = secrets.token_urlsafe(32)
            record["secret_sha256"] = sha256_hex(secret)
            answer.update({"client_secret": secret, "client_secret_issued_at": now, "client_secret_expires_at": 0})
        async with lock:
            store.put_client(client_id, record)
        await persist()
        return json_reply(answer, 201)

    # ---- The link page ---------------------------------------------------------------------

    def bad_request(text: str) -> HTMLResponse:
        return html_reply(message_page(BAD_REQUEST_TITLE, text), 400)

    def error_redirect(o: str, redirect_uri: str, error: str, state: str | None) -> RedirectResponse:
        query = {"error": error, "iss": o}
        if state is not None:
            query["state"] = state
        sep = "&" if "?" in redirect_uri else "?"
        return RedirectResponse(f"{redirect_uri}{sep}{urlencode(query)}", status_code=302)

    async def parse_auth_request(request: Request, params: dict):
        """(client, auth_request) or a Response to send instead."""
        o = origin_of(request)
        client_id = params.get("client_id", "")
        client = await get_client(client_id)
        if client is None:
            if client_id.startswith("https://"):
                return html_reply(message_page("This app could not be checked", "Its details could not be loaded. Try again in a moment."), 400)
            return bad_request("The app that sent you here is not registered with this relay. Start linking again from the app.")
        registered = client["redirect_uris"]
        redirect_uri = params.get("redirect_uri") or (registered[0] if len(registered) == 1 else "")
        if not redirect_uri or not redirect_matches(registered, redirect_uri):
            return bad_request("The app sent you back to an address it did not register. Start linking again from the app.")
        state = params.get("state")
        if params.get("response_type") != "code":
            return error_redirect(o, redirect_uri, "unsupported_response_type", state)
        challenge = params.get("code_challenge", "")
        if params.get("code_challenge_method") != "S256" or not (43 <= len(challenge) <= 128):
            return error_redirect(o, redirect_uri, "invalid_request", state)
        scopes = (params.get("scope") or SCOPE).split()
        if not set(scopes) <= set(SCOPES):
            return error_redirect(o, redirect_uri, "invalid_scope", state)
        resource = params.get("resource")
        if resource is not None and resource.rstrip("/") not in (f"{o}/mcp", o):
            return error_redirect(o, redirect_uri, "invalid_target", state)
        return client, {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": challenge,
            "resource": resource,
        }

    def page_for(request: Request, client: dict, handle: str, error: str | None = None, access: str = "full", status: int = 200, redirect_uri: str = ""):
        host = urlsplit(redirect_uri).hostname or ""
        return html_reply(
            pairing_page(
                client_name=client.get("client_name") or "An app",
                client_domain=client.get("domain"),
                redirect_host=host,
                loopback=_is_loopback(host),
                handle=handle,
                action=f"/authorize?{request.url.query}" if request.url.query else "/authorize",
                error=error,
                access=access,
            ),
            status,
        )

    def cookie_name(handle: str) -> str:
        return f"__Host-mp-consent-{handle[:12]}"

    @app.get("/authorize")
    async def authorize_get(request: Request):
        if pepper() is None:
            return html_reply(message_page(NOT_SET_UP_TITLE, NOT_SET_UP_TEXT), 503)
        if not await take(f"authorize:{keyed('ip', client_ip(request))}", AUTHORIZE_PER_IP):
            return html_reply(message_page("Too many tries", TOO_MANY), 429)
        parsed = await parse_auth_request(request, dict(request.query_params))
        if isinstance(parsed, Response):
            return parsed
        client, auth = parsed
        handle = secrets.token_urlsafe(24)
        cookie = secrets.token_urlsafe(24)
        async with lock:
            store.sweep()
            store.put_consent(handle, sha256_hex(cookie), auth, CONSENT_TTL)
        await persist()
        response = page_for(request, client, handle, redirect_uri=auth["redirect_uri"])
        response.set_cookie(cookie_name(handle), cookie, max_age=CONSENT_TTL, path="/", secure=True, httponly=True, samesite="lax")
        return response

    @app.post("/authorize")
    async def authorize_post(request: Request):
        o = origin_of(request)
        raw = await request.body()
        if len(raw) > 16 * 1024:
            return bad_request("That form was too large.")
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        handle = form.get("handle", "")
        found = store.consent(handle) if handle else None
        cookie = request.cookies.get(cookie_name(handle), "") if handle else ""
        if found is None or not cookie or not same(found[0], sha256_hex(cookie)):
            return html_reply(message_page(EXPIRED_TITLE, EXPIRED_TEXT), 400)
        auth = found[1]
        client = await get_client(auth["client_id"])
        if client is None:
            return html_reply(message_page(EXPIRED_TITLE, EXPIRED_TEXT), 400)

        if form.get("decision") == "cancel":
            async with lock:
                store.drop_consent(handle)
            await persist()
            return error_redirect(o, auth["redirect_uri"], "access_denied", auth.get("state"))

        if pepper() is None:
            return html_reply(message_page(NOT_SET_UP_TITLE, NOT_SET_UP_TEXT), 503)
        access = access_from(form.get("access"))
        code = normalize_code(form.get("code", ""))
        ip_key = f"ip:{keyed('ip', client_ip(request))}"
        code_hash = keyed("code", code) if code else None
        async with lock:
            store.sweep()
            limits = [(ip_key, MAX_ATTEMPTS_PER_IP)] + ([(f"code:{code_hash}", MAX_ATTEMPTS_PER_CODE)] if code_hash else [])
            limited = any(store.hit_count(key) >= limit for key, limit in limits)
            device_id = None
            if not limited:
                for key, _ in limits:
                    store.bump(key, WINDOW_SECONDS)
                row = store.code(code_hash) if code_hash else None
                if row and row[2] > store.now() and same(row[0], code_hash):
                    store.use_code(code_hash, row[3])
                    device_id = row[1]
        await persist()
        if limited:
            return page_for(request, client, handle, TOO_MANY, access, 429, auth["redirect_uri"])
        if device_id is None:
            return page_for(request, client, handle, WRONG_CODE, access, 400, auth["redirect_uri"])

        auth_code = secrets.token_urlsafe(32)
        async with lock:
            store.drop_consent(handle)
            store.put_auth_code(
                sha256_hex(auth_code),
                {**auth, "device_id": device_id, "read_only": access == "read"},
                AUTH_CODE_TTL,
            )
        await persist()
        query = {"code": auth_code, "iss": o}
        if auth.get("state") is not None:
            query["state"] = auth["state"]
        sep = "&" if "?" in auth["redirect_uri"] else "?"
        response = RedirectResponse(f"{auth['redirect_uri']}{sep}{urlencode(query)}", status_code=302)
        response.delete_cookie(cookie_name(handle), path="/", secure=True, httponly=True, samesite="lax")
        return response

    # ---- Tokens ----------------------------------------------------------------------------

    def client_credentials(request: Request, form: dict) -> tuple[str, str | None]:
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                decoded = base64.b64decode(header[6:].strip()).decode()
                client_id, _, secret = decoded.partition(":")
                from urllib.parse import unquote

                return unquote(client_id), unquote(secret) or None
            except (ValueError, UnicodeDecodeError):
                return "", None
        return form.get("client_id", ""), form.get("client_secret")

    async def authenticate_client(request: Request, form: dict) -> tuple[str, dict] | JSONResponse:
        client_id, secret = client_credentials(request, form)
        client = await get_client(client_id) if client_id else None
        if client is None:
            return oauth_error("invalid_client", "Unknown client.", 401)
        if client.get("auth", "none") != "none":
            stored = client.get("secret_sha256")
            if not secret or not stored or not same(stored, sha256_hex(secret)):
                return oauth_error("invalid_client", "Wrong client credentials.", 401)
        return client_id, client

    async def issue_tokens(grant_id: str) -> dict:
        access = "mpa_" + secrets.token_urlsafe(32)
        refresh = "mpr_" + secrets.token_urlsafe(32)
        async with lock:
            store.add_token(sha256_hex(access), "access", grant_id, ACCESS_TTL)
            store.add_token(sha256_hex(refresh), "refresh", grant_id, REFRESH_TTL)
        await persist()
        return {"access_token": access, "token_type": "bearer", "expires_in": ACCESS_TTL, "refresh_token": refresh, "scope": SCOPE}

    @app.post("/oauth/token")
    async def token(request: Request):
        o = origin_of(request)
        raw = await request.body()
        if len(raw) > 16 * 1024:
            return oauth_error("invalid_request", "Request too large.")
        form = {k: v[0] for k, v in parse_qs(raw.decode("utf-8", "replace")).items()}
        grant_type = form.get("grant_type")

        if grant_type is None and form.get("token"):  # revocation (RFC 7009) on the same endpoint
            async with lock:
                dropped = store.drop_token(sha256_hex(form["token"]))
                if dropped and dropped[1] == "refresh":  # revoking a refresh token ends the whole link
                    store.revoke_grant(dropped[0])
            await persist()
            return json_reply({})

        authed = await authenticate_client(request, form)
        if isinstance(authed, JSONResponse):
            return authed
        client_id, _client = authed
        resource = form.get("resource")
        if resource is not None and resource.rstrip("/") not in (f"{o}/mcp", o):
            return oauth_error("invalid_target", "Unknown resource.")

        if grant_type == "authorization_code":
            async with lock:
                data = store.take_auth_code(sha256_hex(form.get("code", "")))
            await persist()
            if data is None or data["client_id"] != client_id:
                return oauth_error("invalid_grant", "The code is wrong, used or expired.")
            if form.get("redirect_uri", data["redirect_uri"]) != data["redirect_uri"]:
                return oauth_error("invalid_grant", "redirect_uri does not match.")
            verifier = form.get("code_verifier", "")
            if not (43 <= len(verifier) <= 128) or not same(b64url(hashlib.sha256(verifier.encode()).digest()), data["code_challenge"]):
                return oauth_error("invalid_grant", "PKCE check failed.")
            grant_id = secrets.token_urlsafe(16)
            async with lock:
                store.add_grant(grant_id, data["device_id"], client_id, bool(data["read_only"]))
            return json_reply(await issue_tokens(grant_id))

        if grant_type == "refresh_token":
            token_hash = sha256_hex(form.get("refresh_token", ""))
            row = store.token(token_hash, "refresh")
            grant = store.grant(row[1]) if row else None
            if row is None or grant is None or grant[2] != client_id:
                return oauth_error("invalid_grant", "The refresh token is wrong, revoked or expired.")
            if row[3] is not None and store.now() - row[3] > REFRESH_GRACE:
                # A replaced token came back late: it may have been stolen, so the link ends.
                async with lock:
                    store.revoke_grant(row[1])
                await persist()
                return oauth_error("invalid_grant", "The refresh token was already used.")
            tokens = await issue_tokens(row[1])
            async with lock:
                store.mark_replaced(token_hash)
                # At most the presented token (for its grace period) and the new one stay live.
                store.keep_only_refresh(row[1], (token_hash, sha256_hex(tokens["refresh_token"])))
            await persist()
            return json_reply(tokens)

        return oauth_error("unsupported_grant_type", "Use authorization_code or refresh_token.")

    # ---- MCP -------------------------------------------------------------------------------

    def unauthorized(request: Request, presented: bool) -> JSONResponse:
        o = origin_of(request)
        challenge = f'Bearer realm="OAuth", resource_metadata="{o}/.well-known/oauth-protected-resource/mcp", scope="{SCOPE}"'
        if presented:
            challenge += ', error="invalid_token"'
        return json_reply({"error": "invalid_token" if presented else "unauthorized"}, 401, {"www-authenticate": challenge})

    def reply_response(reply: Reply) -> Response:
        body = None if reply.status in NULL_BODY_STATUS or reply.body == "" else reply.body
        return Response(body, status_code=reply.status, headers=reply.headers)

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE", "PUT", "PATCH"])
    async def mcp(request: Request):
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            return unauthorized(request, False)
        row = store.token(sha256_hex(header[7:].strip()), "access")
        grant = store.grant(row[1]) if row else None
        if grant is None:
            return unauthorized(request, True)
        if request.method not in ("POST", "DELETE"):
            return json_reply({"error": "method not allowed"}, 405, {"allow": "POST, DELETE"})
        device_id, read_only = grant[1], bool(grant[3])
        if int(request.headers.get("content-length") or 0) > MAX_BODY_BYTES:
            return json_reply({"error": "request too large"}, 413)
        raw = await request.body()
        if len(raw) > MAX_BODY_BYTES:
            return json_reply({"error": "request too large"}, 413)
        body = raw.decode("utf-8", "replace")

        if read_only and request.method == "POST":
            blocked = gates.block_write(body)
            if blocked is None:
                return Response(None, status_code=202)
            if blocked is not gates._MISSING:
                return json_reply(blocked)

        headers = {name: request.headers[name] for name in FORWARDED if name in request.headers}
        reply = await hub.forward(device_id, request.method, headers, body)
        if reply.status >= 400 and request.method == "POST":
            readable = gates.tool_result_for(reply.status, body)
            if readable is not None:
                return json_reply(readable)
        if read_only and request.method == "POST" and reply.status == 200 and gates.asks_tool_list(body):
            reply = Reply(reply.status, reply.headers, gates.filter_tool_list(reply.body, body))
        return reply_response(reply)

    # ---- Devices ---------------------------------------------------------------------------

    @app.post("/device/register")
    async def register_device(request: Request):
        if pepper() is None:
            return json_reply({"error": "relay is not set up: PAIRING_PEPPER is missing"}, 503)
        if not await take(f"register:{keyed('ip', client_ip(request))}", REGISTRATIONS_PER_IP):
            return json_reply({"error": "too many registrations, try again in 10 minutes"}, 429, {"retry-after": "600"})
        try:
            body = await request.json()
        except ValueError:
            body = {}
        name = body.get("name") if isinstance(body, dict) else None
        if name is not None and (not isinstance(name, str) or len(name) > 64):
            return json_reply({"error": "name must be a string of at most 64 characters"}, 400)
        device_id = f"dev_{secrets.token_hex(16)}"
        secret = f"{device_id}.{b64url(secrets.token_bytes(32))}"
        async with lock:
            ok = store.add_device(device_id, sha256_hex(secret))
        await persist()
        if not ok:
            return json_reply({"error": "try again"}, 503)
        return json_reply({"device_id": device_id, "secret": secret})

    @app.post("/device/pair")
    async def pair(request: Request):
        if pepper() is None:
            return json_reply({"error": "relay is not set up: PAIRING_PEPPER is missing"}, 503)
        device_id = device_from_secret(request)
        if device_id is None:
            return json_reply({"error": "unauthorized"}, 401)
        try:
            body = await request.json()
        except ValueError:
            body = None
        review = isinstance(body, dict) and body.get("review") is True
        ttl = REVIEW_TTL_SECONDS if review else CODE_TTL_SECONDS
        uses = REVIEW_USES if review else 1
        for _ in range(3):
            code = new_code()
            async with lock:
                store.sweep()
                ok = store.issue_code(device_id, keyed("code", code), ttl, uses)
            if ok:
                await persist()
                return json_reply({"code": code, "expires_in": ttl, "uses": uses})
        return json_reply({"error": "try again"}, 503)

    @app.post("/device/unlink")
    async def unlink(request: Request):
        device_id = device_from_secret(request)
        if device_id is None:
            return json_reply({"error": "unauthorized"}, 401)
        async with lock:
            revoked = store.revoke_device(device_id)
        await persist()
        return json_reply({"revoked": revoked})

    @app.get("/device/connect")
    async def connect_without_upgrade():
        return json_reply({"error": "expected a websocket upgrade"}, 426)

    @app.websocket("/device/connect")
    async def connect(websocket: WebSocket):
        device_id = device_from_secret(websocket)
        if device_id is None:
            # Modal cannot answer a WebSocket upgrade with an HTTP 401 (the client would see 403,
            # which relay_client retries as a proxy problem), so accept and close with 4401.
            await websocket.accept()
            await websocket.close(code=UNAUTHORIZED_CLOSE_CODE, reason="Unauthorized")
            return
        await websocket.accept()
        sock = await hub.attach(device_id, websocket.send_text, lambda code, reason: websocket.close(code=code, reason=reason))
        opened = time.monotonic()
        try:
            while True:
                try:
                    message = await asyncio.wait_for(websocket.receive(), 30)
                except TimeoutError:
                    if time.monotonic() - opened > MAX_SOCKET_AGE_SECONDS and not sock.pending:
                        await websocket.close(code=RECYCLE_CLOSE_CODE, reason="Reconnect")
                        break
                    continue
                if message["type"] == "websocket.disconnect":
                    break
                text = message.get("text")
                if text is None and message.get("bytes") is not None:
                    text = message["bytes"].decode("utf-8", "replace")
                if text is not None:
                    await hub.message(sock, text)
        except WebSocketDisconnect:
            pass
        finally:
            hub.detach(device_id, sock)

    return app
