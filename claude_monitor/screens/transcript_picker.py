"""On-demand browser for historical Claude transcript files."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, Input, ListItem, ListView, Static

from claude_monitor import transcript


class TranscriptPickerScreen(ModalScreen[transcript.TranscriptCandidate | None]):
    """Search metadata and return exactly one user-selected transcript."""

    DEFAULT_CSS = """
    TranscriptPickerScreen {
        align: center middle;
    }
    TranscriptPickerScreen #transcript-dialog {
        width: 90%;
        max-width: 110;
        height: 75%;
        background: $surface;
        border: thick $primary;
        padding: 1 2;
    }
    TranscriptPickerScreen #transcript-results {
        height: 1fr;
    }
    TranscriptPickerScreen #transcript-actions {
        height: 3;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self._candidates: list[transcript.TranscriptCandidate] = []

    def compose(self) -> ComposeResult:
        with Vertical(id="transcript-dialog"):
            yield Static("Load old Claude transcript")
            yield Input(placeholder="Project or session ID", id="transcript-query")
            with Horizontal(id="transcript-actions"):
                yield Button("Search", id="transcript-search", variant="primary")
                yield Button("Cancel", id="transcript-cancel")
            yield Static("", id="transcript-status")
            yield ListView(id="transcript-results")

    def on_mount(self) -> None:
        self._search()
        self.query_one("#transcript-query", Input).focus()

    def _search(self) -> None:
        query = self.query_one("#transcript-query", Input).value
        self._candidates = transcript.list_transcripts(query)
        results = self.query_one("#transcript-results", ListView)
        results.clear()
        for candidate in self._candidates:
            results.append(ListItem(Static(f"{candidate.project_slug} — {candidate.session_id}")))
        status = self.query_one("#transcript-status", Static)
        status.update(
            f"{len(self._candidates)} matching transcripts (newest first)"
            if self._candidates
            else "No matching transcripts"
        )

    def on_input_submitted(self, _event: Input.Submitted) -> None:
        self._search()

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "transcript-search":
            self._search()
        elif event.button.id == "transcript-cancel":
            self.dismiss(None)

    def on_list_view_selected(self, event: ListView.Selected) -> None:
        index = event.list_view.index
        if index is not None and 0 <= index < len(self._candidates):
            self.dismiss(self._candidates[index])

    def action_cancel(self) -> None:
        self.dismiss(None)
