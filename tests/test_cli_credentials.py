"""End-to-end tests for the claude-monitor-credentials CLI.

Drives the module via `python -m claude_monitor.cli_credentials` with the
`security` binary mocked by a temp shim on PATH; uses real loopback TCP sockets
for --send/--receive. The installed console script (claude-monitor-credentials)
is the same main(), so this exercises the real entry point.
"""

import json
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from claude_monitor import cli_credentials
from claude_monitor import transfer_crypto as tc

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE = "claude_monitor.cli_credentials"
PASSPHRASE = "test-transfer-passphrase"
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
    env["CLAUDE_CREDENTIALS_PASSPHRASE"] = PASSPHRASE  # --send/--receive are encrypted
    return env, capture


def _run(env, *args, stdin=None):
    return subprocess.run(
        [sys.executable, "-m", MODULE, *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )


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


def test_no_args_shows_help(cli_env):
    env, _ = cli_env
    res = _run(env)
    assert res.returncode == 0
    assert "usage:" in res.stdout.lower()
    assert "--oauth-only" in res.stdout
    # Must NOT dump credentials when invoked with no arguments.
    assert "accessToken" not in res.stdout


def test_raw_prints_full_blob(cli_env):
    env, _ = cli_env
    res = _run(env, "--raw")
    assert res.returncode == 0
    assert res.stdout.strip() == KEYCHAIN_JSON


def test_raw_conflicts_with_oauth_only(cli_env):
    env, _ = cli_env
    res = _run(env, "--raw", "--oauth-only")
    assert res.returncode != 0
    assert "oauth-only" in res.stderr.lower()


def test_raw_conflicts_with_other_modes(cli_env):
    env, _ = cli_env
    res = _run(env, "--raw", "--simple")
    assert res.returncode != 0
    assert "mutually exclusive" in res.stderr.lower()


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


def test_full_send_transmits_encrypted_blob(cli_env):
    env, _ = cli_env
    srv, port = _tcp_server()
    try:
        res = _run(env, "--send", "127.0.0.1", "--send-port", str(port))
        assert res.returncode == 0
        srv.settimeout(5)
        conn, _addr = srv.accept()
        data = _recv_all(conn)
        conn.close()
        # Wire bytes are an encrypted frame, not the plaintext blob.
        assert KEYCHAIN_JSON not in data.decode()
        assert tc.decrypt(data.decode(), PASSPHRASE) == KEYCHAIN_JSON
        assert f"encrypted bytes to 127.0.0.1:{port} via TCP" in res.stderr
        assert "(oauth-only)" not in res.stderr
    finally:
        srv.close()


@pytest.mark.parametrize("extra", [("--oauth-only",), ()])
def test_oauth_only_send_transmits_filtered_payload(cli_env, extra):
    env, _ = cli_env
    srv, port = _tcp_server()
    try:
        res = _run(env, "--send", "127.0.0.1", "--send-port", str(port), *extra)
        assert res.returncode == 0
        srv.settimeout(5)
        conn, _addr = srv.accept()
        data = _recv_all(conn)
        conn.close()
        decrypted = tc.decrypt(data.decode(), PASSPHRASE)
        if extra:
            assert json.loads(decrypted) == OAUTH_ONLY
            assert "(oauth-only)" in res.stderr
        else:
            assert decrypted == KEYCHAIN_JSON
    finally:
        srv.close()


def test_send_without_passphrase_errors(cli_env):
    env, _ = cli_env
    del env["CLAUDE_CREDENTIALS_PASSPHRASE"]
    srv, port = _tcp_server()
    try:
        # Empty stdin (a pipe, not a tty) → must error, not prompt.
        res = _run(env, "--send", "127.0.0.1", "--send-port", str(port), stdin="")
        assert res.returncode != 0
        assert "passphrase" in res.stderr.lower()
    finally:
        srv.close()


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


def _free_port():
    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


def _start_receiver(env, port):
    return subprocess.Popen(
        [sys.executable, "-m", MODULE, "--receive", "--port", str(port)],
        cwd=REPO_ROOT,
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


def test_receive_rejects_oversized_payload(cli_env):
    env, capture = cli_env
    env = {**env, "CLAUDE_CREDENTIALS_MAX_PAYLOAD": "100"}
    port = _free_port()
    proc = _start_receiver(env, port)
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        try:
            client.sendall(b"x" * 5000)  # well over the 100-byte cap
        except OSError:
            pass  # the receiver may close mid-stream once the cap trips
        finally:
            client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert "exceeds" in proc.stderr.read().lower()
    assert not capture.exists()  # nothing written to the keychain


def test_receive_times_out_on_idle_peer(cli_env):
    env, capture = cli_env
    env = {**env, "CLAUDE_CREDENTIALS_RECV_TIMEOUT": "1"}
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


def test_receive_decrypts_and_writes_to_keychain(cli_env):
    env, capture = cli_env
    port = _free_port()
    proc = _start_receiver(env, port)
    payload = '{"claudeAiOauth":{"accessToken":"received"}}'
    frame = tc.encrypt(payload, PASSPHRASE).encode()
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        client.sendall(frame)
        client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert capture.read_text() == payload  # decrypted plaintext written to keychain


def test_receive_rejects_wrong_passphrase(cli_env):
    env, capture = cli_env
    port = _free_port()
    proc = _start_receiver(env, port)
    # Frame encrypted under a DIFFERENT passphrase than the receiver expects.
    frame = tc.encrypt('{"claudeAiOauth":{"accessToken":"x"}}', "the-wrong-passphrase").encode()
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        client.sendall(frame)
        client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert "authentication failed" in proc.stderr.read().lower()
    assert not capture.exists()  # keychain left unchanged


def test_receive_without_passphrase_errors(cli_env):
    env, capture = cli_env
    del env["CLAUDE_CREDENTIALS_PASSPHRASE"]
    port = _free_port()
    # No passphrase + non-interactive stdin → fail fast, before listening.
    proc = subprocess.run(
        [sys.executable, "-m", MODULE, "--receive", "--port", str(port)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=10,
    )
    assert proc.returncode != 0
    assert "passphrase" in proc.stderr.lower()
    assert not capture.exists()


# ── In-process --refresh coverage ─────────────────────────────────────────────
# --refresh mutates the keychain via a network OAuth call, so it's driven
# in-process with creds.* monkeypatched rather than as a subprocess.


def test_refresh_writes_new_tokens_and_preserves_other_keys(monkeypatch, capsys):
    written = {}
    monkeypatch.setattr(
        cli_credentials.creds, "extract_oauth_tokens", lambda: ("old-access", "old-refresh", 1.0)
    )
    monkeypatch.setattr(
        cli_credentials.creds, "refresh_tokens", lambda rt: ("new-access", "new-refresh", 3600)
    )
    monkeypatch.setattr(
        cli_credentials.creds,
        "read_json",
        lambda: {"claudeAiOauth": {"accessToken": "old-access"}, "other": "keep"},
    )
    monkeypatch.setattr(cli_credentials.creds, "write", lambda content: written.update(c=content))

    rc = cli_credentials.main(["--refresh"])

    assert rc == 0
    assert "new-access" in capsys.readouterr().out
    saved = json.loads(written["c"])
    assert saved["claudeAiOauth"]["accessToken"] == "new-access"
    assert saved["claudeAiOauth"]["refreshToken"] == "new-refresh"
    assert saved["other"] == "keep"  # unrelated keychain keys survive the rewrite


def test_refresh_without_refresh_token_skips_network(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_credentials.creds, "extract_oauth_tokens", lambda: ("access", "", 1.0)
    )
    called = {}
    monkeypatch.setattr(
        cli_credentials.creds, "refresh_tokens", lambda rt: called.setdefault("hit", True)
    )

    rc = cli_credentials.main(["--refresh"])

    assert rc == 1
    assert "no refresh token" in capsys.readouterr().err.lower()
    assert "hit" not in called  # never attempted the network refresh


def test_refresh_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(
        cli_credentials.creds, "extract_oauth_tokens", lambda: ("access", "refresh", 1.0)
    )
    monkeypatch.setattr(cli_credentials.creds, "refresh_tokens", lambda rt: None)

    rc = cli_credentials.main(["--refresh"])

    assert rc == 1
    assert "refresh failed" in capsys.readouterr().err.lower()


def test_refresh_without_credentials_errors(monkeypatch, capsys):
    monkeypatch.setattr(cli_credentials.creds, "extract_oauth_tokens", lambda: None)

    rc = cli_credentials.main(["--refresh"])

    assert rc == 1
    assert "no oauth token" in capsys.readouterr().err.lower()


# ── Passphrase resolution (_get_passphrase) ───────────────────────────────────


class _FakeStdin:
    def __init__(self, tty):
        self._tty = tty

    def isatty(self):
        return self._tty


def test_passphrase_from_env(monkeypatch):
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PASSPHRASE", "hunter2")
    assert cli_credentials._get_passphrase() == "hunter2"


def test_empty_env_passphrase_treated_as_missing(monkeypatch):
    monkeypatch.setenv("CLAUDE_CREDENTIALS_PASSPHRASE", "")
    monkeypatch.setattr(cli_credentials.sys, "stdin", _FakeStdin(tty=False))
    with pytest.raises(cli_credentials.creds.CredentialsError, match="no passphrase"):
        cli_credentials._get_passphrase()


def test_empty_interactive_passphrase_rejected(monkeypatch):
    monkeypatch.delenv("CLAUDE_CREDENTIALS_PASSPHRASE", raising=False)
    monkeypatch.setattr(cli_credentials.sys, "stdin", _FakeStdin(tty=True))
    monkeypatch.setattr(cli_credentials.getpass, "getpass", lambda *a, **k: "")  # bare Enter
    with pytest.raises(cli_credentials.creds.CredentialsError, match="empty passphrase"):
        cli_credentials._get_passphrase()


def test_interactive_passphrase_accepted(monkeypatch):
    monkeypatch.delenv("CLAUDE_CREDENTIALS_PASSPHRASE", raising=False)
    monkeypatch.setattr(cli_credentials.sys, "stdin", _FakeStdin(tty=True))
    monkeypatch.setattr(cli_credentials.getpass, "getpass", lambda *a, **k: "typed-secret")
    assert cli_credentials._get_passphrase() == "typed-secret"
