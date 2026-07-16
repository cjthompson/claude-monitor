"""Tests for the "hide empty tabs" iTerm scope setting.

The setting hides tabs/windows that have no claude or codex session running.
It must compose orthogonally with iterm_scope: a tab is hidden ONLY when it is
genuinely empty (no active process in ANY of its sessions), never because of
which iTerm2 window it lives in.

Detection walks the real OS process tree from each session's shell PID rather
than matching iTerm2's "jobName" variable. jobName reports whichever
subprocess is momentarily in the pane's foreground (an MCP server, a
`caffeinate` wrapper, a shell snapshot script, ...) — verified against a live
iTerm2 session, an active claude session's jobName is "node", "caffeinate", or
similar, essentially never "claude" itself, which instead sits as a stable
direct child of the pane's shell. A prior fix keyed on jobName and appeared to
work only because the TUI's own tab is unconditionally kept — every other
window's genuinely-active tabs were still hidden. These tests guard the actual
fix (process-tree walk) against that exact failure mode.

Also covers the runtime path: a hook event for a session in a currently hidden
(empty) tab must trigger a layout refresh (so the tab reappears) rather than
spawning a detached fallback panel.
"""

from unittest.mock import MagicMock, patch

import iterm2.api_pb2
import pytest
from iterm2.session import Session, Splitter

from claude_monitor.iterm2_layout import (
    _process_tree_has_target,
    _snapshot_process_tree,
    filter_tabs_by_scope,
    filter_tabs_hide_empty,
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
# _snapshot_process_tree — parses `ps -A -o pid=,ppid=,comm=` output
# ---------------------------------------------------------------------------


class TestSnapshotProcessTree:
    def test_parses_ps_output(self):
        fake_stdout = (
            "    1     0 launchd\n"
            "93496 93495 -fish\n"
            "94118 93496 claude\n"
            "94210 94118 npm exec chrome-devtools-mcp@1.6.0\n"
            "94395 94210 chrome-devtools-mcp\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_stdout)
            children, comm = _snapshot_process_tree()
        assert comm[94118] == "claude"
        assert comm[94395] == "chrome-devtools-mcp"
        assert 94118 in children[93496]
        assert 94210 in children[94118]

    def test_full_path_comm_is_basenamed(self):
        """Some claude processes report comm as a full app-bundle path."""
        fake_stdout = (
            "68907     1 /Users/x/.local/share/claude/ClaudeCode.app/Contents/MacOS/claude\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(stdout=fake_stdout)
            _children, comm = _snapshot_process_tree()
        assert comm[68907] == "claude"

    def test_ps_failure_returns_empty(self):
        with patch("subprocess.run", side_effect=OSError("no ps")):
            children, comm = _snapshot_process_tree()
        assert children == {}
        assert comm == {}


# ---------------------------------------------------------------------------
# _process_tree_has_target — descendant-tree walk
# ---------------------------------------------------------------------------


class TestProcessTreeHasTarget:
    def test_none_root_pid_is_false(self):
        assert _process_tree_has_target(None, {}, {}) is False

    def test_root_itself_matches(self):
        assert _process_tree_has_target(1, {}, {1: "claude"}) is True

    def test_direct_child_matches(self):
        children = {1: [2]}
        comm = {1: "-fish", 2: "claude"}
        assert _process_tree_has_target(1, children, comm) is True

    def test_no_match_in_tree(self):
        children = {1: [2, 3]}
        comm = {1: "-fish", 2: "node", 3: "caffeinate"}
        assert _process_tree_has_target(1, children, comm) is False

    def test_case_insensitive(self):
        children = {1: [2]}
        comm = {1: "-fish", 2: "Codex"}
        assert _process_tree_has_target(1, children, comm) is True

    def test_target_buried_under_unrelated_foreground_job(self):
        """REGRESSION: the real-world shape of the bug. The pane's current
        foreground job (what iTerm2's jobName would report) is an MCP server
        subprocess two levels below the shell; claude itself is a direct
        child of the shell, sitting above the transient foreground job.
        A jobName string match would see "chrome-devtools-mcp" and never
        find "claude" — the tree walk starting from the shell PID does.
        """
        # shell(1) -> claude(2) -> npm-exec(3) -> chrome-devtools-mcp(4) -> watchdog(5)
        children = {1: [2], 2: [3], 3: [4], 4: [5]}
        comm = {
            1: "-fish",
            2: "claude",
            3: "npm exec chrome-devtools-mcp@1.6.0",
            4: "chrome-devtools-mcp",
            5: "node",
        }
        assert _process_tree_has_target(1, children, comm) is True


# ---------------------------------------------------------------------------
# filter_tabs_hide_empty — pure filtering logic
# ---------------------------------------------------------------------------


def _pids_children_comm(sid_to_pid: dict, active_pids: set) -> tuple:
    """Build (session_pids, children, comm) where each pid in active_pids
    has a direct "claude" child, and all other pids have only a shell."""
    children: dict = {}
    comm: dict = {}
    next_pid = max(sid_to_pid.values(), default=0) + 1000
    for pid in sid_to_pid.values():
        comm[pid] = "-fish"
        if pid in active_pids:
            comm[next_pid] = "claude"
            children[pid] = [next_pid]
            next_pid += 1
    return dict(sid_to_pid), children, comm


class TestFilterTabsHideEmpty:
    def test_disabled_is_noop(self):
        """When the setting is off, every tab is returned unchanged."""
        tabs = [_mk_tab("t1", "a"), _mk_tab("t2", "b")]
        result = filter_tabs_hide_empty(tabs, "self", {}, {}, {}, enabled=False)
        assert result == tabs

    def test_no_self_sid_is_noop(self):
        """Without a locatable self session, nothing is hidden (safe fallback)."""
        tabs = [_mk_tab("t1", "a")]
        result = filter_tabs_hide_empty(tabs, None, {}, {}, {}, enabled=True)
        assert result == tabs

    def test_empty_tab_is_hidden(self):
        """A tab whose sessions are all idle is dropped when enabled."""
        self_tab = _mk_tab("self", "self-sid")
        idle_tab = _mk_tab("idle", "idle-a", "idle-b")
        tabs = [self_tab, idle_tab]
        pids, children, comm = _pids_children_comm(
            {"self-sid": 1, "idle-a": 2, "idle-b": 3}, active_pids=set()
        )
        result = filter_tabs_hide_empty(tabs, "self-sid", pids, children, comm, enabled=True)
        assert result == [self_tab]

    def test_active_tab_is_kept(self):
        """A tab with an active claude/codex session survives."""
        self_tab = _mk_tab("self", "self-sid")
        active_tab = _mk_tab("active", "act-a")
        tabs = [self_tab, active_tab]
        pids, children, comm = _pids_children_comm({"self-sid": 1, "act-a": 2}, active_pids={2})
        result = filter_tabs_hide_empty(tabs, "self-sid", pids, children, comm, enabled=True)
        assert result == tabs

    def test_self_tab_always_kept_even_if_idle(self):
        """The TUI's own tab is never hidden, even with no active process."""
        self_tab = _mk_tab("self", "self-sid")
        tabs = [self_tab]
        pids, children, comm = _pids_children_comm({"self-sid": 1}, active_pids=set())
        result = filter_tabs_hide_empty(tabs, "self-sid", pids, children, comm, enabled=True)
        assert result == [self_tab]

    def test_multi_window_active_tab_not_hidden(self):
        """REGRESSION: a tab in a NON-self window with an active session must
        NOT be hidden. This reproduces the reverted bug where every tab outside
        the TUI's own iTerm2 window was dropped even with running sessions.

        filter_tabs_hide_empty has no notion of windows at all — it only sees
        the flat tab list and each session's process tree — so a tab is kept
        purely on process activity, regardless of which window it came from.
        """
        # scope=all_windows: three windows' worth of tabs are all in scope.
        self_tab = _mk_tab("w1-self", "self-sid")  # window 1 (TUI's own)
        other_win_active = _mk_tab("w2-active", "w2-sid")  # window 2, running claude
        other_win_active2 = _mk_tab("w3-active", "w3-sid")  # window 3, running codex
        other_win_idle = _mk_tab("w2-idle", "w2-idle-sid")  # window 2, idle
        tabs = [self_tab, other_win_active, other_win_active2, other_win_idle]
        pids, children, comm = _pids_children_comm(
            {"self-sid": 1, "w2-sid": 2, "w3-sid": 3, "w2-idle-sid": 4},
            active_pids={2, 3},
        )
        result = filter_tabs_hide_empty(tabs, "self-sid", pids, children, comm, enabled=True)
        # Self tab + both active non-self-window tabs kept; only the idle one dropped.
        assert result == [self_tab, other_win_active, other_win_active2]

    def test_active_process_buried_under_transient_foreground_job(self):
        """REGRESSION: mirrors the real bug shape end-to-end through the
        filter, not just the tree walk. A non-self-window session's momentary
        foreground job is an unrelated wrapper (e.g. `caffeinate`), with the
        actual claude process sitting one level up as a direct shell child.
        The tab must still be kept.
        """
        self_tab = _mk_tab("w1-self", "self-sid")
        other_win_active = _mk_tab("w2-active", "w2-sid")
        tabs = [self_tab, other_win_active]
        # w2-sid's shell (pid 10) -> claude (pid 11) -> caffeinate (pid 12, the
        # transient foreground job a jobName-based check would see instead).
        pids = {"self-sid": 1, "w2-sid": 10}
        children = {1: [], 10: [11], 11: [12]}
        comm = {1: "-fish", 10: "-fish", 11: "claude", 12: "caffeinate"}
        result = filter_tabs_hide_empty(tabs, "self-sid", pids, children, comm, enabled=True)
        assert result == tabs

    def test_partial_active_session_keeps_tab(self):
        """A tab with a mix of idle and active sessions is kept."""
        self_tab = _mk_tab("self", "self-sid")
        mixed_tab = _mk_tab("mixed", "idle-x", "active-y")
        tabs = [self_tab, mixed_tab]
        pids, children, comm = _pids_children_comm(
            {"self-sid": 1, "idle-x": 2, "active-y": 3}, active_pids={3}
        )
        result = filter_tabs_hide_empty(tabs, "self-sid", pids, children, comm, enabled=True)
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
        pids, children, comm = _pids_children_comm(
            {"self-sid": 1, "w2-sid": 2, "w2-idle-sid": 3}, active_pids={2}
        )
        result = filter_tabs_hide_empty(scoped, "self-sid", pids, children, comm, enabled=True)
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
