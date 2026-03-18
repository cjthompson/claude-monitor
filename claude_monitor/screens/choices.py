"""ChoicesScreen modal for claude-monitor TUI."""

import json
import logging
import os
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static

from claude_monitor import EVENTS_FILE
from claude_monitor.widgets.scrollbar import HorizontalScrollBarRender

log = logging.getLogger(__name__)


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
        entries: list[str] = []
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
            sug_parts: list[str] = []
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
