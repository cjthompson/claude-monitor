#!/usr/bin/env python3
"""Textual TUI for Claude Code auto-accept.

Watches events logged by auto-accept-hook.py and displays them per session.
Uses iTerm2 API to discover pane layout and session names before startup.
Polls iTerm2 every few seconds to detect pane splits/closes.
"""

import collections
import json
import logging
import os
import threading
import time
from datetime import datetime

import iterm2
from iterm2.session import Splitter, Session
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, Header, RichLog, Sparkline, Static

LOG_FILE = "/tmp/claude-auto-accept/tui-debug.log"
logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG, format="%(asctime)s %(message)s", force=True)
log = logging.getLogger(__name__)


SIGNAL_DIR = "/tmp/claude-auto-accept"
EVENTS_FILE = os.path.join(SIGNAL_DIR, "events.jsonl")
PAUSE_FILE = os.path.join(SIGNAL_DIR, "paused")


# --- iTerm2 layout helpers ---

def _fetch_layout_sync():
    """Fetch iTerm2 pane layout synchronously. Returns (tree, self_session_id).

    Each call creates a Connection + event loop. We close the loop after
    to avoid leaking file descriptors.
    """
    result = [None]

    async def _fetch(connection):
        app = await iterm2.async_get_app(connection)
        window = app.current_terminal_window
        if window and window.current_tab:
            result[0] = window.current_tab.root

    conn = iterm2.Connection()
    conn.run_until_complete(_fetch, retry=False)
    if conn.loop:
        conn.loop.close()

    # Use ITERM_SESSION_ID env var to identify the TUI's own pane.
    # Format is "w0t0p2:UUID" — the UUID matches iTerm2 API session IDs.
    raw = os.environ.get("ITERM_SESSION_ID", "")
    self_sid = raw.split(":", 1)[1] if ":" in raw else raw

    return result[0], self_sid


def _collect_session_ids(node):
    """Extract all session IDs from an iTerm2 Splitter tree."""
    if isinstance(node, Session):
        return {node.session_id}
    elif isinstance(node, Splitter):
        ids = set()
        for child in node.children:
            ids |= _collect_session_ids(child)
        return ids
    return set()


def _layout_fingerprint(node):
    """Return a hashable fingerprint of the layout including frame sizes.

    This changes when panes are added/removed OR resized.
    """
    if isinstance(node, Session):
        w, h = _get_frame_size(node)
        return (node.session_id, int(w), int(h))
    elif isinstance(node, Splitter):
        children = tuple(_layout_fingerprint(c) for c in node.children)
        return ("split", node.vertical, children)
    return ()


# Fetch initial layout before Textual starts
_layout_tree = None
_self_session_id = None


def fetch_iterm_layout():
    global _layout_tree, _self_session_id
    _layout_tree, _self_session_id = _fetch_layout_sync()
    log.debug(f"fetch_iterm_layout done: tree={type(_layout_tree).__name__}, self={_self_session_id}")


# --- Textual widgets ---

class HookEvent(Message):
    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data


class LayoutChanged(Message):
    """Posted when iTerm2 layout has changed (panes added/removed/rearranged)."""
    def __init__(self, tree, self_session_id: str) -> None:
        super().__init__()
        self.tree = tree
        self.self_session_id = self_session_id


class SessionPanel(Static):
    """A bordered panel showing events for one session."""

    DEFAULT_CSS = """
    SessionPanel {
        border: solid $accent;
        height: 1fr;
        width: 1fr;
        padding: 0 1;
    }
    SessionPanel RichLog {
        height: 1fr;
    }
    SessionPanel .panel-status {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
    }
    """

    def __init__(self, session_id: str, title: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session_id = session_id
        self.border_title = title
        self.active_agents: dict[str, str] = {}  # agent_id → agent_type
        self.accept_count = 0
        self.total_agents_completed = 0
        self._start_time = time.time()
        self._last_event_time: float | None = None
        self._state = "waiting"  # waiting, active, idle
        self._event_log: list[str] = []  # stored for replay after rebuild

    def compose(self) -> ComposeResult:
        yield RichLog(markup=True, wrap=True)
        yield Static(self._render_status(), classes="panel-status")

    def write(self, text: str) -> None:
        self._event_log.append(text)
        try:
            self.query_one(RichLog).write(text)
        except Exception:
            pass

    def touch(self) -> None:
        """Mark this panel as having received activity."""
        self._last_event_time = time.time()
        self._state = "active"

    def mark_idle(self) -> None:
        self._state = "idle"

    def _fmt_duration(self, seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m{s % 60:02d}s"
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h{m:02d}m"

    def _render_status(self) -> str:
        SEP = " [dim]│[/] "

        # State indicator
        if self._state == "active":
            state = "[bold green]▶ active[/]"
        elif self._state == "idle":
            state = "[yellow]⏸ idle[/]"
        else:
            state = "[dim]◦ waiting[/]"

        # Agents
        n = len(self.active_agents)
        if n:
            blocks = "█" * min(n, 8)
            types = {}
            for atype in self.active_agents.values():
                types[atype] = types.get(atype, 0) + 1
            detail = " ".join(f"{t}:{c}" for t, c in sorted(types.items()))
            agents = f"[bold magenta]{blocks}[/] {n} ({detail})"
        else:
            agents = "[dim]── none[/]"

        # Completed agents
        done = f"{self.total_agents_completed}"

        # Accepted
        accepted = f"{self.accept_count}"

        # Uptime
        uptime = self._fmt_duration(time.time() - self._start_time)

        return f"{state}{SEP}Agents: {agents}{SEP}Done: {done}{SEP}Accepted: {accepted}{SEP}{uptime}"

    def _update_status(self) -> None:
        try:
            self.query_one(".panel-status", Static).update(self._render_status())
        except Exception:
            pass


class DashboardPanel(Static):
    """Aggregate dashboard shown in the TUI's own pane."""

    DEFAULT_CSS = """
    DashboardPanel {
        border: solid $primary;
        height: 1fr;
        width: 1fr;
        padding: 0 1;
    }
    DashboardPanel .dash-stats {
        height: 3;
    }
    DashboardPanel .dash-sparkline {
        height: 3;
    }
    DashboardPanel .dash-sparkline Sparkline {
        height: 1;
    }
    DashboardPanel RichLog {
        height: 1fr;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Dashboard"
        self._start_time = time.time()
        # Track events per 10-second bucket for sparkline
        self._event_buckets: collections.deque = collections.deque(maxlen=30)
        self._current_bucket_time = 0
        self._current_bucket_count = 0
        self._event_log: list[str] = []  # stored for replay after rebuild

    def compose(self) -> ComposeResult:
        yield Static(self._render_stats(), classes="dash-stats")
        yield Vertical(
            Static("[dim]Activity (events/10s)[/]"),
            Sparkline(list(self._event_buckets) or [0], classes="dash-spark"),
            classes="dash-sparkline",
        )
        yield RichLog(markup=True, wrap=True)

    def record_event(self, text: str) -> None:
        """Add to combined feed and update sparkline data."""
        self._event_log.append(text)
        try:
            self.query_one(RichLog).write(text)
        except Exception:
            pass
        # Sparkline bucketing
        bucket = int(time.time()) // 10
        if bucket != self._current_bucket_time:
            if self._current_bucket_time:
                self._event_buckets.append(self._current_bucket_count)
            self._current_bucket_time = bucket
            self._current_bucket_count = 0
        self._current_bucket_count += 1

    def refresh_dashboard(self, panels: dict) -> None:
        """Called every tick to update stats and sparkline."""
        try:
            self.query_one(".dash-stats", Static).update(self._render_stats(panels))
        except Exception:
            pass
        try:
            data = list(self._event_buckets) + [self._current_bucket_count]
            self.query_one(Sparkline).data = data
        except Exception:
            pass

    def _render_stats(self, panels: dict | None = None) -> str:
        SEP = " [dim]│[/] "

        if panels:
            total_accepted = sum(p.accept_count for p in panels.values())
            total_agents_done = sum(p.total_agents_completed for p in panels.values())
            total_agents_active = sum(len(p.active_agents) for p in panels.values())
            active_sessions = sum(1 for p in panels.values() if p._state == "active")
            idle_sessions = sum(1 for p in panels.values() if p._state == "idle")
        else:
            total_accepted = total_agents_done = total_agents_active = 0
            active_sessions = idle_sessions = 0

        sessions = f"[bold green]{active_sessions}[/] active"
        if idle_sessions:
            sessions += f" [yellow]{idle_sessions}[/] idle"

        uptime = self._fmt_duration(time.time() - self._start_time)

        agents_str = f"[bold magenta]{total_agents_active}[/] running" if total_agents_active else "[dim]0[/]"

        return (
            f"Sessions: {sessions}{SEP}"
            f"Agents: {agents_str} [dim]({total_agents_done} done)[/]{SEP}"
            f"Accepted: [bold]{total_accepted}[/]{SEP}"
            f"Uptime: {uptime}"
        )

    def _fmt_duration(self, seconds: float) -> str:
        s = int(seconds)
        if s < 60:
            return f"{s}s"
        if s < 3600:
            return f"{s // 60}m{s % 60:02d}s"
        h = s // 3600
        m = (s % 3600) // 60
        return f"{h}h{m:02d}m"


def _safe_css_id(session_id: str) -> str:
    return "panel-" + session_id.replace("-", "").replace(":", "").replace("/", "")


def _get_frame_size(node):
    """Get pixel (width, height) of an iTerm2 node from its frame."""
    if isinstance(node, Session):
        try:
            return node.frame.size.width, node.frame.size.height
        except Exception:
            return 0, 0
    elif isinstance(node, Splitter):
        # Sum children along the split axis, max along the other
        if not node.children:
            return 0, 0
        sizes = [_get_frame_size(c) for c in node.children]
        if node.vertical:  # side-by-side → sum widths, max heights
            return sum(w for w, _ in sizes), max(h for _, h in sizes)
        else:  # stacked → max widths, sum heights
            return max(w for w, _ in sizes), sum(h for _, h in sizes)
    return 0, 0


def _build_widget_tree(node, self_session_id, panels, old_panels=None, old_dashboard=None, depth=0):
    """Convert iTerm2 Splitter tree to Textual widget tree.

    If old_panels/old_dashboard are provided, state is transferred to new widgets.
    Uses frame sizes from iTerm2 to set proportional widths/heights.
    Returns (root_widget, dashboard_or_None).
    """
    indent = "  " * depth
    if isinstance(node, Session):
        is_self = node.session_id == self_session_id
        css_id = _safe_css_id(node.session_id)
        if is_self:
            panel = DashboardPanel(id=css_id)
            # Transfer dashboard state
            if old_dashboard:
                panel._start_time = old_dashboard._start_time
                panel._event_buckets = old_dashboard._event_buckets
                panel._current_bucket_time = old_dashboard._current_bucket_time
                panel._current_bucket_count = old_dashboard._current_bucket_count
                panel._event_log = list(old_dashboard._event_log)
            log.debug(f"{indent}Session {node.session_id[:8]} (SELF/TUI) -> DashboardPanel id={css_id}")
            return panel, panel
        else:
            name = node.name or "Session"
            sid_short = node.session_id[:8]
            panel = SessionPanel(node.session_id, f"{name} [{sid_short}]", id=css_id)
            # Transfer panel state from old panel if it existed
            if old_panels and node.session_id in old_panels:
                old = old_panels[node.session_id]
                panel.active_agents = dict(old.active_agents)
                panel.accept_count = old.accept_count
                panel.total_agents_completed = old.total_agents_completed
                panel._start_time = old._start_time
                panel._last_event_time = old._last_event_time
                panel._state = old._state
                panel._event_log = list(old._event_log)
            panels[node.session_id] = panel
            log.debug(f"{indent}Session {node.session_id[:8]} {name!r} -> SessionPanel id={css_id}")
            return panel, None

    elif isinstance(node, Splitter):
        Container = Horizontal if node.vertical else Vertical
        cname = "Horizontal" if node.vertical else "Vertical"

        # Get proportional sizes from frame data
        child_sizes = [_get_frame_size(child) for child in node.children]
        if node.vertical:  # side-by-side → proportional widths
            total = sum(w for w, _ in child_sizes) or 1
            fractions = [w / total for w, _ in child_sizes]
        else:  # stacked → proportional heights
            total = sum(h for _, h in child_sizes) or 1
            fractions = [h / total for _, h in child_sizes]

        log.debug(f"{indent}{cname} ({len(node.children)} children, fractions={[f'{f:.0%}' for f in fractions]})")
        dashboard_ref = None
        children = []
        for i, child in enumerate(node.children):
            widget, dash = _build_widget_tree(child, self_session_id, panels, old_panels, old_dashboard, depth + 1)
            # Set proportional size
            pct = round(fractions[i] * 100)
            if node.vertical:
                widget.styles.width = f"{pct}%"
                widget.styles.height = "1fr"
            else:
                widget.styles.height = f"{pct}%"
                widget.styles.width = "1fr"
            children.append(widget)
            if dash:
                dashboard_ref = dash
        container = Container(*children)
        container.styles.height = "1fr"
        container.styles.width = "1fr"
        return container, dashboard_ref

    return Static("?"), None


class AutoAcceptTUI(App):
    """TUI that mirrors iTerm2 pane layout and displays auto-accept events."""

    CSS = """
    #layout-root {
        height: 1fr;
        width: 1fr;
    }
    #status-bar {
        dock: bottom;
        height: 1;
        text-style: bold;
        padding: 0 1;
    }
    #status-bar.running {
        background: $success;
        color: $text;
    }
    #status-bar.paused {
        background: $warning;
        color: $text;
    }
    #status-bar.refreshing {
        background: $accent;
        color: $text;
    }
    """

    TITLE = "Claude Monitor (Auto)"

    BINDINGS = [
        ("p", "toggle_pause", "Auto/Manual"),
        ("r", "refresh_layout", "Refresh"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.panels: dict[str, SessionPanel] = {}
        self.dashboard: DashboardPanel | None = None
        self._iterm_to_panel: dict[str, str] = {}
        self._stop_event = threading.Event()
        self._current_layout_ids: set[str] = set()
        self._current_layout_fp = None  # fingerprint including frame sizes

    @property
    def paused(self) -> bool:
        return os.path.exists(PAUSE_FILE)

    def compose(self) -> ComposeResult:
        yield Header()

        root = Vertical(id="layout-root")
        if _layout_tree:
            log.debug("compose(): building widget tree from layout")
            layout, dash = _build_widget_tree(_layout_tree, _self_session_id, self.panels)
            self.dashboard = dash
            self._current_layout_ids = _collect_session_ids(_layout_tree)
            self._current_layout_fp = _layout_fingerprint(_layout_tree)
            log.debug(f"compose(): panels={list(self.panels.keys())}, dashboard={dash is not None}")
            root.compose_add_child(layout)
        else:
            log.debug("compose(): NO layout tree, using fallback")

        yield root

        yield Static("AUTO — accepting all permission prompts", id="status-bar", classes="running")
        yield Footer()

    def on_mount(self) -> None:
        log.debug(f"on_mount(): panels={list(self.panels.keys())}")
        # Replay event logs into RichLogs (they aren't mounted during __init__)
        for panel in self.panels.values():
            if panel._event_log:
                try:
                    rl = panel.query_one(RichLog)
                    for line in panel._event_log:
                        rl.write(line)
                except Exception:
                    pass
        if self.dashboard and self.dashboard._event_log:
            try:
                rl = self.dashboard.query_one(RichLog)
                for line in self.dashboard._event_log:
                    rl.write(line)
            except Exception:
                pass
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        if os.path.exists(PAUSE_FILE):
            os.remove(PAUSE_FILE)
        self._update_title()
        self.watch_events()
        self.watch_layout()
        self.set_interval(1.0, self._tick_status)

    def _tick_status(self) -> None:
        """Refresh all panel status bars and dashboard every second."""
        for panel in self.panels.values():
            panel._update_status()
        if self.dashboard:
            self.dashboard.refresh_dashboard(self.panels)

    # --- Layout polling ---

    @work(thread=True, exit_on_error=False)
    def watch_layout(self) -> None:
        """Poll iTerm2 layout every 3 seconds for changes."""
        log.debug("watch_layout: started (polling mode)")
        while not self._stop_event.is_set():
            self._stop_event.wait(3.0)
            if self._stop_event.is_set():
                break
            try:
                tree, self_sid = _fetch_layout_sync()
                if tree:
                    new_fp = _layout_fingerprint(tree)
                    if new_fp != self._current_layout_fp:
                        log.debug(f"watch_layout: layout changed (sessions or sizes)")
                        self.post_message(LayoutChanged(tree, self_sid))
            except Exception as e:
                log.debug(f"watch_layout: error: {e}")
        log.debug("watch_layout: stopped")

    async def on_layout_changed(self, msg: LayoutChanged) -> None:
        """Rebuild the widget tree when iTerm2 layout changes."""
        log.debug("on_layout_changed: rebuilding layout")

        # Save references to old state
        old_panels = dict(self.panels)
        old_dashboard = self.dashboard

        # Clear and rebuild
        new_panels: dict[str, SessionPanel] = {}
        layout, dash = _build_widget_tree(
            msg.tree, msg.self_session_id, new_panels,
            old_panels=old_panels, old_dashboard=old_dashboard,
        )

        # Swap out the layout root contents
        root = self.query_one("#layout-root")
        await root.remove_children()
        await root.mount(layout)

        # Update references
        self.panels = new_panels
        self.dashboard = dash
        self._current_layout_ids = _collect_session_ids(msg.tree)
        self._current_layout_fp = _layout_fingerprint(msg.tree)

        # Preserve iterm→panel mappings for sessions that still exist
        self._iterm_to_panel = {
            k: v for k, v in self._iterm_to_panel.items()
            if v in self.panels
        }

        # Replay event logs into the new RichLog widgets
        for panel in self.panels.values():
            if panel._event_log:
                try:
                    rl = panel.query_one(RichLog)
                    for line in panel._event_log:
                        rl.write(line)
                except Exception:
                    pass
        if self.dashboard and self.dashboard._event_log:
            try:
                rl = self.dashboard.query_one(RichLog)
                for line in self.dashboard._event_log:
                    rl.write(line)
            except Exception:
                pass

        log.debug(f"on_layout_changed: done. panels={list(self.panels.keys())}, dashboard={dash is not None}")

    # --- Hook event handling ---

    def _resolve_panel(self, data: dict) -> SessionPanel | None:
        """Find the panel for a hook event, mapping via iTerm2 session ID."""
        claude_sid = data.get("session_id", "")
        raw_iterm = data.get("_iterm_session_id") or ""
        # Handle both "UUID" and "w0t0p2:UUID" formats
        iterm_sid = raw_iterm.split(":", 1)[1] if ":" in raw_iterm else raw_iterm

        # Already mapped this claude session
        if claude_sid in self._iterm_to_panel:
            return self.panels.get(self._iterm_to_panel[claude_sid])

        # Match via _iterm_session_id from the hook
        if iterm_sid and iterm_sid in self.panels:
            self._iterm_to_panel[claude_sid] = iterm_sid
            return self.panels[iterm_sid]

        # No match — create a fallback panel
        cwd = data.get("cwd", "")
        short_id = claude_sid[:8] if len(claude_sid) > 8 else claude_sid
        title = f"{os.path.basename(cwd) or 'session'} [{short_id}]"
        css_id = _safe_css_id(claude_sid)
        panel = SessionPanel(claude_sid, title, id=css_id)
        self.panels[claude_sid] = panel
        self._iterm_to_panel[claude_sid] = claude_sid
        # Mount into layout root
        try:
            self.query_one("#layout-root").mount(panel)
        except Exception:
            self.mount(panel, before=self.query_one("#status-bar"))
        return panel

    def on_hook_event(self, msg: HookEvent) -> None:
        data = msg.data
        event_name = data.get("hook_event_name", "")
        panel = self._resolve_panel(data)
        if not panel:
            return

        panel.touch()
        event_ts = datetime.fromtimestamp(data.get("_timestamp", time.time()))
        t = event_ts.strftime("%H:%M:%S")

        if event_name == "PermissionRequest":
            tool = data.get("tool_name", "?")
            tool_input = data.get("tool_input", {})
            detail = ""
            if tool == "Bash":
                detail = f" `{tool_input.get('command', '')[:60]}`"
            elif tool in ("Edit", "Write"):
                detail = f" `{tool_input.get('file_path', '')}`"
            elif tool == "WebFetch":
                detail = f" `{tool_input.get('url', '')[:60]}`"

            if self.paused:
                panel.write(f"[{t}] [bold yellow]PAUSED[/] {tool}{detail}")
            else:
                panel.accept_count += 1
                panel.write(f"[{t}] [bold green]ALLOWED[/] {tool}{detail}")

        elif event_name == "Notification":
            ntype = data.get("notification_type", "")
            message = data.get("message", "")
            if ntype == "idle_prompt":
                panel.mark_idle()
                panel.write(f"[{t}] [dim]IDLE[/] {message}")
            else:
                panel.write(f"[{t}] [bold cyan]NOTIFY[/] {message}")

        elif event_name == "SubagentStart":
            agent_id = data.get("agent_id", "?")
            agent_type = data.get("agent_type", "?")
            panel.active_agents[agent_id] = agent_type
            panel.write(f"[{t}] [bold magenta]AGENT+[/] {agent_type} [{agent_id[:8]}]")

        elif event_name == "SubagentStop":
            agent_id = data.get("agent_id", "?")
            agent_type = data.get("agent_type", "?")
            panel.active_agents.pop(agent_id, None)
            panel.total_agents_completed += 1
            panel.write(f"[{t}] [magenta]AGENT-[/] {agent_type} [{agent_id[:8]}]")

        panel._update_status()

        # Feed to dashboard combined feed
        if self.dashboard:
            sid_short = panel.session_id[:8]
            if event_name == "PermissionRequest":
                tool = data.get("tool_name", "?")
                label = "[yellow]PAUSED[/]" if self.paused else "[green]ALLOWED[/]"
                self.dashboard.record_event(f"[{t}] [{sid_short}] {label} {tool}")
            elif event_name == "Notification":
                ntype = data.get("notification_type", "")
                label = "[dim]IDLE[/]" if ntype == "idle_prompt" else "[cyan]NOTIFY[/]"
                self.dashboard.record_event(f"[{t}] [{sid_short}] {label} {data.get('message', '')[:60]}")
            elif event_name == "SubagentStart":
                self.dashboard.record_event(f"[{t}] [{sid_short}] [magenta]AGENT+[/] {data.get('agent_type', '?')}")
            elif event_name == "SubagentStop":
                self.dashboard.record_event(f"[{t}] [{sid_short}] [magenta]AGENT-[/] {data.get('agent_type', '?')}")

    # --- Keybindings ---

    def action_refresh_layout(self) -> None:
        """Manually re-fetch iTerm2 layout and rebuild."""
        status = self.query_one("#status-bar", Static)
        status.update("REFRESHING layout...")
        status.set_classes("refreshing")
        self._do_refresh()

    @work(thread=True)
    def _do_refresh(self) -> None:
        """Fetch layout in a thread (can't run iterm2 sync from Textual's event loop)."""
        try:
            tree, self_sid = _fetch_layout_sync()
            if tree:
                self.post_message(LayoutChanged(tree, self_sid))
        except Exception as e:
            log.debug(f"_do_refresh: error: {e}")

        def _restore():
            try:
                status = self.query_one("#status-bar", Static)
                status.update("AUTO — accepting all permission prompts" if not self.paused
                              else "MANUAL — permission prompts shown to user")
                status.set_classes("running" if not self.paused else "paused")
            except Exception:
                pass

        self.call_from_thread(_restore)

    def _update_title(self) -> None:
        self.title = "Claude Monitor (Manual)" if self.paused else "Claude Monitor (Auto)"

    def action_toggle_pause(self) -> None:
        if self.paused:
            os.remove(PAUSE_FILE)
            status = self.query_one("#status-bar", Static)
            status.update("AUTO — accepting all permission prompts")
            status.set_classes("running")
        else:
            with open(PAUSE_FILE, "w") as f:
                f.write("1")
            status = self.query_one("#status-bar", Static)
            status.update("MANUAL — permission prompts shown to user")
            status.set_classes("paused")
        self._update_title()

    def action_quit(self) -> None:
        self._stop_event.set()
        self.exit()

    def _on_exit_app(self) -> None:
        """Ensure background threads don't prevent exit."""
        self._stop_event.set()

    # --- Event file watcher ---

    @work(thread=True, exit_on_error=False)
    def watch_events(self) -> None:
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        if not os.path.exists(EVENTS_FILE):
            open(EVENTS_FILE, "w").close()

        with open(EVENTS_FILE, "r") as f:
            f.seek(0, 2)
            while not self._stop_event.is_set():
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            self.post_message(HookEvent(data))
                        except json.JSONDecodeError:
                            pass
                else:
                    self._stop_event.wait(0.2)
        log.debug("watch_events: stopped")


def main():
    fetch_iterm_layout()
    app = AutoAcceptTUI()
    app.run()
    # Force exit — background threads (layout polling, event watcher) may be
    # blocked on I/O (iterm2 websocket, file read) and can't be interrupted
    # cleanly. The stop_event is set but threads may not see it immediately.
    os._exit(0)


if __name__ == "__main__":
    main()
