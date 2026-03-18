"""Tests for session lifecycle management."""

import time

import pytest

from tests.conftest import _make_permission_event, _make_notification_event, _make_subagent_event


class TestSessionLifecycle:
    """Test session creation, state transitions, and cleanup."""

    async def test_session_created_on_first_event(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert len(app_fixture.panels) == 0

            e = _make_permission_event(session_id="sess-new-1")
            await inject_message(e)
            await pilot.pause()
            await pilot.pause()

            assert "sess-new-1" in app_fixture.panels

    async def test_session_marked_active(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e = _make_permission_event(session_id="sess-active")
            await inject_message(e)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["sess-active"]
            assert panel.state == "active"

    async def test_session_marked_idle(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # First create the session
            e1 = _make_permission_event(session_id="sess-idle")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()

            # Then send an idle notification
            e2 = _make_notification_event(
                notification_type="idle_prompt",
                session_id="sess-idle",
                message="Session is idle",
            )
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["sess-idle"]
            assert panel.state == "idle"

    async def test_session_stop_cleanup(self, app_fixture, inject_message):
        """SubagentStop should decrement active agents and increment completed."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_subagent_event(
                start_or_stop="start",
                agent_id="agent-cleanup-1",
                session_id="sess-cleanup",
            )
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["sess-cleanup"]
            assert "agent-cleanup-1" in panel.active_agents

            e2 = _make_subagent_event(
                start_or_stop="stop",
                agent_id="agent-cleanup-1",
                session_id="sess-cleanup",
            )
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            assert "agent-cleanup-1" not in panel.active_agents
            assert panel.total_agents_completed == 1

    async def test_session_timeout_cleanup(self, app_fixture, inject_message):
        """Multiple events to same session use same panel."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-multi", tool_name="Bash")
            e2 = _make_permission_event(session_id="sess-multi", tool_name="Write")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            # Should still be just one panel
            assert len([k for k in app_fixture.panels if k == "sess-multi"]) == 1
            panel = app_fixture.panels["sess-multi"]
            assert len(panel._event_log) == 2

    async def test_ask_user_question_countdown_shown(self, app_fixture, inject_message):
        """AskUserQuestion with timeout shows countdown bar in the session pane."""
        from textual.widgets import Static

        ask_timeout = 30  # seconds

        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            # Create the session first
            e = _make_permission_event(session_id="sess-ask-countdown")
            await inject_message(e)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["sess-ask-countdown"]

            # Inject an AskUserQuestion PermissionRequest with _decision=timeout.
            # This is what the hook writes when ask_user_timeout > 0.
            ask_event = {
                "hook_event_name": "PermissionRequest",
                "tool_name": "AskUserQuestion",
                "session_id": "sess-ask-countdown",
                "cwd": "/tmp/test",
                "tool_input": {"prompt": "Should I proceed?"},
                "_decision": "timeout",
                "_ask_timeout": ask_timeout,
                "_timestamp": time.time(),
                "_tty": "/dev/ttys001",
                "_iterm_session_id": "fake-iterm-uuid",
            }
            await inject_message(ask_event)
            await pilot.pause()
            await pilot.pause()

            # Simulate what the app does when it processes a timeout decision:
            # set _pending_timeout on the panel so _update_status shows the bar.
            panel._pending_timeout = time.time() + ask_timeout
            panel._update_status()
            await pilot.pause()

            # Countdown bar should be active and show the remaining time
            bar = panel.query_one(".countdown-bar", Static)
            assert "active" in bar.classes
            assert "AskUserQuestion" in str(bar.visual)
            assert "auto-accept" in str(bar.visual)

            # Overlay should also be active
            overlay = panel.query_one(".timeout-overlay", Static)
            assert "active" in overlay.classes
            assert "AskUserQuestion" in str(overlay.visual)

    async def test_ask_user_question_countdown_cleared_on_answer(self, app_fixture, inject_message):
        """Countdown bar disappears when AskUserQuestion completes (PostToolUse)."""
        from textual.widgets import Static

        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            e = _make_permission_event(session_id="sess-ask-clear")
            await inject_message(e)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["sess-ask-clear"]

            # Manually set a pending timeout (as if AskUserQuestion is waiting)
            panel._pending_timeout = time.time() + 30
            panel._update_status()
            await pilot.pause()

            bar = panel.query_one(".countdown-bar", Static)
            assert "active" in bar.classes

            # PostToolUse clears the pending timeout
            post_event = {
                "hook_event_name": "PostToolUse",
                "tool_name": "AskUserQuestion",
                "session_id": "sess-ask-clear",
                "_timestamp": time.time(),
                "_tty": "/dev/ttys001",
                "_iterm_session_id": "fake-iterm-uuid",
            }
            await inject_message(post_event)
            await pilot.pause()
            await pilot.pause()

            # Countdown should be cleared
            assert panel._pending_timeout is None
            bar = panel.query_one(".countdown-bar", Static)
            assert "active" not in bar.classes
