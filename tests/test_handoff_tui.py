"""Tests for the TUI hand-off surface: panel widget, session meta, poll worker."""

from __future__ import annotations

import time
import types
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from claude_monitor.app_base import HandoffTarget
from claude_monitor.commands import MonitorCommands
from claude_monitor.screens import handoff as handoff_screen
from tests.conftest import _make_permission_event


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
            handoff_auto_rotate_enabled=False,
            handoff_auto_rotate_idle_mins=240,
        )
        defaults.update(settings)
        self.settings = SimpleNamespace(**defaults)
        self._session_meta = {}
        self.panels = {}
        self._handoff_polling = False
        self._record_session_meta = types.MethodType(MonitorApp._record_session_meta, self)
        self._load_manual_handoff_meta = types.MethodType(
            MonitorApp._load_manual_handoff_meta, self
        )
        self._capture_handoff_entry = types.MethodType(MonitorApp._capture_handoff_entry, self)
        self._capture_handoff = types.MethodType(MonitorApp._capture_handoff, self)
        self._is_substantive_handoff_activity = types.MethodType(
            MonitorApp._is_substantive_handoff_activity, self
        )
        self._stage_idle_rotation = types.MethodType(MonitorApp._stage_idle_rotation, self)
        self._deliver_deferred_idle_rotation = types.MethodType(
            MonitorApp._deliver_deferred_idle_rotation, self
        )
        self._cancel_idle_rotation = types.MethodType(MonitorApp._cancel_idle_rotation, self)
        self._poll_handoff_once = types.MethodType(MonitorApp._poll_handoff_once, self)
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
        assert app._session_meta["s1"]["cwd"] == "/tmp/proj"
        assert app._session_meta["s1"]["transcript_path"] == "/tmp/t.jsonl"
        assert app._session_meta["s1"]["last_event_ts"] == 1000.0
        assert app._session_meta["s1"]["live"] is True
        assert app._session_meta["s1"]["input_wait_unsafe"] is False
        assert app._session_meta["s1"]["auto_rotation_armed"] is True

    def test_ignores_events_without_session_id(self):
        app = _FakeApp()
        app._record_session_meta({"cwd": "/tmp"})
        assert app._session_meta == {}

    def test_replayed_event_does_not_enroll_idle_capture(self):
        app = _FakeApp(handoff_capture_idle_mins=5)
        app._record_session_meta(
            {
                "session_id": "old-session",
                "cwd": "/tmp/proj",
                "_timestamp": 1.0,
                "_replay": True,
            }
        )
        assert app._session_meta["old-session"]["cwd"] == "/tmp/proj"
        assert not app._session_meta["old-session"].get("observed_this_run")
        app.panels["old-session"] = object()
        app._poll_handoff_once(1_000_000.0)
        assert not app._session_meta["old-session"].get("last_capture_ts")

    def test_replayed_session_end_is_not_live_for_manual_capture(self):
        app = _FakeApp()
        app._record_session_meta(
            {"session_id": "old", "cwd": "/tmp/proj", "_replay": True, "_timestamp": 1.0}
        )
        app._record_session_meta(
            {"session_id": "old", "hook_event_name": "SessionEnd", "_replay": True}
        )
        assert app._session_meta["old"]["ended"] is True
        assert app._session_meta["old"]["live"] is False

    def test_manual_lookup_restores_only_requested_open_pane(self, tmp_path, monkeypatch):
        events = tmp_path / "events.jsonl"
        events.write_text(
            '{"session_id":"old","cwd":"/tmp/proj","_iterm_session_id":"pane",'
            '"transcript_path":"/tmp/old.jsonl","_timestamp":1}\n'
            '{"session_id":"other","cwd":"/tmp/other","_iterm_session_id":"other-pane"}\n'
        )
        monkeypatch.setattr("claude_monitor.app_base.EVENTS_FILE", str(events))
        app = _FakeApp(handoff_capture_idle_mins=1)

        app._load_manual_handoff_meta(pane_id="pane")

        assert set(app._session_meta) == {"old"}
        assert app._session_meta["old"]["transcript_path"] == "/tmp/old.jsonl"
        assert not app._session_meta["old"].get("observed_this_run")

    def test_fresh_event_restores_exact_pending_rotation(self, monkeypatch):
        app = _FakeApp(handoff_auto_rotate_enabled=True)
        pending = SimpleNamespace(
            entry_session_id="s1",
            project_slug="-tmp-proj",
            rotation_mode="clear",
            target_iterm_session_id="pane",
        )
        finder = MagicMock(return_value=pending)
        sender = MagicMock(return_value=True)
        monkeypatch.setattr("claude_monitor.handoff.find_pending_idle", finder)
        monkeypatch.setattr("claude_monitor.handoff.load_entry", lambda *a: _entry())
        monkeypatch.setattr("claude_monitor.handoff.rotation_command", lambda *a: "rotate")
        monkeypatch.setattr("claude_monitor.app_base.KeystrokeSender.send_text", sender)

        app._record_session_meta(
            {
                "session_id": "s1",
                "cwd": "/tmp/proj",
                "_iterm_session_id": "pane",
                "hook_event_name": "Stop",
            }
        )

        finder.assert_called_once_with(iterm_session_id="pane", originating_session_id="s1")
        sender.assert_called_once_with("pane", "rotate")

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

    def test_records_exact_pane_liveness_and_waiting_marker(self):
        app = _FakeApp()
        app._record_session_meta(
            {
                "session_id": "s1",
                "cwd": "/tmp/proj",
                "_iterm_session_id": "w0t0p2:pane-1",
                "_decision": "deferred",
                "_timestamp": 3.0,
            }
        )
        assert app._session_meta["s1"]["iterm_session_id"] == "pane-1"
        assert app._session_meta["s1"]["live"] is True
        assert app._session_meta["s1"]["input_wait_unsafe"] is True

    def test_clears_waiting_marker_on_idle_and_session_end(self):
        app = _FakeApp()
        app._record_session_meta(
            {
                "session_id": "s1",
                "_decision": "deferred",
                "_timestamp": 1.0,
            }
        )
        app._record_session_meta(
            {
                "session_id": "s1",
                "hook_event_name": "Notification",
                "notification_type": "idle_prompt",
            }
        )
        assert app._session_meta["s1"]["input_wait_unsafe"] is False
        app._record_session_meta({"session_id": "s1", "_decision": "deferred", "_timestamp": 2.0})
        app._record_session_meta({"session_id": "s1", "hook_event_name": "SessionEnd"})
        assert app._session_meta["s1"]["live"] is False
        assert app._session_meta["s1"]["input_wait_unsafe"] is False


def test_handoff_target_is_immutable():
    target = HandoffTarget("session", "pane", "working")
    with pytest.raises(AttributeError):
        target.state = "waiting"


class TestCaptureHandoff:
    def test_passes_meta_through_to_capture(self, monkeypatch):
        app = _FakeApp()
        app._session_meta["s1"] = {"cwd": "/tmp/proj", "transcript_path": "/tmp/t.jsonl"}

        fake = MagicMock(return_value=_entry())
        monkeypatch.setattr("claude_monitor.handoff.capture", fake)

        assert app._capture_handoff("s1", reason="manual") is not None
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


class TestAutomaticRotation:
    def test_waiting_defers_then_stop_sends_once(self, monkeypatch):
        app = _FakeApp(handoff_auto_rotate_enabled=True, handoff_auto_rotate_idle_mins=1)
        app.panels["pane"] = object()
        app._record_session_meta(
            {
                "session_id": "s1",
                "cwd": "/tmp/proj",
                "_iterm_session_id": "pane",
                "hook_event_name": "PermissionRequest",
                "tool_name": "AskUserQuestion",
                "_timestamp": 1.0,
            }
        )
        entry = _entry(session_id="s1", project_path="/tmp/proj", project_slug="-tmp-proj")
        monkeypatch.setattr("claude_monitor.handoff.capture", lambda *a, **k: entry)
        monkeypatch.setattr(
            "claude_monitor.handoff.stage_pending",
            lambda *a, **k: SimpleNamespace(
                entry_session_id="s1",
                project_slug="-tmp-proj",
                rotation_mode="clear",
                target_iterm_session_id="pane",
                generation_token="g",
            ),
        )
        monkeypatch.setattr("claude_monitor.handoff.load_entry", lambda *a: entry)
        sender = MagicMock(return_value=True)
        monkeypatch.setattr("claude_monitor.app_base.KeystrokeSender.send_text", sender)
        app._poll_handoff_once(62.0)
        assert app._session_meta["s1"]["auto_rotation_deferred"] is True
        assert sender.call_count == 0
        app._record_session_meta(
            {
                "session_id": "s1",
                "_iterm_session_id": "pane",
                "hook_event_name": "Stop",
                "_timestamp": 63.0,
            }
        )
        assert sender.call_count == 1


class TestIdleCapture:
    def test_captures_only_open_observed_sessions_and_rearms_after_activity(self, monkeypatch):
        app = _FakeApp(handoff_capture_idle_mins=1)
        app.panels["open-pane"] = object()
        for sid, pane in (("open", "open-pane"), ("closed", "closed-pane")):
            app._record_session_meta(
                {
                    "session_id": sid,
                    "cwd": "/tmp/proj",
                    "_iterm_session_id": pane,
                    "_timestamp": 1.0,
                }
            )
        captures = []
        monkeypatch.setattr(
            "claude_monitor.handoff.capture",
            lambda sid, *args, **kwargs: captures.append(sid) or _entry(session_id=sid),
        )
        monkeypatch.setattr("claude_monitor.app_base.time.time", lambda: 65.0)

        app._poll_handoff_once(62.0)
        app._poll_handoff_once(66.0)
        assert captures == ["open"]
        assert "closed" not in app._session_meta

        app._record_session_meta({"session_id": "open", "_timestamp": 70.0})
        app._poll_handoff_once(131.0)
        assert captures == ["open", "open"]


class TestStartHandoffPolling:
    def test_does_not_rehydrate_historical_sessions(self, monkeypatch, tmp_path):
        events = tmp_path / "events.jsonl"
        events.write_text(
            '{"session_id":"old-session","cwd":"/tmp/proj",'
            '"hook_event_name":"SessionStart","_timestamp":1.0}\n'
        )
        monkeypatch.setattr("claude_monitor.app_base.EVENTS_FILE", str(events))
        app = _FakeApp(handoff_capture_idle_mins=5)

        app._start_handoff_polling()

        assert app._session_meta == {}

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
    @pytest.mark.parametrize(
        "action", ["show_handoff", "capture_handoff", "refresh_handoff", "rotate_selected_handoff"]
    )
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


def test_handoff_panel_exposes_selected_entry():
    panel = handoff_screen.HandoffPanel()
    panel._entries = [_entry(session_id="first"), _entry(session_id="second")]
    panel._selected_session_id = "second"
    assert panel.selected_entry.session_id == "second"


async def test_show_session_summary_from_context_menu_targets_exact_session(
    app_fixture, inject_message, monkeypatch
):
    """The pane menu opens the Hand-off tab on its pane's saved session."""
    from textual.widgets import Markdown, OptionList, TabbedContent

    from claude_monitor.screens.context_menu import PaneContextMenu

    entries = [
        _entry(session_id="newest", title="Newest entry"),
        _entry(session_id="target", title="Target entry"),
    ]
    monkeypatch.setattr(handoff_screen.HandoffPanel, "_load_entries", lambda self: entries)
    monkeypatch.setattr(
        "claude_monitor.handoff.render_entry", lambda entry: f"# {entry.session_id}"
    )
    app_fixture.settings.handoff_enabled = True
    capture_worker = MagicMock()
    monkeypatch.setattr(app_fixture, "_capture_handoff_worker", capture_worker)

    async with app_fixture.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await inject_message(_make_permission_event(session_id="target", cwd="/tmp/target"))
        for _ in range(5):
            await pilot.pause()

        pane = app_fixture.panels["target"]
        await pilot.click(pane, offset=(1, 0))
        await pilot.pause()

        menu = next(
            screen for screen in app_fixture.screen_stack if isinstance(screen, PaneContextMenu)
        )
        options = menu.query_one("#ctx-options", OptionList)
        assert options.get_option_at_index(1).id == "show_session_summary"
        options.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        tabbed = app_fixture.query_one("#tab-content", TabbedContent)
        panel = app_fixture.query_one(handoff_screen.HandoffPanel)
        assert tabbed.active == app_fixture.HANDOFF_TAB_ID
        assert panel._selected_session_id == "target"
        assert panel.selected_entry.session_id == "target"
        assert "# target" in panel.query_one("#handoff-markdown", Markdown).source

        app_fixture.action_refresh_handoff()
        assert panel._selected_session_id == "target"
        assert panel.selected_entry.session_id == "target"
        capture_worker.assert_not_called()


async def test_manual_transcript_picker_loads_only_selected_history(
    app_fixture, monkeypatch, tmp_path
):
    from textual.widgets import Input, ListView, Markdown

    from claude_monitor import transcript
    from claude_monitor.screens.transcript_picker import TranscriptPickerScreen

    root = tmp_path / "projects"
    project = root / "-tmp-proj"
    project.mkdir(parents=True)
    (project / "historic-session.jsonl").write_text('{"cwd":"/tmp/proj"}\n')
    (project / "other-session.jsonl").write_text('{"cwd":"/tmp/proj"}\n')
    monkeypatch.setattr(transcript, "CLAUDE_PROJECTS_DIR", str(root))
    app_fixture.settings.handoff_enabled = True
    entries = []
    monkeypatch.setattr(handoff_screen.HandoffPanel, "_load_entries", lambda self: entries)
    monkeypatch.setattr(
        "claude_monitor.handoff.render_entry", lambda entry: f"# {entry.session_id}"
    )
    calls = []

    def capture(session_id, cwd, **kwargs):
        calls.append((session_id, cwd, kwargs))
        entry = _entry(
            session_id=session_id,
            project_path=cwd,
            project_slug="-tmp-proj",
        )
        entries.append(entry)
        return entry

    monkeypatch.setattr("claude_monitor.handoff.capture", capture)
    monkeypatch.setattr("claude_monitor.handoff.load_entry", lambda *a, **k: None)

    async with app_fixture.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await pilot.click("#load-old-transcript")
        await pilot.pause()
        picker = next(
            screen
            for screen in app_fixture.screen_stack
            if isinstance(screen, TranscriptPickerScreen)
        )
        picker.query_one("#transcript-query", Input).value = "historic"
        await pilot.click("#transcript-search")
        await pilot.pause()
        results = picker.query_one("#transcript-results", ListView)
        assert len(results.children) == 1
        results.focus()
        results.index = 0
        await pilot.press("enter")
        for _ in range(20):
            await pilot.pause()
            if calls:
                break
        await pilot.pause()

        panel = app_fixture.query_one(handoff_screen.HandoffPanel)
        assert panel.selected_entry.session_id == "historic-session"
        assert "# historic-session" in panel.query_one("#handoff-markdown", Markdown).source
        assert calls == [
            (
                "historic-session",
                "/tmp/proj",
                {
                    "transcript_path": str(project / "historic-session.jsonl"),
                    "reason": "manual",
                    "with_llm": False,
                    "settings": app_fixture.settings,
                    "event_stats": {},
                    "persist": False,
                },
            )
        ]


async def test_show_session_summary_missing_session_has_exact_empty_state(
    app_fixture, inject_message, monkeypatch
):
    """A pane with no saved entry does not fall back to another session."""
    from textual.widgets import Markdown, OptionList, TabbedContent

    from claude_monitor.screens.context_menu import PaneContextMenu

    monkeypatch.setattr(
        handoff_screen.HandoffPanel,
        "_load_entries",
        lambda self: [_entry(session_id="newest", title="Unrelated newest entry")],
    )
    app_fixture.settings.handoff_enabled = True

    async with app_fixture.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await inject_message(_make_permission_event(session_id="missing", cwd="/tmp/missing"))
        for _ in range(5):
            await pilot.pause()

        pane = app_fixture.panels["missing"]
        await pilot.click(pane, offset=(1, 0))
        await pilot.pause()

        menu = next(
            screen for screen in app_fixture.screen_stack if isinstance(screen, PaneContextMenu)
        )
        options = menu.query_one("#ctx-options", OptionList)
        assert options.get_option_at_index(1).id == "show_session_summary"
        options.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        tabbed = app_fixture.query_one("#tab-content", TabbedContent)
        panel = app_fixture.query_one(handoff_screen.HandoffPanel)
        assert tabbed.active == app_fixture.HANDOFF_TAB_ID
        assert panel._selected_session_id is None
        assert panel.query_one("#handoff-list", handoff_screen.ListView).index is None
        assert (
            "# No summary for this session" in panel.query_one("#handoff-markdown", Markdown).source
        )

        app_fixture.action_refresh_handoff()
        await pilot.pause()
        assert tabbed.active == app_fixture.HANDOFF_TAB_ID
        assert panel._selected_session_id is None
        assert panel.query_one("#handoff-list", handoff_screen.ListView).index is None
        assert (
            "# No summary for this session" in panel.query_one("#handoff-markdown", Markdown).source
        )


async def test_show_session_summary_loads_target_outside_browser_limit(
    app_fixture, inject_message, monkeypatch
):
    """A target outside the newest 50 entries is loaded by exact session ID."""
    from textual.widgets import Markdown, OptionList, TabbedContent

    from claude_monitor.screens.context_menu import PaneContextMenu

    entries = [_entry(session_id=f"entry-{index}") for index in range(50)]
    target = _entry(session_id="target", title="Target outside browser window")
    monkeypatch.setattr(handoff_screen.HandoffPanel, "_load_entries", lambda self: entries)
    load_entry = MagicMock(return_value=target)
    monkeypatch.setattr("claude_monitor.handoff.load_entry", load_entry)
    monkeypatch.setattr(
        "claude_monitor.handoff.render_entry", lambda entry: f"# {entry.session_id}"
    )
    app_fixture.settings.handoff_enabled = True

    async with app_fixture.run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        await inject_message(_make_permission_event(session_id="target", cwd="/tmp/target"))
        for _ in range(5):
            await pilot.pause()

        pane = app_fixture.panels["target"]
        await pilot.click(pane, offset=(1, 0))
        await pilot.pause()

        menu = next(
            screen for screen in app_fixture.screen_stack if isinstance(screen, PaneContextMenu)
        )
        options = menu.query_one("#ctx-options", OptionList)
        options.focus()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()

        tabbed = app_fixture.query_one("#tab-content", TabbedContent)
        panel = app_fixture.query_one(handoff_screen.HandoffPanel)
        assert tabbed.active == app_fixture.HANDOFF_TAB_ID
        assert panel._selected_session_id == "target"
        assert panel.selected_entry.session_id == "target"
        assert len(panel._entries) == 51
        assert "# target" in panel.query_one("#handoff-markdown", Markdown).source
        load_entry.assert_called_once_with("target")


def test_capture_action_dispatches_only_the_resolved_active_target(monkeypatch):
    from claude_monitor.tui_simple import SimpleTUI

    app = SimpleTUI()
    app.settings.handoff_enabled = True
    target = HandoffTarget("active", None, "working")
    monkeypatch.setattr(app, "_resolve_handoff_target", MagicMock(return_value=target))
    worker = MagicMock()
    monkeypatch.setattr(app, "_capture_handoff_worker", worker)

    app.action_capture_handoff()

    worker.assert_called_once_with(target)
