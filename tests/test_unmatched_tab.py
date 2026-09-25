"""Tests for the new Unmatched tab mount behavior.

Regression tests for the new three-level fallback mount strategy:
1. Try mounting into the Unmatched container.
2. If that fails, try the Unmatched TabPane itself.
3. If both fail, drop the panel to prevent phantom state.
"""

from unittest.mock import MagicMock

import pytest
from textual.widgets import TabbedContent, TabPane

from claude_monitor.tui import AutoAcceptTUI, LayoutChanged
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
        data = _mk_event(claude_sid="c-1", iterm_sid="w0t0p0:iterm-unknown-1")

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
        assert app._fallback_origin_iterm_sids["c-1"] == "iterm-unknown-1"

    def test_fallback_origin_learned_from_later_event(self, app):
        app.query_one = MagicMock(return_value=MagicMock())
        app._resolve_panel(_mk_event(claude_sid="c-late", iterm_sid=""))
        assert "c-late" not in app._fallback_origin_iterm_sids

        app._resolve_panel(_mk_event(claude_sid="c-late", iterm_sid="w0t0p0:iterm-late"))

        assert app._fallback_origin_iterm_sids["c-late"] == "iterm-late"

    def test_later_real_pane_replaces_cached_fallback(self, app):
        app.query_one = MagicMock(return_value=MagicMock())
        fallback = app._resolve_panel(_mk_event(claude_sid="c-late", iterm_sid=""))
        real_panel = SessionPanel("iterm-live", "real pane")
        app.panels["iterm-live"] = real_panel

        result = app._resolve_panel(_mk_event(claude_sid="c-late", iterm_sid="iterm-live"))

        assert result is real_panel
        assert result is not fallback
        assert "c-late" not in app.panels
        assert app._iterm_to_panel["c-late"] == "iterm-live"
        assert "c-late" not in app._fallback_origin_iterm_sids

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
        assert app._fallback_origin_iterm_sids["c-2"] == "iterm-unknown-2"

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
        assert "c-3" not in app._fallback_origin_iterm_sids

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

    async def test_layout_rebuild_discards_fallback_origin(self, app, monkeypatch):
        monkeypatch.setattr(app, "watch_events", lambda: None)
        monkeypatch.setattr(app, "watch_layout", lambda: None)
        monkeypatch.setattr(app, "poll_usage", lambda: None)
        monkeypatch.setattr(app, "serve_api", lambda: None)
        monkeypatch.setattr(app, "_start_handoff_polling", lambda: None)

        async with app.run_test(size=(120, 40)) as pilot:
            root = app.query_one("#layout-root")
            await app._mount_tabs(root, [], None)
            app._resolve_panel(_mk_event(claude_sid="c-rebuild", iterm_sid="iterm-rebuild"))
            await pilot.pause()
            assert app._fallback_origin_iterm_sids["c-rebuild"] == "iterm-rebuild"

            await app.on_layout_changed(LayoutChanged([], None))
            await pilot.pause()

            assert "c-rebuild" not in app.panels
            assert "c-rebuild" not in app._fallback_origin_iterm_sids

    async def test_later_real_pane_removes_mounted_fallback_and_badge(self, app, monkeypatch):
        monkeypatch.setattr(app, "watch_events", lambda: None)
        monkeypatch.setattr(app, "watch_layout", lambda: None)
        monkeypatch.setattr(app, "poll_usage", lambda: None)
        monkeypatch.setattr(app, "serve_api", lambda: None)
        monkeypatch.setattr(app, "_start_handoff_polling", lambda: None)

        async with app.run_test(size=(120, 40)) as pilot:
            root = app.query_one("#layout-root")
            await app._mount_tabs(root, [], None)
            tc = app.query_one("#tab-content", TabbedContent)
            tc.active = app.UNMATCHED_TAB_ID
            fallback = app._resolve_panel(_mk_event(claude_sid="c-late", iterm_sid=""))
            await pilot.pause()
            assert fallback is not None and fallback.is_mounted
            fallback.write("[00:00] fallback activity")
            fallback.accept_count = 2
            fallback.active_agents["agent-1"] = "Explore"
            fallback.touch()

            real_panel = SessionPanel("iterm-live", "real pane")
            await tc.add_pane(TabPane("Real", real_panel, id="tab-real"))
            real_panel.write("[00:01] real pane activity")
            real_panel.accept_count = 1
            app.panels["iterm-live"] = real_panel
            app._tab_original_names["real"] = "Real"
            app._tab_session_ids["real"] = {"iterm-live"}

            result = app._resolve_panel(_mk_event(claude_sid="c-late", iterm_sid="iterm-live"))
            app._update_textual_tab_labels()
            await pilot.pause()

            assert result is real_panel
            assert fallback.parent is None
            assert fallback not in app.query(SessionPanel)
            assert "c-late" not in app.panels
            assert str(tc.get_tab(app.UNMATCHED_TAB_ID).label) == "Unmatched [0]"
            assert real_panel._event_log == [
                "[00:00] fallback activity",
                "[00:01] real pane activity",
            ]
            assert real_panel.accept_count == 3
            assert real_panel.active_agents == {"agent-1": "Explore"}
