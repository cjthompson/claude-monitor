# Current-run Idle Hand-off Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Capture only sessions observed through fresh hook events during this monitor run after the configured idle interval.

**Architecture:** `_session_meta` is populated by fresh events only. The idle worker no longer rehydrates old events on startup. The worker checks that a tracked session still has a panel before capturing or rotating; SessionEnd continues to mark sessions closed.

**Tech Stack:** Python 3.12+, Textual, pytest, Ruff.

---

### Task 1: Exclude replayed events and historical startup state

**Files:** Modify `claude_monitor/app_base.py`; test `tests/test_handoff_tui.py`.

- [x] Add a regression test that records an event with `_replay=True`, starts polling, and asserts no session becomes capture eligible and no capture occurs. Use a fake event file with an old `SessionStart` to prove startup does not rehydrate it.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /Users/chris.thompson/dev/claude-monitor/.venv/bin/pytest -o addopts='' -p no:cacheprovider tests/test_handoff_tui.py -q`; confirm the new test fails for the current replay/rehydration behavior.
- [x] In `_record_session_meta`, return for `_replay` events. In `_start_handoff_polling`, remove `_rehydrate_handoff_meta()` and its obsolete helper; keep `self.poll_handoff()`.
- [x] Re-run the focused tests and confirm green.

### Task 2: Check open panels at the automatic boundary

**Files:** Modify `claude_monitor/app_base.py`; test `tests/test_handoff_tui.py`.

- [x] Add a test with two fresh observed sessions: one with an open panel, one whose panel was removed. At the idle deadline assert capture occurs only for the open session. Add a case for the same session receiving new activity and capturing again after a new interval.
- [x] Run the focused test and confirm the closed-panel case fails.
- [x] In `_poll_handoff_once`, skip metadata without a matching panel and discard it after a one-minute mount grace period. Discard ended metadata. The full TUI keys panels by iTerm pane ID, while simple mode keys by Claude session ID; accept either `sid` or `meta['iterm_session_id']` in `self.panels`.
- [x] Run focused tests, Ruff, and `git diff --check`. Commit the implementation with the design and plan.
