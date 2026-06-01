"""Tests for hook.statusline_main — the claude-monitor-statusline entry point."""

import io
import json
import os
import subprocess
import sys

import claude_monitor
import claude_monitor.hook as hook


def _setup(monkeypatch, tmp_path, stdin_data: str, env: dict | None = None):
    """Wire stdin/stdout/stderr and patch the rate-limits cache to tmp_path."""
    cache_file = str(tmp_path / "rate-limits-cache.json")
    monkeypatch.setattr(claude_monitor, "RATE_LIMITS_CACHE_FILE", cache_file)
    monkeypatch.setattr(claude_monitor, "SIGNAL_DIR", str(tmp_path))
    stdin = io.StringIO(stdin_data)
    stdout = io.StringIO()
    stderr = io.StringIO()
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", stderr)
    monkeypatch.delenv("CLAUDE_MONITOR_STATUSLINE_NEXT", raising=False)
    for k, v in (env or {}).items():
        monkeypatch.setenv(k, v)
    return stdout, stderr, cache_file


def test_writes_rate_limits_cache(tmp_path, monkeypatch):
    payload = {
        "rate_limits": {
            "five_hour": {"used_percentage": 42},
            "seven_day": {"used_percentage": 7},
        }
    }
    stdout, _stderr, cache_file = _setup(monkeypatch, tmp_path, json.dumps(payload))

    rc = hook.statusline_main()

    assert rc == 0
    assert os.path.exists(cache_file)
    with open(cache_file) as f:
        cached = json.load(f)
    assert cached["five_hour"]["used_percentage"] == 42
    assert cached["seven_day"]["used_percentage"] == 7
    assert "fetched_at" in cached
    # Built-in compact summary emitted when not chaining
    assert "5h:42%" in stdout.getvalue()
    assert "7d:7%" in stdout.getvalue()


def test_no_rate_limits_in_input(tmp_path, monkeypatch):
    """Input without rate_limits doesn't crash and doesn't write the cache."""
    stdout, _stderr, cache_file = _setup(monkeypatch, tmp_path, json.dumps({"foo": "bar"}))

    rc = hook.statusline_main()

    assert rc == 0
    assert not os.path.exists(cache_file)
    # Compact summary still emitted (with zeros)
    assert "5h:0%" in stdout.getvalue()


def test_invalid_json_input(tmp_path, monkeypatch):
    """Garbage stdin doesn't crash — cache skipped, summary emitted with zeros."""
    stdout, _stderr, cache_file = _setup(monkeypatch, tmp_path, "not json at all")

    rc = hook.statusline_main()

    assert rc == 0
    assert not os.path.exists(cache_file)
    assert "5h:0%" in stdout.getvalue()


def test_chains_to_configured_command(tmp_path, monkeypatch):
    """When CLAUDE_MONITOR_STATUSLINE_NEXT is set, the chained command runs
    with the original stdin and its stdout is forwarded."""
    payload = {"rate_limits": {"five_hour": {"used_percentage": 80}, "seven_day": {}}}
    # The chained command echoes a marker plus pipes stdin through wc -c to
    # prove it received the raw input.
    chained = "printf 'CHAINED:'; wc -c"
    stdout, stderr, cache_file = _setup(
        monkeypatch,
        tmp_path,
        json.dumps(payload),
        env={"CLAUDE_MONITOR_STATUSLINE_NEXT": chained},
    )

    rc = hook.statusline_main()

    assert rc == 0
    # Cache still written before the chain runs
    assert os.path.exists(cache_file)
    # Chained command's stdout is forwarded; built-in summary suppressed
    out = stdout.getvalue()
    assert out.startswith("CHAINED:")
    assert "5h:80%" not in out
    # wc -c output should match the byte length of our stdin
    byte_count = int(out.split(":", 1)[1].strip())
    assert byte_count == len(json.dumps(payload))


def test_chain_failure_falls_back_to_builtin(tmp_path, monkeypatch):
    """If the chained command fails to spawn (e.g. timeout), the built-in
    summary still appears so CC's status bar isn't blank."""
    payload = {"rate_limits": {"five_hour": {"used_percentage": 33}, "seven_day": {}}}
    stdout, stderr, _cache = _setup(
        monkeypatch,
        tmp_path,
        json.dumps(payload),
        env={"CLAUDE_MONITOR_STATUSLINE_NEXT": "this-command-does-not-exist-xyz"},
    )

    # subprocess.run with shell=True returns rc 127 for missing commands rather
    # than raising — that's a successful "chain" (just with non-zero rc).
    # To force fallback we monkey-patch subprocess.run to raise.
    def boom(*_a, **_kw):
        raise subprocess.SubprocessError("simulated spawn failure")

    monkeypatch.setattr("claude_monitor.hook.subprocess.run", boom)

    rc = hook.statusline_main()

    assert rc == 0
    assert "5h:33%" in stdout.getvalue()
    assert "chained command failed" in stderr.getvalue()


def test_first_invocation_captures_raw_stdin(tmp_path, monkeypatch):
    """The first time statusline_main runs, raw stdin is dumped for inspection."""
    raw = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 11}, "seven_day": {}}})
    _setup(monkeypatch, tmp_path, raw)
    dump_path = tmp_path / "cc-statusline-input.json"
    assert not dump_path.exists()

    hook.statusline_main()

    assert dump_path.exists()
    assert dump_path.read_text() == raw


def test_subsequent_invocations_do_not_overwrite_capture(tmp_path, monkeypatch):
    """Once the dump file exists, later runs leave it alone — `rm` to refresh."""
    dump_path = tmp_path / "cc-statusline-input.json"
    dump_path.write_text("FIRST RUN PAYLOAD")

    raw = json.dumps({"rate_limits": {"five_hour": {"used_percentage": 99}, "seven_day": {}}})
    _setup(monkeypatch, tmp_path, raw)

    hook.statusline_main()

    assert dump_path.read_text() == "FIRST RUN PAYLOAD"


def test_chain_returncode_is_propagated(tmp_path, monkeypatch):
    """Non-zero exit from the chained command propagates to our exit code."""
    payload = {"rate_limits": {"five_hour": {}, "seven_day": {}}}
    stdout, _stderr, _cache = _setup(
        monkeypatch,
        tmp_path,
        json.dumps(payload),
        env={"CLAUDE_MONITOR_STATUSLINE_NEXT": "exit 7"},
    )

    rc = hook.statusline_main()

    assert rc == 7
