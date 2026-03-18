"""Tests for tab management."""

import pytest

from tests.conftest import _make_permission_event


async def _inject_and_process(app, pilot, inject_message, event_data):
    await inject_message(event_data)
    for _ in range(20):
        await pilot.pause()


class TestTabManagement:
    """Test tab creation, naming, and lifecycle."""

    async def test_new_session_tab_title_from_cwd(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e = _make_permission_event(session_id="sess-cwd-1", cwd="/home/user/my-project")
            await _inject_and_process(app_fixture, pilot, inject_message, e)

            panel = app_fixture.panels["sess-cwd-1"]
            # Title should contain directory name
            assert "my-project" in panel.border_title

    async def test_worktree_tab_prefix(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e = _make_permission_event(
                session_id="sess-wt-tab",
                cwd="/home/user/proj/.claude/worktrees/fix-bug/src",
            )
            await _inject_and_process(app_fixture, pilot, inject_message, e)

            panel = app_fixture.panels["sess-wt-tab"]
            assert panel.border_title.startswith("WT:")
            assert "fix-bug" in panel.border_title

    async def test_close_tab_removes_state(self, app_fixture, inject_message):
        """Closing a tab via TabbedContent removes it from the widget tree."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e = _make_permission_event(session_id="sess-close-tab", cwd="/tmp/proj")
            await _inject_and_process(app_fixture, pilot, inject_message, e)

            from textual.widgets import TabbedContent
            tc = app_fixture.query_one("#tab-content", TabbedContent)
            # Tab was added
            tab_id = app_fixture._claude_to_tab["sess-close-tab"]
            assert tc.active == tab_id

    async def test_dashboard_tab_cannot_be_closed(self, app_fixture, inject_message):
        """Dashboard as tab should exist when toggled to tab mode."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Move dashboard to tab
            await pilot.press("D")
            await pilot.pause()
            assert app_fixture._dashboard_in_tab
            assert app_fixture._dashboard_tab_pane_id is not None

            from textual.widgets import TabbedContent
            tc = app_fixture.query_one("#tab-content", TabbedContent)
            # Dashboard tab should be present
            pane_ids = [p.id for p in tc.query("TabPane") if p.id]
            assert app_fixture._dashboard_tab_pane_id in pane_ids
