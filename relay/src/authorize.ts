import { AuthorizationError, CimdFetchError } from "@cloudflare/workers-oauth-provider";
import { SCOPE, type Env, type GrantProps } from "./env";
import { type Access, accessFrom, messagePage, pairingPage } from "./page";
import { GATE, codeKey, ipKey, normalizeCode, pepper } from "./pairing";

/**
 * The OAuth authorize page. The user proves the device is theirs by typing the pairing code it
 * shows, and the grant is bound to that device: tokens only ever reach its Durable Object.
 *
 * GET validates the request and starts a consent transaction (the library binds its handle to
 * this browser with a cookie). POST checks the code first, so a typo keeps the handle usable,
 * then approves the transaction and completes authorization with props {deviceId, readOnly}.
 * readOnly is the user's "Read only" choice on the page (the Muse Connector Platform's 3.1).
 */

const WRONG_CODE = "That code did not work. A code works once and lasts 10 minutes, so make a new one on your device and try again.";
const TOO_MANY = "Too many tries. Wait 10 minutes, then try again.";
const EXPIRED_TITLE = "This link did not work";
const EXPIRED_TEXT = "Start linking again from Muse. This page may have expired or been used already, or this browser blocked its cookie.";
const NOT_SET_UP_TITLE = "This relay is not set up yet";
const NOT_SET_UP_TEXT = "Its pairing secret is missing, so no device can be linked. Whoever runs the relay needs to run deploy.sh again.";
/** The library's default consent cookie prefix (docs/consent-page.md, "Cookie names"). */
const CONSENT_COOKIE = "__Host-oauth-consent";

const CSP = [
  "default-src 'none'",
  "style-src 'unsafe-inline' https://fonts.googleapis.com",
  "font-src https://fonts.gstatic.com",
  // Chrome checks the redirect after the form posts against form-action.
  "form-action 'self' https: http://localhost:* http://127.0.0.1:*",
  "frame-ancestors 'none'",
  "base-uri 'none'",
].join("; ");

function html(body: string, status = 200, headers?: Headers): Response {
  const out = new Headers(headers);
  out.set("Content-Type", "text/html; charset=utf-8");
  out.set("Content-Security-Policy", CSP);
  out.set("X-Frame-Options", "DENY");
  out.set("Referrer-Policy", "no-referrer");
  out.set("Cache-Control", "no-store");
  return new Response(body, { status, headers: out });
}

/** The form posts back to the same URL, so the original authorization query comes with it. */
const formAction = (request: Request) => `/authorize${new URL(request.url).search}`;

async function show(request: Request, env: Env, handle?: string, error?: string, status = 200, access: Access = "full"): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  const authRequest = await oauth.parseAuthRequest(request);
  const details = await oauth.describeConsent(authRequest);
  if (handle) return html(pairingPage(details, handle, formAction(request), error, access), status);
  const consent = await oauth.beginConsent(authRequest);
  return html(pairingPage(details, consent.handle, formAction(request)), 200, consent.headers);
}

async function submit(request: Request, env: Env): Promise<Response> {
  const oauth = env.OAUTH_PROVIDER;
  const form = await request.formData();
  const handle = String(form.get("handle") ?? "");

  if (form.get("decision") === "cancel") {
    const denied = await oauth.denyConsent(request, handle);
    return new Response(null, { status: 302, headers: denied.headers });
  }

  // Without the library's consent cookie the approval below would fail; stop before using up the code.
  if (!(request.headers.get("Cookie") ?? "").includes(CONSENT_COOKIE)) return html(messagePage(EXPIRED_TITLE, EXPIRED_TEXT), 400);

  const key = pepper(env);
  if (!key) return html(messagePage(NOT_SET_UP_TITLE, NOT_SET_UP_TEXT), 503);
  const ip = request.headers.get("CF-Connecting-IP") ?? "unknown";
  const code = normalizeCode(String(form.get("code") ?? ""));
  const access = accessFrom(form.get("access"));
  const result = await env.PAIRING.getByName(GATE).redeem(await ipKey(key, ip), code ? await codeKey(key, code) : null);
  if (!result.ok) {
    return result.reason === "limited"
      ? show(request, env, handle, TOO_MANY, 429, access)
      : show(request, env, handle, WRONG_CODE, 400, access);
  }

  const approved = await oauth.approveConsent(request, handle, { scope: [SCOPE] });
  const props: GrantProps = { deviceId: result.deviceId, readOnly: access === "read" };
  const { redirectTo } = await oauth.completeAuthorization({
    request: approved.request,
    userId: result.deviceId,
    metadata: {},
    scope: approved.request.scope,
    props,
  });
  approved.headers.set("Location", redirectTo);
  return new Response(null, { status: 302, headers: approved.headers });
}

export async function handleAuthorize(request: Request, env: Env): Promise<Response> {
  try {
    if (request.method === "GET") return await show(request, env);
    if (request.method === "POST") return await submit(request, env);
    return new Response("Method not allowed", { status: 405, headers: { Allow: "GET, POST" } });
  } catch (error) {
    if (error instanceof AuthorizationError && error.redirectTo) return Response.redirect(error.redirectTo, 302);
    if (error instanceof AuthorizationError) return html(messagePage(EXPIRED_TITLE, EXPIRED_TEXT), 400);
    if (error instanceof CimdFetchError) {
      return html(messagePage("This app could not be checked", "Its details could not be loaded. Try again in a moment."), 400);
    }
    throw error;
  }
}
