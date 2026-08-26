"""Hand-off panel widget for the claude-monitor TUI.

Renders the durable session hand-off entries written by
``claude_monitor.handoff``: a list of recent sessions on the left, the
selected entry rendered as markdown on the right.
"""

from __future__ import annotations

import logging
import time

from textual.app import ComposeResult
from textual.containers import Horizontal, VerticalScroll
from textual.widgets import ListItem, ListView, Markdown, Static

log = logging.getLogger(__name__)

EMPTY_MESSAGE = (
    "# No hand-offs yet\n\n"
    "Nothing has been captured. Enable **Hand-off summaries** in Settings "
    "(`s`), or capture the current session on demand from the command "
    "palette (*Capture Hand-off Now*).\n"
)

DISABLED_MESSAGE = (
    "# Hand-off summaries are off\n\n"
    "Turn on **handoff_enabled** in Settings (`s`) to start recording where "
    "each session left off.\n"
)


def _relative(ts: float | None) -> str:
    """Render *ts* as a short human-readable age."""
    if not ts:
        return "?"
    delta = max(0.0, time.time() - ts)
    if delta < 90:
        return "just now"
    minutes = delta / 60
    if minutes < 90:
        return f"{int(minutes)}m ago"
    hours = minutes / 60
    if hours < 36:
        return f"{int(hours)}h ago"
    return f"{int(hours / 24)}d ago"


def entry_label(entry) -> str:
    """One-line list label for *entry*."""
    import os

    title = entry.title or entry.session_id[:8] or "session"
    project = os.path.basename(entry.project_path.rstrip("/")) or entry.project_path
    when = _relative(entry.ended_at or entry.started_at)
    return f"{title} — {project} — {when}"


class HandoffPanel(Horizontal):
    """Two-column browser over the hand-off store."""

    DEFAULT_CSS = """
    HandoffPanel {
        height: 1fr;
    }
    HandoffPanel #handoff-list {
        width: 40%;
        min-width: 24;
        border-right: solid $primary;
    }
    HandoffPanel #handoff-detail {
        width: 1fr;
        padding: 0 1;
    }
    HandoffPanel .handoff-item-label {
        padding: 0 1;
    }
    """

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._entries: list = []

    def compose(self) -> ComposeResult:
        yield ListView(id="handoff-list")
        with VerticalScroll(id="handoff-detail"):
            yield Markdown(EMPTY_MESSAGE, id="handoff-markdown")

    def on_mount(self) -> None:
        self.refresh_entries()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_entries(self) -> list:
        from claude_monitor import handoff

        return handoff.list_entries(limit=50)

    def refresh_entries(self) -> None:
        """Reload entries from the store and rebuild the list."""
        settings = getattr(self.app, "settings", None)
        enabled = getattr(settings, "handoff_enabled", False) if settings else False

        try:
            self._entries = self._load_entries()
        except Exception as e:  # noqa: BLE001 - a broken store must not kill the tab
            log.warning(f"HandoffPanel: failed to load entries: {e}")
            self._entries = []

        try:
            lv = self.query_one("#handoff-list", ListView)
        except Exception:
            return
        lv.clear()
        for entry in self._entries:
            lv.append(ListItem(Static(entry_label(entry), classes="handoff-item-label")))

        if self._entries:
            self._show(self._entries[0])
        else:
            self._set_markdown(EMPTY_MESSAGE if enabled else DISABLED_MESSAGE)

    def _set_markdown(self, text: str) -> None:
        try:
            self.query_one("#handoff-markdown", Markdown).update(text)
        except Exception as e:  # noqa: BLE001
            log.debug(f"HandoffPanel: markdown update failed: {e}")

    def _show(self, entry) -> None:
        from claude_monitor import handoff

        try:
            self._set_markdown(handoff.render_entry(entry))
        except Exception as e:  # noqa: BLE001
            log.warning(f"HandoffPanel: failed to render entry: {e}")
            self._set_markdown("# Could not render this entry\n")

    # ------------------------------------------------------------------
    # Events
    # ------------------------------------------------------------------

    def on_list_view_highlighted(self, event: ListView.Highlighted) -> None:
        index = getattr(event.list_view, "index", None)
        if index is None or not (0 <= index < len(self._entries)):
            return
        self._show(self._entries[index])
