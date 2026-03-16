"""Shared Textual widgets, helpers, and utilities for claude-monitor.

This module contains all platform-independent UI components that are reused
across tui.py and any future platform-specific TUI backends (e.g. Linux).

Intentionally does NOT import iterm2 or any macOS-specific libraries.
"""

import json
import logging
import os
import time
from datetime import datetime

from textual.app import ComposeResult
from textual.binding import Binding
from textual.command import DiscoveryHit, Hit, Hits, Provider
from textual.containers import Horizontal, Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.scrollbar import ScrollBarRender
from textual.widgets import Footer, OptionList, RichLog, Sparkline, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option

from claude_monitor import EVENTS_FILE, fmt_duration

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Message classes
# ---------------------------------------------------------------------------

class HookEvent(Message):
    """Posted when a new hook event is parsed from the events JSONL file."""
    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data


# ---------------------------------------------------------------------------
# Scrollbar renderers
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Sparkline widget
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# CSS ID helpers
# ---------------------------------------------------------------------------

def _safe_css_id(session_id: str) -> str:
    return "panel-" + session_id.replace("-", "").replace(":", "").replace("/", "")


def _safe_tab_css_id(tab_id: str) -> str:
    return "tab-" + tab_id.replace("-", "").replace(":", "").replace("/", "").replace(".", "")


# ---------------------------------------------------------------------------
# Formatting helpers
# ---------------------------------------------------------------------------

def _oneline(text: str, max_len: int = 0) -> str:
    """Collapse multi-line text into one line, replacing newlines with ↵."""
    joined = " ↵ ".join(line.strip() for line in text.splitlines() if line.strip())
    return joined[:max_len] if max_len else joined


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


# ---------------------------------------------------------------------------
# SessionPanel widget
# ---------------------------------------------------------------------------

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
        self.tool_counts: dict[str, int] = {}  # tool_name → accepted count
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


# ---------------------------------------------------------------------------
# PaneContextMenu modal
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# DashboardPanel widget
# ---------------------------------------------------------------------------

import collections


class DashboardPanel(Static):
    """Aggregate dashboard shown in the TUI's own pane."""

    DEFAULT_CSS = """
    DashboardPanel {
        border: solid $primary;
        height: 1fr;
        width: 1fr;
        padding: 0 1;
    }
    DashboardPanel #dashboard-summary {
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
        self.tool_counts: dict[str, int] = {}  # tool_name → accepted count
        # Track events per N-second bucket for sparkline
        self._event_buckets: collections.deque = collections.deque(maxlen=300)
        self._bucket_secs = 5  # overridden from settings after mount
        self._bucket_counter = 0  # counts ticks (1s each) within a bucket
        self._current_bucket_count = 0
        self._event_log: list[str] = []  # stored for replay after rebuild

    def compose(self) -> ComposeResult:
        yield Static(self._render_stats(), id="dashboard-summary")
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
            self.query_one("#dashboard-summary", Static).update(self._render_stats(panels))
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
        SEP = "  [dim]│[/]  "

        if panels:
            total_accepted = sum(p.accept_count for p in panels.values())
            total_agents_active = sum(len(p.active_agents) for p in panels.values())
            active_sessions = sum(1 for p in panels.values() if p.state == "active")
            total_sessions = len(panels)
            merged: dict[str, int] = {}
            for p in panels.values():
                for tool, cnt in p.tool_counts.items():
                    merged[tool] = merged.get(tool, 0) + cnt
        else:
            total_accepted = total_agents_active = 0
            active_sessions = total_sessions = 0
            merged = {}

        # Include dashboard's own session agents in totals
        total_accepted += self.accept_count
        total_agents_active += len(self.active_agents)
        for tool, cnt in self.tool_counts.items():
            merged[tool] = merged.get(tool, 0) + cnt

        uptime = fmt_duration(time.time() - self._start_time)

        instances_str = f"Instances: [bold green]{active_sessions}[/]/{total_sessions}"
        agents_str = f"Agents: [bold magenta]{total_agents_active}[/]"

        approved_str = f"Approved: [bold]{total_accepted}[/]"
        if merged:
            top = sorted(merged.items(), key=lambda x: -x[1])[:5]
            breakdown = ", ".join(f"{t}: {c}" for t, c in top)
            approved_str += f" ({breakdown})"

        uptime_str = f"Uptime: [dim]{uptime}[/]"

        return SEP.join([instances_str, agents_str, approved_str, uptime_str])


# ---------------------------------------------------------------------------
# ChoicesScreen modal
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# QuestionsScreen modal
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# HelpScreen — keyboard shortcuts modal
# ---------------------------------------------------------------------------

class HelpScreen(ModalScreen):
    """Modal showing all keybindings as a two-column list."""

    DEFAULT_CSS = """
HelpScreen {
    align: center middle;
}
HelpScreen #help-dialog {
    width: auto;
    min-width: 62;
    max-width: 130;
    height: auto;
    max-height: 80%;
    background: $surface;
    border: thick $primary;
    padding: 0;
}
HelpScreen #help-title {
    text-align: center;
    text-style: bold;
    width: 100%;
    padding: 1 0 0 0;
    margin-bottom: 1;
}
HelpScreen .help-section-header {
    text-align: center;
    text-style: bold;
    color: $primary;
    width: 100%;
    padding: 0 0 0 0;
    margin-bottom: 0;
}
HelpScreen .help-section-rule {
    text-align: center;
    color: $primary-darken-2;
    width: 100%;
    margin-bottom: 0;
}
HelpScreen .help-section-log {
    height: auto;
    min-width: 28;
}
HelpScreen #help-body-wide {
    height: auto;
    padding: 0 2 1 2;
}
HelpScreen #help-body-wide .help-col {
    width: 1fr;
    height: auto;
    padding: 0 1 0 1;
}
HelpScreen #help-body-narrow {
    height: auto;
    padding: 0 2 1 2;
}
HelpScreen #help-body-narrow .help-col {
    width: 100%;
    height: auto;
    padding: 0 0 1 0;
}
HelpScreen #help-footer {
    dock: bottom;
    height: 1;
    text-align: center;
    color: $text-muted;
}
"""

    BINDINGS = [
        Binding("escape", "dismiss", "Close"),
        Binding("q", "dismiss", "Close"),
    ]

    def __init__(self, global_bindings: list, instance_bindings: list | None = None) -> None:
        super().__init__()
        self._global_bindings = global_bindings
        self._instance_bindings = instance_bindings or []

    def compose(self) -> ComposeResult:
        from textual.containers import ScrollableContainer
        with Vertical(id="help-dialog"):
            yield Static("Keyboard Shortcuts", id="help-title")
            with ScrollableContainer(id="help-scroll"):
                # Wide layout: two columns side by side
                with Horizontal(id="help-body-wide"):
                    with Vertical(classes="help-col"):
                        yield Static("GLOBAL", classes="help-section-header")
                        yield Static("─" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-global", classes="help-section-log", highlight=False)
                    with Vertical(classes="help-col"):
                        yield Static("INSTANCE", classes="help-section-header")
                        yield Static("─" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-instance", classes="help-section-log", highlight=False)
                # Narrow layout: stacked
                with Vertical(id="help-body-narrow"):
                    with Vertical(classes="help-col"):
                        yield Static("GLOBAL", classes="help-section-header")
                        yield Static("─" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-global-narrow", classes="help-section-log", highlight=False)
                    with Vertical(classes="help-col"):
                        yield Static("INSTANCE", classes="help-section-header")
                        yield Static("─" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-instance-narrow", classes="help-section-log", highlight=False)
            yield Static("[dim]ESC to close[/]", id="help-footer")

    # Map internal Textual key names to readable display names
    _KEY_DISPLAY = {
        "equals_sign": "=",
        "minus": "-",
        "question_mark": "?",
        "right_square_bracket": "]",
        "left_square_bracket": "[",
        "ctrl+p": "Ctrl+P",
        "shift+tab": "Shift+Tab",
    }

    def _extract_bindings(self, bindings: list) -> list[tuple[str, str]]:
        """Return list of (display_key, description) for visible bindings, deduped by action."""
        seen_actions: set[str] = set()
        result = []
        for binding in bindings:
            if isinstance(binding, tuple):
                key = binding[0] if len(binding) > 0 else ""
                action = binding[1] if len(binding) > 1 else ""
                description = binding[2] if len(binding) > 2 else ""
            else:
                key = getattr(binding, "key", "")
                action = getattr(binding, "action", "")
                description = getattr(binding, "description", "")
            if not description:
                continue
            if action in seen_actions:
                continue
            seen_actions.add(action)
            display_key = self._KEY_DISPLAY.get(key, key)
            result.append((display_key, description))
        return result

    def _populate_rl(self, rl: RichLog, pairs: list[tuple[str, str]]) -> None:
        """Write (key, description) pairs to a RichLog, clearing it first."""
        rl.clear()
        for display_key, description in pairs:
            rl.write(f"[bold #fea62b]{display_key:>14}[/]  {description}")

    def on_mount(self) -> None:
        self._global_pairs = self._extract_bindings(self._global_bindings)
        self._instance_pairs = self._extract_bindings(self._instance_bindings)
        self._apply_layout()

    def on_resize(self, event) -> None:
        self._apply_layout()

    # Wide threshold: terminal width >= 90 columns -> side-by-side
    _WIDE_THRESHOLD = 90

    def _apply_layout(self) -> None:
        """Show/hide wide vs narrow layout based on terminal width and populate RichLogs."""
        wide = self.app.size.width >= self._WIDE_THRESHOLD
        wide_body = self.query_one("#help-body-wide")
        narrow_body = self.query_one("#help-body-narrow")
        if wide:
            wide_body.display = True
            narrow_body.display = False
            self._populate_rl(self.query_one("#help-log-global", RichLog), self._global_pairs)
            self._populate_rl(self.query_one("#help-log-instance", RichLog), self._instance_pairs)
        else:
            wide_body.display = False
            narrow_body.display = True
            self._populate_rl(self.query_one("#help-log-global-narrow", RichLog), self._global_pairs)
            self._populate_rl(self.query_one("#help-log-instance-narrow", RichLog), self._instance_pairs)

    def action_dismiss(self) -> None:
        self.app.pop_screen()


# ---------------------------------------------------------------------------
# MonitorCommands — command palette provider
# ---------------------------------------------------------------------------

class MonitorCommands(Provider):
    """Command palette provider exposing all TUI actions."""

    COMMANDS_LIST = [
        ("Toggle Auto/Manual (global)", "toggle_pause"),
        ("Show Choices Log", "show_choices"),
        ("Show Questions Log", "show_questions"),
        ("Refresh Layout", "refresh_layout"),
        ("Open Settings", "open_settings"),
        ("Show Help", "show_help"),
        ("Dashboard: Grow", "grow_dashboard"),
        ("Dashboard: Shrink", "shrink_dashboard"),
        ("Next Tab", "next_tab"),
        ("Previous Tab", "prev_tab"),
        ("Quit", "quit"),
    ]

    async def startup(self) -> None:
        pass

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        for name, action in self.COMMANDS_LIST:
            score = matcher.match(name)
            if score > 0:
                yield DiscoveryHit(
                    name,
                    getattr(app, f"action_{action}", None) or (lambda: None),
                    help=f"action_{action}",
                    score=score,
                )

    async def discover(self) -> Hits:
        app = self.app
        for name, action in self.COMMANDS_LIST:
            yield DiscoveryHit(
                name,
                getattr(app, f"action_{action}", None) or (lambda: None),
                help=f"action_{action}",
            )
