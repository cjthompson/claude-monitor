"""Message classes for claude-monitor TUI event routing."""

from textual.message import Message


class HookEvent(Message):
    """Posted when a new hook event is parsed from the events JSONL file."""
    def __init__(self, data: dict) -> None:
        super().__init__()
        self.data = data
