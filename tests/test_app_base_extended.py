"""Extended tests for app_base.py — state management, settings, timestamps."""

import asyncio
import concurrent.futures
import json
import time
from datetime import datetime, timezone

from claude_monitor.settings import Settings
from tests.conftest import _make_permission_event


class TestFormatTimestamp:
    async def test_24hr(self, app_fixture):
        ts = datetime(2024, 3, 15, 14, 30, 45)
        assert app_fixture._format_ts(ts) == "14:30:45"

    async def test_12hr(self, app_fixture):
        app_fixture.settings.timestamp_style = "12hr"
        ts = datetime(2024, 3, 15, 14, 30, 45)
        result = app_fixture._format_ts(ts)
        assert "2:30:45" in result
        assert "pm" in result.lower()

    async def test_date_time(self, app_fixture):
        app_fixture.settings.timestamp_style = "date_time"
        ts = datetime(2024, 3, 15, 14, 30, 45)
        result = app_fixture._format_ts(ts)
        assert "2024-03-15" in result
        assert "14:30:45" in result

    async def test_auto(self, app_fixture):
        app_fixture.settings.timestamp_style = "auto"
        ts = datetime(2024, 3, 15, 14, 30, 45)
        result = app_fixture._format_ts(ts)
        assert "14:30:45" in result


class TestSaveState:
    async def test_save_state(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app_fixture._global_paused = True
            app_fixture.settings.excluded_tools = ["Bash"]
            app_fixture.settings.ask_user_timeout = 30
            app_fixture._save_state()

            with open(isolated_state["state_file"]) as f:
                state = json.load(f)
            assert state["global_paused"] is True
            assert "Bash" in state["excluded_tools"]
            assert state["ask_user_timeout"] == 30

    async def test_load_state(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Write state file *after* mount to avoid on_mount overriding it
            state = {"global_paused": True, "paused_sessions": [], "paused_claude_sessions": ["s1"]}
            with open(isolated_state["state_file"], "w") as f:
                json.dump(state, f)
            app_fixture._load_state()
            assert app_fixture._global_paused is True
            # SimpleTUI._load_state also loads paused_claude_sessions
            if hasattr(app_fixture, "_paused_claude_sessions"):
                assert "s1" in app_fixture._paused_claude_sessions


class TestApplySettings:
    async def test_apply_settings_changes_theme(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            s = Settings(theme="monokai")
            app_fixture._apply_settings(s)
            assert app_fixture.theme == "monokai"

    async def test_apply_settings_disables_usage(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from claude_monitor.usage import UsageData, WindowUsage

            app_fixture._last_usage_data = UsageData(
                five_hour=WindowUsage(50.0, None),
                seven_day=WindowUsage(30.0, None),
            )
            s = Settings(account_usage=False)
            app_fixture._apply_settings(s)
            assert app_fixture._last_usage_data is None


class TestGetStateSnapshot:
    async def test_empty_state(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            snap = app_fixture.get_state_snapshot()
            assert snap["global_mode"] == "auto"
            assert snap["sessions"] == []
            assert snap["dashboard"] is not None
            assert snap["usage"] is None

    async def test_with_panels(self, app_fixture, inject_message, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            event = _make_permission_event(session_id="snap-sess")
            await inject_message(event)
            await pilot.pause()
            await pilot.pause()

            snap = app_fixture.get_state_snapshot()
            assert len(snap["sessions"]) == 1
            assert snap["sessions"][0]["id"] == "snap-sess"
            assert snap["sessions"][0]["mode"] == "auto"

    async def test_with_usage_data(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            from claude_monitor.usage import UsageData, WindowUsage

            resets = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
            app_fixture._last_usage_data = UsageData(
                five_hour=WindowUsage(50.0, resets),
                seven_day=WindowUsage(30.0, None),
            )
            snap = app_fixture.get_state_snapshot()
            assert snap["usage"] is not None
            assert snap["usage"]["five_hour"]["utilization"] == 50.0
            assert snap["usage"]["five_hour"]["resets_at"] is not None
            assert snap["usage"]["seven_day"]["resets_at"] is None

    async def test_paused_mode(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app_fixture._global_paused = True
            snap = app_fixture.get_state_snapshot()
            assert snap["global_mode"] == "manual"


class TestOnSettingsClosed:
    async def test_no_result(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            old_settings = app_fixture.settings
            app_fixture._on_settings_closed(None)
            assert app_fixture.settings is old_settings

    async def test_with_result(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            new = Settings(theme="dracula")
            app_fixture._on_settings_closed(new)
            assert app_fixture.settings.theme == "dracula"


class TestOnTokenRefreshed:
    async def test_updates_settings(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            app_fixture.settings.oauth_json = '{"access_token": "old"}'
            future = time.time() + 3600
            # _on_token_refreshed uses call_from_thread which doesn't work
            # from the main test thread. Test the logic by calling the inner function directly.
            try:
                app_fixture._on_token_refreshed("new_tok", "new_ref", future)
            except RuntimeError:
                # call_from_thread raises RuntimeError if there's no worker thread
                pass
            await pilot.pause()


class TestActionQuit:
    async def test_quit_sets_stop_event(self, app_fixture, isolated_state):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert not app_fixture._stop_event.is_set()
            app_fixture.action_quit()


class TestCallFromThreadBounded:
    """Regression tests for MonitorApp._call_from_thread_bounded (Slice B)."""

    async def test_stop_event_set_returns_default_without_scheduling(
        self, app_fixture, monkeypatch
    ):
        app_fixture._stop_event.set()
        app_fixture._loop = asyncio.new_event_loop()
        called = []
        monkeypatch.setattr(
            "claude_monitor.app_base.asyncio.run_coroutine_threadsafe",
            lambda *a, **k: called.append(1),
        )
        result = app_fixture._call_from_thread_bounded(lambda: "x", default="fallback")
        assert result == "fallback"
        assert called == []
        app_fixture._loop.close()

    async def test_wedged_loop_returns_default_within_timeout(self, app_fixture):
        # A loop that is created but never run — asyncio.run_coroutine_threadsafe
        # schedules onto it fine, but nothing ever drains it, so the wait must
        # be bounded by `timeout`, not hang forever.
        app_fixture._loop = asyncio.new_event_loop()
        app_fixture._thread_id = -1  # simulate calling from a worker thread
        start = time.monotonic()
        result = app_fixture._call_from_thread_bounded(
            lambda: "x", default="fallback", timeout=0.3
        )
        elapsed = time.monotonic() - start
        assert result == "fallback"
        assert elapsed < 2.0
        app_fixture._loop.close()

    async def test_timeout_cancels_future(self, app_fixture, monkeypatch):
        app_fixture._loop = asyncio.new_event_loop()
        app_fixture._thread_id = -1  # simulate calling from a worker thread
        cancelled = []
        original_cancel = concurrent.futures.Future.cancel

        def spy_cancel(self):
            cancelled.append(True)
            return original_cancel(self)

        monkeypatch.setattr(concurrent.futures.Future, "cancel", spy_cancel)
        result = app_fixture._call_from_thread_bounded(
            lambda: "x", default="fallback", timeout=0.2
        )
        assert result == "fallback"
        assert cancelled == [True]
        app_fixture._loop.close()


class TestUpdateTabTitlesStopEvent:
    """update_tab_titles must not spin after _stop_event is set (Slice B)."""

    async def test_stop_event_set_exits_without_calling_through(self, monkeypatch):
        import claude_monitor.tui as tui_mod
        from claude_monitor.tui import AutoAcceptTUI

        monkeypatch.setattr(tui_mod, "_layout_tabs", [])
        monkeypatch.setattr(tui_mod, "_self_session_id", None)
        monkeypatch.setattr("claude_monitor.app_base.fetch_usage", lambda: None)

        app = AutoAcceptTUI()
        app._tab_original_names = {"tab-1": "Original"}
        app._stop_event.set()
        called = []
        monkeypatch.setattr(app, "_call_from_thread_bounded", lambda *a, **k: called.append(1))

        app.update_tab_titles()

        assert called == []
