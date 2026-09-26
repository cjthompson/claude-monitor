# Manual Transcript Picker Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Let users select an older raw Claude transcript from the Hand-off tab and load a heuristic summary on demand.

**Architecture:** A transcript catalog enumerates direct `.jsonl` files under Claude project directories using metadata only. A modal picker searches and selects a candidate. A worker loads an existing saved entry or builds a temporary preview from that exact transcript without an LLM.

**Tech Stack:** Python 3.12+, Textual, pytest, Ruff.

---

### Task 1: Metadata-only transcript catalog

**Files:** Modify `claude_monitor/transcript.py`; test `tests/test_transcript.py`.

- [x] Write a failing test with several project folders and `.jsonl` files. Assert `list_transcripts(query, limit)` returns matching files newest first, applies the limit, and never reads file content. Include an invalid path/symlink case.
- [x] Run `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=. /Users/chris.thompson/dev/claude-monitor/.venv/bin/pytest -o addopts='' -p no:cacheprovider tests/test_transcript.py -q`; confirm the new test fails because the catalog is absent.
- [x] Add a small `TranscriptCandidate` value type and `list_transcripts` using directory scans and `stat` only. Add selected-file inspection that reads a bounded head to obtain a validated `cwd` and session ID.
- [x] Re-run the focused tests and confirm green.

### Task 2: Picker interaction

**Files:** Create `claude_monitor/screens/transcript_picker.py`; modify `claude_monitor/screens/handoff.py`; test `tests/test_handoff_tui.py`.

- [x] Write a failing mounted Textual test: open the picker from the Hand-off tab, search by project/session, select a transcript, and assert the exact historical session appears in the panel. Verify no LLM call and no capture of unselected files.
- [x] Run the targeted test and confirm failure because the picker is absent.
- [x] Implement a modal with search input, result list, and cancel action. On selection, validate the candidate and load a saved entry or request a temporary heuristic preview in a worker; display it through `refresh_entries(select_session_id=...)`. Report failures as a visible message.
- [x] Run focused tests, Ruff, and `git diff --check`. Commit the implementation with the design and plan.
