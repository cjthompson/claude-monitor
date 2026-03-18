"""Tests for claude_monitor/__init__.py — utilities and state functions."""

import json
import os

import pytest

from claude_monitor import (
    __version__,
    read_state,
    extract_iterm_session_id,
    fmt_duration,
    _DEFAULT_STATE,
)


class TestVersion:
    def test_version_exists(self):
        assert isinstance(__version__, str)
        assert len(__version__) > 0


class TestReadState:
    def test_defaults_when_missing(self, isolated_state):
        """No state file -> returns defaults."""
        state = read_state()
        assert state == _DEFAULT_STATE

    def test_reads_valid_state(self, isolated_state):
        state_data = {"global_paused": True, "paused_sessions": ["a", "b"]}
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state_data, f)
        state = read_state()
        assert state["global_paused"] is True
        assert state["paused_sessions"] == ["a", "b"]

    def test_corrupt_json(self, isolated_state):
        with open(isolated_state["state_file"], "w") as f:
            f.write("not valid json{{{")
        state = read_state()
        assert state == _DEFAULT_STATE


class TestExtractItermSessionId:
    def test_with_colon(self):
        assert extract_iterm_session_id("w0t0p2:uuid-123") == "uuid-123"

    def test_without_colon(self):
        assert extract_iterm_session_id("just-uuid") == "just-uuid"

    def test_empty(self):
        assert extract_iterm_session_id("") == ""


class TestFmtDuration:
    def test_seconds_only(self):
        assert fmt_duration(45) == "45s"

    def test_zero(self):
        assert fmt_duration(0) == "0s"

    def test_minutes(self):
        assert fmt_duration(125) == "2m05s"

    def test_minutes_compact(self):
        assert fmt_duration(125, compact=True) == "2m"

    def test_exact_minute(self):
        assert fmt_duration(60) == "1m00s"

    def test_exact_minute_compact(self):
        assert fmt_duration(60, compact=True) == "1m"

    def test_hours(self):
        assert fmt_duration(3661) == "1h01m"

    def test_hours_exact(self):
        assert fmt_duration(7200) == "2h00m"

    def test_just_under_hour(self):
        assert fmt_duration(3599) == "59m59s"

    def test_just_under_hour_compact(self):
        assert fmt_duration(3599, compact=True) == "59m"

    def test_just_under_minute(self):
        assert fmt_duration(59) == "59s"

    def test_large_hours(self):
        assert fmt_duration(36000) == "10h00m"
