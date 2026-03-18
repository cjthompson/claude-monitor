"""Tests for pure/static methods in screens — choices, questions, context_menu."""

import json
import os
import time

import pytest

from claude_monitor.screens.choices import ChoicesScreen
from claude_monitor.screens.questions import QuestionsScreen


# ---------------------------------------------------------------------------
# ChoicesScreen._format_choice (pure function, no TUI needed)
# ---------------------------------------------------------------------------

class TestChoicesFormatChoice:
    def _format(self, data: dict) -> str:
        """Call _format_choice on a detached ChoicesScreen instance."""
        screen = ChoicesScreen.__new__(ChoicesScreen)
        return screen._format_choice(data)

    def test_allowed_bash(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {"command": "ls -la", "description": "List files"},
            "session_id": "sess-12345678",
            "cwd": "/tmp/myproject",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "ALLOWED" in result
        assert "Bash" in result
        assert "ls -la" in result
        assert "List files" in result
        assert "myproject" in result

    def test_deferred(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Write",
            "tool_input": {"file_path": "/tmp/test.txt"},
            "session_id": "sess-12345678",
            "cwd": "/tmp/proj",
            "_decision": "deferred",
        }
        result = self._format(data)
        assert "DEFERRED" in result
        assert "Write" in result
        assert "/tmp/test.txt" in result

    def test_excluded_tool(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {},
            "session_id": "sess-12345678",
            "cwd": "",
            "_decision": "deferred",
            "_excluded_tool": True,
        }
        result = self._format(data)
        assert "MANUAL" in result

    def test_edit_tool(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Edit",
            "tool_input": {"file_path": "/tmp/file.py"},
            "session_id": "sess-1",
            "cwd": "/tmp/proj",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "/tmp/file.py" in result

    def test_read_tool(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Read",
            "tool_input": {"file_path": "/tmp/readme.md"},
            "session_id": "sess-1",
            "cwd": "/tmp/proj",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "/tmp/readme.md" in result

    def test_webfetch_tool(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "WebFetch",
            "tool_input": {"url": "https://example.com/api"},
            "session_id": "sess-1",
            "cwd": "/tmp/proj",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "https://example.com/api" in result

    def test_generic_tool(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "CustomTool",
            "tool_input": {"key1": "val1", "key2": "val2", "key3": "val3"},
            "session_id": "sess-1",
            "cwd": "/tmp/proj",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "key1" in result

    def test_suggestions_add_rules(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {},
            "session_id": "sess-1",
            "cwd": "/tmp",
            "_decision": "allowed",
            "permission_suggestions": [
                {"type": "addRules", "rules": [{"toolName": "Bash"}]},
            ],
        }
        result = self._format(data)
        assert "addRule(Bash)" in result

    def test_suggestions_set_mode(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {},
            "session_id": "sess-1",
            "cwd": "/tmp",
            "_decision": "allowed",
            "permission_suggestions": [
                {"type": "setMode", "mode": "trust"},
            ],
        }
        result = self._format(data)
        assert "setMode(trust)" in result

    def test_suggestions_other_type(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {},
            "session_id": "sess-1",
            "cwd": "/tmp",
            "_decision": "allowed",
            "permission_suggestions": [{"type": "customType"}],
        }
        result = self._format(data)
        assert "customType" in result

    def test_no_cwd(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {},
            "session_id": "sess-12345678",
            "cwd": "",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "ALLOWED" in result

    def test_bash_no_description(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "Bash",
            "tool_input": {"command": "echo hello"},
            "session_id": "sess-1",
            "cwd": "/tmp",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "echo hello" in result
        assert "desc" not in result


class TestChoicesLoadChoices:
    def test_load_from_file(self, isolated_state):
        events_file = isolated_state["events_file"]
        # Write some events
        with open(events_file, "w") as f:
            f.write(json.dumps({
                "hook_event_name": "PermissionRequest",
                "_timestamp": time.time(),
                "tool_name": "Bash",
                "tool_input": {"command": "ls"},
                "session_id": "s1",
                "cwd": "/tmp",
                "_decision": "allowed",
            }) + "\n")
            f.write(json.dumps({
                "hook_event_name": "Notification",
                "notification_type": "idle_prompt",
                "message": "idle",
                "session_id": "s1",
                "_timestamp": time.time(),
            }) + "\n")

        screen = ChoicesScreen.__new__(ChoicesScreen)
        import claude_monitor.screens.choices as choices_mod
        # Patch EVENTS_FILE in the module
        original = choices_mod.EVENTS_FILE
        choices_mod.EVENTS_FILE = events_file
        try:
            entries = screen._load_choices()
            assert len(entries) == 1  # Only PermissionRequest
            assert "ALLOWED" in entries[0]
        finally:
            choices_mod.EVENTS_FILE = original

    def test_load_empty_file(self, isolated_state):
        events_file = isolated_state["events_file"]
        with open(events_file, "w") as f:
            f.write("")

        screen = ChoicesScreen.__new__(ChoicesScreen)
        import claude_monitor.screens.choices as choices_mod
        original = choices_mod.EVENTS_FILE
        choices_mod.EVENTS_FILE = events_file
        try:
            entries = screen._load_choices()
            assert entries == []
        finally:
            choices_mod.EVENTS_FILE = original

    def test_load_bad_json(self, isolated_state):
        events_file = isolated_state["events_file"]
        with open(events_file, "w") as f:
            f.write("not json\n")

        screen = ChoicesScreen.__new__(ChoicesScreen)
        import claude_monitor.screens.choices as choices_mod
        original = choices_mod.EVENTS_FILE
        choices_mod.EVENTS_FILE = events_file
        try:
            entries = screen._load_choices()
            assert entries == []
        finally:
            choices_mod.EVENTS_FILE = original


# ---------------------------------------------------------------------------
# QuestionsScreen._format_question and _load_questions
# ---------------------------------------------------------------------------

class TestQuestionsFormatQuestion:
    def _format(self, data: dict) -> str:
        screen = QuestionsScreen.__new__(QuestionsScreen)
        return screen._format_question(data)

    def test_allowed(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "AskUserQuestion",
            "tool_input": {
                "questions": [{"question": "Continue?", "options": [{"label": "Yes"}, {"label": "No"}]}],
            },
            "session_id": "sess-12345678",
            "cwd": "/tmp/proj",
            "_decision": "allowed",
        }
        result = self._format(data)
        assert "ALLOWED" in result
        assert "AskUserQuestion" in result

    def test_deferred(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "AskUserQuestion",
            "tool_input": {},
            "session_id": "sess-1",
            "cwd": "/tmp",
            "_decision": "deferred",
        }
        result = self._format(data)
        assert "DEFERRED" in result

    def test_timeout(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "AskUserQuestion",
            "tool_input": {},
            "session_id": "sess-1",
            "cwd": "/tmp",
            "_decision": "timeout",
        }
        result = self._format(data)
        assert "TIMEOUT" in result

    def test_excluded(self):
        data = {
            "_timestamp": time.time(),
            "tool_name": "AskUserQuestion",
            "tool_input": {},
            "session_id": "sess-1",
            "cwd": "",
            "_decision": "deferred",
            "_excluded_tool": True,
        }
        result = self._format(data)
        assert "MANUAL" in result


class TestQuestionsLoadQuestions:
    def test_load_with_answers_merged(self, isolated_state):
        events_file = isolated_state["events_file"]
        with open(events_file, "w") as f:
            # PermissionRequest
            f.write(json.dumps({
                "hook_event_name": "PermissionRequest",
                "tool_name": "AskUserQuestion",
                "tool_input": {
                    "questions": [{"question": "Pick", "options": [{"label": "A"}, {"label": "B"}]}],
                },
                "session_id": "s1",
                "cwd": "/tmp",
                "_timestamp": time.time(),
                "_decision": "allowed",
            }) + "\n")
            # PostToolUse with answer
            f.write(json.dumps({
                "hook_event_name": "PostToolUse",
                "tool_name": "AskUserQuestion",
                "tool_input": {"answers": {"Pick": "A"}},
                "session_id": "s1",
                "_timestamp": time.time(),
            }) + "\n")

        screen = QuestionsScreen.__new__(QuestionsScreen)
        import claude_monitor.screens.questions as q_mod
        original = q_mod.EVENTS_FILE
        q_mod.EVENTS_FILE = events_file
        try:
            entries = screen._load_questions()
            assert len(entries) == 1
            assert "AskUserQuestion" in entries[0]
        finally:
            q_mod.EVENTS_FILE = original

    def test_load_no_events(self, isolated_state):
        events_file = isolated_state["events_file"]
        with open(events_file, "w") as f:
            f.write("")
        screen = QuestionsScreen.__new__(QuestionsScreen)
        import claude_monitor.screens.questions as q_mod
        original = q_mod.EVENTS_FILE
        q_mod.EVENTS_FILE = events_file
        try:
            entries = screen._load_questions()
            assert entries == []
        finally:
            q_mod.EVENTS_FILE = original

    def test_load_file_not_found(self, isolated_state):
        screen = QuestionsScreen.__new__(QuestionsScreen)
        import claude_monitor.screens.questions as q_mod
        original = q_mod.EVENTS_FILE
        q_mod.EVENTS_FILE = "/tmp/nonexistent-events-file.jsonl"
        try:
            entries = screen._load_questions()
            assert entries == []
        finally:
            q_mod.EVENTS_FILE = original
