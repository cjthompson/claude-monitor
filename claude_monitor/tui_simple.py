"""Simple TUI for claude-monitor — works without iTerm2 (Linux/macOS Terminal).

Sessions are discovered reactively as hook events arrive: each unique Claude
session ID gets its own tab in a TabbedContent widget.  No iTerm2 API is used.

Layout (vertical):
  ┌─────────────────────────────┐
  │  TabbedContent (sessions)   │  1fr
  ├─────────────────────────────┤
  │  DashboardPanel             │  ~30% (or 1-line summary, or tab)
  └─────────────────────────────┘
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime
from pathlib import Path

from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.message import Message
from textual.widgets import Footer, Static, Tab, TabbedContent, TabPane
from textual.widgets._tabbed_content import ContentTab

from claude_monitor import (
    SIGNAL_DIR,
    STATE_FILE,
    LOG_FILE,
    read_state,
)
from claude_monitor.messages import HookEvent
from claude_monitor.commands import MonitorCommands
from claude_monitor.widgets import SessionPanel, DashboardPanel
from claude_monitor.screens import ChoicesScreen, QuestionsScreen, HelpScreen, PaneContextMenu  # noqa: F401
from claude_monitor.formatting import (
    _safe_css_id,
    _safe_tab_css_id,
    _format_ask_user_question_inline,
    _oneline as _oneline_fn,
    format_event as _format_event_shared,
)
from claude_monitor.app_base import MonitorApp
from claude_monitor.settings import Settings, SettingsScreen, load_settings, save_settings

os.makedirs(SIGNAL_DIR, exist_ok=True)
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.DEBUG,
    format="%(asctime)s %(message)s",
    force=True,
)
log = logging.getLogger(__name__)
for _noisy in ("websockets", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# ---------------------------------------------------------------------------
# Dashboard display mode constants
# ---------------------------------------------------------------------------

DASH_TAB = "tab"             # dashboard as a tab in TabbedContent
MIN_DASHBOARD_HEIGHT = 3     # minimum height in lines for the dashboard area


class DraggableDashboard(DashboardPanel):
    """DashboardPanel with drag-to-resize on the top border."""

    class DragDelta(Message):
        """Posted during border drag. delta is signed integer lines."""
        def __init__(self, delta: int) -> None:
            super().__init__()
            self.delta = delta

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._drag_start_y: float | None = None

    def on_mouse_down(self, event: events.MouseDown) -> None:
        if event.button == 1 and event.y == 0:
            self._drag_start_y = event.screen_y
            self.capture_mouse()
            event.stop()

    def on_mouse_move(self, event: events.MouseMove) -> None:
        if self._drag_start_y is not None:
            delta = int(event.screen_y - self._drag_start_y)
            if delta != 0:
                self._drag_start_y = event.screen_y
                self.post_message(DraggableDashboard.DragDelta(delta))
            event.stop()

    def on_mouse_up(self, event: events.MouseUp) -> None:
        if self._drag_start_y is not None:
            self._drag_start_y = None
            self.release_mouse()
            event.stop()

    def on_mouse_release(self, event: events.MouseRelease) -> None:
        self._drag_start_y = None


class SimpleTUI(MonitorApp):
    """Simple session-tabbed TUI for claude-monitor (no iTerm2 required)."""

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
    #sessions-area {
        height: 1fr;
    }
    #dashboard-area {
        height: 12;
    }
    #dashboard-area.hidden {
        display: none;
    }
    #dashboard-summary {
        height: 1;
        background: $surface;
        color: $text-muted;
        padding: 0 1;
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
    #status-left {
        width: 1fr;
        padding: 0 1;
    }
    #status-right {
        width: auto;
        padding: 0 1;
    }
    """

    TITLE = "Claude Monitor (Simple)"

    COMMANDS = {MonitorCommands}

    BINDINGS = [
        Binding("a", "toggle_pause", "Auto/Manual"),
        Binding("shift+tab", "toggle_pause", "Auto/Manual", show=False),
        Binding("c", "show_choices", "Choices", show=False),
        Binding("u", "show_questions", "Questions", show=False),
        Binding("s", "open_settings", "Settings", show=False),
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
        # Maps claude_session_id → tab pane id (for TabbedContent)
        self._claude_to_tab: dict[str, str] = {}
        self._paused_claude_sessions: set[str] = set()
        self._ask_paused_sessions: set[str] = set()
        self._dashboard_in_tab: bool = False
        self._dashboard_tab_pane_id: str | None = None
        # Counter for generating unique tab IDs
        self._tab_counter: int = 0
        # Dashboard height (lines); loaded from settings
        self._dashboard_height: int = max(MIN_DASHBOARD_HEIGHT, self.settings.dashboard_height)
        # Stored height to restore to after minimize (None = not minimized via toggle)
        self._stored_dashboard_height: int | None = None

    # ------------------------------------------------------------------
    # Abstract method implementations
    # ------------------------------------------------------------------

    def is_pane_paused(self, claude_sid: str) -> bool:
        return self._global_paused or claude_sid in self._paused_claude_sessions

    def is_ask_paused(self, claude_sid: str) -> bool:
        return claude_sid in self._ask_paused_sessions

    def _session_id_from_event(self, data: dict) -> str:
        return data.get("session_id", "")

    # ------------------------------------------------------------------
    # State persistence (override to add paused_claude_sessions)
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "global_paused": self._global_paused,
            "paused_sessions": [],  # iTerm2 UUIDs — always empty in simple mode
            "paused_claude_sessions": list(self._paused_claude_sessions),
            "excluded_tools": self.settings.excluded_tools or [],
            "ask_user_timeout": self.settings.ask_user_timeout,
            "ask_paused_sessions": list(self._ask_paused_sessions),
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    def _load_state(self) -> None:
        state = read_state()
        self._global_paused = state.get("global_paused", False)
        self._paused_claude_sessions = set(state.get("paused_claude_sessions", []))
        self._ask_paused_sessions = set(state.get("ask_paused_sessions", []))

    def _update_all_panel_modes(self) -> None:
        for panel in self.panels.values():
            if self.is_pane_paused(panel.session_id):
                panel.add_class("pane-paused")
            else:
                panel.remove_class("pane-paused")

    # ------------------------------------------------------------------
    # Settings overrides (pass simple_mode=True)
    # ------------------------------------------------------------------

    def action_open_settings(self) -> None:
        self.push_screen(SettingsScreen(self.settings, simple_mode=True), self._on_settings_closed)

    # ------------------------------------------------------------------
    # Compose + mount
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        with Horizontal(id="status-bar", classes="running"):
            yield Static("AUTO", id="status-left")
            yield Static("", id="status-right")
        with Vertical(id="layout-root"):
            with Vertical(id="sessions-area"):
                yield TabbedContent(id="tab-content")
            with Vertical(id="dashboard-area"):
                yield DraggableDashboard(id="dashboard-panel")
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_settings(self.settings)
        self.dashboard = self.query_one("#dashboard-panel", DashboardPanel)
        # Apply persisted dashboard height
        self._apply_dashboard_height()
        self._update_arrow()
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        self._load_state()
        # Apply default mode from settings
        if self.settings.default_mode == "manual":
            self._global_paused = True
        elif self.settings.default_mode == "auto":
            self._global_paused = False
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()
        # Start background workers
        self.watch_events()
        # poll_usage is started by _apply_settings if account_usage is on
        self.set_interval(1.0, self._tick_status)
        self.serve_api()

    # ------------------------------------------------------------------
    # Tick / status bar
    # ------------------------------------------------------------------

    def _tick_status(self) -> None:
        """Refresh all panel status bars and dashboard every second."""
        for panel in self.panels.values():
            panel._update_status()
        if self.dashboard:
            self.dashboard.refresh_dashboard(self.panels)
        self._update_status_bar()
        self._update_dashboard_summary()
        # Idle-timeout auto-close logic
        if self.settings.tab_close_mode == "idle":
            timeout = self.settings.tab_idle_timeout_secs
            now = time.time()
            for sid, panel in list(self.panels.items()):
                if (
                    panel._state == "idle"
                    and panel._last_event_time is not None
                    and (now - panel._last_event_time) >= timeout
                ):
                    self.call_later(self._remove_session, sid)

    # ------------------------------------------------------------------
    # Session / tab management
    # ------------------------------------------------------------------

    def _tab_id_for_session(self, claude_sid: str) -> str:
        """Return the TabPane CSS id for a Claude session."""
        return _safe_tab_css_id(claude_sid)

    async def _resolve_panel(self, data: dict) -> SessionPanel | None:
        """Return (or create) the SessionPanel for a hook event.

        Keyed off Claude session ID.  On first event from an unknown session,
        a new TabPane is created and inserted into the TabbedContent.
        """
        claude_sid = data.get("session_id", "")
        if not claude_sid:
            return None

        # Already known
        if claude_sid in self.panels:
            return self.panels[claude_sid]

        # New session — create a tab for it
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
            tab_title = f"WT:{worktree_name}"
            panel_title = f"WT:{worktree_name} [{short_id}]"
        elif cwd:
            dir_name = os.path.basename(cwd.rstrip("/"))
            tab_title = dir_name or short_id
            panel_title = f"{dir_name} [{short_id}]" if dir_name else short_id
        else:
            tab_title = short_id
            panel_title = short_id

        css_id = _safe_css_id(claude_sid)
        tab_pane_id = self._tab_id_for_session(claude_sid)

        panel = SessionPanel(claude_sid, panel_title, id=css_id)
        if worktree_name:
            panel.add_class("worktree")

        self.panels[claude_sid] = panel
        self._claude_to_tab[claude_sid] = tab_pane_id

        # Await add_pane directly — we're on the main event loop thread
        tab_pane = TabPane(tab_title, panel, id=tab_pane_id)
        try:
            tc = self.query_one("#tab-content", TabbedContent)
            await tc.add_pane(tab_pane)
            log.debug(f"_resolve_panel: added tab for session {claude_sid[:8]}")
        except Exception as e:  # Textual raises generic Exception for add_pane failures
            log.warning(f"_resolve_panel: failed to add tab pane: {e}")

        return panel

    # ------------------------------------------------------------------
    # Hook event handling
    # ------------------------------------------------------------------

    async def on_hook_event(self, msg: HookEvent) -> None:
        data = msg.data
        event_name = data.get("hook_event_name", "")
        event_ts = datetime.fromtimestamp(data.get("_timestamp", time.time()))
        t = self._format_ts(event_ts)

        panel = await self._resolve_panel(data)
        if not panel:
            return

        panel.touch()
        self._apply_event(panel, data, event_name)
        label, detail = self._format_event(data, event_name)
        if label:
            panel.write(f"[{t}] {label} {detail}")
        panel._update_status()

        # Feed to dashboard combined feed
        if self.dashboard and label:
            sid_short = panel.session_id[:8]
            self.dashboard.record_event(f"[{t}] [{sid_short}] {label} {detail}")

        self._update_tab_label(panel.session_id)

    def _apply_event(self, panel, data: dict, event_name: str) -> None:
        """Apply side effects of a hook event to a panel."""
        if event_name == "PermissionRequest":
            claude_sid = data.get("session_id", "")
            if not self.is_pane_paused(claude_sid):
                panel.accept_count += 1
                tool = data.get("tool_name", "?")
                panel.tool_counts[tool] = panel.tool_counts.get(tool, 0) + 1

        elif event_name == "Notification":
            ntype = data.get("notification_type", "")
            if ntype == "idle_prompt":
                if hasattr(panel, "mark_idle"):
                    panel.mark_idle()
                    self._update_tab_label(panel.session_id)
                if self.settings.tab_close_mode == "immediate":
                    self.call_later(self._remove_session, panel.session_id)
            elif ntype == "ask_timeout_complete":
                origin = data.get("_timeout_origin")
                if panel._pending_timeout is not None and getattr(panel, "_timeout_origin", None) == origin:
                    panel._pending_timeout = None
                    panel._timeout_origin = None
                    data["_auto_accepted"] = True
            elif ntype == "permission_prompt":
                claude_sid = data.get("session_id", "")
                if not self.is_pane_paused(claude_sid):
                    pending = getattr(panel, "_pending_timeout", None)
                    if pending and pending > time.time():
                        log.debug(f"Skipping auto-approve: AskUserQuestion timeout pending for {claude_sid[:8]}")
                    else:
                        panel.accept_count += 1

        elif event_name == "PostToolUse":
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
            get_panel=lambda d: self.panels.get(d.get("session_id", "")),
            oneline=self._oneline,
            self_sid=None,
        )

    @staticmethod
    def _oneline(text: str, max_len: int = 0) -> str:
        return _oneline_fn(text, max_len)

    # ------------------------------------------------------------------
    # Tab label management
    # ------------------------------------------------------------------

    def _update_tab_label(self, claude_sid: str) -> None:
        """Update a single tab label with active/idle indicator."""
        tab_pane_id = self._claude_to_tab.get(claude_sid)
        if not tab_pane_id:
            return
        panel = self.panels.get(claude_sid)
        if not panel:
            return
        try:
            tab_id = ContentTab.add_prefix(tab_pane_id)
            tab = self.query_one(f"#{tab_id}", Tab)
            # Build label: title + state indicator
            base = panel.border_title
            # Trim to reasonable length
            if len(base) > 20:
                base = base[:19] + "…"
            if panel._state == "active" or len(panel.active_agents) > 0:
                tab.label = f"▶ {base}"
            elif panel._state == "idle":
                tab.label = f"⏸ {base}"
            else:
                tab.label = base
        except Exception:  # Textual raises NoMatches or AttributeError for widget/attribute access failures
            log.debug(f"_update_tab_label: failed for {claude_sid[:8]}")

    # ------------------------------------------------------------------
    # Dashboard mode
    # ------------------------------------------------------------------

    def _update_dashboard_summary(self) -> None:
        """Update the dashboard summary line (both expanded and minimized states)."""
        if not self.dashboard:
            return
        try:
            summary = self.dashboard.query_one("#dashboard-summary", Static)
            if self.panels:
                summary.update(self.dashboard._render_stats(self.panels))
            else:
                summary.update("[dim]No sessions yet — waiting for Claude Code events…[/]")
        except NoMatches:  # Widget not yet mounted
            pass

    def _update_arrow(self) -> None:
        """Set the border_subtitle arrow based on current height and stored height."""
        if not self.dashboard:
            return
        at_min = self._dashboard_height <= MIN_DASHBOARD_HEIGHT
        has_stored = self._stored_dashboard_height is not None
        if at_min and has_stored:
            # Can restore — show ▲
            self.dashboard.border_subtitle = "[@click=app.toggle_dashboard]▲[/]"
        else:
            # Can minimize (or already at min with no stored) — show ▼
            self.dashboard.border_subtitle = "[@click=app.toggle_dashboard]▼[/]"

    def _apply_dashboard_height(self) -> None:
        """Set #dashboard-area CSS height to _dashboard_height."""
        try:
            dash_area = self.query_one("#dashboard-area")
            dash_area.styles.height = self._dashboard_height
        except NoMatches as e:
            log.debug(f"_apply_dashboard_height: {e}")

    def action_toggle_dashboard(self) -> None:
        """Toggle dashboard: minimize to MIN or restore to stored height."""
        at_min = self._dashboard_height <= MIN_DASHBOARD_HEIGHT
        has_stored = self._stored_dashboard_height is not None
        if at_min and has_stored:
            # Restore to stored height
            self._dashboard_height = self._stored_dashboard_height
            self._stored_dashboard_height = None
        elif not at_min:
            # Minimize — store current height for restore
            self._stored_dashboard_height = self._dashboard_height
            self._dashboard_height = MIN_DASHBOARD_HEIGHT
        # If at_min and no stored height: no-op (already at minimum)
        self._apply_dashboard_height()
        self._update_arrow()

    def action_grow_dashboard(self) -> None:
        """Grow the dashboard pane by 1 line."""
        if self._dashboard_in_tab:
            return
        try:
            # Cap: sessions area must retain at least 4 visible lines after grow
            sessions_area = self.query_one("#sessions-area")
            sessions_h = sessions_area.size.height
            if sessions_h <= 4:
                return
            self._dashboard_height += 1
            self._stored_dashboard_height = None
            self._apply_dashboard_height()
            self.settings.dashboard_height = self._dashboard_height
            save_settings(self.settings)
            self._update_arrow()
        except NoMatches as e:
            log.debug(f"action_grow_dashboard: {e}")

    def action_shrink_dashboard(self) -> None:
        """Shrink the dashboard pane by 1 line."""
        if self._dashboard_in_tab:
            return
        if self._dashboard_height <= MIN_DASHBOARD_HEIGHT:
            return
        try:
            self._dashboard_height -= 1
            self._stored_dashboard_height = None
            self._apply_dashboard_height()
            self.settings.dashboard_height = self._dashboard_height
            save_settings(self.settings)
            self._update_arrow()
        except NoMatches as e:
            log.debug(f"action_shrink_dashboard: {e}")

    def on_draggable_dashboard_drag_delta(self, msg: DraggableDashboard.DragDelta) -> None:
        """Handle drag-to-resize from the DragHandle splitter."""
        if self._dashboard_in_tab:
            return
        # Positive delta = dragged down = dashboard shrinks
        # Negative delta = dragged up = dashboard grows
        new_height = self._dashboard_height - msg.delta
        # Clamp: minimum dashboard height
        new_height = max(MIN_DASHBOARD_HEIGHT, new_height)
        # Clamp: sessions area must retain at least 4 visible lines
        try:
            sessions_area = self.query_one("#sessions-area")
            sessions_h = sessions_area.size.height
            total_available = sessions_h + self._dashboard_height
            max_dashboard = total_available - 4
            new_height = min(new_height, max_dashboard)
        except NoMatches as e:
            log.debug(f"on_drag_handle_drag_delta: {e}")
        if new_height == self._dashboard_height:
            return
        self._dashboard_height = new_height
        self._stored_dashboard_height = None
        self._apply_dashboard_height()
        self.settings.dashboard_height = self._dashboard_height
        save_settings(self.settings)
        self._update_arrow()

    async def action_toggle_dashboard_tab(self) -> None:
        """Toggle dashboard between bottom panel and a tab in TabbedContent."""
        try:
            if not self._dashboard_in_tab:
                # Any state → tab mode
                self._dashboard_in_tab = True

                # Hide dashboard area
                dash_area = self.query_one("#dashboard-area")
                dash_area.add_class("hidden")

                # Create a new DashboardPanel in a tab
                tab_pane_id = "tab-dashboard"
                new_dash = DashboardPanel(id="dashboard-tab-panel")
                # Transfer state from current dashboard
                if self.dashboard:
                    new_dash._start_time = self.dashboard._start_time
                    new_dash.active_agents = dict(self.dashboard.active_agents)
                    new_dash.total_agents_completed = self.dashboard.total_agents_completed
                    new_dash.accept_count = self.dashboard.accept_count
                    new_dash._event_log = list(self.dashboard._event_log)

                tab_pane = TabPane("Dashboard", new_dash, id=tab_pane_id)
                tc = self.query_one("#tab-content", TabbedContent)
                # Insert Dashboard as the first (left-most) tab
                existing_panes = tc.query(TabPane)
                first_pane_id = existing_panes.first(TabPane).id if existing_panes else None
                if first_pane_id:
                    await tc.add_pane(tab_pane, before=first_pane_id)
                else:
                    await tc.add_pane(tab_pane)
                tc.active = tab_pane_id
                self.dashboard = new_dash
                self._dashboard_tab_pane_id = tab_pane_id
                # Replay event log into the newly mounted RichLog
                if new_dash._event_log:
                    try:
                        from textual.widgets import RichLog
                        rl = new_dash.query_one(RichLog)
                        for line in new_dash._event_log:
                            rl.write(line)
                    except Exception:
                        log.debug("action_toggle_dashboard_tab: failed to replay event log into tab dashboard")
            else:
                # Tab mode → restore bottom panel
                self._dashboard_in_tab = False

                # Capture log from tab dashboard before removing the pane
                tab_event_log: list[str] = []
                if self.dashboard and self.dashboard._event_log:
                    tab_event_log = list(self.dashboard._event_log)

                # Remove the dashboard tab
                if self._dashboard_tab_pane_id:
                    tc = self.query_one("#tab-content", TabbedContent)
                    await tc.remove_pane(self._dashboard_tab_pane_id)
                    self._dashboard_tab_pane_id = None

                # Restore bottom dashboard
                dash_area = self.query_one("#dashboard-area")
                dash_area.remove_class("hidden")
                self.dashboard = self.query_one("#dashboard-panel", DashboardPanel)

                # Transfer log from tab dashboard and replay into pane dashboard's RichLog
                if tab_event_log:
                    self.dashboard._event_log = tab_event_log
                    try:
                        from textual.widgets import RichLog
                        rl = self.dashboard.query_one(RichLog)
                        rl.clear()
                        for line in tab_event_log:
                            rl.write(line)
                    except Exception:
                        log.debug("action_toggle_dashboard_tab: failed to replay event log into pane dashboard")

                # Apply current height and arrow
                self._apply_dashboard_height()
                self._update_arrow()
        except Exception as e:  # Textual raises generic Exception for add_pane/remove_pane failures
            log.debug(f"action_toggle_dashboard_tab: {e}")

    # ------------------------------------------------------------------
    # Per-pane pause toggle
    # ------------------------------------------------------------------

    def on_session_panel_pane_toggle(self, msg: SessionPanel.PaneToggle) -> None:
        claude_sid = msg.session_id
        if self._global_paused:
            # Exit global manual: pause all except clicked
            self._global_paused = False
            self._paused_claude_sessions = {
                sid for sid in self.panels if sid != claude_sid
            }
        elif claude_sid in self._paused_claude_sessions:
            self._paused_claude_sessions.discard(claude_sid)
        else:
            self._paused_claude_sessions.add(claude_sid)
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()

    def on_session_panel_ask_pause_toggle(self, msg: SessionPanel.AskPauseToggle) -> None:
        claude_sid = msg.session_id
        if claude_sid in self._ask_paused_sessions:
            self._ask_paused_sessions.discard(claude_sid)
        else:
            self._ask_paused_sessions.add(claude_sid)
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()

    def action_toggle_pause(self) -> None:
        if self._global_paused or self._paused_claude_sessions:
            self._global_paused = False
            self._paused_claude_sessions.clear()
        else:
            self._global_paused = True
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()

    # ------------------------------------------------------------------
    # Tab management
    # ------------------------------------------------------------------

    async def action_close_tab(self) -> None:
        """Remove the currently active session tab."""
        try:
            tc = self.query_one("#tab-content", TabbedContent)
            active_pane_id = tc.active
            if not active_pane_id:
                return
            # Don't close dashboard tab
            if active_pane_id == self._dashboard_tab_pane_id:
                return
            # Find the claude session ID for this tab
            claude_sid = None
            for sid, tab_id in self._claude_to_tab.items():
                if tab_id == active_pane_id:
                    claude_sid = sid
                    break
            if claude_sid:
                await self._remove_session(claude_sid)
        except Exception as e:  # Textual raises generic Exception for tab query/remove failures
            log.debug(f"action_close_tab: {e}")

    async def _remove_session(self, claude_sid: str) -> None:
        """Remove a session's tab, panel, and pause state."""
        tab_pane_id = self._claude_to_tab.get(claude_sid)
        if not tab_pane_id:
            return
        try:
            tc = self.query_one("#tab-content", TabbedContent)
            await tc.remove_pane(tab_pane_id)
        except Exception as e:  # Textual raises generic Exception for remove_pane failures
            log.debug(f"_remove_session: failed to remove pane: {e}")
        self.panels.pop(claude_sid, None)
        self._claude_to_tab.pop(claude_sid, None)
        self._paused_claude_sessions.discard(claude_sid)
        log.debug(f"_remove_session: removed session {claude_sid[:8]}")


def main():
    app = SimpleTUI()
    app.run()
    os._exit(0)
