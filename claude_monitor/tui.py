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
from datetime import datetime, timezone

try:
    import iterm2
    from iterm2.session import Splitter, Session
    ITERM2_AVAILABLE = True
except ImportError:
    iterm2 = None  # type: ignore[assignment]
    Splitter = None  # type: ignore[assignment]
    Session = None  # type: ignore[assignment]
    ITERM2_AVAILABLE = False
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.widgets import Footer, RichLog, Static, TabbedContent, TabPane

from claude_monitor import __version__, SIGNAL_DIR, EVENTS_FILE, STATE_FILE, LOG_FILE, API_PORT_FILE, extract_iterm_session_id, fmt_duration, read_state
from claude_monitor.tui_common import (
    HookEvent,
    HorizontalScrollBarRender, VerticalScrollBarRender,
    FixedWidthSparkline,
    SessionPanel, DashboardPanel,
    PaneContextMenu, ChoicesScreen, QuestionsScreen,
    MonitorCommands,
    _safe_css_id, _safe_tab_css_id,
    _format_ask_user_question_inline, _format_ask_user_question_detail,
    _oneline as _oneline_fn,
)
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


# --- Layout change messages (iTerm2-specific, stay in tui.py) ---

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


# SessionPanel, DashboardPanel, PaneContextMenu, ChoicesScreen, QuestionsScreen,
# FixedWidthSparkline, HalfBlockScrollBarRender, HorizontalScrollBarRender,
# VerticalScrollBarRender, _safe_css_id, _safe_tab_css_id,
# _format_ask_user_question_inline, _format_ask_user_question_detail
# — all imported from claude_monitor.tui_common above.

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
    import sys

    simple_mode = "--simple" in sys.argv or not os.environ.get("ITERM_SESSION_ID")

    if simple_mode:
        try:
            from claude_monitor.tui_simple import SimpleTUI
        except ImportError:
            print("Error: SimpleTUI not yet implemented.")
            print("tui_simple.py does not exist yet.")
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
