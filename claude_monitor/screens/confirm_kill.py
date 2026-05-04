"""Modal asking the user whether to kill a stale claude-monitor process holding the API port."""

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Static


class ConfirmKillScreen(ModalScreen[bool]):
    """Y/N modal shown when bind fails because another claude-monitor holds the port."""

    DEFAULT_CSS = """
    ConfirmKillScreen {
        align: center middle;
    }
    ConfirmKillScreen #confirm-kill-dialog {
        width: 70;
        height: auto;
        background: $surface;
        border: thick $warning;
        padding: 1 2;
    }
    ConfirmKillScreen #confirm-kill-title {
        text-align: center;
        text-style: bold;
        color: $warning;
        width: 100%;
        margin-bottom: 1;
    }
    ConfirmKillScreen .body-line {
        width: 100%;
    }
    ConfirmKillScreen #confirm-kill-prompt {
        margin-top: 1;
        text-align: center;
    }
    ConfirmKillScreen #confirm-kill-buttons {
        height: 3;
        margin-top: 1;
        align: center middle;
    }
    ConfirmKillScreen #confirm-kill-buttons Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("y", "confirm", "Yes, kill it"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "No"),
    ]

    def __init__(self, pid: int, port: int, cmdline: str) -> None:
        super().__init__()
        self.pid = pid
        self.port = port
        self.cmdline = cmdline

    def compose(self) -> ComposeResult:
        with Vertical(id="confirm-kill-dialog"):
            yield Static("Port already in use", id="confirm-kill-title")
            yield Static(
                f"Port {self.port} is held by another claude-monitor process:",
                classes="body-line",
            )
            yield Static(f"  PID {self.pid}: {self.cmdline}", classes="body-line")
            yield Static("Kill it and start the API server?", id="confirm-kill-prompt")
            with Horizontal(id="confirm-kill-buttons"):
                yield Button("Yes, kill it", variant="error", id="confirm-yes")
                yield Button("No", variant="default", id="confirm-no")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "confirm-yes":
            self.dismiss(True)
        elif event.button.id == "confirm-no":
            self.dismiss(False)

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)
