"""Session hand-off: capture, store, summarize, and render session context.

Captures a compact snapshot of a Claude Code session (git state, transcript
facts, optional LLM-generated summary) so it can be re-injected as context
into a later session working in the same directory, and/or browsed as
markdown digests.

Storage layout (under ``CONFIG_DIR``, see ``claude_monitor.settings``)::

    handoff/sessions/<project-slug>/<session_id>.json
    handoff/handoff.md                  # global digest
    handoff/projects/<project-slug>.md  # per-project digest

``claude_monitor.transcript`` and ``claude_monitor.llm`` are built by other
tasks in this feature and may not exist yet in a given worktree. They are
imported lazily (inside the functions that use them) so this module can be
imported, and its tests can run, before those modules land. Tests patch
``sys.modules["claude_monitor.transcript"]`` / ``sys.modules["claude_monitor.llm"]``
to stub them out.
"""

import json
import logging
import os
import re
import subprocess
import tempfile
import time
from dataclasses import asdict, dataclass, field

from claude_monitor.settings import CONFIG_DIR

log = logging.getLogger(__name__)

HANDOFF_DIR = os.path.join(CONFIG_DIR, "handoff")
SESSIONS_DIR = os.path.join(HANDOFF_DIR, "sessions")
PROJECTS_DIR = os.path.join(HANDOFF_DIR, "projects")
DIGEST_FILE = os.path.join(HANDOFF_DIR, "handoff.md")

_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_DEFAULT_EVENT_STATS = {"approved": 0, "deferred": 0, "agents_spawned": 0}


@dataclass
class HandoffEntry:
    session_id: str
    project_path: str
    project_slug: str
    title: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    capture_reason: str = "manual"  # session_end | idle | manual
    last_prompt: str | None = None
    last_assistant_message: str | None = None
    git_branch: str | None = None
    git_dirty: bool = False
    files_touched: list[str] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    open_items: list[dict] = field(default_factory=list)
    event_stats: dict = field(default_factory=dict)
    summary: dict | None = None  # {"goal", "stopping_point", "next_steps"}
    summary_model: str | None = None
    summary_generated_at: float | None = None


# --- filesystem helpers -----------------------------------------------------


def _atomic_write(path: str, content: str) -> bool:
    """Write *content* to *path* atomically. Returns True on success."""
    directory = os.path.dirname(path)
    try:
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=directory, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(content)
            os.replace(tmp_path, path)
        except OSError:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return True
    except OSError as e:
        log.warning(f"Failed to write {path}: {e}")
        return False


def project_slug(cwd: str) -> str:
    """Filesystem-safe, stable slug for a directory path.

    Mirrors Claude Code's own project-directory naming: an absolute path
    with separators replaced by ``-`` and a leading ``-``, then strips
    anything outside ``[A-Za-z0-9._-]`` for safety.
    """
    abs_path = os.path.abspath(cwd)
    slug = abs_path.replace("/", "-").replace("\\", "-")
    if not slug.startswith("-"):
        slug = "-" + slug
    slug = _SLUG_SAFE_RE.sub("", slug)
    return slug or "-root"


def _session_path(slug: str, session_id: str) -> str:
    return os.path.join(SESSIONS_DIR, slug, f"{session_id}.json")


def _entry_to_json(entry: HandoffEntry) -> str:
    return json.dumps(asdict(entry), indent=2, sort_keys=True)


def _entry_from_dict(data: dict) -> HandoffEntry | None:
    try:
        known = {k: v for k, v in data.items() if k in HandoffEntry.__dataclass_fields__}
        return HandoffEntry(**known)
    except TypeError as e:
        log.warning(f"Corrupt handoff entry data: {e}")
        return None


def _save_entry(entry: HandoffEntry) -> None:
    path = _session_path(entry.project_slug, entry.session_id)
    _atomic_write(path, _entry_to_json(entry))


def load_entry(session_id: str, slug: str | None = None) -> HandoffEntry | None:
    """Load a single entry by session id. Searches all projects if slug is None."""
    candidates: list[str] = []
    if slug is not None:
        candidates.append(_session_path(slug, session_id))
    else:
        try:
            for project_dir in os.listdir(SESSIONS_DIR):
                candidates.append(os.path.join(SESSIONS_DIR, project_dir, f"{session_id}.json"))
        except OSError:
            return None

    for path in candidates:
        if not os.path.isfile(path):
            continue
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Failed to read handoff entry {path}: {e}")
            continue
        entry = _entry_from_dict(data)
        if entry is not None:
            return entry
    return None


def _entry_sort_key(entry: HandoffEntry, mtime: float) -> float:
    if entry.ended_at is not None:
        return entry.ended_at
    if entry.started_at is not None:
        return entry.started_at
    return mtime


def _iter_entry_files(project: str | None = None):
    if project is not None:
        project_dirs = [project]
        base = SESSIONS_DIR
    else:
        try:
            project_dirs = os.listdir(SESSIONS_DIR)
        except OSError:
            project_dirs = []
        base = SESSIONS_DIR

    for pdir in project_dirs:
        full_dir = os.path.join(base, pdir)
        try:
            names = os.listdir(full_dir)
        except OSError:
            continue
        for name in names:
            if name.endswith(".json"):
                yield os.path.join(full_dir, name)


def list_entries(
    *, project: str | None = None, limit: int | None = None, since: float | None = None
) -> list[HandoffEntry]:
    """List handoff entries, newest first, optionally filtered by project/since."""
    results: list[tuple[float, HandoffEntry]] = []
    for path in _iter_entry_files(project):
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            log.warning(f"Skipping corrupt handoff entry {path}: {e}")
            continue
        entry = _entry_from_dict(data)
        if entry is None:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        sort_key = _entry_sort_key(entry, mtime)
        if since is not None and sort_key < since:
            continue
        results.append((sort_key, entry))

    results.sort(key=lambda pair: pair[0], reverse=True)
    entries = [e for _, e in results]
    if limit is not None:
        entries = entries[:limit]
    return entries


def latest_for_cwd(cwd: str, max_age_hours: float | None = None) -> HandoffEntry | None:
    """Most recent entry for the project at *cwd*, optionally age-capped."""
    slug = project_slug(cwd)
    entries = list_entries(project=slug, limit=1)
    if not entries:
        return None
    entry = entries[0]
    if max_age_hours is not None:
        ts = entry.ended_at or entry.started_at
        if ts is None or (time.time() - ts) > max_age_hours * 3600:
            return None
    return entry


def prune(slug: str, keep: int) -> None:
    """Delete all but the *keep* newest entries for project *slug*."""
    entries_with_paths: list[tuple[float, str]] = []
    project_dir = os.path.join(SESSIONS_DIR, slug)
    try:
        names = os.listdir(project_dir)
    except OSError:
        return

    for name in names:
        if not name.endswith(".json"):
            continue
        path = os.path.join(project_dir, name)
        try:
            with open(path) as f:
                data = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        entry = _entry_from_dict(data)
        if entry is None:
            continue
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        entries_with_paths.append((_entry_sort_key(entry, mtime), path))

    entries_with_paths.sort(key=lambda pair: pair[0], reverse=True)
    for _, path in entries_with_paths[keep:]:
        try:
            os.remove(path)
        except OSError as e:
            log.warning(f"Failed to prune {path}: {e}")


# --- git helpers -------------------------------------------------------------


def _git_info(cwd: str) -> tuple[str | None, bool]:
    """Return (git_branch, git_dirty) for *cwd*. Never raises."""
    branch: str | None = None
    dirty = False
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd,
            timeout=2,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            branch = result.stdout.strip() or None
    except (OSError, subprocess.SubprocessError):
        branch = None

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=cwd,
            timeout=2,
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            dirty = bool(result.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        dirty = False

    return branch, dirty


# --- capture -----------------------------------------------------------------


def capture(
    session_id: str,
    cwd: str,
    *,
    transcript_path: str | None = None,
    reason: str = "manual",
    with_llm: bool = False,
    settings=None,
    event_stats: dict | None = None,
) -> HandoffEntry | None:
    """Capture a hand-off entry for *session_id* running in *cwd*.

    Resolves transcript facts (if the transcript module/session is
    available), merges in git state and caller-supplied event stats,
    optionally asks the LLM for a summary, then persists the entry, prunes
    old entries for the project, and regenerates markdown digests.
    Returns None only if there is genuinely nothing to record.
    """
    facts = None
    try:
        from claude_monitor import transcript

        facts = transcript.parse_session(session_id, cwd, hint=transcript_path)
    except Exception as e:  # noqa: BLE001 - transcript module may not exist yet / may fail
        log.warning(f"Failed to parse transcript for {session_id}: {e}")
        facts = None

    stats = dict(_DEFAULT_EVENT_STATS)
    if event_stats:
        stats.update(event_stats)

    if facts is None and not event_stats:
        return None

    slug = project_slug(cwd)

    title = None
    first_user_prompt = None
    last_prompt = None
    last_assistant_message = None
    files_touched: list[str] = []
    agents: list[dict] = []
    open_items: list[dict] = []
    started_at = None
    ended_at = None

    if facts is not None:
        title = getattr(facts, "ai_title", None)
        first_user_prompt = getattr(facts, "first_user_prompt", None)
        last_prompt = getattr(facts, "last_prompt", None)
        last_assistant_message = getattr(facts, "last_assistant_message", None)
        files_touched = list(getattr(facts, "files_touched", None) or [])
        agents = list(getattr(facts, "agents", None) or [])
        open_items = list(getattr(facts, "open_items", None) or [])
        started_at = getattr(facts, "started_at", None)
        ended_at = getattr(facts, "ended_at", None)

    if not title:
        if first_user_prompt:
            title = first_user_prompt.strip().splitlines()[0][:80]
        else:
            title = os.path.basename(os.path.abspath(cwd)) or cwd

    git_branch, git_dirty = _git_info(cwd)

    entry = HandoffEntry(
        session_id=session_id,
        project_path=os.path.abspath(cwd),
        project_slug=slug,
        title=title,
        started_at=started_at,
        ended_at=ended_at if ended_at is not None else time.time(),
        capture_reason=reason,
        last_prompt=last_prompt,
        last_assistant_message=last_assistant_message,
        git_branch=git_branch,
        git_dirty=git_dirty,
        files_touched=files_touched,
        agents=agents,
        open_items=open_items,
        event_stats=stats,
    )

    llm_enabled = bool(getattr(settings, "handoff_llm_enabled", False)) if settings else False
    if with_llm and llm_enabled:
        try:
            summary = summarize(entry, settings=settings)
        except Exception as e:  # noqa: BLE001 - summarize should not be fatal to capture
            log.warning(f"Summarize failed for {session_id}: {e}")
            summary = None
        if summary is not None:
            entry.summary = summary
            entry.summary_model = getattr(settings, "handoff_model", "") or None
            entry.summary_generated_at = time.time()

    _save_entry(entry)

    keep = getattr(settings, "handoff_retain_per_project", 5) if settings else 5
    try:
        prune(slug, int(keep))
    except Exception as e:  # noqa: BLE001
        log.warning(f"Prune failed for {slug}: {e}")

    markdown_enabled = getattr(settings, "handoff_markdown_enabled", True) if settings else True
    if markdown_enabled:
        try:
            write_markdown(settings)
        except Exception as e:  # noqa: BLE001 - markdown regen must not break capture
            log.warning(f"write_markdown failed: {e}")

    return entry


# --- summarize -----------------------------------------------------------------

_MAX_PROMPT_CHARS = 6000
_EXCERPT_CHARS = 800

_SUMMARY_SYSTEM_PROMPT = (
    "You summarize a coding session so a future session can resume it. "
    "Reply with STRICT JSON only, no prose, no markdown fences. "
    'Keys: "goal" (string), "stopping_point" (string), '
    '"next_steps" (list of short strings).'
)


def _truncate(text: str | None, limit: int) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "..."


def _build_summary_prompt(entry: HandoffEntry) -> str:
    parts = [f"Title: {entry.title or '(untitled)'}"]
    if entry.last_prompt:
        parts.append(f"Last user prompt: {_truncate(entry.last_prompt, _EXCERPT_CHARS)}")
    if entry.last_assistant_message:
        parts.append(
            f"Last assistant message: {_truncate(entry.last_assistant_message, _EXCERPT_CHARS)}"
        )
    if entry.files_touched:
        parts.append("Files touched: " + ", ".join(entry.files_touched[:20]))
    if entry.agents:
        agent_lines = [
            f"- {a.get('type', '?')} ({a.get('status', '?')}): "
            f"{_truncate(a.get('last_message'), 200)}"
            for a in entry.agents[:10]
        ]
        parts.append("Agents:\n" + "\n".join(agent_lines))
    if entry.open_items:
        item_lines = [
            f"- [{i.get('kind', '?')}] {_truncate(i.get('text'), 200)}"
            for i in entry.open_items[:10]
        ]
        parts.append("Open items:\n" + "\n".join(item_lines))

    prompt = "\n\n".join(parts)
    if len(prompt) > _MAX_PROMPT_CHARS:
        prompt = prompt[:_MAX_PROMPT_CHARS].rstrip() + "..."
    return prompt


def _extract_json_object(text: str) -> dict | None:
    """Defensively pull a JSON object out of a possibly prose/fenced reply."""
    text = text.strip()
    # Strip common code fences.
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence_match:
        text = fence_match.group(1).strip()

    # Try direct parse first.
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Find outermost {...} span.
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    candidate = text[start : end + 1]
    try:
        obj = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def summarize(entry: HandoffEntry, *, settings=None) -> dict | None:
    """Ask the configured LLM to summarize *entry*. Returns dict or None on any failure."""
    try:
        from claude_monitor import llm
    except Exception as e:  # noqa: BLE001 - llm module may not exist yet
        log.warning(f"llm module unavailable: {e}")
        return None

    transport = getattr(settings, "handoff_llm_transport", "minimax") if settings else "minimax"
    model = (getattr(settings, "handoff_model", "") if settings else "") or None
    timeout = getattr(settings, "handoff_llm_timeout_secs", 30) if settings else 30

    prompt = _build_summary_prompt(entry)

    try:
        reply = llm.complete(
            prompt,
            system=_SUMMARY_SYSTEM_PROMPT,
            transport=transport,
            model=model,
            timeout=timeout,
        )
    except Exception as e:  # noqa: BLE001 - covers llm.LLMError and anything else
        log.warning(f"LLM summarize call failed: {e}")
        return None

    obj = _extract_json_object(reply)
    if obj is None:
        log.warning("LLM summarize reply was not parseable JSON")
        return None

    if "goal" not in obj or "stopping_point" not in obj or "next_steps" not in obj:
        log.warning(f"LLM summarize reply missing required keys: {list(obj.keys())}")
        return None

    next_steps = obj.get("next_steps")
    if not isinstance(next_steps, list):
        next_steps = [str(next_steps)] if next_steps else []

    return {
        "goal": str(obj.get("goal") or ""),
        "stopping_point": str(obj.get("stopping_point") or ""),
        "next_steps": [str(s) for s in next_steps],
    }


# --- injection context ---------------------------------------------------------


def injection_context(
    cwd: str, *, exclude_session_id: str | None = None, max_age_hours: float = 72
) -> str | None:
    """Short markdown block summarizing the most recent prior session in *cwd*."""
    slug = project_slug(cwd)
    entries = list_entries(project=slug, limit=10)
    now = time.time()

    for entry in entries:
        if exclude_session_id is not None and entry.session_id == exclude_session_id:
            continue
        ts = entry.ended_at or entry.started_at
        if ts is None or (now - ts) > max_age_hours * 3600:
            continue

        return _render_injection(entry, now)

    return None


def _render_injection(entry: HandoffEntry, now: float) -> str:
    from claude_monitor import fmt_duration

    lines = ["> Hand-off summary from a previous session in this directory."]
    ts = entry.ended_at or entry.started_at
    when = f"{fmt_duration(max(now - ts, 0), compact=True)} ago" if ts else "recently"
    lines.append(f"**{entry.title or 'Untitled session'}** ({when})")

    if entry.summary:
        goal = entry.summary.get("goal")
        stopping = entry.summary.get("stopping_point")
        next_steps = entry.summary.get("next_steps") or []
        if goal:
            lines.append(f"- Goal: {_truncate(goal, 300)}")
        if stopping:
            lines.append(f"- Stopped at: {_truncate(stopping, 300)}")
        if next_steps:
            lines.append("- Next steps:")
            for step in next_steps[:5]:
                lines.append(f"  - {_truncate(step, 150)}")
    else:
        if entry.last_prompt:
            lines.append(f"- Last prompt: {_truncate(entry.last_prompt, 250)}")
        if entry.last_assistant_message:
            lines.append(f"- Last response: {_truncate(entry.last_assistant_message, 250)}")

    if entry.files_touched:
        lines.append("- Files touched: " + ", ".join(entry.files_touched[:10]))

    running_agents = [a for a in entry.agents if a.get("status") == "running"]
    if running_agents:
        lines.append(
            "- Still running: "
            + ", ".join(a.get("label") or a.get("type", "agent") for a in running_agents[:5])
        )

    if entry.open_items:
        lines.append("- Open items:")
        for item in entry.open_items[:5]:
            lines.append(f"  - [{item.get('kind', '?')}] {_truncate(item.get('text'), 150)}")

    if entry.git_branch:
        dirty = " (dirty)" if entry.git_dirty else ""
        lines.append(f"- Git branch: {entry.git_branch}{dirty}")

    text = "\n".join(lines)
    if len(text) > 1500:
        text = text[:1500].rstrip() + "..."
    return text


# --- markdown rendering ---------------------------------------------------------


def render_entry(entry: HandoffEntry) -> str:
    """Render a single entry as a markdown section."""
    lines = [f"## {entry.title or 'Untitled session'}"]
    lines.append("")
    meta_bits = [f"session `{entry.session_id}`"]
    if entry.started_at:
        started = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.started_at))
        meta_bits.append(f"started {started}")
    if entry.ended_at:
        ended = time.strftime("%Y-%m-%d %H:%M", time.localtime(entry.ended_at))
        meta_bits.append(f"ended {ended}")
    meta_bits.append(f"reason: {entry.capture_reason}")
    lines.append("_" + " • ".join(meta_bits) + "_")
    lines.append("")

    lines.append(f"- Project: `{entry.project_path}`")
    if entry.git_branch:
        dirty = " (dirty)" if entry.git_dirty else ""
        lines.append(f"- Git: `{entry.git_branch}`{dirty}")

    if entry.summary:
        goal = entry.summary.get("goal")
        stopping = entry.summary.get("stopping_point")
        next_steps = entry.summary.get("next_steps") or []
        if goal:
            lines.append(f"- Goal: {goal}")
        if stopping:
            lines.append(f"- Stopping point: {stopping}")
        if next_steps:
            lines.append("- Next steps:")
            for step in next_steps:
                lines.append(f"  - {step}")
    else:
        if entry.last_prompt:
            lines.append(f"- Last prompt: {_truncate(entry.last_prompt, 300)}")
        if entry.last_assistant_message:
            lines.append(f"- Last response: {_truncate(entry.last_assistant_message, 300)}")

    if entry.files_touched:
        touched = ", ".join(entry.files_touched[:20])
        lines.append(f"- Files touched ({len(entry.files_touched)}): {touched}")

    if entry.agents:
        lines.append("- Agents:")
        for a in entry.agents:
            label = a.get("label") or a.get("type", "agent")
            lines.append(f"  - {label} ({a.get('status', '?')})")

    if entry.open_items:
        lines.append("- Open items:")
        for item in entry.open_items:
            lines.append(f"  - [{item.get('kind', '?')}] {item.get('text', '')}")

    stats = entry.event_stats or {}
    if stats:
        stat_bits = ", ".join(f"{k}: {v}" for k, v in stats.items())
        lines.append(f"- Event stats: {stat_bits}")

    return "\n".join(lines) + "\n"


def render_digest(entries: list[HandoffEntry]) -> str:
    """Render a global digest of *entries*, grouped by project, newest first."""
    if not entries:
        return "# Session Hand-offs\n\nNo sessions recorded yet.\n"

    by_project: dict[str, list[HandoffEntry]] = {}
    order: list[str] = []
    for entry in entries:
        key = entry.project_path
        if key not in by_project:
            by_project[key] = []
            order.append(key)
        by_project[key].append(entry)

    lines = ["# Session Hand-offs", ""]
    for project_path in order:
        lines.append(f"# {project_path}")
        lines.append("")
        for entry in by_project[project_path]:
            lines.append(render_entry(entry))
    return "\n".join(lines)


def write_markdown(settings=None) -> None:
    """Regenerate the global digest and per-project markdown files."""
    entries = list_entries()

    digest = render_digest(entries)
    _atomic_write(DIGEST_FILE, digest)

    by_slug: dict[str, list[HandoffEntry]] = {}
    for entry in entries:
        by_slug.setdefault(entry.project_slug, []).append(entry)

    for slug, project_entries in by_slug.items():
        content = render_digest(project_entries)
        path = os.path.join(PROJECTS_DIR, f"{slug}.md")
        _atomic_write(path, content)
