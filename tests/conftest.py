"""Shared fixtures for claude-monitor integration tests."""

import json
import os
import time

import pytest


@pytest.fixture
def isolated_state(tmp_path, monkeypatch):
    """Patch all module-level path constants to use a temp directory.

    CRITICAL: Python copies module-level imports at import time, so we must
    patch in EVERY module that does `from claude_monitor import EVENTS_FILE` etc.
    """
    signal_dir = str(tmp_path / "signals")
    os.makedirs(signal_dir, exist_ok=True)

    events_file = os.path.join(signal_dir, "events.jsonl")
    state_file = os.path.join(signal_dir, "state.json")
    log_file = os.path.join(signal_dir, "tui-debug.log")

    config_dir = str(tmp_path / "config")
    config_file = os.path.join(config_dir, "config.json")
    os.makedirs(config_dir, exist_ok=True)

    # Patch the canonical module
    import claude_monitor
    monkeypatch.setattr(claude_monitor, "SIGNAL_DIR", signal_dir)
    monkeypatch.setattr(claude_monitor, "EVENTS_FILE", events_file)
    monkeypatch.setattr(claude_monitor, "STATE_FILE", state_file)
    monkeypatch.setattr(claude_monitor, "LOG_FILE", log_file)

    # Patch in every module that imports these at the top level
    import claude_monitor.tui_simple as tui_simple
    monkeypatch.setattr(tui_simple, "SIGNAL_DIR", signal_dir)
    monkeypatch.setattr(tui_simple, "STATE_FILE", state_file)
    monkeypatch.setattr(tui_simple, "LOG_FILE", log_file)

    import claude_monitor.app_base as app_base
    monkeypatch.setattr(app_base, "SIGNAL_DIR", signal_dir)
    monkeypatch.setattr(app_base, "EVENTS_FILE", events_file)
    monkeypatch.setattr(app_base, "STATE_FILE", state_file)

    import claude_monitor.hook as hook
    monkeypatch.setattr(hook, "SIGNAL_DIR", signal_dir)
    monkeypatch.setattr(hook, "EVENTS_FILE", events_file)

    import claude_monitor.settings as settings_mod
    monkeypatch.setattr(settings_mod, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(settings_mod, "CONFIG_FILE", config_file)

    # Patch web module (the new unified HTTP+WebSocket server)
    import claude_monitor.web as web_mod
    monkeypatch.setattr(web_mod, "EVENTS_FILE", events_file)
    monkeypatch.setattr(web_mod, "STATE_FILE", state_file)

    return {
        "signal_dir": signal_dir,
        "events_file": events_file,
        "state_file": state_file,
        "log_file": log_file,
        "config_dir": config_dir,
        "config_file": config_file,
        "tmp_path": tmp_path,
    }


@pytest.fixture
def inject_event(isolated_state):
    """Return a callable that appends a JSON event to events.jsonl."""
    events_file = isolated_state["events_file"]

    def _inject(data: dict):
        with open(events_file, "a") as f:
            f.write(json.dumps(data) + "\n")

    return _inject


def _make_permission_event(
    tool_name="Bash",
    session_id="test-session-1234",
    cwd="/tmp/test-project",
    tool_input=None,
    decision="allowed",
    timestamp=None,
    iterm_session_id=None,
):
    """Factory for PermissionRequest events."""
    event = {
        "hook_event_name": "PermissionRequest",
        "tool_name": tool_name,
        "session_id": session_id,
        "cwd": cwd,
        "tool_input": tool_input or {},
        "_decision": decision,
        "_timestamp": timestamp or time.time(),
        "_tty": "/dev/ttys001",
        "_iterm_session_id": iterm_session_id or "fake-iterm-uuid",
    }
    return event


def _make_notification_event(
    notification_type="idle_prompt",
    session_id="test-session-1234",
    message="Session is idle",
    timestamp=None,
    iterm_session_id=None,
):
    """Factory for Notification events."""
    return {
        "hook_event_name": "Notification",
        "notification_type": notification_type,
        "session_id": session_id,
        "message": message,
        "_timestamp": timestamp or time.time(),
        "_tty": "/dev/ttys001",
        "_iterm_session_id": iterm_session_id or "fake-iterm-uuid",
    }


def _make_subagent_event(
    start_or_stop="start",
    agent_id="agent-abc-123",
    session_id="test-session-1234",
    agent_type="general_purpose",
    timestamp=None,
    iterm_session_id=None,
):
    """Factory for SubagentStart/Stop events."""
    event_name = "SubagentStart" if start_or_stop == "start" else "SubagentStop"
    return {
        "hook_event_name": event_name,
        "agent_id": agent_id,
        "agent_type": agent_type,
        "session_id": session_id,
        "_timestamp": timestamp or time.time(),
        "_tty": "/dev/ttys001",
        "_iterm_session_id": iterm_session_id or "fake-iterm-uuid",
    }


# Make factories available as fixtures too
@pytest.fixture
def make_permission_event():
    return _make_permission_event


@pytest.fixture
def make_notification_event():
    return _make_notification_event


@pytest.fixture
def make_subagent_event():
    return _make_subagent_event


@pytest.fixture
def app_fixture(isolated_state, monkeypatch):
    """Create a SimpleTUI instance ready for async with app.run_test().

    Background threads (serve_api, watch_events, poll_usage) are all patched to
    no-ops to prevent asyncio executor teardown hangs. Tests that need event
    routing use the inject_message fixture instead of inject_event.
    """
    monkeypatch.setattr("claude_monitor.app_base.fetch_usage", lambda: None)
    monkeypatch.setattr("claude_monitor.app_base.MonitorApp.serve_api", lambda self: None)
    monkeypatch.setattr("claude_monitor.app_base.MonitorApp.watch_events", lambda self: None)
    monkeypatch.setattr("claude_monitor.app_base.MonitorApp.poll_usage", lambda self: None)
    # SimpleTUI overrides watch_events with its own @work(thread=True) method, so
    # patching MonitorApp.watch_events alone is not enough — Python MRO finds
    # SimpleTUI.watch_events first. Without this patch, the thread starts, ignores
    # the MonitorApp patch, and blocks pytest from exiting if _stop_event is never set.
    monkeypatch.setattr("claude_monitor.tui_simple.SimpleTUI.watch_events", lambda self: None)

    from claude_monitor.tui_simple import SimpleTUI
    app = SimpleTUI()
    yield app
    app._stop_event.set()  # safety net: unblock any thread that slipped through


@pytest.fixture
def inject_message(app_fixture):
    """Post a HookEvent directly to the app, bypassing the file watcher.

    Faster than inject_event for tests that don't test the file-tailing logic.
    Use this when app_fixture is used (watch_events is patched out).
    """
    from claude_monitor.messages import HookEvent

    async def _post(data: dict):
        app_fixture.post_message(HookEvent(data))

    return _post


@pytest.fixture
async def app_fixture_with_api(isolated_state, monkeypatch):
    """Like app_fixture but with the API server enabled."""
    monkeypatch.setattr("claude_monitor.app_base.fetch_usage", lambda: None)
    monkeypatch.setattr("claude_monitor.app_base.MonitorApp.watch_events", lambda self: None)
    monkeypatch.setattr("claude_monitor.app_base.MonitorApp.poll_usage", lambda self: None)

    from claude_monitor.tui_simple import SimpleTUI
    app = SimpleTUI()
    yield app

    import asyncio
    app._stop_event.set()
    await asyncio.sleep(0.5)
