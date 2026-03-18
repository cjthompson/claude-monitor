"""SessionPanel widget for claude-monitor TUI."""

import logging
import time

from textual.app import ComposeResult
from textual.message import Message
from textual.widgets import RichLog, Static

from claude_monitor import fmt_duration
from claude_monitor.widgets.scrollbar import HorizontalScrollBarRender, VerticalScrollBarRender

log = logging.getLogger(__name__)


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
        self.active_agents: dict[str, str] = {}  # agent_id -> agent_type
        self.accept_count = 0
        self.tool_counts: dict[str, int] = {}  # tool_name -> accepted count
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

    # -- Status rendering helpers ------------------------------------------

    def _render_mode(self) -> tuple[str, str]:
        """Return (markup, plain) for the mode indicator (AUTO/MANUAL + ask-pause)."""
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
                mode += " [cyan]?\u23f8[/]"
                mode_plain += " ?\u23f8"
        except Exception:
            log.debug(f"SessionPanel._render_mode: failed to check pause state for {self.session_id}")
            mode = ""
            mode_plain = ""
        return mode, mode_plain

    def _render_state_badge(self) -> tuple[str, str, str]:
        """Return (markup, short_markup, plain) for the state indicator."""
        if self._state == "active":
            return "[bold green]\u25b6 active[/]", "[bold green]\u25b6[/]", "\u25b6 active"
        elif self._state == "idle":
            return "[yellow]\u23f8 idle[/]", "[yellow]\u23f8[/]", "\u23f8 idle"
        else:
            return "[dim]\u25e6 waiting[/]", "[dim]\u25e6[/]", "\u25e6 waiting"

    def _render_agents(self) -> tuple[str, str, str, str, bool]:
        """Return (full_markup, full_plain, count_markup, count_plain, has_agents)."""
        n = len(self.active_agents)
        has_agents = n > 0
        if has_agents:
            blocks = "\u2588" * min(n, 8)
            types: dict[str, int] = {}
            for atype in self.active_agents.values():
                types[atype] = types.get(atype, 0) + 1
            detail = " ".join(f"{t}:{c}" for t, c in sorted(types.items()))
            agents_full = f"[bold magenta]{blocks}[/] {n} ({detail})"
            agents_full_plain = f"{blocks} {n} ({detail})"
            agents_count = f"[bold magenta]{blocks}[/] {n}"
            agents_count_plain = f"{blocks} {n}"
        else:
            agents_full = "[dim]\u2500\u2500 none[/]"
            agents_full_plain = "\u2500\u2500 none"
            agents_count = agents_full
            agents_count_plain = agents_full_plain
        return agents_full, agents_full_plain, agents_count, agents_count_plain, has_agents

    def _render_uptime(self) -> str:
        """Return formatted uptime string."""
        return fmt_duration(time.time() - self._start_time)

    def _render_accepts(self) -> tuple[int, int]:
        """Return (done, accepted) task counts."""
        return self.total_agents_completed, self.accept_count

    def _render_status(self) -> str:
        SEP = " [dim]\u2502[/] "

        mode, mode_plain = self._render_mode()
        state, state_short, state_plain = self._render_state_badge()
        agents_full, agents_full_plain, agents_count, agents_count_plain, has_agents = self._render_agents()
        done, accepted = self._render_accepts()
        uptime = self._render_uptime()
        n = len(self.active_agents)

        # Available width for choosing tier
        try:
            w = self.size.width
        except Exception:
            log.debug(f"SessionPanel._render_status: failed to get widget width for {self.session_id}, defaulting to 120")
            w = 120  # fallback to widest

        # SEP is ~3 visible chars (" | ")
        S = 3

        # Build tiers from widest to narrowest
        # Tier 1 (>=110): AUTO | > active | Agents: XX 2 (gp:1 Ex:1) | Done: 5 | Accepted: 23 | 14m32s
        if has_agents:
            t1 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}{SEP}Done: {done}{SEP}Accepted: {accepted}{SEP}{uptime}"
            t1_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain) + S + len(f"Done: {done}") + S + len(f"Accepted: {accepted}") + S + len(uptime)
        else:
            t1 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}{SEP}{uptime}"
            t1_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain) + S + len(uptime)

        if w >= t1_len:
            return t1

        # Tier 2 (>=85): AUTO | > active | Agents: XX 2 (gp:1 Ex:1) | Tasks: 5/23
        if has_agents:
            t2 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}{SEP}Tasks: {done}/{accepted}"
            t2_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain) + S + len(f"Tasks: {done}/{accepted}")
        else:
            t2 = f"{mode}{SEP}{state}{SEP}Agents: {agents_full}"
            t2_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_full_plain)

        if w >= t2_len:
            return t2

        # Tier 3 (>=60): AUTO | > active | Agents: XX 2 | Tasks: 5/23
        if has_agents:
            t3 = f"{mode}{SEP}{state}{SEP}Agents: {agents_count} | Tasks: {done}/{accepted}"
            t3_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_count_plain) + len(f" | Tasks: {done}/{accepted}")
        else:
            t3 = f"{mode}{SEP}{state}{SEP}Agents: {agents_count}"
            t3_len = len(mode_plain) + S + len(state_plain) + S + len("Agents: ") + len(agents_count_plain)

        if w >= t3_len:
            return t3

        # Tier 4 (>=48): AUTO | > active | SA: 2 | T: 5/23
        if has_agents:
            t4 = f"{mode}{SEP}{state}{SEP}SA: {n} | T: {done}/{accepted}"
            t4_len = len(mode_plain) + S + len(state_plain) + S + len(f"SA: {n} | T: {done}/{accepted}")
        else:
            t4 = f"{mode}{SEP}{state}{SEP}SA: {n}"
            t4_len = len(mode_plain) + S + len(state_plain) + S + len(f"SA: {n}")

        if w >= t4_len:
            return t4

        # Tier 5 (>=38): AUTO | > | SA: 2 | T: 5/23
        if has_agents:
            t5 = f"{mode}{SEP}{state_short} | SA: {n} | T: {done}/{accepted}"
            t5_len = len(mode_plain) + S + 1 + len(f" | SA: {n} | T: {done}/{accepted}")
        else:
            t5 = f"{mode}{SEP}{state_short} | SA: {n}"
            t5_len = len(mode_plain) + S + 1 + len(f" | SA: {n}")

        if w >= t5_len:
            return t5

        # Tier 6 (>=25): AUTO | > | T:5/23
        if has_agents:
            t6 = f"{mode}{SEP}{state_short} | T:{done}/{accepted}"
            t6_len = len(mode_plain) + S + 1 + len(f" | T:{done}/{accepted}")
        else:
            t6 = f"{mode}{SEP}{state_short}"
            t6_len = len(mode_plain) + S + 1

        if w >= t6_len:
            return t6

        # Tier 7 (>=12): AUTO | >
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
                    bar.update(f" \u23f1 AskUserQuestion auto-accept in {remaining}s")
                    bar.add_class("active")
                    overlay.update(f" \u23f1 AskUserQuestion \u2014 auto-accept in [bold white]{remaining}s[/] ")
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
        from claude_monitor.screens.context_menu import PaneContextMenu
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
