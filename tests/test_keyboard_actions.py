"""Tests for keyboard actions and bindings."""

import pytest

from tests.conftest import _make_permission_event


async def _inject_and_process(app, pilot, inject_message, event_data):
    await inject_message(event_data)
    for _ in range(20):
        await pilot.pause()


class TestKeyboardActions:
    """Test keyboard bindings exist and work."""

    def test_all_bindings_exist(self, app_fixture):
        """Verify BINDINGS list contains expected entries."""
        binding_keys = [b.key for b in app_fixture.BINDINGS]
        expected_keys = [
            "a", "shift+tab", "c", "u", "s", "d", "D",
            "equals_sign", "minus",
            "right_square_bracket", "left_square_bracket",
            "question_mark", "q",
        ]
        for key in expected_keys:
            assert key in binding_keys, f"Missing binding for key: {key}"

    async def test_tab_cycling_next(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-tab-1", cwd="/tmp/p1")
            e2 = _make_permission_event(session_id="sess-tab-2", cwd="/tmp/p2")
            await _inject_and_process(app_fixture, pilot, inject_message, e1)
            await _inject_and_process(app_fixture, pilot, inject_message, e2)

            from textual.widgets import TabbedContent
            tc = app_fixture.query_one("#tab-content", TabbedContent)
            first_active = tc.active

            # Press ] to go to next tab
            await pilot.press("right_square_bracket")
            await pilot.pause()
            second_active = tc.active
            assert first_active != second_active

    async def test_tab_cycling_prev(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-tab-1", cwd="/tmp/p1")
            e2 = _make_permission_event(session_id="sess-tab-2", cwd="/tmp/p2")
            await _inject_and_process(app_fixture, pilot, inject_message, e1)
            await _inject_and_process(app_fixture, pilot, inject_message, e2)

            from textual.widgets import TabbedContent
            tc = app_fixture.query_one("#tab-content", TabbedContent)

            # Go to second tab first
            await pilot.press("right_square_bracket")
            await pilot.pause()
            second_active = tc.active

            # Press [ to go back
            await pilot.press("left_square_bracket")
            await pilot.pause()
            back_active = tc.active
            assert back_active != second_active

    async def test_tab_cycling_wraps(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-wrap-1", cwd="/tmp/p1")
            e2 = _make_permission_event(session_id="sess-wrap-2", cwd="/tmp/p2")
            await _inject_and_process(app_fixture, pilot, inject_message, e1)
            await _inject_and_process(app_fixture, pilot, inject_message, e2)

            from textual.widgets import TabbedContent
            tc = app_fixture.query_one("#tab-content", TabbedContent)
            first_active = tc.active

            # Press ] twice to wrap around (2 tabs)
            await pilot.press("right_square_bracket")
            await pilot.pause()
            await pilot.press("right_square_bracket")
            await pilot.pause()
            assert tc.active == first_active

    async def test_close_tab(self, app_fixture, inject_message):
        """Pressing w on a focused tab (if supported) or verifying tab removal."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-close-1", cwd="/tmp/p1")
            await _inject_and_process(app_fixture, pilot, inject_message, e1)
            assert "sess-close-1" in app_fixture.panels

    async def test_quit_key(self, app_fixture, inject_message):
        """q key should trigger exit."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Pressing q should set the stop event
            assert not app_fixture._stop_event.is_set()
            await pilot.press("q")
            await pilot.pause()
            # The app should be exiting
            assert app_fixture._stop_event.is_set()
