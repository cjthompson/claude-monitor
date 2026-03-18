"""Tests for session lifecycle management."""

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
