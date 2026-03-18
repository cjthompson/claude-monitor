"""QuestionsScreen modal for claude-monitor TUI."""

import json
import logging
import os
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import RichLog, Static

from claude_monitor import EVENTS_FILE
from claude_monitor.formatting import _format_ask_user_question_detail
from claude_monitor.widgets.scrollbar import HorizontalScrollBarRender

log = logging.getLogger(__name__)


class QuestionsScreen(ModalScreen):
    """Review screen showing AskUserQuestion events only.

    Opened via 'u' key. Reads events.jsonl and shows formatted AskUserQuestion entries.
    """

    DEFAULT_CSS = """
    QuestionsScreen {
        align: center middle;
    }
    QuestionsScreen #questions-dialog {
        width: 90%;
        height: 85%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    QuestionsScreen #questions-title {
        text-align: center;
        text-style: bold;
        width: 100%;
        margin-bottom: 1;
    }
    QuestionsScreen RichLog {
        height: 1fr;
    }
    QuestionsScreen #questions-footer {
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
        with Vertical(id="questions-dialog"):
            yield Static("AskUserQuestion Log", id="questions-title")
            yield RichLog(markup=True, wrap=True, id="questions-log")
            yield Static("[dim]ESC to close[/]", id="questions-footer")

    def on_mount(self) -> None:
        rl = self.query_one("#questions-log", RichLog)
        entries = self._load_questions()
        if not entries:
            rl.write("[dim]No AskUserQuestion events recorded yet.[/]")
            return
        for entry in entries:
            rl.write(entry)
        rl.scroll_end(animate=False)
        self.query_one("#questions-log", RichLog).horizontal_scrollbar.renderer = HorizontalScrollBarRender

    def _load_questions(self) -> list[str]:
        """Load AskUserQuestion events from events.jsonl, merging answers from PostToolUse."""
        perm_events: list[dict] = []
        # Map session_id -> list of PostToolUse answers (in order) for merging
        post_answers: dict[str, list[dict]] = {}
        try:
            with open(EVENTS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if data.get("tool_name") != "AskUserQuestion":
                        continue
                    evt = data.get("hook_event_name", "")
                    if evt == "PermissionRequest":
                        perm_events.append(data)
                    elif evt == "PostToolUse":
                        sid = data.get("session_id", "")
                        post_answers.setdefault(sid, []).append(data)
        except FileNotFoundError:
            log.debug("QuestionsScreen: events file not found")

        # Merge answers: match each PermissionRequest with the next PostToolUse from same session
        post_idx: dict[str, int] = {}
        for perm in perm_events:
            sid = perm.get("session_id", "")
            idx = post_idx.get(sid, 0)
            posts = post_answers.get(sid, [])
            if idx < len(posts):
                answers = posts[idx].get("tool_input", {}).get("answers", {})
                perm["_answers"] = answers
                post_idx[sid] = idx + 1

        return [self._format_question(d) for d in perm_events[-200:]]

    def _format_question(self, data: dict) -> str:
        ts = datetime.fromtimestamp(data.get("_timestamp", 0))
        t = ts.strftime("%Y-%m-%d %H:%M:%S")
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
        elif decision == "timeout":
            badge = "[bold cyan]TIMEOUT [/]"
        else:
            badge = "[bold green]ALLOWED [/]"

        detail = _format_ask_user_question_detail(data)
        detail_block = f"\n{detail}" if detail else ""

        return (
            f"[bold]{t}[/]  [{session}]  [bold]{project}[/]\n"
            f"  {badge} [bold]AskUserQuestion[/]{detail_block}\n"
        )

    def action_dismiss(self) -> None:
        self.app.pop_screen()
