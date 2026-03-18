"""Extended tests for hook.py — decide_permission edge cases."""

import pytest

from claude_monitor.hook import decide_permission


class TestDecidePermission:
    def test_allowed_default(self):
        state = {"global_paused": False, "paused_sessions": [], "paused_claude_sessions": []}
        event = {"_iterm_session_id": "uuid1", "session_id": "s1", "tool_name": "Bash"}
        assert decide_permission(state, event) == ("allowed", 0)

    def test_global_paused(self):
        state = {"global_paused": True}
        event = {"tool_name": "Bash"}
        assert decide_permission(state, event) == ("deferred", 0)

    def test_iterm_session_paused(self):
        state = {"global_paused": False, "paused_sessions": ["uuid1"]}
        event = {"_iterm_session_id": "uuid1", "tool_name": "Bash"}
        assert decide_permission(state, event) == ("deferred", 0)

    def test_claude_session_paused(self):
        state = {"global_paused": False, "paused_sessions": [], "paused_claude_sessions": ["cs1"]}
        event = {"session_id": "cs1", "tool_name": "Bash"}
        assert decide_permission(state, event) == ("deferred", 0)

    def test_excluded_tool(self):
        state = {"global_paused": False, "paused_sessions": [], "paused_claude_sessions": [], "excluded_tools": ["Bash"]}
        event = {"tool_name": "Bash"}
        assert decide_permission(state, event) == ("deferred", 0)

    def test_ask_user_question_timeout(self):
        state = {
            "global_paused": False, "paused_sessions": [], "paused_claude_sessions": [],
            "excluded_tools": [], "ask_user_timeout": 30,
        }
        event = {"tool_name": "AskUserQuestion", "_iterm_session_id": "uuid1"}
        decision, timeout = decide_permission(state, event)
        assert decision == "timeout"
        assert timeout == 30

    def test_ask_user_question_paused(self):
        state = {
            "global_paused": False, "paused_sessions": [], "paused_claude_sessions": [],
            "excluded_tools": [], "ask_paused_sessions": ["uuid1"],
        }
        event = {"tool_name": "AskUserQuestion", "_iterm_session_id": "uuid1"}
        assert decide_permission(state, event) == ("deferred", 0)

    def test_ask_user_question_no_timeout(self):
        state = {
            "global_paused": False, "paused_sessions": [], "paused_claude_sessions": [],
            "excluded_tools": [], "ask_user_timeout": 0,
        }
        event = {"tool_name": "AskUserQuestion"}
        assert decide_permission(state, event) == ("allowed", 0)

    def test_missing_state_keys(self):
        """Empty state dict should not crash."""
        state = {}
        event = {"tool_name": "Bash"}
        assert decide_permission(state, event) == ("allowed", 0)

    def test_no_iterm_session_id(self):
        """Event without _iterm_session_id still works."""
        state = {"global_paused": False, "paused_sessions": ["uuid1"]}
        event = {"tool_name": "Bash"}
        # No _iterm_session_id, so paused_sessions check skipped
        assert decide_permission(state, event) == ("allowed", 0)
