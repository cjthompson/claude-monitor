"""Read-only parsing of Claude Code session transcripts.

Transcripts live at ``~/.claude/projects/<project-slug>/<session_id>.jsonl`` and
are newline-delimited JSON, one object per line, written live by Claude Code as
a session progresses. Lines can be malformed or truncated (the file is being
appended to while we read it) — those are skipped silently.

This module never writes to transcripts; it only extracts a small set of facts
useful for session hand-off summaries (see ``TranscriptFacts``).
"""

from __future__ import annotations

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

CLAUDE_PROJECTS_DIR = os.path.expanduser("~/.claude/projects")

# Session ids are UUIDs in practice; this is a conservative allow-list that keeps
# a caller-supplied id from escaping its directory via "..", "/" or a leading dot.
_SAFE_SESSION_ID = re.compile(r"(?!\.)[A-Za-z0-9._-]{1,128}")

# Files larger than this are read via head+tail rather than in full.
_LARGE_FILE_THRESHOLD = 2 * 1024 * 1024  # 2 MB
_TAIL_CHUNK_SIZE = 2 * 1024 * 1024  # 2 MB
_HEAD_CHUNK_SIZE = 256 * 1024  # 256 KB

_EDIT_TOOL_NAMES = ("Edit", "Write", "NotebookEdit", "MultiEdit")
_TRUNCATE_LEN = 500


@dataclass
class TranscriptFacts:
    """Extracted facts from a single session transcript."""

    ai_title: str | None = None
    first_user_prompt: str | None = None
    last_prompt: str | None = None
    last_assistant_message: str | None = None
    files_touched: list[str] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    open_items: list[dict] = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None
    source_jsonl_path: str | None = None


def find_transcript(session_id: str, cwd: str, hint: str | None = None) -> Path | None:
    """Locate the transcript file for a session.

    If ``hint`` is given and points at an existing file inside the Claude
    projects directory, it is used as-is. Otherwise the transcript is looked up
    at ``~/.claude/projects/<slugified-cwd>/<session_id>.jsonl``, where the slug
    replaces ``/``, ``.`` and ``_`` in ``cwd`` with ``-``.

    Both paths are confined to ``~/.claude/projects`` and ``session_id`` is
    validated, so a caller passing untrusted input cannot read arbitrary files.
    """
    if hint:
        hint_path = Path(hint).expanduser()
        if _within_projects_dir(hint_path) and hint_path.is_file():
            return hint_path
        if hint_path.is_file():
            log.warning("ignoring transcript hint outside the Claude projects dir")

    if not _SAFE_SESSION_ID.fullmatch(session_id or ""):
        return None

    slug = _slugify_cwd(cwd)
    candidate = Path(CLAUDE_PROJECTS_DIR) / slug / f"{session_id}.jsonl"
    if not _within_projects_dir(candidate) or not candidate.is_file():
        return None
    # The slug is lossy (``/``, ``.`` and ``_`` all collapse to ``-``), so two
    # distinct directories can map to the same slug. Reject a transcript whose
    # own recorded cwd disagrees with the one we were asked about.
    if not _transcript_cwd_matches(candidate, cwd):
        log.warning("transcript at %s records a different cwd; ignoring", candidate)
        return None
    return candidate


def _within_projects_dir(path: Path) -> bool:
    """True if *path* resolves inside ``CLAUDE_PROJECTS_DIR``."""
    try:
        root = Path(CLAUDE_PROJECTS_DIR).resolve()
        return path.resolve().is_relative_to(root)
    except (OSError, ValueError, RuntimeError):
        return False


def _transcript_cwd_matches(path: Path, cwd: str) -> bool:
    """True unless the transcript records a cwd that differs from *cwd*.

    Transcripts that record no cwd at all are accepted — absence of evidence is
    not a mismatch.
    """
    want = os.path.normpath(os.path.expanduser(cwd or ""))
    for line in _read_head_lines(path, _HEAD_CHUNK_SIZE):
        try:
            entry = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        got = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(got, str) and got:
            return os.path.normpath(got) == want
    return True


def _slugify_cwd(cwd: str) -> str:
    slug = cwd
    for ch in ("/", ".", "_"):
        slug = slug.replace(ch, "-")
    return slug


def parse(path: str | Path) -> TranscriptFacts:
    """Parse a transcript file into ``TranscriptFacts``.

    For files over ~2 MB, only a small head chunk (for ``first_user_prompt``
    and ``started_at``) and the last ~2 MB (for everything else, since the
    "last one wins" fields and tool-use/tool-result pairing live near the
    end) are read.
    """
    path = Path(path)
    facts = TranscriptFacts()

    try:
        file_size = path.stat().st_size
    except OSError:
        return facts

    lines: list[str]
    if file_size > _LARGE_FILE_THRESHOLD:
        lines = _read_head_lines(path, _HEAD_CHUNK_SIZE) + _read_tail_lines(path, _TAIL_CHUNK_SIZE)
    else:
        try:
            with open(path, encoding="utf-8", errors="replace") as fh:
                lines = fh.readlines()
        except OSError:
            return facts

    _extract(lines, facts)
    return facts


def parse_session(session_id: str, cwd: str, hint: str | None = None) -> TranscriptFacts | None:
    """Find and parse the transcript for a session, or None if not found."""
    path = find_transcript(session_id, cwd, hint=hint)
    if path is None:
        return None
    facts = parse(path)
    facts.source_jsonl_path = str(path)
    return facts


def _read_head_lines(path: Path, chunk_size: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(chunk_size)
    except OSError:
        return []
    # Drop a possibly-partial trailing line.
    if b"\n" in chunk:
        chunk = chunk.rsplit(b"\n", 1)[0]
    text = chunk.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line]


def _read_tail_lines(path: Path, chunk_size: int) -> list[str]:
    try:
        with open(path, "rb") as fh:
            size = fh.seek(0, os.SEEK_END)
            start = max(0, size - chunk_size)
            fh.seek(start)
            chunk = fh.read()
    except OSError:
        return []
    # Drop a possibly-partial leading line (unless we read from the very start).
    if start > 0 and b"\n" in chunk:
        chunk = chunk.split(b"\n", 1)[1]
    text = chunk.decode("utf-8", errors="replace")
    return [line for line in text.splitlines() if line]


def _parse_timestamp(ts: str) -> float | None:
    try:
        if ts.endswith("Z"):
            ts = ts[:-1] + "+00:00"
        return datetime.fromisoformat(ts).astimezone(timezone.utc).timestamp()
    except (ValueError, AttributeError):
        return None


def _is_noise_user_content(content) -> bool:
    if isinstance(content, str):
        stripped = content.lstrip()
        for wrapper in ("<local-command-caveat>", "<command-name>", "<local-command-stdout>"):
            if stripped.startswith(wrapper):
                return True
    return False


def _first_text_block(content) -> str | None:
    """Extract plain text from a user/assistant message content list."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text")
    return None


def _tool_result_text(content) -> str | None:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if text:
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


def _extract(lines: list[str], facts: TranscriptFacts) -> None:
    files_seen: dict[str, None] = {}  # ordered set
    # tool_use_id -> agent dict (for AskUserQuestion/ExitPlanMode/Agent matching)
    pending_agent_calls: dict[str, dict] = {}
    pending_open_items: dict[str, dict] = {}
    first_prompt_typed: str | None = None
    first_prompt_fallback: str | None = None
    got_started_at = False

    for raw_line in lines:
        raw_line = raw_line.strip()
        if not raw_line:
            continue
        try:
            entry = json.loads(raw_line)
        except (json.JSONDecodeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue

        entry_type = entry.get("type")
        ts = entry.get("timestamp")
        if isinstance(ts, str):
            epoch = _parse_timestamp(ts)
            if epoch is not None:
                if not got_started_at:
                    facts.started_at = epoch
                    got_started_at = True
                facts.ended_at = epoch

        if entry_type == "ai-title":
            title = entry.get("aiTitle")
            if title:
                facts.ai_title = title

        elif entry_type == "last-prompt":
            if "lastPrompt" in entry:
                facts.last_prompt = entry.get("lastPrompt")

        elif entry_type == "file-history-snapshot":
            snapshot = entry.get("snapshot") or {}
            backups = snapshot.get("trackedFileBackups") or {}
            if isinstance(backups, dict):
                for file_path in backups:
                    if file_path not in files_seen:
                        files_seen[file_path] = None

        elif entry_type == "user":
            _handle_user_entry(
                entry,
                pending_agent_calls,
                pending_open_items,
            )
            if first_prompt_typed is None or first_prompt_fallback is None:
                candidate = _extract_prompt_candidate(entry)
                if candidate is not None:
                    text, is_typed_human = candidate
                    if is_typed_human and first_prompt_typed is None:
                        first_prompt_typed = text
                    elif not is_typed_human and first_prompt_fallback is None:
                        first_prompt_fallback = text

        elif entry_type == "assistant":
            _handle_assistant_entry(
                entry, facts, files_seen, pending_agent_calls, pending_open_items
            )

    facts.first_user_prompt = first_prompt_typed or first_prompt_fallback
    facts.files_touched = list(files_seen.keys())
    facts.agents = list(pending_agent_calls.values())
    facts.open_items = [
        item for item in pending_open_items.values() if not item.pop("_resolved", False)
    ]


def _extract_prompt_candidate(entry: dict) -> tuple[str, bool] | None:
    if entry.get("isMeta"):
        return None
    if entry.get("turnCompanion"):
        return None
    message = entry.get("message") or {}
    content = message.get("content")
    if _is_noise_user_content(content):
        return None
    if isinstance(content, list):
        # Content lists here are typically tool_result wrappers, not prompts.
        has_tool_result = any(
            isinstance(b, dict) and b.get("type") == "tool_result" for b in content
        )
        if has_tool_result:
            return None
    text = _first_text_block(content)
    if not text or not text.strip():
        return None
    origin = entry.get("origin") or {}
    is_typed_human = origin.get("kind") == "human" and entry.get("promptSource") == "typed"
    return text, is_typed_human


def _handle_user_entry(
    entry: dict,
    pending_agent_calls: dict[str, dict],
    pending_open_items: dict[str, dict],
) -> None:
    message = entry.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "tool_result":
            continue
        tool_use_id = block.get("tool_use_id")
        if not tool_use_id:
            continue
        result_text = _tool_result_text(block.get("content"))
        if tool_use_id in pending_agent_calls:
            pending_agent_calls[tool_use_id]["status"] = "completed"
            if result_text:
                pending_agent_calls[tool_use_id]["last_message"] = result_text[:_TRUNCATE_LEN]
        if tool_use_id in pending_open_items:
            pending_open_items[tool_use_id]["_resolved"] = True


def _handle_assistant_entry(
    entry: dict,
    facts: TranscriptFacts,
    files_seen: dict[str, None],
    pending_agent_calls: dict[str, dict],
    pending_open_items: dict[str, dict],
) -> None:
    message = entry.get("message") or {}
    content = message.get("content")
    if not isinstance(content, list):
        return

    for block in content:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")

        if block_type == "text":
            text = block.get("text")
            if text and text.strip():
                facts.last_assistant_message = text

        elif block_type == "tool_use":
            name = block.get("name")
            tool_input = block.get("input") or {}
            tool_id = block.get("id")

            if name in _EDIT_TOOL_NAMES:
                file_path = tool_input.get("file_path") or tool_input.get("notebook_path")
                if file_path and file_path not in files_seen:
                    files_seen[file_path] = None

            elif name == "Agent" and tool_id:
                pending_agent_calls[tool_id] = {
                    "type": tool_input.get("subagent_type", "general-purpose"),
                    "label": tool_input.get("description"),
                    "status": "running",
                    "last_message": None,
                }

            elif name == "AskUserQuestion" and tool_id:
                pending_open_items[tool_id] = {
                    "kind": "askuserquestion",
                    "text": _render_ask_user_question(tool_input),
                    "_resolved": False,
                }

            elif name == "ExitPlanMode" and tool_id:
                pending_open_items[tool_id] = {
                    "kind": "plan",
                    "text": "Plan awaiting approval",
                    "_resolved": False,
                }


def _render_ask_user_question(tool_input: dict) -> str:
    questions = tool_input.get("questions")
    if isinstance(questions, list) and questions:
        first = questions[0]
        if isinstance(first, dict):
            question = first.get("question")
            if question:
                return question
    return "Question awaiting answer"
