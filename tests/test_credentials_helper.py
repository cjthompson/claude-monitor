"""End-to-end tests for the credentials-helper.py CLI.

Drives the real script via subprocess with the `security` binary mocked by a
temp shim on PATH; uses real loopback UDP sockets for --send/--receive.
"""

import json
import os
import socket
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "credentials-helper.py"
KEYCHAIN_JSON = json.dumps(
    {
        "claudeAiOauth": {
            "accessToken": "access-token",
            "refreshToken": "refresh-token",
            "expiresAt": 1790000000000,
        },
        "mcpOAuth": {"machineSpecific": "do-not-send"},
        "other": "kept-in-raw-mode",
    },
    separators=(",", ":"),
)
OAUTH_ONLY = {"claudeAiOauth": json.loads(KEYCHAIN_JSON)["claudeAiOauth"]}

# `security` shim: serve the blob for `find ... -w`, an acct line for bare
# `find`, and capture the written content for `add-generic-password`.
SECURITY_SHIM = """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "find-generic-password" ]]; then
  if [[ "$*" == *" -w"* ]]; then
    printf '%s' "$CLAUDE_CREDENTIALS_TEST_KEYCHAIN"
  else
    printf '    "acct"<blob>="tester"\\n'
  fi
  exit 0
fi
if [[ "${1:-}" == "add-generic-password" ]]; then
  prev=""
  for a in "$@"; do
    if [[ "$prev" == "-w" ]]; then printf '%s' "$a" > "$CLAUDE_CREDENTIALS_TEST_CAPTURE"; fi
    prev="$a"
  done
  exit 0
fi
echo "unexpected security invocation: $*" >&2
exit 2
"""


@pytest.fixture
def cli_env(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    shim = bin_dir / "security"
    shim.write_text(SECURITY_SHIM)
    shim.chmod(0o755)
    capture = tmp_path / "written"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_CREDENTIALS_TEST_KEYCHAIN"] = KEYCHAIN_JSON
    env["CLAUDE_CREDENTIALS_TEST_CAPTURE"] = str(capture)
    return env, capture


def _run(env, *args, stdin=None):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *args],
        cwd=SCRIPT.parent,
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )


def _udp_listener():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("127.0.0.1", 0))
    return sock, sock.getsockname()[1]


def test_default_prints_raw_blob(cli_env):
    env, _ = cli_env
    res = _run(env)
    assert res.returncode == 0
    assert res.stdout.strip() == KEYCHAIN_JSON


def test_oauth_only_prints_filtered_payload(cli_env):
    env, _ = cli_env
    res = _run(env, "--oauth-only")
    assert res.returncode == 0
    assert json.loads(res.stdout) == OAUTH_ONLY


def test_simple_prints_token_fields(cli_env):
    env, _ = cli_env
    res = _run(env, "--simple")
    assert res.returncode == 0
    assert "access_token:  access-token" in res.stdout
    assert "refresh_token: refresh-token" in res.stdout
    assert "expires_at:    1790000000000" in res.stdout


def test_full_send_transmits_raw_blob(cli_env):
    env, _ = cli_env
    sock, port = _udp_listener()
    try:
        res = _run(env, "--send", "127.0.0.1", "--send-port", str(port))
        assert res.returncode == 0
        sock.settimeout(5)
        data, _addr = sock.recvfrom(65535)
        assert data.decode() == KEYCHAIN_JSON
        assert f"Sent {len(KEYCHAIN_JSON)} bytes to 127.0.0.1:{port} via UDP" in res.stderr
        assert "(oauth-only)" not in res.stderr
    finally:
        sock.close()


@pytest.mark.parametrize(
    "extra",
    [("--oauth-only",), ()],
)
def test_oauth_only_send_transmits_filtered_payload(cli_env, extra):
    env, _ = cli_env
    sock, port = _udp_listener()
    try:
        res = _run(env, "--send", "127.0.0.1", "--send-port", str(port), *extra)
        assert res.returncode == 0
        sock.settimeout(5)
        data, _addr = sock.recvfrom(65535)
        if extra:
            assert json.loads(data) == OAUTH_ONLY
            assert "(oauth-only)" in res.stderr
        else:
            assert data.decode() == KEYCHAIN_JSON
    finally:
        sock.close()


def test_oauth_only_only_combinable_with_send(cli_env):
    env, _ = cli_env
    res = _run(env, "--oauth-only", "--simple")
    assert res.returncode != 0
    assert "oauth-only" in res.stderr.lower()


def test_primary_modes_mutually_exclusive(cli_env):
    env, _ = cli_env
    res = _run(env, "--simple", "--refresh")
    assert res.returncode != 0
    assert "mutually exclusive" in res.stderr.lower()


def test_import_stdin_writes_verbatim(cli_env):
    env, capture = cli_env
    payload = '{"claudeAiOauth":{"accessToken":"imported"}}'
    res = _run(env, "--import", "-", stdin=f"  {payload}\n")
    assert res.returncode == 0
    assert capture.read_text() == payload  # surrounding whitespace trimmed


def test_receive_writes_datagram_to_keychain(cli_env):
    env, capture = cli_env
    sock, port = _udp_listener()
    port_for_receive = port
    sock.close()  # free the port for the receiver to bind

    proc = subprocess.Popen(
        [sys.executable, str(SCRIPT), "--receive", "--port", str(port_for_receive)],
        cwd=SCRIPT.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    payload = '{"claudeAiOauth":{"accessToken":"received"}}'
    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # The receiver needs a moment to bind; resend until it exits.
        for _ in range(50):
            sender.sendto(payload.encode(), ("127.0.0.1", port_for_receive))
            try:
                proc.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                continue
        proc.wait(timeout=5)
    finally:
        sender.close()
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert capture.read_text() == payload
