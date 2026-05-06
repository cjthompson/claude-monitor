#!/usr/bin/env python3
"""Textual TUI for Claude Code auto-accept.

Watches events logged by auto-accept-hook.py and displays them per session.
Uses iTerm2 API to discover pane layout and session names before startup.
Polls iTerm2 every few seconds to detect pane splits/closes.
"""

import json
import logging
import os
import re
import threading
import time
from datetime import datetime

from textual import work
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Footer, RichLog, Static, TabbedContent, TabPane

from claude_monitor import (
    __version__,
    SIGNAL_DIR,
    STATE_FILE,
    LOG_FILE,
    extract_iterm_session_id,
    read_state,
)
from claude_monitor.app_base import MonitorApp
from claude_monitor.commands import MonitorCommands
from claude_monitor.formatting import (
    _safe_css_id,
    _safe_tab_css_id,
    _format_ask_user_question_inline,
    _format_ask_user_question_detail,
    _oneline as _oneline_fn,
    format_event as _format_event_shared,
)
from claude_monitor.iterm2_layout import (
    ITERM2_AVAILABLE,
    LayoutFetcher,
    LayoutFingerprint,
    WidgetTreeBuilder,
    KeystrokeSender,
    collect_session_ids,
    filter_tabs_by_scope,
    set_tab_titles,
    start_persistent_connection,
    _iterm2_ready,
)
from claude_monitor.messages import HookEvent
from claude_monitor.screens import (
    PaneContextMenu,
    ChoicesScreen,
    QuestionsScreen,
    HelpScreen,
)
from claude_monitor.settings import Settings, SettingsScreen, load_settings, save_settings
from claude_monitor.usage import (
    fetch_usage,
    format_usage_inline,
    invalidate_usage_cache,
    set_oauth_json,
    set_on_token_refreshed,
)
from claude_monitor.widgets import (
    HorizontalScrollBarRender,
    VerticalScrollBarRender,
    FixedWidthSparkline,
    SessionPanel,
    DashboardPanel,
)

os.makedirs(SIGNAL_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s %(message)s",
    force=True,
)
log = logging.getLogger(__name__)
# Suppress noisy third-party loggers that flood the debug log with
# websocket protocol frames and asyncio selector events on every poll.
for _noisy in ("websockets", "asyncio", "iterm2.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# --- Layout change messages (iTerm2-specific) ---

from textual.message import Message


class LayoutChanged(Message):
    """Posted when iTerm2 layout structure has changed (panes added/removed/rearranged)."""
    def __init__(self, tabs: list, self_session_id: "str | None") -> None:
        super().__init__()
        self.tabs = tabs
        self.self_session_id = self_session_id


class LayoutResized(Message):
    """Posted when iTerm2 pane sizes changed but structure is the same."""
    def __init__(self, tabs: list) -> None:
        super().__init__()
        self.tabs = tabs


# Fetch initial layout before Textual starts
_layout_tabs: list = []  # [(tab_id, tab_name, root_splitter), ...]
_self_session_id: "str | None" = None


def fetch_iterm_layout() -> None:
    global _layout_tabs, _self_session_id
    start_persistent_connection()
    if not _iterm2_ready.wait(timeout=5):
        raise ConnectionRefusedError("Could not connect to iTerm2")
    tabs, self_sid, win_groups = LayoutFetcher.fetch_sync()
    settings = load_settings()
    _layout_tabs = filter_tabs_by_scope(tabs, self_sid, settings.iterm_scope, win_groups)
    _self_session_id = self_sid
    log.debug(
        f"fetch_iterm_layout done: tabs={len(_layout_tabs)}, self={_self_session_id}"
    )


class AutoAcceptTUI(MonitorApp):
    """TUI that mirrors iTerm2 pane layout and displays auto-accept events."""

    CSS = """
    #layout-root {
        height: 1fr;
        width: 1fr;
    }
    #tab-content {
        height: 1fr;
        width: 1fr;
    }
    #tab-content TabPane {
        padding: 0;
    }
    #tab-content Tab.-active {
        background: $accent;
        color: $text;
        text-style: bold;
    }
    #tab-content Tab {
        text-style: none;
    }
    SessionPanel.worktree {
        border: solid $secondary;
    }
    RichLog {
        scrollbar-size-vertical: 1;
        scrollbar-size-horizontal: 1;
        scrollbar-background: $background;
        scrollbar-background-hover: $background;
        scrollbar-background-active: $background;
    }
    #status-bar {
        dock: top;
        height: 1;
        text-style: bold;
    }
    #status-bar.running {
        background: #10643c;
        color: #f0fff5;
    }
    #status-bar.paused {
        background: #6e280f;
        color: #ffe1c8;
    }
    #status-bar.refreshing {
        background: $accent;
        color: $text;
    }
    #status-left {
        width: 1fr;
        padding: 0 1;
    }
    #status-right {
        width: auto;
        padding: 0 1;
    }
    """

    TITLE = "Claude Monitor (Auto)"

    COMMANDS = {MonitorCommands}

    BINDINGS = [
        Binding("a", "toggle_pause", "Auto/Manual"),
        Binding("shift+tab", "toggle_pause", "Auto/Manual", show=False),
        Binding("c", "show_choices", "Choices", show=False),
        Binding("u", "show_questions", "Questions", show=False),
        Binding("r", "refresh_layout", "Refresh", show=False),
        Binding("s", "open_settings", "Settings"),
        Binding("d", "toggle_dashboard", "Dashboard", show=False),
        Binding("D", "toggle_dashboard_tab", "Dashboard Tab", show=False),
        Binding("equals_sign", "grow_dashboard", "Dash+", show=False),
        Binding("minus", "shrink_dashboard", "Dash-", show=False),
        Binding("right_square_bracket", "next_tab", "Next Tab", show=False),
        Binding("left_square_bracket", "prev_tab", "Prev Tab", show=False),
        Binding("x", "close_tab", "Close Tab", show=False),
        Binding("question_mark", "show_help", "Help"),
        Binding("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        # panels and dashboard are defined in MonitorApp.__init__
        self._iterm_to_panel: dict[str, str] = {}
        self._rebuilding = False
        self._current_structure_fp = None
        self._current_size_fp = None
        self._paused_sessions: set[str] = set()
        self._ask_paused_sessions: set[str] = set()
        self._tab_original_names: dict[str, str] = {}
        self._tab_session_ids: dict[str, set[str]] = {}
        self._tab_title_lock = threading.Lock()
        self._tab_title_pending = False
        # iTerm2 session IDs currently in the layout (from WidgetTreeBuilder)
        self._layout_session_ids: set[str] = set()
        # iTerm2 session IDs removed in the most recent layout rebuild
        self._removed_iterm_sids: set[str] = set()

    # ------------------------------------------------------------------
    # Abstract method implementations (required by MonitorApp)
    # ------------------------------------------------------------------

    def is_pane_paused(self, iterm_sid: str) -> bool:
        return self._global_paused or iterm_sid in self._paused_sessions

    def is_ask_paused(self, iterm_sid: str) -> bool:
        return iterm_sid in self._ask_paused_sessions

    def action_toggle_pause(self) -> None:
        if self._global_paused or self._paused_sessions:
            # Any paused state → all auto
            self._global_paused = False
            self._paused_sessions.clear()
        else:
            # All auto → all manual
            self._global_paused = True
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()

    # ------------------------------------------------------------------
    # State persistence (override to include iTerm2 UUID collections)
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "global_paused": self._global_paused,
            "paused_sessions": list(self._paused_sessions),
            "excluded_tools": self.settings.excluded_tools or [],
            "ask_user_timeout": self.settings.ask_user_timeout,
            "ask_paused_sessions": list(self._ask_paused_sessions),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    def _load_state(self) -> None:
        state = read_state()
        self._global_paused = state.get("global_paused", False)
        self._paused_sessions = set(state.get("paused_sessions", []))
        self._ask_paused_sessions = set(state.get("ask_paused_sessions", []))

    # ------------------------------------------------------------------
    # Panel mode helpers
    # ------------------------------------------------------------------

    def _update_all_panel_modes(self) -> None:
        for panel in self.panels.values():
            if self.is_pane_paused(panel.session_id):
                panel.add_class("pane-paused")
            else:
                panel.remove_class("pane-paused")

    # ------------------------------------------------------------------
    # Compose / mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar", classes="running"):
            yield Static("AUTO", id="status-left")
            yield Static("", id="status-right")
        yield Vertical(id="layout-root")
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_settings(self.settings)
        # Build and mount initial layout from pre-fetched tabs
        if _layout_tabs:
            root = self.query_one("#layout-root")
            await self._mount_tabs(root, _layout_tabs, _self_session_id)
            self._current_structure_fp = LayoutFingerprint.structure(_layout_tabs)
            self._current_size_fp = LayoutFingerprint.size(_layout_tabs)
            for _, _, tree in _layout_tabs:
                self._layout_session_ids |= collect_session_ids(tree)
            log.debug(
                f"on_mount(): panels={list(self.panels.keys())}, "
                f"dashboard={self.dashboard is not None}"
            )
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        # Load state and prune stale session entries
        self._load_state()
        current_sids = set(self.panels.keys())
        stale = self._paused_sessions - current_sids
        if stale:
            self._paused_sessions -= stale
        stale_ask = self._ask_paused_sessions - current_sids
        if stale_ask:
            self._ask_paused_sessions -= stale_ask
        # Apply default mode from settings
        if self.settings.default_mode == "manual":
            self._global_paused = True
        elif self.settings.default_mode == "auto":
            self._global_paused = False
        # last_used: leave global pause state as-is
        self._save_state()
        self._update_all_panel_modes()
        # Clean up legacy files
        for legacy in ("paused", "paused-sessions.json"):
            p = os.path.join(SIGNAL_DIR, legacy)
            if os.path.exists(p):
                os.remove(p)
        self._update_status_bar()
        self.watch_events()
        self.watch_layout()
        if self.settings.account_usage:
            self._usage_polling = True
            self.poll_usage()
        self.set_interval(1.0, self._tick_status)
        self.serve_api()

    async def _mount_tabs(
        self,
        root,
        tabs: list,
        self_session_id: "str | None",
        old_panels: "dict | None" = None,
        old_dashboard: "DashboardPanel | None" = None,
    ) -> None:
        """Mount tab layout into a container. Handles single-tab and multi-tab cases."""
        # Record original tab names and which sessions belong to each tab
        for tab_id, tab_name, tree in tabs:
            if tab_id not in self._tab_original_names:
                # Strip any stacked " [N]" or " [N/N]" suffixes we may have set previously
                clean_name = re.sub(r'( \[\d+(/\d+)?\])+$', '', tab_name)
                self._tab_original_names[tab_id] = clean_name
            self._tab_session_ids[tab_id] = collect_session_ids(tree)

        if len(tabs) == 1:
            # Single tab — render directly without tab wrapper
            _tab_id, _tab_name, tree = tabs[0]
            layout, dash = WidgetTreeBuilder.build(
                tree, self_session_id, self.panels,
                old_panels=old_panels, old_dashboard=old_dashboard,
                settings=self.settings,
            )
            self.dashboard = dash
            await root.mount(layout)
        else:
            # Multiple tabs — wrap in TabbedContent
            tc = TabbedContent(id="tab-content")
            await root.mount(tc)
            for tab_id, tab_name, tree in tabs:
                layout, dash = WidgetTreeBuilder.build(
                    tree, self_session_id, self.panels,
                    old_panels=old_panels, old_dashboard=old_dashboard,
                    settings=self.settings,
                )
                if dash:
                    self.dashboard = dash
                pane = TabPane(tab_name, layout, id=_safe_tab_css_id(tab_id))
                await tc.add_pane(pane)

        self.update_tab_titles()

        # Replay event logs into the newly mounted RichLog widgets
        for panel in self.panels.values():
            if panel._event_log:
                try:
                    rl = panel.query_one(RichLog)
                    for line in panel._event_log:
                        rl.write(line)
                except Exception:
                    log.warning(
                        f"_mount_tabs: failed to replay event log for panel {panel.session_id}"
                    )
        if self.dashboard and self.dashboard._event_log:
            try:
                rl = self.dashboard.query_one(RichLog)
                for line in self.dashboard._event_log:
                    rl.write(line)
            except Exception:
                log.warning("_mount_tabs: failed to replay event log for dashboard")

    def _tick_status(self) -> None:
        """Refresh all panel status bars, dashboard, and top bar every second."""
        for panel in self.panels.values():
            panel._update_status()
        if self.dashboard:
            self.dashboard.refresh_dashboard(self.panels)
        self._update_status_bar()

    # ------------------------------------------------------------------
    # Layout polling
    # ------------------------------------------------------------------

    @work(thread=True, exit_on_error=False)
    def watch_layout(self) -> None:
        """Poll iTerm2 layout every 3 seconds for changes."""
        log.debug("watch_layout: started (polling mode)")
        while not self._stop_event.is_set():
            self._stop_event.wait(3.0)
            if self._stop_event.is_set():
                break
            try:
                # Compute tab titles and pass them to the layout fetch so both
                # operations share a single websocket connection.
                pending_titles = (
                    self._compute_tab_titles()
                    if self._tab_original_names and not self._rebuilding
                    else None
                )
                tabs, self_sid, win_groups = LayoutFetcher.fetch_sync(
                    tab_titles=pending_titles
                )
                tabs = filter_tabs_by_scope(
                    tabs, self_sid, self.settings.iterm_scope, win_groups
                )
                if tabs:
                    new_struct = LayoutFingerprint.structure(tabs)
                    if new_struct != self._current_structure_fp:
                        log.debug("watch_layout: structure changed")
                        self.post_message(LayoutChanged(tabs, self_sid))
                    else:
                        new_size = LayoutFingerprint.size(tabs)
                        if new_size != self._current_size_fp:
                            log.debug("watch_layout: sizes changed")
                            self.post_message(LayoutResized(tabs))
            except Exception as e:
                log.debug(f"watch_layout: error: {e}")
        log.debug("watch_layout: stopped")

    async def on_layout_changed(self, msg: LayoutChanged) -> None:
        """Rebuild the widget tree when iTerm2 layout changes."""
        log.debug("on_layout_changed: rebuilding layout")
        self._rebuilding = True
        try:
            # Save the currently active tab and focused panel before rebuild
            active_tab_id = None
            try:
                tc = self.query_one("#tab-content", TabbedContent)
                active_tab_id = tc.active
            except Exception:
                log.debug("on_layout_changed: failed to get active tab before rebuild")
            focused_session_id = None
            focused = self.focused
            if focused:
                for panel in self.panels.values():
                    if panel is focused or focused in panel.ancestors_with_self:
                        focused_session_id = panel.session_id
                        break

            # Save references to old state
            old_panels = dict(self.panels)
            old_dashboard = self.dashboard

            # Clear and rebuild
            self.panels = {}
            self.dashboard = None

            root = self.query_one("#layout-root")
            await root.remove_children()
            await self._mount_tabs(
                root, msg.tabs, msg.self_session_id,
                old_panels=old_panels, old_dashboard=old_dashboard,
            )

            # Restore active tab and focused panel after rebuild
            if active_tab_id:
                try:
                    tc = self.query_one("#tab-content", TabbedContent)
                    tc.active = active_tab_id
                except Exception:
                    log.warning(
                        f"on_layout_changed: failed to restore active tab {active_tab_id}"
                    )
            if focused_session_id and focused_session_id in self.panels:
                try:
                    self.panels[focused_session_id].focus()
                except Exception:
                    log.debug(
                        f"on_layout_changed: failed to restore focus to panel {focused_session_id}"
                    )

            self._current_structure_fp = LayoutFingerprint.structure(msg.tabs)
            self._current_size_fp = LayoutFingerprint.size(msg.tabs)

            # Track which iTerm2 sessions were removed in this rebuild.
            # Late-arriving events for these sessions must not create phantom panels.
            new_layout_sids: set[str] = set()
            for _, _, tree in msg.tabs:
                new_layout_sids |= collect_session_ids(tree)
            self._removed_iterm_sids = self._layout_session_ids - new_layout_sids
            self._layout_session_ids = new_layout_sids

            # Preserve iterm→panel mappings for sessions that still exist
            self._iterm_to_panel = {
                k: v for k, v in self._iterm_to_panel.items()
                if v in self.panels
            }
        finally:
            self._rebuilding = False

        self._update_all_panel_modes()
        self.update_tab_titles()
        log.debug(
            f"on_layout_changed: done. panels={list(self.panels.keys())}, "
            f"dashboard={self.dashboard is not None}"
        )

    def on_layout_resized(self, msg: LayoutResized) -> None:
        """Update widget sizes without rebuilding when only pane sizes changed."""
        self._current_size_fp = LayoutFingerprint.size(msg.tabs)
        for _tab_id, _tab_name, root in msg.tabs:
            self._apply_sizes(root)

    def _apply_sizes(self, node, parent_vertical=None) -> None:
        """Walk iTerm2 tree and update CSS sizes on matching existing widgets."""
        from iterm2.session import Session as _Session, Splitter as _Splitter

        if isinstance(node, _Session):
            return
        if not isinstance(node, _Splitter):
            return
        child_sizes = [LayoutFingerprint._frame_size(c) for c in node.children]
        if node.vertical:
            total = sum(w for w, _ in child_sizes) or 1
            fractions = [w / total for w, _ in child_sizes]
        else:
            total = sum(h for _, h in child_sizes) or 1
            fractions = [h / total for _, h in child_sizes]
        for i, child in enumerate(node.children):
            pct = round(fractions[i] * 100)
            if isinstance(child, _Session):
                css_id = _safe_css_id(child.session_id)
                try:
                    widget = self.query_one(f"#{css_id}")
                    if node.vertical:
                        widget.styles.width = f"{pct}%"
                    else:
                        widget.styles.height = f"{pct}%"
                except Exception:
                    log.debug(f"_apply_sizes: widget query failed for #{css_id}")
            else:
                self._apply_sizes(child, node.vertical)

    # ------------------------------------------------------------------
    # Hook event handling
    # ------------------------------------------------------------------

    @staticmethod
    def _iterm_sid_from_event(data: dict) -> str:
        """Extract the normalized iTerm2 session ID from a hook event dict."""
        return extract_iterm_session_id(data.get("_iterm_session_id") or "")

    def _resolve_panel(self, data: dict) -> "SessionPanel | None":
        """Find the panel for a hook event, mapping via iTerm2 session ID."""
        claude_sid = data.get("session_id", "")
        iterm_sid = self._iterm_sid_from_event(data)

        # Already mapped this claude session
        if claude_sid in self._iterm_to_panel:
            return self.panels.get(self._iterm_to_panel[claude_sid])

        # Match via _iterm_session_id from the hook
        if iterm_sid and iterm_sid in self.panels:
            self._iterm_to_panel[claude_sid] = iterm_sid
            return self.panels[iterm_sid]

        # Don't create phantom panels for:
        # 1. Replay events on startup — the pane may no longer exist.
        # 2. Events for iTerm2 sessions removed in the last layout rebuild —
        #    a late-arriving SessionEnd (or similar) after the pane was closed.
        if data.get("_replay"):
            return None
        if iterm_sid and iterm_sid in self._removed_iterm_sids:
            log.debug(f"_resolve_panel: dropping event for removed pane {iterm_sid[:8]}")
            return None

        # No match — create a fallback panel
        cwd = data.get("cwd", "")
        short_id = claude_sid[:8] if len(claude_sid) > 8 else claude_sid

        # Detect worktree sessions by cwd path
        worktree_name = None
        for marker in ("/.worktrees/", "/.claude/worktrees/"):
            if marker in cwd:
                parts = cwd.split(marker)
                worktree_name = parts[1].split("/")[0]
                break

        if worktree_name:
            title = f"WT:{worktree_name} [{short_id}]"
        else:
            title = f"{os.path.basename(cwd) or 'session'} [{short_id}]"

        css_id = _safe_css_id(claude_sid)
        panel = SessionPanel(claude_sid, title, id=css_id)
        if worktree_name:
            panel.add_class("worktree")
        self.panels[claude_sid] = panel
        self._iterm_to_panel[claude_sid] = claude_sid
        # Mount into layout root
        try:
            self.query_one("#layout-root").mount(panel)
        except Exception:
            log.warning(
                f"_create_fallback_panel: layout-root mount failed for {claude_sid}, "
                "falling back to status-bar mount"
            )
            self.mount(panel, before=self.query_one("#status-bar"))
        return panel

    def _is_dashboard_event(self, data: dict) -> bool:
        """Check if an event belongs to the TUI's own (dashboard) session."""
        iterm_sid = self._iterm_sid_from_event(data)
        return bool(iterm_sid and iterm_sid == _self_session_id)

    def on_hook_event(self, msg: HookEvent) -> None:
        if self._rebuilding:
            return
        data = msg.data
        event_name = data.get("hook_event_name", "")
        event_ts = datetime.fromtimestamp(data.get("_timestamp", time.time()))
        t = self._format_ts(event_ts)

        # Route events from the TUI's own session to the dashboard
        if self._is_dashboard_event(data) and self.dashboard:
            self._handle_dashboard_event(data, event_name, t)
            return

        panel = self._resolve_panel(data)
        if not panel:
            return

        panel.touch()
        self._apply_event(panel, data, event_name)
        # Track active timeout for status bar countdown
        if data.get("_decision") == "timeout" and data.get("_ask_timeout"):
            panel._pending_timeout = data["_timestamp"] + data["_ask_timeout"]
            panel._timeout_origin = data["_timestamp"]
        # Track deferred PermissionRequests so we don't auto-press Enter on the
        # follow-up permission_prompt Notification (which would defeat the
        # exclusion by selecting the first option of an AskUserQuestion menu).
        if event_name == "PermissionRequest" and data.get("_decision") == "deferred":
            panel._pending_deferred_at = data.get("_timestamp", time.time())
        label, detail = self._format_event(data, event_name)
        if label:
            panel.write(f"[{t}] {label} {detail}")
        panel._update_status()

        # Feed to dashboard combined feed
        if self.dashboard:
            if label:
                sid_short = panel.session_id[:8]
                self.dashboard.record_event(f"[{t}] [{sid_short}] {label} {detail}")

        self.update_tab_titles()

    def _handle_dashboard_event(self, data: dict, event_name: str, t: str) -> None:
        """Handle hook events from the TUI's own session on the dashboard."""
        dash = self.dashboard
        sid_short = _self_session_id[:8] if _self_session_id else "self"

        self._apply_event(dash, data, event_name)
        label, detail = self._format_event(data, event_name)
        if label:
            dash.record_event(f"[{t}] [{sid_short}] {label} {detail}")

    @staticmethod
    def _oneline(text: str, max_len: int = 0) -> str:
        return _oneline_fn(text, max_len)

    def _apply_event(self, panel, data: dict, event_name: str) -> None:
        """Apply side effects of a hook event to a panel (or dashboard).

        Mutates panel state: accept_count, active_agents, total_agents_completed,
        idle state, and triggers auto-approve keystrokes.  Must be called before
        _format_event so that labels reflect the updated state.
        """
        if event_name == "PermissionRequest":
            iterm_sid = self._iterm_sid_from_event(data)
            if not (iterm_sid and self.is_pane_paused(iterm_sid)):
                panel.accept_count += 1

        elif event_name == "Notification":
            ntype = data.get("notification_type", "")
            if ntype == "idle_prompt":
                if hasattr(panel, "mark_idle"):
                    panel.mark_idle()
                    self.update_tab_titles()
            elif ntype == "ask_timeout_complete":
                # Clear pending timeout — the hook's sleep finished
                origin = data.get("_timeout_origin")
                if (
                    panel._pending_timeout is not None
                    and getattr(panel, "_timeout_origin", None) == origin
                ):
                    panel._pending_timeout = None
                    panel._timeout_origin = None
                    data["_auto_accepted"] = True  # Signal to _format_event
            elif ntype == "permission_prompt":
                iterm_sid = self._iterm_sid_from_event(data)
                if (
                    iterm_sid
                    and iterm_sid != _self_session_id
                    and not self.is_pane_paused(iterm_sid)
                ):
                    # Skip auto-approve if panel has a pending AskUserQuestion timeout
                    pending = getattr(panel, "_pending_timeout", None)
                    pending_def = getattr(panel, "_pending_deferred_at", None)
                    if pending and pending > time.time():
                        log.debug(
                            f"Skipping auto-approve: AskUserQuestion timeout pending for {iterm_sid}"
                        )
                    elif pending_def and time.time() - pending_def < 30:
                        # The hook deferred the preceding PermissionRequest
                        # (excluded_tools, paused, etc.). Don't press Enter —
                        # the user must confirm manually.
                        log.debug(
                            f"Skipping auto-approve: deferred PermissionRequest for {iterm_sid}"
                        )
                        panel._pending_deferred_at = None
                    else:
                        panel.accept_count += 1
                        if not data.get("_replay"):
                            self._send_approve(iterm_sid)

        elif event_name == "PostToolUse":
            # Clear pending timeout when AskUserQuestion completes (user answered manually)
            if data.get("tool_name") == "AskUserQuestion":
                panel._pending_timeout = None
                panel._timeout_origin = None

        elif event_name == "SubagentStart":
            agent_id = data.get("agent_id", "?")
            panel.active_agents[agent_id] = data.get("agent_type", "?")

        elif event_name == "SubagentStop":
            panel.active_agents.pop(data.get("agent_id", "?"), None)
            panel.total_agents_completed += 1

    def _format_event(self, data: dict, event_name: str):
        """Format a hook event into display text. Delegates to shared formatter."""
        return _format_event_shared(
            data,
            event_name,
            is_pane_paused=self.is_pane_paused,
            get_panel=self._resolve_panel,
            oneline=self._oneline,
            self_sid=_self_session_id,
        )

    # ------------------------------------------------------------------
    # Auto-approve via iTerm2 keystroke
    # ------------------------------------------------------------------

    @work(thread=True, exit_on_error=False)
    def _send_approve(self, session_id: str) -> None:
        """Send Enter to an iTerm2 session to approve a permission prompt.

        Uses \\r (carriage return) because Claude Code reads raw terminal input
        where Enter generates CR, not LF.
        """
        ok = KeystrokeSender.send_approve(session_id)
        log.debug(f"_send_approve: session={session_id[:8]} ok={ok}")

    # ------------------------------------------------------------------
    # Tab title updates
    # ------------------------------------------------------------------

    def _compute_tab_titles(self) -> dict:
        """Compute tab titles with active/total session counts. Pure computation, no I/O."""
        tab_titles: dict[str, str] = {}
        for tab_id, original_name in self._tab_original_names.items():
            session_ids = self._tab_session_ids.get(tab_id, set())
            total_count = sum(1 for sid in session_ids if sid in self.panels)
            active_count = sum(
                1 for sid in session_ids
                if sid in self.panels and (
                    self.panels[sid]._state == "active"
                    or len(self.panels[sid].active_agents) > 0
                )
            )
            title = f"{original_name} [{active_count}/{total_count}]"
            tab_titles[tab_id] = title
        return tab_titles

    def _update_textual_tab_labels(self) -> None:
        """Trim Textual TabbedContent tab labels so all tabs fit the terminal width.

        Each label is trimmed to fit within an equal share of the tab bar width.
        Minimum label length is 6 characters (never truncated shorter).

        NOTE: Textual's Tabs widget does NOT support multi-line/wrapping tab bars.
        The #tabs-list uses a single Horizontal row with overflow: hidden hidden.
        Tabs that overflow the bar are scrolled into view when activated, but there
        is no native wrapping layout. Implementing a custom wrapping tab bar would
        require replacing TabbedContent entirely with a custom widget.
        """
        if not self._tab_original_names:
            return
        try:
            tc = self.query_one("#tab-content", TabbedContent)
        except Exception:
            return  # No TabbedContent present (single-tab mode)

        num_tabs = len(self._tab_original_names)
        if num_tabs == 0:
            return

        # Compute active/total counts for each tab (same as _compute_tab_titles)
        counts: dict[str, tuple] = {}
        for tab_id, original_name in self._tab_original_names.items():
            session_ids = self._tab_session_ids.get(tab_id, set())
            total_count = sum(1 for sid in session_ids if sid in self.panels)
            active_count = sum(
                1 for sid in session_ids
                if sid in self.panels and (
                    self.panels[sid]._state == "active"
                    or len(self.panels[sid].active_agents) > 0
                )
            )
            counts[tab_id] = (active_count, total_count)

        # Each tab gets an equal share of the terminal width.
        # Subtract 2 for the per-tab padding (1 char left, 1 char right).
        terminal_width = self.size.width
        per_tab_width = max(6, (terminal_width // num_tabs) - 2)

        for tab_id, original_name in self._tab_original_names.items():
            active_count, total_count = counts[tab_id]
            suffix = f" [{active_count}/{total_count}]"
            # How many chars are left for the name after the suffix and padding?
            max_name_chars = max(6, per_tab_width - len(suffix))
            if len(original_name) > max_name_chars:
                trimmed_name = original_name[:max(6, max_name_chars - 1)] + "…"
            else:
                trimmed_name = original_name
            label = f"{trimmed_name}{suffix}"
            try:
                tc.get_tab(_safe_tab_css_id(tab_id)).label = label
            except Exception:
                pass  # Tab may not exist yet during rebuild

    @work(thread=True, exit_on_error=False)
    def update_tab_titles(self) -> None:
        """Compute active session count per tab and update iTerm2 tab titles.

        Also updates the Textual TabbedContent tab labels with trimmed versions
        that fit within the available tab bar width.

        Coalesces rapid calls: if a call is already running, mark pending and return.
        The running call will re-check and apply after it finishes.
        """
        if not self._tab_original_names:
            return
        if not self._tab_title_lock.acquire(blocking=False):
            # Another worker is running — mark pending so it re-runs after finishing
            self._tab_title_pending = True
            return
        try:
            while True:
                self._tab_title_pending = False
                set_tab_titles(self._compute_tab_titles())
                self.call_from_thread(self._update_textual_tab_labels)
                if not self._tab_title_pending:
                    break
        finally:
            self._tab_title_lock.release()

    def on_resize(self) -> None:
        """Re-trim Textual tab labels when the terminal is resized."""
        self._update_textual_tab_labels()

    # ------------------------------------------------------------------
    # Per-pane pause toggle handlers
    # ------------------------------------------------------------------

    def on_session_panel_pane_toggle(self, msg: SessionPanel.PaneToggle) -> None:
        iterm_sid = msg.session_id
        if self._global_paused:
            # Exiting global manual: pause ALL except clicked
            self._global_paused = False
            self._paused_sessions = {
                p.session_id for p in self.panels.values()
                if p.session_id != iterm_sid
            }
        elif iterm_sid in self._paused_sessions:
            self._paused_sessions.discard(iterm_sid)
        else:
            self._paused_sessions.add(iterm_sid)
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()

    def on_session_panel_ask_pause_toggle(self, msg: SessionPanel.AskPauseToggle) -> None:
        iterm_sid = msg.session_id
        if iterm_sid in self._ask_paused_sessions:
            self._ask_paused_sessions.discard(iterm_sid)
        else:
            self._ask_paused_sessions.add(iterm_sid)
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()
        # Refresh the affected panel's status bar immediately
        if iterm_sid in self.panels:
            self.panels[iterm_sid]._update_status()

    # ------------------------------------------------------------------
    # Settings (override to handle iTerm scope changes)
    # ------------------------------------------------------------------

    def _on_settings_closed(self, result: "Settings | None") -> None:
        """Called when settings modal is dismissed."""
        if result is None:
            return
        old_scope = self.settings.iterm_scope
        old_oauth = self.settings.oauth_json
        self.settings = result
        self._apply_settings(result)
        if result.iterm_scope != old_scope:
            self._current_structure_fp = None  # force rebuild on next poll
            self._do_refresh()
        if result.oauth_json != old_oauth and result.oauth_json and result.account_usage:
            invalidate_usage_cache()
            self._refresh_usage()
        log.debug(f"Settings updated: {result}")

    # ------------------------------------------------------------------
    # Keybindings
    # ------------------------------------------------------------------

    def action_refresh_layout(self) -> None:
        """Manually re-fetch iTerm2 layout and rebuild."""
        bar = self.query_one("#status-bar", Horizontal)
        self.query_one("#status-left", Static).update("REFRESHING layout...")
        bar.set_classes("refreshing")
        invalidate_usage_cache()
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        """Fetch layout in a thread (can't run iterm2 sync from Textual's event loop)."""
        error = False
        try:
            tabs, self_sid, win_groups = LayoutFetcher.fetch_sync()
            tabs = filter_tabs_by_scope(
                tabs, self_sid, self.settings.iterm_scope, win_groups
            )
            if tabs:
                self.post_message(LayoutChanged(tabs, self_sid))
            else:
                error = True
        except Exception as e:
            log.debug(f"_do_refresh: error: {e}")
            error = True

        # Refresh usage data (already in a thread, token handles expiry/renewal)
        if self.settings.account_usage:
            self._last_usage_data = fetch_usage()

        def _restore() -> None:
            if error:
                try:
                    self.query_one("#status-left", Static).update(
                        "REFRESH FAILED \u2014 iTerm2 not reachable"
                    )
                    self.query_one("#status-bar", Horizontal).set_classes("paused")
                except Exception:
                    log.warning(
                        "_do_refresh: failed to update status bar with refresh failure"
                    )
            else:
                self._update_status_bar()

        self.call_from_thread(_restore)


def main() -> None:
    import sys

    simple_mode = "--simple" in sys.argv or not os.environ.get("ITERM_SESSION_ID")

    if simple_mode:
        try:
            from claude_monitor.tui_simple import SimpleTUI
        except ImportError as e:
            print(f"Error: could not import SimpleTUI: {e}")
            raise SystemExit(1)
        app = SimpleTUI()
        app.run()
        os._exit(0)

    try:
        fetch_iterm_layout()
    except ConnectionRefusedError:
        print("Error: Could not connect to iTerm2.")
        print()
        print("The Python API is probably not enabled. To fix:")
        print("  1. Open iTerm2 → Settings (⌘,)")
        print("  2. Go to General → Magic")
        print('  3. Check "Enable Python API"')
        print()
        print("Then restart claude-monitor.")
        raise SystemExit(1)
    app = AutoAcceptTUI()
    app.run()
    # Force exit — background threads (layout polling, event watcher) may be
    # blocked on I/O (iterm2 websocket, file read) and can't be interrupted
    # cleanly. The stop_event is set but threads may not see it immediately.
    os._exit(0)


if __name__ == "__main__":
    main()
