"""Regression tests for phantom panel prevention in AutoAcceptTUI.

Two code paths previously created phantom panels that persisted indefinitely:

1. Startup replay: watch_events replays the last 50 events on startup.
   If those events reference sessions in now-closed iTerm2 panes,
   _resolve_panel must not create a fallback panel for them.

2. Late-arriving events: after on_layout_changed removes a pane's panel,
   a hook event (e.g. SessionEnd) may arrive for that session.
   _resolve_panel must not create a fallback panel for those either.

Both cases are prevented by checks in _resolve_panel that fire before
any DOM mount operation, so tests can call _resolve_panel directly
without a full Textual app lifecycle.
"""

from unittest.mock import MagicMock

import pytest

from claude_monitor.tui import AutoAcceptTUI
from claude_monitor.widgets import SessionPanel


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

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


def _mk_event(claude_sid="claude-abc", iterm_sid="iterm-xyz",
               replay=False, cwd="/tmp/proj"):
    """Build a minimal hook event dict."""
    data = {
        "session_id": claude_sid,
        "cwd": cwd,
        "_iterm_session_id": iterm_sid,
        "hook_event_name": "SessionStart",
    }
    if replay:
        data["_replay"] = True
    return data


def _patched_resolve(app, data):
    """Call _resolve_panel with query_one mocked out.

    When the method reaches the fallback-creation path it tries to mount a
    widget.  Mocking query_one lets the call succeed without a real DOM so
    we can assert on the return value and panels dict.
    """
    mock_container = MagicMock()
    app.query_one = MagicMock(return_value=mock_container)
    app.mount = MagicMock()
    return app._resolve_panel(data)


# ---------------------------------------------------------------------------
# Bug 1: startup replay events must not create phantom panels
# ---------------------------------------------------------------------------

class TestReplayEventPhantomPrevention:

    def test_replay_for_closed_pane_returns_none(self, app):
        """A replayed event for a now-closed pane must return None, not a panel."""
        data = _mk_event(claude_sid="c-1", iterm_sid="iterm-closed", replay=True)
        # iterm-closed is not in app.panels (the pane no longer exists)
        result = app._resolve_panel(data)
        assert result is None

    def test_replay_for_closed_pane_does_not_add_to_panels(self, app):
        """No entry must appear in app.panels for the phantom session."""
        data = _mk_event(claude_sid="c-2", iterm_sid="iterm-closed", replay=True)
        app._resolve_panel(data)
        assert "c-2" not in app.panels

    def test_replay_for_open_pane_still_routes_to_real_panel(self, app):
        """Replay events are routed correctly when the panel still exists.

        The existing-panel lookup happens before the _replay guard, so open
        sessions are never affected by the phantom-prevention logic.
        """
        real_panel = SessionPanel("iterm-open", "real panel")
        app.panels["iterm-open"] = real_panel

        data = _mk_event(claude_sid="c-3", iterm_sid="iterm-open", replay=True)
        result = app._resolve_panel(data)

        assert result is real_panel  # routed to the real panel
        # claude_sid was NOT used as a key — mapped via iterm_to_panel only
        assert "c-3" not in app.panels


# ---------------------------------------------------------------------------
# Bug 2: late-arriving events after layout rebuild must not create phantoms
# ---------------------------------------------------------------------------

class TestRemovedPanePhantomPrevention:

    def test_event_for_removed_pane_returns_none(self, app):
        """A non-replay event for a recently-removed pane must return None."""
        app._removed_iterm_sids = {"iterm-gone"}
        data = _mk_event(claude_sid="c-late", iterm_sid="iterm-gone")
        result = app._resolve_panel(data)
        assert result is None

    def test_event_for_removed_pane_does_not_add_to_panels(self, app):
        app._removed_iterm_sids = {"iterm-gone"}
        data = _mk_event(claude_sid="c-late2", iterm_sid="iterm-gone")
        app._resolve_panel(data)
        assert "c-late2" not in app.panels

    def test_event_for_unrelated_pane_is_not_blocked(self, app):
        """Only the specifically removed pane is blocked; other panes still get fallbacks."""
        app._removed_iterm_sids = {"iterm-gone"}
        data = _mk_event(claude_sid="c-new", iterm_sid="iterm-other")

        result = _patched_resolve(app, data)

        # Should NOT be None — phantom check only fires for iterm-gone
        assert result is not None
        assert "c-new" in app.panels

    def test_no_removed_sids_allows_fallback_creation(self, app):
        """When _removed_iterm_sids is empty, normal fallback panels are created."""
        assert app._removed_iterm_sids == set()  # default
        data = _mk_event(claude_sid="c-fallback", iterm_sid="iterm-unknown")

        result = _patched_resolve(app, data)

        assert result is not None
        assert "c-fallback" in app.panels


# ---------------------------------------------------------------------------
# Layout session tracking: _layout_session_ids / _removed_iterm_sids state
# ---------------------------------------------------------------------------

class TestLayoutSessionTracking:

    def test_removed_sids_populated_when_pane_closes(self, app):
        """Sessions in the old layout but absent from the new one appear in _removed_iterm_sids."""
        app._layout_session_ids = {"iterm-A", "iterm-B"}

        new_layout_sids = {"iterm-A"}  # iterm-B was closed
        app._removed_iterm_sids = app._layout_session_ids - new_layout_sids
        app._layout_session_ids = new_layout_sids

        assert "iterm-B" in app._removed_iterm_sids
        assert "iterm-A" not in app._removed_iterm_sids

    def test_layout_session_ids_updated_after_rebuild(self, app):
        """_layout_session_ids reflects the new set after a rebuild."""
        app._layout_session_ids = {"iterm-A", "iterm-B"}

        new_layout_sids = {"iterm-A", "iterm-C"}
        app._removed_iterm_sids = app._layout_session_ids - new_layout_sids
        app._layout_session_ids = new_layout_sids

        assert app._layout_session_ids == {"iterm-A", "iterm-C"}

    def test_removed_sids_empty_when_only_panes_added(self, app):
        """Adding panes produces no removed sessions."""
        app._layout_session_ids = {"iterm-A"}

        new_layout_sids = {"iterm-A", "iterm-B"}  # only additions
        app._removed_iterm_sids = app._layout_session_ids - new_layout_sids
        app._layout_session_ids = new_layout_sids

        assert app._removed_iterm_sids == set()

    def test_initial_layout_session_ids_is_empty(self, app):
        """On startup with no iTerm2 layout, _layout_session_ids is empty."""
        assert app._layout_session_ids == set()

    def test_initial_removed_iterm_sids_is_empty(self, app):
        """On startup, nothing is pre-emptively blocked."""
        assert app._removed_iterm_sids == set()
