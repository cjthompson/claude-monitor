"""Session hand-off: capture, store, summarize, and render session context.

Captures a compact snapshot of a Claude Code session (git state, transcript
facts, optional LLM-generated summary) for targeted delivery to a later
session, and/or browsing as markdown digests.

Storage layout (under ``CONFIG_DIR``, see ``claude_monitor.settings``)::

    handoff/sessions/<project-slug>/<session_id>.json
    handoff/pending/<pane-hash>.json   # one-shot targeted delivery
    handoff/handoff.md                  # global digest
    handoff/projects/<project-slug>.md  # per-project digest

``claude_monitor.transcript`` and ``claude_monitor.llm`` are built by other
tasks in this feature and may not exist yet in a given worktree. They are
imported lazily (inside the functions that use them) so this module can be
imported, and its tests can run, before those modules land. Tests patch
``sys.modules["claude_monitor.transcript"]`` / ``sys.modules["claude_monitor.llm"]``
to stub them out.
"""

import fcntl
import hashlib
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field

from claude_monitor import EVENTS_FILE
from claude_monitor.settings import CONFIG_DIR

log = logging.getLogger(__name__)

HANDOFF_DIR = os.path.join(CONFIG_DIR, "handoff")
SESSIONS_DIR = os.path.join(HANDOFF_DIR, "sessions")
PROJECTS_DIR = os.path.join(HANDOFF_DIR, "projects")
DIGEST_FILE = os.path.join(HANDOFF_DIR, "handoff.md")
PENDING_DIR = os.path.join(HANDOFF_DIR, "pending")
HANDOFF_ACTION_LABEL = "claude-monitor action: session hand-off"
RULE_INDEX_RELATIVE_PATH = ".claude/rules/claude-monitor-handoffs.md"
RULE_INDEX_IGNORE_PATTERN = "/.claude/rules/claude-monitor-handoffs.md"

_SLUG_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")
_DEFAULT_EVENT_STATS = {"approved": 0, "deferred": 0, "agents_spawned": 0}


@dataclass
class HandoffEntry:
    session_id: str
    project_path: str
    project_slug: str
    title: str | None = None
    first_user_prompt: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    source_jsonl_path: str | None = None
    capture_reason: str = "manual"  # session_end | idle | manual
    last_prompt: str | None = None
    last_assistant_message: str | None = None
    git_branch: str | None = None
    git_dirty: bool = False
    git_ahead: int = 0
    files_touched: list[str] = field(default_factory=list)
    agents: list[dict] = field(default_factory=list)
    open_items: list[dict] = field(default_factory=list)
    event_stats: dict = field(default_factory=dict)
    summary: dict | None = None  # {"goal", "stopping_point", "next_steps"}
    summary_model: str | None = None
    summary_generated_at: float | None = None


@dataclass(frozen=True)
class PendingHandoff:
    entry_session_id: str
    originating_session_id: str
    target_iterm_session_id: str
    project_path: str
    project_slug: str
    rotation_mode: str
    created_at: float
    generation_token: str
    delivery_context: str
    trigger: str = "manual"  # manual | idle


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


def _pending_path(iterm_session_id: str) -> str:
    """Return a safe deterministic path for a pane's pending hand-off."""
    digest = hashlib.sha256(iterm_session_id.encode("utf-8")).hexdigest()
    return os.path.join(PENDING_DIR, f"{digest}.json")


@contextmanager
def _pending_lock():
    """Serialize staging, consumption, and conditional pending cleanup."""
    os.makedirs(PENDING_DIR, exist_ok=True)
    lock_path = os.path.join(PENDING_DIR, ".lock")
    with open(lock_path, "a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _pending_from_dict(data: dict) -> PendingHandoff | None:
    if not isinstance(data, dict):
        return None
    try:
        known = {k: v for k, v in data.items() if k in PendingHandoff.__dataclass_fields__}
        pending = PendingHandoff(**known)
    except (TypeError, ValueError):
        return None
    string_fields = (
        pending.entry_session_id,
        pending.originating_session_id,
        pending.target_iterm_session_id,
        pending.project_path,
        pending.project_slug,
        pending.generation_token,
        pending.delivery_context,
    )
    if any(not isinstance(value, str) or not value for value in string_fields):
        return None
    if pending.rotation_mode not in ("clear", "compact"):
        return None
    if pending.trigger not in ("manual", "idle"):
        return None
    if isinstance(pending.created_at, bool) or not isinstance(pending.created_at, (int, float)):
        return None
    if not math.isfinite(float(pending.created_at)):
        return None
    if len(pending.delivery_context) > 1500:
        return None
    if pending.delivery_context != HANDOFF_ACTION_LABEL and not pending.delivery_context.startswith(
        HANDOFF_ACTION_LABEL + "\n"
    ):
        return None
    return pending


def _pending_to_json(pending: PendingHandoff) -> str:
    return json.dumps(asdict(pending), indent=2, sort_keys=True)


def _normalized_project_path(path: str) -> str:
    return os.path.normcase(os.path.realpath(os.path.abspath(os.path.expanduser(path))))


def _event_matches_pending_target(event: dict, pending: PendingHandoff) -> bool:
    cwd = event.get("cwd")
    return (
        isinstance(cwd, str)
        and _normalized_project_path(cwd) == pending.project_path
        and event.get("_iterm_session_id") == pending.target_iterm_session_id
    )


def _is_current_session_start(event: dict, data: dict) -> bool:
    return (
        event.get("hook_event_name") == "SessionStart"
        and event.get("source") == data.get("source")
        and event.get("session_id") == data.get("session_id")
        and event.get("_iterm_session_id") == data.get("_iterm_session_id")
        and event.get("_timestamp") == data.get("_timestamp")
    )


def _clear_has_no_intervening_session(pending: PendingHandoff, data: dict) -> bool:
    """Reject a stale clear after another session has occupied the target."""
    try:
        with open(EVENTS_FILE, encoding="utf-8") as events_file:
            for raw_line in events_file:
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict) or not _event_matches_pending_target(event, pending):
                    continue
                timestamp = event.get("_timestamp")
                if not isinstance(timestamp, (int, float)) or timestamp < pending.created_at:
                    continue
                if _is_current_session_start(event, data):
                    continue
                session_id = event.get("session_id")
                if (
                    isinstance(session_id, str)
                    and session_id
                    and session_id != pending.originating_session_id
                ):
                    return False
    except OSError:
        # The event log lives in /tmp and may disappear across a restart. The
        # durable pane/project/source record remains the authority in that case.
        return True
    return True


def stage_pending(
    entry: HandoffEntry,
    *,
    iterm_session_id: str,
    originating_session_id: str,
    rotation_mode: str,
    now: float | None = None,
    trigger: str = "manual",
) -> PendingHandoff | None:
    """Persist a one-shot hand-off delivery targeted at one exact pane."""
    if (
        not isinstance(iterm_session_id, str)
        or not isinstance(originating_session_id, str)
        or rotation_mode not in ("clear", "compact")
        or trigger not in ("manual", "idle")
    ):
        return None
    if not iterm_session_id or not originating_session_id:
        return None
    created_at = time.time() if now is None else float(now)
    project_path = _normalized_project_path(entry.project_path)
    pending = PendingHandoff(
        entry_session_id=entry.session_id,
        originating_session_id=originating_session_id,
        target_iterm_session_id=iterm_session_id,
        project_path=project_path,
        project_slug=project_slug(project_path),
        rotation_mode=rotation_mode,
        created_at=created_at,
        generation_token=uuid.uuid4().hex,
        delivery_context=render_delivery_context(entry),
        trigger=trigger,
    )
    try:
        with _pending_lock():
            if not _atomic_write(_pending_path(iterm_session_id), _pending_to_json(pending)):
                return None
    except OSError as exc:
        log.warning("Failed to lock pending hand-off directory: %s", exc)
        return None
    return pending


def find_pending_idle(
    *, iterm_session_id: str, originating_session_id: str
) -> PendingHandoff | None:
    """Return an automatic pending record for one exact pane/session pair."""
    path = _pending_path(iterm_session_id)
    try:
        with _pending_lock():
            with open(path, encoding="utf-8") as pending_file:
                pending = _pending_from_dict(json.load(pending_file))
    except (OSError, json.JSONDecodeError, TypeError):
        return None
    if (
        pending is None
        or pending.trigger != "idle"
        or pending.target_iterm_session_id != iterm_session_id
        or pending.originating_session_id != originating_session_id
    ):
        return None
    return pending


def _pending_matches(pending: PendingHandoff, data: dict) -> bool:
    if data.get("hook_event_name") != "SessionStart":
        return False
    source = data.get("source")
    if source not in ("clear", "compact") or source != pending.rotation_mode:
        return False
    if data.get("_iterm_session_id") != pending.target_iterm_session_id:
        return False
    cwd = data.get("cwd")
    if not isinstance(cwd, str) or _normalized_project_path(cwd) != pending.project_path:
        return False
    if source == "compact":
        return data.get("session_id") == pending.originating_session_id
    # Claude Code does not include the originating session id in a clear
    # SessionStart payload. Exact pane, project, and source are the strongest
    # available safety checks; unlike compact, clear must work without a
    # preceding SessionEnd event (sleep/shutdown can omit it).
    payload_origin = data.get("originating_session_id")
    if payload_origin is not None and payload_origin != pending.originating_session_id:
        return False
    return (
        isinstance(data.get("session_id"), str)
        and bool(data["session_id"])
        and _clear_has_no_intervening_session(pending, data)
    )


def deliver_pending_session_start(
    data: dict,
    deliver: Callable[[PendingHandoff], None],
    *,
    now: float | None = None,
) -> bool:
    """Deliver and remove one matching record, retaining it if delivery fails."""
    if not isinstance(data, dict):
        return False
    del now  # retained as a source-compatible test seam; pending records do not expire
    delivery_error: BaseException | None = None
    try:
        with _pending_lock():
            names = os.listdir(PENDING_DIR)
            for name in names:
                if not name.endswith(".json"):
                    continue
                path = os.path.join(PENDING_DIR, name)
                try:
                    with open(path, encoding="utf-8") as pending_file:
                        pending = _pending_from_dict(json.load(pending_file))
                except (OSError, json.JSONDecodeError, TypeError):
                    pending = None
                if pending is None:
                    try:
                        os.remove(path)
                    except OSError:
                        pass
                    continue
                if not _pending_matches(pending, data):
                    continue

                try:
                    deliver(pending)
                except BaseException as exc:  # preserve durable state on any failed delivery
                    delivery_error = exc
                    break
                try:
                    os.remove(path)
                except OSError:
                    return False
                return True
    except OSError:
        return False
    if delivery_error is not None:
        raise delivery_error
    return False


def consume_pending_session_start(data: dict, *, now: float | None = None) -> PendingHandoff | None:
    """Consume and return one matching record without an external delivery step."""
    consumed: PendingHandoff | None = None

    def collect(pending: PendingHandoff) -> None:
        nonlocal consumed
        consumed = pending

    delivered = deliver_pending_session_start(data, collect, now=now)
    return consumed if delivered else None


def discard_pending(
    *, iterm_session_id: str, originating_session_id: str, generation_token: str
) -> None:
    """Remove one exact staged record after a caller explicitly abandons it."""
    path = _pending_path(iterm_session_id)
    try:
        with _pending_lock():
            with open(path, encoding="utf-8") as pending_file:
                pending = _pending_from_dict(json.load(pending_file))
            if (
                pending is None
                or pending.target_iterm_session_id != iterm_session_id
                or pending.originating_session_id != originating_session_id
                or pending.generation_token != generation_token
            ):
                return
            os.remove(path)
    except OSError:
        pass


def _event_stats_for_session(session_id: str) -> dict | None:
    """Return monitor event counts for *session_id*, or None if unobserved."""
    stats = dict(_DEFAULT_EVENT_STATS)
    found = False
    try:
        with open(EVENTS_FILE, encoding="utf-8") as events_file:
            for raw_line in events_file:
                try:
                    event = json.loads(raw_line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(event, dict) or event.get("session_id") != session_id:
                    continue
                found = True
                if event.get("hook_event_name") == "PermissionRequest":
                    decision = event.get("_decision")
                    if decision in ("allowed", "timeout"):
                        stats["approved"] += 1
                    elif decision == "deferred":
                        stats["deferred"] += 1
                elif event.get("hook_event_name") == "SubagentStart":
                    stats["agents_spawned"] += 1
    except OSError:
        return None
    return stats if found else None


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

    newest_entry: HandoffEntry | None = None
    newest_key: float | None = None
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
        if entry is None:
            continue
        if slug is not None:
            return entry
        try:
            mtime = os.path.getmtime(path)
        except OSError:
            mtime = 0.0
        sort_key = _entry_sort_key(entry, mtime)
        if newest_key is None or sort_key > newest_key:
            newest_entry = entry
            newest_key = sort_key
    return newest_entry


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


def _git_info(cwd: str, *, timeout: float = 2.0) -> tuple[str | None, bool, int]:
    """Return (git_branch, git_dirty, git_ahead) for *cwd*. Never raises."""
    if timeout <= 0:
        return None, False, 0

    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--branch"],
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            return None, False, 0
    except (OSError, subprocess.SubprocessError):
        return None, False, 0

    lines = result.stdout.splitlines()
    if not lines:
        return None, False, 0

    status_line = lines[0]
    branch: str | None = None
    if status_line.startswith("## "):
        branch_text = status_line[3:]
        if branch_text.startswith("No commits yet on "):
            branch = branch_text.removeprefix("No commits yet on ").split(" ", 1)[0] or None
        elif branch_text != "HEAD (no branch)":
            branch = branch_text.split("...", 1)[0].split(" ", 1)[0] or None

    ahead_match = re.search(r"\bahead (\d+)\b", status_line)
    ahead = int(ahead_match.group(1)) if ahead_match else 0
    dirty = any(line.strip() for line in lines[1:])
    return branch, dirty, ahead


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
    max_seconds: float | None = None,
) -> HandoffEntry | None:
    """Capture a hand-off entry for *session_id* running in *cwd*.

    Resolves transcript facts (if the transcript module/session is
    available), derives event stats when the caller does not provide them,
    merges in git state, optionally asks the LLM for a summary, then persists
    the entry, prunes old entries for the project, and regenerates markdown
    digests. When *max_seconds* is provided, the remaining budget limits
    optional Git, pruning, and Markdown work; persistence remains best-effort
    so a timed-out capture can still save its heuristic entry.
    Returns None only if there is genuinely nothing to record.
    """
    deadline = time.monotonic() + max(0.0, max_seconds) if max_seconds is not None else None

    facts = None
    try:
        from claude_monitor import transcript

        facts = transcript.parse_session(session_id, cwd, hint=transcript_path)
    except Exception as e:  # noqa: BLE001 - transcript module may not exist yet / may fail
        log.warning(f"Failed to parse transcript for {session_id}: {e}")
        facts = None

    if event_stats is None:
        event_stats = _event_stats_for_session(session_id)

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
        source_jsonl_path = getattr(facts, "source_jsonl_path", None)
    else:
        source_jsonl_path = None

    if not title:
        if first_user_prompt:
            title = first_user_prompt.strip().splitlines()[0][:80]
        else:
            title = os.path.basename(os.path.abspath(cwd)) or cwd

    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if remaining is None or remaining > 0:
        git_branch, git_dirty, git_ahead = _git_info(
            cwd, timeout=2.0 if remaining is None else min(2.0, remaining)
        )
    else:
        git_branch, git_dirty, git_ahead = None, False, 0

    entry = HandoffEntry(
        session_id=session_id,
        project_path=os.path.abspath(cwd),
        project_slug=slug,
        title=title,
        first_user_prompt=first_user_prompt,
        source_jsonl_path=source_jsonl_path,
        started_at=started_at,
        ended_at=ended_at if ended_at is not None else time.time(),
        capture_reason=reason,
        last_prompt=last_prompt,
        last_assistant_message=last_assistant_message,
        git_branch=git_branch,
        git_dirty=git_dirty,
        git_ahead=git_ahead,
        files_touched=files_touched,
        agents=agents,
        open_items=open_items,
        event_stats=stats,
    )

    if with_llm:
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
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if remaining is None or remaining > 0:
        try:
            prune(slug, int(keep))
        except Exception as e:  # noqa: BLE001
            log.warning(f"Prune failed for {slug}: {e}")

    markdown_enabled = getattr(settings, "handoff_markdown_enabled", True) if settings else True
    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if markdown_enabled and (remaining is None or remaining > 0):
        try:
            write_markdown(settings)
        except Exception as e:  # noqa: BLE001 - markdown regen must not break capture
            log.warning(f"write_markdown failed: {e}")

    remaining = None if deadline is None else max(0.0, deadline - time.monotonic())
    if remaining is None or remaining > 0:
        try:
            index_result = write_project_rule_index(cwd, max_seconds=remaining)
        except Exception as e:  # noqa: BLE001 - generated output must not break capture
            log.warning(f"write_project_rule_index failed: {e}")
            index_result = False
        if index_result is False:
            log.warning(
                "Captured hand-off for %s, but failed to update the local project rule index",
                session_id,
            )
    else:
        log.warning(
            "Captured hand-off for %s, but failed to update the local project rule index",
            session_id,
        )

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
    if entry.first_user_prompt:
        parts.append(f"First user prompt: {_truncate(entry.first_user_prompt, _EXCERPT_CHARS)}")
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

    if entry.first_user_prompt:
        lines.append(f"- First prompt: {_truncate(entry.first_user_prompt, 250)}")

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
        git_state = []
        if entry.git_dirty:
            git_state.append("dirty")
        if entry.git_ahead:
            git_state.append(f"ahead {entry.git_ahead}")
        suffix = f" ({', '.join(git_state)})" if git_state else ""
        lines.append(f"- Git branch: {entry.git_branch}{suffix}")

    text = "\n".join(lines)
    if len(text) > 1500:
        text = text[:1500].rstrip() + "..."
    return text


def render_delivery_context(entry: HandoffEntry) -> str:
    """Render bounded factual context for a targeted hand-off delivery."""
    lines = [HANDOFF_ACTION_LABEL, f"Session: {entry.title or entry.session_id}"]
    if entry.first_user_prompt:
        lines.append(f"First prompt: {_truncate(entry.first_user_prompt, 250)}")
    if entry.summary:
        goal = entry.summary.get("goal")
        stopping = entry.summary.get("stopping_point")
        next_steps = entry.summary.get("next_steps") or []
        if goal:
            lines.append(f"Goal: {_truncate(str(goal), 300)}")
        if stopping:
            lines.append(f"Stopped at: {_truncate(str(stopping), 300)}")
        if next_steps:
            lines.append("Next steps: " + "; ".join(_truncate(str(s), 150) for s in next_steps[:5]))
    else:
        if entry.last_prompt:
            lines.append(f"Last prompt: {_truncate(entry.last_prompt, 250)}")
        if entry.last_assistant_message:
            lines.append(f"Last response: {_truncate(entry.last_assistant_message, 250)}")
    if entry.files_touched:
        lines.append("Files touched: " + ", ".join(entry.files_touched[:10]))
    if entry.open_items:
        lines.append(
            "Open items: "
            + "; ".join(
                f"[{item.get('kind', '?')}] {_truncate(str(item.get('text', '')), 150)}"
                for item in entry.open_items[:5]
            )
        )
    if entry.git_branch:
        state = []
        if entry.git_dirty:
            state.append("dirty")
        if entry.git_ahead:
            state.append(f"ahead {entry.git_ahead}")
        suffix = f" ({', '.join(state)})" if state else ""
        lines.append(f"Git branch: {entry.git_branch}{suffix}")
    text = "\n".join(lines)
    return text if len(text) <= 1500 else text[:1497].rstrip() + "..."


def rotation_command(entry: HandoffEntry, mode: str) -> str | None:
    """Build the exact-pane command for an automatic or manual rotation."""
    if mode == "clear":
        title = re.sub(r"[\x00-\x1f\x7f]+", " ", entry.title or entry.session_id).strip()
        return f"/rename claude-monitor hand-off: {(title[:80] or entry.session_id)}\r/clear\r"
    if mode == "compact":
        return "/compact\r"
    return None


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
        git_state = []
        if entry.git_dirty:
            git_state.append("dirty")
        if entry.git_ahead:
            git_state.append(f"ahead {entry.git_ahead}")
        suffix = f" ({', '.join(git_state)})" if git_state else ""
        lines.append(f"- Git: `{entry.git_branch}`{suffix}")

    if entry.first_user_prompt:
        lines.append(f"- First prompt: {_truncate(entry.first_user_prompt, 300)}")

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

    try:
        project_files = os.listdir(PROJECTS_DIR)
    except OSError:
        project_files = []
    for name in project_files:
        if not name.endswith(".md"):
            continue
        slug = name[:-3]
        if slug in by_slug:
            continue
        try:
            os.remove(os.path.join(PROJECTS_DIR, name))
        except OSError as e:
            log.warning(f"Failed to remove stale markdown digest {name}: {e}")


def _git_rule_index_paths(cwd: str, *, max_seconds: float | None = None) -> tuple[str, str] | None:
    """Resolve the Git top-level and local exclude paths for *cwd*."""
    timeout = 2.0 if max_seconds is None else min(2.0, max_seconds)
    if timeout <= 0:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--show-toplevel", "--git-path", "info/exclude"],
            cwd=cwd,
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    lines = result.stdout.splitlines()
    if len(lines) < 2 or not lines[0] or not lines[1]:
        return None
    top_level = os.path.abspath(lines[0])
    exclude_path = lines[1]
    if not os.path.isabs(exclude_path):
        exclude_path = os.path.join(top_level, exclude_path)
    return top_level, os.path.abspath(exclude_path)


def _ensure_rule_index_excluded(exclude_path: str) -> bool:
    """Add the exact rule-index pattern to Git's local exclude file."""
    try:
        os.makedirs(os.path.dirname(exclude_path), exist_ok=True)
        with open(exclude_path, "a+", encoding="utf-8") as exclude_file:
            fcntl.flock(exclude_file.fileno(), fcntl.LOCK_EX)
            try:
                exclude_file.seek(0)
                existing = exclude_file.read()
                if RULE_INDEX_IGNORE_PATTERN not in existing.splitlines():
                    if existing and not existing.endswith("\n"):
                        exclude_file.write("\n")
                    exclude_file.write(RULE_INDEX_IGNORE_PATTERN + "\n")
                    exclude_file.flush()
                    os.fsync(exclude_file.fileno())
            finally:
                fcntl.flock(exclude_file.fileno(), fcntl.LOCK_UN)
    except OSError as e:
        log.warning(f"Failed to update Git local excludes {exclude_path}: {e}")
        return False
    return True


def render_project_rule_index(entries: list[HandoffEntry]) -> str:
    """Render the bounded local rule index for a project's hand-offs."""
    lines = [
        "<!-- Machine-generated by claude-monitor. Do not edit. -->",
        "# claude-monitor hand-off index",
        "",
        "This is a bounded index of recent hand-offs for this project. Consult a",
        "listed Claude Code transcript JSONL only when more context is needed.",
        "",
    ]
    for entry in entries[:5]:
        summary = render_delivery_context(entry).splitlines()[1:]
        lines.extend(
            [
                f"## {entry.title or 'Untitled session'}",
                f"- Session: `{entry.session_id}`",
                f"- Source JSONL: `{entry.source_jsonl_path or 'unavailable'}`",
                "- Bounded summary:",
                *[f"  {line}" for line in summary],
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def write_project_rule_index(cwd: str, *, max_seconds: float | None = None) -> bool | None:
    """Write the local project rule index, or skip for non-Git directories."""
    paths = _git_rule_index_paths(cwd, max_seconds=max_seconds)
    if paths is None:
        return None
    top_level, exclude_path = paths
    if not _ensure_rule_index_excluded(exclude_path):
        return False
    entries = list_entries(project=project_slug(cwd), limit=5)
    index_path = os.path.join(top_level, RULE_INDEX_RELATIVE_PATH)
    return _atomic_write(index_path, render_project_rule_index(entries))
