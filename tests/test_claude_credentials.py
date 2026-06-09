import json
import os
import subprocess
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
