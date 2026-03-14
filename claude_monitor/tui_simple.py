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

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.reactive import reactive
from textual.widgets import Footer, RichLog, Static, Tab, TabbedContent, TabPane
from textual.widgets._tabbed_content import ContentTab

from claude_monitor import (
    __version__,
    SIGNAL_DIR,
    EVENTS_FILE,
    STATE_FILE,
    LOG_FILE,
    API_PORT_FILE,
    fmt_duration,
    read_state,
)
from claude_monitor.tui_common import (
    HookEvent,
    HorizontalScrollBarRender,
    VerticalScrollBarRender,
    SessionPanel,
    DashboardPanel,
    PaneContextMenu,
    ChoicesScreen,
    QuestionsScreen,
    MonitorCommands,
    _safe_css_id,
    _safe_tab_css_id,
    _format_ask_user_question_inline,
    _oneline as _oneline_fn,
)
from claude_monitor.api import start_api_server
from claude_monitor.settings import Settings, SettingsScreen, load_settings, save_settings
from claude_monitor.usage import (
    fetch_usage,
    format_usage_inline,
    invalidate_usage_cache,
    set_oauth_json,
    set_on_token_refreshed,
)

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

DASH_EXPANDED = "expanded"   # full dashboard panel below sessions (~30%)
DASH_MINIMIZED = "minimized" # single-line summary bar
DASH_TAB = "tab"             # dashboard as a tab in TabbedContent


class SimpleTUI(App):
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
    #dashboard-area.minimized {
        height: 3;
    }
    DashboardPanel.minimized .dash-stats,
    DashboardPanel.minimized .dash-sparkline,
    DashboardPanel.minimized RichLog {
        display: none;
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
        ("a", "toggle_pause", "Auto/Manual"),
        ("shift+tab", "toggle_pause", "Auto/Manual"),
        ("c", "show_choices", "Choices"),
        ("u", "show_questions", "Questions"),
        ("s", "open_settings", "Settings"),
        ("d", "toggle_dashboard", "Dashboard"),
        ("D", "toggle_dashboard_tab", "Dashboard Tab"),
        ("equals_sign", "grow_dashboard", "Dash+"),
        ("hyphen-minus", "shrink_dashboard", "Dash-"),
        ("right_square_bracket", "next_tab", "Next Tab"),
        ("left_square_bracket", "prev_tab", "Prev Tab"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        # panels keyed by Claude session ID
        self.panels: dict[str, SessionPanel] = {}
        self.dashboard: DashboardPanel | None = None
        # Maps claude_session_id → tab pane id (for TabbedContent)
        self._claude_to_tab: dict[str, str] = {}
        self._stop_event = threading.Event()
        self._global_paused: bool = False
        self._paused_claude_sessions: set[str] = set()
        self._dashboard_mode: str = DASH_EXPANDED
        self._pre_tab_dashboard_mode: str = DASH_EXPANDED
        self._dashboard_tab_pane_id: str | None = None
        self._usage_polling = False
        self._last_usage_data = None
        self._usage_next_fetch: float = 0
        self._api_server = None
        # Counter for generating unique tab IDs
        self._tab_counter: int = 0
        # Dashboard expanded height (lines); loaded from settings, persists across minimize toggles
        self._dashboard_height: int = max(3, self.settings.dashboard_height)

    # ------------------------------------------------------------------
    # Pause state helpers (use Claude session IDs, not iTerm2 UUIDs)
    # ------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        return self._global_paused

    def is_pane_paused(self, claude_sid: str) -> bool:
        return self._global_paused or claude_sid in self._paused_claude_sessions

    def is_ask_paused(self, claude_sid: str) -> bool:
        # Ask-pause is tracked per-panel in the full version; simple version
        # doesn't implement it — always return False.
        return False

    def get_state_snapshot(self) -> dict:
        """Return serializable TUI state for the HTTP API /text endpoint."""
        sessions = []
        for sid, panel in self.panels.items():
            sessions.append({
                "id": sid,
                "title": panel.border_title,
                "state": panel.state,
                "mode": "manual" if self.is_pane_paused(sid) else "auto",
                "active_agents": len(panel.active_agents),
                "completed_agents": panel.total_agents_completed,
                "accept_count": panel.accept_count,
            })

        dashboard_data = None
        if self.dashboard:
            d = self.dashboard
            total_accepted = sum(p.accept_count for p in self.panels.values()) + d.accept_count
            total_agents_active = (
                sum(len(p.active_agents) for p in self.panels.values()) + len(d.active_agents)
            )
            total_agents_done = (
                sum(p.total_agents_completed for p in self.panels.values())
                + d.total_agents_completed
            )
            active_sessions = sum(1 for p in self.panels.values() if p.state == "active")
            idle_sessions = sum(1 for p in self.panels.values() if p.state == "idle")
            dashboard_data = {
                "total_accepted": total_accepted,
                "total_agents_active": total_agents_active,
                "total_agents_completed": total_agents_done,
                "active_sessions": active_sessions,
                "idle_sessions": idle_sessions,
            }

        usage_data = None
        if self._last_usage_data:
            u = self._last_usage_data
            usage_data = {
                "five_hour": {
                    "utilization": u.five_hour.utilization,
                    "resets_at": u.five_hour.resets_at.isoformat() if u.five_hour.resets_at else None,
                },
                "seven_day": {
                    "utilization": u.seven_day.utilization,
                    "resets_at": u.seven_day.resets_at.isoformat() if u.seven_day.resets_at else None,
                },
            }

        return {
            "global_mode": "manual" if self._global_paused else "auto",
            "sessions": sessions,
            "dashboard": dashboard_data,
            "usage": usage_data,
        }

    # ------------------------------------------------------------------
    # State persistence
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        state = {
            "global_paused": self._global_paused,
            "paused_sessions": [],  # iTerm2 UUIDs — always empty in simple mode
            "paused_claude_sessions": list(self._paused_claude_sessions),
            "excluded_tools": self.settings.excluded_tools or [],
            "ask_user_timeout": self.settings.ask_user_timeout,
            "ask_paused_sessions": [],
        }
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)

    def _load_state(self) -> None:
        state = read_state()
        self._global_paused = state.get("global_paused", False)
        self._paused_claude_sessions = set(state.get("paused_claude_sessions", []))

    def _update_all_panel_modes(self) -> None:
        for panel in self.panels.values():
            if self.is_pane_paused(panel.session_id):
                panel.add_class("pane-paused")
            else:
                panel.remove_class("pane-paused")

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
                yield DashboardPanel(id="dashboard-panel")
        yield Footer()

    async def on_mount(self) -> None:
        self._apply_settings(self.settings)
        self.dashboard = self.query_one("#dashboard-panel", DashboardPanel)
        # Apply persisted dashboard height
        self._apply_dashboard_height()
        self._update_dashboard_subtitle()
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
        except Exception as e:
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
        """Format a hook event into display text. Returns (label, detail) or (None, None)."""
        if event_name == "PermissionRequest":
            tool = data.get("tool_name", "?")
            tool_input = data.get("tool_input", {})
            detail = ""
            if tool == "AskUserQuestion":
                detail = _format_ask_user_question_inline(tool_input)
            elif tool == "Bash":
                detail = f" `{self._oneline(tool_input.get('command', ''))}`"
            elif tool in ("Edit", "Write"):
                detail = f" `{tool_input.get('file_path', '')}`"
            elif tool == "WebFetch":
                detail = f" `{tool_input.get('url', '')}`"
            if data.get("_excluded_tool"):
                return f"[bold red]{'MANUAL':<8}[/]", f"{tool}{detail}"
            decision = data.get("_decision", "allowed")
            if decision == "deferred":
                return f"[bold yellow]{'DEFERRED':<8}[/]", f"{tool}{detail}"
            if decision == "timeout":
                timeout_s = data.get("_ask_timeout", "?")
                return f"[bold cyan]{'TIMEOUT':<8}[/]", f"{tool}{detail} ({timeout_s}s)"
            claude_sid = data.get("session_id", "")
            if self.is_pane_paused(claude_sid):
                return f"[bold yellow]{'PAUSED':<8}[/]", f"{tool}{detail}"
            return f"[bold green]{'ALLOWED':<8}[/]", f"{tool}{detail}"

        elif event_name == "PostToolUse":
            tool = data.get("tool_name", "?")
            if tool == "AskUserQuestion":
                answers = data.get("tool_input", {}).get("answers", {})
                answer_vals = [v for v in answers.values() if v]
                if not answer_vals:
                    return None, None
                answer_text = ", ".join(answer_vals)
                return f"[bold green]{'ANSWER':<8}[/]", f"AskUserQuestion -> [bold]{answer_text}[/]"
            return None, None

        elif event_name == "Notification":
            ntype = data.get("notification_type", "")
            message = data.get("message", "")
            if ntype == "idle_prompt":
                return f"[dim]{'IDLE':<8}[/]", self._oneline(message, 80)
            elif ntype == "ask_timeout_complete":
                if data.get("_auto_accepted"):
                    return f"[bold cyan]{'AUTO':<8}[/]", message
                return None, None
            elif ntype == "permission_prompt":
                claude_sid = data.get("session_id", "")
                if not self.is_pane_paused(claude_sid):
                    panel = self.panels.get(claude_sid)
                    pending = getattr(panel, "_pending_timeout", None) if panel else None
                    if pending and pending > time.time():
                        return None, None
                    return f"[bold green]{'APPROVED':<8}[/]", message
            return f"[bold cyan]{'NOTIFY':<8}[/]", self._oneline(message, 80)

        elif event_name == "SubagentStart":
            agent_id = data.get("agent_id", "?")
            agent_type = data.get("agent_type", "?")
            return f"[bold magenta]{'AGENT+':<8}[/]", f"{agent_type} [{agent_id[:8]}]"

        elif event_name == "SubagentStop":
            agent_id = data.get("agent_id", "?")
            agent_type = data.get("agent_type", "?")
            return f"[magenta]{'AGENT-':<8}[/]", f"{agent_type} [{agent_id[:8]}]"

        return None, None

    def _format_ts(self, ts: datetime) -> str:
        style = self.settings.timestamp_style
        if style == "12hr":
            return ts.strftime("%-I:%M:%S%p").lower()
        if style == "date_time":
            return ts.strftime("%Y-%m-%d %H:%M:%S")
        return ts.strftime("%H:%M:%S")

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
        except Exception:
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
        except Exception:
            pass

    def _update_dashboard_subtitle(self) -> None:
        """Set the border_subtitle glyph based on dashboard mode."""
        if not self.dashboard:
            return
        if self._dashboard_mode == DASH_EXPANDED:
            self.dashboard.border_subtitle = "[@click=app.toggle_dashboard]▼[/]"
        elif self._dashboard_mode == DASH_MINIMIZED:
            self.dashboard.border_subtitle = "[@click=app.toggle_dashboard]▲[/]"
        # In tab mode the bottom dashboard is hidden, no subtitle needed

    def _apply_dashboard_height(self) -> None:
        """Set #dashboard-area CSS height to _dashboard_height (only when expanded)."""
        if self._dashboard_mode != DASH_EXPANDED:
            return
        try:
            dash_area = self.query_one("#dashboard-area")
            dash_area.styles.height = self._dashboard_height
        except Exception as e:
            log.debug(f"_apply_dashboard_height: {e}")

    def action_toggle_dashboard(self) -> None:
        """Cycle dashboard mode: expanded → minimized → expanded."""
        try:
            dash_area = self.query_one("#dashboard-area")
            if self._dashboard_mode == DASH_EXPANDED:
                self._dashboard_mode = DASH_MINIMIZED
                # Clear inline height so CSS class rule (.minimized → height: 3) takes effect
                dash_area.styles.height = None
                dash_area.add_class("minimized")
                try:
                    panel = self.query_one("#dashboard-panel", DashboardPanel)
                    panel.add_class("minimized")
                except Exception:
                    pass
            else:
                self._dashboard_mode = DASH_EXPANDED
                dash_area.remove_class("minimized")
                try:
                    panel = self.query_one("#dashboard-panel", DashboardPanel)
                    panel.remove_class("minimized")
                except Exception:
                    pass
                # Re-apply stored expanded height as inline style
                self._apply_dashboard_height()
            self._update_dashboard_subtitle()
        except Exception as e:
            log.debug(f"action_toggle_dashboard: {e}")

    def action_grow_dashboard(self) -> None:
        """Grow the dashboard pane by 1 line (only when expanded)."""
        if self._dashboard_mode != DASH_EXPANDED:
            return
        try:
            # Cap: sessions area must retain at least 4 visible lines after grow
            sessions_area = self.query_one("#sessions-area")
            sessions_h = sessions_area.size.height
            if sessions_h <= 4:
                return
            self._dashboard_height += 1
            self._apply_dashboard_height()
            self.settings.dashboard_height = self._dashboard_height
            save_settings(self.settings)
        except Exception as e:
            log.debug(f"action_grow_dashboard: {e}")

    def action_shrink_dashboard(self) -> None:
        """Shrink the dashboard pane by 1 line (only when expanded)."""
        if self._dashboard_mode != DASH_EXPANDED:
            return
        if self._dashboard_height <= 3:
            return
        try:
            self._dashboard_height -= 1
            self._apply_dashboard_height()
            self.settings.dashboard_height = self._dashboard_height
            save_settings(self.settings)
        except Exception as e:
            log.debug(f"action_shrink_dashboard: {e}")

    async def action_toggle_dashboard_tab(self) -> None:
        """Toggle dashboard between bottom panel and a tab in TabbedContent."""
        try:
            if self._dashboard_mode != DASH_TAB:
                # Any state → tab mode
                self._pre_tab_dashboard_mode = self._dashboard_mode
                self._dashboard_mode = DASH_TAB

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
                await tc.add_pane(tab_pane)
                tc.active = tab_pane_id
                self.dashboard = new_dash
                self._dashboard_tab_pane_id = tab_pane_id
            else:
                # Tab mode → previous state
                self._dashboard_mode = self._pre_tab_dashboard_mode

                # Remove the dashboard tab
                if self._dashboard_tab_pane_id:
                    tc = self.query_one("#tab-content", TabbedContent)
                    await tc.remove_pane(self._dashboard_tab_pane_id)
                    self._dashboard_tab_pane_id = None

                # Restore bottom dashboard
                dash_area = self.query_one("#dashboard-area")
                dash_area.remove_class("hidden")
                self.dashboard = self.query_one("#dashboard-panel", DashboardPanel)

                # Restore expanded vs minimized
                if self._dashboard_mode == DASH_MINIMIZED:
                    dash_area.add_class("minimized")
                    self.dashboard.display = False
                else:
                    dash_area.remove_class("minimized")
                    self.dashboard.display = True
                self._update_dashboard_subtitle()
        except Exception as e:
            log.debug(f"action_toggle_dashboard_tab: {e}")

    # ------------------------------------------------------------------
    # Keybindings
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

    def action_toggle_pause(self) -> None:
        if self._global_paused or self._paused_claude_sessions:
            self._global_paused = False
            self._paused_claude_sessions.clear()
        else:
            self._global_paused = True
        self._save_state()
        self._update_all_panel_modes()
        self._update_status_bar()

    def action_show_choices(self) -> None:
        self.push_screen(ChoicesScreen())

    def action_show_questions(self) -> None:
        self.push_screen(QuestionsScreen())

    def action_next_tab(self) -> None:
        try:
            tc = self.query_one("#tab-content", TabbedContent)
            pane_ids = [pane.id for pane in tc.query(TabPane) if pane.id]
            if not pane_ids or not tc.active:
                return
            idx = pane_ids.index(tc.active)
            tc.active = pane_ids[(idx + 1) % len(pane_ids)]
        except Exception:
            pass

    def action_prev_tab(self) -> None:
        try:
            tc = self.query_one("#tab-content", TabbedContent)
            pane_ids = [pane.id for pane in tc.query(TabPane) if pane.id]
            if not pane_ids or not tc.active:
                return
            idx = pane_ids.index(tc.active)
            tc.active = pane_ids[(idx - 1) % len(pane_ids)]
        except Exception:
            pass

    def action_open_settings(self) -> None:
        self.push_screen(SettingsScreen(self.settings, simple_mode=True), self._on_settings_closed)

    def _on_settings_closed(self, result: Settings | None) -> None:
        if result is None:
            return
        old_oauth = self.settings.oauth_json
        self.settings = result
        self._apply_settings(result)
        if result.oauth_json != old_oauth and result.oauth_json and result.account_usage:
            invalidate_usage_cache()
            self._refresh_usage()
        log.debug(f"Settings updated: {result}")

    def _apply_settings(self, settings: Settings) -> None:
        self.theme = settings.theme
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if settings.debug else logging.WARNING)
        set_oauth_json(settings.oauth_json)
        set_on_token_refreshed(self._on_token_refreshed)
        if settings.account_usage and not self._usage_polling:
            self._usage_polling = True
            self.poll_usage()
        if not settings.account_usage and self._last_usage_data:
            self._last_usage_data = None
            self._update_status_bar()
        self._save_state()

    def _on_token_refreshed(self, token: str, refresh_token: str, expires_at: float) -> None:
        if self.settings.oauth_json:
            oauth_data = {
                "access_token": token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            }
            self.settings.oauth_json = json.dumps(oauth_data)
            save_settings(self.settings)
            set_oauth_json(self.settings.oauth_json)
        ts = self._format_ts(datetime.now().astimezone())
        expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc).astimezone()
        msg = f"[{ts}] [dim]OAuth token refreshed, expires {expires_dt.strftime('%H:%M:%S')}[/]"

        def _log():
            if self.dashboard:
                self.dashboard.record_event(msg)

        self.call_from_thread(_log)

    def _update_status_bar(self) -> None:
        try:
            bar = self.query_one("#status-bar", Horizontal)
            left = self.query_one("#status-left", Static)
            right = self.query_one("#status-right", Static)
            SEP = "  [dim]\u2502[/]  "

            n_paused = sum(1 for sid in self.panels if self.is_pane_paused(sid))
            if self.paused:
                mode_text = "[bold]MANUAL[/]"
                bar.set_classes("paused")
                usage_mode = "paused"
            elif n_paused == 0:
                mode_text = "[bold] AUTO [/]"
                bar.set_classes("running")
                usage_mode = "running"
            else:
                n_total = len(self.panels)
                mode_text = f"[bold]MIXED [/] [dim]{n_total - n_paused}a {n_paused}m[/]"
                bar.set_classes("paused")
                usage_mode = "paused"

            left_parts = [mode_text]
            if self._last_usage_data:
                bar_width = (bar.size.width if bar.size.width > 0 else 120) - 40
                left_parts.append(format_usage_inline(self._last_usage_data, bar_width, usage_mode))
            elif self.settings.account_usage:
                if self._usage_next_fetch > 0:
                    next_dt = datetime.fromtimestamp(self._usage_next_fetch)
                    next_str = next_dt.strftime("%-I:%M%p").lower()
                    left_parts.append(f"[dim]usage: updating at {next_str}[/]")
                else:
                    left_parts.append("[dim]usage: waiting…[/]")
            left.update(SEP.join(left_parts))

            clock = datetime.now().strftime("%-b %-d %-I:%M%p").replace("AM", "am").replace("PM", "pm")
            right.update(f"[dim]v{__version__}[/]{SEP}{clock}")
        except Exception:
            log.debug("_update_status_bar: failed to update status bar widgets")

    def action_quit(self) -> None:
        self._stop_event.set()
        self.exit()

    def _on_exit_app(self) -> None:
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background workers
    # ------------------------------------------------------------------

    @work(thread=True, exit_on_error=False)
    def watch_events(self) -> None:
        """Tail events.jsonl and post HookEvent messages to the app."""
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        Path(EVENTS_FILE).touch(exist_ok=True)

        with open(EVENTS_FILE, "r") as f:
            f.seek(0, 2)  # seek to end — only process new events
            while not self._stop_event.is_set():
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            self.post_message(HookEvent(data))
                        except json.JSONDecodeError:
                            log.debug(f"watch_events: failed to parse JSON: {line[:100]}")
                else:
                    self._stop_event.wait(0.2)
        log.debug("watch_events: stopped")

    @work(thread=True, exit_on_error=False)
    def poll_usage(self) -> None:
        """Poll usage every 5 minutes."""
        log.debug("poll_usage: started")
        while not self._stop_event.is_set():
            if not self.settings.account_usage:
                self._usage_polling = False
                break
            self._last_usage_data = fetch_usage()
            self._usage_next_fetch = time.time() + 300
            self.call_from_thread(self._update_status_bar)
            self._stop_event.wait(300)
        log.debug("poll_usage: stopped")

    @work(thread=True, exit_on_error=False)
    def _refresh_usage(self) -> None:
        self._last_usage_data = fetch_usage()
        self._usage_next_fetch = time.time() + 300
        self.call_from_thread(self._update_status_bar)

    @work(thread=True, exit_on_error=False)
    def serve_api(self) -> None:
        """Run the HTTP API server in a background thread."""
        try:
            self._api_server = start_api_server(self)
            log.debug("serve_api: started")
            while not self._stop_event.is_set():
                self._api_server.handle_request()
        except OSError as e:
            log.error(f"serve_api: failed to start: {e}")
        finally:
            if self._api_server:
                self._api_server.server_close()
            try:
                os.remove(API_PORT_FILE)
            except OSError:
                log.debug("serve_api: failed to remove API port file")
            log.debug("serve_api: stopped")


def main():
    app = SimpleTUI()
    app.run()
    os._exit(0)
