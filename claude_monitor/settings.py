"""Settings management for claude-monitor.

Persists to ~/.config/claude-monitor/config.json.
Provides a SettingsScreen (ModalScreen) for editing settings in the TUI.
"""

import json
import logging
import os
from dataclasses import asdict, dataclass

from textual.app import ComposeResult
from textual.containers import Horizontal, ScrollableContainer, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, Label, Select, Static, Switch, TextArea

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
    ask_user_timeout: int = 0  # seconds to wait before auto-accepting AskUserQuestion (0 = instant)
    sparkline_bucket_secs: int = 5  # seconds per sparkline bucket (events/Ns)
    oauth_json: str = ""  # JSON with access_token (required), refresh_token, expires_at (optional)
    dashboard_height: int = 12  # dashboard pane height in lines (simple mode, expanded)

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
    """Write settings to config file atomically.

    oauth_json is excluded — it's sensitive and kept in memory only.
    """
    import tempfile
    os.makedirs(CONFIG_DIR, exist_ok=True)
    try:
        data = asdict(settings)
        data.pop("oauth_json", None)
        fd, tmp_path = tempfile.mkstemp(dir=CONFIG_DIR, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(data, f, indent=2)
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


_MASKED_PLACEHOLDER = "••••••••"


def _mask_oauth_json(raw: str) -> str:
    """Mask token values in OAuth JSON for display. Shows first 8 chars + masked remainder."""
    if not raw:
        return ""
    try:
        data = json.loads(raw)
        for key in ("access_token", "refresh_token"):
            val = data.get(key, "")
            if val and len(val) > 8:
                data[key] = val[:8] + _MASKED_PLACEHOLDER
            elif val:
                data[key] = _MASKED_PLACEHOLDER
        return json.dumps(data, indent=2, ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        return raw


class SettingsScreen(ModalScreen[Settings | None]):
    """Modal settings dialog."""

    DEFAULT_CSS = """
    SettingsScreen {
        align: center middle;
    }
    SettingsScreen #settings-dialog {
        width: 64;
        max-height: 80vh;
        background: $surface;
        border: thick $primary;
        padding: 0;
    }
    SettingsScreen #settings-scroll {
        padding: 1 2 0 2;
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
        height: 3;
        padding: 0 1 0 0;
        content-align-vertical: middle;
    }
    SettingsScreen .setting-control {
        width: 1fr;
    }
    SettingsScreen .switch-row {
        height: 3;
        margin-bottom: 1;
    }
    SettingsScreen Select {
        width: 1fr;
    }
    SettingsScreen Input {
        width: 1fr;
    }
    SettingsScreen TextArea {
        width: 1fr;
        height: 6;
    }
    SettingsScreen .textarea-row {
        height: auto;
        max-height: 8;
        margin-bottom: 1;
    }
    SettingsScreen .textarea-row .setting-label {
        height: 6;
    }
    SettingsScreen .textarea-row Vertical {
        width: 20;
        height: auto;
    }
    SettingsScreen #oauth-clear-btn {
        width: auto;
        height: 1;
        margin: 0;
        padding: 0;
    }
    SettingsScreen #oauth-clear-btn:hover {
        color: $error;
    }
    SettingsScreen .setting-hint {
        color: $text-muted;
        width: 100%;
        margin: -1 0 1 21;
    }
    SettingsScreen #button-row {
        height: 3;
        padding: 0 2;
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

    def __init__(self, current: Settings, simple_mode: bool = False) -> None:
        super().__init__()
        self._settings = current
        self._simple_mode = simple_mode
        self._masked_oauth = _mask_oauth_json(current.oauth_json)

    def compose(self) -> ComposeResult:
        s = self._settings
        with Vertical(id="settings-dialog"):
            with ScrollableContainer(id="settings-scroll"):
                yield Static("Settings", id="settings-title")

                # Default mode
                with Horizontal(classes="setting-row"):
                    yield Label("Default mode", classes="setting-label")
                    yield Select(
                        MODE_OPTIONS, value=s.default_mode,
                        id="mode-select", classes="setting-control",
                    )

                # Theme
                with Horizontal(classes="setting-row"):
                    yield Label("Theme", classes="setting-label")
                    yield Select(
                        [(t, t) for t in THEMES],
                        value=s.theme,
                        id="theme-select",
                        classes="setting-control",
                    )

                # iTerm scope (hidden in simple mode)
                if not self._simple_mode:
                    with Horizontal(classes="setting-row"):
                        yield Label("iTerm scope", classes="setting-label")
                        yield Select(
                            SCOPE_OPTIONS, value=s.iterm_scope,
                            id="scope-select", classes="setting-control",
                        )

                # Timestamp style
                with Horizontal(classes="setting-row"):
                    yield Label("Timestamp", classes="setting-label")
                    yield Select(
                        TIMESTAMP_OPTIONS, value=s.timestamp_style,
                        id="timestamp-select", classes="setting-control",
                    )

                # Debug toggle
                with Horizontal(classes="switch-row"):
                    yield Label("Debug logging", classes="setting-label")
                    yield Switch(value=s.debug, id="debug-switch")

                # Account usage toggle
                with Horizontal(classes="switch-row"):
                    yield Label("Account usage", classes="setting-label")
                    yield Switch(value=s.account_usage, id="usage-switch")

                # OAuth JSON
                with Horizontal(classes="textarea-row"):
                    with Vertical():
                        yield Label("OAuth token", classes="setting-label")
                        yield Static("[dim]\\[clear][/]", id="oauth-clear-btn")
                    yield TextArea(
                        self._masked_oauth,
                        id="oauth-json-input",
                        classes="setting-control",
                        language="json",
                    )
                yield Static('JSON: access_token (required), refresh_token, expires_at (optional). Not saved to disk.', classes="setting-hint")

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
                yield Static("Seconds to wait before auto-accepting AskUserQuestion (0 = instant, max 300)", classes="setting-hint")

            # Buttons always visible outside scroll area
            with Horizontal(id="button-row"):
                yield Button("Save", variant="primary", id="save-btn", disabled=True)
                yield Button("Cancel", variant="default", id="cancel-btn")

    def _has_changes(self) -> bool:
        """Check if current form values differ from the original settings."""
        try:
            return asdict(self._collect_settings()) != asdict(self._settings)
        except Exception:
            return False

    def _refresh_save_button(self) -> None:
        try:
            self.query_one("#save-btn", Button).disabled = not self._has_changes()
        except Exception:
            pass

    def on_select_changed(self, event: Select.Changed) -> None:
        self._refresh_save_button()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self._refresh_save_button()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_save_button()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._refresh_save_button()

    def _get_select_value(self, select_id: str, fallback: str) -> str:
        """Get value from a Select widget, returning fallback if BLANK."""
        raw = self.query_one(f"#{select_id}", Select).value
        if raw is Select.BLANK:
            return fallback
        return str(raw)

    def _collect_settings(self) -> Settings:
        # Parse excluded tools from comma-separated input
        raw_excluded = self.query_one("#excluded-tools-input", Input).value
        excluded_tools = [t.strip() for t in raw_excluded.split(",") if t.strip()] if raw_excluded.strip() else []

        # Parse ask user timeout (max 300s = 5 min, must fit within hook timeout)
        try:
            ask_timeout = min(300, max(0, int(self.query_one("#ask-timeout-input", Input).value)))
        except (ValueError, TypeError):
            ask_timeout = 0

        oauth_text = self.query_one("#oauth-json-input", TextArea).text.strip()
        # If the user didn't edit the masked display, keep the original token
        if oauth_text == self._masked_oauth.strip():
            oauth_json = self._settings.oauth_json
        else:
            oauth_json = oauth_text

        iterm_scope = (
            self._settings.iterm_scope
            if self._simple_mode
            else self._get_select_value("scope-select", self._settings.iterm_scope)
        )
        return Settings(
            default_mode=self._get_select_value("mode-select", self._settings.default_mode),
            theme=self._get_select_value("theme-select", self._settings.theme),
            debug=self.query_one("#debug-switch", Switch).value,
            iterm_scope=iterm_scope,
            timestamp_style=self._get_select_value("timestamp-select", self._settings.timestamp_style),
            account_usage=self.query_one("#usage-switch", Switch).value,
            excluded_tools=excluded_tools,
            ask_user_timeout=ask_timeout,
            oauth_json=oauth_json,
        )

    def on_click(self, event) -> None:
        if hasattr(event, 'widget') and getattr(event.widget, 'id', None) == "oauth-clear-btn":
            self.query_one("#oauth-json-input", TextArea).clear()
            self._refresh_save_button()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save-btn":
            settings = self._collect_settings()
            save_settings(settings)
            self.dismiss(settings)
        elif event.button.id == "cancel-btn":
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)
