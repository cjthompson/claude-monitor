"""Tests for claude_monitor.cli_handoff.

The CLI is exercised against stub ``handoff``/``llm`` modules matching the
shared contract, so these tests assert wiring and rendering rather than
re-testing the real store. The stubs are swapped onto ``cli_handoff``'s own
module attributes per test — never into ``sys.modules``, which would leak the
fakes into every other test module in the suite.
"""

import os
import sys
import time
import types
from dataclasses import dataclass, field

import pytest


@dataclass
class FakeHandoffEntry:
    session_id: str
    project_path: str
    project_slug: str
    title: str | None = None
    started_at: float | None = None
    ended_at: float | None = None
    capture_reason: str = "manual"
    last_prompt: str | None = None
    last_assistant_message: str | None = None
    git_branch: str | None = None
    git_dirty: bool = False
    files_touched: list = field(default_factory=list)
    agents: list = field(default_factory=list)
    open_items: list = field(default_factory=list)
    event_stats: dict = field(default_factory=dict)
    summary: dict | None = None
    summary_model: str | None = None
    summary_generated_at: float | None = None


@dataclass(frozen=True)
class FakeProvider:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    extra_body: dict


def _make_handoff_stub() -> types.ModuleType:
    from unittest.mock import MagicMock

    mod = types.ModuleType("claude_monitor.handoff")
    mod.HANDOFF_DIR = "/tmp/fake-claude-monitor-handoff"
    mod.HandoffEntry = FakeHandoffEntry
    mod.project_slug = MagicMock(side_effect=lambda cwd: os.path.basename(cwd) or "root")
    mod.capture = MagicMock(return_value=None)
    mod.load_entry = MagicMock(return_value=None)
    mod.list_entries = MagicMock(return_value=[])
    mod.latest_for_cwd = MagicMock(return_value=None)
    mod.injection_context = MagicMock(return_value=None)
    mod.summarize = MagicMock(return_value=None)
    mod.render_entry = MagicMock(return_value="# Entry\n\nsome **bold** text")
    mod.render_digest = MagicMock(return_value="# Digest\n\nsome **bold** text")
    mod.write_markdown = MagicMock(return_value=None)
    mod.prune = MagicMock(return_value=None)
    return mod


def _make_llm_stub() -> types.ModuleType:
    from unittest.mock import MagicMock

    mod = types.ModuleType("claude_monitor.llm")

    class LLMError(Exception):
        pass

    mod.LLMError = LLMError
    mod.Provider = FakeProvider
    mod.PROVIDERS = {
        "minimax": FakeProvider("minimax", "https://x.example", "MINIMAX_API_KEY", "m1", {}),
        "openai": FakeProvider("openai", "https://y.example", "OPENAI_API_KEY", "m2", {}),
        "claude_cli": FakeProvider("claude_cli", "", "ANTHROPIC_API_KEY", "m3", {}),
    }
    mod.available = MagicMock(return_value=True)
    mod.complete = MagicMock(return_value="a summary")
    return mod


import claude_monitor.cli_handoff as cli_handoff  # noqa: E402

fake_handoff = _make_handoff_stub()
fake_llm = _make_llm_stub()


@pytest.fixture(autouse=True)
def _reset_stubs(monkeypatch):
    # Scope the fakes to cli_handoff's own module globals; monkeypatch undoes
    # this after each test so the real modules stay intact for everyone else.
    monkeypatch.setattr(cli_handoff, "handoff", fake_handoff)
    monkeypatch.setattr(cli_handoff, "llm", fake_llm)
    for m in (
        fake_handoff.capture,
        fake_handoff.load_entry,
        fake_handoff.list_entries,
        fake_handoff.latest_for_cwd,
        fake_handoff.render_entry,
        fake_handoff.render_digest,
        fake_handoff.prune,
        fake_llm.available,
    ):
        m.reset_mock(return_value=True, side_effect=True)
    fake_handoff.list_entries.return_value = []
    fake_handoff.load_entry.return_value = None
    fake_handoff.latest_for_cwd.return_value = None
    fake_handoff.capture.return_value = None
    fake_handoff.render_entry.return_value = "# Entry\n\nsome **bold** text"
    fake_handoff.render_digest.return_value = "# Digest\n\nsome **bold** text"
    fake_llm.available.return_value = True
    yield


# --- list -------------------------------------------------------------


def test_list_happy_path(capsys):
    now = time.time()
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(
            session_id="sess-1",
            project_path="/repo/proj-a",
            project_slug="proj-a",
            title="Fix the thing",
            started_at=now - 3600,
            git_branch="main",
            agents=[
                {"type": "reviewer", "label": None, "status": "completed", "last_message": None}
            ],
            summary={"goal": "g", "stopping_point": "s", "next_steps": []},
        )
    ]
    rc = cli_handoff.main(["--no-color", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Fix the thing" in out
    assert "proj-a" in out
    assert "main" in out
    assert "yes" in out  # has an LLM summary
    fake_handoff.list_entries.assert_called_once_with(project=None, limit=None, since=None)


def test_list_days_and_limit_filtering_reach_list_entries(capsys):
    before = time.time()
    rc = cli_handoff.main(
        ["--no-color", "list", "--project", "proj-a", "--days", "3", "--limit", "5"]
    )
    assert rc == 0
    _, kwargs = fake_handoff.list_entries.call_args
    assert kwargs["project"] == "proj-a"
    assert kwargs["limit"] == 5
    assert kwargs["since"] == pytest.approx(before - 3 * 86400, abs=5)


def test_list_empty_store_prints_friendly_message(capsys):
    rc = cli_handoff.main(["--no-color", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No hand-off sessions recorded" in out
    assert "handoff_enabled" in out
    assert "Traceback" not in out


# --- show ---------------------------------------------------------------


def test_show_latest_resolution(capsys):
    entry = FakeHandoffEntry(
        session_id="sess-9", project_path="/p", project_slug="proj-a", title="T"
    )
    fake_handoff.list_entries.return_value = [entry]
    rc = cli_handoff.main(["--no-color", "show", "latest"])
    assert rc == 0
    fake_handoff.render_entry.assert_called_once_with(entry)
    fake_handoff.list_entries.assert_called_once_with(project=None, limit=1)
    out = capsys.readouterr().out
    assert "Entry" in out
    assert "bold" in out


def test_show_latest_scoped_to_project(capsys):
    entry = FakeHandoffEntry(session_id="sess-9", project_path="/p", project_slug="proj-a")
    fake_handoff.list_entries.return_value = [entry]
    rc = cli_handoff.main(["--no-color", "show", "latest", "--project", "proj-a"])
    assert rc == 0
    fake_handoff.list_entries.assert_called_once_with(project="proj-a", limit=1)


def test_show_unknown_session_id_exits_1(capsys):
    fake_handoff.load_entry.return_value = None
    rc = cli_handoff.main(["--no-color", "show", "does-not-exist"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does-not-exist" in err


def test_show_known_session_id(capsys):
    entry = FakeHandoffEntry(session_id="sess-5", project_path="/p", project_slug="proj-a")
    fake_handoff.load_entry.return_value = entry
    rc = cli_handoff.main(["--no-color", "show", "sess-5"])
    assert rc == 0
    fake_handoff.render_entry.assert_called_once_with(entry)


# --- digest ---------------------------------------------------------------


def test_digest_happy_path(capsys):
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(session_id="s", project_path="/p", project_slug="proj-a")
    ]
    rc = cli_handoff.main(["--no-color", "digest"])
    assert rc == 0
    fake_handoff.render_digest.assert_called_once()
    out = capsys.readouterr().out
    assert "Digest" in out
    assert "bold" in out


def test_digest_empty_store_message(capsys):
    rc = cli_handoff.main(["--no-color", "digest"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No hand-off sessions recorded" in out


def test_digest_days_reaches_list_entries(capsys):
    before = time.time()
    cli_handoff.main(["--no-color", "digest", "--days", "7"])
    _, kwargs = fake_handoff.list_entries.call_args
    assert kwargs["since"] == pytest.approx(before - 7 * 86400, abs=5)


# --- capture ---------------------------------------------------------------


def test_capture_resolves_session_from_cwd(capsys, tmp_path):
    fake_handoff.latest_for_cwd.return_value = FakeHandoffEntry(
        session_id="sess-latest", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-latest", project_path=str(tmp_path), project_slug="proj-a"
    )
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])
    assert rc == 0
    fake_handoff.capture.assert_called_once()
    args, kwargs = fake_handoff.capture.call_args
    assert args[0] == "sess-latest"
    assert args[1] == str(tmp_path)
    assert kwargs["with_llm"] is False
    out = capsys.readouterr().out
    assert "sess-latest" in out


def test_capture_defaults_cwd_to_getcwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    fake_handoff.latest_for_cwd.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    rc = cli_handoff.main(["--no-color", "capture"])
    assert rc == 0
    (cwd_arg,), _ = fake_handoff.latest_for_cwd.call_args
    assert os.path.realpath(cwd_arg) == os.path.realpath(str(tmp_path))


def test_capture_llm_flag_forces_with_llm_true(tmp_path):
    fake_handoff.latest_for_cwd.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_llm.available.return_value = True
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path), "--llm"])
    assert rc == 0
    _, kwargs = fake_handoff.capture.call_args
    assert kwargs["with_llm"] is True


def test_capture_llm_missing_api_key_errors_clearly(tmp_path, capsys):
    fake_handoff.latest_for_cwd.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_llm.available.return_value = False
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path), "--llm"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "MINIMAX_API_KEY" in err
    fake_handoff.capture.assert_not_called()


def test_capture_cannot_resolve_session_errors(tmp_path, capsys):
    fake_handoff.latest_for_cwd.return_value = None
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not determine a session id" in err
    fake_handoff.capture.assert_not_called()


def test_capture_explicit_session_skips_resolution(tmp_path):
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-explicit", project_path=str(tmp_path), project_slug="proj-a"
    )
    rc = cli_handoff.main(
        ["--no-color", "capture", "--session", "sess-explicit", "--cwd", str(tmp_path)]
    )
    assert rc == 0
    fake_handoff.latest_for_cwd.assert_not_called()
    args, _ = fake_handoff.capture.call_args
    assert args[0] == "sess-explicit"


def test_capture_none_result_errors(tmp_path, capsys):
    fake_handoff.latest_for_cwd.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_handoff.capture.return_value = None
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not capture session" in err


# --- prune ---------------------------------------------------------------


def test_prune_happy_path(capsys):
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(session_id="a", project_path="/p1", project_slug="proj-a"),
        FakeHandoffEntry(session_id="b", project_path="/p2", project_slug="proj-b"),
    ]
    rc = cli_handoff.main(["--no-color", "prune", "--keep", "3"])
    assert rc == 0
    assert fake_handoff.prune.call_count == 2
    fake_handoff.prune.assert_any_call("proj-a", 3)
    fake_handoff.prune.assert_any_call("proj-b", 3)
    out = capsys.readouterr().out
    assert "Pruned 2 project" in out


def test_prune_empty_store(capsys):
    rc = cli_handoff.main(["--no-color", "prune"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No hand-off sessions recorded" in out
    fake_handoff.prune.assert_not_called()


# --- color handling ---------------------------------------------------------


def test_no_color_env_var_strips_ansi(capsys, monkeypatch):
    monkeypatch.setenv("NO_COLOR", "1")
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(session_id="a", project_path="/p", project_slug="proj-a", title="T")
    ]
    rc = cli_handoff.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out


def test_non_tty_stdout_produces_no_ansi_by_default(capsys):
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(session_id="a", project_path="/p", project_slug="proj-a", title="T")
    ]
    # capsys's stdout replacement is not a TTY, so color should be off even
    # without --no-color or NO_COLOR.
    rc = cli_handoff.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" not in out


def test_color_used_when_tty_and_not_disabled(capsys, monkeypatch):
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(session_id="a", project_path="/p", project_slug="proj-a", title="T")
    ]
    rc = cli_handoff.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" in out
