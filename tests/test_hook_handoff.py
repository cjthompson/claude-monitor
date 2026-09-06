"""Tests for hook.py — session hand-off capture and injection wiring.

`claude_monitor.handoff` does not exist yet in this worktree (built
concurrently by another task) — we inject a stub module into
`sys.modules` so `hook.py`'s lazy `from claude_monitor import handoff`
resolves to it.
"""

import io
import json
import os
import sys
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

import claude_monitor


def _run_hook(input_data: dict, monkeypatch, env_vars=None):
    """Run hook.main() with mocked stdin/stdout/stderr.

    Returns (stdout_data, events_file_contents).
    """
    import claude_monitor.hook as hook

    stdin = io.StringIO(json.dumps(input_data))
    stdout = io.StringIO()

    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    mock_stderr = io.StringIO()
    mock_stderr.isatty = lambda: True
    mock_stderr.fileno = lambda: 2
    monkeypatch.setattr("sys.stderr", mock_stderr)
    monkeypatch.setattr("claude_monitor.hook.os.ttyname", lambda fd: "/dev/ttys999")

    if env_vars:
        for k, v in env_vars.items():
            monkeypatch.setenv(k, v)
    else:
        monkeypatch.delenv("ITERM_SESSION_ID", raising=False)

    hook.main()
    stdout.seek(0)
    stdout_content = stdout.read()

    events_path = hook.EVENTS_FILE
    events_content = ""
    if os.path.exists(events_path):
        with open(events_path) as f:
            events_content = f.read()

    return stdout_content, events_content


@pytest.fixture
def stub_handoff(monkeypatch):
    """Inject a fake claude_monitor.handoff module and return its mocks."""
    module = types.ModuleType("claude_monitor.handoff")
    module.capture = MagicMock(return_value=None)
    module.injection_context = MagicMock(return_value=None)
    monkeypatch.setitem(sys.modules, "claude_monitor.handoff", module)
    # sys.modules alone is not enough once the real module has been imported:
    # `from claude_monitor import handoff` prefers the package attribute.
    monkeypatch.setattr(claude_monitor, "handoff", module, raising=False)
    return module


def _settings(**overrides):
    base = dict(
        handoff_enabled=True,
        handoff_capture_on_session_end=True,
        handoff_inject_on_start=True,
        handoff_inject_max_age_hours=72,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


class TestSessionEnd:
    def test_capture_called_with_correct_args(self, isolated_state, monkeypatch, stub_handoff):
        settings = _settings()
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)

        data = {
            "hook_event_name": "SessionEnd",
            "session_id": "sess-1",
            "cwd": "/tmp/proj",
            "transcript_path": "/tmp/proj/transcript.jsonl",
        }
        _run_hook(data, monkeypatch)

        stub_handoff.capture.assert_called_once()
        args, kwargs = stub_handoff.capture.call_args
        assert args[0] == "sess-1"
        assert args[1] == "/tmp/proj"
        assert kwargs["transcript_path"] == "/tmp/proj/transcript.jsonl"
        assert kwargs["reason"] == "session_end"
        assert kwargs["with_llm"] is False
        assert kwargs["settings"] is settings
        assert 0 < kwargs["max_seconds"] < 5

    def test_no_capture_when_handoff_disabled(self, isolated_state, monkeypatch, stub_handoff):
        settings = _settings(handoff_enabled=False)
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)

        data = {"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": "/tmp"}
        _run_hook(data, monkeypatch)

        stub_handoff.capture.assert_not_called()

    def test_no_capture_when_capture_on_session_end_disabled(
        self, isolated_state, monkeypatch, stub_handoff
    ):
        settings = _settings(handoff_capture_on_session_end=False)
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)

        data = {"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": "/tmp"}
        _run_hook(data, monkeypatch)

        stub_handoff.capture.assert_not_called()

    def test_capture_raises_hook_still_exits_cleanly_and_logs(
        self, isolated_state, monkeypatch, stub_handoff
    ):
        settings = _settings()
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)
        stub_handoff.capture.side_effect = RuntimeError("boom")

        data = {"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": "/tmp"}
        stdout, events = _run_hook(data, monkeypatch)

        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["hook_event_name"] == "SessionEnd"

    def test_settings_load_failure_treated_as_off(self, isolated_state, monkeypatch, stub_handoff):
        def _boom():
            raise RuntimeError("settings broke")

        monkeypatch.setattr("claude_monitor.settings.load_settings", _boom)

        data = {"hook_event_name": "SessionEnd", "session_id": "s1", "cwd": "/tmp"}
        stdout, events = _run_hook(data, monkeypatch)

        assert stdout.strip() == ""
        stub_handoff.capture.assert_not_called()
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["hook_event_name"] == "SessionEnd"


class TestSessionStart:
    def test_writes_hook_specific_output_when_context_available(
        self, isolated_state, monkeypatch, stub_handoff
    ):
        settings = _settings()
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)
        stub_handoff.injection_context.return_value = "## Prior session\nDid stuff."

        data = {"hook_event_name": "SessionStart", "session_id": "sess-2", "cwd": "/tmp/proj"}
        stdout, _ = _run_hook(data, monkeypatch)

        result = json.loads(stdout)
        assert result["hookSpecificOutput"]["hookEventName"] == "SessionStart"
        assert result["hookSpecificOutput"]["additionalContext"] == "## Prior session\nDid stuff."

    def test_no_stdout_when_context_is_none(self, isolated_state, monkeypatch, stub_handoff):
        settings = _settings()
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)
        stub_handoff.injection_context.return_value = None

        data = {"hook_event_name": "SessionStart", "session_id": "sess-2", "cwd": "/tmp/proj"}
        stdout, _ = _run_hook(data, monkeypatch)

        assert stdout.strip() == ""

    def test_excludes_current_session_id(self, isolated_state, monkeypatch, stub_handoff):
        settings = _settings()
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)
        stub_handoff.injection_context.return_value = "context"

        data = {"hook_event_name": "SessionStart", "session_id": "sess-current", "cwd": "/tmp/proj"}
        _run_hook(data, monkeypatch)

        stub_handoff.injection_context.assert_called_once()
        args, kwargs = stub_handoff.injection_context.call_args
        assert args[0] == "/tmp/proj"
        assert kwargs["exclude_session_id"] == "sess-current"
        assert kwargs["max_age_hours"] == 72

    def test_disabled_settings_no_call(self, isolated_state, monkeypatch, stub_handoff):
        settings = _settings(handoff_inject_on_start=False)
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)

        data = {"hook_event_name": "SessionStart", "session_id": "sess-2", "cwd": "/tmp/proj"}
        stdout, _ = _run_hook(data, monkeypatch)

        assert stdout.strip() == ""
        stub_handoff.injection_context.assert_not_called()

    def test_injection_context_raises_exits_cleanly_no_stdout(
        self, isolated_state, monkeypatch, stub_handoff
    ):
        settings = _settings()
        monkeypatch.setattr("claude_monitor.settings.load_settings", lambda: settings)
        stub_handoff.injection_context.side_effect = RuntimeError("boom")

        data = {"hook_event_name": "SessionStart", "session_id": "sess-2", "cwd": "/tmp/proj"}
        stdout, events = _run_hook(data, monkeypatch)

        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["hook_event_name"] == "SessionStart"


class TestNoHandoffImportOnPermissionRequestPath:
    def test_permission_request_never_imports_handoff(self, isolated_state, monkeypatch):
        # Ensure handoff module isn't already imported/cached from another test.
        monkeypatch.delitem(sys.modules, "claude_monitor.handoff", raising=False)

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {"command": "ls"},
        }
        _run_hook(data, monkeypatch)

        assert "claude_monitor.handoff" not in sys.modules

    def test_existing_permission_request_flow_still_works(self, isolated_state, monkeypatch):
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {"command": "ls"},
        }
        stdout, events = _run_hook(data, monkeypatch)
        result = json.loads(stdout)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"
