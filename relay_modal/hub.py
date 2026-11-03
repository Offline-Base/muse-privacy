"""The device sockets, in memory. One container holds every device's WebSocket, so every /mcp
request for a device finds it. Each request becomes a request frame and waits for the response
frame with the same id. Frames pass through memory only: no body is stored or logged."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import uuid
from dataclasses import dataclass, field

DEFAULT_TIMEOUT_SECONDS = 60.0
MAX_PENDING = 32
RESPONSE_HEADERS = ("content-type", "mcp-session-id")
_BAD_HEADER = re.compile(r"[\r\n\0]")


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: str


def error_reply(status: int, error: str) -> Reply:
    return Reply(status, {"content-type": "application/json"}, json.dumps({"error": error}))


def from_frame(frame: dict) -> Reply:
    status = frame.get("status")
    if not (isinstance(status, int) and not isinstance(status, bool) and 200 <= status <= 599):
        status = 502
    headers: dict[str, str] = {}
    given = frame.get("headers")
    if isinstance(given, dict):
        for name in RESPONSE_HEADERS:
            value = given.get(name)
            if isinstance(value, str) and not _BAD_HEADER.search(value):
                headers[name] = value
    body = frame.get("body")
    return Reply(status, headers, body if isinstance(body, str) else "")


@dataclass
class _Socket:
    send: object  # async callable taking a str
    close: object  # async callable taking (code, reason)
    pending: dict[str, asyncio.Future] = field(default_factory=dict)


class Hub:
    def __init__(self, timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS):
        self.timeout = timeout_seconds
        self.sockets: dict[str, _Socket] = {}

    def online(self, device_id: str) -> bool:
        return device_id in self.sockets

    async def attach(self, device_id: str, send, close) -> _Socket:
        """A new connection for the device. An older one is closed and its requests fail."""
        old = self.sockets.get(device_id)
        sock = _Socket(send, close)
        self.sockets[device_id] = sock
        if old is not None:
            self._fail(old)
            with contextlib.suppress(Exception):
                await old.close(4000, "Replaced by a newer connection")
        return sock

    def detach(self, device_id: str, sock: _Socket) -> None:
        if self.sockets.get(device_id) is sock:
            del self.sockets[device_id]
        self._fail(sock)

    def _fail(self, sock: _Socket) -> None:
        for future in list(sock.pending.values()):
            if not future.done():
                future.set_result(error_reply(503, "device offline"))
        sock.pending.clear()

    async def message(self, sock: _Socket, text: str) -> None:
        try:
            frame = json.loads(text)
        except ValueError:
            return
        if not isinstance(frame, dict):
            return
        if frame.get("type") == "ping":
            with contextlib.suppress(Exception):
                await sock.send(json.dumps({"type": "pong"}))
            return
        if frame.get("type") != "response" or not isinstance(frame.get("id"), str):
            return
        future = sock.pending.pop(frame["id"], None)
        if future is not None and not future.done():  # unknown ids are late replies
            future.set_result(from_frame(frame))

    async def forward(self, device_id: str, method: str, headers: dict[str, str], body: str) -> Reply:
        sock = self.sockets.get(device_id)
        if sock is None:
            return error_reply(503, "device offline")
        if len(sock.pending) >= MAX_PENDING:
            return error_reply(429, "device busy")
        request_id = str(uuid.uuid4())
        future: asyncio.Future = asyncio.get_running_loop().create_future()
        sock.pending[request_id] = future
        frame = json.dumps({"type": "request", "id": request_id, "method": method, "headers": headers, "body": body})
        try:
            await sock.send(frame)
        except Exception:
            sock.pending.pop(request_id, None)
            return error_reply(503, "device offline")
        try:
            return await asyncio.wait_for(asyncio.shield(future), self.timeout)
        except TimeoutError:
            return error_reply(504, "device timeout")
        finally:
            sock.pending.pop(request_id, None)
