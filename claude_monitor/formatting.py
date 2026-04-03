"""Formatting helpers for claude-monitor TUI display."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from claude_monitor.widgets.session_panel import SessionPanel

log = logging.getLogger(__name__)


def _safe_css_id(session_id: str) -> str:
    return "panel-" + session_id.replace("-", "").replace(":", "").replace("/", "")


def _safe_tab_css_id(tab_id: str) -> str:
    return "tab-" + tab_id.replace("-", "").replace(":", "").replace("/", "").replace(".", "")


def _oneline(text: str, max_len: int = 0) -> str:
    """Collapse multi-line text into one line, replacing newlines with the return symbol."""
    joined = " ↵ ".join(line.strip() for line in text.splitlines() if line.strip())
    return joined[:max_len] if max_len else joined


def _format_ask_user_question_inline(tool_input: dict) -> str:
    """Format AskUserQuestion tool_input as a readable inline string for the event log.

    Handles two formats:
    - Simple: tool_input has 'question' (str) directly
    - Structured: tool_input has 'questions' (list of dicts with 'question' and 'options')
      and 'answers' (dict mapping question text to selected answer)
    """
    parts: list[str] = []
    questions = tool_input.get("questions", [])
    answers = tool_input.get("answers", {})

    if questions:
        for q in questions:
            q_text = q.get("question", "")
            options = q.get("options", [])
            selected = answers.get(q_text, "")
            option_labels = [o.get("label", "") for o in options if o.get("label")]
            if q_text:
                line = f' "{q_text}"'
                if option_labels:
                    choices_str = " / ".join(option_labels)
                    line += f" [{choices_str}]"
                if selected:
                    line += f" -> [bold]{selected}[/]"
                parts.append(line)
    else:
        # Simple format with just 'question' key
        question = tool_input.get("question", "")
        if question:
            parts.append(f' "{question[:200]}"')
        elif tool_input:
            # Fallback: show first couple keys
            for k, v in list(tool_input.items())[:2]:
                parts.append(f" {k}={str(v)[:80]}")

    return "".join(parts) if parts else ""


def _format_ask_user_question_detail(data: dict) -> str:
    """Format AskUserQuestion event as a multi-line detail string for the QuestionsScreen."""
    tool_input = data.get("tool_input", {})
    questions = tool_input.get("questions", [])
    # Prefer merged answers from PostToolUse, fall back to tool_input answers
    answers = data.get("_answers", tool_input.get("answers", {}))
    decision = data.get("_decision", "allowed")
    is_auto = decision == "timeout"
    lines: list[str] = []

    if questions:
        for i, q in enumerate(questions):
            q_text = q.get("question", "")
            options = q.get("options", [])
            selected = answers.get(q_text, "")
            if q_text:
                lines.append(f"    [dim]Q:[/] {q_text}")
            for o in options:
                label = o.get("label", "")
                desc = o.get("description", "")
                if label:
                    if selected and label == selected:
                        if is_auto:
                            marker = "[bold cyan]>>[/]"
                            lines.append(f"      {marker} [bold cyan]{label}[/]" + (f"  [dim]{desc}[/]" if desc else ""))
                        else:
                            marker = "[bold green]>>[/]"
                            lines.append(f"      {marker} [bold green]{label}[/]" + (f"  [dim]{desc}[/]" if desc else ""))
                    else:
                        lines.append(f"         {label}" + (f"  [dim]{desc}[/]" if desc else ""))
            if selected:
                mode = "[cyan]auto[/]" if is_auto else "[green]manual[/]"
                lines.append(f"    [dim]Answer:[/] [bold]{selected}[/]  ({mode})")
            if i < len(questions) - 1:
                lines.append("")
    else:
        question = tool_input.get("question", "")
        if question:
            lines.append(f"    [dim]Q:[/] {question[:300]}")
        elif tool_input:
            for k, v in list(tool_input.items())[:3]:
                lines.append(f"    [dim]{k}:[/] {str(v)[:200]}")

    return "\n".join(lines)


def format_event(
    data: dict,
    event_name: str,
    *,
    is_pane_paused: Callable[[str], bool],
    get_panel: Callable[[dict], SessionPanel | None],
    oneline: Callable[[str, int], str],
    self_sid: str | None = None,
) -> tuple[str | None, str | None]:
    """Shared hook-event formatter. Returns (label, detail) or (None, None)."""
    panel_ref = get_panel(data)
    _n_agents = len(panel_ref.active_agents) if panel_ref else 0
    _ag = f"[dim]\\[ag{_n_agents}][/] " if _n_agents > 0 else ""
    pause_sid = panel_ref.session_id if panel_ref else data.get("session_id", "")

    if event_name == "PermissionRequest":
        tool = data.get("tool_name", "?")
        tool_input = data.get("tool_input", {})
        detail = ""
        if tool == "AskUserQuestion":
            detail = _format_ask_user_question_inline(tool_input)
        elif tool == "Bash":
            detail = f" `{oneline(tool_input.get('command', ''))}`"
        elif tool in ("Edit", "Write"):
            detail = f" `{tool_input.get('file_path', '')}`"
        elif tool == "WebFetch":
            detail = f" `{tool_input.get('url', '')}`"
        if data.get("_excluded_tool"):
            return f"[bold red]{'MANUAL':<8}[/]", f"{_ag}{tool}{detail}"
        decision = data.get("_decision", "allowed")
        if decision == "deferred":
            return f"[bold yellow]{'DEFERRED':<8}[/]", f"{_ag}{tool}{detail}"
        if decision == "timeout":
            timeout_s = data.get("_ask_timeout", "?")
            return f"[bold cyan]{'TIMEOUT':<8}[/]", f"{_ag}{tool}{detail} ({timeout_s}s)"
        if pause_sid and is_pane_paused(pause_sid):
            return f"[bold yellow]{'PAUSED':<8}[/]", f"{_ag}{tool}{detail}"
        return f"[bold green]{'ALLOWED':<8}[/]", f"{_ag}{tool}{detail}"

    elif event_name == "PostToolUse":
        tool = data.get("tool_name", "?")
        if tool == "AskUserQuestion":
            answers = data.get("tool_input", {}).get("answers", {})
            answer_vals = [v for v in answers.values() if v]
            if not answer_vals:
                return None, None
            answer_text = ", ".join(answer_vals)
            return f"[bold green]{'ANSWER':<8}[/]", f"{_ag}AskUserQuestion -> [bold]{answer_text}[/]"
        return None, None

    elif event_name == "Notification":
        ntype = data.get("notification_type", "")
        message = data.get("message", "")
        if ntype == "idle_prompt":
            return f"[dim]{'IDLE':<8}[/]", f"{_ag}{oneline(message, 80)}"
        elif ntype == "ask_timeout_complete":
            if data.get("_auto_accepted"):
                return f"[bold cyan]{'AUTO':<8}[/]", message
            return None, None
        elif ntype == "permission_prompt":
            if self_sid and pause_sid == self_sid:
                return None, None
            if pause_sid and not is_pane_paused(pause_sid):
                pending = getattr(panel_ref, "_pending_timeout", None) if panel_ref else None
                if pending and pending > time.time():
                    return None, None
                return f"[bold green]{'APPROVED':<8}[/]", f"{_ag}{message}"
        return f"[bold cyan]{'NOTIFY':<8}[/]", f"{_ag}{oneline(message, 80)}"

    elif event_name == "SubagentStart":
        agent_id = data.get("agent_id", "?")
        agent_type = data.get("agent_type", "?")
        return f"[bold magenta]{'AGENT+':<8}[/]", f"{agent_type} [{agent_id[:8]}]"

    elif event_name == "SubagentStop":
        agent_id = data.get("agent_id", "?")
        agent_type = data.get("agent_type", "?")
        return f"[magenta]{'AGENT-':<8}[/]", f"{agent_type} [{agent_id[:8]}]"

    elif event_name == "SessionStart":
        return f"[bold green]{'SESSION+':<8}[/]", f"{_ag}session started"

    elif event_name == "SessionEnd":
        return f"[dim]{'SESSION-':<8}[/]", f"{_ag}session ended"

    elif event_name == "StopFailure":
        error = data.get("error", {})
        if isinstance(error, dict):
            msg = error.get("message", "API error")
        else:
            msg = str(error) if error else "API error"
        return f"[bold red]{'FAIL':<8}[/]", f"{_ag}{oneline(msg, 80)}"

    elif event_name == "PermissionDenied":
        tool = data.get("tool_name", "?")
        reason = data.get("reason", "")
        detail = f"{tool}  [dim]{reason}[/]" if reason else tool
        return f"[bold red]{'DENIED':<8}[/]", f"{_ag}{detail}"

    elif event_name == "PostCompact":
        return f"[dim]{'COMPACT':<8}[/]", f"{_ag}context compacted"

    elif event_name == "TaskCreated":
        subject = data.get("subject") or data.get("description") or "?"
        return f"[bold blue]{'TASK+':<8}[/]", f"{_ag}{oneline(subject, 80)}"

    elif event_name == "CwdChanged":
        cwd = data.get("cwd") or data.get("new_cwd") or "?"
        return f"[dim]{'CWD':<8}[/]", f"{_ag}{cwd}"

    return None, None
