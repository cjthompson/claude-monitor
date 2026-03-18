"""Tests for pause mode toggling."""

import json

import pytest

from tests.conftest import _make_permission_event


class TestPauseModes:
    """Test global and per-pane pause modes."""

    async def test_global_pause_toggle_a_key(self, app_fixture):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Initially auto (not paused)
            assert not app_fixture._global_paused

            # Press 'a' to toggle to manual
            await pilot.press("a")
            await pilot.pause()
            assert app_fixture._global_paused

            # Press 'a' again to toggle back to auto
            await pilot.press("a")
            await pilot.pause()
            assert not app_fixture._global_paused

    async def test_per_pane_pause_m_key(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Create a session
            e = _make_permission_event(session_id="sess-m-test")
            await inject_message(e)
            await pilot.pause()
            await pilot.pause()

            # The session should exist
            assert "sess-m-test" in app_fixture.panels
            # Session should not be paused initially
            assert not app_fixture.is_pane_paused("sess-m-test")

    async def test_mixed_mode_display(self, app_fixture, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-1")
            e2 = _make_permission_event(session_id="sess-2")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            # Pause one session manually
            app_fixture._paused_claude_sessions.add("sess-1")
            app_fixture._save_state()
            app_fixture._update_status_bar()
            await pilot.pause()

            # One paused, one not = mixed
            assert app_fixture.is_pane_paused("sess-1")
            assert not app_fixture.is_pane_paused("sess-2")

    async def test_global_auto_from_mixed(self, app_fixture, inject_message):
        """a key from mixed → all AUTO."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e1 = _make_permission_event(session_id="sess-1")
            e2 = _make_permission_event(session_id="sess-2")
            await inject_message(e1)
            await pilot.pause()
            await pilot.pause()
            await inject_message(e2)
            await pilot.pause()
            await pilot.pause()

            # Set mixed state
            app_fixture._paused_claude_sessions.add("sess-1")

            # Press 'a' — from mixed → all auto
            await pilot.press("a")
            await pilot.pause()
            assert not app_fixture._global_paused
            assert len(app_fixture._paused_claude_sessions) == 0

    async def test_global_manual_from_all_auto(self, app_fixture):
        """a key from all auto → all MANUAL."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Ensure clean state
            assert not app_fixture._global_paused
            assert len(app_fixture._paused_claude_sessions) == 0

            # Press 'a' — from all auto → global manual
            await pilot.press("a")
            await pilot.pause()
            assert app_fixture._global_paused

    async def test_pause_state_persisted_to_state_json(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            # Read state.json
            with open(isolated_state["state_file"]) as f:
                state = json.load(f)
            assert state["global_paused"] is True

    async def test_per_pane_pause_persisted(self, app_fixture, isolated_state, inject_message):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            e = _make_permission_event(session_id="sess-persist")
            await inject_message(e)
            await pilot.pause()
            await pilot.pause()

            # Manually pause this session
            app_fixture._paused_claude_sessions.add("sess-persist")
            app_fixture._save_state()

            with open(isolated_state["state_file"]) as f:
                state = json.load(f)
            assert "sess-persist" in state["paused_claude_sessions"]

    async def test_shift_tab_alias(self, app_fixture):
        """shift+tab should be bound to toggle_pause (same as 'a')."""
        bindings = app_fixture.BINDINGS
        shift_tab_binding = [b for b in bindings if b.key == "shift+tab"]
        assert len(shift_tab_binding) == 1
        assert shift_tab_binding[0].action == "toggle_pause"
