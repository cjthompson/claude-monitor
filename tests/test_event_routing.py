"""Tests for event routing in SimpleTUI."""

import pytest

from tests.conftest import _make_permission_event, _make_notification_event, _make_subagent_event


class TestEventRouting:
    """Test event routing to correct panels/tabs."""

    async def test_single_session_creates_tab(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(session_id="sess-aaa-111", cwd="/tmp/myproject")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            # Check that a panel was created for this session
            assert "sess-aaa-111" in app_fixture.panels
            panel = app_fixture.panels["sess-aaa-111"]
            assert panel is not None

    async def test_multiple_sessions_create_tabs(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-111", cwd="/tmp/proj1")
            e2 = _make_permission_event(session_id="sess-222", cwd="/tmp/proj2")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            assert len(app_fixture.panels) == 2
            assert "sess-111" in app_fixture.panels
            assert "sess-222" in app_fixture.panels

    async def test_events_route_to_correct_panel(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-111", tool_name="Bash")
            e2 = _make_permission_event(session_id="sess-222", tool_name="Write")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            panel1 = app_fixture.panels["sess-111"]
            panel2 = app_fixture.panels["sess-222"]
            # Each panel should have received exactly one event
            assert len(panel1._event_log) == 1
            assert len(panel2._event_log) == 1
            assert "Bash" in panel1._event_log[0]
            assert "Write" in panel2._event_log[0]

    async def test_worktree_detection(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(
                session_id="sess-wt-1",
                cwd="/home/user/project/.worktrees/feature-branch/subdir",
            )
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            panel = app_fixture.panels["sess-wt-1"]
            assert "WT:" in panel.border_title
            assert panel.has_class("worktree")

    async def test_dashboard_receives_combined_feed(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-111", tool_name="Bash")
            e2 = _make_permission_event(session_id="sess-222", tool_name="Write")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            dashboard = app_fixture.dashboard
            assert dashboard is not None
            # Dashboard should have entries from both sessions
            assert len(dashboard._event_log) == 2
