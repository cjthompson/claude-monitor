"""Settings management for claude-monitor.

Persists to ~/.config/claude-monitor/config.json.
Provides a SettingsScreen (ModalScreen) for editing settings in the TUI.
"""

import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from typing import TypedDict

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

    def __post_init__(self) -> None:
        if self.excluded_tools is None:
            self.excluded_tools = []
        # Validate enum-like string fields
        if self.default_mode not in ("auto", "manual", "last_used"):
            self.default_mode = "auto"
        if self.iterm_scope not in ("current_tab", "current_window", "all_windows"):
            self.iterm_scope = "current_tab"
        if self.timestamp_style not in ("12hr", "24hr", "date_time", "auto"):
            self.timestamp_style = "24hr"
        # Clamp numeric fields
        self.ask_user_timeout = max(0, min(300, int(self.ask_user_timeout)))
        self.sparkline_bucket_secs = max(1, int(self.sparkline_bucket_secs))
        self.dashboard_height = max(3, int(self.dashboard_height))


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


# --- Mode / scope / timestamp option lists ---

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


# --- Declarative field definitions ---

class FieldDef(TypedDict, total=False):
    name: str            # Settings dataclass field name
    label: str           # Human-readable label shown in the UI
    widget_type: str     # "switch" | "select" | "input" | "textarea"
    options: list[tuple[str, str]] | None   # For select widgets
    description: str | None                 # Hint text shown below the row
    placeholder: str | None                 # For input widgets
    input_type: str | None                  # Textual Input type (e.g. "integer")
    simple_mode_hidden: bool                # Hide this field when simple_mode=True


FIELD_DEFS: list[FieldDef] = [
    {"name": "default_mode",    "label": "Default mode",    "widget_type": "select",   "options": MODE_OPTIONS},
    {"name": "theme",           "label": "Theme",           "widget_type": "select",   "options": [(t, t) for t in THEMES]},
    {"name": "iterm_scope",     "label": "iTerm scope",     "widget_type": "select",   "options": SCOPE_OPTIONS,   "simple_mode_hidden": True},
    {"name": "timestamp_style", "label": "Timestamp",       "widget_type": "select",   "options": TIMESTAMP_OPTIONS},
    {"name": "debug",           "label": "Debug logging",   "widget_type": "switch"},
    {"name": "account_usage",   "label": "Account usage",   "widget_type": "switch"},
    {
        "name": "oauth_json",
        "label": "OAuth token",
        "widget_type": "textarea",
        "description": "JSON: access_token (required), refresh_token, expires_at (optional). Not saved to disk.",
    },
    {
        "name": "excluded_tools",
        "label": "Excluded tools",
        "widget_type": "input",
        "placeholder": "e.g. AskUserQuestion, Bash",
        "description": "Comma-separated tool names to skip auto-accepting",
    },
    {
        "name": "ask_user_timeout",
        "label": "Ask user timeout",
        "widget_type": "input",
        "placeholder": "0",
        "input_type": "integer",
        "description": "Seconds to wait before auto-accepting AskUserQuestion (0 = instant, max 300)",
    },
]


# --- OAuth masking helpers ---

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


def _widget_id(field_name: str) -> str:
    """Derive a stable widget ID from a field name."""
    return f"field-{field_name.replace('_', '-')}"


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

    # ------------------------------------------------------------------
    # Compose — driven by FIELD_DEFS
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        s = self._settings
        with Vertical(id="settings-dialog"):
            with ScrollableContainer(id="settings-scroll"):
                yield Static("Settings", id="settings-title")

                for fd in FIELD_DEFS:
                    if self._simple_mode and fd.get("simple_mode_hidden"):
                        continue

                    name = fd["name"]
                    label = fd["label"]
                    wtype = fd["widget_type"]
                    widget_id = _widget_id(name)
                    desc = fd.get("description")

                    if wtype == "switch":
                        with Horizontal(classes="switch-row"):
                            yield Label(label, classes="setting-label")
                            yield Switch(value=getattr(s, name), id=widget_id)

                    elif wtype == "select":
                        options = fd.get("options") or []
                        with Horizontal(classes="setting-row"):
                            yield Label(label, classes="setting-label")
                            yield Select(
                                options,
                                value=getattr(s, name),
                                id=widget_id,
                                classes="setting-control",
                            )

                    elif wtype == "textarea":
                        # Special case: oauth_json uses masked display + clear button
                        display_value = self._masked_oauth if name == "oauth_json" else str(getattr(s, name) or "")
                        with Horizontal(classes="textarea-row"):
                            with Vertical():
                                yield Label(label, classes="setting-label")
                                if name == "oauth_json":
                                    yield Static("[dim]\\[clear][/]", id="oauth-clear-btn")
                            yield TextArea(
                                display_value,
                                id=widget_id,
                                classes="setting-control",
                                language="json",
                            )

                    elif wtype == "input":
                        raw_val = getattr(s, name)
                        if isinstance(raw_val, list):
                            display_value = ", ".join(raw_val)
                        else:
                            display_value = str(raw_val) if raw_val is not None else ""
                        kwargs: dict = dict(
                            value=display_value,
                            id=widget_id,
                            classes="setting-control",
                        )
                        if fd.get("placeholder"):
                            kwargs["placeholder"] = fd["placeholder"]
                        if fd.get("input_type"):
                            kwargs["type"] = fd["input_type"]
                        with Horizontal(classes="setting-row"):
                            yield Label(label, classes="setting-label")
                            yield Input(**kwargs)

                    if desc:
                        yield Static(desc, classes="setting-hint")

            # Buttons always visible outside scroll area
            with Horizontal(id="button-row"):
                yield Button("Save", variant="primary", id="save-btn", disabled=True)
                yield Button("Cancel", variant="default", id="cancel-btn")

    # ------------------------------------------------------------------
    # Single generic change handler
    # ------------------------------------------------------------------

    def on_select_changed(self, event: Select.Changed) -> None:
        self._refresh_save_button()

    def on_switch_changed(self, event: Switch.Changed) -> None:
        self._refresh_save_button()

    def on_input_changed(self, event: Input.Changed) -> None:
        self._refresh_save_button()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        self._refresh_save_button()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

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

    def _get_select_value(self, widget_id: str, fallback: str) -> str:
        """Get value from a Select widget, returning fallback if BLANK."""
        raw = self.query_one(f"#{widget_id}", Select).value
        if raw is Select.BLANK:
            return fallback
        return str(raw)

    def _collect_settings(self) -> Settings:
        """Read all form widgets and build a Settings instance."""
        s = self._settings

        # excluded_tools: comma-separated → list
        raw_excluded = self.query_one(f"#{_widget_id('excluded_tools')}", Input).value
        excluded_tools = [t.strip() for t in raw_excluded.split(",") if t.strip()] if raw_excluded.strip() else []

        # ask_user_timeout: integer input, clamped
        try:
            ask_timeout = min(300, max(0, int(self.query_one(f"#{_widget_id('ask_user_timeout')}", Input).value)))
        except (ValueError, TypeError):
            ask_timeout = 0

        # oauth_json: keep original if user didn't edit the masked display
        oauth_text = self.query_one(f"#{_widget_id('oauth_json')}", TextArea).text.strip()
        if oauth_text == self._masked_oauth.strip():
            oauth_json = s.oauth_json
        else:
            oauth_json = oauth_text

        # iterm_scope: hidden in simple mode → preserve original
        if self._simple_mode:
            iterm_scope = s.iterm_scope
        else:
            iterm_scope = self._get_select_value(_widget_id("iterm_scope"), s.iterm_scope)

        return Settings(
            default_mode=self._get_select_value(_widget_id("default_mode"), s.default_mode),
            theme=self._get_select_value(_widget_id("theme"), s.theme),
            debug=self.query_one(f"#{_widget_id('debug')}", Switch).value,
            iterm_scope=iterm_scope,
            timestamp_style=self._get_select_value(_widget_id("timestamp_style"), s.timestamp_style),
            account_usage=self.query_one(f"#{_widget_id('account_usage')}", Switch).value,
            excluded_tools=excluded_tools,
            ask_user_timeout=ask_timeout,
            oauth_json=oauth_json,
            sparkline_bucket_secs=s.sparkline_bucket_secs,
            dashboard_height=s.dashboard_height,
        )

    # ------------------------------------------------------------------
    # Event handlers
    # ------------------------------------------------------------------

    def on_click(self, event) -> None:
        if hasattr(event, "widget") and getattr(event.widget, "id", None) == "oauth-clear-btn":
            self.query_one(f"#{_widget_id('oauth_json')}", TextArea).clear()
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
