"""claude-monitor: shared constants and utilities."""

__version__ = "1.0.20"

import json
import os

SIGNAL_DIR = "/tmp/claude-auto-accept"
EVENTS_FILE = os.path.join(SIGNAL_DIR, "events.jsonl")
STATE_FILE = os.path.join(SIGNAL_DIR, "state.json")
LOG_FILE = os.path.join(SIGNAL_DIR, "tui-debug.log")
API_PORT = 17233
API_PORT_FILE = os.path.join(SIGNAL_DIR, "api-port")

_DEFAULT_STATE = {"global_paused": False, "paused_sessions": [], "paused_claude_sessions": [], "excluded_tools": [], "ask_user_timeout": 0}


def read_state() -> dict:
    """Read the shared state file. Returns defaults if missing/corrupt."""
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return dict(_DEFAULT_STATE)


def extract_iterm_session_id(raw: str) -> str:
    """Extract UUID from iTerm2 session ID format 'w0t0p2:UUID'."""
    return raw.split(":", 1)[1] if ":" in raw else raw


def fmt_duration(seconds: float, compact: bool = False) -> str:
    """Format a duration in seconds as a human-readable string.

    If *compact* is True, omit seconds for values >= 60s (e.g. "3m" instead of "3m05s").
    """
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    if s < 3600:
        if compact:
            return f"{s // 60}m"
        return f"{s // 60}m{s % 60:02d}s"
    h = s // 3600
    m = (s % 3600) // 60
    return f"{h}h{m:02d}m"
