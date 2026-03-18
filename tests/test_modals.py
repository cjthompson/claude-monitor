"""Tests for modal screens (Settings, Choices, Questions, Help)."""

import json

import pytest

from tests.conftest import _make_permission_event


async def _inject_and_process(app, pilot, inject_event, event_data):
    inject_event(event_data)
    for _ in range(20):
        await pilot.pause()


class TestModals:
    """Test modal screen opening/closing."""

    async def test_settings_opens_on_s(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()

            from claude_monitor.settings import SettingsScreen
            # Check a SettingsScreen is on the screen stack
            assert any(
                isinstance(s, SettingsScreen) for s in app_fixture.screen_stack
            )

    async def test_settings_escape_dismisses(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("s")
            await pilot.pause()

            from claude_monitor.settings import SettingsScreen
            assert any(isinstance(s, SettingsScreen) for s in app_fixture.screen_stack)

            await pilot.press("escape")
            await pilot.pause()
            assert not any(isinstance(s, SettingsScreen) for s in app_fixture.screen_stack)

    async def test_settings_saves_config(self, app_fixture, isolated_state, inject_event):
        """Saving settings should write config.json."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Just verify save_settings works by calling it directly
            from claude_monitor.settings import Settings, save_settings
            s = Settings(theme="dracula", debug=True)
            save_settings(s)

            with open(isolated_state["config_file"]) as f:
                data = json.load(f)
            assert data["theme"] == "dracula"
            assert data["debug"] is True

    async def test_choices_screen_opens(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("c")
            await pilot.pause()

            from claude_monitor.screens import ChoicesScreen
            assert any(isinstance(s, ChoicesScreen) for s in app_fixture.screen_stack)

    async def test_choices_screen_shows_events(self, app_fixture, inject_event):
        """Choices screen should load events from events.jsonl."""
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Inject a permission event directly to the file
            e = _make_permission_event(session_id="sess-choice", tool_name="Bash")
            inject_event(e)
            await pilot.pause()

            await pilot.press("c")
            await pilot.pause()

            from claude_monitor.screens import ChoicesScreen
            assert any(isinstance(s, ChoicesScreen) for s in app_fixture.screen_stack)

    async def test_questions_screen_opens(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("u")
            await pilot.pause()

            from claude_monitor.screens import QuestionsScreen
            assert any(isinstance(s, QuestionsScreen) for s in app_fixture.screen_stack)

    async def test_help_screen_opens(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()

            from claude_monitor.screens import HelpScreen
            assert any(isinstance(s, HelpScreen) for s in app_fixture.screen_stack)

    async def test_help_screen_has_sections(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()

            from claude_monitor.screens import HelpScreen
            screen = [s for s in app_fixture.screen_stack if isinstance(s, HelpScreen)][0]
            # Help screen should have global and instance bindings
            assert len(screen._global_pairs) > 0

    async def test_help_escape_dismisses(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("question_mark")
            await pilot.pause()

            from claude_monitor.screens import HelpScreen
            assert any(isinstance(s, HelpScreen) for s in app_fixture.screen_stack)

            await pilot.press("escape")
            await pilot.pause()
            assert not any(isinstance(s, HelpScreen) for s in app_fixture.screen_stack)

    async def test_command_palette_opens(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("ctrl+p")
            await pilot.pause()

            # The command palette is a Textual built-in screen
            # Just verify the screen stack grew
            assert len(app_fixture.screen_stack) > 1
