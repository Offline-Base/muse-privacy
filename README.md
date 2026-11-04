# Muse Privacy

Muse can already reach your files through connectors, but whatever it reads lands in its cloud VM, names and all. That keeps it away from the private, tedious work most ready for automation wherever records have to stay with their owner: grading student papers, medical records, city agency casework. Muse Privacy gives Muse a private workspace on hardware you own, connected through a Muse connector: a laptop today, and a Jetson Orin Nano version that is designed but not run on a board yet. Muse plans the work with tags, a model on your device does every step that touches the documents, and only tagged or summarized results go back: names and identifiers are swapped for tags on the device, and a final leak check runs on every text before it leaves.

Part of Muse Muse, Offline Base's entry (Ethan and Hussam) in the Meta Global AI Developer Hackathon, startup track, theme Build for Your World; the submitted copy lives in [Offline-Base/muse-muse/privacy](https://github.com/Offline-Base/muse-muse/tree/main/privacy). An independent project, not made or endorsed by Meta. Every person, essay and record in this repo is invented.

**Which model, for the submission (Nov 4).** The organizers ruled on Nov 2 that every model the project calls at runtime must be Meta's. This repo's default setup (`DEVICE_ONLY=1`) runs Liquid's LFM3-VL-2B, which is not a Meta model, so it is a development setup outside the submission, and its numbers are not results of the submitted project. The submitted setup sends every step that touches documents to Muse Glimmer 30B in the cloud through OpenRouter (`DEVICE_ONLY=0`, `GLIMMER_BACKEND=openrouter`), on invented data only: on Nov 2, 0 leaks of names or identifiers on 200 invented items ([Muse Muse's privacy/README.md](https://github.com/Offline-Base/muse-muse/blob/main/privacy/README.md)). Glimmer on a device has not run for Muse Privacy.

**Update, Nov 3 to 4.** The device-only setup runs on a Jetson Orin Nano 8GB (jetson-05) since Nov 3, installed with `deploy/jetson/install.sh --liquid --llama-release b11970`, behind the relay, which now runs on Modal (`relay_modal/`) at https://offline-base--muse-privacy-relay.modal.run. Codex, Muse Code, Claude, ChatGPT and the Muse app have been linked to that board through the relay. The Oct 31 status below is kept as written.

**Status, Oct 31.** The setup this repo ships is device-only (`.env.example` sets `DEVICE_ONLY=1`): Liquid's LFM3-VL-2B on the laptop does every model step, your policy file decides what is shared (`ROUTER_LOOSEN=lists,result_words` is the default), and no model is reached off the laptop. On 200 invented items (`fresh_200_b`) there were [0 leaks of names or identifiers, 82.0% agreement with the expected decision (164 of 200) and 76 approval requests, 28 of them needless](eval/reports/matrix_baseline_attack_fixes.md); 2 results were shared that should have been asked about, and 3 were refused that should not have been. "Leaks" counts names and identifiers found in released text. It does not cover everything: a summary can still point at someone in words the floors do not list ([known limits](docs/THREAT_MODEL.md)). The model's labels change a couple of items between runs (82.5% in the [run before](eval/reports/floors_fresh_200_b.md)). That is a development set, because the router fixes came from ask causes seen on sets like it. The sealed set has not been scored, so there is no unseen number for this default yet.

**What has not happened yet.** Glimmer has not been measured on the device. A tuned Glimmer at about 1.6-bit (IQ1_S) is planned and has not landed. Every Glimmer number in this README is from Glimmer on OpenRouter with invented data, a development mode that is not part of today's setup. The persona study has not run, so it has no results; anything from it will be labeled simulated, and there are no real users. Grading quality has not been measured on LFM3-VL-2B, only that the job runs and how long it takes.

**The Muse app.** The reports in [eval/reports/muse_app](eval/reports/muse_app/) are scene rehearsals with a script playing Muse, against the connector in process: [all six scenes reach their expected statuses, and 0 of 18 tool outputs hold a private value](eval/reports/muse_app/rehearsal_2026-10-31.md). No session in the real Muse app has a report there yet, so these reports do not say how the app itself behaves.

**Claude and ChatGPT.** The connector is a plain remote MCP server behind the relay's OAuth, so Claude's custom connectors and ChatGPT's developer mode can link to it the same way: [docs/OTHER_APPS.md](docs/OTHER_APPS.md). A local test links clients that register as Claude, ChatGPT and Muse through the relay and gets tagged results back; neither app has been tried for real yet. `LINKED_APP=Claude` (or ChatGPT) makes the device's pages and notes name that app and its maker.

**The Meta AI app.** It works there by copy and paste, not as a connector: no public source found on Oct 31 shows the Meta AI app taking outside connectors (Muse takes them through its directory). On the device's Prepare page you write the question with real names and copy the tagged version into the Meta AI app; then paste the answer back under Put the names back, and the device swaps in the real names on that page only. Only tags the device handed out come back, and nothing on that page is saved or sent. The Meta AI app sees only what you paste, and it never reaches your files. If the Meta AI app opens to connectors, the same connector should work there, untested.

**Privacy toggles.** There are toggles for student, health, financial, work, privacy-law and government records, a setup page, a receipt for each result and one-command start. A toggle tags more, locks the settings that could undo it, and holds results it should; the government ones send nothing to Muse. A toggle does not make anyone legally covered: your school, clinic, agency or company still decides whether Muse may be used, and whatever you type into Muse reaches Meta before the device sees it. The plan and its limits are in [docs/COMPLIANCE_PLAN.md](docs/COMPLIANCE_PLAN.md).

## Who it's for

Anyone whose work runs on records that have to stay with whoever holds them, and who still wants an agent to do the tedious part. The privacy toggles cover six groups. Teachers are the most built out, and the only group with a measured job from start to finish. For the others, the toggles, invented records and simulated users exist, and most of it is not measured yet.

| Who | What Muse could do | What stays on the device | In this repo | Measured |
|---|---|---|---|---|
| Teachers | Grade essays, write feedback, sum up a class, draft parent notes | Essays, names, student ids, the feedback files | An invented class of 24 with essays, a rubric and records; FERPA, COPPA and state student-privacy toggles | Grading job and decisions, below and in the [status](#muse-privacy) |
| Special education coordinators, school privacy reviewers | Summaries around IEP and 504 work; an audit of what left the device | IEP and 504 notes, health office and lunch records | `demo/records/`; simulated sped_coordinator and privacy_officer users | Not yet: the persona study has not run |
| Clinic front desks, substance-use programs | Visit counts, referral lists, reminders | Visit notes, referrals, dates, record numbers, addiction treatment details | HIPAA and 42 CFR Part 2 toggles; a simulated clinic coordinator with 80 invented items | [HIPAA tagging](eval/reports/compliance_with_model_score_build.md) on invented sentences: 0 missed, 0 of 20 clean sentences changed (sentences used while the rules were written) |
| City casework and other government work | Nothing with the records: the caseworker asks on the device's own Ask page | Everything: the CUI, CJIS and export-control toggles send nothing to Muse, not even tagged text | `demo/casework/` (6 invented housing case files); a simulated housing caseworker | Not yet |
| Finance and tax offices | Summaries and lists over client files | Card numbers and SSNs, which are always tagged, and client details | GLBA, PCI DSS and tax preparer (IRC 7216) toggles | Not yet |
| Law offices, HR, anyone under GDPR or CCPA | Summaries over client or employee files | Client details, salary and reviews, special-category data such as religion or ethnicity | Attorney-client, employee records, GDPR and CCPA toggles | Not yet |
| Families: a parent setting up a teen, or an adult child setting up an older parent | Help with the family member's own forms, letters, bills and school work | Their files; card numbers and SSNs, always tagged; the home address and anything else on the family's never-share list | A Family toggle (codes, account numbers and a home address stay on the device; money, health and custody are asked) and a guardian PIN that locks the settings and, if chosen, approvals | Unit tests only. Not built: asks that reach the guardian away from the device |

Each toggle shows its own limit, such as "There is no BAA with Meta" for HIPAA, and none makes anyone legally covered (see Privacy toggles above).

**Families.** The guardian sets the rules, and the family member uses Muse with them. Two limits shape this case. What someone types or pastes into Muse reaches Meta before the device sees it, so the device keeps the family's files from going out, but it cannot stop a card number typed into the chat; when a request already holds a private detail, the device can only say so afterward. And Meta's terms set a minimum age of 13 for Muse, so this is for teens, not younger children. A guardian PIN on the Setup page locks the settings and, if the guardian chooses, approving results; anyone can still deny one. It stops changes at the device, not someone with an administrator account on the computer.

**Teachers, built out first.** The author builds AI tools with school districts, and grading and feedback, the job this build automates, has a student's name in every file. That is the event's Build for Your World theme: "Solve a problem you know firsthand, free up time and mental space" ([event page](https://dev.meta.ai/events/global-hackathon)). Under FERPA's school-official exception, a school may give education records to an outside service without parent consent only if, among other conditions, the service is "under the direct control of the agency or institution with respect to the use and maintenance of education records" ([US Department of Education](https://studentprivacy.ed.gov/faq/who-school-official-under-ferpa)). Muse Privacy keeps the essays on the teacher's own laptop: Muse plans the job with tags, the device's model grades them, and the feedback files with real names are written only there. In the [device smoke test](eval/reports/device_smoke_lfm_loosen_default.md), a script playing Muse graded 24 invented essays on LFM3-VL-2B in 41.1 s (35.9 s with the FERPA toggle on) and wrote 24 feedback files in a throwaway home, and every receipt said device only. That measures the path and its time, not how good the grades are. Today it is a laptop install (below); the Jetson box, designed but not run on a board yet, is the version a school, clinic or office could own outright.

## How it works

```mermaid
flowchart LR
  subgraph cloud["Cloud"]
    muse["Muse app and its Secure VM<br/>plans with tags like [Student A]"]
    relay["Relay<br/>on Modal (or a Cloudflare Worker), OAuth, pairing code"]
  end
  subgraph device["Your device: a laptop today"]
    client["Relay client<br/>dials out, nothing listens"]
    conn["Connector<br/>MCP server on 127.0.0.1:8790"]
    agent["Workspace agent<br/>reads documents, runs jobs"]
    model["Device model<br/>LFM3-VL-2B today, llama-server on 127.0.0.1"]
    egress["Egress, the only exit<br/>tag names and identifiers, model name pass,<br/>request topics: who the result is for,<br/>privacy router, final leak check"]
    page["Approval page<br/>127.0.0.1:8787"]
    home[("BASE_HOME on the device<br/>feedback files with real names,<br/>decision log, tag map")]
    dream["Dreaming, nightly<br/>rules from your decisions, holdout gate"]
  end
  muse -- "MCP over HTTPS" --> relay
  client -- "WebSocket out" --> relay
  client --> conn
  conn --> agent
  agent --> model
  agent --> home
  conn --> egress
  egress -- "ask the user" --> page
  page --> home
  home --> dream
  dream --> egress
  egress -- "tagged or summarized result" --> conn
```

1. You ask Muse in plain words. Muse plans the job on its VM and calls the connector's eight tools: `list_documents`, `search_documents`, `ask_device`, `run_private_task`, `save_on_device`, `get_result`, `run_dreaming`, `dreaming_report`.
2. Muse reaches the device through the relay, which the device dials out to, so nothing on the device is open to the internet.
3. On the device, the agent swaps tags back to names only to choose what to do, and gives the device model the documents with every name and identifier already swapped for tags. Finished files with real names (for grading, one feedback file per student) are written only under `BASE_HOME`.
4. Every result goes through `base/egress.py`: exact checks for names on the roster and identifiers (phones, emails, SSNs, Luhn-checked cards, student ids, addresses, dates of birth), and a second pass where the device model finds names the roster misses. Then the device reads Muse's request: a keyword scan for never-share topics, and one device-model call that labels who the result is for and what it asks (for a student's family, for someone outside the class, a whole text, who a tag really is, a class-level question). The privacy router then decides to share it tagged, share a summary, ask you on the approval page, or keep it; in today's default your policy decides, with no model call for the decision. A final leak check runs on every text before it leaves, and fails closed. If a check cannot run, the result is held on the device.
5. Each night, dreaming rebuilds the router's rules from your approve and deny decisions, grouped by the kind of result and its topics, request topics included. It keeps them only if they score no worse on a holdout set with zero leaks. A learned "ask first" or "refuse" rule is a floor: it holds for every narrower group of its kind, so a closer share rule can never loosen it.

On a Jetson (the device-only setup ran on jetson-05 on Nov 3; the grading handoff below is tested in simulation only), grading would be handed to the laptop: the laptop dials out to the Jetson (`base/heavy/worker.py`), so the managed laptop never listens on the network. The interfaces between modules are in [docs/CONTRACTS.md](docs/CONTRACTS.md).

## Built during the event, and before

All of the code in this repository was written during the event; the first commit is from Oct 27, 2026. Three earlier projects by the same author were read for ideas only, and nothing was copied from them: decision-guard (exact checks before text is sent, for `base/guard.py`), base-router-subagent (a device that dials out to a Cloudflare relay with OAuth and pairing codes, for `relay/` and `base/connector/`) and decision-tree (a status light, for `base/light.py` and `hardware/light/`). The code was written with Claude Code; Muse Spark 1.3 wrote the 2,000-item practice set and graded the invented essays a second time as a cross-check. Details and the open-source libraries used are in [docs/PRIOR_WORK.md](docs/PRIOR_WORK.md).

## Related work

Parts of this exist elsewhere; [docs/PRIOR_WORK.md](docs/PRIOR_WORK.md#related-work) lists them with links. Perplexity's hybrid compute on Mac (Sep 2026) also has cloud models plan while a local model guards private data, with a gate that can mask sensitive details, keep them local, refuse the action, ask for consent, or rewrite the request. PlanTwin is research on a cloud planner working over a de-identified twin of local private state, ARIEL learns a user's sharing judgments, and Canvas MCP and SafeGrader already give teachers pseudonymous student IDs over MCP and local grading. Meta's Muse Gadgets Linux SDK already pairs a Linux box with Muse so Muse "can run commands and move files on the machine"; Muse Privacy is the version where the device decides what Muse may see. Other entries in this hackathon also put Glimmer redaction in front of Meta's models, through the Meta Model API rather than the Muse app. What we did not find elsewhere is this combination inside Meta's Muse: the documents and the tag map stay on hardware the user owns, a model on that hardware does the document work, each result is routed four ways by its content and by who it is for, approvals appear on a page the device serves, and the rules are rebuilt each night from the user's answers behind a holdout gate.

## Setup

You need macOS or Linux, [uv](https://docs.astral.sh/uv/), and for the relay Node 22 and a Cloudflare account.

### Laptop, device-only (what `.env.example` sets)

Every model runs on the laptop. You need llama.cpp b11960 or newer, the LFM3-VL-2B GGUF and projector in `models/`, and for search the EmbeddingGemma 2 GGUF.

```sh
git clone https://github.com/Offline-Base/muse-privacy.git && cd muse-privacy
uv sync
cp .env.example .env                 # then set CHECKER_SERVER_BIN
scripts/start.sh                     # models, approval page and relay client; Ctrl-C stops all
open http://127.0.0.1:8787/setup     # say who you're protecting and finish the checklist
scripts/install_login.sh             # optional: start at login
```

### Laptop, cloud mode (development, invented data only)

Glimmer runs on OpenRouter, so the device reads only folders marked `.synthetic`, such as `demo/`.

```sh
git clone https://github.com/Offline-Base/muse-privacy.git && cd muse-privacy
uv sync
cp .env.example .env                 # then set DEVICE_ONLY=0, CHECKER_BACKEND=glimmer and OPENROUTER_API_KEY
uv run pytest -q                     # unit tests; every model and API is mocked
uv run python -m base                # approval page http://127.0.0.1:8787, connector on 127.0.0.1:8790
```

### Laptop with your own Glimmer file (not run or measured yet)

Not run yet: it needs about 20 GB of free disk. Use llama.cpp b10353 or newer. To swap a tuned file in beside the device-only setup instead, see "Plugging in your own model" in [docs/MUSE_SETUP.md](docs/MUSE_SETUP.md#plugging-in-your-own-model).

```sh
git clone https://github.com/Offline-Base/muse-privacy.git && cd muse-privacy && uv sync && cp .env.example .env
git clone https://github.com/ggml-org/llama.cpp ~/llama.cpp
cmake -S ~/llama.cpp -B ~/llama.cpp/build -DGGML_METAL=ON && cmake --build ~/llama.cpp/build -j --target llama-server
uvx --from huggingface_hub hf download meta-models/Muse-Glimmer-30B-GGUF Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf --local-dir models
printf 'GLIMMER_BACKEND=llama_server\nLLAMA_SERVER_BIN=%s\nGLIMMER_GGUF=%s\nWORKSPACE_ROOT=%s\n' "$HOME/llama.cpp/build/bin/llama-server" "$PWD/models/Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf" "$HOME/<your class folder>" >> .env
scripts/start.sh                     # Glimmer on 127.0.0.1:8081 and 8082, then the server; Ctrl-C stops all
```

Later lines in `.env` win, so the `printf` overrides the cloud defaults. `uv run python -m scripts.bench_speed --label laptop` then writes the laptop's speed report.

### Jetson Orin Nano 8GB

Run on jetson-05 on Nov 3 with `--liquid --llama-release b11970` (LFM3-VL-2B, device only, outside the submission); the Glimmer path below has not run on a board. Run as root from a checkout on the board; `install.sh` makes the service user, builds llama.cpp with CUDA, downloads and checks Glimmer i1-IQ1_S (6.5 GB), makes a self-signed certificate for the approval page and installs three systemd units. See [deploy/jetson/README.md](deploy/jetson/README.md).

```sh
git clone https://github.com/Offline-Base/muse-privacy.git && cd muse-privacy
sudo deploy/jetson/install.sh --build-llama
sudoedit /etc/muse-privacy/env       # APPROVAL_HOST, RELAY_URL, WORKSPACE_ROOT, LIGHT_SERIAL
sudo systemctl start muse-privacy-glimmer muse-privacy
j() { sudo -u muse-privacy env BASE_DEVICE=jetson GLIMMER_BACKEND=llama_server BASE_HOME=/var/lib/muse-privacy/home /opt/muse-privacy/app/.venv/bin/python -m "$@"; }
j base.connector.relay_client register --name "Jetson" --relay-url "https://muse-privacy-relay.<your-subdomain>.workers.dev"
sudo systemctl enable --now muse-privacy-relay
j base.connector.relay_client pair   # type this code in Muse
```

To hand grading to the laptop, print the worker token on the board with `j base.heavy.routes`, copy `/var/lib/muse-privacy/home/tls/cert.pem` to the laptop, and run there `uv run python -m base.heavy.worker --jetson-url https://<jetson address>:8787 --token <token> --ca-cert cert.pem`.

## Add the connector to Muse

The live relay runs on Modal: `relay_modal/deploy.sh` (one always-on container, SQLite on a Modal Volume; see `relay_modal/modal_app.py`). The Cloudflare Worker in `relay/` is the earlier version and works the same way:

1. Deploy the relay once: `cd relay && npx wrangler login && ./deploy.sh`. It prints `https://muse-privacy-relay.<your-subdomain>.workers.dev`.
2. On the laptop: put that address in `.env` as `RELAY_URL`, run `uv run python -m base.connector.relay_client register` once, then keep `uv run python -m base.connector.relay_client run` going next to `python -m base`.
3. In Muse, add it as a custom connector. Meta's Help Center says you "can ask Muse to create a Custom Connector" and shows no field for a connector address, so ask in chat: "Create a custom connector for a remote MCP server, then connect to it. Name: Muse Privacy. Transport: remote streamable HTTP. URL: the address plus `/mcp`. Auth: OAuth (the server supports dynamic client registration)." If your app shows an add-custom-connector option under **Settings > Connectors**, use it with the same address and leave the client ID and secret empty. If the app will not take it, use Muse Code, whose docs describe remote MCP servers with OAuth sign-in (step 5 of [docs/MUSE_SETUP.md](docs/MUSE_SETUP.md)).
4. Muse opens the relay's Link your device page. Run `uv run python -m base.connector.relay_client pair` and type the code there within 10 minutes. Each code works once.
5. Tell Muse who each result is for. The connector's instructions ask Muse to say it ("for my own grading", "for the family newsletter") in run_private_task's instructions and in search_documents' optional `purpose`; the device reads it to decide what it can share. A search whose check needs more time or your OK answers with a job id, and Muse gets the snippets with get_result.

[docs/MUSE_SETUP.md](docs/MUSE_SETUP.md) has the full steps, the Muse Code fallback, the two scheduled jobs to give Muse (dreaming at 2 AM, a one-sentence report at 8 AM) and a test script of prompts. No session in the real Muse app has a report yet, so these steps are not checked against one.

## What Meta's models do here

The device-only app connects to the Muse app. Muse Spark wrote the invented eval data. LFM3-VL-2B, Liquid's model and not Meta's, does the model steps on the device until the tuned Glimmer lands. Glimmer's numbers below are earlier ones, on OpenRouter with invented data.

- **Muse plans over tags it cannot resolve.** Muse runs the job through eight tools and works only with tagged or summarized text: it asks for a grading job and gets back a tagged per-student table, not the essays. So far the reports have a script in Muse's place; in the [scene rehearsal](eval/reports/muse_app/rehearsal_2026-10-31.md), 0 of 18 tool outputs held a private value.
- **Muse is set up to drive the device's learning.** Two scheduled Muse jobs are meant to start dreaming at 2 AM and ask for a one-sentence report at 8 AM (no report from the app yet; the rehearsal's script makes the same two calls). Each night the device rebuilds its rules from the teacher's answers and keeps them only if they pass a holdout with zero leaks. On the device, before today's router default, five simulated nights left accuracy on 100 unseen items at 74.0% and approval requests at 44 ([nights_device_rules/nights.md](eval/reports/nights_device_rules/nights.md)), so learning has not shown a gain on this model. In the earlier Glimmer simulation on OpenRouter, approval requests fell from 10 to 6 a day, and accuracy on 100 items no step had seen rose from 72% to 81%; the cost was that ask first fell from 100% to 68%, with 8 snippets she wanted to see shared tagged ([final/nights.md](eval/reports/final/nights.md)).
- **Spark builds the test.** Muse Spark 1.3 wrote the 2,000 invented practice items and graded the essays a second time: the same total as Glimmer for 15 of 24, within one point for 23 of 24 ([grading_crosscheck.md](eval/reports/grading_crosscheck.md)).
- **Glimmer on OpenRouter, the earlier setup.** One 30B Muse Glimmer graded, found names the roster misses, read who Muse's request is for, wrote summaries and was the privacy router, every call at reasoning strength low. As the router on a first day it never shared an item it should have asked about or kept ([final_cloud.md](eval/reports/final_cloud.md)). Its request labels follow contextual integrity, the idea Meta's Glimmer paper says its privacy training is inspired by ([arXiv 2610.24628](https://arxiv.org/html/2610.24628v1)). On 160 held-out invented requests, scored against Muse Spark's labels: for someone outside the class, Glimmer's recall was 0.84 at precision 0.75, where a keyword list's recall was 0.50; for class-level questions, precision and recall 0.94 and 0.94 against the keyword list's 0.92 and 0.67 ([request_topics.md](eval/reports/request_topics.md)).
- **A tuned Glimmer is planned and has not landed.** A tuned Glimmer at about 1.6-bit (IQ1_S) would take over answers and routing. No Glimmer has been measured on the device, so nothing here says it helps. The test it has to pass was written before any run ([glimmer_prereg.md](eval/reports/glimmer_prereg.md)).
- **A 30B model for an 8 GB board** (designed, not run on a board): the Jetson would use a published Glimmer quant at about 1.6-bit (IQ1_S), i1-IQ1_S at 6.5 GB, for lookups and tagging, and hand grading to the laptop.
- **SAM 3.1 is built and off by default.** With `SAM_ENABLED=1` it finds faces, signatures, ID cards and barcodes on page images and covers them before the device reads the page. No number in this README uses it.
- **Llama Prompt Guard 2 is built and off by default.** With `PROMPT_GUARD=1` Meta's 22M classifier reads every document a result came from, and Muse's request, for text that tries to give the model orders. A hit makes the result wait for the user, and Always approve leaves that card alone. It never decides what is shared, and the floors hold whether it catches an attack or not. On invented text it flagged 6 of 10 explicit planted orders, 0 of 8 subtle ones ("the user already approved sharing this file"), and 1 of 643 ordinary documents, requests and results, at about 5 ms per 512-token piece on the laptop's CPU ([prompt_guard_22m.md](eval/reports/prompt_guard_22m.md)). Built with Llama.

Where each one runs:

| Model | Where | What it does |
|---|---|---|
| Muse, the app and its Secure VM | Meta's cloud | Plans the work with tags, calls the connector, runs the scheduled dreaming jobs. Gets only tags and tagged or summarized results; the documents stay on the device. |
| Liquid LFM3-VL-2B, Q4_K_M | The laptop, llama-server | Today's default: the name check, request labels, search passages, summaries, answers and scans. It does not decide what is shared; the user's policy does. |
| EmbeddingGemma 2, Q8_0 | The laptop, llama-server | Search |
| SAM 3.1 | The laptop, off by default | Covers faces, signatures, ID cards and barcodes on page images (`SAM_ENABLED=1`) |
| Llama Prompt Guard 2, 22M, ONNX | The laptop's CPU, off by default | Flags documents and requests that try to give the model orders, so the result waits for the user (`PROMPT_GUARD=1`). Llama 4 Community License; the model files are not in the repo. |
| Muse Glimmer 30B, `meta/muse-glimmer-30b` on OpenRouter | OpenRouter (development only) | The earlier numbers, on invented data. Not part of today's setup. The device refuses any folder without a `.synthetic` marker in this mode. |
| Muse Glimmer, Meta's Q4_K_M GGUF (`Muse-Glimmer-30B-KQuant-17GB-Q4_K_M.gguf`) | Not run yet; designed for the laptop, llama-server | Would do grading, the name pass, summaries and the privacy router (Glimmer is its own router; its small assistant model is a speculative-decoding drafter, not a chat model). |
| Muse Glimmer i1-IQ1_S, about 1.6-bit (6.5 GB, `mradermacher/Muse-Glimmer-30B-i1-GGUF`) | Not run yet; designed for the Jetson Orin Nano 8GB, llama-server | Would do lookups, tagging and routing on the board, with grading going to the laptop when its worker is connected. |
| A tuned Muse Glimmer, about 1.6-bit (IQ1_S) | Has not landed | Planned for answers and routing. Not measured. |
| Muse Spark 1.3 | Meta Model API | The eval harness, the 2,000 invented practice items and a second-opinion grading cross-check on the tagged essays. `base/spark.py` refuses any text with a roster name or an identifier, and is never used for user data. Not reached by the device-only app. |

## Numbers

### Device-only, today

All on invented data, on the laptop, with LFM3-VL-2B. Each report starts with the command that made it.

| What | Number | Report |
|---|---|---|
| Decisions on `fresh_200_b`, 200 invented items, policy decides with LFM labels, today's `ROUTER_LOOSEN` default. A development set, not unseen | 82.0% (164 of 200), 0 leaks, 76 approval requests (28 needless), 2 missed asks, 0 refusals shared, 3 over-refusals | [matrix_baseline_attack_fixes.md](eval/reports/matrix_baseline_attack_fixes.md) |
| The same set before the router fixes | 74.5% (149 of 200), 0 leaks, 88 approval requests (40 needless) | [score_build_fresh_200_b.md](eval/reports/score_build_fresh_200_b.md) |
| Who should decide, 0 leaks in every row, measured just before the left-out floor and the wider outside-class floor. `fresh_200_b`, then `tasks.jsonl` | Keywords, no model: 64.5%, 59.0%. Policy decides with LFM labels: 81.5%, 82.0%. LFM can only tighten: 44.5%, 58.0%. LFM decides under the floors: 43.0%, 59.0%. LFM as the router over-refuses (86 and 87 on `fresh_200_b`), so the policy decides. The policy row here had 77 approval requests, 29 needless, because LFM's labels differ a little between runs | [matrix_lfm_loosen_default.md](eval/reports/matrix_lfm_loosen_default.md) |
| What each router fix did, on 200 other invented items | None 70.0%, `lists` 71.0%, `result_words` 81.0%, 0 leaks in each. Only `result_words` is above the noise; `lists` needs a learned share rule, which these runs do not have | [dev/loosen/README.md](eval/reports/dev/loosen/README.md) |
| Six demo scenes, with a script playing Muse in process, not the app | All six reach their expected statuses, 0 of 18 tool outputs hold a private value | [rehearsal_2026-10-31.md](eval/reports/muse_app/rehearsal_2026-10-31.md) |
| Device smoke test, with a script playing Muse | Grading 24 essays took 32.9 s (32.8 s with the FERPA toggle on), 24 feedback files, every receipt device only. In the FERPA run the model's text used a never-share word, so the result was kept on the device and the files were still saved; the earlier run shared it. Time, not grade quality | [device_smoke_lfm_floors.md](eval/reports/device_smoke_lfm_floors.md) |
| HIPAA toggle's tagging on invented sentences, rules and LFM3-VL-2B. This set was used while the rules were written, and it is not a legal measure of de-identification | 0 missed, 0 of 20 clean sentences changed | [compliance_with_model_score_build.md](eval/reports/compliance_with_model_score_build.md) |
| Five simulated nights of dreaming, before today's router default | On 100 unseen items, 74.0% before and after, 44 approval requests before and after, 0 leaks | [nights_device_rules/nights.md](eval/reports/nights_device_rules/nights.md) |

Not measured yet on the device: Glimmer of any size, and grade quality.

### Earlier, Glimmer on OpenRouter

These are not today's setup. All are on OpenRouter's Glimmer with invented data. Each links to the report, which starts with the command that made it; raw rows are next to each report under `raw/`. The summary with every source is [eval/reports/final.md](eval/reports/final.md).

| What | Number | Report |
|---|---|---|
| Leaks | 0 in every run: the 100-item run (34 approved releases checked too), the holdout, the night-5 rules on the 100 items, every night's holdout scoring, every simulated day with approvals, the unseen test with the night-0 router and after the last night, and both 24-essay grading jobs. The last round's one leak (an invented name off the roster, on the unseen test) came from a guard gap that is now closed: with a full-name-only name pass, that item leaked before the fix and not after. A name the pass misses entirely is still not caught | [final.md](eval/reports/final.md#leaks) |
| What the guard leaves when the name pass reports part of a name (1384 items, fixed answers, no model call) | Full name only: 15 items leaked before the fix, 0 after. Last word only: 31 before, 1 after. The cost: 30 to 33 extra name tags on 15 to 16 items, 0 before | [name_parts.md](eval/reports/name_parts.md) |
| Router accuracy, 100 items, first day, inputs as the connector sends them | 76% overall: share tagged 3 of 25, share a summary 25 of 25, ask first 25 of 25, refuse 23 of 25; 48 approval requests. No item was shared that should have been asked about or kept | [final_cloud.md](eval/reports/final_cloud.md) |
| Agreement with the simulated teacher on the 20 holdout items, first day | 75% | [final_cloud_holdout.md](eval/reports/final_cloud_holdout.md) |
| Router accuracy, the same 100 items with the rules after night 5 | 88%, 35 approval requests. In-sample: the holdout and the day items the rules learned from are among the 100. One item she would refuse (t085, a discipline note) was shared tagged | [final_cloud_night5_rules.md](eval/reports/final_cloud_night5_rules.md) |
| Approval requests per simulated day (20 items a day, 5 she wants to be asked about) | 10, 9, 7, 6, 6 (unneeded ones 5, 4, 2, 1, 1) | [final/nights.md](eval/reports/final/nights.md) |
| Holdout agreement after each of five nights of dreaming | 75, 85, 90, 85, 85, 90%; the gate kept all five nights. The gate selects on the holdout, so this is an upper bound | [final/nights.md](eval/reports/final/nights.md) |
| Unseen test: 100 practice items no step ever saw | 72% with no rules, 81% after the last night; approval requests 53 to 28. Ask first fell from 100% to 68%: 8 snippets she wanted asked about were shared tagged | [final/nights.md](eval/reports/final/nights.md#unseen-test) |
| Grading 24 essays, from Muse's request to the tagged result Muse read | 118.8 s on a first day (the job itself 102.2 s, 28 Glimmer calls), with an instant approval; 84.9 s with one learned rule. OpenRouter was slow that hour: the same job took 33.4 s and 30.1 s earlier the same day, with about the same tokens | [e2e_cloud_no_rules.md](eval/reports/e2e_cloud_no_rules.md), [e2e_cloud.md](eval/reports/e2e_cloud.md) |
| Cloud speed per step | Router 1805 ms mean in the 100-item run (p95 2950 ms). Bench means, taken while OpenRouter was slow: router 1640 ms, name pass 7521 ms, request topics 3031 ms, grading one essay 29030 ms | [final.md, section 7](eval/reports/final.md#7-speed-in-cloud-mode) |
| Request labels on 160 held-out invented requests (precision / recall) | parents 0.86 / 0.86, someone outside the class 0.75 / 0.84, class-level 0.94 / 0.94, who a tag is 1.00 / 1.00 (6 items), whole text 0.50 / 1.00 | [request_topics.md](eval/reports/request_topics.md) |
| Grading consistency | Same total for 21 of 24 essays between two Glimmer runs; Muse Spark 1.3 gave the same total for 15 of 24 and within one point for 23 of 24 | [grading_spotcheck.md](eval/reports/grading_spotcheck.md), [grading_crosscheck.md](eval/reports/grading_crosscheck.md) |

![Five nights of dreaming on production inputs](eval/reports/final/five_nights.png)

The chart's leak line counts the holdout, the simulated days and the unseen test with both routers. It counts names and identifiers, not policy misses: on day 2 a learned rule shared a discipline note the teacher would have kept (see Known limits).

Not measured yet: Glimmer's speed on the laptop and on a Jetson, the Jetson's memory use, quantization quality (Q4_K_M against i1-IQ1_S), a router LoRA, how often Glimmer's name pass reports only part of a name, and the author's own grading spot-check ([eval/reports/grading_spotcheck.md](eval/reports/grading_spotcheck.md) is ready for those scores).

## Known limits

- **No Glimmer numbers on the device.** The device numbers above are LFM3-VL-2B. Every Glimmer number is on OpenRouter with invented data. Speed on the Jetson, the Jetson's memory, and how much the about 1.6-bit (IQ1_S) quant loses are all unmeasured. Cloud times also swing: the same grading job took 33.4 s in one round and 118.8 s in the next, with about the same tokens.
- **No real users, and the persona study has not run.** Every teacher here is a policy file, and every item is invented. `fresh_200_b` is a development set, and the sealed set has not been scored.
- **The real Muse app has no report yet.** The relay is deployed, and a script playing Muse has paired, listed the tools and listed the essays through it; no grading job or approval has a report through the deployed relay. The reports in `eval/reports/muse_app` have a script in Muse's place, in process. Whether the app takes this connector reliably (Meta documents asking Muse in chat to build a custom connector, not a field for an MCP address), which OAuth client style it uses, whether it fills in who a result is for, how it waits for approvals, whether it uses tags across several steps, and its scheduled jobs all have no report. Pre-registered OAuth clients are not supported, and redirect URIs must be https or loopback.
- **A name off the roster still depends on the device model's name pass finding it once.** The roster cannot know siblings, guardians or staff. The guard now tags every word of a name the pass reports, and a one-word name takes in up to two capitalized words on each side, so a pass that reports "Bennick Cinderfall" also covers a later "Bennick" (the one leak in an earlier Glimmer round). Each of these values joins the tag map, so later texts are tagged and the final check catches them with no model call. What still depends on the pass: a name it misses entirely, which an approved card releases and the final check cannot stop, and a name it reports only in part where the guard cannot widen it (a lowercase common word, or a name split by a comma or a line break). On a real device you would see the name on the card before you approve. The cost: more tags. A part gets its own `[Person N]`, so Muse can read one person as two, and a capitalized word next to a name, or a part that is also a common word ("Grace period"), can be tagged as a name.
- **The router errs on the strict side on a first day.** On `fresh_200_b` it asks about 24 of the 59 results the simulated teacher would share tagged without asking. It also shared 2 of the 51 items she wanted to be asked about, as tagged text, and all of that is one simulated teacher's policy over 200 items.
- **The device model is a labeler here, not a judge.** Given the decision, LFM3-VL-2B refuses too much (86 to 87 over-refusals on `fresh_200_b`), so today's default has the policy decide and uses the model for labels, names and summaries.
- **Dreaming can learn too broad a rule from a few answers.** In the earlier Glimmer simulation, two approvals of snippets with no topics taught "share tagged" for every such snippet. On the unseen test the router then no longer asked about 8 of 25 items the teacher wanted to see first, and on day 2 of the simulation it shared t085, a discipline note her policy refuses, with no card (the night-5 rules did the same on the 100 items). These went out tagged, so they are not leaks, and the leak count does not see a policy miss like this.
- **The learning numbers are simulated and partly selected on.** The teacher is a policy file. In the earlier Glimmer runs the gate selects on the 20 holdout items, and the request-topic descriptions were written with the eval requests in view, so the holdout's 75% to 90% is an upper bound, and the night-5 rules' 88% on the 100 items is in-sample. The unseen test's 72% to 81% is the more honest number, and its items are Spark-written in a similar style. One changed decision moves a holdout category by 20 points, and the same rules scored 90% and then 85% on two runs.
- **Request topics depend on how Muse asks.** A search query alone rarely says who the snippets are for, so the device needs Muse to fill in `purpose`. Earlier, Glimmer over-labeled whole-text requests (precision 0.50 on held-out requests), so asking for one paragraph can be treated as asking for a whole essay.
- **A never-share word only in Muse's request asks instead of refusing.** "Do not mention any IEP" is a caution, so it no longer refuses a harmless result; the cost is that "Which students have an IEP?" over a list of tags also asks, unless the router or a learned refuse rule refuses it.
- **A broader learned "ask first" or "refuse" rule outranks a narrower share rule you taught.** If free-text answers with no topics learn "ask first", a learned "share a summary" for class-level answers does not take effect under it.
- **Each result costs up to two extra model calls,** the name pass and the request-topic label (none for document lists and dreaming reports). A name the pass wrongly finds in a summary makes egress refuse that summary. Initials such as "C.R." are not tagged.
- **Long jobs work by polling.** Muse starts a job and calls get_result until it is done, and get_result can wait on the device for up to 40 seconds; the relay holds no stream open and answers 504 after 60 seconds without a reply from the device. A search whose check runs past 40 seconds or waits for you returns a job id, so Muse has to follow it with get_result too.
- **A Jetson's approval page uses a self-signed certificate,** so browsers warn on first visit; the laptop worker pins it.
- **The status light and the enclosure are designed, not built.** The firmware is unit-tested but not flashed, and the enclosure's mounting holes are placeholders until a board is measured.

## Layout

| Path | What |
|---|---|
| `base/` | The device: guard, model client, workspace and agent, router, approvals, egress, connector, relay client, approval page, heavy-step queue and worker, dreaming, light |
| `relay_modal/` | The relay Muse connects to, as it runs now, on Modal |
| `relay/` | The earlier Cloudflare Worker version of the relay |
| `eval/` | The eval set, holdout, invented roster and teacher policy, the harness, and every report |
| `scripts/` | Simulation, charts, synthetic data, speed bench, secrets scan, Modal scripts |
| `deploy/jetson/` | systemd units and the installer for the board |
| `hardware/` | Status light firmware and the enclosure model |
| `demo/` | The invented class folder (marked `.synthetic`) |
| `docs/` | Contracts, decisions, prior work, Muse setup, demo script, handoff |
| `site/` | The landing page with the animated walkthrough: static HTML, CSS and JS that load nothing from other sites. Open `site/index.html`, or serve it with `python3 -m http.server 8765 --bind 127.0.0.1 --directory site` |
