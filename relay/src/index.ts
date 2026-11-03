import { OAuthProvider } from "@cloudflare/workers-oauth-provider";
import { handleAuthorize } from "./authorize";
import { randomBase64Url, randomHex, sha256Hex } from "./crypto";
import { DeviceHub, type ForwardResponse } from "./device";
import { SCOPE, type Env, type GrantProps } from "./env";
import { CODE_TTL_SECONDS, GATE, PairingGate, REVIEW_TTL_SECONDS, REVIEW_USES, codeKey, ipKey, newCode, pepper } from "./pairing";
import { asksToolList, blockWrite, filterToolList } from "./readonly";
import { toolResultFor } from "./toolresult";

export { DeviceHub, PairingGate };

/**
 * Muse Privacy relay. Muse reaches the user's device through here; the device dials out.
 *
 *   POST /device/register   new device id + secret, returned once (only the secret's hash is kept)
 *   POST /device/pair       device asks for an 8 character pairing code (bearer secret); with
 *                           {"review": true}, a review code: 20 links within 14 days
 *   POST /device/unlink     device revokes every Muse link bound to it (bearer secret)
 *   GET  /device/connect    device WebSocket (bearer secret); a new connection replaces the old one
 *   GET/POST /authorize     OAuth page that asks for the pairing code
 *   POST/DELETE /mcp        Muse's MCP requests (OAuth bearer), forwarded to the device as frames;
 *                           on a read-only link, writes are answered here and tools/list is filtered
 *   /oauth/token, /oauth/register, /.well-known/*   served by the OAuth library
 *
 * Muse's client may identify itself by dynamic client registration or by a Client ID Metadata
 * Document (an https URL as client_id); both are on.
 *
 * Frames are described in docs/CONTRACTS.md ("Relay frames"). Bodies are never stored or logged.
 * When the device is offline, slow or busy, a tools/call gets a readable tool result instead of
 * an HTTP error (./toolresult.ts), which reads only the body's method and id.
 */

const DEVICE_ID = /^dev_[0-9a-f]{32}$/;
/** The secret names its device, so the relay needs no lookup table: dev_<32 hex>.<43 base64url>. */
const DEVICE_SECRET = /^(dev_[0-9a-f]{32})\.[A-Za-z0-9_-]{43}$/;
/** MCP request headers the device's server needs. Lowercase in frames. Authorization never goes down. */
/** mcp-method and mcp-name are the MCP 2026-07-28 request headers (ChatGPT sends them); the device's MCP library refuses a request without them. */
const FORWARDED = ["content-type", "accept", "mcp-session-id", "mcp-protocol-version", "mcp-method", "mcp-name"];
const MAX_BODY_BYTES = 4 * 1024 * 1024;
const REGISTRATIONS_PER_IP = 10;
const NULL_BODY_STATUS = new Set([204, 205, 304]);

function json(body: unknown, status = 200, headers: Record<string, string> = {}): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", "cache-control": "no-store", ...headers },
  });
}

function bearer(request: Request): string {
  const match = /^Bearer\s+(\S+)$/i.exec(request.headers.get("Authorization") ?? "");
  return match ? match[1] : "";
}

const clientIp = (request: Request) => request.headers.get("CF-Connecting-IP") ?? "unknown";

/** Without PAIRING_PEPPER the relay cannot hash codes or IPs, so it registers and pairs nothing. */
const notSetUp = () => json({ error: "relay is not set up: PAIRING_PEPPER is missing" }, 503);

/** The device a bearer secret belongs to, or null when the secret is malformed or wrong. */
async function authDevice(request: Request, env: Env) {
  const secret = bearer(request);
  const match = DEVICE_SECRET.exec(secret);
  if (!match) return null;
  const stub = env.DEVICE.getByName(match[1]);
  return (await stub.verify(secret)) ? { deviceId: match[1], stub } : null;
}

async function register(request: Request, env: Env): Promise<Response> {
  const key = pepper(env);
  if (!key) return notSetUp();
  const allowed = await env.PAIRING.getByName(GATE).take(`register:${await ipKey(key, clientIp(request))}`, REGISTRATIONS_PER_IP);
  if (!allowed) return json({ error: "too many registrations, try again in 10 minutes" }, 429, { "retry-after": "600" });
  // The label is optional and only checked, never stored.
  const input = (await request.json().catch(() => ({}))) as { name?: unknown } | null;
  const name = input && typeof input === "object" ? input.name : undefined;
  if (name !== undefined && (typeof name !== "string" || name.length > 64)) {
    return json({ error: "name must be a string of at most 64 characters" }, 400);
  }
  const deviceId = `dev_${randomHex(16)}`;
  const secret = `${deviceId}.${randomBase64Url(32)}`;
  if (!(await env.DEVICE.getByName(deviceId).init(await sha256Hex(secret)))) return json({ error: "try again" }, 503);
  return json({ device_id: deviceId, secret });
}

async function pair(request: Request, env: Env): Promise<Response> {
  const key = pepper(env);
  if (!key) return notSetUp();
  const device = await authDevice(request, env);
  if (!device) return json({ error: "unauthorized" }, 401);
  // {"review": true} asks for a review code (./pairing.ts); any other body, or none, a one-time code.
  const input = (await request.json().catch(() => null)) as { review?: unknown } | null;
  const review = input !== null && typeof input === "object" && input.review === true;
  const ttl = review ? REVIEW_TTL_SECONDS : CODE_TTL_SECONDS;
  const uses = review ? REVIEW_USES : 1;
  const gate = env.PAIRING.getByName(GATE);
  for (let attempt = 0; attempt < 3; attempt++) {
    const code = newCode();
    if (await gate.issue(device.deviceId, await codeKey(key, code), ttl, uses)) return json({ code, expires_in: ttl, uses });
  }
  return json({ error: "try again" }, 503);
}

/**
 * Revoke every Muse link (OAuth grant) bound to this device, for a phished pairing code or a
 * lost session. The device secret stays valid; `relay_client register --force` replaces that.
 */
async function unlink(request: Request, env: Env): Promise<Response> {
  const device = await authDevice(request, env);
  if (!device) return json({ error: "unauthorized" }, 401);
  let revoked = 0;
  let cursor: string | undefined;
  do {
    const page = await env.OAUTH_PROVIDER.listUserGrants(device.deviceId, { limit: 100, cursor });
    for (const grant of page.items) {
      await env.OAUTH_PROVIDER.revokeGrant(grant.id, device.deviceId);
      revoked++;
    }
    cursor = page.cursor;
  } while (cursor);
  return json({ revoked });
}

async function connect(request: Request, env: Env): Promise<Response> {
  if (request.headers.get("Upgrade")?.toLowerCase() !== "websocket") return json({ error: "expected a websocket upgrade" }, 426);
  const device = await authDevice(request, env);
  if (!device) return json({ error: "unauthorized" }, 401);
  return device.stub.fetch(request);
}

const notAllowed = (allow: string) => json({ error: "method not allowed" }, 405, { allow });

const defaultHandler = {
  async fetch(request: Request, env: Env): Promise<Response> {
    const { pathname, origin } = new URL(request.url);
    if (pathname === "/authorize") return handleAuthorize(request, env);
    if (pathname === "/device/register") return request.method === "POST" ? register(request, env) : notAllowed("POST");
    if (pathname === "/device/pair") return request.method === "POST" ? pair(request, env) : notAllowed("POST");
    if (pathname === "/device/unlink") return request.method === "POST" ? unlink(request, env) : notAllowed("POST");
    if (pathname === "/device/connect") return request.method === "GET" ? connect(request, env) : notAllowed("GET");
    if (pathname === "/health") return json({ ok: true });
    if (pathname === "/") {
      return new Response(`Muse Privacy relay. Add ${origin}/mcp to Muse as a connector, then type the pairing code from your device.\n`, {
        headers: { "content-type": "text/plain; charset=utf-8" },
      });
    }
    return json({ error: "not found" }, 404);
  },
};

function toResponse(reply: ForwardResponse): Response {
  const body = NULL_BODY_STATUS.has(reply.status) || reply.body === "" ? null : reply.body;
  return new Response(body, { status: reply.status, headers: reply.headers });
}

/** Muse's MCP requests. The OAuth library has already checked the bearer token. */
const mcpHandler = {
  async fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    // No server-initiated stream in v1.
    if (request.method !== "POST" && request.method !== "DELETE") return notAllowed("POST, DELETE");
    const props = (ctx as ExecutionContext<GrantProps>).props;
    const deviceId = props?.deviceId;
    if (typeof deviceId !== "string" || !DEVICE_ID.test(deviceId)) return json({ error: "invalid_token" }, 401);
    const readOnly = props.readOnly === true;

    if (Number(request.headers.get("content-length") ?? "0") > MAX_BODY_BYTES) return json({ error: "request too large" }, 413);
    const body = await request.text();
    if (body.length > MAX_BODY_BYTES) return json({ error: "request too large" }, 413);

    // A read-only link never sends a write tool to the device (./readonly.ts).
    if (readOnly && request.method === "POST") {
      const blocked = blockWrite(body);
      if (blocked) return blocked;
    }

    const headers: Record<string, string> = {};
    for (const name of FORWARDED) {
      const value = request.headers.get(name);
      if (value !== null) headers[name] = value;
    }
    const reply = await env.DEVICE.getByName(deviceId).forward({ method: request.method, headers, body });
    if (reply.status >= 400 && request.method === "POST") {
      const readable = toolResultFor(reply.status, body);
      if (readable) return readable;
    }
    if (readOnly && request.method === "POST" && reply.status === 200 && asksToolList(body)) {
      return toResponse({ ...reply, body: filterToolList(reply.body, body) });
    }
    return toResponse(reply);
  },
};

// The resource URL must match the address Muse uses exactly, so the provider is built per origin
// (the workers.dev or custom hostname, or localhost under `wrangler dev`).
const providers = new Map<string, OAuthProvider<Env>>();

function providerFor(origin: string): OAuthProvider<Env> {
  let provider = providers.get(origin);
  if (!provider) {
    provider = new OAuthProvider<Env>({
      apiRoute: "/mcp",
      apiHandler: mcpHandler,
      defaultHandler,
      authorizeEndpoint: "/authorize",
      tokenEndpoint: "/oauth/token",
      clientRegistrationEndpoint: "/oauth/register",
      // Also accept an https URL as client_id (CIMD). Needs the global_fetch_strictly_public flag.
      clientIdMetadataDocumentEnabled: true,
      scopesSupported: [SCOPE, "offline_access"],
      requiredScopes: [SCOPE],
      resourceMetadata: {
        resource: `${origin}/mcp`,
        authorization_servers: [origin],
        bearer_methods_supported: ["header"],
        resource_name: "Muse Privacy",
      },
      accessTokenTTL: 3600,
      refreshTokenTTL: 30 * 24 * 3600,
    });
    providers.set(origin, provider);
  }
  return provider;
}

export default {
  fetch(request: Request, env: Env, ctx: ExecutionContext): Promise<Response> {
    return providerFor(new URL(request.url).origin).fetch(request, env, ctx);
  },
} satisfies ExportedHandler<Env>;
