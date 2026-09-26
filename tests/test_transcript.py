"""Tests for claude_monitor.transcript."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from claude_monitor import transcript

FIXTURE = Path(__file__).parent / "fixtures" / "transcript_sample.jsonl"


def test_parse_ai_title_last_wins():
    facts = transcript.parse(FIXTURE)
    assert facts.ai_title == "Fix flaky widget test in test_widgets.py"


def test_parse_last_prompt_skips_entries_without_key():
    facts = transcript.parse(FIXTURE)
    assert facts.last_prompt == "Please fix the flaky test in test_widgets.py"


def test_first_user_prompt_prefers_typed_human():
    facts = transcript.parse(FIXTURE)
    assert facts.first_user_prompt == "Please fix the flaky test in test_widgets.py"


def test_noise_filtering_excludes_meta_and_command_wrappers():
    facts = transcript.parse(FIXTURE)
    # None of the noise lines (isMeta, <command-name>, <local-command-stdout>,
    # turnCompanion) should have become first_user_prompt.
    assert "command-name" not in (facts.first_user_prompt or "")
    assert "turnCompanion" not in (facts.first_user_prompt or "")


def test_last_assistant_message():
    facts = transcript.parse(FIXTURE)
    assert facts.last_assistant_message == (
        "Applied the fix, ran the tests, and they now pass consistently."
    )


def test_files_touched_dedupe_and_order():
    facts = transcript.parse(FIXTURE)
    assert facts.files_touched == [
        "/Users/chris/dev/project/test_widgets.py",
        "/Users/chris/dev/project/new_file.py",
        "/Users/chris/dev/project/config.yaml",
    ]


def test_agents_running_vs_completed():
    facts = transcript.parse(FIXTURE)
    by_type = {a["type"]: a for a in facts.agents}

    reviewer = by_type["code-reviewer"]
    assert reviewer["status"] == "completed"
    assert reviewer["label"] == "Review the fix"
    assert reviewer["last_message"] == "Looks good, one nit about naming."

    general = by_type["general-purpose"]
    assert general["status"] == "running"
    assert general["label"] == "Investigate a second thing"
    assert general["last_message"] is None


def test_open_items_detection():
    facts = transcript.parse(FIXTURE)
    kinds = {item["kind"]: item for item in facts.open_items}
    assert kinds["askuserquestion"]["text"] == "Should I also update the docs?"
    assert kinds["plan"]["text"] == "Plan awaiting approval"
    assert len(facts.open_items) == 2


def test_timestamps_converted_to_epoch():
    facts = transcript.parse(FIXTURE)
    assert facts.started_at is not None
    assert facts.ended_at is not None
    assert facts.started_at < facts.ended_at


def test_malformed_lines_do_not_crash():
    # The fixture itself contains malformed lines; parse() must not raise.
    facts = transcript.parse(FIXTURE)
    assert facts.ai_title is not None


def test_missing_file_returns_empty_facts():
    facts = transcript.parse("/nonexistent/path/does-not-exist.jsonl")
    assert facts == transcript.TranscriptFacts()


def test_find_transcript_with_hint(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    hinted = tmp_path / "session.jsonl"
    hinted.write_text('{"type":"ai-title","aiTitle":"x"}\n')
    found = transcript.find_transcript("some-session", "/some/cwd", hint=str(hinted))
    assert found == hinted


def test_find_transcript_rejects_hint_outside_projects_dir(tmp_path, monkeypatch):
    """A hint must stay inside the Claude projects dir — it is caller-supplied."""
    projects = tmp_path / "projects"
    projects.mkdir()
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(projects))

    outside = tmp_path / "secret.jsonl"
    outside.write_text('{"type":"ai-title","aiTitle":"x"}\n')

    assert transcript.find_transcript("sess-1", "/some/cwd", hint=str(outside)) is None
    escaping = projects / ".." / "secret.jsonl"
    assert transcript.find_transcript("sess-1", "/some/cwd", hint=str(escaping)) is None


@pytest.mark.parametrize(
    "bad_id",
    ["../../../etc/passwd", "a/b", ".hidden", "", "x" * 129, "sess$1"],
)
def test_find_transcript_rejects_unsafe_session_id(tmp_path, monkeypatch, bad_id):
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    assert transcript.find_transcript(bad_id, "/Users/chris/dev/project") is None


def test_find_transcript_rejects_slug_collision(tmp_path, monkeypatch):
    """The cwd slug is lossy, so a transcript recording a different cwd is refused."""
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    slug_dir = tmp_path / "-Users-chris-dev-my-proj"
    slug_dir.mkdir()
    session_file = slug_dir / "sess-42.jsonl"
    session_file.write_text('{"type":"user","cwd":"/Users/chris/dev/my.proj"}\n')

    # Same slug, different real directory.
    assert transcript.find_transcript("sess-42", "/Users/chris/dev/my_proj") is None
    assert transcript.find_transcript("sess-42", "/Users/chris/dev/my.proj") == session_file


def test_find_transcript_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    found = transcript.find_transcript("missing-session", "/Users/chris/dev/project")
    assert found is None


def test_find_transcript_slugifies_cwd(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    slug_dir = tmp_path / "-Users-chris-dev-my-proj"
    slug_dir.mkdir()
    session_file = slug_dir / "sess-42.jsonl"
    session_file.write_text('{"type":"ai-title","aiTitle":"x"}\n')

    found = transcript.find_transcript("sess-42", "/Users/chris/dev/my.proj")
    assert found == session_file


def test_parse_session_missing_returns_none(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    result = transcript.parse_session("missing-session", "/Users/chris/dev/project")
    assert result is None


def test_parse_session_found(tmp_path, monkeypatch):
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(tmp_path))
    slug_dir = tmp_path / "-Users-chris-dev-project"
    slug_dir.mkdir()
    session_file = slug_dir / "sess-99.jsonl"
    session_file.write_text('{"type":"ai-title","aiTitle":"Hello","sessionId":"sess-99"}\n')

    result = transcript.parse_session("sess-99", "/Users/chris/dev/project")
    assert result is not None
    assert result.ai_title == "Hello"
    assert result.source_jsonl_path == str(session_file)


def test_large_file_tail_path(tmp_path):
    """Files over the size threshold should still yield correct last-wins facts."""
    big_file = tmp_path / "big-session.jsonl"

    lines = []
    lines.append(
        json.dumps(
            {
                "type": "user",
                "uuid": "head-1",
                "timestamp": "2026-08-25T09:00:00.000Z",
                "cwd": "/Users/chris/dev/project",
                "origin": {"kind": "human"},
                "promptSource": "typed",
                "message": {"role": "user", "content": "First prompt of a very long session"},
            }
        )
    )
    # Pad with filler assistant text blocks to exceed the large-file threshold.
    filler_text = "x" * 1000
    padding_line = json.dumps(
        {
            "type": "assistant",
            "timestamp": "2026-08-25T09:30:00.000Z",
            "message": {"role": "assistant", "content": [{"type": "text", "text": filler_text}]},
        }
    )
    needed_bytes = transcript._LARGE_FILE_THRESHOLD + 500_000
    while sum(len(line) + 1 for line in lines) < needed_bytes:
        lines.append(padding_line)

    lines.append(
        json.dumps(
            {
                "type": "ai-title",
                "aiTitle": "Tail title wins",
                "sessionId": "big-session",
            }
        )
    )
    lines.append(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-25T23:00:00.000Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Final message near the tail."}],
                },
            }
        )
    )

    big_file.write_text("\n".join(lines) + "\n")
    assert big_file.stat().st_size > transcript._LARGE_FILE_THRESHOLD

    facts = transcript.parse(big_file)
    assert facts.ai_title == "Tail title wins"
    assert facts.last_assistant_message == "Final message near the tail."
    # first_user_prompt / started_at come from the head chunk.
    assert facts.first_user_prompt == "First prompt of a very long session"
    assert facts.started_at is not None


def test_list_transcripts_searches_metadata_only_and_limits_results(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    alpha = root / "-tmp-alpha"
    beta = root / "-tmp-beta"
    alpha.mkdir(parents=True)
    beta.mkdir()
    older = alpha / "old-session.jsonl"
    newer = alpha / "new-session.jsonl"
    other = beta / "other-session.jsonl"
    for path in (older, newer, other):
        path.write_text("not valid JSON\n")
    os.utime(older, (10, 10))
    os.utime(newer, (30, 30))
    os.utime(other, (20, 20))
    outside = tmp_path / "outside.jsonl"
    outside.write_text("outside\n")
    (alpha / "linked-session.jsonl").symlink_to(outside)
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(root))
    monkeypatch.setattr(
        transcript,
        "_read_head_lines",
        lambda *args: pytest.fail("catalog read transcript contents"),
    )

    assert [item.session_id for item in transcript.list_transcripts("alpha", limit=1)] == [
        "new-session"
    ]
    assert [item.session_id for item in transcript.list_transcripts("old-session")] == [
        "old-session"
    ]
    assert [item.session_id for item in transcript.list_transcripts()] == [
        "new-session",
        "other-session",
        "old-session",
    ]


def test_selected_transcript_reads_only_valid_bounded_head(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "-tmp-alpha"
    project.mkdir(parents=True)
    selected = project / "old-session.jsonl"
    selected.write_text(json.dumps({"cwd": "/tmp/alpha", "type": "user"}) + "\n")
    other = project / "other-session.jsonl"
    other.write_text("not JSON\n")
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(root))
    candidates = transcript.list_transcripts()
    old = next(item for item in candidates if item.session_id == "old-session")

    assert transcript.selected_transcript_cwd(old) == "/tmp/alpha"
    assert other.read_text() == "not JSON\n"
    selected.unlink()
    with pytest.raises(ValueError, match="unavailable"):
        transcript.selected_transcript_cwd(old)


def test_selected_transcript_rejects_mismatched_project(tmp_path, monkeypatch):
    root = tmp_path / "projects"
    project = root / "-tmp-alpha"
    project.mkdir(parents=True)
    (project / "old-session.jsonl").write_text('{"cwd":"/tmp/other"}\n')
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(root))

    with pytest.raises(ValueError, match="project path"):
        transcript.selected_transcript_cwd(transcript.list_transcripts()[0])
