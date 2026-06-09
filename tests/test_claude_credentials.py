import json
import os
import socket
import subprocess
import time
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "claude-credentials.sh"
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


def _write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def _run_script(tmp_path: Path, *args: str) -> tuple[subprocess.CompletedProcess[str], Path]:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    capture_file = tmp_path / "udp-payload"
    python_args_file = tmp_path / "python-args"

    _write_executable(
        bin_dir / "security",
        """#!/usr/bin/env bash
set -euo pipefail
if [[ "${1:-}" == "find-generic-password" ]]; then
  printf '%s' "$CLAUDE_CREDENTIALS_TEST_KEYCHAIN"
  exit 0
fi
echo "unexpected security invocation: $*" >&2
exit 2
""",
    )
    _write_executable(
        bin_dir / "python3",
        """#!/usr/bin/env bash
set -euo pipefail
cat > "$CLAUDE_CREDENTIALS_TEST_CAPTURE"
printf '%s\n' "$@" > "$CLAUDE_CREDENTIALS_TEST_PYTHON_ARGS"
""",
    )

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_CREDENTIALS_TEST_KEYCHAIN"] = KEYCHAIN_JSON
    env["CLAUDE_CREDENTIALS_TEST_CAPTURE"] = str(capture_file)
    env["CLAUDE_CREDENTIALS_TEST_PYTHON_ARGS"] = str(python_args_file)

    result = subprocess.run(
        ["bash", str(SCRIPT), *args],
        cwd=SCRIPT.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    return result, capture_file


def test_send_uses_raw_keychain_payload_by_default(tmp_path):
    result, capture_file = _run_script(tmp_path, "--send", "127.0.0.1", "--send-port", "59999")

    assert result.returncode == 0
    assert capture_file.read_text() == KEYCHAIN_JSON
    assert f"Sent {len(KEYCHAIN_JSON)} bytes to 127.0.0.1:59999 via TCP" in result.stderr


@pytest.mark.parametrize(
    "args",
    [
        ("--oauth-only", "--send", "example.test"),
        ("--send", "example.test", "--oauth-only"),
    ],
)
def test_oauth_only_send_uses_filtered_payload(tmp_path, args):
    result, capture_file = _run_script(tmp_path, *args)

    assert result.returncode == 0
    payload = json.loads(capture_file.read_text())
    assert payload == {"claudeAiOauth": json.loads(KEYCHAIN_JSON)["claudeAiOauth"]}
    assert "mcpOAuth" not in payload
    assert "other" not in payload


def test_oauth_only_alone_prints_filtered_payload(tmp_path):
    result, capture_file = _run_script(tmp_path, "--oauth-only")

    assert result.returncode == 0
    assert not capture_file.exists()
    assert json.loads(result.stdout) == {"claudeAiOauth": json.loads(KEYCHAIN_JSON)["claudeAiOauth"]}


def test_oauth_only_is_only_combinable_with_send(tmp_path):
    result, _capture_file = _run_script(tmp_path, "--oauth-only", "--receive")

    assert result.returncode == 1
    assert "Error: --oauth-only can only be used by itself or with --send" in result.stderr


def test_no_args_shows_help(tmp_path):
    result, _capture_file = _run_script(tmp_path)

    assert result.returncode == 0
    assert "usage:" in result.stdout.lower()
    # Must NOT dump credentials when invoked with no arguments.
    assert "accessToken" not in result.stdout


def test_raw_prints_full_blob(tmp_path):
    result, _capture_file = _run_script(tmp_path, "--raw")

    assert result.returncode == 0
    assert result.stdout.strip() == KEYCHAIN_JSON


def test_raw_conflicts_with_oauth_only(tmp_path):
    result, _capture_file = _run_script(tmp_path, "--raw", "--oauth-only")

    assert result.returncode == 1
    assert "Error: --oauth-only can only be used by itself or with --send" in result.stderr


# ── Real loopback TCP coverage ────────────────────────────────────────────────
# The tests above shim python3, so they never run the embedded socket code. The
# tests below leave python3 real and use loopback sockets, exercising the actual
# --send/--receive transport in claude-credentials.sh.

# `security` shim that also serves the bare-find account line and captures writes
# (the receive path discovers the account and calls add-generic-password).
SECURITY_SHIM_FULL = """#!/usr/bin/env bash
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


def _real_python_env(tmp_path, **extra):
    """Build an env that shims only `security`, leaving python3 real."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_executable(bin_dir / "security", SECURITY_SHIM_FULL)
    capture = tmp_path / "written"

    env = os.environ.copy()
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["CLAUDE_CREDENTIALS_TEST_KEYCHAIN"] = KEYCHAIN_JSON
    env["CLAUDE_CREDENTIALS_TEST_CAPTURE"] = str(capture)
    env.update(extra)
    return env, capture


def _tcp_server():
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    return srv, srv.getsockname()[1]


def _recv_all(conn):
    chunks = []
    while True:
        b = conn.recv(65535)
        if not b:
            break
        chunks.append(b)
    return b"".join(chunks)


def _free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _start_receiver(env, port):
    return subprocess.Popen(
        ["bash", str(SCRIPT), "--receive", "--port", str(port)],
        cwd=SCRIPT.parent,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def _connect_with_retry(port):
    for _ in range(50):
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=1)
        except OSError:
            time.sleep(0.1)
    return None


def test_send_real_tcp_delivers_full_blob(tmp_path):
    env, _capture = _real_python_env(tmp_path)
    srv, port = _tcp_server()
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--send", "127.0.0.1", "--send-port", str(port)],
            cwd=SCRIPT.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        srv.settimeout(5)
        conn, _addr = srv.accept()
        data = _recv_all(conn)
        conn.close()
        assert data.decode() == KEYCHAIN_JSON
        assert "via TCP" in result.stderr
    finally:
        srv.close()


def test_send_real_tcp_oauth_only_delivers_filtered_payload(tmp_path):
    env, _capture = _real_python_env(tmp_path)
    srv, port = _tcp_server()
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--oauth-only", "--send", "127.0.0.1", "--send-port", str(port)],
            cwd=SCRIPT.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        srv.settimeout(5)
        conn, _addr = srv.accept()
        data = _recv_all(conn)
        conn.close()
        assert json.loads(data) == {"claudeAiOauth": json.loads(KEYCHAIN_JSON)["claudeAiOauth"]}
        assert "(oauth-only)" in result.stderr
    finally:
        srv.close()


def test_receive_real_tcp_writes_to_keychain(tmp_path):
    env, capture = _real_python_env(tmp_path)
    port = _free_port()
    proc = _start_receiver(env, port)
    payload = '{"claudeAiOauth":{"accessToken":"received"}}'
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        client.sendall(payload.encode())
        client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0, proc.stderr.read()
    assert capture.read_text() == payload


def test_receive_real_tcp_rejects_oversized_payload(tmp_path):
    env, capture = _real_python_env(tmp_path, CLAUDE_CREDENTIALS_MAX_PAYLOAD="100")
    port = _free_port()
    proc = _start_receiver(env, port)
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        try:
            client.sendall(b"x" * 5000)
        except OSError:
            pass  # receiver may close mid-stream once the cap trips
        finally:
            client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert "exceeds" in proc.stderr.read().lower()
    assert not capture.exists()


def test_receive_real_tcp_times_out_on_idle_peer(tmp_path):
    env, capture = _real_python_env(tmp_path, CLAUDE_CREDENTIALS_RECV_TIMEOUT="1")
    port = _free_port()
    proc = _start_receiver(env, port)
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        # Connect but never send — the idle-read timeout must abort the receiver.
        proc.wait(timeout=5)
        client.close()
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert "idle" in proc.stderr.read().lower()
    assert not capture.exists()
