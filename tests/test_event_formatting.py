"""Tests for event formatting logic."""

import time

import pytest

from claude_monitor.formatting import _oneline, _format_ask_user_question_inline


class TestEventFormatting:
    """Test event formatting in SimpleTUI."""

    async def test_permission_allowed_format(self, app_fixture, inject_message):
        """Allowed permission event formats correctly."""
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la"},
            "session_id": "sess-fmt-1",
            "_decision": "allowed",
        }
        label, detail = app_fixture._format_event(data, "PermissionRequest")
        assert label is not None
        assert "ALLOWED" in label
        assert "Bash" in detail
        assert "ls -la" in detail

    async def test_permission_deferred_format(self, app_fixture, inject_message):
        """Deferred permission event formats correctly."""
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.txt"},
            "session_id": "sess-fmt-2",
            "_decision": "deferred",
        }
        label, detail = app_fixture._format_event(data, "PermissionRequest")
        assert "DEFERRED" in label
        assert "Write" in detail

    async def test_subagent_start_format(self, app_fixture, inject_message):
        data = {
            "hook_event_name": "SubagentStart",
            "agent_id": "agent-123-abc-def",
            "agent_type": "general_purpose",
            "session_id": "sess-fmt-3",
        }
        label, detail = app_fixture._format_event(data, "SubagentStart")
        assert "AGENT+" in label
        assert "general_purpose" in detail

    async def test_subagent_stop_format(self, app_fixture, inject_message):
        data = {
            "hook_event_name": "SubagentStop",
            "agent_id": "agent-123-abc-def",
            "agent_type": "general_purpose",
            "session_id": "sess-fmt-4",
        }
        label, detail = app_fixture._format_event(data, "SubagentStop")
        assert "AGENT-" in label

    async def test_notification_idle_format(self, app_fixture, inject_message):
        data = {
            "hook_event_name": "Notification",
            "notification_type": "idle_prompt",
            "message": "Session is idle",
            "session_id": "sess-fmt-5",
        }
        label, detail = app_fixture._format_event(data, "Notification")
        assert "IDLE" in label

    def test_newline_collapsing(self):
        """Multi-line text should be collapsed with arrow symbols."""
        text = "line one\nline two\nline three"
        result = _oneline(text)
        assert "\n" not in result
        assert "line one" in result
        assert "line two" in result

    async def test_timestamp_24hr_format(self, app_fixture, inject_message):
        """Default 24hr timestamp format."""
        from datetime import datetime
        ts = datetime(2024, 3, 15, 14, 30, 45)
        result = app_fixture._format_ts(ts)
        assert "14:30:45" in result

    async def test_timestamp_12hr_format(self, app_fixture, inject_message):
        """12hr timestamp format."""
        app_fixture.settings.timestamp_style = "12hr"
        from datetime import datetime
        ts = datetime(2024, 3, 15, 14, 30, 45)
        result = app_fixture._format_ts(ts)
        assert "pm" in result.lower() or "PM" in result
