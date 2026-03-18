"""PaneContextMenu modal for claude-monitor TUI."""

import logging

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList
from textual.widgets.option_list import Option

log = logging.getLogger(__name__)


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
        from claude_monitor.widgets.session_panel import SessionPanel
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
