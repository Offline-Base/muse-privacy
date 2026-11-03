import { SELF, env, runInDurableObject } from "cloudflare:test";
import { afterEach, describe, expect, it, vi } from "vitest";
import { sha256Hex } from "../src/crypto";
import { fromFrame } from "../src/device";
import { BUSY_TEXT, OFFLINE_TEXT, TIMEOUT_TEXT, toolCallId, toolResultFor } from "../src/toolresult";
import { ORIGIN, REDIRECT_URI, answerFrames, connectDevice, linkApp, mcp, pairCode, registerDevice, startAuthorize, submitCode } from "./helpers";

/** An invented JSON-RPC call, the kind Muse sends. */
const CALL = JSON.stringify({ jsonrpc: "2.0", id: 7, method: "tools/call", params: { name: "list_documents", arguments: { folder: "class-4b" } } });
/** Not a tools/call: keeps the relay's own status codes when the device cannot answer. */
const INIT = JSON.stringify({ jsonrpc: "2.0", id: 1, method: "initialize", params: { protocolVersion: "2025-06-18", capabilities: {}, clientInfo: { name: "muse", version: "1" } } });
const MCP_HEADERS = {
  "content-type": "application/json",
  accept: "application/json, text/event-stream",
  "mcp-protocol-version": "2025-06-18",
  "mcp-session-id": "session-abc",
  // MCP 2026-07-28 headers: forwarded so the device's MCP library accepts the request.
  "mcp-method": "tools/call",
  "mcp-name": "list_documents",
};

describe("OAuth metadata", () => {
  it("publishes authorization server metadata with dynamic registration and refresh tokens", async () => {
    const response = await SELF.fetch(`${ORIGIN}/.well-known/oauth-authorization-server`);
    expect(response.status).toBe(200);
    const meta = await response.json<Record<string, unknown>>();
    expect(meta.issuer).toBe(ORIGIN);
    expect(meta.authorization_endpoint).toBe(`${ORIGIN}/authorize`);
    expect(meta.token_endpoint).toBe(`${ORIGIN}/oauth/token`);
    expect(meta.registration_endpoint).toBe(`${ORIGIN}/oauth/register`);
    expect(meta.grant_types_supported).toContain("refresh_token");
    expect(meta.code_challenge_methods_supported).toEqual(["S256"]);
    // Both ways for Muse's client to identify itself: DCR above, and Client ID Metadata Documents.
    expect(meta.client_id_metadata_document_supported).toBe(true);
  });

  it("publishes protected resource metadata for /mcp", async () => {
    const response = await SELF.fetch(`${ORIGIN}/.well-known/oauth-protected-resource/mcp`);
    expect(response.status).toBe(200);
    const meta = await response.json<Record<string, unknown>>();
    expect(meta.resource).toBe(`${ORIGIN}/mcp`);
    expect(meta.authorization_servers).toEqual([ORIGIN]);
  });
});

/** An invented client that identifies itself with a Client ID Metadata Document. */
const CIMD_URL = "https://muse.example.com/oauth/client-metadata.json";
const CIMD_DOC = {
  client_id: CIMD_URL,
  client_name: "Muse (CIMD test)",
  redirect_uris: [REDIRECT_URI],
  token_endpoint_auth_method: "none",
  grant_types: ["authorization_code", "refresh_token"],
  response_types: ["code"],
};

/** Serve CIMD_URL from a stub; every other fetch is untouched. Nothing goes to the network. */
function serveCimd(status = 200): void {
  const real = globalThis.fetch;
  vi.spyOn(globalThis, "fetch").mockImplementation(async (input, init) => {
    const url = input instanceof Request ? input.url : String(input);
    if (url !== CIMD_URL) return real(input, init);
    return new Response(status === 200 ? JSON.stringify(CIMD_DOC) : "not found", {
      status,
      headers: { "content-type": "application/json", "cache-control": "no-store" },
    });
  });
}

describe("Client ID Metadata Documents", () => {
  afterEach(() => vi.restoreAllMocks());

  it("link a client whose client_id is its metadata URL, and its token reaches the device", async () => {
    serveCimd();
    const start = await startAuthorize(CIMD_URL);
    expect(start.html).toContain("Muse (CIMD test)");
    expect(start.html).toContain("Published by <strong>muse.example.com</strong>");

    const device = await registerDevice();
    const tokens = await linkApp(device.secret, CIMD_URL);
    expect(tokens.scope).toBe("mcp");
    const socket = await connectDevice(device.secret);
    answerFrames(socket, () => ({ status: 200, headers: { "content-type": "application/json" }, body: '{"jsonrpc":"2.0","id":7,"result":{}}' }));
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: CALL });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ jsonrpc: "2.0", id: 7, result: {} });
    socket.close();
  });

  it("use a pairing code once for CIMD clients too", async () => {
    serveCimd();
    const device = await registerDevice();
    const code = await pairCode(device.secret);
    expect((await submitCode(await startAuthorize(CIMD_URL), code, "198.51.100.11")).status).toBe(302);
    expect((await submitCode(await startAuthorize(CIMD_URL), code, "198.51.100.12")).status).toBe(400);
  });

  it("show a plain page when the metadata document cannot be loaded", async () => {
    serveCimd(404);
    const query = new URLSearchParams({ response_type: "code", client_id: CIMD_URL, redirect_uri: REDIRECT_URI, code_challenge: "x".repeat(43), code_challenge_method: "S256" });
    const response = await SELF.fetch(`${ORIGIN}/authorize?${query}`, { redirect: "manual" });
    expect(response.status).toBe(400);
    expect(response.headers.get("location")).toBeNull();
    expect(await response.text()).toContain("This app could not be checked");
  });
});

describe("/mcp authentication", () => {
  it("rejects a request with no bearer token and points at the metadata", async () => {
    const response = await mcp(null, { headers: MCP_HEADERS, body: CALL });
    expect(response.status).toBe(401);
    expect(response.headers.get("www-authenticate")).toContain(`resource_metadata="${ORIGIN}/.well-known/oauth-protected-resource/mcp"`);
  });

  it("rejects a made-up token and a device secret used as a token", async () => {
    const device = await registerDevice();
    expect((await mcp("not-a-real-token", { headers: MCP_HEADERS, body: CALL })).status).toBe(401);
    expect((await mcp(device.secret, { headers: MCP_HEADERS, body: CALL })).status).toBe(401);
  });

  it("issues refresh tokens that work", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    expect(tokens.scope).toBe("mcp");
    expect(tokens.refresh_token).toBeTruthy();
    const refreshed = await SELF.fetch(`${ORIGIN}/oauth/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: tokens.refresh_token, client_id: tokens.clientId }).toString(),
    });
    expect(refreshed.status).toBe(200);
    const next = await refreshed.json<{ access_token: string }>();
    expect(next.access_token).not.toBe(tokens.access_token);
    // Offline device: the new token is accepted and reaches the device's object.
    expect((await mcp(next.access_token, { headers: MCP_HEADERS, body: INIT })).status).toBe(503);
  });
});

describe("/mcp forwarding", () => {
  it("sends the request down the device socket as a frame and returns the response frame", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    const seen: Record<string, any>[] = [];
    answerFrames(socket, (frame) => {
      seen.push(frame);
      return {
        status: 200,
        headers: { "content-type": "application/json", "mcp-session-id": "session-xyz", "set-cookie": "dropped=1", "x-extra": "dropped" },
        body: JSON.stringify({ jsonrpc: "2.0", id: 7, result: { content: [{ type: "text", text: "[Student A] essay.md" }] } }),
      };
    });

    const response = await mcp(tokens.access_token, { headers: { ...MCP_HEADERS, "x-not-forwarded": "1" }, body: CALL });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(response.headers.get("mcp-session-id")).toBe("session-xyz");
    expect(response.headers.get("set-cookie")).toBeNull();
    expect(response.headers.get("x-extra")).toBeNull();
    expect(await response.json()).toEqual({ jsonrpc: "2.0", id: 7, result: { content: [{ type: "text", text: "[Student A] essay.md" }] } });

    expect(seen).toHaveLength(1);
    const frame = seen[0];
    expect(frame.type).toBe("request");
    expect(frame.id).toMatch(/^[0-9a-f-]{36}$/);
    expect(frame.method).toBe("POST");
    expect(frame.body).toBe(CALL);
    // Only the MCP headers, lowercase; never the OAuth token.
    expect(frame.headers).toEqual(MCP_HEADERS);
    socket.close();
  });

  it("keeps no body anywhere: the device object holds only the secret hash, KV only the library's records", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    const marker = "Invented essay text 4471";
    answerFrames(socket, () => ({ status: 200, headers: { "content-type": "application/json" }, body: JSON.stringify({ text: marker }) }));
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: JSON.stringify({ jsonrpc: "2.0", id: 1, method: "x", params: { marker } }) });
    expect(await response.text()).toContain(marker);

    const stored = await runInDurableObject(env.DEVICE.getByName(device.device_id), async (_instance, state) => [...(await state.storage.list()).entries()]);
    expect(stored).toEqual([["secret_sha256", await sha256Hex(device.secret)]]);

    const keys = (await env.OAUTH_KV.list()).keys.map((key) => key.name);
    expect(keys.length).toBeGreaterThan(0);
    for (const key of keys) {
      expect(key).not.toContain(marker);
      expect((await env.OAUTH_KV.get(key)) ?? "").not.toContain(marker);
    }
    socket.close();
  });

  it("forwards DELETE (end of session) with its method", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    let method = "";
    answerFrames(socket, (frame) => {
      method = frame.method;
      return { status: 204, headers: {}, body: "" };
    });
    const response = await mcp(tokens.access_token, { method: "DELETE", headers: { "mcp-session-id": "session-abc" } });
    expect(response.status).toBe(204);
    expect(method).toBe("DELETE");
    socket.close();
  });

  it("passes an event-stream body through as one body", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    const sse = 'event: message\ndata: {"jsonrpc":"2.0","id":7,"result":{}}\n\n';
    answerFrames(socket, () => ({ status: 200, headers: { "content-type": "text/event-stream" }, body: sse }));
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: CALL });
    expect(response.headers.get("content-type")).toBe("text/event-stream");
    expect(await response.text()).toBe(sse);
    socket.close();
  });

  it("answers 405 to GET /mcp: no server-initiated stream in v1", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const response = await mcp(tokens.access_token, { method: "GET", headers: { accept: "text/event-stream" } });
    expect(response.status).toBe(405);
    expect(response.headers.get("allow")).toBe("POST, DELETE");
  });

  it("answers 503 when the device is offline", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: INIT });
    expect(response.status).toBe(503);
    expect(await response.json()).toEqual({ error: "device offline" });
  });

  it("answers 503 when the device disconnects mid-request", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    socket.addEventListener("message", () => socket.close(1000, "bye"));
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: INIT });
    expect(response.status).toBe(503);
  });

  it("answers 504 when no response frame comes in time (timeout shortened in tests)", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    const started = Date.now();
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: INIT });
    expect(response.status).toBe(504);
    expect(await response.json()).toEqual({ error: "device timeout" });
    expect(Date.now() - started).toBeGreaterThanOrEqual(300);
    socket.close();
  });

  it("answers an offline tools/call with a readable tool result that has the same id", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: CALL });
    expect(response.status).toBe(200);
    expect(response.headers.get("content-type")).toBe("application/json");
    expect(await response.json()).toEqual({ jsonrpc: "2.0", id: 7, result: { content: [{ type: "text", text: OFFLINE_TEXT }], isError: true } });
  });

  it("answers a tools/call the device dropped or never answered in plain words", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    let socket = await connectDevice(device.secret);
    socket.addEventListener("message", () => socket.close(1000, "bye"));
    const dropped = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: CALL });
    expect(dropped.status).toBe(200);
    expect((await dropped.json<any>()).result.content[0].text).toBe(OFFLINE_TEXT);

    socket = await connectDevice(device.secret);
    const call = JSON.stringify({ jsonrpc: "2.0", id: "call-9", method: "tools/call", params: { name: "get_result", arguments: { job_id: "j1" } } });
    const slow = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: call });
    expect(slow.status).toBe(200);
    expect(await slow.json()).toEqual({ jsonrpc: "2.0", id: "call-9", result: { content: [{ type: "text", text: TIMEOUT_TEXT }], isError: true } });
    socket.close();
  });

  it("answers a tools/call in plain words when the device says its app is not running", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    answerFrames(socket, () => ({ status: 502, headers: { "content-type": "application/json" }, body: '{"error": "the connector is not running on the device"}' }));
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: CALL });
    expect(response.status).toBe(200);
    expect((await response.json<any>()).result).toEqual({ content: [{ type: "text", text: OFFLINE_TEXT }], isError: true });
    socket.close();
  });

  it("keeps the status code for batches, notifications and other methods", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const notification = JSON.stringify({ jsonrpc: "2.0", method: "tools/call", params: { name: "list_documents" } });
    const list = JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" });
    for (const body of [`[${CALL}]`, notification, list, INIT, "not json"]) {
      const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body });
      expect(response.status).toBe(503);
      expect(await response.json()).toEqual({ error: "device offline" });
    }
  });

  it("ignores a response frame for an unknown id", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const socket = await connectDevice(device.secret);
    socket.addEventListener("message", (event) => {
      const frame = JSON.parse(String(event.data));
      socket.send(JSON.stringify({ type: "response", id: "someone-else", status: 200, headers: {}, body: "wrong" }));
      socket.send("not json");
      socket.send(JSON.stringify({ type: "response", id: frame.id, status: 200, headers: {}, body: "right" }));
    });
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: CALL });
    expect(await response.text()).toBe("right");
    socket.close();
  });

  it("refuses bodies over 4 MiB", async () => {
    const device = await registerDevice();
    const tokens = await linkApp(device.secret);
    const response = await mcp(tokens.access_token, { headers: MCP_HEADERS, body: "x".repeat(4 * 1024 * 1024 + 1) });
    expect(response.status).toBe(413);
  });

  it("binds each grant to its own device", async () => {
    const [a, b] = [await registerDevice(), await registerDevice()];
    const tokensA = await linkApp(a.secret);
    const socketB = await connectDevice(b.secret);
    let reachedB = false;
    answerFrames(socketB, () => {
      reachedB = true;
      return { status: 200, headers: {}, body: "{}" };
    });
    // Only B is online; A's token must not reach B.
    expect((await mcp(tokensA.access_token, { headers: MCP_HEADERS, body: INIT })).status).toBe(503);
    expect(reachedB).toBe(false);
    socketB.close();
  });
});

describe("readable tool results", () => {
  it("are made only for a single tools/call with an id, and only for offline, slow or busy", async () => {
    expect(toolCallId(CALL)).toBe(7);
    expect(toolCallId(JSON.stringify({ jsonrpc: "2.0", id: "a", method: "tools/call" }))).toBe("a");
    for (const body of [`[${CALL}]`, "null", "7", "{", JSON.stringify({ jsonrpc: "2.0", id: null, method: "tools/call" }), JSON.stringify({ id: 1, method: "tools/call" })]) {
      expect(toolCallId(body)).toBeUndefined();
    }
    const busy = toolResultFor(429, CALL);
    expect(busy?.status).toBe(200);
    expect(await busy?.json()).toEqual({ jsonrpc: "2.0", id: 7, result: { content: [{ type: "text", text: BUSY_TEXT }], isError: true } });
    for (const status of [400, 401, 404, 413, 500]) expect(toolResultFor(status, CALL)).toBeNull();
    // The text is fixed: nothing from the request (here an invented folder name) is echoed back.
    expect(await toolResultFor(503, CALL)?.text()).not.toContain("class-4b");
  });

  it("have no em dashes", () => {
    for (const text of [OFFLINE_TEXT, TIMEOUT_TEXT, BUSY_TEXT]) expect(text).not.toMatch(/[\u2013\u2014]/);
  });
});

describe("response frames", () => {
  it("are cleaned: bad status becomes 502, odd headers and non-text bodies are dropped", () => {
    expect(fromFrame({ status: 99, headers: null, body: 5 })).toEqual({ status: 502, headers: {}, body: "" });
    expect(fromFrame({ status: 200.5 }).status).toBe(502);
    expect(fromFrame({ status: 404, headers: { "content-type": "text/plain\r\nx: y", "mcp-session-id": "s" }, body: "nope" })).toEqual({
      status: 404,
      headers: { "mcp-session-id": "s" },
      body: "nope",
    });
  });
});


describe("POST /device/unlink", () => {
  it("revokes every Muse link bound to the device, so its tokens stop working", async () => {
    // Review SEC-6: a phished pairing code used to keep its access for good.
    const device = await registerDevice();
    const socket = await connectDevice(device.secret);
    answerFrames(socket, (frame) => ({ status: 200, headers: { "content-type": "application/json" }, body: frame.body }));
    const tokens = await linkApp(device.secret);
    expect((await mcp(tokens.access_token, { method: "POST", headers: MCP_HEADERS, body: CALL })).status).toBe(200);

    const unauthorized = await SELF.fetch(`${ORIGIN}/device/unlink`, { method: "POST" });
    expect(unauthorized.status).toBe(401);
    const response = await SELF.fetch(`${ORIGIN}/device/unlink`, {
      method: "POST",
      headers: { Authorization: `Bearer ${device.secret}` },
    });
    expect(response.status).toBe(200);
    expect(await response.json()).toEqual({ revoked: 1 });
    expect((await mcp(tokens.access_token, { method: "POST", headers: MCP_HEADERS, body: CALL })).status).toBe(401);
    socket.close();
  });
});
