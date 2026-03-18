"""Tests for status bar display."""

import pytest

from claude_monitor import __version__


class TestStatusBar:
    """Test status bar content."""

    async def test_status_bar_shows_auto(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Static
            left = app_fixture.query_one("#status-left", Static)
            # Should contain AUTO when not paused
            assert "AUTO" in str(left.visual)

    async def test_status_bar_shows_manual(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("a")
            await pilot.pause()

            from textual.widgets import Static
            left = app_fixture.query_one("#status-left", Static)
            assert "MANUAL" in str(left.visual)

    async def test_status_bar_shows_version(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Static
            right = app_fixture.query_one("#status-right", Static)
            assert __version__ in str(right.visual)

    async def test_status_bar_shows_clock(self, app_fixture, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from textual.widgets import Static
            right = app_fixture.query_one("#status-right", Static)
            # Clock should contain am or pm
            content = str(right.visual)
            assert "am" in content.lower() or "pm" in content.lower()
