#!/usr/bin/env python3
"""Claude Code hook for auto-accepting permission requests.

Handles both PermissionRequest and Notification events.
Logs all events to /tmp/claude-auto-accept/events.jsonl.
Auto-allows PermissionRequest unless paused.
"""

import json
import os
import sys
import time

from claude_monitor import SIGNAL_DIR, EVENTS_FILE, extract_iterm_session_id, read_state


def main():
    os.makedirs(SIGNAL_DIR, exist_ok=True)

    data = json.load(sys.stdin)
    event_name = data.get("hook_event_name", "")
    data["_timestamp"] = time.time()
    data["_tty"] = os.ttyname(sys.stderr.fileno()) if sys.stderr.isatty() else None
    raw = os.environ.get("ITERM_SESSION_ID", "")
    data["_iterm_session_id"] = extract_iterm_session_id(raw) or None

    # For PermissionRequest, check global and per-session pause state
    paused = False
    if event_name == "PermissionRequest":
        state = read_state()
        paused = state.get("global_paused", False)
        if not paused:
            iterm_sid = data["_iterm_session_id"]
            if iterm_sid:
                paused = iterm_sid in state.get("paused_sessions", [])
        if not paused:
            claude_sid = data.get("session_id", "")
            if claude_sid:
                paused = claude_sid in state.get("paused_claude_sessions", [])
        # Check if this tool is in the excluded list
        if not paused:
            tool_name = data.get("tool_name", "")
            excluded_tools = state.get("excluded_tools", [])
            if tool_name and tool_name in excluded_tools:
                paused = True
                data["_excluded_tool"] = True
        # Check per-pane AskUserQuestion pause
        if not paused:
            tool_name = data.get("tool_name", "")
            if tool_name == "AskUserQuestion":
                iterm_sid = data["_iterm_session_id"]
                if iterm_sid and iterm_sid in state.get("ask_paused_sessions", []):
                    paused = True
                    data["_excluded_tool"] = True
        # Check AskUserQuestion timeout: sleep to give user time to respond manually
        ask_timeout = 0
        if not paused:
            tool_name = data.get("tool_name", "")
            if tool_name == "AskUserQuestion":
                ask_timeout = state.get("ask_user_timeout", 0)
        data["_decision"] = "deferred" if paused else "allowed"
        if ask_timeout > 0:
            data["_decision"] = "timeout"
            data["_ask_timeout"] = ask_timeout

    # Log the event
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")

    # Only auto-allow for PermissionRequest when not paused
    if event_name != "PermissionRequest" or paused:
        return

    # AskUserQuestion with timeout: sleep then auto-allow
    if ask_timeout > 0:
        time.sleep(ask_timeout)
        # Log timeout completion so TUI can show it
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
