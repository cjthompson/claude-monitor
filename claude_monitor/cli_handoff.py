"""CLI for browsing and managing Claude Code session hand-off summaries.

Installed as the ``claude-monitor-handoff`` console script (and runnable via
``python -m claude_monitor.cli_handoff``). Reads/writes the hand-off store
maintained by ``claude_monitor.handoff`` — a small local archive of "what was
I doing" summaries captured when a session ends (or on request), so the next
session in a project can pick up where the last one left off.

Subcommands:
  list                A compact, newest-first table of recent sessions.
  show <id|latest>     Full rendered entry for one session.
  digest               The morning-standup view: recent sessions grouped by
                        project. This is the primary command.
  capture               Manually capture the current (or given) session.
  prune                 Trim each project's history down to N entries.

Output is plain text (no rich/textual). ANSI is limited to bold titles/headers
and dim timestamps, and is suppressed automatically when stdout is not a TTY,
when ``NO_COLOR`` is set, or when ``--no-color`` is passed.
"""

import argparse
import logging
import os
import re
import sys
import time

from claude_monitor import fmt_duration, handoff, llm
from claude_monitor.settings import load_settings

log = logging.getLogger("claude_monitor.cli_handoff")

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"

_MD_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")


def _err(msg: str) -> None:
    print(msg, file=sys.stderr)


def _use_color(no_color_flag: bool) -> bool:
    """Decide whether to emit ANSI codes: --no-color / NO_COLOR / non-TTY all disable it."""
    if no_color_flag:
        return False
    if os.environ.get("NO_COLOR") is not None:
        return False
    return sys.stdout.isatty()


def _style(text: str, code: str, use_color: bool) -> str:
    return f"{code}{text}{RESET}" if use_color else text


def _render_markdown_plain(md: str, use_color: bool) -> str:
    """Render the markdown returned by handoff.render_entry/render_digest as plain text.

    Not a full markdown renderer — just enough to make headings and **bold**
    spans readable (and stylable) in a terminal, without pulling in rich.
    """
    lines = []
    for line in md.splitlines():
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            lines.append(_style(heading, BOLD, use_color))
            continue
        lines.append(_MD_BOLD_RE.sub(lambda m: _style(m.group(1), BOLD, use_color), line))
    return "\n".join(lines)


def _empty_store_message() -> str:
    return (
        "No hand-off sessions recorded yet.\n\n"
        "Hand-off capture is off by default. To turn it on, set handoff_enabled = true\n"
        "in ~/.config/claude-monitor/config.json (or via the TUI settings screen), then\n"
        "end or idle-out a session to capture one automatically.\n\n"
        "You can also capture the current session right now:\n"
        "  claude-monitor-handoff capture"
    )


def _relative_time(epoch: float | None) -> str:
    if epoch is None:
        return "?"
    delta = max(0.0, time.time() - epoch)
    return fmt_duration(delta, compact=True) + " ago"


def _since_from_days(days: int | None) -> float | None:
    if not days:
        return None
    return time.time() - days * 86400


# --- subcommands ---------------------------------------------------------


def cmd_list(args: argparse.Namespace, use_color: bool) -> int:
    since = _since_from_days(args.days)
    entries = handoff.list_entries(project=args.project, limit=args.limit, since=since)
    if not entries:
        print(_empty_store_message())
        return 0

    header = f"{'WHEN':<10} {'TITLE':<42} {'PROJECT':<16} {'BRANCH':<16} {'AGENTS':>6}  LLM"
    print(_style(header, BOLD, use_color))
    for e in entries:
        when = _relative_time(e.started_at)
        title = e.title or "(untitled)"
        if len(title) > 42:
            title = title[:39] + "..."
        branch = e.git_branch or "-"
        agents = str(len(e.agents))
        has_llm = "yes" if e.summary else "no"
        # Pad the plain string to width *before* wrapping it in ANSI codes —
        # padding after would count the escape bytes toward the field width.
        when_padded = f"{when:<10}"
        line = (
            f"{_style(when_padded, DIM, use_color)} {title:<42} {e.project_slug:<16} "
            f"{branch:<16} {agents:>6}  {has_llm}"
        )
        print(line)
    return 0


def cmd_show(args: argparse.Namespace, use_color: bool) -> int:
    if args.session_id == "latest":
        entries = handoff.list_entries(project=args.project, limit=1)
        if not entries:
            print(_empty_store_message())
            return 0
        entry = entries[0]
    else:
        entry = handoff.load_entry(args.session_id, slug=args.project)
        if entry is None:
            _err(f"Error: no hand-off entry found for session '{args.session_id}'")
            return 1

    print(_render_markdown_plain(handoff.render_entry(entry), use_color))
    return 0


def cmd_digest(args: argparse.Namespace, use_color: bool) -> int:
    since = _since_from_days(args.days)
    entries = handoff.list_entries(since=since)
    if not entries:
        print(_empty_store_message())
        return 0
    print(_render_markdown_plain(handoff.render_digest(entries), use_color))
    return 0


def cmd_capture(args: argparse.Namespace, use_color: bool) -> int:
    settings = load_settings()
    cwd = args.cwd or os.getcwd()

    session_id = args.session
    if not session_id:
        latest = handoff.latest_for_cwd(cwd)
        if latest is None:
            _err(
                f"Error: could not determine a session id for '{cwd}'; "
                "pass --session <id> explicitly"
            )
            return 1
        session_id = latest.session_id

    with_llm = True if args.llm else bool(getattr(settings, "handoff_llm_enabled", False))
    if with_llm:
        transport = getattr(settings, "handoff_llm_transport", "minimax")
        if not llm.available(transport):
            provider = llm.PROVIDERS.get(transport)
            env_name = provider.api_key_env if provider else "the provider API key"
            _err(
                f"Error: {env_name} is not set; cannot generate an LLM summary "
                f"(transport={transport})."
            )
            _err(f"Set {env_name}, or omit --llm to capture without a summary.")
            return 1

    entry = handoff.capture(session_id, cwd, reason="manual", with_llm=with_llm, settings=settings)
    if entry is None:
        _err(f"Error: could not capture session '{session_id}' (no transcript found?)")
        return 1

    print(f"Captured hand-off entry for session {entry.session_id} ({entry.project_slug})")
    return 0


def cmd_prune(args: argparse.Namespace, use_color: bool) -> int:
    settings = load_settings()
    keep = (
        args.keep
        if args.keep is not None
        else int(getattr(settings, "handoff_retain_per_project", 5))
    )
    if keep < 1:
        _err("Error: --keep must be at least 1")
        return 1

    entries = handoff.list_entries()
    if not entries:
        print(_empty_store_message())
        return 0

    slugs = sorted({e.project_slug for e in entries})
    for slug in slugs:
        handoff.prune(slug, keep)

    noun = "entry" if keep == 1 else "entries"
    print(f"Pruned {len(slugs)} project(s) to {keep} {noun} each.")
    return 0


# --- argument parsing ------------------------------------------------------


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-monitor-handoff",
        description="Browse and manage Claude Code session hand-off summaries.",
    )
    parser.add_argument(
        "--no-color", action="store_true", help="disable ANSI output (also honours NO_COLOR)"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="table of recent sessions, newest first")
    p_list.add_argument("--project", metavar="SLUG", help="only this project")
    p_list.add_argument("--days", type=int, metavar="N", help="only sessions from the last N days")
    p_list.add_argument("--limit", type=int, metavar="N", help="cap the number of rows")
    p_list.set_defaults(func=cmd_list)

    p_show = sub.add_parser("show", help="full rendered entry for one session")
    p_show.add_argument("session_id", metavar="<session_id|latest>")
    p_show.add_argument("--project", metavar="SLUG", help="scope 'latest' to this project")
    p_show.set_defaults(func=cmd_show)

    p_digest = sub.add_parser("digest", help="morning-standup digest, grouped by project")
    p_digest.add_argument(
        "--days", type=int, default=1, metavar="N", help="look back N days (default: 1)"
    )
    p_digest.set_defaults(func=cmd_digest)

    p_capture = sub.add_parser("capture", help="manually capture a session now")
    p_capture.add_argument(
        "--session", metavar="ID", help="session id (default: most recent for --cwd)"
    )
    p_capture.add_argument(
        "--cwd", metavar="PATH", help="working directory (default: current directory)"
    )
    p_capture.add_argument(
        "--llm", action="store_true", help="force an LLM summary for this capture"
    )
    p_capture.set_defaults(func=cmd_capture)

    p_prune = sub.add_parser("prune", help="trim each project's history")
    p_prune.add_argument(
        "--keep", type=int, metavar="N", help="entries to retain per project (default: settings)"
    )
    p_prune.set_defaults(func=cmd_prune)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.WARNING, format="[%(levelname)s] %(message)s", stream=sys.stderr
    )

    use_color = _use_color(args.no_color)
    try:
        return args.func(args, use_color)
    except OSError as e:
        _err(f"Error: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
