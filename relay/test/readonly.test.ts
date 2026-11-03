import { SELF, env, runInDurableObject } from "cloudflare:test";
import { describe, expect, it } from "vitest";
import { codeKey, GATE, REVIEW_TTL_SECONDS, REVIEW_USES } from "../src/pairing";
import { BATCH_TEXT, READ_TOOLS, blockWrite, filterToolList, readOnlyText } from "../src/readonly";
import { ORIGIN, answerFrames, connectDevice, freshIp, linkApp, mcp, pairCode, registerDevice, startAuthorize, submitCode } from "./helpers";

/** Read-only links and review codes. All data is invented. */

const HEADERS = { "content-type": "application/json", accept: "application/json, text/event-stream" };
const call = (id: number, name: string, args: Record<string, unknown> = {}) =>
  JSON.stringify({ jsonrpc: "2.0", id, method: "tools/call", params: { name, arguments: args } });
const LIST = JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list" });
const ALL_TOOLS = [
  "list_documents", "search_documents", "ask_device", "run_private_task",
  "save_on_device", "get_result", "run_dreaming", "dreaming_report",
].map((name) => ({ name, inputSchema: { type: "object" } }));

/** A fake device that records frames, lists all eight tools and answers any call with "ran <tool>". */
async function device() {
  const dev = await registerDevice();
  const socket = await connectDevice(dev.secret);
  const seen: string[] = [];
  answerFrames(socket, (frame) => {
    seen.push(frame.body);
    const body = JSON.parse(frame.body);
    const result = body.method === "tools/list" ? { tools: ALL_TOOLS } : { content: [{ type: "text", text: `ran ${body.params?.name}` }] };
    return { status: 200, headers: { "content-type": "application/json" }, body: JSON.stringify({ jsonrpc: "2.0", id: body.id, result }) };
  });
  return { dev, socket, seen };
}

describe("the link page", () => {
  it("offers Full access and Read only, with Full access picked", async () => {
    const { html } = await startAuthorize();
    expect(html).toContain('name="access" value="full" checked');
    expect(html).toContain('name="access" value="read"');
    expect(html).toContain("Read only");
    expect(html).not.toContain("—");
  });

  it("keeps Read only picked after a mistyped code", async () => {
    const start = await startAuthorize();
    const response = await submitCode(start, "ZZZZ2222", freshIp(), "link", "read");
    expect(response.status).toBe(400);
    const html = await response.text();
    expect(html).toContain('name="access" value="read" checked');
    expect(html).not.toContain('value="full" checked');
  });
});

describe("a read-only link", () => {
  it("answers write tools itself and never sends them to the device", async () => {
    const { dev, socket, seen } = await device();
    const tokens = await linkApp(dev.secret, undefined, "read");
    for (const [id, tool] of [[11, "run_private_task"], [12, "save_on_device"], [13, "run_dreaming"], [14, "made_up_tool"]] as const) {
      const response = await mcp(tokens.access_token, { headers: HEADERS, body: call(id, tool) });
      expect(response.status).toBe(200);
      expect(await response.json()).toEqual({ jsonrpc: "2.0", id, result: { content: [{ type: "text", text: readOnlyText(tool) }], isError: true } });
    }
    expect(seen).toEqual([]);
    socket.close();
  });

  it("runs the read tools on the device", async () => {
    const { dev, socket, seen } = await device();
    const tokens = await linkApp(dev.secret, undefined, "read");
    for (const tool of READ_TOOLS) {
      const response = await mcp(tokens.access_token, { headers: HEADERS, body: call(3, tool) });
      expect((await response.json<any>()).result.content[0].text).toBe(`ran ${tool}`);
    }
    expect(seen).toHaveLength(READ_TOOLS.size);
    socket.close();
  });

  it("lists only the read tools", async () => {
    const { dev, socket } = await device();
    const tokens = await linkApp(dev.secret, undefined, "read");
    const response = await mcp(tokens.access_token, { headers: HEADERS, body: LIST });
    const names = (await response.json<any>()).result.tools.map((tool: { name: string }) => tool.name);
    expect(names.sort()).toEqual([...READ_TOOLS].sort());
    socket.close();
  });

  it("keeps working after a token refresh", async () => {
    const { dev, socket, seen } = await device();
    const tokens = await linkApp(dev.secret, undefined, "read");
    const refreshed = await SELF.fetch(`${ORIGIN}/oauth/token`, {
      method: "POST",
      headers: { "content-type": "application/x-www-form-urlencoded" },
      body: new URLSearchParams({ grant_type: "refresh_token", refresh_token: tokens.refresh_token, client_id: tokens.clientId }).toString(),
    });
    const fresh = await refreshed.json<{ access_token: string }>();
    const response = await mcp(fresh.access_token, { headers: HEADERS, body: call(5, "run_private_task") });
    expect((await response.json<any>()).result.isError).toBe(true);
    expect(seen).toEqual([]);
    socket.close();
  });
});

describe("a full-access link", () => {
  it("is the default, lists every tool and runs writes", async () => {
    const { dev, socket, seen } = await device();
    const tokens = await linkApp(dev.secret);
    const list = await mcp(tokens.access_token, { headers: HEADERS, body: LIST });
    expect((await list.json<any>()).result.tools).toHaveLength(8);
    const response = await mcp(tokens.access_token, { headers: HEADERS, body: call(6, "run_private_task") });
    expect((await response.json<any>()).result.content[0].text).toBe("ran run_private_task");
    expect(seen).toHaveLength(2);
    socket.close();
  });
});

describe("read-only helpers", () => {
  it("answer a batch with a write whole, and pass reads and other methods through", () => {
    expect(blockWrite(call(1, "search_documents"))).toBeNull();
    expect(blockWrite(LIST)).toBeNull();
    expect(blockWrite("not json")).toBeNull();
    const batch = JSON.stringify([JSON.parse(call(1, "search_documents")), JSON.parse(call(2, "save_on_device")), { jsonrpc: "2.0", method: "notifications/x" }]);
    return blockWrite(batch)!.json().then((answers) => {
      expect(answers).toEqual([
        { jsonrpc: "2.0", id: 1, error: { code: -32600, message: BATCH_TEXT } },
        { jsonrpc: "2.0", id: 2, result: { content: [{ type: "text", text: readOnlyText("save_on_device") }], isError: true } },
      ]);
    });
  });

  it("treat a call with no tool name as a write", async () => {
    const body = JSON.stringify({ jsonrpc: "2.0", id: 9, method: "tools/call", params: {} });
    expect((await blockWrite(body)!.json<any>()).result.content[0].text).toBe(readOnlyText("that tool"));
  });

  it("filter an event stream, and replace a body that does not parse with an empty list", () => {
    const stream = `event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: 2, result: { tools: ALL_TOOLS } })}\n\n`;
    const data = JSON.parse(filterToolList(stream, LIST).split("\n")[1].slice(6));
    expect(data.result.tools.map((t: { name: string }) => t.name).sort()).toEqual([...READ_TOOLS].sort());
    expect(JSON.parse(filterToolList("<html>", LIST))).toEqual({ jsonrpc: "2.0", id: 2, result: { tools: [] } });
  });
});

describe("review codes", () => {
  it("link several Muse accounts within 14 days, and a one-time code replaces them", async () => {
    const { dev, socket } = await device();
    const pair = await SELF.fetch(`${ORIGIN}/device/pair`, {
      method: "POST",
      headers: { Authorization: `Bearer ${dev.secret}`, "content-type": "application/json" },
      body: JSON.stringify({ review: true }),
    });
    const got = await pair.json<{ code: string; expires_in: number; uses: number }>();
    expect(got).toMatchObject({ expires_in: REVIEW_TTL_SECONDS, uses: REVIEW_USES });

    const first = await linkApp(dev.secret, undefined, undefined, got.code);
    const second = await linkApp(dev.secret, undefined, "read", got.code);
    expect((await mcp(first.access_token, { headers: HEADERS, body: call(1, "run_dreaming") }).then((r) => r.json<any>())).result.isError).toBeUndefined();
    expect((await mcp(second.access_token, { headers: HEADERS, body: call(1, "run_dreaming") }).then((r) => r.json<any>())).result.isError).toBe(true);

    const hash = await codeKey(env.PAIRING_PEPPER!, got.code);
    const left = await runInDurableObject(env.PAIRING.getByName(GATE), async (_gate, state) =>
      state.storage.sql.exec<{ uses_left: number }>("SELECT uses_left FROM codes WHERE code_hash = ?", hash).toArray()[0]?.uses_left,
    );
    expect(left).toBe(REVIEW_USES - 2);

    await pairCode(dev.secret);
    const start = await startAuthorize();
    expect((await submitCode(start, got.code, freshIp())).status).toBe(400);
    socket.close();
  });

  it("are one-time codes unless the body says review: true", async () => {
    const dev = await registerDevice();
    const pair = await SELF.fetch(`${ORIGIN}/device/pair`, {
      method: "POST",
      headers: { Authorization: `Bearer ${dev.secret}`, "content-type": "application/json" },
      body: JSON.stringify({ review: "yes" }),
    });
    expect(await pair.json()).toMatchObject({ expires_in: 600, uses: 1 });
  });
});
