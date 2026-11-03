"""Device side of the relay. See docs/CONTRACTS.md, "Relay frames".

The device dials out to the relay over a WebSocket (it never listens on the network), takes
each request frame, forwards it to the local connector at http://127.0.0.1:<port>/mcp with the
device connector token, and sends back one response frame. Bodies are never logged.

    python -m base.connector.relay_client register   # once: get a device id and secret
    python -m base.connector.relay_client pair       # print a one-time code to enter in Muse
    python -m base.connector.relay_client run        # stay connected, reconnecting with backoff
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import random
import re
import sys
import time
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import InvalidStatus, WebSocketException

from base.connector.audit import VIA_HEADER

log = logging.getLogger("base.relay")

FORWARD_HEADERS = (
    "content-type", "accept", "mcp-session-id", "mcp-protocol-version", "last-event-id", "mcp-method", "mcp-name",
)  # fmt: skip
RETURN_HEADERS = ("content-type", "mcp-session-id", "allow")
METHODS = ("POST", "GET", "DELETE")
FORWARD_TIMEOUT = 55.0  # the relay gives up at 60 s
BACKOFF_BASE = 1.0
BACKOFF_CAP = 60.0
# The relay closes a device socket with this code when a newer one connects with the same secret.
REPLACED_CLOSE_CODE = 4000


EXIT_NO_RETRY = 3  # bad or revoked device secret, or another copy took over


class RelayError(RuntimeError):
    """A relay problem with a plain message for the person at the device."""


class RelayAuthError(RelayError):
    """The relay rejected the device secret."""


class RelayReplacedError(RelayError):
    """Another client connected with this device's secret, so the relay closed this one."""


# Device registration file


def config_path(settings) -> Path:
    return Path(settings.home) / "relay.json"


def load_config(settings) -> dict | None:
    """{relay_url, device_id, secret} from relay.json, or None when not registered."""
    try:
        data = json.loads(config_path(settings).read_text())
    except FileNotFoundError:
        return None
    except ValueError:
        raise RelayError("relay.json is not valid. Run register again with --force.") from None
    if not all(isinstance(data.get(k), str) and data[k] for k in ("relay_url", "device_id", "secret")):
        raise RelayError("relay.json is incomplete. Run register again with --force.")
    return data


def save_config(settings, data: dict) -> Path:
    """Write relay.json with mode 0600 (it holds the device secret)."""
    path = config_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)
    return path


def http_base(relay_url: str) -> str:
    """The relay's https base URL. A cleartext http:// or ws:// URL is refused unless it is on
    loopback (wrangler dev): the device secret and every MCP response would cross in the clear."""
    url = relay_url.rstrip("/")
    for ws, http in (("wss://", "https://"), ("ws://", "http://")):
        if url.startswith(ws):
            url = http + url[len(ws) :]
            break
    if url.startswith("http://"):
        host = (httpx.URL(url).host or "").strip("[]")
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise RelayError("RELAY_URL must start with https:// (plain http is only for a relay on this machine).")
    return url


def connect_url(relay_url: str) -> str:
    url = http_base(relay_url)
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if url.startswith(http):
            url = ws + url[len(http) :]
    return url + "/device/connect"


async def register(
    settings,
    name: str = "Muse Privacy device",
    *,
    relay_url: str | None = None,
    force: bool = False,
    http: httpx.AsyncClient | None = None,
) -> str:
    """Register this device with the relay, store the secret in relay.json, return the device id."""
    relay_url = relay_url or settings.relay_url
    if not relay_url:
        raise RelayError("Set RELAY_URL in .env (or pass --relay-url) before registering.")
    existing = None if force else load_config(settings)
    if existing:
        raise RelayError(
            f"This device is already registered as {existing['device_id']}. Use --force to replace it."
        )
    async with _client(http, settings) as client:
        resp = await client.post(http_base(relay_url) + "/device/register", json={"name": name})
        _check(resp, "register")
        data = resp.json()
    if not (isinstance(data.get("device_id"), str) and isinstance(data.get("secret"), str)):
        raise RelayError("The relay sent an unexpected answer to register.")
    save_config(settings, {"relay_url": relay_url, "device_id": data["device_id"], "secret": data["secret"]})
    return data["device_id"]


async def pair(settings, *, review: bool = False, http: httpx.AsyncClient | None = None) -> dict:
    """Ask the relay for a pairing code: {"code": "ABCD2345", "expires_in": 600, "uses": 1}.

    With review, a review code for a connector directory's reviewers: it links up to 20 Muse
    accounts within 14 days. Use it only on a device that holds invented data.
    """
    cfg = load_config(settings)
    if cfg is None:
        raise RelayError("This device is not registered yet. Run register first.")
    async with _client(http, settings) as client:
        resp = await client.post(
            http_base(cfg["relay_url"]) + "/device/pair",
            headers={"authorization": f"Bearer {cfg['secret']}"},
            json={"review": True} if review else None,
        )
        _check(resp, "pair")
        data = resp.json()
    if not isinstance(data.get("code"), str):
        raise RelayError("The relay sent an unexpected answer to pair.")
    return {"code": data["code"], "expires_in": int(data.get("expires_in") or 600), "uses": int(data.get("uses") or 1)}


async def unlink(settings, *, http: httpx.AsyncClient | None = None) -> int:
    """Revoke every Muse link bound to this device. Returns how many were revoked.

    Use it after a pairing code was shared by mistake or a session might be in the wrong hands.
    The device stays registered; pair again to link Muse. A stolen device secret needs
    `register --force` instead, which gives the device a new id and secret.
    """
    cfg = load_config(settings)
    if cfg is None:
        raise RelayError("This device is not registered yet. Run register first.")
    async with _client(http, settings) as client:
        resp = await client.post(
            http_base(cfg["relay_url"]) + "/device/unlink", headers={"authorization": f"Bearer {cfg['secret']}"}
        )
        _check(resp, "unlink")
        data = resp.json()
    return int(data.get("revoked") or 0)


def _client(http: httpx.AsyncClient | None, settings=None):
    """Use the caller's client as is, or make one that closes when done. It uses the system or
    environment proxy unless RELAY_PROXY=direct."""
    if http is not None:
        return contextlib.nullcontext(http)
    return httpx.AsyncClient(timeout=20, trust_env=proxy_mode(settings) != "direct")


def proxy_mode(settings) -> str:
    """RELAY_PROXY from settings: "system" (the default, the system or environment proxy) or "direct"."""
    return "direct" if getattr(settings, "relay_proxy", "system") == "direct" else "system"


# The relay answers 401 for a bad device secret and never 403, so a 403 comes from Cloudflare's
# edge or a proxy on the way (error 1010, a WAF challenge): a refused connection, not a bad secret.
PROXY_HINT = "If this keeps happening on a managed network, try RELAY_PROXY=direct."


def _check(resp: httpx.Response, what: str) -> None:
    if resp.status_code == 401:
        raise RelayAuthError(
            f"The relay rejected this device during {what}. Run register again with --force."
        )
    if resp.status_code == 403:
        raise RelayError(f"Something between this device and the relay refused {what} (HTTP 403). {PROXY_HINT}")
    if resp.status_code >= 400:
        raise RelayError(f"The relay answered {resp.status_code} to {what}.")


# Frames


def backoff_delay(
    attempt: int,
    *,
    base: float = BACKOFF_BASE,
    cap: float = BACKOFF_CAP,
    rand: Callable[[], float] | None = None,
) -> float:
    """Exponential backoff with jitter: between half and all of min(cap, base * 2**attempt)."""
    ceiling = min(cap, base * 2 ** min(attempt, 16))
    return ceiling / 2 + (rand or random.random)() * ceiling / 2


async def collect_sse(lines: AsyncIterator[str]) -> str:
    """Collapse an SSE stream into one body: each event's data lines, in order, until it ends."""
    events: list[tuple[str | None, list[str]]] = []
    name: str | None = None
    data: list[str] = []
    async for line in lines:
        line = line.rstrip("\r\n")
        if line == "":
            if data:
                events.append((name, data))
            name, data = None, []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)
        elif line.startswith("event:"):
            name = line[6:].strip() or None
    if data:
        events.append((name, data))
    out = []
    for event, chunks in events:
        head = f"event: {event}\n" if event else ""
        out.append(head + "".join(f"data: {c}\n" for c in chunks) + "\n")
    return "".join(out)


def _response(frame_id: str, status: int, body: str = "", headers: dict | None = None) -> dict:
    return {"type": "response", "id": frame_id, "status": status, "headers": headers or {}, "body": body}


def _error(frame_id: str, status: int, message: str) -> dict:
    return _response(frame_id, status, json.dumps({"error": message}), {"content-type": "application/json"})


# MCP 2026-07-28 has each request repeat its method in an Mcp-Method header, and for three methods
# the tool, prompt or resource it names in Mcp-Name; the device's MCP library refuses a request
# whose headers are missing or differ from the body (HTTP 400). The relay deployed before Oct 31
# forwards neither header, so ChatGPT, which speaks 2026-07-28, got a 400 on its first call. When a
# request carries the 2026-07-28 envelope and a header did not arrive, it is filled from the body.
# Nothing between the relay and the device routes on these headers, so no check is lost; a header
# that did arrive is kept as sent, so a request that disagrees with itself is still refused.
MODERN_VERSION_KEY = "io.modelcontextprotocol/protocolVersion"
NAME_PARAMS = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
_HEADER_SAFE = re.compile(r"[\x21-\x7e]{1,256}")  # printable ASCII, no spaces: never needs encoding


def fill_method_headers(sent: dict, body: Any) -> None:
    """Add Mcp-Method and Mcp-Name from a 2026-07-28 request's body when they did not arrive."""
    if "mcp-method" in sent and "mcp-name" in sent or not isinstance(body, str):
        return
    try:
        message = json.loads(body)
    except ValueError:
        return
    params = message.get("params") if isinstance(message, dict) else None
    meta = params.get("_meta") if isinstance(params, dict) else None
    if not isinstance(meta, dict) or MODERN_VERSION_KEY not in meta:
        return  # an older protocol: these headers mean nothing to it
    method = message.get("method")
    if not isinstance(method, str) or not _HEADER_SAFE.fullmatch(method):
        return
    sent.setdefault("mcp-method", method)
    value = params.get(NAME_PARAMS.get(method, ""))
    if isinstance(value, str) and _HEADER_SAFE.fullmatch(value):
        sent.setdefault("mcp-name", value)


class RelayClient:
    """Holds the relay socket and forwards request frames to the local connector."""

    def __init__(
        self,
        *,
        relay_url: str,
        secret: str,
        connector_url: str,
        token: str,
        http: httpx.AsyncClient | None = None,
        connect: Callable[..., Any] = ws_connect,
        sleep: Callable[[float], Any] = asyncio.sleep,
        forward_timeout: float = FORWARD_TIMEOUT,
        proxy: str = "system",
    ):
        self.relay_url = relay_url
        # "system": the relay socket goes through the system or environment proxy, as websockets
        # does by default. "direct" (RELAY_PROXY=direct) bypasses it, for a proxy the relay refuses.
        self.proxy = "direct" if proxy == "direct" else "system"
        self._secret = secret
        self.connector_url = connector_url
        self._token = token
        # trust_env=False: a proxy from the environment must never sit between us and 127.0.0.1
        self.http = http or httpx.AsyncClient(
            timeout=httpx.Timeout(forward_timeout, connect=5), trust_env=False
        )
        self.connect = connect
        self.sleep = sleep
        self.forward_timeout = forward_timeout

    async def handle(self, raw: str | bytes) -> dict | None:
        """One incoming frame to the frame to send back, if any."""
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            log.warning("ignored a frame that is not JSON")
            return None
        if not isinstance(frame, dict):
            return None
        kind = frame.get("type")
        if kind == "ping":
            return {"type": "pong"}
        if kind == "request":
            return await self.forward(frame)
        return None

    async def forward(self, frame: dict) -> dict | None:
        """Send one request frame to the local MCP server and build the response frame."""
        frame_id = frame.get("id")
        if not isinstance(frame_id, str) or not frame_id:
            log.warning("ignored a request frame with no id")
            return None
        method = str(frame.get("method", "")).upper()
        started = time.perf_counter()
        if method not in METHODS:
            reply = _error(frame_id, 405, "method not allowed")
        elif method == "GET":  # v1 has no long-lived streams, so there is no GET stream to open
            reply = _response(frame_id, 405, "", {"allow": "POST, DELETE"})
        else:
            try:
                reply = await self._local(frame_id, method, frame.get("headers"), frame.get("body"))
            except Exception as exc:  # a malformed frame still gets an answer, never a dead task
                log.warning("request %s failed (%s)", frame_id, type(exc).__name__)
                reply = _error(frame_id, 500, "the device could not handle the request")
        ms = int((time.perf_counter() - started) * 1000)
        log.info("request %s %s -> %s in %d ms", frame_id, method, reply["status"], ms)
        return reply

    async def _local(self, frame_id: str, method: str, headers: Any, body: Any) -> dict:
        given = headers if isinstance(headers, dict) else {}
        sent = {k.lower(): v for k, v in given.items() if k.lower() in FORWARD_HEADERS and isinstance(v, str)}
        sent["authorization"] = f"Bearer {self._token}"
        sent[VIA_HEADER] = "relay"  # the audit labels the call; a frame can never set this itself
        fill_method_headers(sent, body)
        content = body.encode("utf-8", "replace") if isinstance(body, str) else b""
        try:
            async with asyncio.timeout(self.forward_timeout):
                async with self.http.stream(
                    method, self.connector_url, headers=sent, content=content
                ) as resp:
                    ctype = resp.headers.get("content-type", "")
                    if ctype.startswith("text/event-stream"):
                        text, ctype = await collect_sse(resp.aiter_lines()), "text/event-stream"
                    else:
                        text = (await resp.aread()).decode("utf-8", "replace")
                    out = {k: resp.headers[k] for k in RETURN_HEADERS if k in resp.headers}
                    if ctype:
                        out["content-type"] = ctype
                    return _response(frame_id, resp.status_code, text, out)
        except (TimeoutError, httpx.TimeoutException):
            return _error(frame_id, 504, "the device took too long")
        except httpx.HTTPError as exc:
            log.warning("local connector unreachable (%s)", type(exc).__name__)
            return _error(frame_id, 502, "the connector is not running on the device")

    async def session(self, ws) -> None:
        """Serve one connected socket until it closes. Requests run concurrently."""
        lock = asyncio.Lock()
        tasks: set[asyncio.Task] = set()

        async def answer(raw):
            reply = await self.handle(raw)
            if reply is not None:
                async with lock:
                    await ws.send(json.dumps(reply))

        try:
            async for raw in ws:
                task = asyncio.create_task(answer(raw))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        finally:  # a closed socket cannot carry the answers, so stop the work in flight
            for task in list(tasks):
                task.cancel()

    async def run(self, *, attempts: int | None = None) -> None:
        """Stay connected: dial out, serve, and reconnect with jittered backoff capped at 60 s.

        Stops with RelayAuthError when the relay rejects the secret (401), and with
        RelayReplacedError when the relay closes the socket because another client connected with
        the same secret. A 403 never comes from the relay itself (Cloudflare's edge or a proxy), so
        it is retried like any refused connection.

        `attempts` limits the number of connection attempts (for tests); None means forever.
        """
        url = connect_url(self.relay_url)
        failures = tried = 0
        while attempts is None or tried < attempts:
            tried += 1
            try:
                async with self.connect(
                    url,
                    additional_headers={"Authorization": f"Bearer {self._secret}"},
                    proxy=None if self.proxy == "direct" else True,
                ) as ws:
                    failures = 0
                    log.info("connected to the relay")
                    await self.session(ws)
                    log.info("relay connection closed")
            except InvalidStatus as exc:
                if exc.response.status_code == 401:
                    raise RelayAuthError(
                        "The relay rejected the device secret. Run register again."
                    ) from None
                if exc.response.status_code == 403:  # the edge or a proxy, not the relay: keep trying
                    log.warning("something between this device and the relay refused the connection (HTTP 403). %s", PROXY_HINT)
                else:
                    log.warning("relay refused the connection (HTTP %s)", exc.response.status_code)
            except (OSError, TimeoutError, WebSocketException) as exc:
                if getattr(getattr(exc, "rcvd", None), "code", None) == REPLACED_CLOSE_CODE:
                    # Reconnecting would knock the other copy off in turn, and both would keep flapping.
                    raise RelayReplacedError(
                        "Another copy of this device connected to the relay, so this one stopped. "
                        "Run one relay client per device."
                    ) from None
                log.warning("relay connection failed (%s)", type(exc).__name__)
            if attempts is not None and tried >= attempts:
                break
            delay = backoff_delay(failures)
            failures += 1
            log.info("reconnecting in %.1f s", delay)
            await self.sleep(delay)

    async def aclose(self) -> None:
        await self.http.aclose()


def client_for(settings) -> RelayClient:
    """A RelayClient for this device from relay.json and the local connector token."""
    from base.connector.server import read_or_create_token

    cfg = load_config(settings)
    if cfg is None:
        raise RelayError("This device is not registered yet. Run register first.")
    return RelayClient(
        relay_url=cfg["relay_url"],
        secret=cfg["secret"],
        connector_url=f"http://127.0.0.1:{settings.connector_port}/mcp",
        token=read_or_create_token(settings.paths.connector_token),
        proxy=proxy_mode(settings),
    )


def main(argv: list[str] | None = None) -> int:
    from base.config import load_settings

    parser = argparse.ArgumentParser(
        prog="python -m base.connector.relay_client", description=__doc__.split("\n")[0]
    )
    sub = parser.add_subparsers(dest="command", required=True)
    reg = sub.add_parser("register", help="register this device with the relay (once)")
    reg.add_argument("--name", default="Muse Privacy device", help="a label for this device")
    reg.add_argument("--relay-url", default=None, help="the relay address (default: RELAY_URL)")
    reg.add_argument("--force", action="store_true", help="replace an existing registration")
    pr = sub.add_parser("pair", help="print a one-time pairing code to enter in Muse")
    pr.add_argument(
        "--review",
        action="store_true",
        help="a code for connector reviewers: 20 links within 14 days (only on a device with invented data)",
    )
    sub.add_parser("unlink", help="revoke every Muse link to this device (pair again to reconnect)")
    sub.add_parser("run", help="stay connected to the relay")
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for noisy in ("httpx", "httpcore", "websockets"):  # they would log the relay address
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = load_settings()
    try:
        if args.command == "register":
            device_id = asyncio.run(register(settings, args.name, relay_url=args.relay_url, force=args.force))
            print(
                f"Registered this device as {device_id}. Next run: python -m base.connector.relay_client pair"
            )
        elif args.command == "unlink":
            revoked = asyncio.run(unlink(settings))
            print(f"Revoked {revoked} Muse link(s). Muse can reach this device again only after a new pairing code.")
        elif args.command == "pair":
            got = asyncio.run(pair(settings, review=args.review))
            print(f"Pairing code: {got['code']}")
            if args.review:
                days = max(1, round(got["expires_in"] / 86400))
                print(
                    f"Review code: it links up to {got['uses']} Muse accounts to this device for {days} days. "
                    "Give it only to reviewers, and only on a device with invented data. "
                    "A new pair replaces it; unlink revokes every link it made."
                )
            else:
                minutes = max(1, round(got["expires_in"] / 60))
                print(
                    f"It works once and expires in {minutes} minutes. Enter it in Muse when you connect Muse Privacy."
                )
        else:
            asyncio.run(client_for(settings).run())
    except (RelayAuthError, RelayReplacedError) as exc:
        print(str(exc), file=sys.stderr)
        return EXIT_NO_RETRY  # the unit's RestartPreventExitStatus: retrying cannot help
    except RelayError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
