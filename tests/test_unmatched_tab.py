"""Tests for the new Unmatched tab mount behavior.

Regression tests for the new three-level fallback mount strategy:
1. Try mounting into the Unmatched container.
2. If that fails, try the Unmatched TabPane itself.
3. If both fail, drop the panel to prevent phantom state.
"""

from unittest.mock import MagicMock

import pytest
from textual.widgets import TabbedContent

from claude_monitor.tui import AutoAcceptTUI
from claude_monitor.widgets import SessionPanel


@pytest.fixture
def app(monkeypatch):
    """Bare AutoAcceptTUI with iTerm2 and background threads disabled.

    _layout_tabs is empty so on_mount skips all iTerm2 initialisation.
    We call _resolve_panel directly — no run_test() lifecycle needed.
    """
    import claude_monitor.tui as tui_mod

    monkeypatch.setattr(tui_mod, "_layout_tabs", [])
    monkeypatch.setattr(tui_mod, "_self_session_id", None)
    monkeypatch.setattr("claude_monitor.app_base.fetch_usage", lambda: None)
    return AutoAcceptTUI()


def _mk_event(claude_sid="claude-abc", iterm_sid="iterm-xyz", cwd="/tmp/proj"):
    """Build a minimal hook event dict."""
    return {
        "session_id": claude_sid,
        "cwd": cwd,
        "_iterm_session_id": iterm_sid,
        "hook_event_name": "SessionStart",
    }


class TestFallbackPanelUnmatchedMounting:
    """Test the new mount strategy for fallback panels."""

    def test_fallback_panel_mounts_into_unmatched_container(self, app):
        """Level 1: panel mounts into the Unmatched container successfully."""
        data = _mk_event(claude_sid="c-1", iterm_sid="iterm-unknown-1")

        mock_container = MagicMock()

        def _query_one(selector, *args, **kwargs):
            if selector == f"#{app.UNMATCHED_CONTAINER_ID}":
                return mock_container
            raise Exception(f"Selector {selector} not found")

        app.query_one = MagicMock(side_effect=_query_one)
        result = app._resolve_panel(data)

        assert result is not None
        assert result.session_id == "c-1"
        mock_container.mount.assert_called_once_with(result)
        assert "c-1" in app.panels

    def test_fallback_panel_never_mounts_into_layout_root(self, app):
        """Level 2 fallback: when container fails, tries TabPane instead of #layout-root."""
        data = _mk_event(claude_sid="c-2", iterm_sid="iterm-unknown-2")

        mock_pane = MagicMock()
        mock_tc = MagicMock()
        mock_tc.get_pane.return_value = mock_pane

        def _query_one(selector, *args, **kwargs):
            if selector == f"#{app.UNMATCHED_CONTAINER_ID}":
                raise Exception("Container not mounted")
            if selector == "#tab-content" and args and args[0] == TabbedContent:
                return mock_tc
            raise Exception(f"Selector {selector} not found")

        app.query_one = MagicMock(side_effect=_query_one)
        result = app._resolve_panel(data)

        assert result is not None
        assert result.session_id == "c-2"
        mock_tc.get_pane.assert_called_once_with(app.UNMATCHED_TAB_ID)
        mock_pane.mount.assert_called_once_with(result)
        assert "c-2" in app.panels

    def test_fallback_panel_dropped_when_container_missing(self, app):
        """Level 3: when both container and TabPane fail, panel is dropped."""
        data = _mk_event(claude_sid="c-3", iterm_sid="iterm-unknown-3")

        def _query_one(selector, *args, **kwargs):
            raise Exception(f"Selector {selector} not found")

        app.query_one = MagicMock(side_effect=_query_one)
        result = app._resolve_panel(data)

        # Panel creation failed at mount time
        assert result is None
        # Panel was added to tracking, then removed when mount failed
        assert "c-3" not in app.panels
        assert "c-3" not in app._iterm_to_panel

    async def test_thirty_panels_keep_min_height(self, app, monkeypatch):
        """Thirty mounted panels retain their height and overflow the tab."""
        from textual.containers import VerticalScroll

        monkeypatch.setattr(app, "watch_events", lambda: None)
        monkeypatch.setattr(app, "watch_layout", lambda: None)
        monkeypatch.setattr(app, "poll_usage", lambda: None)
        monkeypatch.setattr(app, "serve_api", lambda: None)
        monkeypatch.setattr(app, "_start_handoff_polling", lambda: None)

        async with app.run_test(size=(120, 40)) as pilot:
            root = app.query_one("#layout-root")
            await app._mount_tabs(root, [], None)

            tab_content = app.query_one("#tab-content", TabbedContent)
            tab_content.active = app.UNMATCHED_TAB_ID
            container = app.query_one(f"#{app.UNMATCHED_CONTAINER_ID}", VerticalScroll)
            panels = [SessionPanel(f"claude-{i:02d}", f"session [{i:02d}]") for i in range(30)]
            await container.mount(*panels)
            await pilot.pause()

            assert all(panel.outer_size.height >= 12 for panel in panels)
            assert container.virtual_size.height > container.content_region.height
