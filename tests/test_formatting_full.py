"""Comprehensive tests for formatting.py — all event types and edge cases."""

import time
from unittest.mock import MagicMock

from claude_monitor.formatting import (
    _safe_css_id,
    _safe_tab_css_id,
    _oneline,
    _format_ask_user_question_inline,
    _format_ask_user_question_detail,
    format_event,
)


# ---------------------------------------------------------------------------
# Pure helper functions
# ---------------------------------------------------------------------------

class TestSafeCssId:
    def test_basic(self):
        assert _safe_css_id("abc-def") == "panel-abcdef"

    def test_with_colons_and_slashes(self):
        result = _safe_css_id("w0:abc/def")
        assert ":" not in result
        assert "/" not in result
        assert result.startswith("panel-")


class TestSafeTabCssId:
    def test_basic(self):
        assert _safe_tab_css_id("abc-def") == "tab-abcdef"

    def test_with_dots(self):
        result = _safe_tab_css_id("abc.def")
        assert "." not in result


class TestOneline:
    def test_single_line(self):
        assert _oneline("hello") == "hello"

    def test_multiline(self):
        result = _oneline("line one\nline two\nline three")
        assert "line one" in result
        assert "line two" in result
        assert "\n" not in result
        assert "\u21b5" in result  # return symbol

    def test_with_max_len(self):
        result = _oneline("a very long line\nanother line", max_len=10)
        assert len(result) == 10

    def test_empty_lines_skipped(self):
        result = _oneline("hello\n\n\nworld")
        assert result == "hello \u21b5 world"

    def test_empty_string(self):
        assert _oneline("") == ""


# ---------------------------------------------------------------------------
# AskUserQuestion formatting
# ---------------------------------------------------------------------------

class TestFormatAskUserQuestionInline:
    def test_simple_question(self):
        result = _format_ask_user_question_inline({"question": "Continue?"})
        assert "Continue?" in result

    def test_structured_questions(self):
        tool_input = {
            "questions": [
                {
                    "question": "Pick a color",
                    "options": [{"label": "Red"}, {"label": "Blue"}],
                }
            ],
            "answers": {"Pick a color": "Red"},
        }
        result = _format_ask_user_question_inline(tool_input)
        assert "Pick a color" in result
        assert "Red" in result
        assert "Blue" in result

    def test_structured_no_selection(self):
        tool_input = {
            "questions": [{"question": "Pick one", "options": [{"label": "A"}]}],
            "answers": {},
        }
        result = _format_ask_user_question_inline(tool_input)
        assert "Pick one" in result

    def test_empty_tool_input(self):
        assert _format_ask_user_question_inline({}) == ""

    def test_fallback_keys(self):
        result = _format_ask_user_question_inline({"foo": "bar", "baz": "qux"})
        assert "foo" in result or "baz" in result

    def test_long_question_truncated(self):
        long_q = "x" * 500
        result = _format_ask_user_question_inline({"question": long_q})
        assert len(result) <= 210  # 200 chars + quotes and space


class TestFormatAskUserQuestionDetail:
    def test_structured_with_answers(self):
        data = {
            "tool_input": {
                "questions": [
                    {
                        "question": "Pick one",
                        "options": [
                            {"label": "A", "description": "Option A"},
                            {"label": "B", "description": "Option B"},
                        ],
                    }
                ],
                "answers": {"Pick one": "A"},
            },
            "_decision": "allowed",
        }
        result = _format_ask_user_question_detail(data)
        assert "Pick one" in result
        assert "A" in result
        assert "Option A" in result

    def test_timeout_decision_cyan(self):
        data = {
            "tool_input": {
                "questions": [
                    {
                        "question": "Confirm?",
                        "options": [{"label": "Yes"}, {"label": "No"}],
                    }
                ],
            },
            "_answers": {"Confirm?": "Yes"},
            "_decision": "timeout",
        }
        result = _format_ask_user_question_detail(data)
        assert "cyan" in result
        assert "auto" in result

    def test_manual_answer_green(self):
        data = {
            "tool_input": {
                "questions": [
                    {
                        "question": "Q?",
                        "options": [{"label": "Y"}],
                    }
                ],
            },
            "_answers": {"Q?": "Y"},
            "_decision": "allowed",
        }
        result = _format_ask_user_question_detail(data)
        assert "green" in result
        assert "manual" in result

    def test_simple_question(self):
        data = {
            "tool_input": {"question": "Hello?"},
            "_decision": "allowed",
        }
        result = _format_ask_user_question_detail(data)
        assert "Hello?" in result

    def test_empty_tool_input_with_keys(self):
        data = {
            "tool_input": {"custom_key": "custom_value"},
            "_decision": "allowed",
        }
        result = _format_ask_user_question_detail(data)
        assert "custom_key" in result

    def test_multiple_questions(self):
        data = {
            "tool_input": {
                "questions": [
                    {"question": "Q1", "options": [{"label": "A"}]},
                    {"question": "Q2", "options": [{"label": "B"}]},
                ],
            },
            "_answers": {"Q1": "A"},
            "_decision": "allowed",
        }
        result = _format_ask_user_question_detail(data)
        assert "Q1" in result
        assert "Q2" in result

    def test_unselected_option(self):
        data = {
            "tool_input": {
                "questions": [
                    {
                        "question": "Pick",
                        "options": [
                            {"label": "Selected"},
                            {"label": "NotSelected"},
                        ],
                    }
                ],
            },
            "_answers": {"Pick": "Selected"},
            "_decision": "allowed",
        }
        result = _format_ask_user_question_detail(data)
        assert "Selected" in result
        assert "NotSelected" in result


# ---------------------------------------------------------------------------
# format_event — all event types
# ---------------------------------------------------------------------------

def _mock_panel(session_id="test-sess"):
    panel = MagicMock()
    panel.session_id = session_id
    panel.active_agents = {}
    panel._pending_timeout = None
    return panel


def _no_pause(sid):
    return False


def _always_pause(sid):
    return True


class TestFormatEvent:
    """Test format_event with all event types and edge cases."""

    def test_permission_allowed(self):
        data = {"tool_name": "Bash", "tool_input": {"command": "ls"}, "session_id": "s1", "_decision": "allowed"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "ALLOWED" in label
        assert "Bash" in detail
        assert "ls" in detail

    def test_permission_deferred(self):
        data = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/x"}, "session_id": "s1", "_decision": "deferred"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "DEFERRED" in label

    def test_permission_timeout(self):
        data = {"tool_name": "AskUserQuestion", "tool_input": {"question": "Go?"}, "session_id": "s1",
                "_decision": "timeout", "_ask_timeout": 30}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "TIMEOUT" in label
        assert "30" in detail

    def test_permission_excluded_tool(self):
        data = {"tool_name": "Bash", "tool_input": {}, "session_id": "s1", "_excluded_tool": True}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "MANUAL" in label

    def test_permission_paused(self):
        data = {"tool_name": "Bash", "tool_input": {}, "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_always_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "PAUSED" in label

    def test_permission_edit_tool(self):
        data = {"tool_name": "Edit", "tool_input": {"file_path": "/tmp/test.py"}, "session_id": "s1", "_decision": "allowed"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "/tmp/test.py" in detail

    def test_permission_write_tool(self):
        data = {"tool_name": "Write", "tool_input": {"file_path": "/tmp/new.txt"}, "session_id": "s1", "_decision": "allowed"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "/tmp/new.txt" in detail

    def test_permission_webfetch_tool(self):
        data = {"tool_name": "WebFetch", "tool_input": {"url": "https://example.com"}, "session_id": "s1", "_decision": "allowed"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "https://example.com" in detail

    def test_permission_ask_user_question(self):
        data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"question": "Continue?"},
            "session_id": "s1",
            "_decision": "allowed",
        }
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "Continue?" in detail

    def test_permission_with_agents(self):
        data = {"tool_name": "Bash", "tool_input": {}, "session_id": "s1", "_decision": "allowed"}
        panel = _mock_panel("s1")
        panel.active_agents = {"a1": "gp", "a2": "gp"}
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "ag2" in detail

    def test_post_tool_use_with_answer(self):
        data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"answers": {"Q": "Yes"}},
            "session_id": "s1",
        }
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PostToolUse",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "ANSWER" in label
        assert "Yes" in detail

    def test_post_tool_use_no_answer(self):
        data = {
            "tool_name": "AskUserQuestion",
            "tool_input": {"answers": {}},
            "session_id": "s1",
        }
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PostToolUse",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert label is None

    def test_post_tool_use_non_ask(self):
        data = {"tool_name": "Bash", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "PostToolUse",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert label is None

    def test_notification_idle(self):
        data = {"notification_type": "idle_prompt", "message": "Session is idle", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "IDLE" in label

    def test_notification_ask_timeout_complete_auto(self):
        data = {"notification_type": "ask_timeout_complete", "message": "Auto", "session_id": "s1", "_auto_accepted": True}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "AUTO" in label

    def test_notification_ask_timeout_complete_no_auto(self):
        data = {"notification_type": "ask_timeout_complete", "message": "...", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert label is None

    def test_notification_permission_prompt_not_paused(self):
        data = {"notification_type": "permission_prompt", "message": "Approved", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "APPROVED" in label

    def test_notification_permission_prompt_paused(self):
        data = {"notification_type": "permission_prompt", "message": "...", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_always_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "NOTIFY" in label

    def test_notification_permission_prompt_self_sid(self):
        data = {"notification_type": "permission_prompt", "message": "...", "session_id": "self-sid"}
        panel = _mock_panel("self-sid")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
            self_sid="self-sid",
        )
        assert label is None

    def test_notification_permission_prompt_pending_timeout(self):
        data = {"notification_type": "permission_prompt", "message": "...", "session_id": "s1"}
        panel = _mock_panel("s1")
        panel._pending_timeout = time.time() + 100  # still pending
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert label is None

    def test_notification_generic(self):
        data = {"notification_type": "something_else", "message": "Info msg", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "Notification",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "NOTIFY" in label

    def test_subagent_start(self):
        data = {"agent_id": "agent-12345678-abcd", "agent_type": "general_purpose", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "SubagentStart",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "AGENT+" in label
        assert "general_purpose" in detail
        assert "agent-12" in detail

    def test_subagent_stop(self):
        data = {"agent_id": "agent-12345678-abcd", "agent_type": "general_purpose", "session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "SubagentStop",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert "AGENT-" in label

    def test_unknown_event(self):
        data = {"session_id": "s1"}
        panel = _mock_panel("s1")
        label, detail = format_event(
            data, "UnknownEvent",
            is_pane_paused=_no_pause, get_panel=lambda d: panel, oneline=_oneline,
        )
        assert label is None

    def test_no_panel(self):
        data = {"tool_name": "Bash", "tool_input": {}, "session_id": "s1", "_decision": "allowed"}
        label, detail = format_event(
            data, "PermissionRequest",
            is_pane_paused=_no_pause, get_panel=lambda d: None, oneline=_oneline,
        )
        assert "ALLOWED" in label
