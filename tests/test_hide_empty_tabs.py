"""Tests for the "hide empty tabs" iTerm scope setting.

The setting hides tabs/windows that have no claude or codex session running.
It must compose orthogonally with iterm_scope: a tab is hidden ONLY when it is
genuinely empty (no active process in ANY of its sessions), never because of
which iTerm2 window it lives in. This directly guards the reverted bug where
non-self-window tabs with active sessions were being hidden.

Also covers the runtime path: a hook event for a session in a currently hidden
(empty) tab must trigger a layout refresh (so the tab reappears) rather than
spawning a detached fallback panel.
"""

from unittest.mock import MagicMock

import iterm2.api_pb2
import pytest
from iterm2.session import Session, Splitter

from claude_monitor.iterm2_layout import (
    filter_tabs_by_scope,
    filter_tabs_hide_empty,
    job_is_active,
)
from claude_monitor.settings import Settings
from claude_monitor.tui import AutoAcceptTUI

# ---------------------------------------------------------------------------
# Tree-building helpers (real iterm2 Session/Splitter objects so the
# isinstance checks in collect_session_ids pass)
# ---------------------------------------------------------------------------


def _mk_session(sid: str, title: str = "sess") -> Session:
    summary = iterm2.api_pb2.SessionSummary()
    summary.unique_identifier = sid
    summary.title = title
    return Session(None, None, summary=summary)


def _mk_tab(tab_id: str, *session_ids: str) -> tuple:
    """Build a (tab_id, tab_name, root_splitter) tuple containing given sessions."""
    root = Splitter(False)
    for sid in session_ids:
        root.add_child(_mk_session(sid))
    return (tab_id, f"tab-{tab_id}", root)


# ---------------------------------------------------------------------------
# Settings dataclass
# ---------------------------------------------------------------------------


class TestSettingsField:
    def test_default_is_false(self):
        assert Settings().iterm_hide_empty_tabs is False

    def test_roundtrip_true(self):
        assert Settings(iterm_hide_empty_tabs=True).iterm_hide_empty_tabs is True


# ---------------------------------------------------------------------------
# job_is_active marker matching
# ---------------------------------------------------------------------------


class TestJobIsActive:
    @pytest.mark.parametrize("job", ["claude", "Claude", "codex", "CODEX", "node claude wrapper"])
    def test_active_markers(self, job):
        assert job_is_active(job) is True

    @pytest.mark.parametrize("job", ["", None, "node", "bash", "-zsh", "python"])
    def test_inactive(self, job):
        assert job_is_active(job) is False


# ---------------------------------------------------------------------------
# filter_tabs_hide_empty — pure filtering logic
# ---------------------------------------------------------------------------


class TestFilterTabsHideEmpty:
    def test_disabled_is_noop(self):
        """When the setting is off, every tab is returned unchanged."""
        tabs = [_mk_tab("t1", "a"), _mk_tab("t2", "b")]
        procs = {}  # no active processes anywhere
        result = filter_tabs_hide_empty(tabs, "self", procs, enabled=False)
        assert result == tabs

    def test_no_self_sid_is_noop(self):
        """Without a locatable self session, nothing is hidden (safe fallback)."""
        tabs = [_mk_tab("t1", "a")]
        result = filter_tabs_hide_empty(tabs, None, {}, enabled=True)
        assert result == tabs

    def test_empty_tab_is_hidden(self):
        """A tab whose sessions are all idle is dropped when enabled."""
        self_tab = _mk_tab("self", "self-sid")
        idle_tab = _mk_tab("idle", "idle-a", "idle-b")
        tabs = [self_tab, idle_tab]
        procs = {"idle-a": "node", "idle-b": "bash"}  # no claude/codex
        result = filter_tabs_hide_empty(tabs, "self-sid", procs, enabled=True)
        assert result == [self_tab]

    def test_active_tab_is_kept(self):
        """A tab with an active claude/codex session survives."""
        self_tab = _mk_tab("self", "self-sid")
        active_tab = _mk_tab("active", "act-a")
        tabs = [self_tab, active_tab]
        procs = {"act-a": "claude"}
        result = filter_tabs_hide_empty(tabs, "self-sid", procs, enabled=True)
        assert result == tabs

    def test_self_tab_always_kept_even_if_idle(self):
        """The TUI's own tab is never hidden, even with no active process."""
        self_tab = _mk_tab("self", "self-sid")
        tabs = [self_tab]
        procs = {"self-sid": "python"}  # self is idle
        result = filter_tabs_hide_empty(tabs, "self-sid", procs, enabled=True)
        assert result == [self_tab]

    def test_multi_window_active_tab_not_hidden(self):
        """REGRESSION: a tab in a NON-self window with an active session must
        NOT be hidden. This reproduces the reverted bug where every tab outside
        the TUI's own iTerm2 window was dropped even with running sessions.

        filter_tabs_hide_empty has no notion of windows at all — it only sees
        the flat tab list and the process map — so a tab is kept purely on
        process activity, regardless of which window it came from.
        """
        # scope=all_windows: three windows' worth of tabs are all in scope.
        self_tab = _mk_tab("w1-self", "self-sid")  # window 1 (TUI's own)
        other_win_active = _mk_tab("w2-active", "w2-sid")  # window 2, running claude
        other_win_active2 = _mk_tab("w3-active", "w3-sid")  # window 3, running codex
        other_win_idle = _mk_tab("w2-idle", "w2-idle-sid")  # window 2, idle
        tabs = [self_tab, other_win_active, other_win_active2, other_win_idle]
        procs = {
            "self-sid": "python",
            "w2-sid": "claude",
            "w3-sid": "codex",
            "w2-idle-sid": "node",
        }
        result = filter_tabs_hide_empty(tabs, "self-sid", procs, enabled=True)
        # Self tab + both active non-self-window tabs kept; only the idle one dropped.
        assert result == [self_tab, other_win_active, other_win_active2]

    def test_partial_active_session_keeps_tab(self):
        """A tab with a mix of idle and active sessions is kept."""
        self_tab = _mk_tab("self", "self-sid")
        mixed_tab = _mk_tab("mixed", "idle-x", "active-y")
        tabs = [self_tab, mixed_tab]
        procs = {"idle-x": "bash", "active-y": "claude"}
        result = filter_tabs_hide_empty(tabs, "self-sid", procs, enabled=True)
        assert result == tabs

    def test_composes_with_scope_all_windows(self):
        """hide_empty applied after all_windows scope removes only idle tabs."""
        self_tab = _mk_tab("w1-self", "self-sid")
        w2_active = _mk_tab("w2-active", "w2-sid")
        w2_idle = _mk_tab("w2-idle", "w2-idle-sid")
        all_tabs = [self_tab, w2_active, w2_idle]
        # all_windows scope keeps everything...
        scoped = filter_tabs_by_scope(all_tabs, "self-sid", "all_windows", {})
        assert scoped == all_tabs
        # ...then hide_empty drops only the idle tab (window identity ignored).
        procs = {"self-sid": "python", "w2-sid": "codex", "w2-idle-sid": "bash"}
        result = filter_tabs_hide_empty(scoped, "self-sid", procs, enabled=True)
        assert result == [self_tab, w2_active]


# ---------------------------------------------------------------------------
# Runtime: hook event for a session in a hidden tab triggers refresh
# ---------------------------------------------------------------------------


@pytest.fixture
def app(monkeypatch):
    import claude_monitor.tui as tui_mod

    monkeypatch.setattr(tui_mod, "_layout_tabs", [])
    monkeypatch.setattr(tui_mod, "_self_session_id", None)
    monkeypatch.setattr("claude_monitor.app_base.fetch_usage", lambda: None)
    return AutoAcceptTUI()


def _mk_event(claude_sid="claude-abc", iterm_sid="iterm-xyz"):
    return {
        "session_id": claude_sid,
        "cwd": "/tmp/proj",
        "_iterm_session_id": iterm_sid,
        "hook_event_name": "SessionStart",
    }


class TestHiddenTabRefresh:
    def test_initial_hidden_tab_sids_is_empty(self, app):
        assert app._hidden_tab_iterm_sids == set()

    def test_event_for_hidden_tab_triggers_refresh_not_panel(self, app):
        """A hook event for a session in a hidden (empty) tab re-renders the
        layout and does NOT create a fallback panel."""
        app._do_refresh = MagicMock()
        app._hidden_tab_iterm_sids = {"iterm-hidden"}
        data = _mk_event(claude_sid="c-hidden", iterm_sid="iterm-hidden")

        result = app._resolve_panel(data)

        assert result is None
        assert "c-hidden" not in app.panels
        app._do_refresh.assert_called_once()
        # The sid is cleared so a burst of events triggers a single refresh.
        assert "iterm-hidden" not in app._hidden_tab_iterm_sids

    def test_event_for_non_hidden_pane_still_creates_fallback(self, app):
        """Sessions not in a hidden tab are unaffected by the new branch."""
        app._do_refresh = MagicMock()
        app.query_one = MagicMock(return_value=MagicMock())
        app.mount = MagicMock()
        data = _mk_event(claude_sid="c-normal", iterm_sid="iterm-normal")

        result = app._resolve_panel(data)

        assert result is not None
        assert "c-normal" in app.panels
        app._do_refresh.assert_not_called()

    def test_hidden_and_out_of_scope_are_distinct(self, app):
        """out-of-scope sids are dropped silently; hidden-tab sids trigger a
        refresh. The two collections must not be conflated."""
        app._do_refresh = MagicMock()
        app._out_of_scope_iterm_sids = {"iterm-oos"}
        app._hidden_tab_iterm_sids = {"iterm-hidden"}

        # out-of-scope: dropped, no refresh
        assert app._resolve_panel(_mk_event("c1", "iterm-oos")) is None
        app._do_refresh.assert_not_called()

        # hidden-tab: dropped, refresh fired
        assert app._resolve_panel(_mk_event("c2", "iterm-hidden")) is None
        app._do_refresh.assert_called_once()
