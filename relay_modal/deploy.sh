#!/usr/bin/env bash
# Deploy the Modal relay and hand the device over to the new container.
#
# The relay runs in one container (max_containers=1) that holds the device WebSockets, which are
# long-lived inputs. After `modal deploy`, Modal keeps the old container until those inputs end,
# and the new version cannot start its container meanwhile, so requests would hang. This script
# stops the old container right after the deploy; the device reconnects within seconds and lands
# on the new one. Run from the repo root. First time: create the secret (see modal_app.py).
set -euo pipefail
cd "$(dirname "$0")/.."
# The offline-base workspace defaults to image builder 2026.10, newer than the Modal client here
# supports; 2025.06 builds the same small image. MODAL_PROFILE picks the workspace (offlinebase).
export MODAL_IMAGE_BUILDER_VERSION="${MODAL_IMAGE_BUILDER_VERSION:-2025.06}"
old=$(modal container list --json 2>/dev/null | python3 -c '
import json, sys
try:
    rows = json.load(sys.stdin)
except ValueError:
    rows = []
print(" ".join(r.get("Container ID", "") for r in rows if "muse-privacy-relay" in str(r.get("App Name", ""))))')
modal deploy -m relay_modal.modal_app
for id in $old; do
  echo "Stopping the previous container $id"
  modal container stop -y "$id" >/dev/null
done
url=$(python3 -c 'print("https://" + __import__("subprocess").check_output(["modal", "profile", "current"], text=True).strip() + "--muse-privacy-relay.modal.run")')
for _ in $(seq 1 24); do
  if curl -fs -m 10 "$url/health" >/dev/null; then echo "Relay is up: $url"; exit 0; fi
  sleep 5
done
echo "The relay did not answer at $url/health within 2 minutes." >&2
exit 1
