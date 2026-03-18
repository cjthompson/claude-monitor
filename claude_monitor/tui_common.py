"""Backward-compatibility shim -- imports have moved to subpackages.

This module re-exports everything that was previously defined here so that
existing imports in tui.py and tui_simple.py continue to work unchanged.
The canonical locations are now:

  claude_monitor.messages      - HookEvent
  claude_monitor.formatting    - _oneline, _safe_css_id, _safe_tab_css_id,
                                 _format_ask_user_question_inline,
                                 _format_ask_user_question_detail
  claude_monitor.commands      - MonitorCommands
  claude_monitor.widgets       - SessionPanel, DashboardPanel,
                                 FixedWidthSparkline, scrollbar renderers
  claude_monitor.screens       - PaneContextMenu, ChoicesScreen,
                                 QuestionsScreen, HelpScreen
"""

# Messages
from claude_monitor.messages import HookEvent  # noqa: F401

# Formatting helpers
from claude_monitor.formatting import (  # noqa: F401
    _oneline,
    _safe_css_id,
    _safe_tab_css_id,
    _format_ask_user_question_inline,
    _format_ask_user_question_detail,
)

# Command palette
from claude_monitor.commands import MonitorCommands  # noqa: F401

# Widgets
from claude_monitor.widgets import (  # noqa: F401
    HalfBlockScrollBarRender,
    HorizontalScrollBarRender,
    VerticalScrollBarRender,
    FixedWidthSparkline,
    SessionPanel,
    DashboardPanel,
)

# Screens
from claude_monitor.screens import (  # noqa: F401
    PaneContextMenu,
    ChoicesScreen,
    QuestionsScreen,
    HelpScreen,
)
