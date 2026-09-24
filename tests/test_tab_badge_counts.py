"""Tests for tab badge counting in _update_textual_tab_labels.

Regression tests for #012: the Unmatched badge over-counted panels whose
iTerm pane was legitimately hidden by iterm_hide_empty_tabs, excluded by
iterm_scope, or removed in the last layout rebuild — because those panels
just aren't present in self._tab_session_ids, which only tracks tabs
surviving both filters. Also covers a pre-existing per_tab_width off-by-one
(Hand-off pane was never counted in the tab-count divisor).
"""

from unittest.mock import MagicMock

import pytest
from textual.geometry import Size

from claude_monitor.tui import AutoAcceptTUI, ScopeSidsUpdated
from claude_monitor.widgets import SessionPanel


@pytest.fixture
def app(monkeypatch):
    """Bare AutoAcceptTUI, no run_test() lifecycle — same pattern as
    test_phantom_panel_prevention.py / test_unmatched_tab.py."""
    import claude_monitor.tui as tui_mod

    monkeypatch.setattr(tui_mod, "_layout_tabs", [])
    monkeypatch.setattr(tui_mod, "_self_session_id", None)
    monkeypatch.setattr("claude_monitor.app_base.fetch_usage", lambda: None)
    return AutoAcceptTUI()


def _mock_tabbed_content():
    """Mock TabbedContent whose get_tab(css_id) returns a stable per-id
    MagicMock so tests can inspect the .label written to each tab."""
    tabs: dict[str, MagicMock] = {}

    def get_tab(css_id):
        return tabs.setdefault(css_id, MagicMock())

    mock_tc = MagicMock()
    mock_tc.get_tab.side_effect = get_tab
    return mock_tc, tabs


def _wire_query_one(app, mock_tc):
    def query_one(selector, *a, **kw):
        if selector == "#tab-content":
            return mock_tc
        raise Exception(f"unexpected selector {selector}")

    app.query_one = query_one


class TestUnmatchedBadgeCounting:
    def test_hidden_empty_tab_pane_not_counted_as_unmatched(self, app):
        """The reported bug: a real pane hidden by iterm_hide_empty_tabs
        must not inflate the Unmatched badge."""
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)

        app._tab_original_names = {"tab-real": "proj"}
        app._tab_session_ids = {"tab-real": {"sid-real"}}
        app.panels = {"claude-hidden": SessionPanel("claude-hidden", "hidden-panel")}
        app._fallback_origin_iterm_sids = {"claude-hidden": "sid-hidden"}
        app._hidden_tab_iterm_sids = {"sid-hidden"}

        app._update_textual_tab_labels()

        assert tabs["tab-unmatched"].label == "Unmatched [0]"

    def test_out_of_scope_pane_not_counted_as_unmatched(self, app):
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)

        app._tab_original_names = {"tab-real": "proj"}
        app._tab_session_ids = {"tab-real": {"sid-real"}}
        app.panels = {"claude-oos": SessionPanel("claude-oos", "oos-panel")}
        app._fallback_origin_iterm_sids = {"claude-oos": "sid-oos"}
        app._out_of_scope_iterm_sids = {"sid-oos"}

        app._update_textual_tab_labels()

        assert tabs["tab-unmatched"].label == "Unmatched [0]"

    def test_removed_pane_not_counted_as_unmatched(self, app):
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)

        app._tab_original_names = {"tab-real": "proj"}
        app._tab_session_ids = {"tab-real": {"sid-real"}}
        app.panels = {"claude-removed": SessionPanel("claude-removed", "removed-panel")}
        app._fallback_origin_iterm_sids = {"claude-removed": "sid-removed"}
        app._removed_iterm_sids = {"sid-removed"}

        app._update_textual_tab_labels()

        assert tabs["tab-unmatched"].label == "Unmatched [0]"

    def test_genuinely_unmatched_fallback_panel_is_counted(self, app):
        """A real fallback panel (Claude-sid-keyed, not in any of the three
        suppression sets) must still be counted — the fix must not
        over-suppress."""
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)

        app._tab_original_names = {"tab-real": "proj"}
        app._tab_session_ids = {"tab-real": {"sid-real"}}
        app.panels = {"claude-unmatched-1": SessionPanel("claude-unmatched-1", "fallback")}
        app._fallback_origin_iterm_sids = {"claude-unmatched-1": "sid-unrecognized"}

        app._update_textual_tab_labels()

        assert tabs["tab-unmatched"].label == "Unmatched [1]"

    def test_scope_update_refreshes_badge_for_existing_fallback(self, app):
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)
        app._tab_original_names = {"tab-real": "proj"}
        app._tab_session_ids = {"tab-real": set()}
        app.panels = {"claude-oos": SessionPanel("claude-oos", "fallback")}
        app._iterm_to_panel = {"claude-oos": "claude-oos"}
        app._fallback_origin_iterm_sids = {"claude-oos": "sid-oos"}

        app._update_textual_tab_labels()
        assert tabs["tab-unmatched"].label == "Unmatched [1]"

        app.on_scope_sids_updated(ScopeSidsUpdated(all_iterm_sids={"sid-oos"}, scoped_iterm_sids=set()))

        assert tabs["tab-unmatched"].label == "Unmatched [0]"
        assert "claude-oos" not in app.panels
        assert "claude-oos" not in app._iterm_to_panel
        assert "claude-oos" not in app._fallback_origin_iterm_sids

    def test_hidden_fallback_is_removed_and_layout_refreshed(self, app):
        app.query_one = MagicMock(return_value=MagicMock())
        app._do_refresh = MagicMock()
        app._resolve_panel(
            {"session_id": "claude-hidden", "cwd": "/tmp/proj", "_iterm_session_id": "sid-hidden"}
        )

        app.on_scope_sids_updated(
            ScopeSidsUpdated(all_iterm_sids={"sid-hidden"}, scoped_iterm_sids={"sid-hidden"})
        )

        assert "claude-hidden" not in app.panels
        assert "claude-hidden" not in app._iterm_to_panel
        assert "claude-hidden" not in app._fallback_origin_iterm_sids
        assert "sid-hidden" not in app._hidden_tab_iterm_sids
        app._do_refresh.assert_called_once()

    def test_unmatched_badge_updates_without_real_tabs(self, app):
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)
        app.panels = {"claude-unmatched": SessionPanel("claude-unmatched", "fallback")}

        app._update_textual_tab_labels()

        assert tabs["tab-unmatched"].label == "Unmatched [1]"


class TestPerTabWidthFixedTabCount:
    def test_per_tab_width_accounts_for_all_fixed_tabs(self, app):
        """per_tab_width must divide by (num_tabs + Unmatched + Hand-off),
        not (num_tabs + 1)."""
        mock_tc, tabs = _mock_tabbed_content()
        _wire_query_one(app, mock_tc)

        app._size = Size(80, 25)
        long_name = "a-tab-name-that-is-twenty-five-chars"  # len 37
        app._tab_original_names = {"tab-real": long_name}
        app._tab_session_ids = {"tab-real": set()}
        app.panels = {}

        app._update_textual_tab_labels()

        label = tabs["tab-tabreal"].label
        assert label == f"{long_name[:17]}… [0/0]"
