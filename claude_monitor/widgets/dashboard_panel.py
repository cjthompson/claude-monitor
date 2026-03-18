"""DashboardPanel widget for claude-monitor TUI."""

import collections
import logging
import time

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.css.query import NoMatches
from textual.widgets import RichLog, Static

from claude_monitor import fmt_duration
from claude_monitor.widgets.scrollbar import HorizontalScrollBarRender, VerticalScrollBarRender
from claude_monitor.widgets.sparkline import FixedWidthSparkline

log = logging.getLogger(__name__)


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
        self.active_agents: dict[str, str] = {}  # agent_id -> agent_type (own session)
        self.total_agents_completed = 0
        self.accept_count = 0
        self.tool_counts: dict[str, int] = {}  # tool_name -> accepted count
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
        except NoMatches:
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
        except NoMatches:
            log.debug("Dashboard.refresh_dashboard: dash-stats query failed")
        try:
            self.query_one(FixedWidthSparkline).data = self._scaled_data()
            self.query_one(".dash-scale-label", Static).update(self._render_scale_label())
        except NoMatches:
            log.debug("Dashboard.refresh_dashboard: Sparkline query failed")

    _MIN_Y_SCALE = 4  # minimum y-axis max so low counts don't fill the bar

    def _visible_data(self) -> list[int]:
        """Return the sparkline data that's actually visible (last `width` buckets)."""
        raw = list(self._event_buckets) + [self._current_bucket_count]
        try:
            width = self.query_one(FixedWidthSparkline).size.width
        except NoMatches:
            log.debug("Dashboard._visible_data: failed to get sparkline width, using raw data length")
            width = len(raw)
        return raw[-width:] if len(raw) > width else raw

    def _scaled_data(self) -> list[float]:
        """Return sparkline data normalized to 0.0-1.0 against visible peak."""
        raw = self._visible_data()
        if not raw:
            return [0.0]
        peak = max(max(raw), self._MIN_Y_SCALE)
        return [v / peak for v in raw]

    def _render_scale_label(self) -> str:
        raw = self._visible_data()
        peak = max(max(raw), self._MIN_Y_SCALE) if raw else self._MIN_Y_SCALE
        return f"[dim]now {self._current_bucket_count} \u00b7 peak {peak}[/]"

    def _render_stats(self, panels: dict | None = None) -> str:
        SEP = "  [dim]\u2502[/]  "

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
