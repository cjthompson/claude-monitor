"""iTerm2 layout helpers for claude-monitor.

Provides:
- LayoutFetcher: fetch iTerm2 pane layout (persistent websocket connection)
- LayoutFingerprint: compute structure/size fingerprints for change detection
- WidgetTreeBuilder: convert iTerm2 Splitter tree to Textual widgets
- KeystrokeSender: send keystrokes to iTerm2 sessions
- filter_tabs_by_scope: filter tabs by iTerm scope setting

All iTerm2 API calls are routed through a single persistent websocket
connection managed by a daemon thread to avoid repeated connection
overhead and FD leaks.
"""

from __future__ import annotations

import asyncio as _asyncio
import logging
import os
import threading
from typing import TYPE_CHECKING

try:
    import iterm2
    from iterm2.session import Splitter, Session
    ITERM2_AVAILABLE = True
except ImportError:
    iterm2 = None  # type: ignore[assignment]
    Splitter = None  # type: ignore[assignment]
    Session = None  # type: ignore[assignment]
    ITERM2_AVAILABLE = False

from textual.containers import Horizontal, Vertical
from textual.widgets import Static

from claude_monitor import extract_iterm_session_id
from claude_monitor.formatting import _safe_css_id

if TYPE_CHECKING:
    from claude_monitor.settings import Settings
    from claude_monitor.widgets import SessionPanel, DashboardPanel

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Persistent iTerm2 websocket connection
# ---------------------------------------------------------------------------
#
# A single websocket stays open for the lifetime of the app.  All iTerm2
# operations (layout fetch, tab title set, keystroke send) are scheduled
# on its event loop via asyncio.run_coroutine_threadsafe().

_iterm2_loop: _asyncio.AbstractEventLoop | None = None
_iterm2_app: "iterm2.App | None" = None  # type: ignore[name-defined]
_iterm2_ready = threading.Event()


def start_persistent_connection() -> None:
    """Launch a daemon thread that holds a persistent iTerm2 websocket."""
    def _run() -> None:
        global _iterm2_loop, _iterm2_app
        conn = iterm2.Connection()

        async def _init(connection: "iterm2.Connection") -> None:  # type: ignore[name-defined]
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


def _iterm2_call(coro_func, timeout: int = 10):
    """Schedule an async callable on the persistent iTerm2 connection.

    *coro_func* receives the iterm2.App and returns a result.
    Blocks the calling thread until the result is ready (or timeout).
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


# ---------------------------------------------------------------------------
# LayoutFetcher
# ---------------------------------------------------------------------------

class LayoutFetcher:
    """Fetch iTerm2 pane layout via the persistent websocket connection."""

    @staticmethod
    def fetch_sync(
        tab_titles: "dict[str, str] | None" = None,
    ) -> "tuple[list[tuple], str | None, dict]":
        """Fetch the current iTerm2 layout.

        Optionally sets *tab_titles* (tab_id -> title) in the same round-trip.
        Returns ``(tabs, self_session_id, window_groups)`` where *tabs* is a
        list of ``(tab_id, tab_name, root_splitter)`` tuples.
        """
        async def _do(app: "iterm2.App") -> "tuple[list, dict]":  # type: ignore[name-defined]
            tabs = []
            window_groups: dict[str, list[str]] = {}
            for window in app.terminal_windows:
                win_tab_ids: list[str] = []
                for tab in window.tabs:
                    if tab.root:
                        try:
                            tab_name = await tab.async_get_variable("title") or "Tab"
                        except Exception:
                            log.debug(
                                "_fetch_layout_sync: failed to get tab title, defaulting to 'Tab'"
                            )
                            tab_name = "Tab"
                        tabs.append((tab.tab_id, tab_name, tab.root))
                        win_tab_ids.append(tab.tab_id)
                    if tab_titles and tab.tab_id in tab_titles:
                        try:
                            await tab.async_set_title(tab_titles[tab.tab_id])
                        except Exception:
                            log.debug(
                                f"_fetch_layout_sync: failed to set title for tab {tab.tab_id}"
                            )
                if win_tab_ids:
                    window_groups[window.window_id] = win_tab_ids
            return tabs, window_groups

        raw = os.environ.get("ITERM_SESSION_ID", "")
        self_sid = extract_iterm_session_id(raw)

        result = _iterm2_call(_do)
        if result:
            return result[0], self_sid, result[1]
        return [], self_sid, {}


# ---------------------------------------------------------------------------
# LayoutFingerprint
# ---------------------------------------------------------------------------

class LayoutFingerprint:
    """Compute structure and size fingerprints from iTerm2 layout data."""

    @staticmethod
    def structure(tabs: list) -> tuple:
        """Structural fingerprint: session IDs, tab IDs, split directions.

        Changes when panes are added, removed, or rearranged.
        Tab names are excluded to avoid spurious rebuilds from dynamic titles.
        """
        return tuple(
            (tab_id, LayoutFingerprint._structure_node(root))
            for tab_id, _tab_name, root in tabs
        )

    @staticmethod
    def _structure_node(node) -> tuple:
        if isinstance(node, Session):
            return ("session", node.session_id)
        elif isinstance(node, Splitter):
            children = tuple(LayoutFingerprint._structure_node(c) for c in node.children)
            return ("split", node.vertical, children)
        return ()

    @staticmethod
    def size(tabs: list) -> tuple:
        """Size fingerprint: pixel dimensions of all panes.

        Changes on terminal resize but not on add/remove.
        """
        return tuple(LayoutFingerprint._size_node(root) for _, _, root in tabs)

    @staticmethod
    def _size_node(node) -> tuple:
        if isinstance(node, Session):
            w, h = LayoutFingerprint._frame_size(node)
            return (int(w), int(h))
        elif isinstance(node, Splitter):
            return tuple(LayoutFingerprint._size_node(c) for c in node.children)
        return ()

    @staticmethod
    def _frame_size(node) -> "tuple[float, float]":
        """Return pixel (width, height) of an iTerm2 node."""
        if isinstance(node, Session):
            try:
                return node.frame.size.width, node.frame.size.height
            except Exception:
                log.debug(
                    f"_get_node_size: failed to get frame size for session {node.session_id}"
                )
                return 0, 0
        elif isinstance(node, Splitter):
            if not node.children:
                return 0, 0
            sizes = [LayoutFingerprint._frame_size(c) for c in node.children]
            if node.vertical:  # side-by-side → sum widths, max heights
                return sum(w for w, _ in sizes), max(h for _, h in sizes)
            else:  # stacked → max widths, sum heights
                return max(w for w, _ in sizes), sum(h for _, h in sizes)
        return 0, 0


# Expose _frame_size as module-level helper for WidgetTreeBuilder
def _get_frame_size(node) -> "tuple[float, float]":
    return LayoutFingerprint._frame_size(node)


# ---------------------------------------------------------------------------
# WidgetTreeBuilder
# ---------------------------------------------------------------------------

class WidgetTreeBuilder:
    """Convert an iTerm2 Splitter tree into a Textual widget tree."""

    @staticmethod
    def build(
        node,
        self_session_id: "str | None",
        panels: "dict[str, SessionPanel]",
        old_panels: "dict[str, SessionPanel] | None" = None,
        old_dashboard: "DashboardPanel | None" = None,
        depth: int = 0,
        settings: "Settings | None" = None,
    ) -> "tuple[object, DashboardPanel | None]":
        """Convert *node* (Session or Splitter) to Textual widgets.

        State from *old_panels* / *old_dashboard* is transferred to newly
        created widgets so event logs survive a layout rebuild.

        Returns ``(root_widget, dashboard_or_None)``.
        """
        # Defer imports to avoid circular dependencies at module load time.
        from claude_monitor.widgets import SessionPanel, DashboardPanel

        indent = "  " * depth

        if isinstance(node, Session):
            is_self = node.session_id == self_session_id
            css_id = _safe_css_id(node.session_id)
            if is_self:
                panel: DashboardPanel = DashboardPanel(id=css_id)
                if settings:
                    panel._bucket_secs = settings.sparkline_bucket_secs
                if old_dashboard:
                    panel._start_time = old_dashboard._start_time
                    panel.active_agents = dict(old_dashboard.active_agents)
                    panel.total_agents_completed = old_dashboard.total_agents_completed
                    panel.accept_count = old_dashboard.accept_count
                    panel.tool_counts = dict(old_dashboard.tool_counts)
                    panel._event_buckets = old_dashboard._event_buckets
                    panel._bucket_counter = old_dashboard._bucket_counter
                    panel._current_bucket_count = old_dashboard._current_bucket_count
                    panel._event_log = list(old_dashboard._event_log)
                log.debug(
                    f"{indent}Session {node.session_id[:8]} (SELF/TUI) -> DashboardPanel id={css_id}"
                )
                return panel, panel
            else:
                name = node.name or "Session"
                sid_short = node.session_id[:8]
                sp: SessionPanel = SessionPanel(
                    node.session_id, f"{name} [{sid_short}]", id=css_id
                )
                if old_panels and node.session_id in old_panels:
                    old = old_panels[node.session_id]
                    sp.active_agents = dict(old.active_agents)
                    sp.accept_count = old.accept_count
                    sp.total_agents_completed = old.total_agents_completed
                    sp._start_time = old._start_time
                    sp._last_event_time = old._last_event_time
                    sp._state = old.state
                    sp._event_log = list(old._event_log)
                panels[node.session_id] = sp
                log.debug(
                    f"{indent}Session {node.session_id[:8]} {name!r} -> SessionPanel id={css_id}"
                )
                return sp, None

        elif isinstance(node, Splitter):
            Container = Horizontal if node.vertical else Vertical
            cname = "Horizontal" if node.vertical else "Vertical"
            child_sizes = [_get_frame_size(child) for child in node.children]
            if node.vertical:
                total = sum(w for w, _ in child_sizes) or 1
                fractions = [w / total for w, _ in child_sizes]
            else:
                total = sum(h for _, h in child_sizes) or 1
                fractions = [h / total for _, h in child_sizes]

            log.debug(
                f"{indent}{cname} ({len(node.children)} children, "
                f"fractions={[f'{f:.0%}' for f in fractions]})"
            )
            dashboard_ref = None
            children = []
            for i, child in enumerate(node.children):
                widget, dash = WidgetTreeBuilder.build(
                    child,
                    self_session_id,
                    panels,
                    old_panels=old_panels,
                    old_dashboard=old_dashboard,
                    depth=depth + 1,
                    settings=settings,
                )
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


# ---------------------------------------------------------------------------
# KeystrokeSender
# ---------------------------------------------------------------------------

class KeystrokeSender:
    """Send keystrokes to iTerm2 sessions via the persistent websocket."""

    @staticmethod
    def send_text(session_id: str, text: str) -> bool:
        """Send *text* to the iTerm2 session identified by *session_id*.

        Returns ``True`` on success, ``False`` if the session was not found
        or the connection is unavailable.
        """
        async def _do(app: "iterm2.App") -> bool:  # type: ignore[name-defined]
            session = app.get_session_by_id(session_id)
            if session:
                await session.async_send_text(text)
                return True
            return False

        return _iterm2_call(_do) or False

    @staticmethod
    def send_approve(session_id: str) -> bool:
        """Send a carriage return to *session_id* to approve a permission prompt.

        Uses ``\\r`` (carriage return) because Claude Code reads raw terminal
        input where Enter generates CR, not LF.
        """
        return KeystrokeSender.send_text(session_id, "\r")


# ---------------------------------------------------------------------------
# Tab title helpers
# ---------------------------------------------------------------------------

def set_tab_titles(tab_titles: "dict[str, str]") -> None:
    """Set iTerm2 tab titles using the persistent connection."""
    if not tab_titles:
        return

    async def _do(app: "iterm2.App") -> None:  # type: ignore[name-defined]
        for window in app.terminal_windows:
            for tab in window.tabs:
                if tab.tab_id in tab_titles:
                    try:
                        await tab.async_set_title(tab_titles[tab.tab_id])
                    except Exception:
                        log.debug(
                            f"set_tab_titles: failed to set title for tab {tab.tab_id}"
                        )

    _iterm2_call(_do)


# ---------------------------------------------------------------------------
# Scope filtering
# ---------------------------------------------------------------------------

def collect_session_ids(node) -> set:
    """Extract all session IDs from an iTerm2 Splitter/Session tree."""
    if isinstance(node, Session):
        return {node.session_id}
    elif isinstance(node, Splitter):
        ids: set = set()
        for child in node.children:
            ids |= collect_session_ids(child)
        return ids
    return set()


def filter_tabs_by_scope(
    tabs: list,
    self_sid: "str | None",
    scope: str,
    window_groups: "dict | None" = None,
) -> list:
    """Filter *tabs* to those visible under *scope*.

    *scope* is one of ``"current_tab"``, ``"current_window"``, or
    ``"all_windows"``.  Falls back to returning all tabs when the TUI's own
    pane cannot be located.
    """
    if scope == "all_windows" or not self_sid:
        return tabs

    # Find the tab containing the TUI's own session
    self_tab_id = None
    for tab_id, _tab_name, root in tabs:
        if self_sid in collect_session_ids(root):
            self_tab_id = tab_id
            break

    if not self_tab_id:
        return tabs  # can't determine our tab, show everything

    if scope == "current_tab":
        return [(tid, tn, r) for tid, tn, r in tabs if tid == self_tab_id]

    if scope == "current_window" and window_groups:
        for _win_id, win_tab_ids in window_groups.items():
            if self_tab_id in win_tab_ids:
                allowed = set(win_tab_ids)
                return [(tid, tn, r) for tid, tn, r in tabs if tid in allowed]

    return tabs
