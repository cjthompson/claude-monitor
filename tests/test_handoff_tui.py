"""Tests for the TUI hand-off surface: panel widget, session meta, poll worker."""

from __future__ import annotations

import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claude_monitor.commands import MonitorCommands
from claude_monitor.screens import handoff as handoff_screen


def _entry(**over):
    base = dict(
        session_id="sess-abcdef123",
        project_path="/Users/chris/dev/proj",
        project_slug="-Users-chris-dev-proj",
        title="Fix the widget test",
        started_at=time.time() - 3600,
        ended_at=time.time() - 600,
        capture_reason="manual",
    )
    base.update(over)
    return SimpleNamespace(**base)


class TestEntryLabel:
    def test_includes_title_project_and_age(self):
        label = handoff_screen.entry_label(_entry())
        assert "Fix the widget test" in label
        assert "proj" in label
        assert "ago" in label

    def test_falls_back_to_session_id_when_untitled(self):
        label = handoff_screen.entry_label(_entry(title=None))
        assert label.startswith("sess-abc")


class TestRelative:
    @pytest.mark.parametrize(
        "delta,expected",
        [
            (10, "just now"),
            (60 * 30, "30m ago"),
            (3600 * 5, "5h ago"),
            (3600 * 24 * 3, "3d ago"),
        ],
    )
    def test_buckets(self, delta, expected):
        assert handoff_screen._relative(time.time() - delta) == expected

    def test_none_is_unknown(self):
        assert handoff_screen._relative(None) == "?"


class _FakeApp:
    """Minimal stand-in for MonitorApp for the non-widget helpers."""

    def __init__(self, **settings):
        from claude_monitor.app_base import MonitorApp

        defaults = dict(
            handoff_enabled=True,
            handoff_llm_enabled=False,
            handoff_capture_idle_mins=0,
        )
        defaults.update(settings)
        self.settings = SimpleNamespace(**defaults)
        self._session_meta = {}
        self._handoff_polling = False
        self._record_session_meta = types.MethodType(MonitorApp._record_session_meta, self)
        self._capture_handoff = types.MethodType(MonitorApp._capture_handoff, self)
        self._start_handoff_polling = types.MethodType(MonitorApp._start_handoff_polling, self)
        self.poll_handoff = MagicMock()


class TestRecordSessionMeta:
    def test_records_cwd_transcript_and_timestamp(self):
        app = _FakeApp()
        app._record_session_meta(
            {
                "session_id": "s1",
                "cwd": "/tmp/proj",
                "transcript_path": "/tmp/t.jsonl",
                "_timestamp": 1000.0,
            }
        )
        assert app._session_meta["s1"] == {
            "cwd": "/tmp/proj",
            "transcript_path": "/tmp/t.jsonl",
            "last_event_ts": 1000.0,
        }

    def test_ignores_events_without_session_id(self):
        app = _FakeApp()
        app._record_session_meta({"cwd": "/tmp"})
        assert app._session_meta == {}

    def test_later_event_does_not_clear_known_cwd(self):
        app = _FakeApp()
        app._record_session_meta({"session_id": "s1", "cwd": "/tmp/proj", "_timestamp": 1.0})
        app._record_session_meta({"session_id": "s1", "_timestamp": 2.0})
        assert app._session_meta["s1"]["cwd"] == "/tmp/proj"
        assert app._session_meta["s1"]["last_event_ts"] == 2.0

    def test_never_raises_on_garbage(self):
        app = _FakeApp()
        app._record_session_meta({"session_id": "s1", "_timestamp": object()})
        assert "s1" in app._session_meta


class TestCaptureHandoff:
    def test_passes_meta_through_to_capture(self, monkeypatch):
        app = _FakeApp()
        app._session_meta["s1"] = {"cwd": "/tmp/proj", "transcript_path": "/tmp/t.jsonl"}

        fake = MagicMock(return_value=_entry())
        monkeypatch.setattr("claude_monitor.handoff.capture", fake)

        assert app._capture_handoff("s1", reason="manual") is True
        _, kwargs = fake.call_args
        assert fake.call_args[0] == ("s1", "/tmp/proj")
        assert kwargs["transcript_path"] == "/tmp/t.jsonl"
        assert kwargs["reason"] == "manual"
        assert app._session_meta["s1"]["last_capture_ts"] > 0

    def test_no_cwd_means_no_capture(self, monkeypatch):
        app = _FakeApp()
        app._session_meta["s1"] = {}
        fake = MagicMock()
        monkeypatch.setattr("claude_monitor.handoff.capture", fake)
        assert app._capture_handoff("s1", reason="manual") is False
        fake.assert_not_called()

    def test_llm_used_for_manual_when_enabled(self, monkeypatch):
        app = _FakeApp(handoff_llm_enabled=True)
        app._session_meta["s1"] = {"cwd": "/tmp/proj"}
        fake = MagicMock(return_value=_entry())
        monkeypatch.setattr("claude_monitor.handoff.capture", fake)
        app._capture_handoff("s1", reason="manual")
        assert fake.call_args[1]["with_llm"] is True

    def test_llm_never_used_for_session_end(self, monkeypatch):
        app = _FakeApp(handoff_llm_enabled=True)
        app._session_meta["s1"] = {"cwd": "/tmp/proj"}
        fake = MagicMock(return_value=_entry())
        monkeypatch.setattr("claude_monitor.handoff.capture", fake)
        app._capture_handoff("s1", reason="session_end")
        assert fake.call_args[1]["with_llm"] is False


class TestStartHandoffPolling:
    def test_not_started_when_disabled(self):
        app = _FakeApp(handoff_enabled=False, handoff_capture_idle_mins=30)
        app._start_handoff_polling()
        app.poll_handoff.assert_not_called()

    def test_not_started_when_idle_mins_zero(self):
        app = _FakeApp(handoff_capture_idle_mins=0)
        app._start_handoff_polling()
        app.poll_handoff.assert_not_called()

    def test_started_once_when_enabled(self):
        app = _FakeApp(handoff_capture_idle_mins=30)
        app._start_handoff_polling()
        app._start_handoff_polling()
        app.poll_handoff.assert_called_once()
        assert app._handoff_polling is True


class TestCommandPalette:
    @pytest.mark.parametrize("action", ["show_handoff", "capture_handoff", "refresh_handoff"])
    def test_handoff_actions_registered(self, action):
        assert any(a == action for _, a in MonitorCommands.COMMANDS_LIST)

    def test_commands_list_stays_alphabetical(self):
        names = [n for n, _ in MonitorCommands.COMMANDS_LIST]
        assert names == sorted(names)


class TestBindings:
    @pytest.mark.parametrize("module", ["claude_monitor.tui_simple"])
    def test_handoff_keys_bound(self, module):
        import importlib

        mod = importlib.import_module(module)
        app_cls = mod.SimpleTUI
        actions = {b.action for b in app_cls.BINDINGS}
        assert "show_handoff" in actions
        assert "capture_handoff" in actions
