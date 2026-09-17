"""Tests for claude_monitor.cli_handoff.

The CLI is exercised against stub ``handoff``/``llm`` modules matching the
shared contract, so these tests assert wiring and rendering rather than
re-testing the real store. The stubs are swapped onto ``cli_handoff``'s own
module attributes per test — never into ``sys.modules``, which would leak the
fakes into every other test module in the suite.
"""

import json
import os
import sys
import time
import types
from dataclasses import dataclass, field
from types import SimpleNamespace

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
    mod.stage_pending = MagicMock(return_value=SimpleNamespace())
    mod.discard_pending = MagicMock(return_value=None)
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
        fake_handoff.write_markdown,
        fake_handoff.prune,
        fake_handoff.stage_pending,
        fake_handoff.discard_pending,
        fake_llm.available,
    ):
        m.reset_mock(return_value=True, side_effect=True)
    fake_handoff.list_entries.return_value = []
    fake_handoff.load_entry.return_value = None
    fake_handoff.latest_for_cwd.return_value = None
    fake_handoff.capture.return_value = None
    fake_handoff.render_entry.return_value = "# Entry\n\nsome **bold** text"
    fake_handoff.render_digest.return_value = "# Digest\n\nsome **bold** text"
    fake_handoff.write_markdown.return_value = None
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


def _write_events(path, *events):
    path.write_text("\n".join(json.dumps(event) for event in events) + "\n")


def test_capture_resolves_live_session_from_cwd(capsys, tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {
            "session_id": "sess-live",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_timestamp": 1.0,
        },
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    monkeypatch.setattr(
        cli_handoff, "load_settings", lambda: SimpleNamespace(handoff_llm_enabled=False)
    )
    fake_handoff.latest_for_cwd.return_value = FakeHandoffEntry(
        session_id="sess-old", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-live", project_path=str(tmp_path), project_slug="proj-a"
    )
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])
    assert rc == 0
    fake_handoff.capture.assert_called_once()
    args, kwargs = fake_handoff.capture.call_args
    assert args[0] == "sess-live"
    assert args[1] == str(tmp_path)
    assert kwargs["with_llm"] is False
    fake_handoff.latest_for_cwd.assert_not_called()
    out = capsys.readouterr().out
    assert "sess-live" in out


def test_capture_ignores_ended_session_when_resolving_live_session(capsys, tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {
            "session_id": "sess-ended",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_timestamp": 3.0,
        },
        {
            "session_id": "sess-ended",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionEnd",
            "_timestamp": 4.0,
        },
        {
            "session_id": "sess-live",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_timestamp": 1.0,
        },
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-live", project_path=str(tmp_path), project_slug="proj-a"
    )

    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])

    assert rc == 0
    assert fake_handoff.capture.call_args.args[0] == "sess-live"
    assert "sess-live" in capsys.readouterr().out


def test_capture_defaults_cwd_to_getcwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {"session_id": "sess-1", "cwd": str(tmp_path), "hook_event_name": "SessionStart"},
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    rc = cli_handoff.main(["--no-color", "capture"])
    assert rc == 0
    fake_handoff.latest_for_cwd.assert_not_called()
    args, _ = fake_handoff.capture.call_args
    assert os.path.realpath(args[1]) == os.path.realpath(str(tmp_path))


def test_capture_llm_flag_forces_with_llm_true(tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, {"session_id": "sess-1", "cwd": str(tmp_path)})
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )
    fake_llm.available.return_value = True
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path), "--llm"])
    assert rc == 0
    _, kwargs = fake_handoff.capture.call_args
    assert kwargs["with_llm"] is True


def test_capture_llm_missing_api_key_errors_clearly(tmp_path, capsys, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, {"session_id": "sess-1", "cwd": str(tmp_path)})
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
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
    assert "could not determine a live session id" in err
    fake_handoff.capture.assert_not_called()


def test_capture_configured_llm_failure_keeps_heuristic_entry(tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, {"session_id": "sess-1", "cwd": str(tmp_path)})
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    monkeypatch.setattr(
        cli_handoff,
        "load_settings",
        lambda: SimpleNamespace(handoff_llm_enabled=True, handoff_llm_transport="minimax"),
    )
    fake_llm.available.return_value = False
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-1", project_path=str(tmp_path), project_slug="proj-a"
    )

    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])

    assert rc == 0
    fake_llm.available.assert_not_called()
    assert fake_handoff.capture.call_args[1]["with_llm"] is True


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


def test_capture_none_result_errors(tmp_path, capsys, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(events_file, {"session_id": "sess-1", "cwd": str(tmp_path)})
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = None
    rc = cli_handoff.main(["--no-color", "capture", "--cwd", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "could not capture session" in err


def test_rotate_defaults_to_clear_and_sends_safe_rename_then_clear(tmp_path, capsys, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {
            "session_id": "sess-live",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_iterm_session_id": "pane-1",
        },
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="sess-live",
        project_path=str(tmp_path),
        project_slug="proj",
        title="Ship it\nnow\t\x1b[31m",
    )
    monkeypatch.setattr(
        cli_handoff, "KeystrokeSender", SimpleNamespace(send_text=lambda sid, text: True)
    )
    sent = []
    monkeypatch.setattr(
        cli_handoff.KeystrokeSender, "send_text", lambda sid, text: sent.append((sid, text)) or True
    )

    rc = cli_handoff.main(["--no-color", "rotate", "--cwd", str(tmp_path)])

    assert rc == 0
    assert sent == [
        ("pane-1", "/rename claude-monitor hand-off: Ship it now [31m\r/clear\r"),
    ]
    fake_handoff.stage_pending.assert_called_once()
    assert fake_handoff.stage_pending.call_args.kwargs["rotation_mode"] == "clear"
    assert not any(character in sent[0][1] for character in ("\n", "\t", "\x1b"))


def test_rotate_compact_sends_only_compact(tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {
            "session_id": "s",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_iterm_session_id": "p",
        },
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="s", project_path=str(tmp_path), project_slug="p"
    )
    sent = []
    monkeypatch.setattr(
        cli_handoff.KeystrokeSender, "send_text", lambda sid, text: sent.append((sid, text)) or True
    )
    rc = cli_handoff.main(["--no-color", "rotate", "--cwd", str(tmp_path), "--mode", "compact"])
    assert rc == 0
    assert sent == [("p", "/compact\r")]


def test_rotate_explicit_session_uses_its_pane_not_newer_session(tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {
            "session_id": "wanted",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_iterm_session_id": "wanted-pane",
        },
        {
            "session_id": "newer",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_iterm_session_id": "newer-pane",
        },
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="wanted", project_path=str(tmp_path), project_slug="p"
    )
    sent = []
    monkeypatch.setattr(
        cli_handoff.KeystrokeSender, "send_text", lambda sid, text: sent.append((sid, text)) or True
    )
    rc = cli_handoff.main(
        ["--no-color", "rotate", "--session", "wanted", "--cwd", str(tmp_path), "--mode", "compact"]
    )
    assert rc == 0
    assert sent == [("wanted-pane", "/compact\r")]


def test_rotate_without_pane_keeps_local_capture_and_does_not_stage(tmp_path, capsys, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file, {"session_id": "s", "cwd": str(tmp_path), "hook_event_name": "SessionStart"}
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    fake_handoff.capture.return_value = FakeHandoffEntry(
        session_id="s", project_path=str(tmp_path), project_slug="p"
    )
    rc = cli_handoff.main(["--no-color", "rotate", "--cwd", str(tmp_path)])
    assert rc == 1
    assert "Captured hand-off entry" in capsys.readouterr().out
    fake_handoff.stage_pending.assert_not_called()


def test_rotate_transport_failure_retains_pending_and_keeps_capture(tmp_path, monkeypatch):
    events_file = tmp_path / "events.jsonl"
    _write_events(
        events_file,
        {
            "session_id": "s",
            "cwd": str(tmp_path),
            "hook_event_name": "SessionStart",
            "_iterm_session_id": "p",
        },
    )
    monkeypatch.setattr(cli_handoff, "EVENTS_FILE", str(events_file), raising=False)
    entry = FakeHandoffEntry(session_id="s", project_path=str(tmp_path), project_slug="p")
    fake_handoff.capture.return_value = entry
    monkeypatch.setattr(cli_handoff.KeystrokeSender, "send_text", lambda sid, text: False)
    rc = cli_handoff.main(["--no-color", "rotate", "--cwd", str(tmp_path), "--mode", "compact"])
    assert rc == 1
    fake_handoff.discard_pending.assert_not_called()


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
    fake_handoff.write_markdown.assert_called_once()
    out = capsys.readouterr().out
    assert "Pruned 2 project" in out


def test_prune_empty_store(capsys):
    rc = cli_handoff.main(["--no-color", "prune"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "No hand-off sessions recorded" in out
    fake_handoff.prune.assert_not_called()
    fake_handoff.write_markdown.assert_called_once()


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
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    fake_handoff.list_entries.return_value = [
        FakeHandoffEntry(session_id="a", project_path="/p", project_slug="proj-a", title="T")
    ]
    rc = cli_handoff.main(["list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "\x1b[" in out
