"""Tests for claude_monitor.handoff."""

import os
import subprocess
import sys
import types
from dataclasses import dataclass, field
from unittest import mock

import pytest

import claude_monitor
from claude_monitor import handoff


@pytest.fixture(autouse=True)
def _isolated_handoff_dir(tmp_path, monkeypatch):
    """Redirect all handoff storage into a tmp dir so real config is untouched."""
    handoff_dir = tmp_path / "handoff"
    sessions_dir = handoff_dir / "sessions"
    projects_dir = handoff_dir / "projects"
    digest_file = handoff_dir / "handoff.md"

    monkeypatch.setattr(handoff, "HANDOFF_DIR", str(handoff_dir))
    monkeypatch.setattr(handoff, "SESSIONS_DIR", str(sessions_dir))
    monkeypatch.setattr(handoff, "PROJECTS_DIR", str(projects_dir))
    monkeypatch.setattr(handoff, "DIGEST_FILE", str(digest_file))
    yield


@dataclass
class FakeFacts:
    ai_title: str | None = None
    first_user_prompt: str | None = None
    last_prompt: str | None = None
    last_assistant_message: str | None = None
    files_touched: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    open_items: list = field(default_factory=list)
    started_at: float | None = None
    ended_at: float | None = None


def _install(name: str, mod: types.ModuleType) -> types.ModuleType:
    """Install a fake submodule so ``from claude_monitor import <name>`` finds it.

    Setting ``sys.modules`` alone is not enough once the real module has been
    imported: ``from package import name`` prefers the attribute already bound
    on the package, so we must shadow that too.
    """
    sys.modules[f"claude_monitor.{name}"] = mod
    setattr(claude_monitor, name, mod)
    return mod


def _install_fake_transcript_module(parse_session_return):
    mod = types.ModuleType("claude_monitor.transcript")
    mod.parse_session = mock.Mock(return_value=parse_session_return)
    return _install("transcript", mod)


def _install_fake_llm_module(complete_side_effect=None, complete_return=None):
    mod = types.ModuleType("claude_monitor.llm")

    class LLMError(Exception):
        pass

    mod.LLMError = LLMError
    if complete_side_effect is not None:
        mod.complete = mock.Mock(side_effect=complete_side_effect)
    else:
        mod.complete = mock.Mock(return_value=complete_return)
    return _install("llm", mod)


@pytest.fixture(autouse=True)
def _clean_fake_modules():
    saved = {
        name: (sys.modules.get(f"claude_monitor.{name}"), getattr(claude_monitor, name, None))
        for name in ("transcript", "llm")
    }
    yield
    for name, (real_mod, real_attr) in saved.items():
        key = f"claude_monitor.{name}"
        if real_mod is None:
            sys.modules.pop(key, None)
        else:
            sys.modules[key] = real_mod
        if real_attr is None:
            if hasattr(claude_monitor, name):
                delattr(claude_monitor, name)
        else:
            setattr(claude_monitor, name, real_attr)


# --- project_slug --------------------------------------------------------


def test_project_slug_stable_and_safe():
    slug1 = handoff.project_slug("/Users/chris/dev/proj")
    slug2 = handoff.project_slug("/Users/chris/dev/proj")
    assert slug1 == slug2
    assert slug1.startswith("-")
    assert all(c.isalnum() or c in "._-" for c in slug1)


def test_project_slug_differs_for_different_paths():
    assert handoff.project_slug("/a/b") != handoff.project_slug("/a/c")


# --- git helpers ----------------------------------------------------------


def test_git_info_in_repo(tmp_path):
    result_branch = mock.Mock(returncode=0, stdout="main\n")
    result_status = mock.Mock(returncode=0, stdout="")
    with mock.patch("subprocess.run", side_effect=[result_branch, result_status]):
        branch, dirty = handoff._git_info(str(tmp_path))
    assert branch == "main"
    assert dirty is False


def test_git_info_dirty(tmp_path):
    result_branch = mock.Mock(returncode=0, stdout="feature\n")
    result_status = mock.Mock(returncode=0, stdout=" M file.py\n")
    with mock.patch("subprocess.run", side_effect=[result_branch, result_status]):
        branch, dirty = handoff._git_info(str(tmp_path))
    assert branch == "feature"
    assert dirty is True


def test_git_info_not_a_repo(tmp_path):
    result = mock.Mock(returncode=128, stdout="")
    with mock.patch("subprocess.run", side_effect=[result, result]):
        branch, dirty = handoff._git_info(str(tmp_path))
    assert branch is None
    assert dirty is False


def test_git_info_timeout(tmp_path):
    with mock.patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="git", timeout=2)):
        branch, dirty = handoff._git_info(str(tmp_path))
    assert branch is None
    assert dirty is False


def test_git_info_git_missing(tmp_path):
    with mock.patch("subprocess.run", side_effect=FileNotFoundError()):
        branch, dirty = handoff._git_info(str(tmp_path))
    assert branch is None
    assert dirty is False


# --- capture ----------------------------------------------------------------


def test_capture_with_transcript_facts(tmp_path):
    facts = FakeFacts(
        ai_title="Fix the widget",
        first_user_prompt="please fix widget",
        last_prompt="last prompt",
        last_assistant_message="done",
        files_touched=["a.py", "b.py"],
        agents=[{"type": "worker", "label": None, "status": "completed", "last_message": "ok"}],
        open_items=[{"kind": "plan", "text": "todo"}],
        started_at=1000.0,
        ended_at=2000.0,
    )
    _install_fake_transcript_module(facts)

    with mock.patch.object(handoff, "_git_info", return_value=("main", False)):
        entry = handoff.capture(
            "sess-1",
            str(tmp_path),
            reason="manual",
            event_stats={"approved": 3, "deferred": 1, "agents_spawned": 1},
        )

    assert entry is not None
    assert entry.title == "Fix the widget"
    assert entry.git_branch == "main"
    assert entry.files_touched == ["a.py", "b.py"]
    assert entry.event_stats["approved"] == 3

    loaded = handoff.load_entry("sess-1", slug=entry.project_slug)
    assert loaded is not None
    assert loaded.title == "Fix the widget"


def test_capture_survives_parse_session_none_with_event_stats(tmp_path):
    _install_fake_transcript_module(None)
    with mock.patch.object(handoff, "_git_info", return_value=(None, False)):
        entry = handoff.capture(
            "sess-2",
            str(tmp_path),
            event_stats={"approved": 1, "deferred": 0, "agents_spawned": 0},
        )
    assert entry is not None
    assert entry.title  # falls back to basename


def test_capture_returns_none_when_nothing_known(tmp_path):
    _install_fake_transcript_module(None)
    with mock.patch.object(handoff, "_git_info", return_value=(None, False)):
        entry = handoff.capture("sess-3", str(tmp_path), event_stats=None)
    assert entry is None


def test_capture_title_falls_back_to_first_prompt(tmp_path):
    facts = FakeFacts(ai_title=None, first_user_prompt="do the thing please, this is long")
    _install_fake_transcript_module(facts)
    with mock.patch.object(handoff, "_git_info", return_value=(None, False)):
        entry = handoff.capture("sess-4", str(tmp_path), event_stats={"approved": 1})
    assert entry is not None
    assert entry.title.startswith("do the thing")


def test_capture_writes_markdown(tmp_path):
    facts = FakeFacts(ai_title="Title", started_at=1.0, ended_at=2.0)
    _install_fake_transcript_module(facts)
    with mock.patch.object(handoff, "_git_info", return_value=(None, False)):
        handoff.capture("sess-5", str(tmp_path), event_stats={"approved": 1})
    assert os.path.exists(handoff.DIGEST_FILE)


# --- store round trip / list_entries -----------------------------------------


def _make_entry(session_id, slug, ended_at, project_path="/proj"):
    return handoff.HandoffEntry(
        session_id=session_id,
        project_path=project_path,
        project_slug=slug,
        title=f"Title {session_id}",
        ended_at=ended_at,
    )


def test_store_round_trip(tmp_path):
    entry = _make_entry("s1", "-proj", 100.0)
    handoff._save_entry(entry)
    loaded = handoff.load_entry("s1", slug="-proj")
    assert loaded == entry


def test_load_entry_searches_all_projects_when_no_slug(tmp_path):
    entry = _make_entry("s2", "-proj-x", 100.0)
    handoff._save_entry(entry)
    loaded = handoff.load_entry("s2")
    assert loaded is not None
    assert loaded.session_id == "s2"


def test_list_entries_ordering(tmp_path):
    handoff._save_entry(_make_entry("s1", "-p", 100.0))
    handoff._save_entry(_make_entry("s2", "-p", 300.0))
    handoff._save_entry(_make_entry("s3", "-p", 200.0))

    entries = handoff.list_entries(project="-p")
    assert [e.session_id for e in entries] == ["s2", "s3", "s1"]


def test_list_entries_filters_by_project(tmp_path):
    handoff._save_entry(_make_entry("s1", "-p1", 100.0))
    handoff._save_entry(_make_entry("s2", "-p2", 200.0))

    entries = handoff.list_entries(project="-p1")
    assert [e.session_id for e in entries] == ["s1"]


def test_list_entries_filters_by_since(tmp_path):
    handoff._save_entry(_make_entry("s1", "-p", 100.0))
    handoff._save_entry(_make_entry("s2", "-p", 300.0))

    entries = handoff.list_entries(project="-p", since=200.0)
    assert [e.session_id for e in entries] == ["s2"]


def test_list_entries_limit(tmp_path):
    for i in range(5):
        handoff._save_entry(_make_entry(f"s{i}", "-p", float(i)))
    entries = handoff.list_entries(project="-p", limit=2)
    assert len(entries) == 2


def test_list_entries_skips_corrupt_files(tmp_path):
    handoff._save_entry(_make_entry("s1", "-p", 100.0))
    project_dir = os.path.join(handoff.SESSIONS_DIR, "-p")
    os.makedirs(project_dir, exist_ok=True)
    with open(os.path.join(project_dir, "bad.json"), "w") as f:
        f.write("{not valid json")

    entries = handoff.list_entries(project="-p")
    assert [e.session_id for e in entries] == ["s1"]


# --- prune --------------------------------------------------------------


def test_prune_keeps_exactly_n(tmp_path):
    for i in range(5):
        handoff._save_entry(_make_entry(f"s{i}", "-p", float(i)))
    handoff.prune("-p", 2)
    entries = handoff.list_entries(project="-p")
    assert len(entries) == 2
    assert [e.session_id for e in entries] == ["s4", "s3"]


def test_prune_nonexistent_project_is_noop(tmp_path):
    handoff.prune("-does-not-exist", 3)  # should not raise


# --- latest_for_cwd -------------------------------------------------------


def test_latest_for_cwd_returns_newest(tmp_path):
    cwd = str(tmp_path)
    slug = handoff.project_slug(cwd)
    handoff._save_entry(_make_entry("s1", slug, 100.0, project_path=cwd))
    handoff._save_entry(_make_entry("s2", slug, 200.0, project_path=cwd))
    latest = handoff.latest_for_cwd(cwd)
    assert latest.session_id == "s2"


def test_latest_for_cwd_age_cap(tmp_path):
    import time as time_mod

    cwd = str(tmp_path)
    slug = handoff.project_slug(cwd)
    old_entry = _make_entry("s1", slug, time_mod.time() - 100 * 3600, project_path=cwd)
    handoff._save_entry(old_entry)
    result = handoff.latest_for_cwd(cwd, max_age_hours=1)
    assert result is None


def test_latest_for_cwd_none_when_empty(tmp_path):
    assert handoff.latest_for_cwd(str(tmp_path)) is None


# --- injection_context -----------------------------------------------------


def test_injection_context_excludes_current_session(tmp_path):
    import time as time_mod

    cwd = str(tmp_path)
    slug = handoff.project_slug(cwd)
    now = time_mod.time()
    handoff._save_entry(_make_entry("current", slug, now, project_path=cwd))
    handoff._save_entry(_make_entry("previous", slug, now - 10, project_path=cwd))

    ctx = handoff.injection_context(cwd, exclude_session_id="current")
    assert ctx is not None
    # Should reflect the "previous" entry's title, not the excluded "current" one.
    assert "Title previous" in ctx


def test_injection_context_none_when_stale(tmp_path):
    import time as time_mod

    cwd = str(tmp_path)
    slug = handoff.project_slug(cwd)
    handoff._save_entry(_make_entry("old", slug, time_mod.time() - 100 * 3600, project_path=cwd))
    ctx = handoff.injection_context(cwd, max_age_hours=1)
    assert ctx is None


def test_injection_context_none_when_empty(tmp_path):
    assert handoff.injection_context(str(tmp_path)) is None


def test_injection_context_under_size_budget(tmp_path):
    import time as time_mod

    cwd = str(tmp_path)
    slug = handoff.project_slug(cwd)
    entry = handoff.HandoffEntry(
        session_id="big",
        project_path=cwd,
        project_slug=slug,
        title="A" * 500,
        ended_at=time_mod.time(),
        last_prompt="B" * 5000,
        last_assistant_message="C" * 5000,
        files_touched=[f"file{i}.py" for i in range(50)],
        open_items=[{"kind": "plan", "text": "D" * 500} for _ in range(20)],
    )
    handoff._save_entry(entry)
    ctx = handoff.injection_context(cwd)
    assert ctx is not None
    assert len(ctx) <= 1600  # a little slack for the truncation marker


# --- summarize --------------------------------------------------------------


def test_summarize_happy_path(tmp_path):
    _install_fake_llm_module(
        complete_return='{"goal": "fix bug", "stopping_point": "tests pass", '
        '"next_steps": ["deploy"]}'
    )
    entry = handoff.HandoffEntry(
        session_id="s1", project_path="/proj", project_slug="-proj", title="T"
    )
    result = handoff.summarize(entry, settings=None)
    assert result == {
        "goal": "fix bug",
        "stopping_point": "tests pass",
        "next_steps": ["deploy"],
    }


def test_summarize_llm_error_returns_none(tmp_path):
    mod = _install_fake_llm_module()

    def _raise(*args, **kwargs):
        raise mod.LLMError("boom")

    mod.complete.side_effect = _raise
    entry = handoff.HandoffEntry(
        session_id="s1", project_path="/proj", project_slug="-proj", title="T"
    )
    assert handoff.summarize(entry, settings=None) is None


def test_summarize_unparseable_reply_returns_none(tmp_path):
    _install_fake_llm_module(complete_return="not json at all")
    entry = handoff.HandoffEntry(
        session_id="s1", project_path="/proj", project_slug="-proj", title="T"
    )
    assert handoff.summarize(entry, settings=None) is None


def test_summarize_parses_json_in_fences(tmp_path):
    reply = (
        "Sure, here you go:\n```json\n"
        '{"goal": "g", "stopping_point": "s", "next_steps": ["a", "b"]}\n'
        "```\nHope that helps!"
    )
    _install_fake_llm_module(complete_return=reply)
    entry = handoff.HandoffEntry(
        session_id="s1", project_path="/proj", project_slug="-proj", title="T"
    )
    result = handoff.summarize(entry, settings=None)
    assert result == {"goal": "g", "stopping_point": "s", "next_steps": ["a", "b"]}


def test_summarize_missing_keys_returns_none(tmp_path):
    _install_fake_llm_module(complete_return='{"goal": "only goal"}')
    entry = handoff.HandoffEntry(
        session_id="s1", project_path="/proj", project_slug="-proj", title="T"
    )
    assert handoff.summarize(entry, settings=None) is None


# --- markdown rendering ------------------------------------------------------


def test_render_entry_populated_nonempty():
    entry = handoff.HandoffEntry(
        session_id="s1",
        project_path="/proj",
        project_slug="-proj",
        title="Do a thing",
        started_at=1.0,
        ended_at=2.0,
        git_branch="main",
        git_dirty=True,
        last_prompt="prompt",
        last_assistant_message="response",
        files_touched=["a.py"],
        agents=[{"type": "worker", "label": "w1", "status": "running", "last_message": "hi"}],
        open_items=[{"kind": "plan", "text": "todo"}],
        event_stats={"approved": 1, "deferred": 0, "agents_spawned": 1},
        summary={"goal": "goal", "stopping_point": "stop", "next_steps": ["next"]},
    )
    rendered = handoff.render_entry(entry)
    assert rendered.strip()
    assert "Do a thing" in rendered
    assert "goal" in rendered


def test_render_entry_sparse_no_crash():
    entry = handoff.HandoffEntry(session_id="s1", project_path="/proj", project_slug="-proj")
    rendered = handoff.render_entry(entry)
    assert rendered.strip()


def test_render_digest_empty():
    digest = handoff.render_digest([])
    assert digest.strip()


def test_render_digest_groups_by_project():
    e1 = _make_entry("s1", "-p1", 100.0, project_path="/p1")
    e2 = _make_entry("s2", "-p2", 200.0, project_path="/p2")
    digest = handoff.render_digest([e2, e1])
    assert "/p1" in digest
    assert "/p2" in digest


def test_write_markdown_produces_files(tmp_path):
    handoff._save_entry(_make_entry("s1", "-p", 100.0, project_path="/p"))
    handoff.write_markdown(None)
    assert os.path.exists(handoff.DIGEST_FILE)
    project_file = os.path.join(handoff.PROJECTS_DIR, "-p.md")
    assert os.path.exists(project_file)
    with open(handoff.DIGEST_FILE) as f:
        assert f.read().strip()


def test_write_markdown_no_entries_no_crash(tmp_path):
    handoff.write_markdown(None)
    assert os.path.exists(handoff.DIGEST_FILE)
