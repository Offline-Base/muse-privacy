/**
 * Read-only links. The Muse Connector Platform guidelines (3.1) ask a connector to let the user
 * limit Muse to reading. The user picks "Read only" on the link page; the grant then carries
 * readOnly, and the relay enforces it here, before anything reaches the device:
 *
 * - tools/call for a tool that is not in READ_TOOLS is answered by the relay with a readable
 *   tool result (isError) and never forwarded. A call with no tool name counts as a write.
 * - The device's tools/list answer is filtered to READ_TOOLS, so Muse never offers the others.
 *
 * READ_TOOLS is an allowlist, so a tool added on the device later is blocked on read-only links
 * until it is added here. Only the method, id and tool name of a request are read.
 */

/** The connector's Read tools (docs/CONTRACTS.md, "Connector"). Everything else is a write. */
export const READ_TOOLS: ReadonlySet<string> = new Set([
  "list_documents",
  "search_documents",
  "ask_device",
  "get_result",
  "dreaming_report",
]);

export const readOnlyText = (tool: string) =>
  `This Muse link is read only, so the device did not run ${tool}. Nothing was started or saved. ` +
  "To allow it, the user can link Muse again with full access.";

export const BATCH_TEXT = "A read-only link does not take a write tool inside a batch, so nothing in this batch ran.";

type Message = { jsonrpc?: unknown; method?: unknown; id?: unknown; params?: unknown };

const validId = (id: unknown): id is string | number =>
  typeof id === "string" || (typeof id === "number" && Number.isFinite(id));

function parse(body: string): unknown {
  try {
    return JSON.parse(body);
  } catch {
    return undefined;
  }
}

/** The tool a message calls when it is a write on a read-only link, or null. */
function blockedTool(message: unknown): string | null {
  if (!message || typeof message !== "object") return null;
  const { method, params } = message as Message;
  if (method !== "tools/call") return null;
  const name = params && typeof params === "object" ? (params as { name?: unknown }).name : undefined;
  if (typeof name === "string" && READ_TOOLS.has(name)) return null;
  return typeof name === "string" && name.length <= 64 ? name : "that tool";
}

const toolError = (id: string | number, text: string) => ({
  jsonrpc: "2.0",
  id,
  result: { content: [{ type: "text", text }], isError: true },
});

function reply(answer: unknown): Response {
  if (answer === null) return new Response(null, { status: 202 });
  return new Response(JSON.stringify(answer), {
    status: 200,
    headers: { "content-type": "application/json", "cache-control": "no-store" },
  });
}

/**
 * The relay's own answer when a read-only link asks for a write, or null to forward the body.
 * A batch holding a write is answered whole: the write gets the read-only result, every other
 * request a JSON-RPC error, so no part of it runs on the device.
 */
export function blockWrite(body: string): Response | null {
  const message = parse(body);
  if (Array.isArray(message)) {
    if (!message.some((item) => blockedTool(item) !== null)) return null;
    const answers = message.flatMap((item) => {
      const id = item && typeof item === "object" ? (item as Message).id : undefined;
      if (!validId(id)) return [];
      const tool = blockedTool(item);
      return [tool !== null ? toolError(id, readOnlyText(tool)) : { jsonrpc: "2.0", id, error: { code: -32600, message: BATCH_TEXT } }];
    });
    return reply(answers.length ? answers : null);
  }
  const tool = blockedTool(message);
  if (tool === null) return null;
  const id = (message as Message).id;
  return reply(validId(id) ? toolError(id, readOnlyText(tool)) : null);
}

/** Whether the request is (or a batch holds) a tools/list, whose answer must be filtered. */
export function asksToolList(body: string): boolean {
  const message = parse(body);
  const items = Array.isArray(message) ? message : [message];
  return items.some((item) => item && typeof item === "object" && (item as Message).method === "tools/list");
}

function filterMessage(message: unknown): unknown {
  if (Array.isArray(message)) return message.map(filterMessage);
  if (!message || typeof message !== "object") return message;
  const result = (message as { result?: unknown }).result;
  if (!result || typeof result !== "object" || !Array.isArray((result as { tools?: unknown }).tools)) return message;
  const tools = (result as { tools: unknown[] }).tools.filter(
    (tool) => tool && typeof tool === "object" && READ_TOOLS.has(String((tool as { name?: unknown }).name)),
  );
  return { ...message, result: { ...result, tools } };
}

/**
 * The device's tools/list answer with only READ_TOOLS left, as JSON or as an event stream
 * (each `data:` line is filtered on its own). A body that does not parse is replaced by an
 * empty list for the request's id rather than passed through, so a write tool is never offered.
 */
export function filterToolList(body: string, requestBody: string): string {
  const whole = parse(body);
  if (whole !== undefined) return JSON.stringify(filterMessage(whole));
  if (!/^(event|data|id|retry):|^:/m.test(body)) {
    const request = parse(requestBody);
    const id = request && typeof request === "object" && !Array.isArray(request) ? (request as Message).id : undefined;
    return JSON.stringify({ jsonrpc: "2.0", id: validId(id) ? id : null, result: { tools: [] } });
  }
  return body
    .split("\n")
    .map((line) => {
      if (!line.startsWith("data:")) return line;
      const data = parse(line.slice(5).trim());
      return data === undefined ? "data: {}" : `data: ${JSON.stringify(filterMessage(data))}`;
    })
    .join("\n");
}
