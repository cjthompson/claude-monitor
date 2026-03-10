# Tasks

---

## task: Add global keybindings to switch to next/prev tab
**ID:** #003 | **Date:** 2026-03-10 00:45 | **Priority:** medium | **Tags:** #ui #keybindings
**Status:** completed (2026-03-10 01:15)

### Requirements
- Add a next-tab keybinding (`]`) to the app-level BINDINGS that switches to the next tab
- Add a prev-tab keybinding (`[`) that switches to the previous tab
- Bindings should be no-ops when only one tab is present (single-tab mode has no TabbedContent)
- Add the bindings to the Footer so they appear in the keybinding hint bar

---

## task: Trim tab labels to fit all tabs visible; investigate tab bar wrapping
**ID:** #002 | **Date:** 2026-03-10 00:00 | **Priority:** medium | **Tags:** #ui #tui
**Status:** completed (2026-03-10 00:30)

### Requirements
- Trim tab labels dynamically so all tabs fit within the available tab bar width
- Minimum label length of 6 characters — never truncate shorter than 6
- Investigate whether Textual's Tabs widget supports multi-line/wrapping tab bar; implement if supported, otherwise document the limitation

---

## task: Document that TUI restart must use venv Python for iterm2 module
**ID:** #001 | **Date:** 2026-03-10 00:00 | **Priority:** medium | **Tags:** #documentation #debugging
**Status:** completed (2026-03-10 01:35)

### Requirements
- Add a section to CLAUDE.md under "How to restart the TUI" explaining that the Python script must use the venv's Python interpreter (not system Python)
- Document why: the iterm2 module is only installed in the venv, not in system Python
- Provide a corrected example showing the use of `.venv/bin/python3` instead of system `python3`
- Add a note warning against the common mistake of using system Python, which will fail with `ModuleNotFoundError: No module named 'iterm2'`

---

## task: AskUserQuestion countdown disappears on pane update — use modal overlay
**Date:** 2026-03-09 00:00 | **Priority:** medium | **Tags:** #ui #tui
**Status:** completed (2026-03-09 23:02)

### Requirements
- Make the AskUserQuestion countdown display as a modal overlay on top of the pane, rather than inline content that gets replaced on update
- The modal should have a higher z-index so it renders on top of pane content
- Pane updates should continue to appear underneath the modal while the countdown is active
- When the countdown expires or the user responds, the modal should dismiss and reveal the updated pane content beneath
