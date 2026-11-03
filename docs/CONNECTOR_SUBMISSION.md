# Muse Connector Platform submission

What to paste into Meta's connector form, what Meta's guidelines ask and how Muse Privacy meets each one, and what only the owner can do. Prepared Nov 3, 2026. Nothing has been submitted.

The form, at muse.ai/platform ("Submit a connector"), as described by a third-party write-up (https://stacktr.ee/blog/muse-connector-platform.md); Meta's own pages do not list its fields. Signing in needs a work email and an emailed code. The last step accepts the Muse Connector Terms, which that write-up could not open.

## Before submitting (owner)

1. **Support email: done.** team@offlinebase.com is in `site/connector.html`, `site/privacy.html` and `site/terms.html` (Nov 3). Deploy the site so the pages go live.
2. **Read the privacy policy and terms** (`site/privacy.html`, `site/terms.html`). They were written from the code and docs, not by a lawyer. They promise: no sale, ads, profiles or model training on connector data; telling users and Meta within 48 hours of an incident; replies within 2 business days.
3. **Deploy the relay** from this branch, so the read-only choice and review codes are live: `cd ~/muse-privacy-lanes/g-connector/relay && npx wrangler deploy`. That worktree's uncommitted `wrangler.jsonc` already has the real OAuth KV id (never commit it). Roll back with `npx wrangler rollback 08ca4d91-8de9-4f2b-ab17-8c33b13a1a2a` (the Oct 27 build). Existing links keep full access.
4. **Set up a reviewer device** that stays on through review: a Jetson or a Mac with `DEVICE_ONLY=1`, the invented `demo/` folders only, and Always approve on (so reviewers are not stuck on results waiting for an OK). Then run `python -m base.connector.relay_client pair --review` there. The code links up to 20 Muse accounts for 14 days. A normal `pair` on that device replaces it; `unlink` revokes every link it made.
5. **Confirm the entity and track.** CLAUDE.md says startup track; the form asks for company or developer and a work email.
6. **Make the docs reachable.** The GitHub repo is private, so the form's documentation link is the site's `/connector` page, not the repo.

Per-client setup (Claude, ChatGPT, Codex, Muse Code, the Muse app): docs/CONNECT_CLIENTS.md.

## Step 1: Overview

| Field | Answer |
|---|---|
| Connector name | Muse Privacy |
| Company or developer | Offline Base (owner to confirm the legal name) |
| Product website | https://muse-privacy-ten.vercel.app |
| Connector icon | `site/icon-512.png` (512 by 512 PNG) or `site/icon-512.svg` |
| Payments | Does not accept payments |
| Your name / work email | Owner |
| Support email or URL | team@offlinebase.com |
| Privacy policy URL | https://muse-privacy-ten.vercel.app/privacy |
| Terms of service URL | https://muse-privacy-ten.vercel.app/terms |

Example prompts, in a user's own words:

- Grade the essays in class-4b against rubric.md and write feedback for each student on my laptop.
- Which of my students haven't turned in the lab report yet?
- Find what I wrote about Student A's reading progress this month, for my own planning.
- Write a note home to each family about Friday's field trip and save the drafts on my device.
- What did my device learn from my share and keep choices last night?

Anything else box:

> Muse Privacy is the privacy part of Muse Muse, by Offline Base. It gives Muse a private workspace on a device the user owns (a Mac or an NVIDIA Jetson Orin Nano). Every step that reads the user's documents runs on that device with its own model; Muse gets back only text with names and identifiers replaced by tags such as [Student A], or a summary, after an on-device privacy router and the user's own rules. Finished files with real names are written on the device and never sent. Users can link Muse as Read only. No tool sends, buys, publishes or deletes, so there are no sensitive writes. Reviewers can link with a review code on a device holding invented data; ask the support email for it.

## Step 2: Technical specs

| Field | Answer |
|---|---|
| Connection type | Existing MCP |
| Hosted MCP endpoint | https://muse-privacy-relay.theethancastro.workers.dev/mcp |
| Documentation | https://muse-privacy-ten.vercel.app/connector |
| Authentication | OAuth with PKCE (dynamic client registration and Client ID Metadata Documents; scopes `mcp`, `offline_access`) |

Access requirements:

> Each user needs the Muse Privacy app running on their own device (Mac or Jetson Orin Nano), awake and online; when it is offline every tool says so and nothing is shared. Linking asks for an 8 character pairing code made on that device, and offers Full access or Read only. English documents. No account, plan, payment or region limit. Reviewers: we provide a review code for a dedicated device with invented classroom data, good for 14 days.

## Guidelines, and how Muse Privacy meets them

| Guideline | Status |
|---|---|
| 1.1 Complete the task | Tasks finish on the device: grading writes per-student files, save_on_device writes notes home. Results that need the user's OK wait on the device and Muse polls get_result; nothing sends the user elsewhere to finish. |
| 1.2 Value beyond the browser | Muse's browser cannot reach files on the user's computer; the connector works on them without the names leaving. |
| 3.1 Read-only option | **Added Nov 3**: Full access or Read only on the link page; the relay enforces it. |
| 3.2 Classification | Read: list_documents, search_documents, ask_device, get_result, dreaming_report. Write: run_private_task, save_on_device, run_dreaming. No sensitive writes (save_on_device with guardians makes draft files, sends nothing). MCP annotations match (`readOnlyHint`). |
| 3.2 Factual descriptions | Descriptions say what each tool does. Review risk: results and INSTRUCTIONS tell Muse "do not try to get the same content another way" after a refusal. That protects the user's choice rather than steering Muse, but a reviewer could read it as instructing Muse; reword to "The user chose to keep this on the device" if asked. |
| 3.4 Scope of approval | The device's own approval card shows the exact text before it is shared; a changed request is a new decision. |
| 4.2 Minimum data, no model training | Requests are kept on the device 90 days for the user's history and on-device dreaming (plain rules, no model trained). **Submit with `DEVICE_ONLY=1`**: the `lora` dreaming backend writes training examples for cloud LoRA training, and `GLIMMER_BACKEND=openrouter` sends text to a cloud model (only for folders marked invented). Neither belongs in the listed connector. |
| 4.4 Security | Device token 0600; relay stores only hashes and grants; TLS end to end; per-device grants; unlink revokes all. docs/THREAT_MODEL.md. |
| 4.6, 4.7 Finance, transactions | Not applicable. |
| 4.8 Incident response | Notify vendor-incident@meta.com within 48 hours. The privacy policy promises the same to users. |
| 5.5 Test account | Review code plus reviewer device (owner steps 3 and 4). |

## What changed in code for this

- `relay/src/readonly.ts`: the read tool allowlist, the relay's answer to a write on a read-only link, and tools/list filtering (JSON and event streams; an unreadable list becomes empty).
- `relay/src/authorize.ts`, `page.ts`, `env.ts`: the Full access or Read only choice, kept across a mistyped code, stored as `readOnly` in the grant's props.
- `relay/src/pairing.ts`, `index.ts`: review codes (`POST /device/pair` with `{"review": true}`: 20 uses, 14 days); the codes table gains `uses_left` on first start.
- `base/connector/relay_client.py`: `pair --review`.
- `site/connector.html`, `privacy.html`, `terms.html`, `doc.css`, `icon-512.png`, `icon-512.svg`; footer links on the home page.
- Tests: `relay/test/readonly.test.ts` (12), `tests/test_relay_client.py` (review flag).
