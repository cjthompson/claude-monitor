#!/usr/bin/env python3
"""Claude Code hook for auto-accepting permission requests.

Handles both PermissionRequest and Notification events.
Logs all events to /tmp/claude-auto-accept/events.jsonl.
Auto-allows PermissionRequest unless paused.
"""

import json
import os
import socket
import sys
import time

from claude_monitor import SIGNAL_DIR, EVENTS_FILE, API_PORT_FILE, extract_iterm_session_id, read_state


def _tui_is_running() -> bool:
    """Check if the TUI is running by attempting a TCP connect to its API port."""
    try:
        with open(API_PORT_FILE) as f:
            port = int(f.read().strip())
    except (FileNotFoundError, ValueError):
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def decide_permission(state: dict, event: dict) -> tuple[str, int]:
    """Determine the permission decision for a PermissionRequest event.

    Returns a tuple of (decision, ask_timeout) where decision is one of:
      - "allowed"  — auto-accept
      - "deferred" — paused (manual mode)
      - "timeout"  — AskUserQuestion with a configured timeout

    Pure function: reads from ``state`` and ``event`` only, no I/O.
    """
    iterm_sid = event.get("_iterm_session_id")
    claude_sid = event.get("session_id", "")
    tool_name = event.get("tool_name", "")

    # Global pause
    if state.get("global_paused", False):
        return "deferred", 0

    # Per-iTerm-session pause
    if iterm_sid and iterm_sid in state.get("paused_sessions", []):
        return "deferred", 0

    # Per-Claude-session pause
    if claude_sid and claude_sid in state.get("paused_claude_sessions", []):
        return "deferred", 0

    # Excluded tools
    if tool_name and tool_name in state.get("excluded_tools", []):
        return "deferred", 0

    # Per-pane AskUserQuestion pause
    if tool_name == "AskUserQuestion":
        if iterm_sid and iterm_sid in state.get("ask_paused_sessions", []):
            return "deferred", 0
        ask_timeout = state.get("ask_user_timeout", 0)
        if ask_timeout > 0:
            return "timeout", ask_timeout

    return "allowed", 0


def main():
    os.makedirs(SIGNAL_DIR, exist_ok=True)

    data = json.load(sys.stdin)
    event_name = data.get("hook_event_name", "")
    data["_timestamp"] = time.time()
    data["_tty"] = os.ttyname(sys.stderr.fileno()) if sys.stderr.isatty() else None
    raw = os.environ.get("ITERM_SESSION_ID", "")
    data["_iterm_session_id"] = extract_iterm_session_id(raw) or None

    ask_timeout = 0

    tui_running = _tui_is_running()

    if event_name == "PermissionRequest":
        if not tui_running:
            # TUI not running — don't auto-accept, let the user decide
            data["_decision"] = "no_monitor"
        else:
            state = read_state()
            decision, ask_timeout = decide_permission(state, data)
            data["_decision"] = decision
            if decision == "deferred":
                tool_name = data.get("tool_name", "")
                excluded = state.get("excluded_tools", [])
                if tool_name and tool_name in excluded:
                    data["_excluded_tool"] = True
            elif decision == "timeout":
                data["_ask_timeout"] = ask_timeout

    # Log the event
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")

    # Only auto-allow for PermissionRequest when monitor is running and not paused
    if event_name != "PermissionRequest" or data.get("_decision") in ("deferred", "no_monitor"):
        return

    # AskUserQuestion with timeout: sleep then auto-allow
    if ask_timeout > 0:
        time.sleep(ask_timeout)
        completion = {
            "_timestamp": time.time(),
            "_iterm_session_id": data.get("_iterm_session_id"),
            "hook_event_name": "Notification",
            "notification_type": "ask_timeout_complete",
            "message": f"AskUserQuestion auto-accepted after {ask_timeout}s",
            "tool_name": "AskUserQuestion",
            "_timeout_origin": data["_timestamp"],
        }
        with open(EVENTS_FILE, "a") as f:
            f.write(json.dumps(completion) + "\n")

    # Auto-allow
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "allow"},
            }
        },
        sys.stdout,
    )


if __name__ == "__main__":
    main()
