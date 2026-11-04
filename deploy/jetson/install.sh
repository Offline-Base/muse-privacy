#!/usr/bin/env bash
# Installs Muse Privacy on a Jetson Orin Nano. Run as root from a checkout of this repo or from a
# bundle made by scripts/make_bundle.py. It creates the service user and directories, copies the
# app, downloads Glimmer i1-IQ1_S with resume and a SHA-256 check (or copies a GGUF you brought on
# a USB stick, --model-file), and installs the systemd units. It leaves the board's settings alone
# and never restarts the machine. Not yet run on a real board; see README.md.
set -euo pipefail

PREFIX=/opt/muse-privacy
STATE=/var/lib/muse-privacy
CONF=/etc/muse-privacy
UNIT_DIR=/etc/systemd/system
SERVICE_USER=muse-privacy

MODEL_REPO=mradermacher/Muse-Glimmer-30B-i1-GGUF
MODEL_NAME=Muse-Glimmer-30B.i1-IQ1_S.gguf
MODEL_URL="https://huggingface.co/$MODEL_REPO/resolve/main/$MODEL_NAME"
MODEL_API="https://huggingface.co/api/models/$MODEL_REPO/tree/main"
LLAMA_REPO=https://github.com/ggml-org/llama.cpp
# The laptop's build: LFM3-VL-2B needs b11960 or newer, Glimmer b10353 or newer.
LLAMA_REF="${LLAMA_REF:-b11972}"

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_SRC="$(cd "$HERE/../.." && pwd)"

BUILD_LLAMA=0
START=0
SKIP_MODEL=0
MODEL_FILE=""
MODEL_FILE_SHA=""
LIQUID=0
LLAMA_RELEASE=""
LIQUID_REPO=LiquidAI/LFM3-VL-2B-GGUF
LIQUID_FILES="LFM3-VL-2B-Q4_K_M.gguf mmproj-LFM3-VL-2B-Q8_0.gguf"

usage() {
  cat <<'EOF'
Usage: sudo deploy/jetson/install.sh [--build-llama | --llama-release TAG] [--liquid | --skip-model | --model-file FILE --model-sha256 HEX] [--start]

  --build-llama   clone llama.cpp (tag b11972 or LLAMA_REF) into /opt/muse-privacy/llama.cpp
                  and build llama-server with CUDA if it is not there yet (takes a while)
  --llama-release TAG
                  instead of building, unpack llama.cpp's official arm64 CUDA 13.4 release for TAG
                  (for example b11970) and its CUDA runtime, each checked against GitHub's SHA-256,
                  into /opt/muse-privacy/llama.cpp/build/bin (no compiler or CUDA toolkit needed)
  --skip-model    do not download the Glimmer GGUF
  --model-file F  copy this GGUF (from a USB stick) into /opt/muse-privacy/models instead of
                  downloading one, and point the model service at it (GLIMMER_GGUF in /etc/muse-privacy/env)
  --model-sha256  the SHA-256 that file must have (needed with --model-file; plug_in_model --check prints it)
  --liquid        run device only on Liquid's LFM3-VL-2B instead of Glimmer: download it and its
                  vision projector (SHA-256 checked), install the Liquid service on 127.0.0.1:8083, set
                  DEVICE_ONLY=1 and PRIMARY_MODEL=lfm, and mask the Glimmer service
  --start         start the services at the end (otherwise they are only enabled)

Environment:
  MODEL_SHA256   expected SHA-256 of the downloaded GGUF, if you do not want it read from the Hugging Face API
  LLAMA_REF      llama.cpp tag to build (default b11972, the laptop's build; Glimmer needs b10353 or newer
                 and LFM3-VL-2B b11960 or newer)
  CUDACXX        the CUDA compiler (default /usr/local/cuda/bin/nvcc)
  BUILD_JOBS     parallel compile jobs for --build-llama (default 2; more can run out of memory)
EOF
}

say() { printf '\n== %s\n' "$*"; }
die() { printf 'install.sh: %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --build-llama) BUILD_LLAMA=1 ;;
    --skip-model) SKIP_MODEL=1 ;;
    --model-file) shift; MODEL_FILE="${1:-}" ;;
    --model-sha256) shift; MODEL_FILE_SHA="${1:-}" ;;
    --start) START=1 ;;
    --liquid) LIQUID=1 ;;
    --llama-release) shift; LLAMA_RELEASE="${1:-}" ;;
    -h|--help) usage; exit 0 ;;
    *) usage >&2; die "unknown option: $1" ;;
  esac
  shift
done

if [ -n "$MODEL_FILE" ] || [ -n "$MODEL_FILE_SHA" ]; then
  [ -n "$MODEL_FILE" ] && [ -n "$MODEL_FILE_SHA" ] || die "--model-file and --model-sha256 go together"
  [ -f "$MODEL_FILE" ] || die "no file at $MODEL_FILE"
  case "$MODEL_FILE" in *.gguf) ;; *) die "--model-file must be a .gguf file" ;; esac
  [[ "$MODEL_FILE_SHA" =~ ^[0-9a-f]{64}$ ]] || die "--model-sha256 must be 64 lowercase hex characters"
  [ "$SKIP_MODEL" -eq 0 ] || die "--skip-model and --model-file do not go together"
fi

if [ -n "$LLAMA_RELEASE" ]; then
  [[ "$LLAMA_RELEASE" =~ ^b[0-9]+$ ]] || die "--llama-release takes a tag like b11970"
  [ "$BUILD_LLAMA" -eq 0 ] || die "--build-llama and --llama-release do not go together"
fi
if [ "$LIQUID" -eq 1 ] && { [ -n "$MODEL_FILE" ] || [ "$SKIP_MODEL" -eq 1 ]; }; then
  die "--liquid downloads its own model: leave out --model-file and --skip-model"
fi

[ "$(uname -s)" = Linux ] || die "run this on the Jetson itself (Linux)"
[ "$(id -u)" -eq 0 ] || die "run as root: sudo $0"
for cmd in curl python3 sha256sum systemctl install tar useradd; do
  command -v "$cmd" >/dev/null 2>&1 || die "missing command: $cmd"
done

say "Service user"
if ! id -u "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "$STATE" --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
# video: the GPU device nodes; dialout: the USB status light. Skipped if a group does not exist.
for group in video render dialout; do
  if getent group "$group" >/dev/null; then usermod -aG "$group" "$SERVICE_USER"; fi
done

say "Directories"
install -d -m 0755 "$PREFIX" "$PREFIX/bin" "$PREFIX/models" "$PREFIX/app"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$STATE" "$STATE/home" "$STATE/adapters"
install -d -m 0750 -o root -g "$SERVICE_USER" "$CONF"
if [ ! -f "$CONF/env" ]; then
  cat >"$CONF/env" <<'EOF'
# Site settings for Muse Privacy on this board. Read by the systemd units.
# The approval page address the laptop worker dials (this board's address on your own network).
# APPROVAL_HOST=
# The connector relay (Cloudflare Worker) this board dials out to.
# RELAY_URL=
# The status light, if one is plugged in.
# LIGHT_SERIAL=/dev/ttyACM0
# Where your documents live on this board.
# WORKSPACE_ROOT=/var/lib/muse-privacy/workspace
# TLS for the approval port (install.sh made these). Copy cert.pem to the laptop for --ca-cert.
APPROVAL_TLS_CERT=/var/lib/muse-privacy/home/tls/cert.pem
APPROVAL_TLS_KEY=/var/lib/muse-privacy/home/tls/key.pem
EOF
  chown root:"$SERVICE_USER" "$CONF/env"
  chmod 0640 "$CONF/env"
fi

say "llama-server key"
# One random key, read by llama-server (--api-key-file) and by the server (BASE_HOME/llama_api_key),
# so no other local process can use the model or change the router adapter's scale.
KEY_FILE="$STATE/home/llama_api_key"
if [ ! -s "$KEY_FILE" ]; then
  (umask 077 && python3 -c 'import secrets; print(secrets.token_urlsafe(32))' >"$KEY_FILE")
fi
chown "$SERVICE_USER:$SERVICE_USER" "$KEY_FILE"
chmod 0600 "$KEY_FILE"

say "TLS certificate"
# A self-signed certificate for the approval port, so the session cookie, the worker token and the
# essays in heavy steps never cross the home network in the clear. The laptop worker pins it.
TLS_DIR="$STATE/home/tls"
if [ ! -s "$TLS_DIR/cert.pem" ]; then
  if command -v openssl >/dev/null 2>&1; then
    install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$TLS_DIR"
    openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:prime256v1 -nodes -days 3650 \
      -subj "/CN=muse-privacy" -addext "subjectAltName=DNS:muse-privacy" \
      -keyout "$TLS_DIR/key.pem" -out "$TLS_DIR/cert.pem" 2>/dev/null
    chown "$SERVICE_USER:$SERVICE_USER" "$TLS_DIR/key.pem" "$TLS_DIR/cert.pem"
    chmod 0600 "$TLS_DIR/key.pem"
    chmod 0644 "$TLS_DIR/cert.pem"
  else
    echo "openssl is not installed, so no certificate was made; the approval page will be plain http."
  fi
fi

say "App"
# Copy only what git tracks: never .env (secrets stay on the laptop), a virtualenv, node_modules
# or local state from anywhere in the tree. Without git (an unpacked bundle from
# scripts/make_bundle.py), fall back to tar with unanchored excludes.
if command -v git >/dev/null 2>&1 && git -C "$APP_SRC" rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  CHANGED="$(git -C "$APP_SRC" status --porcelain | wc -l | tr -d ' ')"
  if [ "$CHANGED" -gt 0 ]; then
    echo "Warning: $CHANGED file(s) differ from the last commit, and git archive HEAD installs only"
    echo "committed files. To install this tree as it is, build a bundle on the laptop with"
    echo "uv run python -m scripts.make_bundle, copy it over and run install.sh from the unpacked bundle."
  fi
  git -C "$APP_SRC" archive --format=tar HEAD | tar -C "$PREFIX/app" -xf -
else
  tar -C "$APP_SRC" --exclude=.env --exclude=.venv --exclude=.git --exclude=node_modules \
    --exclude=.wrangler --exclude=__pycache__ --exclude='*.gguf' -cf - . | tar -C "$PREFIX/app" -xf -
fi
chown -R root:root "$PREFIX/app"
if command -v uv >/dev/null 2>&1; then
  # Keep the Python uv installs under /opt so the service user can reach it (ProtectHome hides /root).
  (cd "$PREFIX/app" && UV_PYTHON_INSTALL_DIR="$PREFIX/python" uv sync --frozen --no-dev)
else
  echo "uv is not installed, so the app's virtualenv was not built."
  echo "Install uv, then run: cd $PREFIX/app && UV_PYTHON_INSTALL_DIR=$PREFIX/python uv sync --frozen --no-dev"
fi

say "llama.cpp"
LLAMA_BIN="$PREFIX/llama.cpp/build/bin/llama-server"
if [ -x "$LLAMA_BIN" ]; then
  echo "llama-server found."
elif [ -n "$LLAMA_RELEASE" ]; then
  # Two assets of the release: llama-server with its libraries, and the CUDA runtime it loads. Both go
  # in one folder, where the libraries find each other. GitHub's API lists each asset's SHA-256.
  BIN_DIR="$(dirname "$LLAMA_BIN")"
  install -d -m 0755 "$BIN_DIR"
  RELEASE_JSON="$(curl -fsSL --retry 3 "https://api.github.com/repos/ggml-org/llama.cpp/releases/tags/$LLAMA_RELEASE")" \
    || die "could not read llama.cpp release $LLAMA_RELEASE from GitHub"
  for asset in "llama-$LLAMA_RELEASE-bin-ubuntu-cuda-13.4-arm64.tar.gz" "cudart-llama-$LLAMA_RELEASE-bin-ubuntu-cuda-13.4-arm64.tar.gz"; do
    INFO="$(printf '%s' "$RELEASE_JSON" | python3 -c '
import json, sys
name = sys.argv[1]
for a in json.load(sys.stdin).get("assets", []):
    if a.get("name") == name and str(a.get("digest", "")).startswith("sha256:"):
        print(a["digest"][7:], a["browser_download_url"])
' "$asset")"
    [ -n "$INFO" ] || die "release $LLAMA_RELEASE has no $asset with a SHA-256"
    SHA="${INFO%% *}"; URL="${INFO##* }"
    TMP="$PREFIX/$asset.part"
    echo "Downloading $asset."
    curl -fL --retry 10 --retry-delay 5 --continue-at - -o "$TMP" "$URL"
    [ "$(sha256sum "$TMP" | cut -d' ' -f1)" = "$SHA" ] || { rm -f "$TMP"; die "SHA-256 mismatch for $asset"; }
    tar -C "$BIN_DIR" --strip-components=1 -xzf "$TMP"
    rm -f "$TMP"
  done
  chown -R root:root "$BIN_DIR"
  [ -x "$LLAMA_BIN" ] || die "the release did not contain llama-server"
  "$LLAMA_BIN" --version 2>&1 | head -n 2 || true
elif [ "$BUILD_LLAMA" -eq 1 ]; then
  command -v git >/dev/null 2>&1 || die "missing command: git"
  command -v cmake >/dev/null 2>&1 || die "missing command: cmake"
  # JetPack puts nvcc in /usr/local/cuda/bin, which sudo's secure_path leaves off PATH.
  NVCC="${CUDACXX:-/usr/local/cuda/bin/nvcc}"
  [ -x "$NVCC" ] || die "no CUDA compiler at $NVCC (install the JetPack CUDA toolkit, or set CUDACXX)"
  export CUDACXX="$NVCC" PATH="$(dirname "$NVCC"):$PATH"
  [ -d "$PREFIX/llama.cpp" ] || git clone --depth 1 --branch "$LLAMA_REF" "$LLAMA_REPO" "$PREFIX/llama.cpp"
  # The CUDA kernels need well over 1 GB each to compile, so build two at a time on the 8 GB
  # board, with the model service stopped.
  systemctl stop muse-privacy-glimmer.service 2>/dev/null || true
  # Orin is CUDA compute capability 8.7.
  cmake -S "$PREFIX/llama.cpp" -B "$PREFIX/llama.cpp/build" -DGGML_CUDA=ON -DCMAKE_CUDA_ARCHITECTURES=87 \
    -DCMAKE_BUILD_TYPE=Release -DLLAMA_BUILD_TESTS=OFF
  cmake --build "$PREFIX/llama.cpp/build" --config Release -j "${BUILD_JOBS:-2}" --target llama-server
else
  echo "llama-server is not at $LLAMA_BIN. Run again with --build-llama, or build llama.cpp $LLAMA_REF"
  echo "or newer with CUDA there yourself. The Glimmer service will not start until it exists."
fi

# Download one file from a Hugging Face repo into $PREFIX/models, resuming, and keep it only when
# its SHA-256 matches the one the Hugging Face API lists.
hf_fetch() {
  local repo="$1" name="$2" dest info sha size have free_kb
  dest="$PREFIX/models/$name"
  info="$(curl -fsSL --retry 3 "https://huggingface.co/api/models/$repo/tree/main" | python3 -c '
import json, sys
name = sys.argv[1]
for entry in json.load(sys.stdin):
    lfs = entry.get("lfs") or {}
    if entry.get("path") == name and lfs.get("oid"):
        print(lfs["oid"], lfs.get("size") or entry.get("size") or 0)
        break
' "$name")" || die "could not read $name's SHA-256 from the Hugging Face API"
  [ -n "$info" ] || die "the Hugging Face API did not list $name"
  sha="${info%% *}"; size="${info##* }"
  [[ "$sha" =~ ^[0-9a-f]{64}$ ]] || die "expected SHA-256 for $name does not look like one"
  if [ -f "$dest" ] && [ "$(sha256sum "$dest" | cut -d' ' -f1)" = "$sha" ]; then
    echo "$name already in place and verified."
    return
  fi
  have=0
  [ -f "$dest.part" ] && have="$(stat -c %s "$dest.part")"
  free_kb="$(df -Pk "$PREFIX/models" | awk 'NR == 2 {print $4}')"
  [ $(( (size - have) / 1024 + 1048576 )) -le "$free_kb" ] || die "not enough free disk for $name"
  if [ "$have" -ne "$size" ]; then
    echo "Downloading $name (resumes if interrupted)."
    curl -fL --retry 10 --retry-delay 5 --continue-at - -o "$dest.part" "https://huggingface.co/$repo/resolve/main/$name"
  fi
  [ "$(sha256sum "$dest.part" | cut -d' ' -f1)" = "$sha" ] || die "SHA-256 mismatch for $dest.part. Delete it and run again."
  mv -f "$dest.part" "$dest"
  chmod 0644 "$dest"
  echo "$name verified."
}

set_env() {
  # One KEY=VALUE line in the site settings, replaced if the key is there.
  if grep -q "^$1=" "$CONF/env"; then sed -i "s|^$1=.*|$1=$2|" "$CONF/env"; else printf '%s=%s\n' "$1" "$2" >>"$CONF/env"; fi
}

say "Model"
if [ "$LIQUID" -eq 1 ]; then
  for name in $LIQUID_FILES; do hf_fetch "$LIQUID_REPO" "$name"; done
  # Every step, the router included, runs on Liquid's model on this board; nothing off the device.
  set_env DEVICE_ONLY 1
  set_env PRIMARY_MODEL lfm
  echo "Device only on LFM3-VL-2B (DEVICE_ONLY=1, PRIMARY_MODEL=lfm in $CONF/env)."
elif [ -n "$MODEL_FILE" ]; then
  DEST="$PREFIX/models/$(basename "$MODEL_FILE")"
  if [ ! -f "$DEST" ] || [ "$(sha256sum "$DEST" | cut -d' ' -f1)" != "$MODEL_FILE_SHA" ]; then
    FREE_KB="$(df -Pk "$PREFIX/models" | awk 'NR == 2 {print $4}')"
    NEED_KB=$(( $(stat -c %s "$MODEL_FILE") / 1024 + 1048576 ))
    [ "$NEED_KB" -le "$FREE_KB" ] || die "not enough free disk in $PREFIX/models for $(basename "$MODEL_FILE")"
    echo "Copying $(basename "$MODEL_FILE") (no download)."
    cp "$MODEL_FILE" "$DEST.part"
    ACTUAL_SHA="$(sha256sum "$DEST.part" | cut -d' ' -f1)"
    [ "$ACTUAL_SHA" = "$MODEL_FILE_SHA" ] || { rm -f "$DEST.part"; die "SHA-256 mismatch for the copy of $(basename "$MODEL_FILE"). Copy it to the stick again."; }
    mv -f "$DEST.part" "$DEST"
  fi
  chmod 0644 "$DEST"
  # The model service reads GLIMMER_GGUF from the site settings (glimmer-env.sh).
  if grep -q '^GLIMMER_GGUF=' "$CONF/env"; then
    sed -i "s|^GLIMMER_GGUF=.*|GLIMMER_GGUF=$DEST|" "$CONF/env"
  else
    printf '# The model the Glimmer service loads (install.sh --model-file).\nGLIMMER_GGUF=%s\n' "$DEST" >>"$CONF/env"
  fi
  echo "Model verified; the model service will load $(basename "$DEST")."
elif [ "$SKIP_MODEL" -eq 1 ]; then
  echo "Skipped (--skip-model)."
else
  DEST="$PREFIX/models/$MODEL_NAME"
  if [ -n "${MODEL_SHA256:-}" ]; then
    EXPECTED_SHA="$MODEL_SHA256"
    EXPECTED_SIZE=0
  else
    # The Hugging Face tree API lists each LFS file's SHA-256 and size.
    INFO="$(curl -fsSL --retry 3 "$MODEL_API" | python3 -c '
import json, sys
name = sys.argv[1]
for entry in json.load(sys.stdin):
    lfs = entry.get("lfs") or {}
    if entry.get("path") == name and lfs.get("oid"):
        print(lfs["oid"], lfs.get("size") or entry.get("size") or 0)
        break
' "$MODEL_NAME")" || die "could not read the model's SHA-256 from the Hugging Face API"
    [ -n "$INFO" ] || die "the Hugging Face API did not list $MODEL_NAME"
    EXPECTED_SHA="${INFO%% *}"
    EXPECTED_SIZE="${INFO##* }"
  fi
  [[ "$EXPECTED_SHA" =~ ^[0-9a-f]{64}$ ]] || die "expected SHA-256 does not look like one"

  if [ -f "$DEST" ] && [ "$(sha256sum "$DEST" | cut -d' ' -f1)" = "$EXPECTED_SHA" ]; then
    echo "Model already in place and verified."
  else
    FREE_KB="$(df -Pk "$PREFIX/models" | awk 'NR == 2 {print $4}')"
    HAVE=0
    [ -f "$DEST.part" ] && HAVE="$(stat -c %s "$DEST.part")"
    if [ "$EXPECTED_SIZE" -gt 0 ] && [ $(( (EXPECTED_SIZE - HAVE) / 1024 + 1048576 )) -gt "$FREE_KB" ]; then
      die "not enough free disk for the model (needs about $((EXPECTED_SIZE / 1073741824 + 1)) GB)"
    fi
    if [ "$EXPECTED_SIZE" -gt 0 ] && [ "$HAVE" -eq "$EXPECTED_SIZE" ]; then
      echo "Download already complete, verifying."
    else
      echo "Downloading $MODEL_NAME (resumes if interrupted)."
      curl -fL --retry 10 --retry-delay 5 --continue-at - -o "$DEST.part" "$MODEL_URL"
    fi
    ACTUAL_SHA="$(sha256sum "$DEST.part" | cut -d' ' -f1)"
    [ "$ACTUAL_SHA" = "$EXPECTED_SHA" ] || die "SHA-256 mismatch for $DEST.part. Delete it and run again."
    mv -f "$DEST.part" "$DEST"
    chmod 0644 "$DEST"
    echo "Model verified."
  fi
fi

say "systemd units"
install -m 0755 "$HERE/glimmer-env.sh" "$PREFIX/bin/glimmer-env.sh"
install -m 0755 "$HERE/adapter_sync.sh" "$PREFIX/bin/adapter_sync.sh"
for unit in muse-privacy-glimmer.service muse-privacy-checker.service muse-privacy.service muse-privacy-relay.service; do
  install -m 0644 "$HERE/$unit" "$UNIT_DIR/$unit"
done
systemctl daemon-reload
if [ "$LIQUID" -eq 1 ]; then
  MODEL_UNIT=muse-privacy-checker.service
  # The server's Wants= on the Glimmer unit is ignored while it is masked. A mask is a link in
  # $UNIT_DIR, so the unit file installed there above has to go first.
  systemctl disable muse-privacy-glimmer.service 2>/dev/null || true
  rm -f "$UNIT_DIR/muse-privacy-glimmer.service"
  systemctl mask muse-privacy-glimmer.service
  systemctl daemon-reload
else
  MODEL_UNIT=muse-privacy-glimmer.service
fi
systemctl enable "$MODEL_UNIT" muse-privacy.service
if [ "$START" -eq 1 ]; then
  systemctl restart "$MODEL_UNIT" muse-privacy.service
fi

say "Done"
cat <<EOF
Next steps:
  1. Edit $CONF/env (APPROVAL_HOST so the laptop worker can reach this board, RELAY_URL, LIGHT_SERIAL).
  2. Start:   systemctl start $MODEL_UNIT muse-privacy
     Logs:    journalctl -u $MODEL_UNIT -u muse-privacy -f
  3. Pair the laptop worker: sudo -u $SERVICE_USER env BASE_HOME=$STATE/home $PREFIX/app/.venv/bin/python -m base.heavy.routes
     and copy $STATE/home/tls/cert.pem to the laptop for the worker's --ca-cert.
  4. After pairing the relay: systemctl enable --now muse-privacy-relay
  The board should boot without the desktop (multi-user.target): the model needs about 6.5 to 7.3 GB.
  From the unpacked bundle, check a model file first: uv run python -m scripts.plug_in_model FILE --check --device jetson
EOF
