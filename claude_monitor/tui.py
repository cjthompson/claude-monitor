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
import re
import threading
import time
from datetime import datetime, timezone

import iterm2
from iterm2.session import Splitter, Session
from textual import work
from textual.app import App, ComposeResult
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBarRender
from textual.widgets import Footer, OptionList, RichLog, Sparkline, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from claude_monitor import __version__, SIGNAL_DIR, EVENTS_FILE, STATE_FILE, LOG_FILE, API_PORT_FILE, extract_iterm_session_id, fmt_duration, read_state
from claude_monitor.api import start_api_server
from claude_monitor.settings import Settings, SettingsScreen, load_settings, save_settings
from claude_monitor.usage import fetch_usage, format_usage_inline, invalidate_usage_cache, set_oauth_json, set_on_token_refreshed

os.makedirs(SIGNAL_DIR, exist_ok=True)
logging.basicConfig(filename=LOG_FILE, level=logging.DEBUG, format="%(asctime)s %(message)s", force=True)
log = logging.getLogger(__name__)
# Suppress noisy third-party loggers that flood the debug log with
# websocket protocol frames and asyncio selector events on every poll.
for _noisy in ("websockets", "asyncio", "iterm2.connection"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)


# --- Persistent iTerm2 websocket connection ---
#
# A single websocket stays open for the lifetime of the app.  All iTerm2
# operations (layout fetch, tab title set, keystroke send) are scheduled
# on its event loop via asyncio.run_coroutine_threadsafe().

import asyncio as _asyncio

_iterm2_loop: _asyncio.AbstractEventLoop | None = None
_iterm2_app: iterm2.App | None = None
_iterm2_ready = threading.Event()


def _start_iterm2_connection():
    """Launch a daemon thread that holds a persistent iTerm2 websocket."""
    def _run():
        global _iterm2_loop, _iterm2_app
        conn = iterm2.Connection()

        async def _init(connection):
            global _iterm2_loop, _iterm2_app
            _iterm2_loop = _asyncio.get_event_loop()
            _iterm2_app = await iterm2.async_get_app(connection)
            log.debug("iterm2: persistent connection established")
            _iterm2_ready.set()

        try:
            conn.run_forever(_init, retry=True)
        except Exception as e:
            log.debug(f"iterm2: persistent connection died: {e}")

    threading.Thread(target=_run, daemon=True, name="iterm2-ws").start()


def _iterm2_call(coro_func, timeout=10):
    """Schedule an async callable on the persistent iTerm2 connection.

    coro_func receives the iterm2.App and returns a result.
    Blocks the calling thread until the result is ready.
    """
    if not _iterm2_ready.wait(timeout=5):
        log.debug("iterm2: connection not ready")
        return None
    future = _asyncio.run_coroutine_threadsafe(coro_func(_iterm2_app), _iterm2_loop)
    try:
        return future.result(timeout=timeout)
    except Exception as e:
        log.debug(f"iterm2: call failed: {e}")
        return None


# --- iTerm2 layout helpers ---

def _fetch_layout_sync(tab_titles: dict[str, str] | None = None):
    """Fetch iTerm2 pane layout, optionally setting tab titles.

    Returns (tabs, self_session_id, window_groups).
    Uses the persistent websocket connection.
    """
    async def _do(app):
        tabs = []
        window_groups = {}
        for window in app.terminal_windows:
            win_tab_ids = []
            for tab in window.tabs:
                if tab.root:
                    try:
                        tab_name = await tab.async_get_variable("title") or "Tab"
                    except Exception:
                        log.debug("_fetch_layout_sync: failed to get tab title, defaulting to 'Tab'")
                        tab_name = "Tab"
                    tabs.append((tab.tab_id, tab_name, tab.root))
                    win_tab_ids.append(tab.tab_id)
                if tab_titles and tab.tab_id in tab_titles:
                    try:
                        await tab.async_set_title(tab_titles[tab.tab_id])
                    except Exception:
                        log.debug(f"_fetch_layout_sync: failed to set title for tab {tab.tab_id}")
            if win_tab_ids:
                window_groups[window.window_id] = win_tab_ids
        return tabs, window_groups

    raw = os.environ.get("ITERM_SESSION_ID", "")
    self_sid = extract_iterm_session_id(raw)

    result = _iterm2_call(_do)
    if result:
        return result[0], self_sid, result[1]
    return [], self_sid, {}


def _send_keystroke_sync(session_id: str, text: str) -> bool:
    """Send keystrokes to a specific iTerm2 session. Returns True on success."""
    async def _do(app):
        session = app.get_session_by_id(session_id)
        if session:
            await session.async_send_text(text)
            return True
        return False

    return _iterm2_call(_do) or False


def _set_tab_titles_sync(tab_titles: dict[str, str]) -> None:
    """Set iTerm2 tab titles using the persistent connection."""
    if not tab_titles:
        return

    async def _do(app):
        for window in app.terminal_windows:
            for tab in window.tabs:
                if tab.tab_id in tab_titles:
                    try:
                        await tab.async_set_title(tab_titles[tab.tab_id])
                    except Exception:
                        log.debug(f"_set_tab_titles_sync: failed to set title for tab {tab.tab_id}")

    _iterm2_call(_do)


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


def _filter_tabs_by_scope(tabs, self_sid, scope, window_groups=None):
    """Filter tabs based on iTerm scope setting.

    scope: "current_tab", "current_window", or "all_windows"
    Requires knowing which tab/window contains self_sid (the TUI's own pane).
    Falls back to returning all tabs if self_sid is not found.
    """
    if scope == "all_windows" or not self_sid:
        return tabs

    # Find the tab containing the TUI's own session
    self_tab_id = None
    for tab_id, tab_name, root in tabs:
        if self_sid in _collect_session_ids(root):
            self_tab_id = tab_id
            break

    if not self_tab_id:
        return tabs  # can't determine our tab, show everything

    if scope == "current_tab":
        return [(tid, tn, r) for tid, tn, r in tabs if tid == self_tab_id]

    if scope == "current_window" and window_groups:
        # Find which window contains our tab
        for _win_id, win_tab_ids in window_groups.items():
            if self_tab_id in win_tab_ids:
                allowed = set(win_tab_ids)
                return [(tid, tn, r) for tid, tn, r in tabs if tid in allowed]

    return tabs


def _structure_fingerprint_node(node):
    """Fingerprint of layout structure only (no sizes). Changes on add/remove/reorder."""
    if isinstance(node, Session):
        return ("session", node.session_id)
    elif isinstance(node, Splitter):
        children = tuple(_structure_fingerprint_node(c) for c in node.children)
        return ("split", node.vertical, children)
    return ()


def _structure_fingerprint(tabs):
    """Structural fingerprint: tabs, panes, split directions. No sizes or names."""
    return tuple(
        (tab_id, _structure_fingerprint_node(root))
        for tab_id, _tab_name, root in tabs
    )


def _size_fingerprint_node(node):
    """Fingerprint of frame sizes only. Changes on resize."""
    if isinstance(node, Session):
        w, h = _get_frame_size(node)
        return (int(w), int(h))
    elif isinstance(node, Splitter):
        return tuple(_size_fingerprint_node(c) for c in node.children)
    return ()


def _size_fingerprint(tabs):
    """Size fingerprint: pixel dimensions of all panes."""
    return tuple(_size_fingerprint_node(root) for _, _, root in tabs)


# Fetch initial layout before Textual starts
_layout_tabs: list[tuple] = []  # [(tab_id, tab_name, root_splitter), ...]
_self_session_id: str | None = None


def fetch_iterm_layout():
    global _layout_tabs, _self_session_id
    _start_iterm2_connection()
    if not _iterm2_ready.wait(timeout=5):
        raise ConnectionRefusedError("Could not connect to iTerm2")
    tabs, self_sid, win_groups = _fetch_layout_sync()
    settings = load_settings()
    _layout_tabs = _filter_tabs_by_scope(tabs, self_sid, settings.iterm_scope, win_groups)
    _self_session_id = self_sid
    log.debug(f"fetch_iterm_layout done: tabs={len(_layout_tabs)}, self={_self_session_id}")


# --- Textual widgets ---

class HookEvent(Message):
    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data


class LayoutChanged(Message):
    """Posted when iTerm2 layout structure has changed (panes added/removed/rearranged)."""
    def __init__(self, tabs: list[tuple], self_session_id: str) -> None:
        super().__init__()
        self.tabs = tabs
        self.self_session_id = self_session_id


class LayoutResized(Message):
    """Posted when iTerm2 pane sizes changed but structure is the same."""
    def __init__(self, tabs: list[tuple]) -> None:
        super().__init__()
        self.tabs = tabs


class SessionPanel(Static):
    """A bordered panel showing events for one session."""

    class PaneToggle(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    class AskPauseToggle(Message):
        def __init__(self, session_id: str) -> None:
            super().__init__()
            self.session_id = session_id

    DEFAULT_CSS = """
    SessionPanel {
        border: solid $accent;
        height: 1fr;
        width: 1fr;
        padding: 0 1;
        layers: base overlay;
    }
    SessionPanel.pane-paused {
        border: solid $warning;
    }
    SessionPanel RichLog {
        height: 1fr;
        background: $background;
        layer: base;
    }
    SessionPanel:focus {
        border: double $accent;
    }
    SessionPanel.pane-paused:focus {
        border: double $warning;
    }
    SessionPanel .panel-status {
        dock: bottom;
        height: 1;
        background: $surface;
        color: $text-muted;
        layer: base;
    }
    SessionPanel .countdown-bar {
        dock: bottom;
        height: 1;
        background: $warning-darken-3;
        color: $warning;
        text-style: bold;
        display: none;
        layer: base;
    }
    SessionPanel .countdown-bar.active {
        display: block;
    }
    SessionPanel .timeout-overlay {
        display: none;
        layer: overlay;
        dock: bottom;
        offset-y: -1;
        height: 1;
        width: 1fr;
        background: #006080;
        color: #00e5ff;
        text-style: bold;
        text-align: center;
        content-align: center middle;
    }
    SessionPanel .timeout-overlay.active {
        display: block;
    }
    """

    can_focus = True

    BINDINGS = [
        ("m", "toggle_pane_mode", "Toggle Auto/Manual"),
        ("p", "toggle_ask_pause", "Pause Questions"),
    ]

    def __init__(self, session_id: str, title: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.session_id = session_id
        self.border_title = title
        self.border_subtitle = ""
        self.active_agents: dict[str, str] = {}  # agent_id → agent_type
        self.accept_count = 0
        self.total_agents_completed = 0
        self._start_time = time.time()
        self._last_event_time: float | None = None
        self._state = "waiting"  # waiting, active, idle
        self._event_log: list[str] = []  # stored for replay after rebuild
        self._pending_timeout: float | None = None  # epoch when timeout expires
        self._timeout_origin: float | None = None  # origin timestamp of the timeout event

    def compose(self) -> ComposeResult:
        yield RichLog(markup=True, wrap=False)
        yield Static("", classes="timeout-overlay")
        yield Static("", classes="countdown-bar")
        yield Static(self._render_status(), classes="panel-status")

    @property
    def state(self) -> str:
        return self._state

    def on_mount(self) -> None:
        rl = self.query_one(RichLog)
        rl.horizontal_scrollbar.renderer = HorizontalScrollBarRender
        rl.vertical_scrollbar.renderer = VerticalScrollBarRender

    def write(self, text: str) -> None:
        self._event_log.append(text)
        try:
            self.query_one(RichLog).write(text)
        except Exception:
            log.debug(f"SessionPanel.write: RichLog query failed for session {self.session_id}")

    def touch(self) -> None:
        """Mark this panel as having received activity."""
        self._last_event_time = time.time()
        self._state = "active"

    def mark_idle(self) -> None:
        self._state = "idle"

    def _render_status(self) -> str:
        SEP = " [dim]│[/] "

        # Mode indicator — check if app has per-pane pause info
        try:
            app = self.app
            if hasattr(app, "is_pane_paused") and app.is_pane_paused(self.session_id):
                mode = "[yellow]MANUAL[/]"
                mode_plain = "MANUAL"
            else:
                mode = "[green]AUTO[/]"
                mode_plain = "AUTO"
            # Ask-pause indicator
            if hasattr(app, "is_ask_paused") and app.is_ask_paused(self.session_id):
                mode += " [cyan]?⏸[/]"
                mode_plain += " ?⏸"
        except Exception:
            log.debug(f"SessionPanel._render_status: failed to check pause state for {self.session_id}")
            mode = ""
            mode_plain = ""

        # State indicator
        if self._state == "active":
            state = "[bold green]▶ active[/]"
            state_short = "[bold green]▶[/]"
            state_plain = "▶ active"
        elif self._state == "idle":
            state = "[yellow]⏸ idle[/]"
            state_short = "[yellow]⏸[/]"
            state_plain = "⏸ idle"
        else:
            state = "[dim]◦ waiting[/]"
            state_short = "[dim]◦[/]"
            state_plain = "◦ waiting"

        # Agents
        n = len(self.active_agents)
        has_agents = n > 0
        if has_agents:
            blocks = "█" * min(n, 8)
            types = {}
            for atype in self.active_agents.values():
                types[atype] = types.get(atype, 0) + 1
            detail = " ".join(f"{t}:{c}" for t, c in sorted(types.items()))
            agents_full = f"[bold magenta]{blocks}[/] {n} ({detail})"
            agents_full_plain = f"{blocks} {n} ({detail})"
            agents_count = f"[bold magenta]{blocks}[/] {n}"
            agents_count_plain = f"{blocks} {n}"
        else:
            agents_full = "[dim]── none[/]"
            agents_full_plain = "── none"
            agents_count = agents_full
            agents_count_plain = agents_full_plain

        # Task counts (Done/Accepted)
        done = self.total_agents_completed
        accepted = self.accept_count

        # Uptime
        uptime = fmt_duration(time.time() - self._start_time)

        # Available width for choosing tier
        try:
            w = self.size.width
        except Exception:
            log.debug(f"SessionPanel._render_status: failed to get widget width for {self.session_id}, defaulting to 120")
            w = 120  # fallback to widest

        # SEP is ~3 visible chars (" │ ")
        S = 3

        # Build tiers from widest to narrowest
        # Tier 1 (>=110): AUTO │ ▶ active │ Agents: ██ 2 (gp:1 Ex:1) │ Done: 5 │ Accepted: 23 │ 14m32s
        if has_agents:
            t1 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}{SEP}Done: {done}{SEP}Accepted: {accepted}{SEP}{uptime}"
            t1_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain) + S + len(f"Done: {done}") + S + len(f"Accepted: {accepted}") + S + len(uptime)
        else:
            t1 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}{SEP}{uptime}"
            t1_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain) + S + len(uptime)

        if w >= t1_len:
            return t1

        # Tier 2 (>=85): AUTO │ ▶ active │ Agents: ██ 2 (gp:1 Ex:1) │ Tasks: 5/23
        if has_agents:
            t2 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}{SEP}Tasks: {done}/{accepted}"
            t2_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain) + S + len(f"Tasks: {done}/{accepted}")
        else:
            t2 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}"
            t2_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain)

        if w >= t2_len:
            return t2

        # Tier 3 (>=60): AUTO │ ▶ active │ Agents: ██ 2 | Tasks: 5/23
        if has_agents:
            t3 = f"{mode}{SEP}{state}{SEP}Agents: {agents_count} | Tasks: {done}/{accepted}"
            t3_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_count_plain) + len(f" | Tasks: {done}/{accepted}")
        else:
            t3 = f"{mode}{SEP}{state}{SEP}Agents: {agents_count}"
            t3_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_count_plain)

        if w >= t3_len:
            return t3

        # Tier 4 (>=48): AUTO │ ▶ active │ SA: 2 | T: 5/23
        if has_agents:
            t4 = f"{mode}{SEP}{state}{SEP}SA: {n} | T: {done}/{accepted}"
            t4_len = len(mode_plain) + S + len(state_plain) + S + len(f"SA: {n} | T: {done}/{accepted}")
        else:
            t4 = f"{mode}{SEP}{state}{SEP}SA: {n}"
            t4_len = len(mode_plain) + S + len(state_plain) + S + len(f"SA: {n}")

        if w >= t4_len:
            return t4

        # Tier 5 (>=38): AUTO │ ▶ | SA: 2 | T: 5/23
        if has_agents:
            t5 = f"{mode}{SEP}{state_short} | SA: {n} | T: {done}/{accepted}"
            t5_len = len(mode_plain) + S + 1 + len(f" | SA: {n} | T: {done}/{accepted}")
        else:
            t5 = f"{mode}{SEP}{state_short} | SA: {n}"
            t5_len = len(mode_plain) + S + 1 + len(f" | SA: {n}")

        if w >= t5_len:
            return t5

        # Tier 6 (>=25): AUTO │ ▶ | T:5/23
        if has_agents:
            t6 = f"{mode}{SEP}{state_short} | T:{done}/{accepted}"
            t6_len = len(mode_plain) + S + 1 + len(f" | T:{done}/{accepted}")
        else:
            t6 = f"{mode}{SEP}{state_short}"
            t6_len = len(mode_plain) + S + 1

        if w >= t6_len:
            return t6

        # Tier 7 (>=12): AUTO │ ▶
        t7 = f"{mode}{SEP}{state_short}"
        t7_len = len(mode_plain) + S + 1

        if w >= t7_len:
            return t7

        # Tier 8 (<12): AUTO
        return mode

    def _update_status(self) -> None:
        try:
            self.query_one(".panel-status", Static).update(self._render_status())
        except Exception:
            log.debug(f"SessionPanel._update_status: panel-status query failed for {self.session_id}")
        # Update countdown overlay and bar
        try:
            bar = self.query_one(".countdown-bar", Static)
            overlay = self.query_one(".timeout-overlay", Static)
            if self._pending_timeout is not None:
                remaining = max(0, int(self._pending_timeout - time.time()))
                if remaining > 0:
                    bar.update(f" ⏱ AskUserQuestion auto-accept in {remaining}s")
                    bar.add_class("active")
                    overlay.update(f" ⏱ AskUserQuestion — auto-accept in [bold white]{remaining}s[/] ")
                    overlay.add_class("active")
                else:
                    self._pending_timeout = None
                    bar.update("")
                    bar.remove_class("active")
                    overlay.update("")
                    overlay.remove_class("active")
            else:
                bar.update("")
                bar.remove_class("active")
                overlay.update("")
                overlay.remove_class("active")
        except Exception:
            log.debug(f"SessionPanel._update_status: countdown/overlay query failed for {self.session_id}")

    def on_click(self, event) -> None:
        """Title bar click opens context menu; status bar click toggles mode."""
        # Click on top border row (where the title is) opens context menu
        if event.screen_y == self.region.y:
            event.stop()
            self.app.push_screen(PaneContextMenu(self.session_id, click_x=event.screen_x, click_y=event.screen_y))
            return
        try:
            status = self.query_one(".panel-status", Static)
            if event.screen_y >= status.region.y:
                self.post_message(self.PaneToggle(self.session_id))
        except Exception:
            log.debug(f"SessionPanel.on_click: panel-status query failed for {self.session_id}")

    def action_toggle_pane_mode(self) -> None:
        self.post_message(self.PaneToggle(self.session_id))

    def action_toggle_ask_pause(self) -> None:
        self.post_message(self.AskPauseToggle(self.session_id))


class PaneContextMenu(ModalScreen):
    """Context menu for a SessionPanel, shown on body click."""

    DEFAULT_CSS = """
    PaneContextMenu {
        layout: horizontal;
    }
    PaneContextMenu #ctx-menu {
        width: 40;
        height: auto;
        background: $surface;
        border: solid $secondary;
        padding: 0;
    }
    PaneContextMenu OptionList {
        height: auto;
        max-height: 10;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
    ]

    def __init__(self, session_id: str, click_x: int = 0, click_y: int = 0, **kwargs) -> None:
        super().__init__(**kwargs)
        self._ctx_session_id = session_id
        self._click_x = click_x
        self._click_y = click_y

    def compose(self) -> ComposeResult:
        with Vertical(id="ctx-menu"):
            yield OptionList(
                Option("Toggle Auto/Manual", id="toggle_mode"),
                Option("View Choices Log", id="choices"),
                Option("View Questions Log", id="questions"),
                Option("Copy Session ID", id="copy_sid"),
                Option("Open Settings", id="settings"),
                id="ctx-options",
            )

    def on_mount(self) -> None:
        menu = self.query_one("#ctx-menu", Vertical)
        menu.styles.margin = (self._click_y, 0, 0, self._click_x)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        self.app.pop_screen()
        if option_id == "toggle_mode":
            self.app.on_session_panel_pane_toggle(
                SessionPanel.PaneToggle(self._ctx_session_id)
            )
        elif option_id == "choices":
            self.app.action_show_choices()
        elif option_id == "questions":
            self.app.action_show_questions()
        elif option_id == "copy_sid":
            self.app.copy_to_clipboard(self._ctx_session_id)
            self.app.notify(f"Session ID copied: {self._ctx_session_id[:12]}...")
        elif option_id == "settings":
            self.app.action_open_settings()

    def on_click(self, event) -> None:
        """Dismiss if clicking outside the menu."""
        try:
            menu = self.query_one("#ctx-menu", Vertical)
            region = menu.region
            if not region.contains(event.screen_x, event.screen_y):
                self.app.pop_screen()
        except Exception:
            self.app.pop_screen()

    def action_dismiss(self) -> None:
        self.app.pop_screen()


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
        height: 1;
    }
    DashboardPanel .dash-sparkline {
        height: 2;
        width: 100%;
    }
    DashboardPanel .dash-sparkline-label {
        width: 24;
        height: 2;
    }
    DashboardPanel .dash-sparkline FixedWidthSparkline {
        height: 2;
        width: 1fr;
    }
    DashboardPanel RichLog {
        height: 1fr;
        background: $background;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.border_title = "Dashboard"
        self._start_time = time.time()
        self.active_agents: dict[str, str] = {}  # agent_id → agent_type (own session)
        self.total_agents_completed = 0
        self.accept_count = 0
        # Track events per N-second bucket for sparkline
        self._event_buckets: collections.deque = collections.deque(maxlen=300)
        self._bucket_secs = 5  # overridden from settings after mount
        self._bucket_counter = 0  # counts ticks (1s each) within a bucket
        self._current_bucket_count = 0
        self._event_log: list[str] = []  # stored for replay after rebuild

    def compose(self) -> ComposeResult:
        yield Static(self._render_stats(), classes="dash-stats")
        yield Horizontal(
            Vertical(
                Static(f"[dim]Activity (events/{self._bucket_secs}s)[/]"),
                Static(self._render_scale_label(), classes="dash-scale-label"),
                classes="dash-sparkline-label",
            ),
            FixedWidthSparkline(self._scaled_data()),
            classes="dash-sparkline",
        )
        yield RichLog(markup=True, wrap=False)

    def on_mount(self) -> None:
        rl = self.query_one(RichLog)
        rl.horizontal_scrollbar.renderer = HorizontalScrollBarRender
        rl.vertical_scrollbar.renderer = VerticalScrollBarRender

    def record_event(self, text: str) -> None:
        """Add to combined feed and update sparkline data."""
        self._event_log.append(text)
        try:
            self.query_one(RichLog).write(text)
        except Exception:
            log.debug("Dashboard.record_event: RichLog query failed")
        self._current_bucket_count += 1

    def refresh_dashboard(self, panels: dict) -> None:
        """Called every tick (1s) to update stats and sparkline."""
        self._bucket_counter += 1
        if self._bucket_counter >= self._bucket_secs:
            self._event_buckets.append(self._current_bucket_count)
            self._current_bucket_count = 0
            self._bucket_counter = 0
        try:
            self.query_one(".dash-stats", Static).update(self._render_stats(panels))
        except Exception:
            log.debug("Dashboard.refresh_dashboard: dash-stats query failed")
        try:
            self.query_one(FixedWidthSparkline).data = self._scaled_data()
            self.query_one(".dash-scale-label", Static).update(self._render_scale_label())
        except Exception:
            log.debug("Dashboard.refresh_dashboard: Sparkline query failed")

    _MIN_Y_SCALE = 4  # minimum y-axis max so low counts don't fill the bar

    def _visible_data(self) -> list[int]:
        """Return the sparkline data that's actually visible (last `width` buckets)."""
        raw = list(self._event_buckets) + [self._current_bucket_count]
        try:
            width = self.query_one(FixedWidthSparkline).size.width
        except Exception:
            log.debug("Dashboard._visible_data: failed to get sparkline width, using raw data length")
            width = len(raw)
        return raw[-width:] if len(raw) > width else raw

    def _scaled_data(self) -> list[float]:
        """Return sparkline data normalized to 0.0–1.0 against visible peak."""
        raw = self._visible_data()
        if not raw:
            return [0.0]
        peak = max(max(raw), self._MIN_Y_SCALE)
        return [v / peak for v in raw]

    def _render_scale_label(self) -> str:
        raw = self._visible_data()
        peak = max(max(raw), self._MIN_Y_SCALE) if raw else self._MIN_Y_SCALE
        return f"[dim]now {self._current_bucket_count} · peak {peak}[/]"

    def _render_stats(self, panels: dict | None = None) -> str:
        SEP = " [dim]│[/] "

        if panels:
            total_accepted = sum(p.accept_count for p in panels.values())
            total_agents_done = sum(p.total_agents_completed for p in panels.values())
            total_agents_active = sum(len(p.active_agents) for p in panels.values())
            active_sessions = sum(1 for p in panels.values() if p.state == "active")
            idle_sessions = sum(1 for p in panels.values() if p.state == "idle")
        else:
            total_accepted = total_agents_done = total_agents_active = 0
            active_sessions = idle_sessions = 0

        # Include dashboard's own session agents in totals
        total_accepted += self.accept_count
        total_agents_done += self.total_agents_completed
        total_agents_active += len(self.active_agents)

        sessions = f"[bold green]{active_sessions}[/] active"
        if idle_sessions:
            sessions += f" [yellow]{idle_sessions}[/] idle"

        uptime = fmt_duration(time.time() - self._start_time)

        agents_str = f"[bold magenta]{total_agents_active}[/] running" if total_agents_active else "[dim]0[/]"

        return (
            f"Sessions: {sessions}{SEP}"
            f"Agents: {agents_str} [dim]({total_agents_done} done)[/]{SEP}"
            f"Accepted: [bold]{total_accepted}[/]{SEP}"
            f"Uptime: {uptime}"
        )


class ChoicesScreen(ModalScreen):
    """Review screen showing all auto-accepted permission decisions.

    Opened via 'c' key. Reads events.jsonl and shows formatted PermissionRequest entries.
    """

    DEFAULT_CSS = """
    ChoicesScreen {
        align: center middle;
    }
    ChoicesScreen #choices-dialog {
        width: 90%;
        height: 85%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    ChoicesScreen #choices-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }
    ChoicesScreen RichLog {
        height: 1fr;
    }
    ChoicesScreen #choices-footer {
        dock: bottom;
        height: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="choices-dialog"):
            yield Static("Permission Choices Log", id="choices-title")
            yield RichLog(markup=True, wrap=True, id="choices-log")
            yield Static("[dim]ESC to close[/]", id="choices-footer")

    def on_mount(self) -> None:
        rl = self.query_one("#choices-log", RichLog)
        entries = self._load_choices()
        if not entries:
            rl.write("[dim]No permission events recorded yet.[/]")
            return
        for entry in entries:
            rl.write(entry)
        rl.scroll_end(animate=False)
        self.query_one("#choices-log", RichLog).horizontal_scrollbar.renderer = HorizontalScrollBarRender

    def _load_choices(self) -> list[str]:
        """Load PermissionRequest events from events.jsonl (newest first)."""
        entries = []
        try:
            with open(EVENTS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        log.debug(f"PermissionSearchProvider: failed to parse JSON line: {line[:100]}")
                        continue
                    if data.get("hook_event_name") != "PermissionRequest":
                        continue
                    entries.append(self._format_choice(data))
        except FileNotFoundError:
            log.debug("PermissionSearchProvider: events file not found")
        return entries[-200:]

    def _format_choice(self, data: dict) -> str:
        ts = datetime.fromtimestamp(data.get("_timestamp", 0))
        t = ts.strftime("%Y-%m-%d %H:%M:%S")
        tool = data.get("tool_name", "?")
        tool_input = data.get("tool_input", {})
        session = data.get("session_id", "?")[:8]
        cwd = data.get("cwd", "")
        project = os.path.basename(cwd) if cwd else ""
        decision = data.get("_decision", "allowed")
        excluded = data.get("_excluded_tool", False)

        # Decision badge
        if excluded:
            badge = "[bold red]MANUAL  [/]"
        elif decision == "deferred":
            badge = "[bold yellow]DEFERRED[/]"
        else:
            badge = "[bold green]ALLOWED [/]"

        # Tool detail
        detail = ""
        if tool == "Bash":
            cmd = tool_input.get("command", "")
            desc = tool_input.get("description", "")
            detail = f"\n    [dim]cmd:[/] {cmd[:120]}"
            if desc:
                detail += f"\n    [dim]desc:[/] {desc[:80]}"
        elif tool in ("Edit", "Write", "Read"):
            fp = tool_input.get("file_path", "")
            detail = f"\n    [dim]file:[/] {fp}"
        elif tool == "WebFetch":
            url = tool_input.get("url", "")
            detail = f"\n    [dim]url:[/] {url[:100]}"
        elif tool_input:
            for k, v in list(tool_input.items())[:2]:
                detail += f"\n    [dim]{k}:[/] {str(v)[:80]}"

        # Suggestions
        suggestions = data.get("permission_suggestions", [])
        sug_text = ""
        if suggestions:
            sug_parts = []
            for s in suggestions:
                stype = s.get("type", "?")
                if stype == "addRules":
                    rules = s.get("rules", [])
                    for r in rules:
                        sug_parts.append(f"addRule({r.get('toolName', '?')})")
                elif stype == "setMode":
                    sug_parts.append(f"setMode({s.get('mode', '?')})")
                else:
                    sug_parts.append(stype)
            sug_text = f"\n    [dim]suggestions:[/] {', '.join(sug_parts)}"

        return (
            f"[bold]{t}[/]  [{session}]  [bold]{project}[/]\n"
            f"  {badge} [bold]{tool}[/]{detail}{sug_text}\n"
        )

    def action_dismiss(self) -> None:
        self.app.pop_screen()


class QuestionsScreen(ModalScreen):
    """Review screen showing AskUserQuestion events only.

    Opened via 'u' key. Reads events.jsonl and shows formatted AskUserQuestion entries.
    """

    DEFAULT_CSS = """
    QuestionsScreen {
        align: center middle;
    }
    QuestionsScreen #questions-dialog {
        width: 90%;
        height: 85%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    QuestionsScreen #questions-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }
    QuestionsScreen RichLog {
        height: 1fr;
    }
    QuestionsScreen #questions-footer {
        dock: bottom;
        height: 1;
        text-align: center;
        color: $text-muted;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("q", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="questions-dialog"):
            yield Static("AskUserQuestion Log", id="questions-title")
            yield RichLog(markup=True, wrap=True, id="questions-log")
            yield Static("[dim]ESC to close[/]", id="questions-footer")

    def on_mount(self) -> None:
        rl = self.query_one("#questions-log", RichLog)
        entries = self._load_questions()
        if not entries:
            rl.write("[dim]No AskUserQuestion events recorded yet.[/]")
            return
        for entry in entries:
            rl.write(entry)
        rl.scroll_end(animate=False)
        self.query_one("#questions-log", RichLog).horizontal_scrollbar.renderer = HorizontalScrollBarRender

    def _load_questions(self) -> list[str]:
        """Load AskUserQuestion events from events.jsonl, merging answers from PostToolUse."""
        perm_events: list[dict] = []
        # Map session_id -> list of PostToolUse answers (in order) for merging
        post_answers: dict[str, list[dict]] = {}
        try:
            with open(EVENTS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("tool_name") != "AskUserQuestion":
                        continue
                    evt = data.get("hook_event_name", "")
                    if evt == "PermissionRequest":
                        perm_events.append(data)
                    elif evt == "PostToolUse":
                        sid = data.get("session_id", "")
                        post_answers.setdefault(sid, []).append(data)
        except FileNotFoundError:
            log.debug("QuestionsScreen: events file not found")

        # Merge answers: match each PermissionRequest with the next PostToolUse from same session
        post_idx: dict[str, int] = {}
        for perm in perm_events:
            sid = perm.get("session_id", "")
            idx = post_idx.get(sid, 0)
            posts = post_answers.get(sid, [])
            if idx < len(posts):
                answers = posts[idx].get("tool_input", {}).get("answers", {})
                perm["_answers"] = answers
                post_idx[sid] = idx + 1

        return [self._format_question(d) for d in perm_events[-200:]]

    def _format_question(self, data: dict) -> str:
        ts = datetime.fromtimestamp(data.get("_timestamp", 0))
        t = ts.strftime("%Y-%m-%d %H:%M:%S")
        session = data.get("session_id", "?")[:8]
        cwd = data.get("cwd", "")
        project = os.path.basename(cwd) if cwd else ""
        decision = data.get("_decision", "allowed")
        excluded = data.get("_excluded_tool", False)

        # Decision badge
        if excluded:
            badge = "[bold red]MANUAL  [/]"
        elif decision == "deferred":
            badge = "[bold yellow]DEFERRED[/]"
        elif decision == "timeout":
            timeout_s = data.get("_ask_timeout", "?")
            badge = f"[bold cyan]TIMEOUT [/]"
        else:
            badge = "[bold green]ALLOWED [/]"

        detail = _format_ask_user_question_detail(data)
        detail_block = f"\n{detail}" if detail else ""

        return (
            f"[bold]{t}[/]  [{session}]  [bold]{project}[/]\n"
            f"  {badge} [bold]AskUserQuestion[/]{detail_block}\n"
        )

    def action_dismiss(self) -> None:
        self.app.pop_screen()


def _format_ask_user_question_inline(tool_input: dict) -> str:
    """Format AskUserQuestion tool_input as a readable inline string for the event log.

    Handles two formats:
    - Simple: tool_input has 'question' (str) directly
    - Structured: tool_input has 'questions' (list of dicts with 'question' and 'options')
      and 'answers' (dict mapping question text to selected answer)
    """
    parts = []
    questions = tool_input.get("questions", [])
    answers = tool_input.get("answers", {})

    if questions:
        for q in questions:
            q_text = q.get("question", "")
            options = q.get("options", [])
            selected = answers.get(q_text, "")
            option_labels = [o.get("label", "") for o in options if o.get("label")]
            if q_text:
                line = f" \"{q_text}\""
                if option_labels:
                    choices_str = " / ".join(option_labels)
                    line += f" [{choices_str}]"
                if selected:
                    line += f" -> [bold]{selected}[/]"
                parts.append(line)
    else:
        # Simple format with just 'question' key
        question = tool_input.get("question", "")
        if question:
            parts.append(f" \"{question[:200]}\"")
        elif tool_input:
            # Fallback: show first couple keys
            for k, v in list(tool_input.items())[:2]:
                parts.append(f" {k}={str(v)[:80]}")

    return "".join(parts) if parts else ""


def _format_ask_user_question_detail(data: dict) -> str:
    """Format AskUserQuestion event as a multi-line detail string for the QuestionsScreen."""
    tool_input = data.get("tool_input", {})
    questions = tool_input.get("questions", [])
    # Prefer merged answers from PostToolUse, fall back to tool_input answers
    answers = data.get("_answers", tool_input.get("answers", {}))
    decision = data.get("_decision", "allowed")
    is_auto = decision == "timeout"
    lines = []

    if questions:
        for i, q in enumerate(questions):
            q_text = q.get("question", "")
            options = q.get("options", [])
            selected = answers.get(q_text, "")
            if q_text:
                lines.append(f"    [dim]Q:[/] {q_text}")
            for o in options:
                label = o.get("label", "")
                desc = o.get("description", "")
                if label:
                    if selected and label == selected:
                        if is_auto:
                            marker = "[bold cyan]>>[/]"
                            lines.append(f"      {marker} [bold cyan]{label}[/]" + (f"  [dim]{desc}[/]" if desc else ""))
                        else:
                            marker = "[bold green]>>[/]"
                            lines.append(f"      {marker} [bold green]{label}[/]" + (f"  [dim]{desc}[/]" if desc else ""))
                    else:
                        lines.append(f"         {label}" + (f"  [dim]{desc}[/]" if desc else ""))
            if selected:
                mode = "[cyan]auto[/]" if is_auto else "[green]manual[/]"
                lines.append(f"    [dim]Answer:[/] [bold]{selected}[/]  ({mode})")
            if i < len(questions) - 1:
                lines.append("")
    else:
        question = tool_input.get("question", "")
        if question:
            lines.append(f"    [dim]Q:[/] {question[:300]}")
        elif tool_input:
            for k, v in list(tool_input.items())[:3]:
                lines.append(f"    [dim]{k}:[/] {str(v)[:200]}")

    return "\n".join(lines)


def _safe_css_id(session_id: str) -> str:
    return "panel-" + session_id.replace("-", "").replace(":", "").replace("/", "")


def _safe_tab_css_id(tab_id: str) -> str:
    return "tab-" + tab_id.replace("-", "").replace(":", "").replace("/", "").replace(".", "")


def _get_frame_size(node):
    """Get pixel (width, height) of an iTerm2 node from its frame."""
    if isinstance(node, Session):
        try:
            return node.frame.size.width, node.frame.size.height
        except Exception:
            log.debug(f"_get_node_size: failed to get frame size for session {node.session_id}")
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


def _build_widget_tree(node, self_session_id, panels, old_panels=None, old_dashboard=None, depth=0, settings=None):
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
            if settings:
                panel._bucket_secs = settings.sparkline_bucket_secs
            # Transfer dashboard state
            if old_dashboard:
                panel._start_time = old_dashboard._start_time
                panel.active_agents = dict(old_dashboard.active_agents)
                panel.total_agents_completed = old_dashboard.total_agents_completed
                panel.accept_count = old_dashboard.accept_count
                panel._event_buckets = old_dashboard._event_buckets
                panel._bucket_counter = old_dashboard._bucket_counter
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
                panel._state = old.state
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
            widget, dash = _build_widget_tree(child, self_session_id, panels, old_panels, old_dashboard, depth + 1, settings=settings)
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


class MonitorCommands(Provider):
    """Command palette provider for Claude Monitor actions."""

    _COMMANDS = [
        ("Toggle Auto/Manual", "action_toggle_pause", "Switch between auto-accept and manual mode"),
        ("Choices Log", "action_show_choices", "Review auto-accepted permission decisions"),
        ("Questions Log", "action_show_questions", "Review AskUserQuestion events"),
        ("Refresh Layout", "action_refresh_layout", "Re-fetch iTerm2 pane layout"),
        ("Settings", "action_open_settings", "Open settings screen"),
        ("Next Tab", "action_next_tab", "Switch to the next tab"),
        ("Previous Tab", "action_prev_tab", "Switch to the previous tab"),
        ("Quit", "action_quit", "Exit Claude Monitor"),
    ]

    async def discover(self) -> Hits:
        for name, action, help_text in self._COMMANDS:
            yield DiscoveryHit(name, getattr(self.app, action), help=help_text)

    async def search(self, query: str) -> Hits:
        matcher = self.matcher(query)
        for name, action, help_text in self._COMMANDS:
            score = matcher.match(name)
            if score > 0:
                yield Hit(score, matcher.highlight(name), getattr(self.app, action), help=help_text)


class FixedWidthSparkline(Sparkline):
    """Sparkline where each data point = exactly 1 column, right-aligned.

    Pads with zeros on the left so 1 data point = 1 column with no
    horizontal scaling. Data should be pre-normalized to 0.0–1.0.
    """

    def render(self):
        from fractions import Fraction
        from rich.color import Color
        from rich.segment import Segment
        from rich.style import Style
        from rich.color_triplet import ColorTriplet

        width = self.size.width
        height = self.size.height
        data = list(self.data or [])
        if len(data) > width:
            data = data[-width:]
        if len(data) < width:
            data = [0.0] * (width - len(data)) + data
        if len(data) < 2:
            data = [0.0, 0.0]

        _, base = self.background_colors
        min_color = base + (
            self.get_component_styles("sparkline--min-color").color
            if self.min_color is None
            else self.min_color
        )
        max_color = base + (
            self.get_component_styles("sparkline--max-color").color
            if self.max_color is None
            else self.max_color
        )

        # Render directly: data is pre-normalized 0.0-1.0, no re-scaling.
        BARS = "▁▂▃▄▅▆▇█"
        bar_segs_per_row = len(BARS)
        total_bar_segs = bar_segs_per_row * height - 1

        mc = min_color.rich_color
        xc = max_color.rich_color

        def _blend(ratio: float) -> Style:
            r1, g1, b1 = mc.triplet or ColorTriplet(0, 0, 0)
            r2, g2, b2 = xc.triplet or ColorTriplet(255, 255, 255)
            r = int(r1 + (r2 - r1) * ratio)
            g = int(g1 + (g2 - g1) * ratio)
            b = int(b1 + (b2 - b1) * ratio)
            return Style.from_color(Color.from_rgb(r, g, b))

        lines: list[list[Segment]] = []
        for row in reversed(range(height)):
            row_low = row * bar_segs_per_row
            row_high = (row + 1) * bar_segs_per_row
            segs: list[Segment] = []
            for val in data:
                bar_idx = int(val * total_bar_segs)
                bar_idx = min(bar_idx, total_bar_segs)
                if bar_idx < row_low:
                    segs.append(Segment(" "))
                elif bar_idx >= row_high:
                    segs.append(Segment("█", _blend(val)))
                else:
                    ch = BARS[bar_idx % bar_segs_per_row]
                    segs.append(Segment(ch, _blend(val)))
            lines.append(segs)

        from rich.console import RenderableType, ConsoleOptions, RenderResult as RR, Console
        class _Renderable:
            def __rich_console__(self_r, console: Console, options: ConsoleOptions) -> RR:
                for i, line in enumerate(lines):
                    yield from line
                    if i < len(lines) - 1:
                        yield Segment.line()

        return _Renderable()


class HalfBlockScrollBarRender(ScrollBarRender):
    """Base renderer that draws the thumb using the half-block glyph in bar color (no reverse)."""

    @classmethod
    def render_bar(cls, size=25, virtual_size=50, window_size=20, position=0,
                   thickness=1, vertical=True,
                   back_color=None, bar_color=None) -> "Segments":
        from rich.color import Color
        from rich.segment import Segment, Segments
        from rich.style import Style
        from math import ceil
        if back_color is None:
            back_color = Color.parse("#000000")
        if bar_color is None:
            bar_color = Color.parse("bright_magenta")
        bars = cls.VERTICAL_BARS if vertical else cls.HORIZONTAL_BARS
        len_bars = len(bars)
        width_thickness = thickness if vertical else 1
        blank = cls.BLANK_GLYPH * width_thickness
        foreground_meta = {"@mouse.down": "grab"}
        if window_size and size and virtual_size and size != virtual_size:
            bar_ratio = virtual_size / size
            thumb_size = max(1, window_size / bar_ratio)
            position_ratio = position / (virtual_size - window_size)
            position = (size - thumb_size) * position_ratio
            start = int(position * len_bars)
            end = start + ceil(thumb_size * len_bars)
            start_index, start_bar = divmod(max(0, start), len_bars)
            end_index, end_bar = divmod(max(0, end), len_bars)
            upper = {"@mouse.down": "scroll_up"}
            lower = {"@mouse.down": "scroll_down"}
            upper_back_segment = Segment(blank, Style(bgcolor=back_color, meta=upper))
            lower_back_segment = Segment(blank, Style(bgcolor=back_color, meta=lower))
            segments = [upper_back_segment] * int(size)
            segments[end_index:] = [lower_back_segment] * (size - end_index)
            # Thumb: use bar_color as foreground, back_color as background (no reverse)
            segments[start_index:end_index] = [
                Segment(blank, Style(color=bar_color, bgcolor=back_color, meta=foreground_meta))
            ] * (end_index - start_index)
            # Fractional end caps
            if start_index < len(segments):
                bar_character = bars[len_bars - 1 - start_bar]
                if bar_character != " ":
                    segments[start_index] = Segment(
                        bar_character * width_thickness,
                        Style(color=bar_color, bgcolor=back_color, meta=foreground_meta),
                    )
            if end_index < len(segments):
                bar_character = bars[len_bars - 1 - end_bar]
                if bar_character != " ":
                    segments[end_index] = Segment(
                        bar_character * width_thickness,
                        Style(color=bar_color, bgcolor=back_color, meta=foreground_meta),
                    )
        else:
            segments = [Segment(blank, Style(bgcolor=back_color))] * int(size)
        if vertical:
            return Segments(segments, new_lines=True)
        else:
            return Segments((segments + [Segment.line()]) * thickness, new_lines=False)


class HorizontalScrollBarRender(HalfBlockScrollBarRender):
    BLANK_GLYPH = "▄"
    HORIZONTAL_BARS = [" ", " ", " ", " ", " ", " ", " ", " "]  # disable fractional end caps


class VerticalScrollBarRender(HalfBlockScrollBarRender):
    BLANK_GLYPH = "▐"
    VERTICAL_BARS = ["▐", "▐", "▐", "▐", "▐", "▐", "▐", " "]  # disable fractional end caps


class AutoAcceptTUI(App):
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
        ("a", "toggle_pause", "Auto/Manual"),
        ("shift+tab", "toggle_pause", "Auto/Manual"),
        ("c", "show_choices", "Choices"),
        ("u", "show_questions", "Questions"),
        ("r", "refresh_layout", "Refresh"),
        ("s", "open_settings", "Settings"),
        ("right_square_bracket", "next_tab", "Next Tab"),
        ("left_square_bracket", "prev_tab", "Prev Tab"),
        ("q", "quit", "Quit"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self.settings = load_settings()
        self.panels: dict[str, SessionPanel] = {}
        self.dashboard: DashboardPanel | None = None
        self._iterm_to_panel: dict[str, str] = {}
        self._stop_event = threading.Event()
        self._rebuilding = False
        self._current_structure_fp = None
        self._current_size_fp = None
        self._usage_polling = False
        self._last_usage_data = None
        self._usage_next_fetch: float = 0  # epoch time of next usage poll
        self._api_server = None
        self._global_paused: bool = False
        self._paused_sessions: set[str] = set()
        self._ask_paused_sessions: set[str] = set()  # panes with AskUserQuestion paused
        self._tab_original_names: dict[str, str] = {}  # tab_id → original tab name
        self._tab_session_ids: dict[str, set[str]] = {}  # tab_id → set of session_ids in that tab
        self._tab_title_lock = threading.Lock()  # prevents concurrent _set_tab_titles_sync calls
        self._tab_title_pending = False  # coalesces rapid update_tab_titles calls

    @property
    def paused(self) -> bool:
        return self._global_paused

    def is_pane_paused(self, iterm_sid: str) -> bool:
        return self._global_paused or iterm_sid in self._paused_sessions

    def is_ask_paused(self, iterm_sid: str) -> bool:
        return iterm_sid in self._ask_paused_sessions

    def get_state_snapshot(self) -> dict:
        """Return a serializable dict of the full TUI state for the API.

        This is the single public interface used by the HTTP API /text endpoint,
        avoiding direct access to private panel/app attributes.
        """
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
            total_agents_active = sum(len(p.active_agents) for p in self.panels.values()) + len(d.active_agents)
            total_agents_done = sum(p.total_agents_completed for p in self.panels.values()) + d.total_agents_completed
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

    def _update_all_panel_modes(self) -> None:
        for panel in self.panels.values():
            if self.is_pane_paused(panel.session_id):
                panel.add_class("pane-paused")
            else:
                panel.remove_class("pane-paused")

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
            self._current_structure_fp = _structure_fingerprint(_layout_tabs)
            self._current_size_fp = _size_fingerprint(_layout_tabs)
            log.debug(f"on_mount(): panels={list(self.panels.keys())}, dashboard={self.dashboard is not None}")
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

    async def _mount_tabs(self, root, tabs, self_session_id, old_panels=None, old_dashboard=None):
        """Mount tab layout into a container. Handles single-tab and multi-tab cases."""
        # Record original tab names and which sessions belong to each tab
        for tab_id, tab_name, tree in tabs:
            if tab_id not in self._tab_original_names:
                # Strip any stacked " [N]" or " [N/N]" suffixes we may have set previously
                clean_name = re.sub(r'( \[\d+(/\d+)?\])+$', '', tab_name)
                self._tab_original_names[tab_id] = clean_name
            self._tab_session_ids[tab_id] = _collect_session_ids(tree)

        if len(tabs) == 1:
            # Single tab — render directly without tab wrapper
            _tab_id, _tab_name, tree = tabs[0]
            layout, dash = _build_widget_tree(
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
                layout, dash = _build_widget_tree(
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
                    log.warning(f"_build_widget_tree: failed to replay event log for panel {panel.session_id}")
        if self.dashboard and self.dashboard._event_log:
            try:
                rl = self.dashboard.query_one(RichLog)
                for line in self.dashboard._event_log:
                    rl.write(line)
            except Exception:
                log.warning("_build_widget_tree: failed to replay event log for dashboard")

    def _tick_status(self) -> None:
        """Refresh all panel status bars, dashboard, and top bar every second."""
        for panel in self.panels.values():
            panel._update_status()
        if self.dashboard:
            self.dashboard.refresh_dashboard(self.panels)
        self._update_status_bar()

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
                # Compute tab titles and pass them to the layout fetch so both
                # operations share a single websocket connection.
                pending_titles = self._compute_tab_titles() if self._tab_original_names and not self._rebuilding else None
                tabs, self_sid, win_groups = _fetch_layout_sync(tab_titles=pending_titles)
                tabs = _filter_tabs_by_scope(tabs, self_sid, self.settings.iterm_scope, win_groups)
                if tabs:
                    new_struct = _structure_fingerprint(tabs)
                    if new_struct != self._current_structure_fp:
                        log.debug("watch_layout: structure changed")
                        self.post_message(LayoutChanged(tabs, self_sid))
                    else:
                        new_size = _size_fingerprint(tabs)
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
            await self._mount_tabs(root, msg.tabs, msg.self_session_id,
                                   old_panels=old_panels, old_dashboard=old_dashboard)

            # Restore active tab and focused panel after rebuild
            if active_tab_id:
                try:
                    tc = self.query_one("#tab-content", TabbedContent)
                    tc.active = active_tab_id
                except Exception:
                    log.warning(f"on_layout_changed: failed to restore active tab {active_tab_id}")
            if focused_session_id and focused_session_id in self.panels:
                try:
                    self.panels[focused_session_id].focus()
                except Exception:
                    log.debug(f"on_layout_changed: failed to restore focus to panel {focused_session_id}")

            self._current_structure_fp = _structure_fingerprint(msg.tabs)
            self._current_size_fp = _size_fingerprint(msg.tabs)

            # Preserve iterm→panel mappings for sessions that still exist
            self._iterm_to_panel = {
                k: v for k, v in self._iterm_to_panel.items()
                if v in self.panels
            }
        finally:
            self._rebuilding = False

        self._update_all_panel_modes()
        self.update_tab_titles()
        log.debug(f"on_layout_changed: done. panels={list(self.panels.keys())}, dashboard={self.dashboard is not None}")

    def on_layout_resized(self, msg: LayoutResized) -> None:
        """Update widget sizes without rebuilding when only pane sizes changed."""
        self._current_size_fp = _size_fingerprint(msg.tabs)
        for _tab_id, _tab_name, root in msg.tabs:
            self._apply_sizes(root)

    def _apply_sizes(self, node, parent_vertical=None):
        """Walk iTerm2 tree and update CSS sizes on matching existing widgets."""
        if isinstance(node, Session):
            return
        if not isinstance(node, Splitter):
            return
        child_sizes = [_get_frame_size(c) for c in node.children]
        if node.vertical:
            total = sum(w for w, _ in child_sizes) or 1
            fractions = [w / total for w, _ in child_sizes]
        else:
            total = sum(h for _, h in child_sizes) or 1
            fractions = [h / total for _, h in child_sizes]
        for i, child in enumerate(node.children):
            pct = round(fractions[i] * 100)
            if isinstance(child, Session):
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

    # --- Hook event handling ---

    @staticmethod
    def _iterm_sid_from_event(data: dict) -> str:
        """Extract the normalized iTerm2 session ID from a hook event dict."""
        return extract_iterm_session_id(data.get("_iterm_session_id") or "")

    def _resolve_panel(self, data: dict) -> SessionPanel | None:
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
            log.warning(f"_create_fallback_panel: layout-root mount failed for {claude_sid}, falling back to status-bar mount")
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

    def _format_ts(self, ts: datetime) -> str:
        """Format a timestamp according to the timestamp_style setting."""
        style = self.settings.timestamp_style
        if style == "12hr":
            return ts.strftime("%-I:%M:%S%p").lower()
        if style == "date_time":
            return ts.strftime("%Y-%m-%d %H:%M:%S")
        # 24hr and auto
        return ts.strftime("%H:%M:%S")

    @staticmethod
    def _oneline(text: str, max_len: int = 0) -> str:
        """Collapse multi-line text into one line, replacing newlines with ↵."""
        joined = " ↵ ".join(line.strip() for line in text.splitlines() if line.strip())
        return joined[:max_len] if max_len else joined

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
                if panel._pending_timeout is not None and getattr(panel, "_timeout_origin", None) == origin:
                    panel._pending_timeout = None
                    panel._timeout_origin = None
                    data["_auto_accepted"] = True  # Signal to _format_event
            elif ntype == "permission_prompt":
                iterm_sid = self._iterm_sid_from_event(data)
                if iterm_sid and iterm_sid != _self_session_id and not self.is_pane_paused(iterm_sid):
                    # Skip auto-approve if panel has a pending AskUserQuestion timeout
                    pending = getattr(panel, "_pending_timeout", None)
                    if pending and pending > time.time():
                        log.debug(f"Skipping auto-approve: AskUserQuestion timeout pending for {iterm_sid}")
                    else:
                        panel.accept_count += 1
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
        """Format a hook event into display text.  Pure formatting — no side effects.

        Returns (label, detail) or (None, None).
        """
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
            # Check event-level flags first, then live pane state
            if data.get("_excluded_tool"):
                return f"[bold red]{'MANUAL':<8}[/]", f"{tool}{detail}"
            decision = data.get("_decision", "allowed")
            if decision == "deferred":
                return f"[bold yellow]{'DEFERRED':<8}[/]", f"{tool}{detail}"
            if decision == "timeout":
                timeout_s = data.get("_ask_timeout", "?")
                return f"[bold cyan]{'TIMEOUT':<8}[/]", f"{tool}{detail} ({timeout_s}s)"
            iterm_sid = self._iterm_sid_from_event(data)
            if iterm_sid and self.is_pane_paused(iterm_sid):
                return f"[bold yellow]{'PAUSED':<8}[/]", f"{tool}{detail}"
            return f"[bold green]{'ALLOWED':<8}[/]", f"{tool}{detail}"

        elif event_name == "PostToolUse":
            tool = data.get("tool_name", "?")
            if tool == "AskUserQuestion":
                answers = data.get("tool_input", {}).get("answers", {})
                answer_vals = [v for v in answers.values() if v]
                if not answer_vals:
                    return None, None  # Auto-accepted with no real answer
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
                # Already answered manually or origin mismatch — suppress
                return None, None
            elif ntype == "permission_prompt":
                iterm_sid = self._iterm_sid_from_event(data)
                if iterm_sid and iterm_sid != _self_session_id and not self.is_pane_paused(iterm_sid):
                    # Don't show APPROVED when AskUserQuestion timeout is pending
                    panel = self._resolve_panel(data)
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

    # --- Auto-approve via iTerm2 keystroke ---

    @work(thread=True, exit_on_error=False)
    def _send_approve(self, session_id: str) -> None:
        """Send Enter to an iTerm2 session to approve a permission prompt.

        Uses \\r (carriage return) because Claude Code reads raw terminal input
        where Enter generates CR, not LF.
        """
        ok = _send_keystroke_sync(session_id, "\r")
        log.debug(f"_send_approve: session={session_id[:8]} ok={ok}")

    # --- Tab title updates ---

    def _compute_tab_titles(self) -> dict[str, str]:
        """Compute tab titles with active/total session counts. Pure computation, no I/O."""
        tab_titles: dict[str, str] = {}
        for tab_id, original_name in self._tab_original_names.items():
            session_ids = self._tab_session_ids.get(tab_id, set())
            total_count = sum(1 for sid in session_ids if sid in self.panels)
            active_count = sum(
                1 for sid in session_ids
                if sid in self.panels and (
                    self.panels[sid]._state == "active" or len(self.panels[sid].active_agents) > 0
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
        counts: dict[str, tuple[int, int]] = {}
        for tab_id, original_name in self._tab_original_names.items():
            session_ids = self._tab_session_ids.get(tab_id, set())
            total_count = sum(1 for sid in session_ids if sid in self.panels)
            active_count = sum(
                1 for sid in session_ids
                if sid in self.panels and (
                    self.panels[sid]._state == "active" or len(self.panels[sid].active_agents) > 0
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
                _set_tab_titles_sync(self._compute_tab_titles())
                self.call_from_thread(self._update_textual_tab_labels)
                if not self._tab_title_pending:
                    break
        finally:
            self._tab_title_lock.release()

    def on_resize(self) -> None:
        """Re-trim Textual tab labels when the terminal is resized."""
        self._update_textual_tab_labels()

    # --- Keybindings ---

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
            tabs, self_sid, win_groups = _fetch_layout_sync()
            tabs = _filter_tabs_by_scope(tabs, self_sid, self.settings.iterm_scope, win_groups)
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

        def _restore():
            if error:
                try:
                    self.query_one("#status-left", Static).update("REFRESH FAILED \u2014 iTerm2 not reachable")
                    self.query_one("#status-bar", Horizontal).set_classes("paused")
                except Exception:
                    log.warning("_do_refresh: failed to update status bar with refresh failure")
            else:
                self._update_status_bar()

        self.call_from_thread(_restore)

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

    def action_show_choices(self) -> None:
        """Open the permission choices review screen."""
        self.push_screen(ChoicesScreen())

    def action_show_questions(self) -> None:
        """Open the AskUserQuestion review screen."""
        self.push_screen(QuestionsScreen())

    def action_next_tab(self) -> None:
        """Switch to the next tab."""
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
        """Switch to the previous tab."""
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
        """Open the settings modal."""
        self.push_screen(SettingsScreen(self.settings), self._on_settings_closed)

    def _on_settings_closed(self, result: Settings | None) -> None:
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

    def _apply_settings(self, settings: Settings) -> None:
        """Apply settings to the running app."""
        self.theme = settings.theme
        # Debug logging level
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.DEBUG if settings.debug else logging.WARNING)
        # Push OAuth tokens to usage module
        set_oauth_json(settings.oauth_json)
        set_on_token_refreshed(self._on_token_refreshed)
        # Start usage polling if newly enabled
        if settings.account_usage and not self._usage_polling:
            self._usage_polling = True
            self.poll_usage()
        # If usage disabled, clear usage from status bar
        if not settings.account_usage and self._last_usage_data:
            self._last_usage_data = None
            self._update_status_bar()
        # Persist excluded_tools and ask_user_timeout to state.json for the hook
        self._save_state()

    def _on_token_refreshed(self, token: str, refresh_token: str, expires_at: float) -> None:
        """Called from usage module when OAuth token is refreshed. May run in a background thread."""
        # Update oauth_json in settings if it was the token source
        if self.settings.oauth_json:
            oauth_data = {"access_token": token, "refresh_token": refresh_token, "expires_at": expires_at}
            self.settings.oauth_json = json.dumps(oauth_data)
            save_settings(self.settings)
            set_oauth_json(self.settings.oauth_json)
        # Log to dashboard
        ts = self._format_ts(datetime.now().astimezone())
        expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc).astimezone()
        msg = f"[{ts}] [dim]OAuth token refreshed, expires {expires_dt.strftime('%H:%M:%S')}[/]"

        def _log():
            if self.dashboard:
                self.dashboard.record_event(msg)

        self.call_from_thread(_log)

    def _update_status_bar(self) -> None:
        """Update the status bar to reflect current pause state, usage, version, and clock."""
        try:
            bar = self.query_one("#status-bar", Horizontal)
            left = self.query_one("#status-left", Static)
            right = self.query_one("#status-right", Static)
            SEP = "  [dim]\u2502[/]  "

            n_paused = sum(1 for p in self.panels.values() if self.is_pane_paused(p.session_id))
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
        """Ensure background threads don't prevent exit."""
        self._stop_event.set()

    # --- Event file watcher ---

    @work(thread=True, exit_on_error=False)
    def watch_events(self) -> None:
        os.makedirs(SIGNAL_DIR, exist_ok=True)
        from pathlib import Path
        Path(EVENTS_FILE).touch(exist_ok=True)

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
                            log.debug(f"_tail_events: failed to parse JSON line: {line[:100]}")
                else:
                    self._stop_event.wait(0.2)
        log.debug("watch_events: stopped")

    # --- Usage polling ---

    @work(thread=True, exit_on_error=False)
    def poll_usage(self) -> None:
        """Poll usage every 5 minutes (matches API cache TTL)."""
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
        """One-shot usage fetch in a background thread."""
        self._last_usage_data = fetch_usage()
        self._usage_next_fetch = time.time() + 300
        self.call_from_thread(self._update_status_bar)

    # --- API server ---

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
