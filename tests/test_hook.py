"""Tests for hook.py — hook decision logic."""

import io
import json
import os
import sys
import time

import pytest

from tests.conftest import _make_permission_event, _make_notification_event, _make_subagent_event


def _run_hook(input_data: dict, monkeypatch, env_vars=None):
    """Run hook.main() with mocked stdin/stdout/stderr, return (stdout_data, events_file_contents)."""
    import claude_monitor.hook as hook

    stdin = io.StringIO(json.dumps(input_data))
    stdout = io.StringIO()

    monkeypatch.setattr("sys.stdin", stdin)
    monkeypatch.setattr("sys.stdout", stdout)
    # Create a mock stderr that has isatty() → True and fileno() → 2
    mock_stderr = io.StringIO()
    mock_stderr.isatty = lambda: True
    mock_stderr.fileno = lambda: 2
    monkeypatch.setattr("sys.stderr", mock_stderr)
    # Mock os.ttyname so it doesn't fail on fd 2 in test environment
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


class TestHookDecisionLogic:
    """Test the hook's auto-allow/defer logic."""

    def test_permission_request_auto_allow(self, isolated_state, monkeypatch):
        """Unpaused state → hook outputs allow decision."""
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

    def test_permission_request_paused_global(self, isolated_state, monkeypatch):
        """global_paused=true → no allow output."""
        # Write paused state
        state = {"global_paused": True, "paused_sessions": [], "paused_claude_sessions": []}
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {},
        }
        stdout, events = _run_hook(data, monkeypatch)
        assert stdout.strip() == ""  # No allow output

    def test_permission_request_paused_per_session(self, isolated_state, monkeypatch):
        """Session in paused_sessions (iTerm) → deferred."""
        state = {
            "global_paused": False,
            "paused_sessions": ["my-iterm-uuid"],
            "paused_claude_sessions": [],
        }
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {},
        }
        stdout, events = _run_hook(data, monkeypatch, env_vars={"ITERM_SESSION_ID": "w0t0p0:my-iterm-uuid"})
        assert stdout.strip() == ""
        # Check the logged event has deferred decision
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["_decision"] == "deferred"

    def test_permission_request_paused_claude_session(self, isolated_state, monkeypatch):
        """Session in paused_claude_sessions → deferred."""
        state = {
            "global_paused": False,
            "paused_sessions": [],
            "paused_claude_sessions": ["sess-1"],
        }
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {},
        }
        stdout, events = _run_hook(data, monkeypatch)
        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["_decision"] == "deferred"

    def test_excluded_tool(self, isolated_state, monkeypatch):
        """Tool in excluded_tools → deferred with _excluded_tool flag."""
        state = {
            "global_paused": False,
            "paused_sessions": [],
            "paused_claude_sessions": [],
            "excluded_tools": ["Bash"],
        }
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {},
        }
        stdout, events = _run_hook(data, monkeypatch)
        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["_decision"] == "deferred"
        assert logged["_excluded_tool"] is True

    def test_ask_user_question_pause(self, isolated_state, monkeypatch):
        """Session in ask_paused_sessions, tool=AskUserQuestion → deferred."""
        state = {
            "global_paused": False,
            "paused_sessions": [],
            "paused_claude_sessions": [],
            "excluded_tools": [],
            "ask_paused_sessions": ["my-iterm-uuid"],
        }
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {"question": "Continue?"},
        }
        stdout, events = _run_hook(data, monkeypatch, env_vars={"ITERM_SESSION_ID": "w0t0p0:my-iterm-uuid"})
        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["_decision"] == "deferred"

    def test_notification_event_logged(self, isolated_state, monkeypatch):
        """Notification event → appended to events.jsonl, no stdout output."""
        data = {
            "hook_event_name": "Notification",
            "notification_type": "idle_prompt",
            "session_id": "sess-1",
            "message": "Session is idle",
        }
        stdout, events = _run_hook(data, monkeypatch)
        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["hook_event_name"] == "Notification"
        assert logged["notification_type"] == "idle_prompt"

    def test_subagent_events_logged(self, isolated_state, monkeypatch):
        """SubagentStart/Stop → appended to events.jsonl."""
        data_start = {
            "hook_event_name": "SubagentStart",
            "agent_id": "agent-1",
            "agent_type": "general_purpose",
            "session_id": "sess-1",
        }
        stdout, events = _run_hook(data_start, monkeypatch)
        assert stdout.strip() == ""
        logged = json.loads(events.strip().split("\n")[-1])
        assert logged["hook_event_name"] == "SubagentStart"

    def test_event_metadata_added(self, isolated_state, monkeypatch):
        """Hook adds _timestamp, _tty, _iterm_session_id to logged events."""
        data = {
            "hook_event_name": "Notification",
            "notification_type": "idle_prompt",
            "session_id": "sess-1",
            "message": "test",
        }
        stdout, events = _run_hook(data, monkeypatch, env_vars={"ITERM_SESSION_ID": "w0t0p5:uuid-123-456"})
        logged = json.loads(events.strip().split("\n")[-1])
        assert "_timestamp" in logged
        assert isinstance(logged["_timestamp"], float)
        assert "_tty" in logged
        assert logged["_iterm_session_id"] == "uuid-123-456"

    def test_hook_reads_state_file(self, isolated_state, monkeypatch):
        """State changes in state.json affect hook behavior."""
        # First call: unpaused → allow
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "sess-1",
            "cwd": "/tmp/test",
            "tool_input": {},
        }
        stdout1, _ = _run_hook(data, monkeypatch)
        assert "allow" in stdout1

        # Now pause
        state = {"global_paused": True, "paused_sessions": [], "paused_claude_sessions": []}
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        # Second call: paused → no output
        stdout2, _ = _run_hook(data, monkeypatch)
        assert stdout2.strip() == ""
