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

SIGNAL_DIR = "/tmp/claude-auto-accept"
EVENTS_FILE = os.path.join(SIGNAL_DIR, "events.jsonl")
PAUSE_FILE = os.path.join(SIGNAL_DIR, "paused")


def main():
    os.makedirs(SIGNAL_DIR, exist_ok=True)

    data = json.load(sys.stdin)
    event_name = data.get("hook_event_name", "")
    data["_timestamp"] = time.time()
    data["_tty"] = os.ttyname(sys.stderr.fileno()) if sys.stderr.isatty() else None
    # ITERM_SESSION_ID is "w0t0p2:UUID" — extract just the UUID
    raw = os.environ.get("ITERM_SESSION_ID", "")
    data["_iterm_session_id"] = raw.split(":", 1)[1] if ":" in raw else raw or None

    # Log the event
    with open(EVENTS_FILE, "a") as f:
        f.write(json.dumps(data) + "\n")

    # Only auto-allow for PermissionRequest
    if event_name != "PermissionRequest":
        return

    # Check if paused
    if os.path.exists(PAUSE_FILE):
        return  # exit 0 without output → normal permission dialog

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
