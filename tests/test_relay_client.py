"""Relay client tests: frames, SSE collapse, ping/pong, reconnect backoff, register and pair.

No network: the local connector is an httpx MockTransport or the real connector app over an
in-process ASGI transport, and the relay socket is a fake. All data is invented.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from types import SimpleNamespace

import httpx
import pytest
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosedError, InvalidStatus
from websockets.frames import Close
from websockets.http11 import Response

from base.connector import relay_client as rc
from base.connector.relay_client import (
    RelayAuthError,
    RelayClient,
    RelayError,
    RelayReplacedError,
    backoff_delay,
    collect_sse,
)
from base.connector.server import connector_app

LOCAL = "http://127.0.0.1:8790/mcp"
RELAY = "https://relay.example.org"
REAL_A = "Zephyrine Quoll"


def make_client(handler=None, *, http=None, **kw) -> RelayClient:
    if http is None:
        http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return RelayClient(
        relay_url=RELAY, secret="device-secret", connector_url=LOCAL, token="local-token", http=http, **kw
    )


def request_frame(frame_id="f-1", method="POST", body='{"jsonrpc":"2.0","id":1,"method":"ping"}', **headers):
    base = {"content-type": "application/json", "accept": "application/json, text/event-stream"}
    return {"type": "request", "id": frame_id, "method": method, "headers": {**base, **headers}, "body": body}


# Pure helpers


def test_backoff_is_jittered_exponential_and_capped():
    highs = [backoff_delay(a, rand=lambda: 1.0) for a in range(12)]
    lows = [backoff_delay(a, rand=lambda: 0.0) for a in range(12)]
    assert highs[:4] == [1.0, 2.0, 4.0, 8.0]
    assert lows[:3] == [0.5, 1.0, 2.0]
    assert max(highs) == 60.0 and highs[-1] == 60.0
    assert all(lo <= hi for lo, hi in zip(lows, highs, strict=True))
    assert backoff_delay(10_000) <= 60.0


async def lines(text: str):
    for line in text.split("\n"):
        yield line


async def test_collect_sse_keeps_only_event_data():
    stream = (
        ": keepalive\n\n"
        'event: message\nid: 7\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        "retry: 1000\n\n"
        'event: message\ndata: {"jsonrpc":"2.0",\ndata:"id":1,"result":{}}\n\n'
        "data: tail-without-blank-line"
    )
    body = await collect_sse(lines(stream))
    assert body == (
        'event: message\ndata: {"jsonrpc":"2.0","method":"notifications/progress"}\n\n'
        'event: message\ndata: {"jsonrpc":"2.0",\ndata: "id":1,"result":{}}\n\n'
        "data: tail-without-blank-line\n\n"
    )


def test_urls():
    assert rc.connect_url("https://relay.example.org/") == "wss://relay.example.org/device/connect"
    assert rc.connect_url("http://127.0.0.1:8787") == "ws://127.0.0.1:8787/device/connect"
    assert rc.connect_url("wss://relay.example.org") == "wss://relay.example.org/device/connect"
    assert rc.http_base("wss://relay.example.org") == "https://relay.example.org"


# Frame handling against a mock local connector


async def test_request_forwarded_with_local_token_and_sse_collapsed():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["method"] = request.method
        seen["headers"] = dict(request.headers)
        seen["body"] = request.content.decode()
        sse = 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n'
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream", "mcp-session-id": "s-1", "x-other": "no"},
            text=sse,
        )

    client = make_client(handler)
    frame = request_frame(
        **{
            "mcp-session-id": "s-1",
            "mcp-protocol-version": "2025-06-18",
            "authorization": "Bearer relay-x",
            "cookie": "c",
        }
    )
    reply = await client.handle(json.dumps(frame))
    assert seen["url"] == LOCAL and seen["method"] == "POST"
    assert seen["headers"]["authorization"] == "Bearer local-token"  # never the relay's header
    assert "cookie" not in seen["headers"]
    assert (
        seen["headers"]["mcp-session-id"] == "s-1" and seen["headers"]["mcp-protocol-version"] == "2025-06-18"
    )
    assert seen["body"] == frame["body"]
    assert reply == {
        "type": "response",
        "id": "f-1",
        "status": 200,
        "headers": {"content-type": "text/event-stream", "mcp-session-id": "s-1"},
        "body": 'event: message\ndata: {"jsonrpc":"2.0","id":1,"result":{}}\n\n',
    }


async def test_json_response_passes_through():
    def handler(request):
        return httpx.Response(202, headers={"content-type": "application/json"}, text='{"ok":true}')

    reply = await make_client(handler).forward(request_frame(frame_id="f-2"))
    assert reply["status"] == 202 and reply["body"] == '{"ok":true}'
    assert reply["headers"] == {"content-type": "application/json"}


async def test_delete_is_forwarded_with_empty_body():
    seen = {}

    def handler(request):
        seen["method"], seen["body"] = request.method, request.content
        return httpx.Response(405, headers={"allow": "POST"})

    reply = await make_client(handler).forward(request_frame(method="DELETE", body=None))
    assert seen == {"method": "DELETE", "body": b""}
    assert reply["status"] == 405 and reply["headers"]["allow"] == "POST"


async def test_get_is_answered_locally_with_405():
    def handler(request):
        raise AssertionError("GET must not reach the connector in v1")

    reply = await make_client(handler).forward(request_frame(method="GET", body=""))
    assert reply["status"] == 405 and reply["headers"] == {"allow": "POST, DELETE"}


async def test_ping_pong_and_ignored_frames():
    client = make_client(lambda r: httpx.Response(500))
    assert await client.handle('{"type": "ping"}') == {"type": "pong"}
    assert await client.handle('{"type": "pong"}') is None
    assert await client.handle("not json") is None
    assert await client.handle("[1, 2]") is None
    assert await client.handle('{"type": "request", "method": "POST"}') is None  # no id
    bad = await client.handle(json.dumps(request_frame(method="PUT")))
    assert bad["status"] == 405


async def test_connector_down_gives_502_without_details():
    def handler(request):
        raise httpx.ConnectError(f"refused while sending {REAL_A}")

    reply = await make_client(handler).forward(request_frame())
    assert reply["status"] == 502
    assert REAL_A not in reply["body"] and json.loads(reply["body"]) == {
        "error": "the connector is not running on the device"
    }


async def test_slow_connector_gives_504():
    async def handler(request):
        await asyncio.sleep(5)
        return httpx.Response(200)

    reply = await make_client(handler, forward_timeout=0.05).forward(request_frame())
    assert reply["status"] == 504


async def test_bodies_never_logged(caplog):
    caplog.set_level("DEBUG", logger="base.relay")

    def handler(request):
        return httpx.Response(
            200, headers={"content-type": "application/json"}, text=f'{{"name":"{REAL_A}"}}'
        )

    await make_client(handler).forward(request_frame(body=f'{{"q":"{REAL_A}"}}'))
    assert caplog.records and REAL_A not in caplog.text and "device-secret" not in caplog.text


# Socket session and reconnects


class FakeSocket:
    """Yields scripted frames, then stays open until `expect` frames were sent."""

    def __init__(self, frames, expect):
        self.frames = [json.dumps(f) if isinstance(f, dict) else f for f in frames]
        self.sent: list[dict] = []
        self.expect = expect
        self.done = asyncio.Event()

    async def send(self, text):
        self.sent.append(json.loads(text))
        if len(self.sent) >= self.expect:
            self.done.set()

    def __aiter__(self):
        return self._iter()

    async def _iter(self):
        for frame in self.frames:
            yield frame
        if self.expect:
            await asyncio.wait_for(self.done.wait(), 5)


async def test_session_answers_pings_and_requests_concurrently():
    async def handler(request):
        body = json.loads(request.content)
        if body["id"] == 1:
            await asyncio.sleep(0.05)  # the slow one must not block the other
        return httpx.Response(
            200, headers={"content-type": "application/json"}, text=json.dumps({"id": body["id"]})
        )

    ws = FakeSocket(
        [request_frame("slow", body='{"id": 1}'), {"type": "ping"}, request_frame("fast", body='{"id": 2}')],
        expect=3,
    )
    await make_client(handler).session(ws)
    assert ws.sent[0] == {"type": "pong"}
    assert [f["id"] for f in ws.sent[1:]] == ["fast", "slow"]
    assert json.loads(ws.sent[2]["body"]) == {"id": 1}


class FakeConnect:
    """Stands in for websockets' connect(): each attempt raises or opens a FakeSocket."""

    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls: list[tuple[str, dict]] = []
        self.proxies: list = []

    def __call__(self, url, additional_headers=None, proxy=True):
        self.calls.append((url, additional_headers))
        self.proxies.append(proxy)
        outcome = self.outcomes.pop(0)
        connect = self

        class Ctx:
            async def __aenter__(self):
                if isinstance(outcome, BaseException):
                    raise outcome
                return outcome

            async def __aexit__(self, *exc):
                return False

        connect.last = Ctx
        return Ctx()


def rejected(status: int) -> InvalidStatus:
    return InvalidStatus(Response(status, "nope", Headers()))


async def test_run_reconnects_with_backoff_after_failures(monkeypatch):
    monkeypatch.setattr(rc.random, "random", lambda: 1.0)
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    ws = FakeSocket([{"type": "ping"}], expect=1)
    connect = FakeConnect([OSError("down"), rejected(503), ws, OSError("down again")])
    client = make_client(lambda r: httpx.Response(200), connect=connect, sleep=sleep)
    await client.run(attempts=4)
    assert [c[0] for c in connect.calls] == ["wss://relay.example.org/device/connect"] * 4
    assert connect.calls[0][1] == {"Authorization": "Bearer device-secret"}
    assert ws.sent == [{"type": "pong"}]
    assert sleeps == [1.0, 2.0, 1.0]  # backoff resets after a connection that worked


async def test_run_stops_when_the_relay_rejects_the_secret():
    connect = FakeConnect([rejected(401)])
    client = make_client(lambda r: httpx.Response(200), connect=connect, sleep=lambda s: None)
    with pytest.raises(RelayAuthError):
        await client.run()


async def test_a_403_from_the_edge_or_a_proxy_is_retried_not_treated_as_a_bad_secret(monkeypatch, caplog):
    # The relay answers 401 for a bad secret and never 403: a 403 is Cloudflare's edge or a proxy
    # (error 1010), so the client keeps backing off instead of stopping for good with exit 3.
    monkeypatch.setattr(rc.random, "random", lambda: 1.0)
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    ws = FakeSocket([{"type": "ping"}], expect=1)
    connect = FakeConnect([rejected(403), rejected(403), ws])
    client = make_client(lambda r: httpx.Response(200), connect=connect, sleep=sleep)
    with caplog.at_level("WARNING"):
        await client.run(attempts=3)
    assert len(connect.calls) == 3 and ws.sent == [{"type": "pong"}] and sleeps == [1.0, 2.0]
    assert "RELAY_PROXY=direct" in caplog.text
    with pytest.raises(rc.RelayError) as err:
        rc._check(httpx.Response(403), "pair")
    assert not isinstance(err.value, RelayAuthError) and "RELAY_PROXY=direct" in str(err.value)
    with pytest.raises(RelayAuthError):
        rc._check(httpx.Response(401), "pair")


class ClosedSocket:
    """A socket the relay closes with `code` as soon as it is read."""

    def __init__(self, code: int):
        self.code = code

    def __aiter__(self):
        return self

    async def __anext__(self):
        raise ConnectionClosedError(Close(self.code, "closed by the relay"), None)


async def test_run_stops_when_another_copy_replaces_it():
    sleeps: list[float] = []

    async def sleep(seconds):
        sleeps.append(seconds)

    connect = FakeConnect([ClosedSocket(4000), OSError("never reached")])
    client = make_client(lambda r: httpx.Response(200), connect=connect, sleep=sleep)
    with pytest.raises(RelayReplacedError, match="Another copy of this device"):
        await client.run()
    assert len(connect.calls) == 1 and sleeps == []


async def test_run_reconnects_after_other_close_codes():
    async def sleep(seconds):
        pass

    connect = FakeConnect([ClosedSocket(1011), ClosedSocket(1006)])
    client = make_client(lambda r: httpx.Response(200), connect=connect, sleep=sleep)
    await client.run(attempts=2)
    assert len(connect.calls) == 2


# Through the real connector app


def fake_services(settings):
    released = []

    def release(tool, request, raw_text, result_kind, **kw):
        released.append(raw_text)
        return SimpleNamespace(
            status="shared", text=raw_text.replace(REAL_A, "[Student A]"), approval_id=None
        )

    from base.workspace.jobs import JobStore

    docs = [SimpleNamespace(id="d1", path=f"essays/{REAL_A} essay.md", title=f"{REAL_A} essay")]
    return SimpleNamespace(
        settings=settings,
        egress=SimpleNamespace(release=release),
        jobs=JobStore(settings.paths.db),
        index=SimpleNamespace(list=lambda folder: docs),
        tagmap={},
        audit=None,
        released=released,
    )


async def test_frames_reach_the_real_connector(settings, monkeypatch):
    from base.connector import server

    monkeypatch.setattr(server, "restore_tags", lambda services, text: text)
    services = fake_services(settings)
    app = connector_app(services, token="local-token")
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        client = make_client(http=http)
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        }
        reply = await client.forward(request_frame("f-init", body=json.dumps(init)))
        assert reply["status"] == 200 and reply["headers"]["content-type"].startswith("application/json")
        assert json.loads(reply["body"])["result"]["serverInfo"]["name"] == "muse-privacy"

        call = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "list_documents", "arguments": {}},
        }
        reply = await client.forward(
            request_frame("f-call", body=json.dumps(call), **{"mcp-protocol-version": "2025-06-18"})
        )
        result = json.loads(reply["body"])["result"]
        assert "[Student A] essay" in result["content"][0]["text"] and REAL_A not in reply["body"]
        assert services.released and REAL_A in services.released[0]

        wrong = make_client(http=http)
        wrong._token = "not-the-token"
        assert (await wrong.forward(request_frame("f-bad")))["status"] == 401
        await http.aclose()


MODERN = "2026-07-28"
ENVELOPE = {
    "io.modelcontextprotocol/protocolVersion": MODERN,
    "io.modelcontextprotocol/clientCapabilities": {},
    "io.modelcontextprotocol/clientInfo": {"name": "t", "version": "1"},
}


def modern(method, frame_id, **params):
    body = {"jsonrpc": "2.0", "id": frame_id, "method": method, "params": {**params, "_meta": ENVELOPE}}
    return request_frame(frame_id, body=json.dumps(body), **{"mcp-protocol-version": MODERN})


async def test_a_2026_request_works_when_the_relay_drops_its_method_headers(settings, monkeypatch):
    # ChatGPT speaks 2026-07-28; the deployed relay forwards neither Mcp-Method nor Mcp-Name, and
    # the device's MCP library answers 400 without them. The relay client fills them from the body.
    from base.connector import server

    monkeypatch.setattr(server, "restore_tags", lambda services, text: text)
    services = fake_services(settings)
    app = connector_app(services, token="local-token")
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        client = make_client(http=http)
        listed = await client.forward(modern("tools/list", "f-list"))
        assert listed["status"] == 200 and "list_documents" in listed["body"]
        called = await client.forward(modern("tools/call", "f-call", name="list_documents", arguments={}))
        assert called["status"] == 200 and "[Student A] essay" in called["body"] and REAL_A not in called["body"]
        # A header that did arrive is kept, so a request that disagrees with itself is still refused.
        lying = modern("tools/call", "f-lie", name="list_documents", arguments={})
        lying["headers"]["mcp-name"] = "search_documents"
        assert (await client.forward(lying))["status"] == 400
        await http.aclose()


def test_method_headers_are_filled_only_for_2026_requests():
    old = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_documents"}})
    sent = {}
    rc.fill_method_headers(sent, old)
    assert sent == {}
    new = json.loads(modern("tools/call", "x", name="list_documents")["body"])
    sent = {"mcp-method": "tools/call"}
    rc.fill_method_headers(sent, json.dumps(new))
    assert sent == {"mcp-method": "tools/call", "mcp-name": "list_documents"}
    new["params"]["name"] = "bad name\r\nx-injected: 1"
    sent = {}
    rc.fill_method_headers(sent, json.dumps(new))
    assert sent == {"mcp-method": "tools/call"}  # a value a header cannot carry plainly is left out
    for junk in ("not json", "[]", '{"params": 3}', None):
        rc.fill_method_headers(sent := {}, junk)
        assert sent == {}


# register and pair


def relay_handler(seen):
    def handler(request):
        seen.append((request.method, request.url.path, request.headers.get("authorization"), request.content))
        if request.url.path == "/device/register":
            return httpx.Response(200, json={"device_id": "dev-123", "secret": "s3cret-value"})
        if request.url.path == "/device/pair":
            if request.headers.get("authorization") != "Bearer s3cret-value":
                return httpx.Response(401, json={"error": "unauthorized"})
            return httpx.Response(200, json={"code": "ABCD2345", "expires_in": 600})
        return httpx.Response(404)

    return handler


async def test_register_stores_secret_0600_then_pair(settings):
    seen = []
    http = httpx.AsyncClient(transport=httpx.MockTransport(relay_handler(seen)))
    device_id = await rc.register(settings, "Test laptop", relay_url=RELAY, http=http)
    assert device_id == "dev-123"
    path = settings.home / "relay.json"
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert json.loads(path.read_text()) == {
        "relay_url": RELAY,
        "device_id": "dev-123",
        "secret": "s3cret-value",
    }
    assert seen[0][:2] == ("POST", "/device/register") and json.loads(seen[0][3]) == {"name": "Test laptop"}

    with pytest.raises(RelayError, match="already registered"):
        await rc.register(settings, relay_url=RELAY, http=http)
    assert await rc.register(settings, relay_url=RELAY, http=http, force=True) == "dev-123"

    got = await rc.pair(settings, http=http)
    assert got == {"code": "ABCD2345", "expires_in": 600, "uses": 1}
    assert seen[-1][:3] == ("POST", "/device/pair", "Bearer s3cret-value")
    assert not seen[-1][3]

    await rc.pair(settings, review=True, http=http)
    assert json.loads(seen[-1][3]) == {"review": True}


async def test_register_needs_a_relay_url(settings):
    with pytest.raises(RelayError, match="RELAY_URL"):
        await rc.register(settings)


async def test_pair_before_register_and_with_a_bad_secret(settings):
    with pytest.raises(RelayError, match="not registered"):
        await rc.pair(settings)
    rc.save_config(settings, {"relay_url": RELAY, "device_id": "dev-1", "secret": "stale"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(relay_handler([])))
    with pytest.raises(RelayAuthError):
        await rc.pair(settings, http=http)


def test_cli_pair_prints_code_and_expiry(settings, monkeypatch, capsys):
    async def fake_pair(s, review=False):
        return {"code": "WXYZ6789", "expires_in": 600, "uses": 1}

    monkeypatch.setattr(rc, "pair", fake_pair)
    monkeypatch.setattr("base.config.load_settings", lambda: settings)
    assert rc.main(["pair"]) == 0
    out = capsys.readouterr().out
    assert "Pairing code: WXYZ6789" in out and "10 minutes" in out and "—" not in out


def test_cli_pair_review_says_how_long_and_for_whom(settings, monkeypatch, capsys):
    asked = []

    async def fake_pair(s, review=False):
        asked.append(review)
        return {"code": "WXYZ6789", "expires_in": 14 * 86400, "uses": 20}

    monkeypatch.setattr(rc, "pair", fake_pair)
    monkeypatch.setattr("base.config.load_settings", lambda: settings)
    assert rc.main(["pair", "--review"]) == 0
    out = capsys.readouterr().out
    assert asked == [True]
    assert "20 Muse accounts" in out and "14 days" in out and "invented data" in out and "—" not in out


def test_cli_reports_errors_plainly(settings, monkeypatch, capsys):
    monkeypatch.setattr("base.config.load_settings", lambda: settings)
    assert rc.main(["run"]) == 1
    assert "not registered" in capsys.readouterr().err


def test_client_for_uses_local_connector_and_token(settings):
    rc.save_config(settings, {"relay_url": RELAY, "device_id": "dev-1", "secret": "sec"})
    client = rc.client_for(settings)
    assert client.connector_url == f"http://127.0.0.1:{settings.connector_port}/mcp"
    assert client._token == settings.paths.connector_token.read_text().strip()


async def test_malformed_request_frame_still_gets_an_answer():
    seen = []

    def handler(request):
        seen.append(dict(request.headers))
        return httpx.Response(400, headers={"content-type": "application/json"}, text='{"error":"bad"}')

    frame = {
        "type": "request",
        "id": "f-odd",
        "method": "post",
        "headers": ["not", "a", "dict"],
        "body": "\ud800",
    }
    reply = await make_client(handler).handle(json.dumps(frame))
    assert reply["id"] == "f-odd" and reply["status"] == 400
    assert seen[0]["authorization"] == "Bearer local-token"


def test_default_local_client_ignores_env_proxies():
    client = RelayClient(relay_url=RELAY, secret="s", connector_url=LOCAL, token="t")
    assert client.http.trust_env is False


# Proxies on the way to the relay (GAP-9): the system proxy by default, RELAY_PROXY=direct bypasses it


@pytest.mark.parametrize(("mode", "proxy"), [("system", True), ("direct", None)])
async def test_relay_socket_uses_the_system_proxy_unless_direct(mode, proxy):
    connect = FakeConnect([OSError("down")])
    client = make_client(lambda r: httpx.Response(200), connect=connect, sleep=lambda s: None, proxy=mode)
    await client.run(attempts=1)
    assert connect.proxies == [proxy]


def test_relay_proxy_setting_and_client_for(settings, monkeypatch):
    from base.config import load_settings

    assert settings.relay_proxy == "system" and rc.proxy_mode(settings) == "system"
    direct = load_settings({"BASE_HOME": str(settings.home), "RELAY_PROXY": "direct"})
    assert direct.relay_proxy == "direct" and rc.proxy_mode(direct) == "direct"
    with pytest.raises(ValueError, match="RELAY_PROXY"):
        load_settings({"BASE_HOME": str(settings.home), "RELAY_PROXY": "socks"})
    rc.save_config(direct, {"relay_url": RELAY, "device_id": "dev-1", "secret": "sec"})
    assert rc.client_for(direct).proxy == "direct" and rc.client_for(settings).proxy == "system"


@pytest.mark.parametrize(("mode", "trusted"), [("system", True), ("direct", False)])
async def test_pair_and_unlink_clients_follow_relay_proxy(settings, mode, trusted):
    async with rc._client(None, SimpleNamespace(relay_proxy=mode)) as http:
        assert http.trust_env is trusted
    async with rc._client(None) as http:  # no settings: the system proxy, as before
        assert http.trust_env is True


@pytest.mark.parametrize("error", [RelayAuthError("bad secret"), rc.RelayReplacedError("another copy")])
def test_cli_exits_3_when_retrying_cannot_help(settings, monkeypatch, error):
    # Review device F9: the unit sets RestartPreventExitStatus=3, so a revoked secret or a second
    # copy does not restart forever every 10 s.
    class Dead:
        async def run(self):
            raise error

    monkeypatch.setattr("base.config.load_settings", lambda: settings)
    monkeypatch.setattr(rc, "client_for", lambda s: Dead())
    assert rc.main(["run"]) == rc.EXIT_NO_RETRY == 3


def test_cleartext_relay_urls_are_refused_off_loopback():
    # Review SEC-8: the device secret and every response would cross in the clear.
    for url in ("http://relay.example.org", "ws://relay.example.org"):
        with pytest.raises(rc.RelayError):
            rc.connect_url(url)
    assert rc.connect_url("http://127.0.0.1:8787") == "ws://127.0.0.1:8787/device/connect"
    assert rc.connect_url("https://relay.example.org") == "wss://relay.example.org/device/connect"


async def test_unlink_revokes_links_with_the_device_secret(settings):
    # Review SEC-6: a way to cut Muse off without registering the device again.
    seen = []

    def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.headers.get("authorization") != "Bearer sec":
            return httpx.Response(401, json={"error": "unauthorized"})
        return httpx.Response(200, json={"revoked": 2})

    rc.save_config(settings, {"relay_url": RELAY, "device_id": "dev-1", "secret": "sec"})
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    assert await rc.unlink(settings, http=http) == 2
    assert seen == [("/device/unlink", "Bearer sec")]


# Relay or local in the audit (the connector reads the relay client's header)


async def test_forwarded_calls_say_relay_and_a_frame_cannot_set_it():
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get_list("x-muse-privacy-via"))
        return httpx.Response(200, headers={"content-type": "application/json"}, text="{}")

    client = make_client(handler)
    await client.handle(json.dumps(request_frame()))
    injected = request_frame("f-2", **{"x-muse-privacy-via": "local", "X-Muse-Privacy-Via": "local"})
    await client.handle(json.dumps(injected))
    assert seen == [["relay"], ["relay"]]


async def test_relayed_calls_are_audited_as_relay(settings, monkeypatch):
    from base.connector import server
    from base.connector.audit import Audit

    monkeypatch.setattr(server, "restore_tags", lambda services, text: text)
    services = fake_services(settings)
    services.audit = Audit(settings.paths.db)
    app = connector_app(services, token="local-token")
    async with app.router.lifespan_context(app):
        http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app))
        client = make_client(http=http)
        init = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "Muse App", "version": "1"},
            },
        }
        await client.forward(request_frame("f-init", body=json.dumps(init)))
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {"name": "list_documents", "arguments": {}}}
        await client.forward(request_frame("f-call", body=json.dumps(call), **{"mcp-protocol-version": "2025-06-18"}))
        await http.aclose()
    rows = [(r["tool"], r["outcome"], r["via"]) for r in reversed(services.audit.recent(5))]
    assert rows == [("initialize", "muse_app", "relay"), ("list_documents", "shared", "relay")]
