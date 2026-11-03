"""Read-only links and readable tool results, the same rules as relay/src/readonly.ts and
relay/src/toolresult.ts. Only a request's method, id and tool name are read; nothing is stored."""

from __future__ import annotations

import json
import math
import re

READ_TOOLS = frozenset({"list_documents", "search_documents", "ask_device", "get_result", "dreaming_report"})

OFFLINE_TEXT = (
    "The user's Muse Privacy device is offline right now (asleep, or the app is not running), so nothing was shared. "
    "Ask the user to wake the device and try again."
)
TIMEOUT_TEXT = (
    "The user's Muse Privacy device took too long, and nothing was shared. "
    "If you started a job, call get_result with its job_id in a minute."
)
BUSY_TEXT = "The user's Muse Privacy device is busy with other requests right now, and nothing was shared. Try again in a minute."
TEXTS = {502: OFFLINE_TEXT, 503: OFFLINE_TEXT, 504: TIMEOUT_TEXT, 429: BUSY_TEXT}

BATCH_TEXT = "A read-only link does not take a write tool inside a batch, so nothing in this batch ran."


def read_only_text(tool: str) -> str:
    return (
        f"This Muse link is read only, so the device did not run {tool}. Nothing was started or saved. "
        "To allow it, the user can link Muse again with full access."
    )


_MISSING = object()


def _parse(body: str):
    try:
        return json.loads(body)
    except ValueError:
        return _MISSING


def _valid_id(value) -> bool:
    if isinstance(value, bool):
        return False
    return isinstance(value, str) or (isinstance(value, (int, float)) and math.isfinite(value))


def _tool_error(request_id, text: str) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"content": [{"type": "text", "text": text}], "isError": True}}


def tool_call_id(body: str):
    message = _parse(body)
    if not isinstance(message, dict) or message.get("jsonrpc") != "2.0" or message.get("method") != "tools/call":
        return _MISSING
    request_id = message.get("id", _MISSING)
    return request_id if _valid_id(request_id) else _MISSING


def tool_result_for(status: int, body: str) -> dict | None:
    """The readable tool result for a failed forward, or None when the status or body does not call for one."""
    text = TEXTS.get(status)
    if text is None:
        return None
    request_id = tool_call_id(body)
    if request_id is _MISSING:
        return None
    return _tool_error(request_id, text)


def _blocked_tool(message) -> str | None:
    if not isinstance(message, dict) or message.get("method") != "tools/call":
        return None
    params = message.get("params")
    name = params.get("name") if isinstance(params, dict) else None
    if isinstance(name, str) and name in READ_TOOLS:
        return None
    return name if isinstance(name, str) and len(name) <= 64 else "that tool"


def block_write(body: str):
    """The relay's own answer when a read-only link asks for a write: a JSON value to send, None
    for an empty 202, or _MISSING to forward the body to the device."""
    message = _parse(body)
    if isinstance(message, list):
        if not any(_blocked_tool(item) is not None for item in message):
            return _MISSING
        answers = []
        for item in message:
            request_id = item.get("id", _MISSING) if isinstance(item, dict) else _MISSING
            if not _valid_id(request_id):
                continue
            tool = _blocked_tool(item)
            if tool is not None:
                answers.append(_tool_error(request_id, read_only_text(tool)))
            else:
                answers.append({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32600, "message": BATCH_TEXT}})
        return answers or None
    tool = _blocked_tool(message)
    if tool is None:
        return _MISSING
    request_id = message.get("id", _MISSING)
    return _tool_error(request_id, read_only_text(tool)) if _valid_id(request_id) else None


def asks_tool_list(body: str) -> bool:
    message = _parse(body)
    items = message if isinstance(message, list) else [message]
    return any(isinstance(item, dict) and item.get("method") == "tools/list" for item in items)


def _filter(message):
    if isinstance(message, list):
        return [_filter(item) for item in message]
    if not isinstance(message, dict):
        return message
    result = message.get("result")
    if not isinstance(result, dict) or not isinstance(result.get("tools"), list):
        return message
    tools = [t for t in result["tools"] if isinstance(t, dict) and str(t.get("name")) in READ_TOOLS]
    return {**message, "result": {**result, "tools": tools}}


_SSE = re.compile(r"^(event|data|id|retry):|^:", re.M)


def filter_tool_list(body: str, request_body: str) -> str:
    whole = _parse(body)
    if whole is not _MISSING:
        return json.dumps(_filter(whole))
    if not _SSE.search(body):
        request = _parse(request_body)
        request_id = request.get("id") if isinstance(request, dict) else None
        return json.dumps({"jsonrpc": "2.0", "id": request_id if _valid_id(request_id) else None, "result": {"tools": []}})
    lines = []
    for line in body.split("\n"):
        if line.startswith("data:"):
            data = _parse(line[5:].strip())
            line = "data: {}" if data is _MISSING else f"data: {json.dumps(_filter(data))}"
        lines.append(line)
    return "\n".join(lines)
