import { SELF } from "cloudflare:test";
import { expect } from "vitest";

/** Test helpers: a fake device and a fake Muse client. All data is invented. */

export const ORIGIN = "https://relay.test";
export const REDIRECT_URI = "https://muse.example.com/oauth/callback";

/** A fresh client IP per call, so attempt counters never leak between tests. */
export function freshIp(): string {
  const [a, b, c] = crypto.getRandomValues(new Uint8Array(3));
  return `10.${a}.${b}.${c}`;
}

export interface Device {
  device_id: string;
  secret: string;
}

export async function registerDevice(name = "Test laptop"): Promise<Device> {
  const response = await SELF.fetch(`${ORIGIN}/device/register`, {
    method: "POST",
    headers: { "content-type": "application/json", "CF-Connecting-IP": freshIp() },
    body: JSON.stringify({ name }),
  });
  expect(response.status).toBe(200);
  return response.json<Device>();
}

export async function pairCode(secret: string, review = false): Promise<string> {
  const response = await SELF.fetch(`${ORIGIN}/device/pair`, {
    method: "POST",
    headers: { Authorization: `Bearer ${secret}`, "content-type": "application/json" },
    body: review ? JSON.stringify({ review: true }) : undefined,
  });
  expect(response.status).toBe(200);
  return (await response.json<{ code: string }>()).code;
}

/** Open the device WebSocket as the device would. */
export async function connectDevice(secret: string): Promise<WebSocket> {
  const response = await SELF.fetch(`${ORIGIN}/device/connect`, {
    headers: { Upgrade: "websocket", Authorization: `Bearer ${secret}` },
  });
  expect(response.status).toBe(101);
  const socket = response.webSocket!;
  socket.accept();
  return socket;
}

export function nextMessage(socket: WebSocket): Promise<Record<string, unknown>> {
  return new Promise((resolve) => {
    socket.addEventListener("message", (event) => resolve(JSON.parse(String(event.data))), { once: true });
  });
}

/** A fake device that answers every request frame with `answer(frame)`. */
export function answerFrames(socket: WebSocket, answer: (frame: Record<string, any>) => Record<string, unknown>): void {
  socket.addEventListener("message", (event) => {
    const frame = JSON.parse(String(event.data));
    if (frame.type === "request") socket.send(JSON.stringify({ type: "response", id: frame.id, ...answer(frame) }));
  });
}

function base64Url(bytes: Uint8Array): string {
  return btoa(String.fromCharCode(...bytes)).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
}

export interface AuthorizeStart {
  clientId: string;
  verifier: string;
  url: string;
  handle: string;
  cookie: string;
  html: string;
}

/** Register a client by DCR, as Muse might. */
export async function registerClient(): Promise<string> {
  const registration = await SELF.fetch(`${ORIGIN}/oauth/register`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ client_name: "Muse (test)", redirect_uris: [REDIRECT_URI], token_endpoint_auth_method: "none" }),
  });
  expect(registration.status).toBe(201);
  return (await registration.json<{ client_id: string }>()).client_id;
}

/**
 * Open the authorize page, like Muse sending the user there. Without `clientId` the client
 * registers by DCR first; with one (for example a CIMD URL) it is used as is.
 */
export async function startAuthorize(clientId?: string): Promise<AuthorizeStart> {
  clientId ??= await registerClient();

  const verifier = base64Url(crypto.getRandomValues(new Uint8Array(32)));
  const challenge = base64Url(new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(verifier))));
  const query = new URLSearchParams({
    response_type: "code",
    client_id: clientId,
    redirect_uri: REDIRECT_URI,
    state: "state-123",
    scope: "mcp",
    code_challenge: challenge,
    code_challenge_method: "S256",
    resource: `${ORIGIN}/mcp`,
  });
  const url = `${ORIGIN}/authorize?${query}`;
  const page = await SELF.fetch(url);
  expect(page.status).toBe(200);
  const html = await page.text();
  const handle = /name="handle" value="([^"]+)"/.exec(html)?.[1] ?? "";
  const cookie = (page.headers.get("set-cookie") ?? "").split(";")[0];
  expect(handle).not.toBe("");
  expect(cookie).toContain("=");
  return { clientId, verifier, url, handle, cookie, html };
}

export function submitCode(start: AuthorizeStart, code: string, ip: string, decision = "link", access?: string): Promise<Response> {
  const form = new URLSearchParams({ handle: start.handle, code, decision });
  if (access !== undefined) form.set("access", access);
  return SELF.fetch(start.url, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded", cookie: start.cookie, "CF-Connecting-IP": ip },
    body: form.toString(),
    redirect: "manual",
  });
}

export interface Tokens {
  access_token: string;
  refresh_token: string;
  scope: string;
  clientId: string;
}

/**
 * The whole link: DCR (unless `clientId` is given), authorize page, pairing code, token exchange.
 * `access` is the link page's choice ("read" for read only); `code` reuses a code, such as a review code.
 */
export async function linkApp(secret: string, clientId?: string, access?: string, code?: string): Promise<Tokens> {
  const start = await startAuthorize(clientId);
  code ??= await pairCode(secret);
  const response = await submitCode(start, code, freshIp(), "link", access);
  expect(response.status).toBe(302);
  const location = new URL(response.headers.get("location")!);
  expect(`${location.origin}${location.pathname}`).toBe(REDIRECT_URI);
  expect(location.searchParams.get("state")).toBe("state-123");

  const token = await SELF.fetch(`${ORIGIN}/oauth/token`, {
    method: "POST",
    headers: { "content-type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams({
      grant_type: "authorization_code",
      code: location.searchParams.get("code")!,
      redirect_uri: REDIRECT_URI,
      client_id: start.clientId,
      code_verifier: start.verifier,
    }).toString(),
  });
  expect(token.status).toBe(200);
  return { ...(await token.json<Omit<Tokens, "clientId">>()), clientId: start.clientId };
}

export function mcp(accessToken: string | null, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (accessToken) headers.set("Authorization", `Bearer ${accessToken}`);
  return SELF.fetch(`${ORIGIN}/mcp`, { method: "POST", ...init, headers });
}
