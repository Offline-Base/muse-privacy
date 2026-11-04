"""Jetson units and scripts, scripts/start.sh, and the cloud scripts' offline modes."""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

from tests.synthetic_helpers import mark_synthetic

REPO = Path(__file__).resolve().parent.parent
DEPLOY = REPO / "deploy" / "jetson"
UNITS = sorted(DEPLOY.glob("*.service"))
BASH_SCRIPTS = [DEPLOY / "install.sh", DEPLOY / "adapter_sync.sh", REPO / "scripts" / "start.sh",
                REPO / "scripts" / "supervise.sh", REPO / "scripts" / "install_login.sh"]
SH_SCRIPTS = [DEPLOY / "glimmer-env.sh"]
SCANNED = [*UNITS, *BASH_SCRIPTS, *SH_SCRIPTS, DEPLOY / "README.md", REPO / "hardware" / "light" / "README.md",
           REPO / "scripts" / "cloud" / "README.md"]
CLOUD = REPO / "scripts" / "cloud"
HARDENING = {
    "User": "muse-privacy",
    "NoNewPrivileges": "yes",
    "ProtectSystem": "strict",
    "ProtectHome": "yes",
    "PrivateTmp": "yes",
    "ReadWritePaths": "/var/lib/muse-privacy",
    "Restart": "on-failure",
}


def parse_unit(path: Path) -> dict[str, dict[str, list[str]]]:
    """A small systemd INI parser: sections of key -> list of values (keys may repeat)."""
    sections: dict[str, dict[str, list[str]]] = {}
    current = None
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith(("#", ";")):
            continue
        if line.startswith("[") and line.endswith("]"):
            current = sections.setdefault(line[1:-1], {})
            continue
        assert current is not None, f"{path.name}:{n}: key outside a section"
        assert "=" in line, f"{path.name}:{n}: not key=value"
        assert not line.endswith("\\"), f"{path.name}:{n}: keep each setting on one line"
        key, _, value = line.partition("=")
        assert re.fullmatch(r"[A-Za-z]+", key), f"{path.name}:{n}: bad key {key!r}"
        current.setdefault(key, []).append(value)
    return sections


def test_the_units_exist():
    assert [u.name for u in UNITS] == [
        "muse-privacy-checker.service",
        "muse-privacy-glimmer.service",
        "muse-privacy-relay.service",
        "muse-privacy.service",
    ]


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_unit_parses_and_is_hardened(unit):
    sections = parse_unit(unit)
    assert set(sections) == {"Unit", "Service", "Install"}
    service = sections["Service"]
    for key, value in HARDENING.items():
        assert service.get(key) == [value], f"{unit.name}: {key}"
    families = service["RestrictAddressFamilies"][0].split()
    assert set(families) <= {"AF_UNIX", "AF_INET", "AF_INET6"}
    assert service["ExecStart"][0].startswith("/opt/muse-privacy/")
    assert sections["Install"]["WantedBy"] == ["multi-user.target"]
    if unit.name not in ("muse-privacy-glimmer.service", "muse-privacy-checker.service"):
        assert "MemoryMax" in service


def test_glimmer_unit_command():
    service = parse_unit(DEPLOY / "muse-privacy-glimmer.service")["Service"]
    cmd = service["ExecStart"][0]
    assert cmd.startswith("/opt/muse-privacy/llama.cpp/")
    assert " --model ${GLIMMER_GGUF} " in cmd  # braced: one argument; glimmer-env.sh writes it
    unit = parse_unit(DEPLOY / "muse-privacy-glimmer.service")["Unit"]
    assert not any(p.endswith(".gguf") for p in unit["AssertPathExists"])  # glimmer-env.sh checks the model
    for flag in ("--host 127.0.0.1", "--port 8081", "-c 4096", "-np 1", "-ngl 99", "--no-mmproj"):
        assert f" {flag}" in cmd
    assert cmd.endswith(" $LORA_ARGS")  # unbraced: splits into zero or more arguments
    assert service["ExecStartPre"] == ["/opt/muse-privacy/bin/glimmer-env.sh"]
    assert service["EnvironmentFile"] == ["-/var/lib/muse-privacy/glimmer.env"]
    assert service["IPAddressDeny"] == ["any"] and service["IPAddressAllow"] == ["localhost"]


def test_python_units_set_device_and_backend():
    for name, module in (("muse-privacy.service", "-m base"), ("muse-privacy-relay.service", "-m base.connector.relay_client run")):
        service = parse_unit(DEPLOY / name)["Service"]
        assert service["ExecStart"][0].endswith(module)
        env = service["Environment"]
        assert "BASE_DEVICE=jetson" in env and "GLIMMER_BACKEND=llama_server" in env
        assert any(e.startswith("BASE_HOME=/var/lib/muse-privacy/") for e in env)


IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d.])")
HOSTISH = re.compile(r"\b[\w-]+\.(?:local|lan|internal|ts\.net)\b|ngrok|jetson-\d|\b\w+@[\w.-]+:", re.I)


@pytest.mark.parametrize("path", SCANNED, ids=lambda p: p.name)
def test_no_wildcard_bind_ips_or_hostnames(path):
    text = path.read_text()
    assert not re.search(r"(?<![\d.])0\.0\.0\.0(?![\d/])", text)  # a wildcard bind; "10.0.0.0/8" is a range
    # 127.0.0.1, and the private network ranges the server unit may reach (never a real address)
    private_ranges = {"10.0.0.0", "172.16.0.0", "192.168.0.0"}
    assert set(IPV4.findall(text)) <= {"127.0.0.1", *private_ranges}, path.name
    assert not HOSTISH.search(text), HOSTISH.search(text)


@pytest.mark.parametrize("script", BASH_SCRIPTS, ids=lambda p: p.name)
def test_bash_syntax(script):
    subprocess.run(["bash", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", SH_SCRIPTS, ids=lambda p: p.name)
def test_sh_syntax(script):
    subprocess.run(["sh", "-n", str(script)], check=True)


@pytest.mark.parametrize("script", [*BASH_SCRIPTS, *SH_SCRIPTS], ids=lambda p: p.name)
def test_scripts_are_executable_with_shebang(script):
    assert script.read_text().startswith("#!")
    assert os.access(script, os.X_OK)


def code_lines(path: Path) -> str:
    return "\n".join(line for line in path.read_text().splitlines() if not line.lstrip().startswith("#"))


def test_install_never_reboots_or_touches_power():
    code = code_lines(DEPLOY / "install.sh")
    for word in ("reboot", "shutdown", "nvpmodel", "jetson_clocks", "poweroff"):
        assert word not in code
    assert "https://huggingface.co/mradermacher/Muse-Glimmer-30B-i1-GGUF/resolve/main/Muse-Glimmer-30B.i1-IQ1_S.gguf" in (
        (DEPLOY / "install.sh").read_text().replace('$MODEL_REPO', 'mradermacher/Muse-Glimmer-30B-i1-GGUF')
        .replace('$MODEL_NAME', 'Muse-Glimmer-30B.i1-IQ1_S.gguf'))
    assert "--continue-at -" in code and "sha256sum" in code and "/api/models/" in code


def test_install_help_runs_anywhere():
    out = subprocess.run(["bash", str(DEPLOY / "install.sh"), "--help"], capture_output=True, text=True, check=True)
    assert "--build-llama" in out.stdout


def test_adapter_sync_rejects_non_gguf(tmp_path):
    bad = tmp_path / "router.gguf"
    bad.write_bytes(b"NOPE")
    r = subprocess.run(["bash", str(DEPLOY / "adapter_sync.sh"), str(bad)], capture_output=True, text=True)
    assert r.returncode != 0 and "not a GGUF" in r.stderr
    code = code_lines(DEPLOY / "adapter_sync.sh")
    assert "/lora-adapters" in code and "127.0.0.1:8081" in code


def glimmer_env(tmp_path, site: str | None = None, adapter: bool = False):
    default = tmp_path / "Muse-Glimmer-30B.i1-IQ1_S.gguf"
    default.write_bytes(b"GGUF")
    if adapter:
        (tmp_path / "router.gguf").write_bytes(b"GGUF")
    if site is not None:
        (tmp_path / "site.env").write_text(site)
    env = {**os.environ, "MUSE_ADAPTER": str(tmp_path / "router.gguf"), "MUSE_GLIMMER_ENV": str(tmp_path / "glimmer.env"),
           "MUSE_SITE_ENV": str(tmp_path / "site.env"), "MUSE_DEFAULT_MODEL": str(default),
           "GLIMMER_GGUF": "/an/old/start.gguf"}  # what last start's glimmer.env loaded: never used
    return subprocess.run(["sh", str(DEPLOY / "glimmer-env.sh")], env=env, capture_output=True, text=True)


@pytest.mark.parametrize("with_adapter", [True, False])
def test_glimmer_env_writes_the_model_and_lora_args(tmp_path, with_adapter):
    assert glimmer_env(tmp_path, adapter=with_adapter).returncode == 0
    lines = (tmp_path / "glimmer.env").read_text().splitlines()
    assert lines[0] == f"GLIMMER_GGUF={tmp_path / 'Muse-Glimmer-30B.i1-IQ1_S.gguf'}"  # no site setting: the IQ1_S file
    adapter = tmp_path / "router.gguf"
    assert lines[1] == (f'LORA_ARGS="--lora {adapter} --lora-init-without-apply"' if with_adapter else "LORA_ARGS=")


def test_glimmer_env_takes_the_sites_last_model_line_and_refuses_a_missing_one(tmp_path):
    tuned = tmp_path / "glimmer-tuned-iq1_s.gguf"
    tuned.write_bytes(b"GGUF")
    site = f"RELAY_URL=\nGLIMMER_GGUF=/opt/old.gguf\nGLIMMER_GGUF={tuned}\n"
    assert glimmer_env(tmp_path, site).returncode == 0
    assert (tmp_path / "glimmer.env").read_text().splitlines()[0] == f"GLIMMER_GGUF={tuned}"
    tuned.unlink()
    r = glimmer_env(tmp_path, site)
    assert r.returncode == 1 and "no model at" in r.stderr
    r = glimmer_env(tmp_path, "GLIMMER_GGUF=models/relative.gguf\n")
    assert r.returncode == 1 and "absolute path" in r.stderr


def test_checker_unit_matches_the_laptops_liquid_server():
    sections = parse_unit(DEPLOY / "muse-privacy-checker.service")
    cmd = sections["Service"]["ExecStart"][0]
    for flag in ("--host 127.0.0.1", "--port 8083", "-c 16384", "-np 1", "-ngl 99", "--jinja", "--reasoning-budget 0",
                 "--mmproj /opt/muse-privacy/models/mmproj-LFM3-VL-2B-Q8_0.gguf", "--alias lfm3-vl-2b",
                 "--api-key-file /var/lib/muse-privacy/home/llama_api_key"):
        assert f" {flag}" in cmd
    start = (REPO / "scripts" / "start_checker.sh").read_text()
    for flag in ("--temp 0.1", "--top-k 50", "--repeat-penalty 1.1", "--cache-ram 0", "--ctx-checkpoints 0"):
        assert flag in start and f" {flag}" in cmd
    assert sections["Service"]["IPAddressDeny"] == ["any"] and sections["Service"]["IPAddressAllow"] == ["localhost"]
    assert any(p.endswith("LFM3-VL-2B-Q4_K_M.gguf") for p in sections["Unit"]["AssertPathExists"])


def test_install_liquid_mode():
    code = code_lines(DEPLOY / "install.sh")
    assert "set_env DEVICE_ONLY 1" in code and "set_env PRIMARY_MODEL lfm" in code
    assert "systemctl mask muse-privacy-glimmer.service" in code
    for name in ("LFM3-VL-2B-Q4_K_M.gguf", "mmproj-LFM3-VL-2B-Q8_0.gguf"):
        assert name in code
    out = subprocess.run(["bash", str(DEPLOY / "install.sh"), "--liquid", "--skip-model"], capture_output=True, text=True)
    assert out.returncode != 0 and "--liquid downloads its own model" in out.stderr
    out = subprocess.run(["bash", str(DEPLOY / "install.sh"), "--llama-release", "11970"], capture_output=True, text=True)
    assert out.returncode != 0 and "a tag like b11970" in out.stderr
    assert 'sha256sum "$TMP"' in code and "cuda-13.4-arm64.tar.gz" in code


def test_install_copies_a_usb_model_with_its_sha_and_builds_the_laptops_llama_cpp():
    text = (DEPLOY / "install.sh").read_text()
    assert 'LLAMA_REF="${LLAMA_REF:-b11972}"' in text
    code = code_lines(DEPLOY / "install.sh")
    # the copy branch comes first, so --model-file never reaches the download
    assert code.index('elif [ -n "$MODEL_FILE" ]; then') < code.index('-o "$DEST.part" "$MODEL_URL"')
    assert "GLIMMER_GGUF=" in code and "sha256sum \"$DEST.part\"" in code
    assert "git -C \"$APP_SRC\" status --porcelain" in code and "scripts.make_bundle" in code
    out = subprocess.run(["bash", str(DEPLOY / "install.sh"), "--help"], capture_output=True, text=True, check=True)
    assert "--model-file" in out.stdout and "--model-sha256" in out.stdout


@pytest.mark.parametrize(
    ("args", "says"),
    [(["--model-file", "x.gguf"], "go together"), (["--model-sha256", "ab"], "go together"),
     (["--model-file", "README.md", "--model-sha256", "0" * 64], ".gguf file")],
)
def test_install_checks_the_model_file_options_first(args, says, tmp_path):
    if args[0] == "--model-file" and args[1] == "README.md":
        args = ["--model-file", str(DEPLOY / "README.md"), *args[2:]]
    r = subprocess.run(["bash", str(DEPLOY / "install.sh"), *args], capture_output=True, text=True)
    assert r.returncode == 1 and says in r.stderr


# -- scripts/start.sh ----------------------------------------------------------------------------


def run_start(tmp_path, **env):
    full = {"PATH": os.environ["PATH"], "HOME": str(tmp_path), "BASE_HOME": str(tmp_path / "home"),
            "APPROVAL_HOST": "127.0.0.1", **env}
    return subprocess.run(["bash", str(REPO / "scripts" / "start.sh")], env=full, capture_output=True, text=True,
                          timeout=60)


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").exists(), reason="needs the uv virtualenv")
def test_start_refuses_jetson(tmp_path):
    r = run_start(tmp_path, BASE_DEVICE="jetson", GLIMMER_BACKEND="llama_server")
    assert r.returncode != 0 and "BASE_DEVICE=laptop" in r.stderr


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").exists(), reason="needs the uv virtualenv")
def test_start_needs_llama_server_bin(tmp_path):
    r = run_start(tmp_path, BASE_DEVICE="laptop", DEVICE_ONLY="0", GLIMMER_BACKEND="llama_server",
                  LLAMA_SERVER_BIN=str(tmp_path / "missing"), GLIMMER_GGUF=str(tmp_path / "missing.gguf"))
    assert r.returncode != 0 and "LLAMA_SERVER_BIN" in r.stderr


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").exists(), reason="needs the uv virtualenv")
def test_start_needs_gguf(tmp_path):
    fake_bin = tmp_path / "llama-server"
    fake_bin.write_text("#!/bin/sh\nexit 1\n")
    fake_bin.chmod(0o755)
    r = run_start(tmp_path, BASE_DEVICE="laptop", DEVICE_ONLY="0", GLIMMER_BACKEND="llama_server", LLAMA_SERVER_BIN=str(fake_bin),
                  GLIMMER_GGUF=str(tmp_path / "missing.gguf"))
    assert r.returncode != 0 and "GLIMMER_GGUF" in r.stderr


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").exists(), reason="needs the uv virtualenv")
def test_start_refuses_lan_bind_on_laptop(tmp_path):
    r = run_start(tmp_path, BASE_DEVICE="laptop", GLIMMER_BACKEND="openrouter", APPROVAL_HOST="192.0.2.11")
    assert r.returncode != 0 and "127.0.0.1" in r.stderr


def test_start_binds_loopback_only():
    code = code_lines(REPO / "scripts" / "start.sh")
    hosts = re.findall(r"--host (\S+)", code)
    assert hosts and set(hosts) == {"127.0.0.1"}
    assert "--port 8081" in code and "--port 8082" in code and "/health" in code


def test_drafter_flags_only_with_a_drafter():
    code = code_lines(REPO / "scripts" / "start.sh")
    branch = re.search(r'if \[ -n "\$GLIMMER_DRAFT_GGUF" \]; then\n(.*?)\n  fi\n', code, re.S)
    assert branch, "no GLIMMER_DRAFT_GGUF branch"
    for flag in ("-md ", "-ngld 99", "--spec-type draft-dflash", "--spec-draft-n-max"):
        assert flag in branch.group(1) and code.count(flag) == 1, flag
    # The knobs reach Glimmer's servers only, never the checker or the embedder.
    assert code.count('"${GLIMMER_FLAGS[@]}"') == 3
    for name in ("start_checker.sh", "start_embedder.sh"):
        assert "GLIMMER_FLAGS" not in code_lines(REPO / "scripts" / name)


def port_answers(port: int) -> bool:
    import socket

    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def glimmer_start(tmp_path, **knobs):
    """Run start.sh with a fake llama-server that writes its arguments and stops, so nothing binds."""
    args = tmp_path / "args.txt"
    fake = tmp_path / "llama-server"
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$@" > "{args}"\nexit 1\n')
    fake.chmod(0o755)
    (tmp_path / "model.gguf").write_bytes(b"GGUF")
    env = {"BASE_DEVICE": "laptop", "DEVICE_ONLY": "1", "PRIMARY_MODEL": "glimmer", "GLIMMER_BACKEND": "llama_server",
           "CHECKER_BACKEND": "lfm", "LLAMA_SERVER_BIN": str(fake), "GLIMMER_GGUF": str(tmp_path / "model.gguf"),
           "ANSWER_URL": "http://127.0.0.1:8081/v1", "ROUTER_URL": "http://127.0.0.1:8081/v1", "ANSWER_CTX": "4096",
           "WORKSPACE_ROOT": str(tmp_path), "SEARCH_EMBEDDINGS": "0", "RELAY_URL": "", "GLIMMER_DRAFT_GGUF": "",
           "GLIMMER_DRAFT_N": "", "GLIMMER_FA": "", "GLIMMER_REASONING_BUDGET": "", **knobs}  # fmt: skip
    r = run_start(tmp_path, **env)
    return r, (args.read_text().splitlines() if args.exists() else None)


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").exists(), reason="needs the uv virtualenv")
def test_start_passes_the_glimmer_knobs_to_llama_server(tmp_path):
    if port_answers(8081):
        pytest.skip("something answers on 127.0.0.1:8081")
    r, args = glimmer_start(tmp_path)
    assert r.returncode == 1 and "Glimmer stopped while starting" in r.stderr
    assert args[args.index("--host") + 1] == "127.0.0.1" and args[args.index("-fa") + 1] == "auto"
    assert args[args.index("--reasoning-budget") + 1] == "-1" and "-md" not in args
    draft = tmp_path / "draft.gguf"
    draft.write_bytes(b"GGUF")
    r, args = glimmer_start(tmp_path, GLIMMER_DRAFT_GGUF=str(draft), GLIMMER_DRAFT_N="7", GLIMMER_FA="on",
                            GLIMMER_REASONING_BUDGET="0")
    assert args[args.index("-md") + 1] == str(draft) and args[args.index("-ngld") + 1] == "99"
    assert args[args.index("--spec-type") + 1] == "draft-dflash" and args[args.index("--spec-draft-n-max") + 1] == "7"
    assert args[args.index("-fa") + 1] == "on" and args[args.index("--reasoning-budget") + 1] == "0"
    assert "draft.gguf" in r.stdout and str(tmp_path) not in r.stdout


@pytest.mark.skipif(not (REPO / ".venv" / "bin" / "python").exists(), reason="needs the uv virtualenv")
@pytest.mark.parametrize(
    ("knobs", "message"),
    [({"GLIMMER_FA": "maybe"}, "GLIMMER_FA"), ({"GLIMMER_REASONING_BUDGET": "-5"}, "GLIMMER_REASONING_BUDGET"),
     ({"GLIMMER_DRAFT_GGUF": "/nonexistent/draft.gguf"}, "GLIMMER_DRAFT_GGUF")],
)
def test_start_refuses_bad_glimmer_knobs_before_starting_anything(tmp_path, knobs, message):
    r, args = glimmer_start(tmp_path, **knobs)
    assert r.returncode == 1 and message in r.stderr and args is None


# -- staying up (scripts/supervise.sh) -----------------------------------------------------------

BASH = "/bin/bash" if Path("/bin/bash").exists() else "bash"  # macOS's bash 3.2 when there is one


def supervise(tmp_path, script: str, timeout=30, **env):
    full = {"PATH": os.environ["PATH"], **env}
    lib = REPO / "scripts" / "supervise.sh"
    return subprocess.run([BASH, "-c", f"set -euo pipefail; . '{lib}'; {script}"], cwd=tmp_path, env=full,
                          capture_output=True, text=True, timeout=timeout)


def test_an_optional_helper_restarts_with_a_growing_pause_and_stops_on_exit_3(tmp_path):
    helper = tmp_path / "helper.sh"
    helper.write_text('#!/bin/sh\nn=$(cat count 2>/dev/null || echo 0); n=$((n + 1)); echo $n > count\n'
                      'echo "run $n"\n[ "$n" -ge 3 ] && exit 3\nexit 1\n')
    helper.chmod(0o755)
    r = supervise(tmp_path, 'keep_running "the helper" log.txt ./helper.sh & wait $!', RESTART_DELAY="1",
                  RESTART_MAX_DELAY="2")
    assert r.returncode == 0
    assert "the helper stopped (exit 1); starting it again in 1s" in r.stderr
    assert "starting it again in 2s" in r.stderr and "trying again cannot help (exit 3)" in r.stderr
    assert (tmp_path / "log.txt").read_text() == "run 1\nrun 2\nrun 3\n"


def test_stopping_the_loop_stops_its_helper(tmp_path):
    r = supervise(tmp_path, 'keep_running "a sleeper" log.txt sleep 30 & sup=$!; sleep 1; '
                            'child=$(pgrep -P $sup); kill $sup; wait $sup || true; sleep 0.5; '
                            'if kill -0 $child 2>/dev/null; then echo alive; else echo gone; fi')
    assert r.returncode == 0 and r.stdout.strip() == "gone"


def test_wait_up_stops_at_once_when_the_helper_loop_has_stopped(tmp_path):
    # start_sam.sh exits 3 with no weights, so keep_running returns and nothing will ever answer:
    # waiting the full time would keep the relay client from starting for minutes.
    started = time.monotonic()
    r = supervise(tmp_path, 'true & loop=$!; wait $loop; if wait_up "http://127.0.0.1:9/health" 60 "$loop"; '
                            'then echo up; else echo down; fi')
    assert r.returncode == 0 and r.stdout.strip() == "down" and time.monotonic() - started < 15


def test_start_connects_the_relay_before_it_waits_for_sam():
    code = code_lines(REPO / "scripts" / "start.sh")
    sam_wait = code.index('wait_up "http://127.0.0.1:$SAM_PORT/health" 180 "$SAM_LOOP"')
    assert code.index('keep_running "the relay client"') < sam_wait and code.index("caffeinate -i") < sam_wait
    assert code.index("watch_required") > sam_wait


def test_a_required_process_stopping_is_named(tmp_path):
    r = supervise(tmp_path, 'sleep 30 & require $! "a long one"; sleep 1 & require $! "the app"; '
                            'watch_required; echo "stopped: $STOPPED"; kill %1', WATCH_EVERY="1")
    assert r.returncode == 0 and r.stdout.strip() == "stopped: the app"


def test_start_requires_the_models_and_the_app_but_keeps_helpers_running():
    code = code_lines(REPO / "scripts" / "start.sh")
    required = re.findall(r'require "\$(\w+)"', code)
    assert required == ["ANSWER_PID", "ROUTER_PID", "CHECKER_PID", "APP_PID"]
    assert 'keep_running "the embedder"' in code and 'keep_running "the relay client"' in code
    assert "watch_required" in code and "a process this script started has stopped" not in code


def test_the_login_agent_restarts_without_throttling_and_finds_homebrew():
    import plistlib

    text = (REPO / "scripts" / "install_login.sh").read_text()
    body = text.split("<<PLIST\n", 1)[1].split("\nPLIST\n", 1)[0]
    for name, value in {"$LABEL": "ai.example", "$ROOT": "/repo", "$LOGS": "/logs"}.items():
        body = body.replace(name, value)
    plist = plistlib.loads(body.encode())
    assert plist["KeepAlive"] is True and plist["RunAtLoad"] is True and plist["ThrottleInterval"] == 30
    assert plist["ProcessType"] == "Interactive" and plist["ProgramArguments"] == ["/repo/scripts/start.sh"]
    assert "/opt/homebrew/bin" in plist["EnvironmentVariables"]["PATH"].split(":")
    assert plist["StandardOutPath"] == "/logs/start.log"


# -- cloud scripts -------------------------------------------------------------------------------


def run_cloud(name, *args):
    # A fresh BASE_HOME, so a script's defaults never read the live ~/.base (its tag map holds real names).
    env = {**os.environ, "BASE_HOME": tempfile.mkdtemp(prefix="base-home-")}
    return subprocess.run([sys.executable, str(CLOUD / name), *args], capture_output=True, text=True, timeout=60,
                          cwd=str(REPO), env=env)


def test_modal_not_needed_for_offline_modes():
    for name in ("quant_eval_modal.py", "train_lora_modal.py"):
        source = (CLOUD / name).read_text()
        top = source.split("def build_app")[0]
        assert "import modal" not in top, name


@pytest.mark.parametrize("name", ["quant_eval_modal.py", "train_lora_modal.py"])
def test_cloud_help_runs_without_modal(name):
    r = run_cloud(name, "--help")
    assert r.returncode == 0 and "--check" in r.stdout


def make_eval_fixture(root: Path, synthetic: bool = True) -> list[str]:
    essays = root / "demo" / "class"
    essays.mkdir(parents=True)
    (essays / "rubric.md").write_text("# Rubric\n- Ideas\n- Organization\n")
    students = [{"name": f"Testname{i} Example", "first": f"Testname{i}", "last": "Example", "nicknames": []}
                for i in range(20)]
    for i, s in enumerate(students):
        (essays / f"essay_{i:02d}.md").write_text(f"My summer, by {s['first']}. I went to the lake with my cousin.\n")
    roster = {"teacher": {"name": "Teacher Example", "first": "Teacher", "last": "Example"}, "students": students}
    (root / "roster.json").write_text(json.dumps(roster))
    items = []
    for d in ("share_tagged", "share_summary", "ask_user", "refuse"):
        for k in range(5):
            items.append({"id": f"{d}-{k}", "request": "How did [Student A] do?", "result": "Testname1 scored 3.",
                          "result_kind": "per_student", "topics": [], "expected": d, "names": ["Testname1"]})
    (root / "tasks.jsonl").write_text("\n".join(json.dumps(i) for i in items))
    (root / "policy.yaml").write_text("name: invented\n")
    if synthetic:
        mark_synthetic(root / "demo")
    return ["--essays", str(essays), "--roster", str(root / "roster.json"), "--tasks", str(root / "tasks.jsonl"),
            "--policy", str(root / "policy.yaml")]


def test_quant_eval_check_passes_on_invented_inputs(tmp_path):
    r = run_cloud("quant_eval_modal.py", "--check", *make_eval_fixture(tmp_path))
    assert r.returncode == 0, r.stderr
    assert "inputs ok" in r.stdout and "lookups: 20" in r.stdout


def test_quant_eval_finds_rubric_one_folder_up(tmp_path):
    args = make_eval_fixture(tmp_path)
    essays = Path(args[1])
    (essays / "rubric.md").rename(essays.parent / "rubric.md")  # the demo/ layout: rubric beside essays/
    (essays / "README.md").write_text("Not an essay.\n")
    mark_synthetic(essays.parent)  # both are new paths for the manifest
    r = run_cloud("quant_eval_modal.py", "--check", *args)
    assert r.returncode == 0, r.stderr
    assert "essays: 20" in r.stdout


def test_quant_eval_check_refuses_unmarked_folder(tmp_path):
    r = run_cloud("quant_eval_modal.py", "--check", *make_eval_fixture(tmp_path, synthetic=False))
    assert r.returncode == 1 and ".synthetic" in r.stderr


def test_quant_eval_check_refuses_a_stray_file_in_a_marked_folder(tmp_path):
    args = make_eval_fixture(tmp_path)
    (Path(args[1]) / "notes.md").write_text("Not in the manifest.\n")
    r = run_cloud("quant_eval_modal.py", "--check", *args)
    assert r.returncode == 1 and ".synthetic-manifest.json" in r.stderr and "notes.md" not in r.stderr


def test_quant_eval_report_shape(tmp_path):
    sys.path.insert(0, str(CLOUD))
    try:
        import quant_eval_modal as q
    finally:
        sys.path.remove(str(CLOUD))
    result = {
        "names": [{"expected": ["Testname1 Example"], "found": ["Testname1"]}],
        "lookups": [{"id": "a", "expected": "refuse", "got": "refuse"}, {"id": "b", "expected": "ask_user", "got": None}],
        "grades": [{"file": "e.md", "total": 7, "reference_total": 8}],
        "parse_failures": 1, "prompt_tps": 900.0, "gen_tps": 40.5, "size_bytes": 6_500_000_000, "seconds": 60,
        "file": "x.gguf", "repo": "example/repo",
    }
    out = tmp_path / "quantization.md"
    q.write_report({"Q4_K_M": result, "i1-IQ1_S": result}, "uv run python scripts/cloud/quant_eval_modal.py --run", out)
    text = out.read_text()
    assert text.splitlines()[2].startswith("Command: `uv run")
    assert "| i1-IQ1_S | 6.5 GB | 900.0 | 40.5 | 100% | 50% | 0% | 1.00 | 1 |" in text
    assert (tmp_path / "raw" / "quantization.json").is_file()


def test_train_check_passes_and_counts_labels(tmp_path):
    data = tmp_path / "train.jsonl"
    rows = [
        {"request": "Share the class average", "result_kind": "class_aggregate", "tagged_text": "Average 3.1",
         "label": "share_summary", "weight": 3},
        {"messages": [{"role": "user", "content": "x"}, {"role": "assistant", "content": '{"decision": "refuse"}'}]},
    ]
    data.write_text("\n".join(json.dumps(r) for r in rows))
    r = run_cloud("train_lora_modal.py", "--check", "--data", str(data), "--roster", str(tmp_path / "none.json"),
                  "--tagmap", str(tmp_path / "none.json"))  # never the live BASE_HOME's tag map
    assert r.returncode == 0, r.stderr
    assert '"refuse": 1' in r.stdout and '"share_summary": 1' in r.stdout


@pytest.mark.parametrize(
    "text",
    ["Call 512-555-0143 after school", "Email parent@example.com", "SSN 900-12-3456", "Card 4111 1111 1111 1111",
     "Testname3 asked for help"],
)
def test_train_check_refuses_identifiers_and_names(tmp_path, text):
    roster = tmp_path / "roster.json"
    roster.write_text(json.dumps({"teacher": {}, "students": [{"name": "Testname3 Example", "first": "Testname3",
                                                               "last": "Example"}]}))
    data = tmp_path / "train.jsonl"
    data.write_text(json.dumps({"request": "r", "result_kind": "free_text", "tagged_text": text, "label": "refuse"}))
    r = run_cloud("train_lora_modal.py", "--check", "--data", str(data), "--roster", str(roster),
                  "--tagmap", str(tmp_path / "none.json"))
    assert r.returncode == 1
    assert text not in r.stderr  # problems name the line and kind, never the value


# ----- review fixes: device F1, F2, F9, SEC-1, SEC-5, SEC-9 -----


@pytest.mark.parametrize("unit", UNITS, ids=lambda p: p.name)
def test_protect_clock_never_blocks_devices(unit):
    # On systemd before 252, ProtectClock implies DeviceAllow=char-rtc and blocks every other
    # device node: the Jetson GPU for llama-server, the light's serial port for the server.
    service = parse_unit(unit)["Service"]
    if service.get("ProtectClock") == ["yes"]:
        assert service.get("PrivateDevices") == ["yes"] or "DeviceAllow" in service, unit.name


def test_glimmer_unit_memory_and_key_flags():
    service = parse_unit(DEPLOY / "muse-privacy-glimmer.service")["Service"]
    cmd = service["ExecStart"][0]
    for flag in ("-ub 128", "--cache-ram 0", "--ctx-checkpoints 0", "--swa-full", "--no-slots"):
        assert f" {flag} " in cmd, flag
    assert " --api-key-file /var/lib/muse-privacy/home/llama_api_key " in cmd
    assert " --api-key " not in cmd  # a key on the command line shows in /proc


def test_server_unit_cannot_reach_the_internet():
    service = parse_unit(DEPLOY / "muse-privacy.service")["Service"]
    assert service["IPAddressDeny"] == ["any"]
    allowed = service["IPAddressAllow"][0].split()
    assert "localhost" in allowed and "0.0.0.0/0" not in allowed


def test_relay_unit_sees_only_its_two_files_and_stops_on_auth_errors():
    service = parse_unit(DEPLOY / "muse-privacy-relay.service")["Service"]
    assert service["TemporaryFileSystem"] == ["/var/lib/muse-privacy:ro"]
    bound = service["BindReadOnlyPaths"][0].split()
    assert sorted(Path(b).name for b in bound) == ["connector_token", "relay.json"]
    assert service["RestartPreventExitStatus"] == ["3"]


@pytest.mark.parametrize(
    "text",
    ["camila wrote three pages", "kowalczyk asked for help", "essays/camila_restrepo_draft.md",
     "Call 512‑555‑0143 after school", "C a m i l a R e s t r e p o"],
)
def test_train_check_decodes_and_uses_the_guard(tmp_path, text):
    # Review privacy F9: lowercase names, slugs and escaped Unicode dashes passed the old check.
    data = tmp_path / "train.jsonl"
    data.write_text(json.dumps({"request": "r", "result_kind": "free_text", "tagged_text": text, "label": "refuse"}))
    r = run_cloud("train_lora_modal.py", "--check", "--data", str(data), "--roster", str(REPO / "eval" / "roster.json"),
                  "--tagmap", str(tmp_path / "none.json"))
    assert r.returncode == 1, r.stdout
    assert text not in r.stderr


def test_train_labels_follow_the_chat_template():
    sys.path.insert(0, str(CLOUD))
    import importlib

    mod = importlib.import_module("train_lora_modal")

    class Tok:  # one token per character
        def __call__(self, text, add_special_tokens=False):
            return {"input_ids": [ord(c) for c in text]}

    full = "<|start|>user<|message|>hi<|eot|><|start|>assistant to=user<|message|>{}<|eot|>"
    prefix = "<|start|>user<|message|>hi<|eot|><|start|>assistant"
    ids, labels = mod.split_labels(Tok(), full, prefix, 4096)
    learned = "".join(chr(t) for t in labels if t != -100)
    assert learned == " to=user<|message|>{}<|eot|>"  # the header and the end token are learned too
    assert mod.target_modules(r".*language_model.*\.(q_proj)$").startswith(".*")
    assert mod.target_modules("q_proj, v_proj") == ["q_proj", "v_proj"]


def test_quant_eval_survives_a_failed_request_and_uses_the_jetson_flags(tmp_path, monkeypatch):
    # Review device F8: one 500 or timeout used to lose every model's results after a paid download.
    sys.path.insert(0, str(CLOUD))
    try:
        import quant_eval_modal as q
    finally:
        sys.path.remove(str(CLOUD))
    calls = []

    def flaky(url, messages, schema, max_tokens=2500):
        calls.append(1)
        if len(calls) == 3:
            raise TimeoutError("slow")
        return {"names": [], "decision": "refuse", "total": 8}, {}

    monkeypatch.setattr(q, "chat", flaky)
    payload = {"essays": [{"text": "x", "names": [], "file": f"{i}.md"} for i in range(q.N_NAMES)],
               "lookups": [{"id": "a", "request": "r", "result_kind": "snippet", "text": "t", "expected": "refuse"}],
               "policy": "", "rubric": "r"}
    out = q.run_jobs("http://127.0.0.1:1", payload, None)
    assert out["request_errors"] == {"TimeoutError": 1} and out["parse_failures"] == 1
    unit = (DEPLOY / "muse-privacy-glimmer.service").read_text()
    for flag in ("-c 4096", "-ub 128", "--cache-ram 0", "--ctx-checkpoints 0"):
        assert flag in " ".join(q.JETSON_FLAGS) and flag in unit
    report = tmp_path / "quantization.md"
    q.write_report({"i1-IQ1_S": {"failed": "RuntimeError", "file": "x.gguf", "repo": "example/repo"}}, "cmd", report)
    assert "not run: RuntimeError" in report.read_text()
