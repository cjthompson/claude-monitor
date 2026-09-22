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
import asyncio
import concurrent.futures
import errno
import json
import logging
import os
import signal
import subprocess
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import partial
from pathlib import Path

from textual import work
from textual._callback import invoke
from textual.app import App
from textual.containers import Horizontal
from textual.css.query import NoMatches
from textual.widgets import Static

from claude_monitor import (
    API_PORT,
    EVENTS_FILE,
    SIGNAL_DIR,
    STATE_FILE,
    __version__,
    extract_iterm_session_id,
    read_state,
)
from claude_monitor.iterm2_layout import KeystrokeSender
from claude_monitor.messages import HookEvent
from claude_monitor.screens import ChoicesScreen, ConfirmKillScreen, HelpScreen, QuestionsScreen
from claude_monitor.settings import Settings, SettingsScreen, load_settings, save_settings
from claude_monitor.usage import (
    fetch_usage,
    format_usage_inline,
    invalidate_usage_cache,
    set_oauth_json,
    set_on_token_refreshed,
)
from claude_monitor.web import start_web_server
from claude_monitor.widgets import DashboardPanel, SessionPanel

log = logging.getLogger(__name__)

# Bound for MonitorApp._call_from_thread_bounded / call_from_thread_async.
# App.call_from_thread has no timeout; a wedged message loop blocks forever.
_CALL_FROM_THREAD_TIMEOUT = 5.0


@dataclass(frozen=True)
class HandoffTarget:
    """Exact live session and pane selected for a manual hand-off."""

    session_id: str
    iterm_session_id: str | None
    state: str


def _find_port_holder(port: int) -> int | None:
    """Return the PID of the process listening on ``port``, or None."""
    try:
        out = subprocess.check_output(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-t"],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    if not out:
        return None
    try:
        return int(out.splitlines()[0])
    except ValueError:
        return None


def _process_cmdline(pid: int) -> str | None:
    """Return the full command line for ``pid``, or None if unavailable."""
    try:
        out = subprocess.check_output(
            ["ps", "-p", str(pid), "-o", "command="],
            text=True,
            timeout=2,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return None
    return out or None


def _kill_pid(pid: int) -> bool:
    """SIGTERM then SIGKILL ``pid``. Returns True if the process is gone."""
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    for _ in range(20):  # up to 2s
        time.sleep(0.1)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False
    time.sleep(0.2)
    try:
        os.kill(pid, 0)
        return False
    except ProcessLookupError:
        return True


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
        self._global_ask_paused: bool = False

        # Usage polling
        self._usage_polling: bool = False
        self._last_usage_data = None
        self._usage_next_fetch: float = 0

        # HTTP API server handle
        self._api_server = None

        # Hand-off: session_id → lifecycle metadata.  Populated from hook events; consumed by the
        # ``poll_handoff`` worker and by ``action_capture_handoff``.
        self._session_meta: dict[str, dict] = {}
        self._handoff_polling: bool = False

    # ------------------------------------------------------------------
    # Bounded thread → Textual-loop callbacks
    # ------------------------------------------------------------------

    def _call_from_thread_bounded(
        self,
        fn,
        *args,
        default=None,
        timeout: float = _CALL_FROM_THREAD_TIMEOUT,
    ):
        """Cancelling on timeout only stops scheduling; a running callback can't be interrupted."""
        if self._stop_event.is_set():
            return default
        if self._loop is None:
            return default
        if self._thread_id == threading.get_ident():
            # Same guard as textual's call_from_thread: a same-thread call would
            # block this thread's own loop waiting on itself, deadlocking until
            # `timeout` instead of failing fast.
            raise RuntimeError(
                "_call_from_thread_bounded must run in a different thread from the app"
            )
        callback_with_args = partial(fn, *args)

        async def run_callback():
            with self._context():
                return await invoke(callback_with_args)

        future = asyncio.run_coroutine_threadsafe(run_callback(), loop=self._loop)
        try:
            return future.result(timeout=timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            return default

    async def call_from_thread_async(
        self,
        fn,
        *args,
        default=None,
        timeout: float = _CALL_FROM_THREAD_TIMEOUT,
    ):
        """Async counterpart of ``_call_from_thread_bounded`` for callers already on an event loop.

        Awaiting here (instead of a blocking ``future.result()``) keeps the
        caller's own loop — e.g. the web server's — free to service other
        concurrent work while this call is outstanding.
        """
        if self._stop_event.is_set():
            return default
        if self._loop is None:
            return default
        if self._thread_id == threading.get_ident():
            raise RuntimeError(
                "call_from_thread_async must run in a different thread from the app"
            )
        callback_with_args = partial(fn, *args)

        async def run_callback():
            with self._context():
                return await invoke(callback_with_args)

        future = asyncio.run_coroutine_threadsafe(run_callback(), loop=self._loop)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), timeout)
        except asyncio.TimeoutError:
            future.cancel()
            return default

    # ------------------------------------------------------------------
    # Abstract interface — subclasses MUST implement these
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def is_pane_paused(self, sid: str) -> bool:
        """Return True if the pane identified by *sid* is in manual mode."""

    @abc.abstractmethod
    def is_ask_paused(self, sid: str) -> bool:
        """Return True if AskUserQuestion auto-accept is paused for *sid*."""

    @abc.abstractmethod
    def _resolve_handoff_target(self, session_id: str | None = None) -> HandoffTarget | None:
        """Resolve the active or explicitly selected live hand-off target."""

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

        # Build reverse mapping: panel_id -> list of Claude session IDs
        # _iterm_to_panel maps claude_sid -> panel_id (iTerm sid)
        iterm_to_panel = getattr(self, "_iterm_to_panel", {})
        panel_to_claude: dict[str, list[str]] = {}
        for claude_sid, panel_id in iterm_to_panel.items():
            if claude_sid != panel_id:  # skip self-mappings from fallback panels
                panel_to_claude.setdefault(panel_id, []).append(claude_sid)

        sessions = []
        for sid, panel in panels_snapshot:
            sess_data: dict[str, object] = {
                "id": sid,
                "title": panel.border_title,
                "state": panel.state,
                "mode": "manual" if self.is_pane_paused(sid) else "auto",
                "active_agents": len(panel.active_agents),
                "completed_agents": panel.total_agents_completed,
                "accept_count": panel.accept_count,
            }
            # Include Claude session IDs mapped to this panel
            claude_sids = panel_to_claude.get(sid, [])
            if claude_sids:
                sess_data["claude_session_ids"] = claude_sids
            sessions.append(sess_data)

        dashboard_data = None
        if self.dashboard:
            d = self.dashboard
            total_accepted = sum(p.accept_count for p in panels_values) + d.accept_count
            total_agents_active = sum(len(p.active_agents) for p in panels_values) + len(
                d.active_agents
            )
            total_agents_done = (
                sum(p.total_agents_completed for p in panels_values) + d.total_agents_completed
            )
            active_sessions = sum(1 for p in panels_values if p.state == "active")
            idle_sessions = sum(1 for p in panels_values if p.state == "idle")
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
                        u.five_hour.resets_at.isoformat() if u.five_hour.resets_at else None
                    ),
                },
                "seven_day": {
                    "utilization": u.seven_day.utilization,
                    "resets_at": (
                        u.seven_day.resets_at.isoformat() if u.seven_day.resets_at else None
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
            result = ts.strftime("%-I:%M:%S%p").lower()
            # %-I omits leading zero on single-digit hours (e.g. "9:..."),
            # but always produces two digits on double-digit hours (e.g. "10:...").
            # Pad to a uniform 10-character width so log timestamps align.
            if len(result) == 9:
                return " " + result
            return result
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

            n_paused = sum(1 for sid in self.panels if self.is_pane_paused(sid))
            # Count only per-pane ask pauses (exclude global \u2014 shown separately)
            n_ask_paused = sum(
                1 for sid in self.panels if not self._global_ask_paused and self.is_ask_paused(sid)
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
                mode_text = f"[bold]MIXED [/] [dim]{n_total - n_paused}a {n_paused}m[/]"
                bar.set_classes("paused")
                usage_mode = "paused"

            left_parts = [mode_text]
            if self._global_ask_paused:
                left_parts.append("[bold cyan]Q-PAUSED[/]")
            elif n_ask_paused > 0:
                left_parts.append(f"[bold cyan]? PAUSED[/] [dim]({n_ask_paused})[/]")
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

            clock = (
                datetime.now().strftime("%-b %-d %-I:%M%p").replace("AM", "am").replace("PM", "pm")
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
        self._start_handoff_polling()
        if not settings.account_usage and self._last_usage_data:
            self._last_usage_data = None
            self._update_status_bar()
        self._global_ask_paused = not settings.auto_answer_questions
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

    def _on_token_refreshed(self, token: str, refresh_token: str, expires_at: float) -> None:
        """Called from the usage module when the OAuth token is refreshed.

        May be called from a background thread — all mutable state changes
        are marshalled to the main thread via ``call_from_thread``.
        """
        if self.settings.oauth_json:
            new_json = json.dumps(
                {
                    "access_token": token,
                    "refresh_token": refresh_token,
                    "expires_at": expires_at,
                }
            )

            def _update_settings() -> None:
                self.settings.oauth_json = new_json
                save_settings(self.settings)
                set_oauth_json(new_json)

            self._call_from_thread_bounded(_update_settings)
        ts = self._format_ts(datetime.now().astimezone())
        expires_dt = datetime.fromtimestamp(expires_at, tz=timezone.utc).astimezone()
        msg = f"[{ts}] [dim]OAuth token refreshed, expires {expires_dt.strftime('%H:%M:%S')}[/]"

        def _log() -> None:
            if self.dashboard:
                self.dashboard.record_event(msg)

        self._call_from_thread_bounded(_log)

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
            "global_ask_paused": self._global_ask_paused,
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
        self._global_ask_paused = state.get("global_ask_paused", False)

    # ------------------------------------------------------------------
    # Shared actions
    # ------------------------------------------------------------------

    @abc.abstractmethod
    def action_toggle_pause(self) -> None:
        """``a`` key: toggle between all-auto and all-manual.

        Must be overridden — pause collections differ between tui.py (iTerm2
        UUID-based) and tui_simple.py (Claude session ID-based).
        """

    def action_toggle_ask_pause(self) -> None:
        """``A`` key (shift+a): toggle global AskUserQuestion pause."""
        self._global_ask_paused = not self._global_ask_paused
        self._save_state()
        self._update_status_bar()

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
            # Replay recent events to restore state after restart
            lines = f.readlines()
            recent = lines[-50:] if len(lines) > 50 else lines
            for line in recent:
                line = line.strip()
                if line:
                    try:
                        data = json.loads(line)
                        data["_replay"] = True
                        self.post_message(HookEvent(data))
                    except json.JSONDecodeError:
                        pass

            # Now tail for new events from current position
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
        """Poll usage every 5 minutes (matches API cache TTL)."""
        log.debug("poll_usage: started")
        while not self._stop_event.is_set():
            if not self.settings.account_usage:
                self._usage_polling = False
                break
            self._last_usage_data = fetch_usage()
            self._usage_next_fetch = time.time() + 300
            self._call_from_thread_bounded(self._update_status_bar)
            self._stop_event.wait(300)
        log.debug("poll_usage: stopped")

    # ------------------------------------------------------------------
    # Hand-off summaries
    # ------------------------------------------------------------------

    def _record_session_meta(self, data: dict) -> None:
        """Remember cwd/transcript/last-activity for a hook event's session.

        Cheap and called for every event, so it must never raise.
        """
        try:
            sid = data.get("session_id")
            if not sid:
                return
            meta = self._session_meta.setdefault(sid, {})
            meta.setdefault("live", True)
            meta.setdefault("input_wait_unsafe", False)
            meta.setdefault("auto_rotation_armed", True)
            meta.setdefault("auto_rotation_fired", False)
            meta.setdefault("auto_rotation_deferred", False)
            meta.setdefault("auto_rotation_delivered", False)
            meta.setdefault("waiting_for_input", False)
            meta.setdefault("ended", False)
            cwd = data.get("cwd")
            if cwd:
                meta["cwd"] = cwd
            transcript_path = data.get("transcript_path")
            if transcript_path:
                meta["transcript_path"] = transcript_path
            event_ts = data.get("_timestamp") or time.time()
            event_name = data.get("hook_event_name") or ""
            meta["last_event_ts"] = event_ts
            meta["last_event_name"] = event_name
            pane_id = extract_iterm_session_id(data.get("_iterm_session_id") or "")
            if pane_id:
                meta["iterm_session_id"] = pane_id
            waiting = (
                (
                    event_name == "Notification"
                    and data.get("notification_type") == "permission_prompt"
                )
                or (
                    event_name == "PermissionRequest"
                    and (
                        data.get("tool_name") in ("AskUserQuestion", "ExitPlanMode")
                        or data.get("_decision") == "deferred"
                    )
                )
                or data.get("_decision") == "deferred"
                or data.get("_ask_timeout")
            )
            if waiting:
                meta["waiting_for_input"] = True
                meta["input_wait_unsafe"] = True
            elif event_name in ("PostToolUse", "Stop") or (
                event_name == "Notification" and data.get("notification_type") == "idle_prompt"
            ):
                meta["input_wait_unsafe"] = False
            if self._is_substantive_handoff_activity(data):
                meta["last_substantive_activity_ts"] = event_ts
                if meta.get("auto_rotation_fired"):
                    meta["auto_rotation_armed"] = True
                    meta["auto_rotation_fired"] = False
                    meta["auto_rotation_deferred"] = False
                    meta["auto_rotation_delivered"] = False
            if event_name in ("Stop",) or (
                event_name == "Notification" and data.get("notification_type") == "idle_prompt"
            ):
                meta["waiting_for_input"] = False
                meta["prompt_ready"] = True
                if not getattr(self, "_rehydrating_handoff", False):
                    self._deliver_deferred_idle_rotation(sid)
            if event_name == "SessionEnd":
                meta["live"] = False
                meta["input_wait_unsafe"] = False
                meta["waiting_for_input"] = False
                meta["ended"] = True
                self._cancel_idle_rotation(sid)
        except Exception as e:  # noqa: BLE001 - never break event handling
            log.debug(f"_record_session_meta: {e}")

    def _is_substantive_handoff_activity(self, data: dict) -> bool:
        event_name = data.get("hook_event_name")
        if event_name in ("SessionStart", "Stop", "SessionEnd"):
            return False
        if event_name == "Notification" and data.get("notification_type") in (
            "idle_prompt",
            "permission_prompt",
        ):
            return False
        if event_name == "PermissionRequest" and (
            data.get("tool_name") in ("AskUserQuestion", "ExitPlanMode")
            or data.get("_decision") == "deferred"
        ):
            return False
        if data.get("_ask_timeout"):
            return False
        return True

    def _rehydrate_handoff_meta(self) -> None:
        """Rebuild lifecycle state from the complete event log before polling."""
        from claude_monitor import handoff

        self._rehydrating_handoff = True
        try:
            with open(EVENTS_FILE, encoding="utf-8") as events_file:
                for raw_line in events_file:
                    try:
                        data = json.loads(raw_line)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    if isinstance(data, dict):
                        self._record_session_meta(data)
        except OSError:
            return
        finally:
            self._rehydrating_handoff = False
        for sid, meta in self._session_meta.items():
            if meta.get("ended") or not meta.get("iterm_session_id"):
                continue
            pending = handoff.find_pending_idle(
                iterm_session_id=meta["iterm_session_id"], originating_session_id=sid
            )
            if pending is not None:
                meta.update(
                    pending=pending,
                    auto_rotation_fired=True,
                    auto_rotation_deferred=bool(meta.get("waiting_for_input")),
                    auto_rotation_armed=False,
                )

    def _stage_idle_rotation(self, session_id: str) -> None:
        from claude_monitor import handoff

        meta = self._session_meta.get(session_id) or {}
        pane_id = meta.get("iterm_session_id")
        if meta.get("ended") or meta.get("auto_rotation_fired"):
            return
        from_existing_capture = meta.get("last_capture_ts", 0) >= meta.get("last_event_ts", 0)
        entry = None
        if from_existing_capture:
            entry = handoff.load_entry(session_id, handoff.project_slug(meta.get("cwd", "")))
        if entry is None:
            entry = self._capture_handoff_entry(session_id, reason="idle")
        if entry is None:
            return
        if not pane_id:
            meta.update(auto_rotation_fired=True, auto_rotation_armed=False)
            return
        pending = handoff.stage_pending(
            entry,
            iterm_session_id=pane_id,
            originating_session_id=session_id,
            rotation_mode=getattr(self.settings, "handoff_rotation_mode", "clear"),
            trigger="idle",
        )
        if pending is None:
            return
        meta.update(
            pending=pending,
            auto_rotation_fired=True,
            auto_rotation_armed=False,
            auto_rotation_delivered=False,
            auto_rotation_deferred=bool(meta.get("waiting_for_input")),
        )
        if not meta.get("waiting_for_input"):
            self._deliver_deferred_idle_rotation(session_id)

    def _deliver_deferred_idle_rotation(self, session_id: str) -> None:
        meta = self._session_meta.get(session_id) or {}
        pending = meta.get("pending")
        if pending is None or meta.get("waiting_for_input") or meta.get("auto_rotation_delivered"):
            return
        from claude_monitor import handoff

        entry = handoff.load_entry(pending.entry_session_id, pending.project_slug)
        command = handoff.rotation_command(entry, pending.rotation_mode) if entry else None
        if command is None:
            return
        try:
            if KeystrokeSender.send_text(pending.target_iterm_session_id, command):
                meta["auto_rotation_delivered"] = True
                meta["auto_rotation_deferred"] = False
        except Exception as exc:  # noqa: BLE001 - transport boundary
            log.warning("automatic hand-off rotation send failed: %s", exc)

    def _cancel_idle_rotation(self, session_id: str) -> None:
        meta = self._session_meta.get(session_id) or {}
        pending = meta.get("pending")
        if pending is None:
            return
        from claude_monitor import handoff

        handoff.discard_pending(
            iterm_session_id=pending.target_iterm_session_id,
            originating_session_id=session_id,
            generation_token=pending.generation_token,
        )
        meta.pop("pending", None)
        meta["auto_rotation_deferred"] = False

    def _capture_handoff(self, session_id: str, *, reason: str) -> bool:
        """Capture one hand-off entry. Returns True if an entry was written."""
        return self._capture_handoff_entry(session_id, reason=reason) is not None

    def _capture_handoff_entry(self, session_id: str, *, reason: str):
        """Capture one hand-off entry and return it for targeted delivery."""
        from claude_monitor import handoff

        meta = self._session_meta.get(session_id) or {}
        cwd = meta.get("cwd")
        if not cwd:
            return None
        with_llm = reason != "session_end" and bool(
            getattr(self.settings, "handoff_llm_enabled", False)
        )
        entry = handoff.capture(
            session_id,
            cwd,
            transcript_path=meta.get("transcript_path"),
            reason=reason,
            with_llm=with_llm,
            settings=self.settings,
        )
        if entry is not None:
            meta["last_capture_ts"] = time.time()
        return entry

    def _poll_handoff_once(self, now: float) -> float | None:
        capture_mins = int(getattr(self.settings, "handoff_capture_idle_mins", 0) or 0)
        rotate_enabled = bool(getattr(self.settings, "handoff_auto_rotate_enabled", False))
        rotate_mins = int(getattr(self.settings, "handoff_auto_rotate_idle_mins", 240) or 240)
        if not getattr(self.settings, "handoff_enabled", False):
            return None
        next_due = None
        for sid, meta in list(self._session_meta.items()):
            if meta.get("ended") or not meta.get("live", True):
                continue
            last_event = meta.get("last_event_ts") or 0
            if not last_event:
                continue
            try:
                if capture_mins > 0 and now >= last_event + capture_mins * 60:
                    if meta.get("last_capture_ts", 0) < last_event:
                        self._capture_handoff(sid, reason="idle")
                if rotate_enabled:
                    due = last_event + rotate_mins * 60
                    if now >= due and not meta.get("auto_rotation_fired"):
                        self._stage_idle_rotation(sid)
                    elif not meta.get("auto_rotation_fired"):
                        next_due = due if next_due is None else min(next_due, due)
            except Exception as exc:  # noqa: BLE001 - one bad session must not stop polling
                log.warning("poll_handoff: session %s failed: %s", sid, exc)
        return next_due

    @work(thread=True, exit_on_error=False)
    def poll_handoff(self) -> None:
        """Capture hand-off entries for sessions that have gone idle."""
        log.debug("poll_handoff: started")
        while not self._stop_event.is_set():
            if not getattr(self.settings, "handoff_enabled", False) or not (
                int(getattr(self.settings, "handoff_capture_idle_mins", 0) or 0) > 0
                or bool(getattr(self.settings, "handoff_auto_rotate_enabled", False))
            ):
                self._handoff_polling = False
                break

            try:
                next_due = self._poll_handoff_once(time.time())
            except Exception as e:  # noqa: BLE001 - one bad session must not stop the loop
                log.warning(f"poll_handoff: poll failed: {e}")
                next_due = None
            wait_for = 60 if next_due is None else max(0.1, min(60, next_due - time.time()))
            self._stop_event.wait(wait_for)
        log.debug("poll_handoff: stopped")

    def _start_handoff_polling(self) -> None:
        """Start ``poll_handoff`` if enabled and not already running."""
        if self._handoff_polling:
            return
        if not getattr(self.settings, "handoff_enabled", False):
            return
        if int(getattr(self.settings, "handoff_capture_idle_mins", 0) or 0) <= 0 and not getattr(
            self.settings, "handoff_auto_rotate_enabled", False
        ):
            return
        self._rehydrate_handoff_meta()
        self._handoff_polling = True
        self.poll_handoff()

    def _handoff_panel(self):
        """Return the mounted ``HandoffPanel``, or None."""
        from claude_monitor.screens.handoff import HandoffPanel

        try:
            return self.query_one(HandoffPanel)
        except Exception:
            return None

    def action_show_handoff(self, session_id: str | None = None) -> None:
        """Open/focus the Hand-off tab, optionally showing one session."""
        panel = self._handoff_panel()
        if panel is None:
            self.notify("Hand-off tab is not available.", severity="warning")
            return
        try:
            from textual.widgets import TabbedContent, TabPane

            pane = panel
            while pane is not None and not isinstance(pane, TabPane):
                pane = pane.parent
            if pane is not None:
                tc = pane.parent
                while tc is not None and not isinstance(tc, TabbedContent):
                    tc = tc.parent
                if tc is not None and pane.id:
                    tc.active = pane.id
        except Exception as e:  # noqa: BLE001
            log.debug(f"action_show_handoff: {e}")
        if session_id is None:
            panel.refresh_entries()
        else:
            panel.show_session_summary(session_id)

    def action_refresh_handoff(self) -> None:
        """Reload the hand-off list from disk."""
        panel = self._handoff_panel()
        if panel is not None:
            panel.refresh_entries()

    def action_capture_handoff(self) -> None:
        """Capture a hand-off for the active session right now."""
        if not getattr(self.settings, "handoff_enabled", False):
            self.notify("Hand-off summaries are disabled in Settings.", severity="warning")
            return
        target = self._resolve_handoff_target()
        if target is None:
            self.notify("No active live session is selected.", severity="warning")
            return
        self._capture_handoff_worker(target)

    def action_rotate_selected_handoff(self) -> None:
        """Capture and rotate the session selected in the Hand-off tab."""
        if not getattr(self.settings, "handoff_enabled", False):
            self.notify("Hand-off summaries are disabled in Settings.", severity="warning")
            return
        panel = self._handoff_panel()
        entry = panel.selected_entry if panel is not None else None
        if entry is None:
            self.notify("Select a hand-off entry first.", severity="warning")
            return
        target = self._resolve_handoff_target(entry.session_id)
        if target is None:
            self.notify("The selected hand-off session is no longer live.", severity="warning")
            return
        self._capture_handoff_worker(target)

    def on_selected_handoff(self, _message) -> None:
        """Rotate the entry selected by the Hand-off list."""
        self.action_rotate_selected_handoff()

    @work(thread=True, exit_on_error=False)
    def _capture_handoff_worker(self, target: HandoffTarget) -> None:
        """Run manual captures off the UI thread (may call an LLM)."""
        entry = None
        capture_error = None
        delivery_status = "nothing"
        try:
            entry = self._capture_handoff_entry(target.session_id, reason="manual")
        except Exception as e:  # noqa: BLE001
            capture_error = e
            log.warning(f"action_capture_handoff: capture failed for {target.session_id}: {e}")
        if entry is not None:
            if target.state == "waiting":
                delivery_status = "deferred"
            elif not target.iterm_session_id:
                delivery_status = "unavailable"
            else:
                from claude_monitor import handoff
                from claude_monitor.iterm2_layout import KeystrokeSender

                mode = getattr(self.settings, "handoff_rotation_mode", "clear")
                if target.state == "prompt_ready":
                    if mode == "clear":
                        import re

                        title = re.sub(
                            r"[\x00-\x1f\x7f]+", " ", entry.title or entry.session_id
                        ).strip()
                        title = title[:80] or entry.session_id
                        command = f"/rename claude-monitor hand-off: {title}\r/clear\r"
                    else:
                        command = "/compact\r"
                    try:
                        pending = handoff.stage_pending(
                            entry,
                            iterm_session_id=target.iterm_session_id,
                            originating_session_id=target.session_id,
                            rotation_mode=mode,
                        )
                    except Exception as e:  # noqa: BLE001
                        log.warning("action_capture_handoff: staging failed: %s", e)
                        pending = None
                    if pending is None:
                        delivery_status = "failed"
                    else:
                        delivery_status = (
                            "delivered"
                            if self._send_handoff_text(
                                KeystrokeSender, target.iterm_session_id, command
                            )
                            else "failed"
                        )
                else:
                    command = (
                        "claude-monitor action: session hand-off\n"
                        "Provide only goal, progress, files changed, blockers, and next steps.\r"
                    )
                    delivery_status = (
                        "delivered"
                        if self._send_handoff_text(
                            KeystrokeSender, target.iterm_session_id, command
                        )
                        else "failed"
                    )

        def _done() -> None:
            panel = self._handoff_panel()
            if panel is not None:
                panel.refresh_entries(select_session_id=entry.session_id if entry else None)
            if capture_error is not None:
                self.notify(
                    "Hand-off capture failed; no local entry was written.", severity="error"
                )
                return
            if entry is None:
                self.notify("Nothing to capture yet.", severity="warning")
                return
            if delivery_status == "deferred":
                self.notify("Captured locally; pane input is unsafe while it is waiting.")
                return
            if delivery_status == "unavailable":
                self.notify(
                    "Captured locally; live pane delivery is unavailable.", severity="warning"
                )
                return
            if delivery_status == "delivered":
                self.notify("Captured and delivered to the exact pane.")
            else:
                self.notify("Captured locally; pane delivery failed.", severity="warning")

        self._call_from_thread_bounded(_done)

    @staticmethod
    def _send_handoff_text(sender, iterm_session_id: str, command: str) -> bool:
        """Send hand-off input from the capture worker thread."""
        try:
            return bool(sender.send_text(iterm_session_id, command))
        except Exception as e:  # noqa: BLE001 - transport is an external boundary
            log.warning("action_capture_handoff: pane send failed: %s", e)
            return False

    @work(thread=True, exit_on_error=False)
    def _refresh_usage(self) -> None:
        """One-shot usage fetch triggered by settings changes."""
        self._last_usage_data = fetch_usage()
        self._usage_next_fetch = time.time() + 300
        self._call_from_thread_bounded(self._update_status_bar)

    @work(thread=True, exit_on_error=False)
    def serve_api(self) -> None:
        """Run the unified HTTP+WebSocket server in a background thread.

        On EADDRINUSE, identify the holder; if it's another claude-monitor,
        ask the user (via a modal) whether to kill it and retry.
        """
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        stop_event = asyncio.Event()

        def _watch_stop() -> None:
            """Bridge threading.Event → asyncio.Event for clean shutdown."""
            self._stop_event.wait()
            try:
                loop.call_soon_threadsafe(stop_event.set)
            except RuntimeError:
                # Loop is already closed, that's fine
                pass

        watcher = threading.Thread(target=_watch_stop, daemon=True)
        watcher.start()

        try:
            while not self._stop_event.is_set():
                try:
                    log.debug("serve_api: starting")
                    loop.run_until_complete(
                        start_web_server(self, port=API_PORT, stop_event=stop_event)
                    )
                    break  # clean shutdown via stop_event
                except OSError as e:
                    if e.errno != errno.EADDRINUSE:
                        log.error(f"serve_api: failed to start: {e}")
                        break
                    if not self._handle_port_in_use(API_PORT):
                        break
                    # Holder killed; loop and retry the bind
        finally:
            loop.close()
            log.debug("serve_api: stopped")

    def _handle_port_in_use(self, port: int) -> bool:
        """Return True if the holder was killed and the bind should be retried.

        Identifies the listening PID; if it's another claude-monitor, prompts the
        user via a modal and (on confirmation) kills it.
        """
        pid = _find_port_holder(port)
        if pid is None:
            log.error(f"serve_api: port {port} in use but holder PID not found")
            return False
        if pid == os.getpid():
            log.error(f"serve_api: port {port} appears held by self (pid {pid})")
            return False
        cmdline = _process_cmdline(pid)
        if not cmdline or "claude-monitor" not in cmdline:
            log.error(
                f"serve_api: port {port} held by pid {pid} ({cmdline!r}); "
                "not a claude-monitor, refusing to kill"
            )
            return False

        result: dict[str, bool] = {"answer": False}
        done = threading.Event()

        def on_response(answer: bool | None) -> None:
            result["answer"] = bool(answer)
            done.set()

        try:
            self.call_from_thread(
                self.push_screen,
                ConfirmKillScreen(pid, port, cmdline),
                on_response,
            )
        except RuntimeError:
            log.debug("serve_api: app loop unavailable, cannot prompt for kill")
            return False

        if not done.wait(timeout=120):
            log.warning("serve_api: kill prompt timed out without user response")
            return False
        if not result["answer"]:
            log.info(f"serve_api: user declined to kill pid {pid}")
            return False

        log.info(f"serve_api: killing stale claude-monitor pid {pid}")
        if not _kill_pid(pid):
            log.error(f"serve_api: failed to kill pid {pid}")
            return False
        time.sleep(0.3)  # let the kernel release the port
        return True
