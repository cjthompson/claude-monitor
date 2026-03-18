"""Shared base class for claude-monitor TUI applications.

``MonitorApp`` captures the logic that is identical (or nearly identical) between
``tui.py`` (``AutoAcceptTUI``) and ``tui_simple.py`` (``SimpleTUI``):

* Shared instance-variable initialisation
* Status-bar rendering
* Settings application and OAuth token refresh
* Usage polling
* Event-file tailing
* HTTP API server
* Verbatim ``action_*`` implementations that both subclasses share
* Abstract hooks that each subclass implements differently

This is Phase 1 of the TUI consolidation plan.  Neither ``tui.py`` nor
``tui_simple.py`` is modified here; those updates come in Phase 3 once the
base class is proven stable.
"""

from __future__ import annotations

import abc
import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from textual import work
from textual.app import App
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Static

from claude_monitor import (
    __version__,
    SIGNAL_DIR,
    EVENTS_FILE,
    STATE_FILE,
    API_PORT_FILE,
    read_state,
)
from claude_monitor.messages import HookEvent
from claude_monitor.screens import ChoicesScreen, QuestionsScreen, HelpScreen
from claude_monitor.widgets import SessionPanel, DashboardPanel
from claude_monitor.api import start_api_server
from claude_monitor.settings import Settings, SettingsScreen, load_settings, save_settings
from claude_monitor.usage import (
    fetch_usage,
    format_usage_inline,
    invalidate_usage_cache,
    set_oauth_json,
    set_on_token_refreshed,
)

log = logging.getLogger(__name__)


class MonitorApp(App):
    """Abstract base class shared by AutoAcceptTUI and SimpleTUI.

    Subclasses MUST implement the abstract methods below.  They MAY override
    any of the non-abstract methods, but should call ``super()`` where
    appropriate (particularly ``__init__`` and ``on_mount``-level helpers).
    """

    # ------------------------------------------------------------------
    # Initialisation
    # ------------------------------------------------------------------

    def __init__(self) -> None:
        super().__init__()

        # Settings (subclass may override after calling super().__init__())
        self.settings: Settings = load_settings()

        # Session panels keyed by session ID (iTerm2 UUID in tui.py,
        # Claude session ID in tui_simple.py).
        self.panels: dict[str, SessionPanel] = {}

        # Dashboard panel reference (set during compose/mount)
        self.dashboard: DashboardPanel | None = None

        # Stop signal for background worker threads
        self._stop_event = threading.Event()

        # Pause state
        self._global_paused: bool = False

        # Usage polling
        self._usage_polling: bool = False
        self._last_usage_data = None
        self._usage_next_fetch: float = 0

        # HTTP API server handle
        self._api_server = None

    # ------------------------------------------------------------------
    # Abstract interface — subclasses MUST implement these
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def is_pane_paused(self, sid: str) -> bool:
        """Return True if the pane identified by *sid* is in manual mode."""

    @abc.abstractmethod
    def is_ask_paused(self, sid: str) -> bool:
        """Return True if AskUserQuestion auto-accept is paused for *sid*."""

    # ------------------------------------------------------------------
    # Pause state — shared property
    # ------------------------------------------------------------------

    @property
    def paused(self) -> bool:
        """True when global manual mode is active."""
        return self._global_paused

    # ------------------------------------------------------------------
    # State snapshot (used by HTTP API /text endpoint)
    # ------------------------------------------------------------------

    def get_state_snapshot(self) -> dict[str, object]:
        """Return a serialisable dict of the full TUI state for the API.

        Called from the HTTP API thread — snapshots panels dict upfront to
        avoid RuntimeError if the main thread adds/removes panels concurrently.
        """
        # Snapshot before iterating — main thread can modify panels at any time.
        panels_snapshot = list(self.panels.items())
        panels_values = [p for _, p in panels_snapshot]

        sessions = []
        for sid, panel in panels_snapshot:
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
            total_accepted = (
                sum(p.accept_count for p in panels_values) + d.accept_count
            )
            total_agents_active = (
                sum(len(p.active_agents) for p in panels_values)
                + len(d.active_agents)
            )
            total_agents_done = (
                sum(p.total_agents_completed for p in panels_values)
                + d.total_agents_completed
            )
            active_sessions = sum(
                1 for p in panels_values if p.state == "active"
            )
            idle_sessions = sum(
                1 for p in panels_values if p.state == "idle"
            )
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
                    "resets_at": (
                        u.five_hour.resets_at.isoformat()
                        if u.five_hour.resets_at
                        else None
                    ),
                },
                "seven_day": {
                    "utilization": u.seven_day.utilization,
                    "resets_at": (
                        u.seven_day.resets_at.isoformat()
                        if u.seven_day.resets_at
                        else None
                    ),
                },
            }

        return {
            "global_mode": "manual" if self._global_paused else "auto",
            "sessions": sessions,
            "dashboard": dashboard_data,
            "usage": usage_data,
        }

    # ------------------------------------------------------------------
    # Timestamp formatting
    # ------------------------------------------------------------------

    def _format_ts(self, ts: datetime) -> str:
        """Format a timestamp according to the current ``timestamp_style`` setting."""
        style = self.settings.timestamp_style
        if style == "12hr":
            return ts.strftime("%-I:%M:%S%p").lower()
        if style == "date_time":
            return ts.strftime("%Y-%m-%d %H:%M:%S")
        # "24hr" and "auto"
        return ts.strftime("%H:%M:%S")

    # ------------------------------------------------------------------
    # Status bar
    # ------------------------------------------------------------------

    def _update_status_bar(self) -> None:
        """Update the top status bar with mode, usage, version and clock."""
        try:
            bar = self.query_one("#status-bar", Horizontal)
            left = self.query_one("#status-left", Static)
            right = self.query_one("#status-right", Static)
            SEP = "  [dim]\u2502[/]  "

            n_paused = sum(
                1 for sid in self.panels if self.is_pane_paused(sid)
            )
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
                mode_text = (
                    f"[bold]MIXED [/] [dim]{n_total - n_paused}a {n_paused}m[/]"
                )
                bar.set_classes("paused")
                usage_mode = "paused"

            left_parts = [mode_text]
            if self._last_usage_data:
                bar_width = (bar.size.width if bar.size.width > 0 else 120) - 40
                left_parts.append(
                    format_usage_inline(self._last_usage_data, bar_width, usage_mode)
                )
            elif self.settings.account_usage:
                if self._usage_next_fetch > 0:
                    next_dt = datetime.fromtimestamp(self._usage_next_fetch)
                    next_str = next_dt.strftime("%-I:%M%p").lower()
                    left_parts.append(f"[dim]usage: updating at {next_str}[/]")
                else:
                    left_parts.append("[dim]usage: waiting…[/]")
            left.update(SEP.join(left_parts))

            clock = (
                datetime.now()
                .strftime("%-b %-d %-I:%M%p")
                .replace("AM", "am")
                .replace("PM", "pm")
            )
            right.update(f"[dim]v{__version__}[/]{SEP}{clock}")
        except NoMatches:
            log.debug("_update_status_bar: failed to update status bar widgets")

    # ------------------------------------------------------------------
    # Settings
    # ------------------------------------------------------------------

    def _apply_settings(self, settings: Settings) -> None:
        """Apply *settings* to the running app (theme, logging, OAuth, usage)."""
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

    def _on_settings_closed(self, result: Settings | None) -> None:
        """Callback invoked when the SettingsScreen modal is dismissed."""
        if result is None:
            return
        old_oauth = self.settings.oauth_json
        self.settings = result
        self._apply_settings(result)
        if result.oauth_json != old_oauth and result.oauth_json and result.account_usage:
            invalidate_usage_cache()
            self._refresh_usage()
        log.debug(f"Settings updated: {result}")

    def _on_token_refreshed(
        self, token: str, refresh_token: str, expires_at: float
    ) -> None:
        """Called from the usage module when the OAuth token is refreshed.

        May be called from a background thread — all mutable state changes
        are marshalled to the main thread via ``call_from_thread``.
        """
        if self.settings.oauth_json:
            new_json = json.dumps({
                "access_token": token,
                "refresh_token": refresh_token,
                "expires_at": expires_at,
            })

            def _update_settings() -> None:
                self.settings.oauth_json = new_json
                save_settings(self.settings)
                set_oauth_json(new_json)

            self.call_from_thread(_update_settings)
        ts = self._format_ts(datetime.now().astimezone())
        expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc).astimezone()
        msg = (
            f"[{ts}] [dim]OAuth token refreshed, "
            f"expires {expires_dt.strftime('%H:%M:%S')}[/]"
        )

        def _log() -> None:
            if self.dashboard:
                self.dashboard.record_event(msg)

        self.call_from_thread(_log)

    # ------------------------------------------------------------------
    # State persistence (minimal shared implementation)
    # ------------------------------------------------------------------

    def _save_state(self) -> None:
        """Persist pause state and settings to STATE_FILE.

        Subclasses may override to add extra fields (e.g. iTerm2 UUIDs).
        """
        state = {
            "global_paused": self._global_paused,
            "paused_sessions": [],
            "excluded_tools": self.settings.excluded_tools or [],
            "ask_user_timeout": self.settings.ask_user_timeout,
            "ask_paused_sessions": [],
        }
        try:
            with open(STATE_FILE, "w") as f:
                json.dump(state, f)
        except OSError as e:
            log.debug(f"_save_state: {e}")

    def _load_state(self) -> None:
        """Load pause state from STATE_FILE.

        Subclasses may override to load additional fields.
        """
        state = read_state()
        self._global_paused = state.get("global_paused", False)

    # ------------------------------------------------------------------
    # Shared actions
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def action_toggle_pause(self) -> None:
        """``a`` key: toggle between all-auto and all-manual.

        Must be overridden — pause collections differ between tui.py (iTerm2
        UUID-based) and tui_simple.py (Claude session ID-based).
        """

    def action_show_choices(self) -> None:
        """``c`` key: open the permission choices review screen."""
        self.push_screen(ChoicesScreen())

    def action_show_questions(self) -> None:
        """``u`` key: open the AskUserQuestion review screen."""
        self.push_screen(QuestionsScreen())

    def action_show_help(self) -> None:
        """``?`` key: open the keyboard shortcuts help modal."""
        self.push_screen(HelpScreen(self.BINDINGS, SessionPanel.BINDINGS))

    def action_next_tab(self) -> None:
        """``]`` key: switch to the next tab."""
        from textual.widgets import TabbedContent, TabPane

        try:
            tc = self.query_one("#tab-content", TabbedContent)
            pane_ids = [pane.id for pane in tc.query(TabPane) if pane.id]
            if not pane_ids or not tc.active:
                return
            idx = pane_ids.index(tc.active)
            tc.active = pane_ids[(idx + 1) % len(pane_ids)]
        except (NoMatches, ValueError):
            pass

    def action_prev_tab(self) -> None:
        """``[`` key: switch to the previous tab."""
        from textual.widgets import TabbedContent, TabPane

        try:
            tc = self.query_one("#tab-content", TabbedContent)
            pane_ids = [pane.id for pane in tc.query(TabPane) if pane.id]
            if not pane_ids or not tc.active:
                return
            idx = pane_ids.index(tc.active)
            tc.active = pane_ids[(idx - 1) % len(pane_ids)]
        except (NoMatches, ValueError):
            pass

    def action_open_settings(self) -> None:
        """``s`` key: open the settings modal."""
        self.push_screen(SettingsScreen(self.settings), self._on_settings_closed)

    def action_quit(self) -> None:
        """``q`` key: stop background threads and exit."""
        self._stop_event.set()
        self.exit()

    def _on_exit_app(self) -> None:
        """Ensure background threads don't prevent a clean exit."""
        self._stop_event.set()

    # ------------------------------------------------------------------
    # Background workers (identical in both subclasses)
    # ------------------------------------------------------------------

    @work(thread=True, exit_on_error=False)
    def watch_events(self) -> None:
        """Tail ``events.jsonl`` and post ``HookEvent`` messages to the app."""
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
                            log.debug(
                                f"watch_events: failed to parse JSON: {line[:100]}"
                            )
                else:
                    self._stop_event.wait(0.2)
        log.debug("watch_events: stopped")

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
        """One-shot usage fetch triggered by settings changes."""
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
