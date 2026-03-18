"""Extended tests for tui_simple.py — uncovered branches and edge cases."""

import json
import time

import pytest

from tests.conftest import (
    _make_permission_event,
    _make_notification_event,
    _make_subagent_event,
)


class TestSessionResolution:
    async def test_worktree_session(self, app_fixture, inject_message):
        """Worktree sessions get WT: prefix."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(
                session_id="wt-sess-1",
                cwd="/tmp/project/.worktrees/feature-x/code",
            )
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels.get("wt-sess-1")
            assert panel is not None
            assert "WT:" in panel.border_title

    async def test_worktree_claude_dir(self, app_fixture, inject_message):
        """Worktree sessions via .claude/worktrees/ path."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(
                session_id="wt-sess-2",
                cwd="/tmp/project/.claude/worktrees/bugfix/code",
            )
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels.get("wt-sess-2")
            assert panel is not None
            assert "WT:" in panel.border_title

    async def test_no_cwd_session(self, app_fixture, inject_message):
        """Session with no cwd uses short session ID."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(session_id="nocwd-sess-1234", cwd="")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels.get("nocwd-sess-1234")
            assert panel is not None

    async def test_no_session_id(self, app_fixture, inject_message):
        """Event with empty session_id returns no panel."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(session_id="")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()
            assert len(app_fixture.panels) == 0


class TestApplyEvent:
    async def test_permission_counts(self, app_fixture, inject_message):
        """PermissionRequest increments accept_count when not paused."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(session_id="count-sess")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["count-sess"]
            assert panel.accept_count == 1
            assert panel.tool_counts.get("Bash", 0) == 1

    async def test_permission_paused_no_count(self, app_fixture, inject_message):
        """Paused session doesn't increment counts."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app_fixture._global_paused = True
            event = _make_permission_event(session_id="paused-sess")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["paused-sess"]
            assert panel.accept_count == 0

    async def test_idle_notification_marks_idle(self, app_fixture, inject_message):
        """idle_prompt notification marks panel as idle."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # First create the session
            event1 = _make_permission_event(session_id="idle-sess")
            await inject_message(event1)
            await pilot.pause()
            await pilot.pause()

            # Then send idle notification
            event2 = _make_notification_event(
                notification_type="idle_prompt",
                session_id="idle-sess",
                message="Session is idle",
            )
            await inject_message(event2)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["idle-sess"]
            assert panel._state == "idle"

    async def test_subagent_start_stop(self, app_fixture, inject_message):
        """SubagentStart/Stop updates agent tracking."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event1 = _make_permission_event(session_id="agent-sess")
            await inject_message(event1)
            await pilot.pause()
            await pilot.pause()

            start = _make_subagent_event(
                start_or_stop="start", agent_id="ag1",
                session_id="agent-sess", agent_type="general_purpose",
            )
            await inject_message(start)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["agent-sess"]
            assert "ag1" in panel.active_agents

            stop = _make_subagent_event(
                start_or_stop="stop", agent_id="ag1",
                session_id="agent-sess",
            )
            await inject_message(stop)
            await pilot.pause()
            await pilot.pause()

            assert "ag1" not in panel.active_agents
            assert panel.total_agents_completed == 1

    async def test_post_tool_use_clears_timeout(self, app_fixture, inject_message):
        """PostToolUse for AskUserQuestion clears pending timeout."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event1 = _make_permission_event(session_id="timeout-sess")
            await inject_message(event1)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["timeout-sess"]
            panel._pending_timeout = time.time() + 60
            panel._timeout_origin = 12345.0

            post = {
                "hook_event_name": "PostToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {"answers": {"Q": "A"}},
                "session_id": "timeout-sess",
                "_timestamp": time.time(),
                "_iterm_session_id": "fake",
            }
            await inject_message(post)
            await pilot.pause()
            await pilot.pause()

            assert panel._pending_timeout is None

    async def test_notification_permission_prompt_counts(self, app_fixture, inject_message):
        """permission_prompt notification increments accept_count when not paused."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event1 = _make_permission_event(session_id="perm-sess")
            await inject_message(event1)
            await pilot.pause()
            await pilot.pause()

            initial_count = app_fixture.panels["perm-sess"].accept_count

            notif = _make_notification_event(
                notification_type="permission_prompt",
                session_id="perm-sess",
                message="Permission granted",
            )
            await inject_message(notif)
            await pilot.pause()
            await pilot.pause()

            assert app_fixture.panels["perm-sess"].accept_count == initial_count + 1

    async def test_ask_timeout_complete_auto_accepted(self, app_fixture, inject_message):
        """ask_timeout_complete with matching origin sets _auto_accepted."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event1 = _make_permission_event(session_id="ato-sess")
            await inject_message(event1)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["ato-sess"]
            panel._pending_timeout = time.time() + 60
            panel._timeout_origin = 12345.0

            notif = {
                "hook_event_name": "Notification",
                "notification_type": "ask_timeout_complete",
                "message": "Auto-accepted",
                "session_id": "ato-sess",
                "_timestamp": time.time(),
                "_iterm_session_id": "fake",
                "_timeout_origin": 12345.0,
            }
            await inject_message(notif)
            await pilot.pause()
            await pilot.pause()

            assert panel._pending_timeout is None


class TestTogglePause:
    async def test_toggle_pause_on_off(self, app_fixture):
        """Toggle pause: auto -> manual -> auto."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not app_fixture._global_paused
            app_fixture.action_toggle_pause()
            assert app_fixture._global_paused
            app_fixture.action_toggle_pause()
            assert not app_fixture._global_paused

    async def test_toggle_pause_from_mixed(self, app_fixture):
        """Mixed state (some paused) -> toggle -> all auto."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app_fixture._paused_claude_sessions = {"s1"}
            app_fixture.action_toggle_pause()
            assert not app_fixture._global_paused
            assert len(app_fixture._paused_claude_sessions) == 0


class TestAskPauseToggle:
    async def test_toggle_ask_pause(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from claude_monitor.widgets.session_panel import SessionPanel
            event = _make_permission_event(session_id="ask-sess")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            assert not app_fixture.is_ask_paused("ask-sess")
            app_fixture.on_session_panel_ask_pause_toggle(
                SessionPanel.AskPauseToggle("ask-sess")
            )
            assert app_fixture.is_ask_paused("ask-sess")
            app_fixture.on_session_panel_ask_pause_toggle(
                SessionPanel.AskPauseToggle("ask-sess")
            )
            assert not app_fixture.is_ask_paused("ask-sess")


class TestDashboardActions:
    async def test_toggle_dashboard_minimize_restore(self, app_fixture, isolated_state):
        """Toggle dashboard minimizes and restores."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            original_height = app_fixture._dashboard_height
            app_fixture.action_toggle_dashboard()
            # Should minimize to MIN_DASHBOARD_HEIGHT
            from claude_monitor.tui_simple import MIN_DASHBOARD_HEIGHT
            assert app_fixture._dashboard_height == MIN_DASHBOARD_HEIGHT
            assert app_fixture._stored_dashboard_height == original_height

            # Toggle back restores
            app_fixture.action_toggle_dashboard()
            assert app_fixture._dashboard_height == original_height
            assert app_fixture._stored_dashboard_height is None

    async def test_shrink_dashboard(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            original = app_fixture._dashboard_height
            app_fixture.action_shrink_dashboard()
            assert app_fixture._dashboard_height == original - 1

    async def test_shrink_at_minimum(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from claude_monitor.tui_simple import MIN_DASHBOARD_HEIGHT
            app_fixture._dashboard_height = MIN_DASHBOARD_HEIGHT
            app_fixture.action_shrink_dashboard()
            assert app_fixture._dashboard_height == MIN_DASHBOARD_HEIGHT

    async def test_idle_close_mode(self, app_fixture, inject_message, isolated_state):
        """tab_close_mode=immediate removes session on idle_prompt."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app_fixture.settings.tab_close_mode = "immediate"
            event = _make_permission_event(session_id="close-sess")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            assert "close-sess" in app_fixture.panels

            idle = _make_notification_event(
                notification_type="idle_prompt",
                session_id="close-sess",
            )
            await inject_message(idle)
            await pilot.pause()
            await pilot.pause()
            await pilot.pause()


class TestUpdateTabLabel:
    async def test_active_label(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(session_id="tab-sess")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["tab-sess"]
            assert panel._state == "active"
            # Just verify it doesn't crash
            app_fixture._update_tab_label("tab-sess")

    async def test_unknown_session(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Should not raise
            app_fixture._update_tab_label("nonexistent")
