"""Widgets subpackage for claude-monitor TUI."""

from claude_monitor.widgets.dashboard_panel import DashboardPanel
from claude_monitor.widgets.scrollbar import (
    HalfBlockScrollBarRender,
    HorizontalScrollBarRender,
    VerticalScrollBarRender,
)
from claude_monitor.widgets.session_panel import SessionPanel
from claude_monitor.widgets.sparkline import FixedWidthSparkline

# Legacy aliases used in tui_common.py shim and original tui.py/tui_simple.py
HalfBlockHorizontalScrollBar = HorizontalScrollBarRender
HalfBlockVerticalScrollBar = VerticalScrollBarRender

__all__ = [
    "HalfBlockScrollBarRender",
    "HalfBlockHorizontalScrollBar",
    "HalfBlockVerticalScrollBar",
    "HorizontalScrollBarRender",
    "VerticalScrollBarRender",
    "FixedWidthSparkline",
    "SessionPanel",
    "DashboardPanel",
]
