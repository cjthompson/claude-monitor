"""HelpScreen modal for claude-monitor TUI."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static


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
                        yield Static("\u2500" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-global", classes="help-section-log", highlight=False)
                    with Vertical(classes="help-col"):
                        yield Static("INSTANCE", classes="help-section-header")
                        yield Static("\u2500" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-instance", classes="help-section-log", highlight=False)
                # Narrow layout: stacked
                with Vertical(id="help-body-narrow"):
                    with Vertical(classes="help-col"):
                        yield Static("GLOBAL", classes="help-section-header")
                        yield Static("\u2500" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-global-narrow", classes="help-section-log", highlight=False)
                    with Vertical(classes="help-col"):
                        yield Static("INSTANCE", classes="help-section-header")
                        yield Static("\u2500" * 20, classes="help-section-rule")
                        yield RichLog(markup=True, id="help-log-instance-narrow", classes="help-section-log", highlight=False)
            yield Static("[dim]ESC to close[/]", id="help-footer")

    # Map internal Textual key names to readable display names
    _KEY_DISPLAY: dict[str, str] = {
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
        result: list[tuple[str, str]] = []
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
