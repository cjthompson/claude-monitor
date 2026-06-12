import base64
import hashlib
import hmac
import json
import os
import socket
import subprocess
import threading
import time
from pathlib import Path

import pytest

from claude_monitor import transfer_crypto as tc

SCRIPT = Path(__file__).resolve().parents[1] / "claude-credentials.sh"
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


# NOTE: --send/--receive now run real python3 (crypto), so they can't be tested
# with the python3-shimming _run_script helper — they are covered by the real
# loopback TCP tests further down, which encrypt/decrypt with transfer_crypto.


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
    env["CLAUDE_CREDENTIALS_PASSPHRASE"] = PASSPHRASE  # --send/--receive are encrypted
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


def _serve_one(srv, holder):
    # Accept, read to EOF, and close PROMPTLY. nc half-closes its write side on
    # stdin EOF, then waits (up to -w) for the peer's FIN before exiting; closing
    # here sends that FIN so nc returns immediately instead of burning the timeout.
    srv.settimeout(10)
    conn, _addr = srv.accept()
    try:
        holder.append(_recv_all(conn))
    finally:
        conn.close()


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


def test_send_real_tcp_delivers_encrypted_full_blob(tmp_path):
    # The bash sender encrypts; the Python transfer_crypto must decrypt it
    # (this is the bash->python interop check, against the real openssl binary).
    env, _capture = _real_python_env(tmp_path)
    srv, port = _tcp_server()
    holder = []
    server = threading.Thread(target=_serve_one, args=(srv, holder), daemon=True)
    server.start()
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--send", "127.0.0.1", "--send-port", str(port)],
            cwd=SCRIPT.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        server.join(timeout=10)
        assert result.returncode == 0, result.stderr
        data = holder[0]
        assert KEYCHAIN_JSON not in data.decode()  # on the wire it's encrypted
        assert tc.decrypt(data.decode(), PASSPHRASE) == KEYCHAIN_JSON
        assert "encrypted bytes" in result.stderr and "via TCP" in result.stderr
    finally:
        srv.close()


def test_send_real_tcp_oauth_only_delivers_filtered_payload(tmp_path):
    env, _capture = _real_python_env(tmp_path)
    srv, port = _tcp_server()
    holder = []
    server = threading.Thread(target=_serve_one, args=(srv, holder), daemon=True)
    server.start()
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--oauth-only", "--send", "127.0.0.1", "--send-port", str(port)],
            cwd=SCRIPT.parent,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        server.join(timeout=10)
        assert result.returncode == 0, result.stderr
        decrypted = tc.decrypt(holder[0].decode(), PASSPHRASE)
        assert json.loads(decrypted) == {"claudeAiOauth": json.loads(KEYCHAIN_JSON)["claudeAiOauth"]}
        assert "(oauth-only)" in result.stderr
    finally:
        srv.close()


def test_send_uses_nc_delegate(tmp_path):
    # The README/CHANGELOG advertise that --send delegates the outbound socket
    # to /usr/bin/nc (an Apple platform binary exempt from macOS Local Network
    # Privacy). Prove the bash frontend actually does so — a fake nc, injected
    # via CLAUDE_CREDENTIALS_NC, must receive the host/port and the frame.
    env, _capture = _real_python_env(tmp_path)
    nc_args = tmp_path / "nc-args"
    nc_stdin = tmp_path / "nc-stdin"
    fake_nc = tmp_path / "fake-nc"
    _write_executable(
        fake_nc,
        f"""#!/usr/bin/env bash
printf '%s\\n' "$@" > "{nc_args}"
cat > "{nc_stdin}"
""",
    )
    env["CLAUDE_CREDENTIALS_NC"] = str(fake_nc)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--send", "10.0.0.5", "--send-port", "47000"],
        cwd=SCRIPT.parent,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert nc_args.exists(), f"--send did not invoke the nc delegate; stderr={result.stderr}"
    assert result.returncode == 0, result.stderr
    args = nc_args.read_text()
    assert "10.0.0.5" in args and "47000" in args  # nc got the host + port
    # nc received the encrypted frame on stdin, and it decrypts to the blob.
    assert tc.decrypt(nc_stdin.read_text(), PASSPHRASE) == KEYCHAIN_JSON


def test_send_real_tcp_requires_passphrase(tmp_path):
    env, _capture = _real_python_env(tmp_path)
    del env["CLAUDE_CREDENTIALS_PASSPHRASE"]
    srv, port = _tcp_server()
    try:
        result = subprocess.run(
            ["bash", str(SCRIPT), "--send", "127.0.0.1", "--send-port", str(port)],
            cwd=SCRIPT.parent,
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,  # non-tty → must error, not prompt
            check=False,
        )
        assert result.returncode != 0
        assert "passphrase" in result.stderr.lower()
    finally:
        srv.close()


def test_receive_real_tcp_decrypts_and_writes_to_keychain(tmp_path):
    # Python transfer_crypto encrypts; the bash receiver must decrypt it
    # (python->bash interop check).
    env, capture = _real_python_env(tmp_path)
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
    assert proc.returncode == 0, proc.stderr.read()
    assert capture.read_text() == payload  # decrypted plaintext written


def test_receive_real_tcp_rejects_wrong_passphrase(tmp_path):
    env, capture = _real_python_env(tmp_path)
    port = _free_port()
    proc = _start_receiver(env, port)
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


def test_bash_rejects_empty_interactive_passphrase(tmp_path):
    # A bare Enter at the prompt must be rejected, not used as the shared secret.
    # Drive --send over a real pty (so the script prompts) and feed an empty line;
    # get_passphrase rejects before any network connection happens.
    pty = pytest.importorskip("pty")
    import select

    env, capture = _real_python_env(tmp_path)
    del env["CLAUDE_CREDENTIALS_PASSPHRASE"]

    pid, fd = pty.fork()
    if pid == 0:  # child: bash sees a tty on stdin and prompts
        try:
            os.chdir(str(SCRIPT.parent))
            os.execvpe(
                "bash",
                ["bash", str(SCRIPT), "--send", "127.0.0.1", "--send-port", "59998"],
                env,
            )
        except Exception:
            os._exit(127)

    output = b""
    try:
        os.write(fd, b"\n")  # empty passphrase + Enter
        for _ in range(40):
            r, _w, _e = select.select([fd], [], [], 0.25)
            if not r:
                continue
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            output += chunk
            if b"empty passphrase" in output:
                break
        os.waitpid(pid, 0)
    finally:
        os.close(fd)

    assert b"empty passphrase" in output, output
    assert not capture.exists()  # rejected before reading/sending anything


def test_receive_real_tcp_rejects_empty_decrypted_payload(tmp_path):
    # A valid-HMAC frame whose plaintext is empty must NOT overwrite the keychain
    # with nothing. Without a guard, `security add-generic-password -w ""` would
    # wipe the receiver's credentials — reject it and leave them untouched.
    env, capture = _real_python_env(tmp_path)
    port = _free_port()
    proc = _start_receiver(env, port)
    frame = tc.encrypt("", PASSPHRASE).encode()  # authentic, but decrypts to ""
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        client.sendall(frame)
        client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = proc.stderr.read()
    assert proc.returncode != 0
    assert "empty" in err.lower()
    assert not capture.exists()  # keychain left unchanged


def test_receive_real_tcp_rejects_authenticated_undecryptable_frame(tmp_path):
    # Valid HMAC (the peer knows the passphrase) but the ciphertext can't be
    # decrypted — length is not a multiple of the AES block. The bash receiver
    # must reject it with a clean message, NOT a raw Python/openssl traceback,
    # and leave the keychain unchanged. (Parity with the Python decrypt path.)
    env, capture = _real_python_env(tmp_path)
    port = _free_port()
    proc = _start_receiver(env, port)
    salt = bytes(range(16))
    _enc_key, _iv, mac_key = tc._derive(PASSPHRASE, salt)
    blob = salt + b"short"  # 5 bytes -> not a multiple of the block size
    tag = hmac.new(mac_key, blob, hashlib.sha256).hexdigest()
    frame = (base64.b64encode(blob).decode() + "\n" + tag + "\n").encode()
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        client.sendall(frame)
        client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    err = proc.stderr.read()
    assert proc.returncode != 0
    assert "Traceback" not in err, err  # clean message, not a raw traceback
    assert "keychain left unchanged" in err.lower()
    assert not capture.exists()


def test_receive_real_tcp_rejects_malformed_frame(tmp_path):
    env, capture = _real_python_env(tmp_path)
    port = _free_port()
    proc = _start_receiver(env, port)
    try:
        client = _connect_with_retry(port)
        assert client is not None, "receiver never started listening"
        client.sendall(b"not-a-valid-frame\nzzzz\nextra\n")  # wrong shape + bad tag
        client.close()
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode != 0
    assert "malformed transfer frame" in proc.stderr.read().lower()
    assert not capture.exists()
