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
        data["_decision"] = "deferred" if paused else "allowed"

    # Log the event
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")

    # Only auto-allow for PermissionRequest when not paused
    if event_name != "PermissionRequest" or paused:
        return

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
