"""Hand-off panel widget for the claude-monitor TUI.

Renders the durable session hand-off entries written by
``claude_monitor.handoff``: a list of recent sessions on the left, the
selected entry rendered as markdown on the right.
"""

from __future__ import annotations

import logging
import time

from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.widgets import Button, ListItem, ListView, Markdown, Static

from claude_monitor import transcript
from claude_monitor.screens.transcript_picker import TranscriptPickerScreen

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

NO_SESSION_MESSAGE = (
    "# No summary for this session\n\n"
    "No hand-off summary has been captured for this exact session.\n\n"
    "Return to its pane and use **Capture Hand-off Now** or press `H` to capture one.\n"
)


class SelectedHandoff(Message):
    """Posted when the user selects an entry for targeted rotation."""

    def __init__(self, session_id: str) -> None:
        super().__init__()
        self.session_id = session_id


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
    HandoffPanel #handoff-browser {
        width: 40%;
        min-width: 24;
        border-right: solid $primary;
    }
    HandoffPanel #handoff-list {
        height: 1fr;
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
        self._selected_session_id: str | None = None
        self._targeted_session_id: str | None = None
        self._preview_entry = None

    @property
    def selected_entry(self):
        """Return the currently selected entry, if any."""
        if self._selected_session_id:
            return next(
                (entry for entry in self._entries if entry.session_id == self._selected_session_id),
                None,
            )
        try:
            index = self.query_one("#handoff-list", ListView).index
        except Exception:
            index = None
        if index is None or not 0 <= index < len(self._entries):
            return None
        return self._entries[index]

    def compose(self) -> ComposeResult:
        with Vertical(id="handoff-browser"):
            yield Button("Load old transcript", id="load-old-transcript")
            yield ListView(id="handoff-list")
        with VerticalScroll(id="handoff-detail"):
            yield Markdown(EMPTY_MESSAGE, id="handoff-markdown")

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "load-old-transcript":
            self.app.push_screen(TranscriptPickerScreen(), self._load_selected_transcript)

    def _load_selected_transcript(self, candidate: transcript.TranscriptCandidate | None) -> None:
        if candidate is None:
            return
        self._load_transcript_worker(candidate)

    @work(thread=True, exclusive=True, group="old-transcript", exit_on_error=False)
    def _load_transcript_worker(self, candidate: transcript.TranscriptCandidate) -> None:
        """Read only the selected transcript outside Textual's event loop."""
        from claude_monitor import handoff

        try:
            cwd = transcript.selected_transcript_cwd(candidate)
            entry = handoff.load_entry(candidate.session_id, slug=handoff.project_slug(cwd))
            if entry is None:
                entry = handoff.capture(
                    candidate.session_id,
                    cwd,
                    transcript_path=str(candidate.path),
                    reason="manual",
                    with_llm=False,
                    settings=getattr(self.app, "settings", None),
                    event_stats={},
                    persist=False,
                )
        except (OSError, ValueError) as exc:
            self.app.call_from_thread(
                self.app.notify, f"Could not load transcript: {exc}", severity="error"
            )
            return
        if entry is None:
            self.app.call_from_thread(
                self.app.notify,
                "Could not load a hand-off from this transcript.",
                severity="error",
            )
            return
        self.app.call_from_thread(self._show_loaded_transcript, entry)

    def _show_loaded_transcript(self, entry) -> None:
        self._preview_entry = entry
        self.refresh_entries(select_session_id=entry.session_id)

    def on_mount(self) -> None:
        self.refresh_entries()

    # ------------------------------------------------------------------
    # Data
    # ------------------------------------------------------------------

    def _load_entries(self) -> list:
        from claude_monitor import handoff

        return handoff.list_entries(limit=50)

    def refresh_entries(self, select_session_id: str | None = None) -> None:
        """Reload entries from the store and rebuild the list."""
        if select_session_id is not None:
            self._targeted_session_id = select_session_id
        previous_id = select_session_id or self._selected_session_id
        settings = getattr(self.app, "settings", None)
        enabled = getattr(settings, "handoff_enabled", False) if settings else False

        try:
            self._entries = self._load_entries()
        except Exception as e:  # noqa: BLE001 - a broken store must not kill the tab
            log.warning(f"HandoffPanel: failed to load entries: {e}")
            self._entries = []
        if self._preview_entry is not None and not any(
            entry.session_id == self._preview_entry.session_id for entry in self._entries
        ):
            self._entries.append(self._preview_entry)

        target_index = None
        if self._targeted_session_id:
            target_index = next(
                (
                    i
                    for i, entry in enumerate(self._entries)
                    if entry.session_id == self._targeted_session_id
                ),
                None,
            )
            if target_index is None:
                from claude_monitor import handoff

                try:
                    target = handoff.load_entry(self._targeted_session_id)
                except Exception as e:  # noqa: BLE001 - a broken store must not kill the tab
                    log.warning(f"HandoffPanel: failed to load targeted entry: {e}")
                    target = None
                if target is not None:
                    self._entries.append(target)
                    target_index = len(self._entries) - 1

        try:
            lv = self.query_one("#handoff-list", ListView)
        except Exception:
            return
        lv.clear()
        for entry in self._entries:
            lv.append(ListItem(Static(entry_label(entry), classes="handoff-item-label")))

        if self._targeted_session_id and target_index is None:
            self._selected_session_id = None
            lv.index = None
            self._set_markdown(NO_SESSION_MESSAGE)
        elif self._entries:
            selected_index = target_index
            if selected_index is None:
                selected_index = next(
                    (i for i, entry in enumerate(self._entries) if entry.session_id == previous_id),
                    0,
                )
            self._selected_session_id = self._entries[selected_index].session_id
            lv.index = selected_index
            self._show(self._entries[selected_index])
        else:
            self._selected_session_id = None
            lv.index = None
            self._set_markdown(EMPTY_MESSAGE if enabled else DISABLED_MESSAGE)

    def show_session_summary(self, session_id: str) -> None:
        """Show the saved hand-off for *session_id*, without another capture."""
        self._targeted_session_id = session_id
        self.refresh_entries(select_session_id=session_id)

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
        entry = self._entries[index]
        if self._targeted_session_id and entry.session_id != self._targeted_session_id:
            self._targeted_session_id = None
        self._selected_session_id = entry.session_id
        self._show(entry)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = getattr(event.list_view, "index", None)
        if index is None or not (0 <= index < len(self._entries)):
            return
        entry = self._entries[index]
        if self._targeted_session_id and entry.session_id != self._targeted_session_id:
            self._targeted_session_id = None
        self._selected_session_id = entry.session_id
        self.post_message(SelectedHandoff(self._selected_session_id))
