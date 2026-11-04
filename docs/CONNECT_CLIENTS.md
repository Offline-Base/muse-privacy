# Using Muse Privacy from Muse, Claude, ChatGPT, Codex and Muse Code

Every client connects to the same address and links to your device the same way:

- **Connector URL:** `https://offline-base--muse-privacy-relay.modal.run/mcp`
- **Sign-in:** OAuth. The client opens a "Link your device" page; type a pairing code from the device.
- **Pairing code:** on the device, `uv run python -m base.connector.relay_client pair` (8 characters, one use, 10 minutes). Make a new code for each client.
- **Before you add or restart a client, the device must be online.** If the device is off when a client starts, the client marks the connector as failed for that session; restart the client once the device is back. On this laptop the LaunchAgent keeps it up (`docs/UPTIME.md`).
- Results that need your OK wait on the approval page, http://127.0.0.1:8787, on the device.

Status on Nov 3, 2026, evening, after the relay moved to Modal (every older link points at a retired address):

| Client | State | Checked how |
|---|---|---|
| Codex (CLI, IDE extension, desktop app) | Linked on the Modal relay | `codex mcp login` with a pairing code, then real `list_documents` and `get_result` calls; the link survived two relay restarts |
| Muse Code | Linked on the Modal relay | `muse mcp login` with a pairing code, then a real `list_documents` call |
| Claude (claude.ai, Desktop, mobile, Claude Code) | Needs linking again | Remove the old Muse Privacy connector in claude.ai, add the new URL, type a pairing code |
| ChatGPT | Needs linking again | Remove the old app, add the new URL, type a pairing code |
| Muse app | Needs linking again | Ask Muse to remove the old custom connector and create one with the new URL |

## Claude (claude.ai, Desktop, mobile, Claude Code)

One connector serves all of them. Linked on this account before Nov 3 evening; link it again on the new URL.

To add it on another account: claude.ai, Customize > Connectors > Add custom connector, paste the URL, choose "Use Claude's published identity", Connect, then type a pairing code. Free plans allow one custom connector; on Team and Enterprise an Owner adds it first.

Claude Code on a machine signed in with a different account:

```sh
claude mcp add --transport http --scope user muse-privacy https://offline-base--muse-privacy-relay.modal.run/mcp
claude mcp login muse-privacy      # or /mcp inside a session
```

## Codex (CLI, IDE extension, desktop app)

Set up on this Mac (`~/.codex/config.toml`, `[mcp_servers.muse-privacy]`). On another machine:

```sh
codex mcp add muse-privacy --url https://offline-base--muse-privacy-relay.modal.run/mcp
codex mcp login muse-privacy
```

Codex asks before each tool call. In `codex exec` (no prompts) tool calls are refused unless you add `-c 'mcp_servers.muse-privacy.default_tools_approval_mode="approve"'`. Codex Web (cloud tasks) cannot reach it. If login says the client cannot be found, `codex mcp logout muse-privacy` and log in again.

## Muse Code

Set up on this Mac: `~/.config/muse/settings.json` has

```json
"mcp_servers": {
  "muse-privacy": {
    "transport": "streamable_http",
    "url": "https://offline-base--muse-privacy-relay.modal.run/mcp",
    "enabled": true,
    "mode": "optional"
  }
}
```

`mode: optional` keeps Muse Code working when the device is off. Sign in with `muse mcp login muse-privacy`. The old settings file is kept as `settings.json.bak-20261103`.

## ChatGPT

Linked on Nov 1 through developer mode, on an address now retired. To add it again (web only):

1. chatgpt.com/plugins, the plus button, "Add custom MCP server" (older accounts: Settings > Apps & Connectors > Advanced > Developer mode, then Create).
2. Name: Muse Privacy. URL: the connector URL. Authentication: OAuth, client id and secret left empty.
3. Accept the warning, create it, connect, and type a pairing code.
4. In a chat, type @ and pick Muse Privacy. After the device's tools change, open the app and click Refresh.

ChatGPT asks before write tools (run_private_task, save_on_device, run_dreaming). Read tools run without asking. Developer mode is for your own use; listing it for other ChatGPT users is a separate OpenAI submission (not started).

## The Muse app

Paired Oct 28 as a custom connector, on an address now retired. Meta does not review custom connectors, so this works today without the platform listing. To add it again, in a Muse chat:

> Create a custom connector for a remote MCP server, then connect to it. Name: Muse Privacy. Transport: remote streamable HTTP. URL: https://offline-base--muse-privacy-relay.modal.run/mcp. Auth: OAuth (the server supports dynamic client registration). Do not ask me for an API key or header.

Tap Connect, then type a pairing code. The listing on Meta's Connector Platform (docs/CONNECTOR_SUBMISSION.md) is what lets other Muse users find it.

## Read only

Once the new relay is deployed, the link page offers Full access or Read only. A Read only link hides the three write tools in every client. To change it, unlink in the client (or `logout`) and link again.

## Unlinking

`uv run python -m base.connector.relay_client unlink` revokes every link to the device at once: Claude, Codex, Muse Code, ChatGPT and Muse all need a new code afterwards.
