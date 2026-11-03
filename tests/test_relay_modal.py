"""The Modal relay (relay_modal/): the same contract as relay/ (docs/CONTRACTS.md, "Relay"), driven
the way Muse, Claude, Codex and the device drive it. Invented data only; no network."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import threading
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from relay_modal import app as relay
from relay_modal.app import Settings, create_app
from relay_modal.gates import OFFLINE_TEXT, read_only_text
from relay_modal.hub import Hub
from relay_modal.store import Store

ORIGIN = "https://relay.test"
PEPPER = "p" * 40
REDIRECT = "http://127.0.0.1:4567/callback"
CALL = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_documents", "arguments": {}}}
WRITE = {"jsonrpc": "2.0", "id": 8, "method": "tools/call", "params": {"name": "run_dreaming", "arguments": {}}}
TOOLS = [{"name": n} for n in ("list_documents", "search_documents", "ask_device", "get_result", "dreaming_report", "run_private_task", "save_on_device", "run_dreaming")]


@pytest.fixture
def client(tmp_path):
    store = Store(tmp_path / "relay.db")
    app = create_app(store, Settings(pepper=PEPPER, origin=ORIGIN), hub=Hub(timeout_seconds=2))
    with TestClient(app, base_url=ORIGIN) as c:
        c.store = store
        yield c


def register(c):
    r = c.post("/device/register", json={"name": "Laptop"})
    assert r.status_code == 200
    return r.json()


def pair(c, secret, review=False):
    r = c.post("/device/pair", headers={"authorization": f"Bearer {secret}"}, json={"review": True} if review else {})
    assert r.status_code == 200
    return r.json()


def pkce():
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    return verifier, challenge


def new_client(c, redirect=REDIRECT, method="none"):
    r = c.post("/oauth/register", json={"redirect_uris": [redirect], "client_name": "Test App", "token_endpoint_auth_method": method})
    assert r.status_code == 201
    return r.json()


def start(c, client_id, challenge, redirect=REDIRECT, **extra):
    params = {"response_type": "code", "client_id": client_id, "redirect_uri": redirect, "code_challenge": challenge,
              "code_challenge_method": "S256", "state": "st", "scope": "mcp offline_access", "resource": f"{ORIGIN}/mcp", **extra}
    page = c.get("/authorize", params=params, follow_redirects=False)
    return page, params


def handle_of(page):
    return re.search(r'name="handle" value="([^"]+)"', page.text).group(1)


def submit(c, page, params, code, access="full", decision="link"):
    return c.post("/authorize", params=params, data={"handle": handle_of(page), "code": code, "access": access, "decision": decision}, follow_redirects=False)


def link(c, secret, access="full", code=None):
    """A whole link, as an app does it: register, authorize page, code, token. Returns tokens."""
    reg = new_client(c)
    verifier, challenge = pkce()
    page, params = start(c, reg["client_id"], challenge)
    assert page.status_code == 200
    code = code or pair(c, secret)["code"]
    r = submit(c, page, params, code, access)
    assert r.status_code == 302, r.text
    query = parse_qs(urlsplit(r.headers["location"]).query)
    assert query["state"] == ["st"] and query["iss"] == [ORIGIN]
    tok = c.post("/oauth/token", data={"grant_type": "authorization_code", "code": query["code"][0], "redirect_uri": REDIRECT,
                                       "client_id": reg["client_id"], "code_verifier": verifier, "resource": f"{ORIGIN}/mcp"})
    assert tok.status_code == 200, tok.text
    return {**tok.json(), "client_id": reg["client_id"]}


def mcp(c, token, body, headers=None):
    return c.post("/mcp", headers={"authorization": f"Bearer {token}", "content-type": "application/json",
                                   "mcp-method": "tools/call", "mcp-name": "list_documents", **(headers or {})}, content=json.dumps(body))


class FakeDevice:
    """Connects like base/connector/relay_client.py and answers each request frame."""

    def __init__(self, c, secret, answer):
        self.frames = []
        self.cm = c.websocket_connect("/device/connect", headers={"authorization": f"Bearer {secret}"})
        self.ws = self.cm.__enter__()
        self.answer = answer
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()

    def loop(self):
        try:
            while True:
                frame = json.loads(self.ws.receive_text())
                if frame.get("type") != "request":
                    continue
                self.frames.append(frame)
                status, body = self.answer(frame)
                self.ws.send_text(json.dumps({"type": "response", "id": frame["id"], "status": status,
                                              "headers": {"content-type": "application/json", "set-cookie": "x"}, "body": body}))
        except Exception:
            return

    def close(self):
        self.cm.__exit__(None, None, None)


def echo_tools(frame):
    request = json.loads(frame["body"])
    if request.get("method") == "tools/list":
        return 200, json.dumps({"jsonrpc": "2.0", "id": request["id"], "result": {"tools": TOOLS}})
    return 200, json.dumps({"jsonrpc": "2.0", "id": request.get("id"), "result": {"content": [{"type": "text", "text": "24 documents"}]}})


def test_discovery_matches_the_cloudflare_relay(client):
    meta = client.get("/.well-known/oauth-authorization-server").json()
    assert meta["issuer"] == ORIGIN and meta["registration_endpoint"] == f"{ORIGIN}/oauth/register"
    assert meta["code_challenge_methods_supported"] == ["S256"] and meta["client_id_metadata_document_supported"] is True
    assert meta["authorization_response_iss_parameter_supported"] is True and meta["scopes_supported"] == ["mcp", "offline_access"]
    for path in ("/.well-known/oauth-protected-resource", "/.well-known/oauth-protected-resource/mcp"):
        pr = client.get(path).json()
        assert pr == {"resource": f"{ORIGIN}/mcp", "authorization_servers": [ORIGIN], "scopes_supported": ["mcp"],
                      "bearer_methods_supported": ["header"], "resource_name": "Muse Privacy"}
    r = client.post("/mcp")
    assert r.status_code == 401
    assert r.headers["www-authenticate"] == f'Bearer realm="OAuth", resource_metadata="{ORIGIN}/.well-known/oauth-protected-resource/mcp", scope="mcp"'
    assert 'error="invalid_token"' in client.post("/mcp", headers={"authorization": "Bearer nope"}).headers["www-authenticate"]


def test_full_link_and_tool_call_reach_the_device(client):
    device = register(client)
    tokens = link(client, device["secret"])
    fake = FakeDevice(client, device["secret"], echo_tools)
    try:
        r = mcp(client, tokens["access_token"], CALL)
        assert r.status_code == 200 and "24 documents" in r.text
        assert "set-cookie" not in r.headers  # only content-type and mcp-session-id come back
        frame = fake.frames[0]
        assert frame["method"] == "POST" and json.loads(frame["body"]) == CALL
        assert frame["headers"]["mcp-method"] == "tools/call" and frame["headers"]["mcp-name"] == "list_documents"
        assert "authorization" not in frame["headers"]
        assert client.get("/mcp", headers={"authorization": f"Bearer {tokens['access_token']}"}).status_code == 405
    finally:
        fake.close()


def test_secrets_and_codes_are_stored_only_as_hashes(client):
    device = register(client)
    code = pair(client, device["secret"])["code"]
    dump = "\n".join(client.store.db.iterdump())
    assert device["secret"] not in dump and code not in dump


def test_offline_device_gets_a_readable_tool_result(client):
    device = register(client)
    tokens = link(client, device["secret"])
    r = mcp(client, tokens["access_token"], CALL)
    assert r.status_code == 200 and r.json()["result"]["isError"] is True
    assert r.json()["result"]["content"][0]["text"] == OFFLINE_TEXT and r.json()["id"] == 7
    init = mcp(client, tokens["access_token"], {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init.status_code == 503


def test_read_only_link_lists_and_runs_only_read_tools(client):
    device = register(client)
    tokens = link(client, device["secret"], access="read")
    fake = FakeDevice(client, device["secret"], echo_tools)
    try:
        listed = mcp(client, tokens["access_token"], {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        names = sorted(t["name"] for t in listed.json()["result"]["tools"])
        assert names == ["ask_device", "dreaming_report", "get_result", "list_documents", "search_documents"]
        blocked = mcp(client, tokens["access_token"], WRITE)
        assert blocked.json()["result"]["content"][0]["text"] == read_only_text("run_dreaming")
        assert all(json.loads(f["body"]).get("params", {}).get("name") != "run_dreaming" for f in fake.frames)
        assert mcp(client, tokens["access_token"], CALL).status_code == 200
    finally:
        fake.close()


def test_read_only_survives_a_refresh_and_refresh_tokens_rotate(client):
    device = register(client)
    tokens = link(client, device["secret"], access="read")
    first = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"]})
    assert first.status_code == 200 and first.json()["refresh_token"] != tokens["refresh_token"]
    assert mcp(client, first.json()["access_token"], WRITE).json()["result"]["isError"] is True
    # Another client cannot use it.
    other = new_client(client)
    stolen = client.post("/oauth/token", data={"grant_type": "refresh_token", "refresh_token": first.json()["refresh_token"], "client_id": other["client_id"]})
    assert stolen.status_code == 400


def test_old_refresh_token_stops_after_the_grace_period(client, monkeypatch):
    device = register(client)
    tokens = link(client, device["secret"])
    now = [client.store.now()]
    client.store.now = lambda: now[0]
    data = {"grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": tokens["client_id"]}
    assert client.post("/oauth/token", data=data).status_code == 200
    assert client.post("/oauth/token", data=data).status_code == 200  # a quick retry still works
    now[0] += relay.REFRESH_GRACE + 1
    assert client.post("/oauth/token", data=data).status_code == 400


def test_wrong_code_keeps_the_page_and_the_choice(client):
    device = register(client)
    reg = new_client(client)
    _, challenge = pkce()
    page, params = start(client, reg["client_id"], challenge)
    assert "Read only" in page.text and 'value="full" checked' in page.text
    r = submit(client, page, params, "ZZZZ2222", access="read")
    assert r.status_code == 400 and "did not work" in r.text and 'value="read" checked' in r.text
    ok = submit(client, page, params, pair(client, device["secret"])["code"])
    assert ok.status_code == 302


def test_code_is_single_use_and_review_code_links_many(client):
    device = register(client)
    code = pair(client, device["secret"])["code"]
    link(client, device["secret"], code=code)
    reg = new_client(client)
    _, challenge = pkce()
    page, params = start(client, reg["client_id"], challenge)
    assert submit(client, page, params, code).status_code == 400
    review = pair(client, device["secret"], review=True)
    assert review["uses"] == 20 and review["expires_in"] == 14 * 24 * 3600
    for _ in range(3):
        link(client, device["secret"], code=review["code"])


def test_pair_attempts_are_limited(client):
    register(client)
    reg = new_client(client)
    _, challenge = pkce()
    page, params = start(client, reg["client_id"], challenge)
    statuses = [submit(client, page, params, "ZZZZ2222").status_code for _ in range(6)]
    assert statuses[:5] == [400] * 5 and statuses[5] == 429


def test_cancel_and_bad_requests(client):
    reg = new_client(client)
    verifier, challenge = pkce()
    page, params = start(client, reg["client_id"], challenge)
    r = submit(client, page, params, "", decision="cancel")
    q = parse_qs(urlsplit(r.headers["location"]).query)
    assert q["error"] == ["access_denied"] and q["iss"] == [ORIGIN]
    assert start(client, reg["client_id"], challenge, redirect="https://evil.example/cb")[0].status_code == 400
    assert start(client, "unknown", challenge)[0].status_code == 400
    plain = start(client, reg["client_id"], challenge, code_challenge_method="plain")[0]
    assert plain.status_code == 302 and "error=invalid_request" in plain.headers["location"]
    # A loopback redirect may come back on another port (RFC 8252).
    assert start(client, reg["client_id"], challenge, redirect="http://127.0.0.1:9999/callback")[0].status_code == 200


def test_pkce_and_client_secret_are_checked(client):
    device = register(client)
    reg = new_client(client, method="client_secret_post")
    verifier, challenge = pkce()
    page, params = start(client, reg["client_id"], challenge)
    r = submit(client, page, params, pair(client, device["secret"])["code"])
    code = parse_qs(urlsplit(r.headers["location"]).query)["code"][0]
    base = {"grant_type": "authorization_code", "code": code, "redirect_uri": REDIRECT, "client_id": reg["client_id"], "code_verifier": verifier}
    assert client.post("/oauth/token", data=base).status_code == 401  # no secret
    assert client.post("/oauth/token", data={**base, "client_secret": reg["client_secret"], "code_verifier": "x" * 50}).status_code == 400
    # The code was used up by the failed try.
    assert client.post("/oauth/token", data={**base, "client_secret": reg["client_secret"]}).status_code == 400


def test_unlink_revokes_every_link(client):
    device = register(client)
    tokens = link(client, device["secret"])
    r = client.post("/device/unlink", headers={"authorization": f"Bearer {device['secret']}"})
    assert r.json() == {"revoked": 1}
    assert mcp(client, tokens["access_token"], CALL).status_code == 401


def test_wrong_device_secret_is_refused(client):
    device = register(client)
    wrong = device["secret"][:-4] + "AAAA"
    assert client.post("/device/pair", headers={"authorization": f"Bearer {wrong}"}).status_code == 401
    from starlette.testclient import WebSocketDenialResponse
    from starlette.websockets import WebSocketDisconnect

    with pytest.raises((WebSocketDenialResponse, WebSocketDisconnect)):
        with client.websocket_connect("/device/connect", headers={"authorization": f"Bearer {wrong}"}):
            pass


def test_newer_device_connection_replaces_the_old(client):
    device = register(client)
    tokens = link(client, device["secret"])
    first = FakeDevice(client, device["secret"], lambda f: (200, json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "old"}]}})))
    second = FakeDevice(client, device["secret"], lambda f: (200, json.dumps({"jsonrpc": "2.0", "id": 7, "result": {"content": [{"type": "text", "text": "new"}]}})))
    try:
        assert "new" in mcp(client, tokens["access_token"], CALL).text
    finally:
        second.close()
        first.close()


def test_client_id_metadata_document(tmp_path, monkeypatch):
    url = "https://app.example.org/oauth/client.json"
    doc = {"client_id": url, "client_name": "Example", "redirect_uris": ["https://app.example.org/cb"]}
    transport = httpx.MockTransport(lambda req: httpx.Response(200, json=doc) if str(req.url) == url else httpx.Response(404))
    store = Store(tmp_path / "relay.db")
    app = create_app(store, Settings(pepper=PEPPER, origin=ORIGIN), http=httpx.AsyncClient(transport=transport))

    monkeypatch.setattr(relay.socket, "getaddrinfo", lambda *a, **k: [(None, None, None, None, ("93.184.216.34", 443))])
    with TestClient(app, base_url=ORIGIN) as c:
        _, challenge = pkce()
        page, _ = start(c, url, challenge, redirect="https://app.example.org/cb")
        assert page.status_code == 200 and "Published by <strong>app.example.org</strong>" in page.text
        assert start(c, url, challenge, redirect="https://app.example.org/other")[0].status_code == 400
        assert start(c, "https://app.example.org/missing.json", challenge, redirect="https://app.example.org/cb")[0].status_code == 400
