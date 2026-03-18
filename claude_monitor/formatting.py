"""Formatting helpers for claude-monitor TUI display."""

import logging

log = logging.getLogger(__name__)


def _safe_css_id(session_id: str) -> str:
    return "panel-" + session_id.replace("-", "").replace(":", "").replace("/", "")


def _safe_tab_css_id(tab_id: str) -> str:
    return "tab-" + tab_id.replace("-", "").replace(":", "").replace("/", "").replace(".", "")


def _oneline(text: str, max_len: int = 0) -> str:
    """Collapse multi-line text into one line, replacing newlines with the return symbol."""
    joined = " \u21b5 ".join(line.strip() for line in text.splitlines() if line.strip())
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
