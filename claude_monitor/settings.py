"""Settings management for claude-monitor.

Persists to ~/.config/claude-monitor/config.json.
Provides a SettingsScreen (ModalScreen) for editing settings in the TUI.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, RadioButton, RadioSet, Select, Static, Switch

log = logging.getLogger(__name__)

CONFIG_DIR = os.path.expanduser("~/.config/claude-monitor")
CONFIG_FILE = os.path.join(CONFIG_DIR, "config.json")

THEMES = [
    "textual-dark",
    "textual-light",
    "textual-ansi",
    "atom-one-dark",
    "atom-one-light",
    "catppuccin-frappe",
    "catppuccin-latte",
    "catppuccin-macchiato",
    "catppuccin-mocha",
    "dracula",
    "flexoki",
    "gruvbox",
    "monokai",
    "nord",
    "rose-pine",
    "rose-pine-dawn",
    "rose-pine-moon",
    "solarized-dark",
    "solarized-light",
    "tokyo-night",
]


@dataclass
class Settings:
    default_mode: str = "auto"  # auto / manual / last_used
    theme: str = "textual-dark"
    debug: bool = False
    iterm_scope: str = "current_tab"  # current_tab / current_window / all_windows
    timestamp_style: str = "24hr"  # 12hr / 24hr / date_time / auto
    account_usage: bool = False
    excluded_tools: list[str] | None = None  # tool names to skip auto-accepting
    ask_user_timeout: int = 0  # seconds to wait before auto-accepting AskUserQuestion (0 = auto-accept immediately)

    def __post_init__(self):
        if self.excluded_tools is None:
            self.excluded_tools = []


def load_settings() -> Settings:
    """Load settings from config file, returning defaults if missing."""
    if not os.path.exists(CONFIG_FILE):
        return Settings()
    try:
        with open(CONFIG_FILE) as f:
            data = json.load(f)
        return Settings(**{k: v for k, v in data.items() if k in Settings.__dataclass_fields__})
    except Exception as e:
        log.warning(f"Failed to load settings: {e}")
        return Settings()


def save_settings(settings: Settings) -> None:
    """Write settings to config file atomically."""
    import tempfile
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(asdict(settings), f, indent=2)
            os.replace(tmp_path, CONFIG_FILE)
        except Exception:
            os.unlink(tmp_path)
            raise
    except Exception as e:
        log.warning(f"Failed to save settings: {e}")


# --- Mode / scope / timestamp mappings ---

MODE_OPTIONS = [("Auto", "auto"), ("Manual", "manual"), ("Last Used", "last_used")]
SCOPE_OPTIONS = [
    ("Current tab only", "current_tab"),
    ("All tabs in window", "current_window"),
    ("All windows", "all_windows"),
]
TIMESTAMP_OPTIONS = [
    ("12-hour", "12hr"),
    ("24-hour", "24hr"),
    ("Date + time", "date_time"),
    ("Auto (responsive)", "auto"),
]


class SettingsScreen(ModalScreen[Settings | None]):
    """Modal settings dialog."""

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    SettingsScreen #settings-dialog {
        width: 64;
        max-height: 50;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    SettingsScreen #settings-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }
    SettingsScreen .setting-row {
        height: auto;
        margin-bottom: 1;
    }
    SettingsScreen .setting-label {
        width: 20;
        height: 1;
        padding: 0 1 0 0;
    }
    SettingsScreen .setting-control {
        width: 1fr;
    }
    SettingsScreen .switch-row {
        height: 3;
        margin-bottom: 1;
    }
    SettingsScreen .switch-row .setting-label {
        height: 3;
        content-align-vertical: middle;
    }
    SettingsScreen RadioSet {
        height: auto;
        width: 1fr;
    }
    SettingsScreen Select {
        width: 1fr;
    }
    SettingsScreen Input {
        width: 1fr;
    }
    SettingsScreen .setting-hint {
        color: $text-muted;
        width: 100%;
        margin: -1 0 1 21;
    }
    SettingsScreen #button-row {
        height: 3;
        margin-top: 1;
        align: center middle;
    }
    SettingsScreen #button-row Button {
        margin: 0 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, current: Settings) -> None:
        super().__init__()
        self._settings = current

    def compose(self) -> ComposeResult:
        s = self._settings
        with Vertical(id="settings-dialog"):
            yield Static("Settings", id="settings-title")

            # Default mode
            with Horizontal(classes="setting-row"):
                yield Label("Default mode", classes="setting-label")
                with RadioSet(id="mode-radio", classes="setting-control"):
                    for label, value in MODE_OPTIONS:
                        yield RadioButton(label, value=value == s.default_mode)

            # Theme
            with Horizontal(classes="setting-row"):
                yield Label("Theme", classes="setting-label")
                yield Select(
                    [(t, t) for t in THEMES],
                    value=s.theme,
                    id="theme-select",
                    classes="setting-control",
                )

            # iTerm scope
            with Horizontal(classes="setting-row"):
                yield Label("iTerm scope", classes="setting-label")
                with RadioSet(id="scope-radio", classes="setting-control"):
                    for label, value in SCOPE_OPTIONS:
                        yield RadioButton(label, value=value == s.iterm_scope)

            # Timestamp style
            with Horizontal(classes="setting-row"):
                yield Label("Timestamp", classes="setting-label")
                with RadioSet(id="timestamp-radio", classes="setting-control"):
                    for label, value in TIMESTAMP_OPTIONS:
                        yield RadioButton(label, value=value == s.timestamp_style)

            # Debug toggle
            with Horizontal(classes="switch-row"):
                yield Label("Debug logging", classes="setting-label")
                yield Switch(value=s.debug, id="debug-switch")

            # Account usage toggle
            with Horizontal(classes="switch-row"):
                yield Label("Account usage", classes="setting-label")
                yield Switch(value=s.account_usage, id="usage-switch")

            # Excluded tools
            with Horizontal(classes="setting-row"):
                yield Label("Excluded tools", classes="setting-label")
                yield Input(
                    value=", ".join(s.excluded_tools or []),
                    placeholder="e.g. AskUserQuestion, Bash",
                    id="excluded-tools-input",
                    classes="setting-control",
                )
            yield Static("Comma-separated tool names to skip auto-accepting", classes="setting-hint")

            # AskUserQuestion timeout
            with Horizontal(classes="setting-row"):
                yield Label("Ask user timeout", classes="setting-label")
                yield Input(
                    value=str(s.ask_user_timeout),
                    placeholder="0",
                    id="ask-timeout-input",
                    classes="setting-control",
                    type="integer",
                )
            yield Static("Seconds to wait for user reply (0 = auto-accept immediately)", classes="setting-hint")

            # Buttons
            with Horizontal(id="button-row"):
                yield Button("Save", variant="primary", id="save-btn")
                yield Button("Cancel", variant="default", id="cancel-btn")

    def _get_theme_value(self) -> str:
        """Get theme value, guarding against Select.BLANK."""
        raw = self.query_one("#theme-select", Select).value
        if raw is Select.BLANK:
            return self._settings.theme
        return str(raw)

    def _get_radio_value(self, radio_id: str, options: list[tuple[str, str]]) -> str:
        """Get the value for the selected radio button in a RadioSet."""
        radio_set = self.query_one(f"#{radio_id}", RadioSet)
        idx = radio_set.pressed_index
        if idx < 0 or idx >= len(options):
            return options[0][1]
        return options[idx][1]

    def _collect_settings(self) -> Settings:
        # Parse excluded tools from comma-separated input
        raw_excluded = self.query_one("#excluded-tools-input", Input).value
        excluded_tools = [t.strip() for t in raw_excluded.split(",") if t.strip()] if raw_excluded.strip() else []

        # Parse ask user timeout
        try:
            ask_timeout = max(0, int(self.query_one("#ask-timeout-input", Input).value))
        except (ValueError, TypeError):
            ask_timeout = 0

        return Settings(
            default_mode=self._get_radio_value("mode-radio", MODE_OPTIONS),
            theme=self._get_theme_value(),
            debug=self.query_one("#debug-switch", Switch).value,
            iterm_scope=self._get_radio_value("scope-radio", SCOPE_OPTIONS),
            timestamp_style=self._get_radio_value("timestamp-radio", TIMESTAMP_OPTIONS),
            account_usage=self.query_one("#usage-switch", Switch).value,
            excluded_tools=excluded_tools,
            ask_user_timeout=ask_timeout,
        )

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            settings = self._collect_settings()
            save_settings(settings)
            self.dismiss(settings)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
