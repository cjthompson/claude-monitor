"""Regression tests for the AskUserQuestion keystroke-suppression bug.

The bug: when ``excluded_tools`` contains a tool (e.g. ``AskUserQuestion``), the
hook correctly defers the PermissionRequest. Claude Code then fires a
``permission_prompt`` Notification asking the user to confirm manually. The
TUI's permission_prompt handler used to send ``\\r`` via ``_send_approve``
unconditionally, which selected the first option of the AskUserQuestion menu —
defeating the exclusion.

The fix tracks the most recent deferred PermissionRequest on the panel
(``_pending_deferred_at``); if it's recent (<30s), the follow-up permission_prompt
must NOT trigger a keystroke.
"""

import time
from unittest.mock import MagicMock

import pytest

from claude_monitor.tui import AutoAcceptTUI
from claude_monitor.widgets import SessionPanel


@pytest.fixture
def app_and_panel(monkeypatch):
    """Build an AutoAcceptTUI + SessionPanel pair without mounting widgets.

    Mocks ``_send_approve`` so tests can assert whether the keystroke fired.
    Stubs ``is_pane_paused`` to ``False`` so the permission_prompt branch isn't
    short-circuited by pause state.
    """
    app = AutoAcceptTUI()
    panel = SessionPanel("test-iterm-uuid-1234", "test panel")
    monkeypatch.setattr(app, "_send_approve", MagicMock())
    monkeypatch.setattr(app, "is_pane_paused", lambda sid: False)
    return app, panel


def _permission_prompt_event(iterm_sid="test-iterm-uuid-1234", ts=None):
    return {
        "hook_event_name": "Notification",
        "notification_type": "permission_prompt",
        "_iterm_session_id": iterm_sid,
        "_timestamp": ts or time.time(),
    }


class TestPermissionPromptSuppression:
    def test_recent_deferred_suppresses_keystroke(self, app_and_panel):
        """A permission_prompt arriving shortly after a deferred PermissionRequest
        must not trigger _send_approve — the user must confirm manually."""
        app, panel = app_and_panel
        panel._pending_deferred_at = time.time()  # deferred just now
        before = panel.accept_count

        app._apply_event(panel, _permission_prompt_event(), "Notification")

        assert app._send_approve.call_count == 0
        assert panel.accept_count == before  # no spurious accept
        assert panel._pending_deferred_at is None  # flag consumed

    def test_no_deferred_keystroke_fires_normally(self, app_and_panel):
        """Baseline: permission_prompt with no preceding deferred request DOES
        fire the keystroke. Guards against over-suppression breaking the
        normal auto-approve flow."""
        app, panel = app_and_panel
        # _pending_deferred_at is None by default
        before = panel.accept_count

        app._apply_event(panel, _permission_prompt_event(), "Notification")

        assert app._send_approve.call_count == 1
        assert panel.accept_count == before + 1

    def test_stale_deferred_does_not_suppress(self, app_and_panel):
        """A deferred flag older than the 30s window is treated as stale —
        the keystroke fires normally for new permission_prompts."""
        app, panel = app_and_panel
        panel._pending_deferred_at = time.time() - 60  # 60s ago, beyond window
        before = panel.accept_count

        app._apply_event(panel, _permission_prompt_event(), "Notification")

        assert app._send_approve.call_count == 1
        assert panel.accept_count == before + 1

    def test_pending_timeout_takes_precedence_over_deferred(self, app_and_panel):
        """Existing _pending_timeout suppression must still work (regression
        guard for the original AskUserQuestion timeout countdown feature).
        Both flags set → timeout branch wins, _pending_deferred_at is left
        untouched."""
        app, panel = app_and_panel
        panel._pending_timeout = time.time() + 60  # active timeout
        panel._pending_deferred_at = time.time()  # also recent

        app._apply_event(panel, _permission_prompt_event(), "Notification")

        assert app._send_approve.call_count == 0
        # _pending_deferred_at not consumed because the timeout branch handled it
        assert panel._pending_deferred_at is not None

    def test_replay_does_not_send_keystroke(self, app_and_panel):
        """Event replay (e.g., during layout rebuild) must never re-send
        keystrokes even if no deferred flag suppresses them."""
        app, panel = app_and_panel
        event = _permission_prompt_event()
        event["_replay"] = True

        app._apply_event(panel, event, "Notification")

        assert app._send_approve.call_count == 0


class TestEndToEndScenarios:
    """Each test reproduces a real user-facing scenario by chaining the hook's
    decide_permission output through the TUI's pending-flag setter and then
    verifying that the follow-up permission_prompt notification doesn't fire
    a keystroke.

    These guard against drift in either layer breaking the seam between them
    — the original bug lived in that seam (hook deferred correctly; TUI
    didn't know).
    """

    @staticmethod
    def _simulate_permission_request(panel, decision, timeout=0, ts=None):
        """Mirror the on_hook_event setter logic for a PermissionRequest.

        on_hook_event itself requires a mounted Textual app, so we replicate
        only the panel-state mutations that gate the keystroke decision.
        """
        ts = ts or time.time()
        if decision == "timeout" and timeout:
            panel._pending_timeout = ts + timeout
            panel._timeout_origin = ts
        if decision == "deferred":
            panel._pending_deferred_at = ts

    def test_excluded_tools_scenario(self, app_and_panel):
        """User puts 'AskUserQuestion' in excluded_tools. Hook defers the
        permission request, Claude Code falls back to a manual prompt, and
        the TUI must NOT auto-press Enter (which would select option 1)."""
        from claude_monitor.hook import decide_permission

        app, panel = app_and_panel
        state = {
            "global_paused": False,
            "paused_sessions": [],
            "paused_claude_sessions": [],
            "excluded_tools": ["AskUserQuestion"],
            "ask_user_timeout": 120,  # set, but exclusion takes precedence
            "ask_paused_sessions": [],
        }
        event = {
            "tool_name": "AskUserQuestion",
            "_iterm_session_id": panel.session_id,
            "session_id": "claude-sess-1",
        }

        decision, timeout = decide_permission(state, event)
        assert decision == "deferred"
        assert timeout == 0

        self._simulate_permission_request(panel, decision, timeout)
        app._apply_event(panel, _permission_prompt_event(panel.session_id), "Notification")

        assert app._send_approve.call_count == 0
        assert panel._pending_deferred_at is None  # consumed

    def test_qpause_scenario(self, app_and_panel):
        """User toggles Q-Pause for their pane (adds iterm_sid to
        ask_paused_sessions). Same code path as excluded_tools — hook defers,
        TUI must suppress the keystroke."""
        from claude_monitor.hook import decide_permission

        app, panel = app_and_panel
        state = {
            "global_paused": False,
            "paused_sessions": [],
            "paused_claude_sessions": [],
            "excluded_tools": [],
            "ask_user_timeout": 120,
            "ask_paused_sessions": [panel.session_id],
        }
        event = {
            "tool_name": "AskUserQuestion",
            "_iterm_session_id": panel.session_id,
            "session_id": "claude-sess-1",
        }

        decision, timeout = decide_permission(state, event)
        assert decision == "deferred"

        self._simulate_permission_request(panel, decision, timeout)
        app._apply_event(panel, _permission_prompt_event(panel.session_id), "Notification")

        assert app._send_approve.call_count == 0
        assert panel._pending_deferred_at is None

    def test_timeout_scenario_suppresses_during_window(self, app_and_panel):
        """User has ask_user_timeout=120 with no exclusion. Hook returns
        'timeout' (sleeps then emits 'allow'). During the 120s window, the
        TUI must suppress the keystroke via _pending_timeout — otherwise
        the \\r would race with the hook's auto-allow and select an option
        before the timeout completes."""
        from claude_monitor.hook import decide_permission

        app, panel = app_and_panel
        state = {
            "global_paused": False,
            "paused_sessions": [],
            "paused_claude_sessions": [],
            "excluded_tools": [],
            "ask_user_timeout": 120,
            "ask_paused_sessions": [],
        }
        event = {
            "tool_name": "AskUserQuestion",
            "_iterm_session_id": panel.session_id,
            "session_id": "claude-sess-1",
        }

        decision, timeout = decide_permission(state, event)
        assert decision == "timeout"
        assert timeout == 120

        self._simulate_permission_request(panel, decision, timeout)
        app._apply_event(panel, _permission_prompt_event(panel.session_id), "Notification")

        assert app._send_approve.call_count == 0
        # _pending_timeout branch handles this — _pending_deferred_at remains None
        assert panel._pending_deferred_at is None
        assert panel._pending_timeout is not None  # still active until ask_timeout_complete fires


class TestDeferredFlagSetter:
    """Tests for the on_hook_event setter (line where _pending_deferred_at is
    set after _apply_event returns)."""

    def test_deferred_permission_request_marks_panel(self):
        """A PermissionRequest with _decision='deferred' must set
        _pending_deferred_at so the next permission_prompt is suppressed.

        We exercise the same code path the message handler runs by inlining
        the one-liner — full on_hook_event requires a mounted Textual app."""
        panel = SessionPanel("test-iterm-uuid-5678", "test")
        ts = time.time()
        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "AskUserQuestion",
            "_decision": "deferred",
            "_timestamp": ts,
            "_iterm_session_id": "test-iterm-uuid-5678",
        }

        # Mirror tui.py:on_hook_event setter
        if data.get("_decision") == "deferred":
            panel._pending_deferred_at = data["_timestamp"]

        assert panel._pending_deferred_at == ts

    def test_allowed_permission_request_does_not_mark_panel(self):
        """Only deferred decisions set the flag; allowed/timeout don't."""
        panel = SessionPanel("test-iterm-uuid-5678", "test")
        for decision in ("allowed", "timeout", None):
            panel._pending_deferred_at = None
            data = {"_decision": decision, "_timestamp": time.time()}
            if data.get("_decision") == "deferred":
                panel._pending_deferred_at = data["_timestamp"]
            assert panel._pending_deferred_at is None
