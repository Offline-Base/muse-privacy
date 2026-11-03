import type { OAuthHelpers } from "@cloudflare/workers-oauth-provider";
import type { DeviceHub } from "./device";
import type { PairingGate } from "./pairing";

export interface Env {
  /** The OAuth library's clients, grants and tokens (hashed). deploy.sh creates it. */
  OAUTH_KV: KVNamespace;
  /** Set by the OAuth library for the default handler. */
  OAUTH_PROVIDER: OAuthHelpers;
  /** One Durable Object per device: its secret hash and its WebSocket. */
  DEVICE: DurableObjectNamespace<DeviceHub>;
  /** One Durable Object for pairing codes (hashed) and attempt counters. */
  PAIRING: DurableObjectNamespace<PairingGate>;
  /** How long /mcp waits for the device's response frame. Default 60000. Tests set it lower. */
  REQUEST_TIMEOUT_MS?: string;
  /**
   * Worker secret, at least 32 characters, that keys the HMAC of pairing codes and client IPs.
   * deploy.sh sets it with `wrangler secret put`; `wrangler dev` reads it from .dev.vars.
   * Without it the relay refuses to register or pair devices.
   */
  PAIRING_PEPPER?: string;
}

/** Stored, encrypted, with each grant; the /mcp handler sees it as ctx.props. */
export interface GrantProps {
  deviceId: string;
  /**
   * True when the user picked "Read only" on the link page: the relay then lists and runs only
   * the read tools (./readonly.ts). Links made before the choice existed have no value: full access.
   */
  readOnly?: boolean;
}

/** The one scope a linked app gets: reach the device's connector. */
export const SCOPE = "mcp";
