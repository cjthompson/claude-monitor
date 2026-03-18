"""Tests for dashboard panel behavior."""

import pytest

from tests.conftest import _make_permission_event, _make_subagent_event


async def _inject_and_process(app, pilot, inject_message, event_data):
    await inject_message(event_data)
    for _ in range(20):
        await pilot.pause()


MIN_DASHBOARD_HEIGHT = 3


class TestDashboard:
    """Test dashboard panel states and controls."""

    async def test_dashboard_exists_on_mount(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert app_fixture.dashboard is not None

    async def test_dashboard_minimize_d(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Dashboard starts at configured height (default 12)
            initial_height = app_fixture._dashboard_height
            assert initial_height > MIN_DASHBOARD_HEIGHT

            await pilot.press("d")
            await pilot.pause()
            assert app_fixture._dashboard_height == MIN_DASHBOARD_HEIGHT

    async def test_dashboard_restore_d(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            initial_height = app_fixture._dashboard_height

            # Minimize
            await pilot.press("d")
            await pilot.pause()
            assert app_fixture._dashboard_height == MIN_DASHBOARD_HEIGHT

            # Restore
            await pilot.press("d")
            await pilot.pause()
            assert app_fixture._dashboard_height == initial_height

    async def test_dashboard_grow_equals(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            h_before = app_fixture._dashboard_height

            await pilot.press("equals_sign")
            await pilot.pause()
            # Height should grow by 1
            assert app_fixture._dashboard_height == h_before + 1

    async def test_dashboard_shrink_minus(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            h_before = app_fixture._dashboard_height

            await pilot.press("minus")
            await pilot.pause()
            assert app_fixture._dashboard_height == h_before - 1

    async def test_dashboard_shrink_floor(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Shrink to minimum
            app_fixture._dashboard_height = MIN_DASHBOARD_HEIGHT
            app_fixture._apply_dashboard_height()

            await pilot.press("minus")
            await pilot.pause()
            # Should not go below minimum
            assert app_fixture._dashboard_height == MIN_DASHBOARD_HEIGHT

    async def test_dashboard_to_tab_D(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not app_fixture._dashboard_in_tab

            await pilot.press("D")
            await pilot.pause()
            assert app_fixture._dashboard_in_tab
            assert app_fixture._dashboard_tab_pane_id is not None

    async def test_dashboard_from_tab_D(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Move to tab
            await pilot.press("D")
            await pilot.pause()
            assert app_fixture._dashboard_in_tab

            # Move back to bottom
            await pilot.press("D")
            await pilot.pause()
            assert not app_fixture._dashboard_in_tab

    async def test_dashboard_stats_update(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e = _make_permission_event(session_id="sess-stats-1")
            await _inject_and_process(app_fixture, pilot, inject_message, e)

            # Dashboard should have tracked the event
            assert len(app_fixture.dashboard._event_log) >= 1

    async def test_dashboard_sparkline_data(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Record some events
            e = _make_permission_event(session_id="sess-spark")
            await _inject_and_process(app_fixture, pilot, inject_message, e)

            # The current bucket should have at least 1 event
            assert app_fixture.dashboard._current_bucket_count >= 1
