# VHS UI Tests — Coverage-Integrated

## Problem

Unit tests cover 83% of the codebase but can't reach stateful UI code in `tui_simple.py` (74%), `app_base.py` (71%), `context_menu.py` (36%), and `settings.py` modal callbacks (87%). These paths require a running Textual app with mounted widgets in specific states.

## Solution

Use [VHS](https://github.com/charmbracelet/vhs) to script terminal interactions against the real TUI, running under `coverage run --parallel-mode` so results combine with pytest coverage.

## Prerequisites

- VHS installed (`brew install vhs`)
- `.venv` with package installed in editable mode
- No running TUI instance on the same `SIGNAL_DIR`

## Source Changes Required

### 1. Make `SIGNAL_DIR` overridable via env var

In `claude_monitor/__init__.py`, change:
```python
SIGNAL_DIR = "/tmp/claude-auto-accept"
```
to:
```python
SIGNAL_DIR = os.environ.get("CLAUDE_MONITOR_SIGNAL_DIR", "/tmp/claude-auto-accept")
```

All derived paths (`EVENTS_FILE`, `STATE_FILE`, etc.) already reference `SIGNAL_DIR`, so they'll pick up the override automatically.

### 2. Add `__main__` guard to `tui_simple.py`

Append to `claude_monitor/tui_simple.py`:
```python
if __name__ == "__main__":
    main()
```

This allows `coverage run --parallel-mode -m claude_monitor.tui_simple` to work.

### 3. Event injection after startup

`watch_events` in `app_base.py` seeks to end of file on open (`f.seek(0, 2)`), so pre-written events are invisible. The test harness must **append events after the TUI starts**. Each tape uses VHS `Type` to launch the TUI, waits for mount, then a background process appends fixture events to `$SIGNAL_DIR/events.jsonl`.

## Structure

```
tests/
  ui/
    tapes/                    # .tape files (one per UI workflow)
      basic_lifecycle.tape
      dashboard_modes.tape
      settings_modal.tape
      pause_toggle.tape
      tab_navigation.tape
      help_modal.tape
      command_palette.tape
    fixtures/
      events.jsonl            # Template event stream (appended after TUI starts)
      state.json              # Initial state (not paused)
    inject_events.sh          # Appends fixture events to SIGNAL_DIR/events.jsonl
    run_ui_tests.sh           # Orchestrator script
test-ui.sh                    # Top-level runner (analogous to test.sh)
```

## How It Works

### Event Fixture

A template `events.jsonl` with ~20 events covering all event types:
- PermissionRequest: Bash, Edit, Write, WebFetch, AskUserQuestion (allowed, deferred, timeout)
- Notification: idle_prompt, permission_prompt, ask_timeout_complete
- SubagentStart / SubagentStop
- PostToolUse: AskUserQuestion with answers

Events use distinct `session_id` values so the TUI creates multiple session panels/tabs.

### Event Injection

`inject_events.sh` appends fixture events line-by-line to `$SIGNAL_DIR/events.jsonl` with small delays between lines so the TUI processes them incrementally. Called after the TUI has started and rendered.

### Tape Execution

Each tape:
1. Sets env: `CLAUDE_MONITOR_SIGNAL_DIR` → a temp dir per tape
2. Initializes `state.json` in the temp dir
3. Launches: `coverage run --parallel-mode -m claude_monitor.tui_simple`
4. Waits for the TUI to render (~3s)
5. Runs `inject_events.sh` in the background to append events
6. Waits for events to render (~2s)
7. Sends keystrokes to exercise specific paths
8. Sends `q` to quit cleanly

### Coverage Combination

`run_ui_tests.sh`:
1. Cleans stale `.coverage.*` files
2. Runs each tape via `vhs <tape>`
3. Runs `coverage combine` to merge parallel coverage data
4. Runs `coverage report --show-missing` for the UI test contribution
5. Exit code reflects whether all tapes completed without error

### Integration with Existing Tests

- `./test.sh` — unchanged, runs pytest unit/integration tests only
- `./test-ui.sh` — runs VHS UI tests, combines coverage
- To get combined total: run both, then `coverage combine && coverage report`

## Tapes

### basic_lifecycle.tape
**Target:** App startup, event rendering, clean quit.
**Keystrokes:** (wait 3s) → (inject events) → (wait 2s) → `q`
**Exercises:** `tui_simple.py` compose/mount, `app_base.py` event loading, panel creation

### dashboard_modes.tape
**Target:** Dashboard pane/tab toggle, resize, minimize.
**Keystrokes:** (wait, inject) → `D` (to tab) → (wait) → `D` (back to pane) → `d` (minimize) → `d` (restore) → `=` (grow) → `=` → `-` (shrink) → `-` → `q`
**Exercises:** `tui_simple.py` action_toggle_dashboard_tab, action_toggle_dashboard, _apply_dashboard_height, _update_arrow

### settings_modal.tape
**Target:** Settings screen open, navigation, close.
**Keystrokes:** (wait, inject) → `s` → (Tab through fields) → `Escape` → `q`
**Exercises:** `settings.py` SettingsScreen mount/dismiss callbacks, `app_base.py` on_settings_closed

### pause_toggle.tape
**Target:** Global and per-pane pause toggling.
**Keystrokes:** (wait, inject) → `a` (all manual) → `a` (all auto) → (click/focus a session panel) → `m` (per-pane) → `m` (toggle back) → `q`
**Notes:** `m` is bound on `SessionPanel`, so a panel must have focus before sending `m`. Use VHS `Type` or mouse click to focus.
**Exercises:** `app_base.py` action_toggle_pause, `tui_simple.py` per-pane pause, `_save_state`

### tab_navigation.tape
**Target:** Tab switching, tab closing.
**Keystrokes:** (wait, inject) → `]` (next tab) → `]` → `[` (prev tab) → `x` (close tab) → `q`
**Exercises:** `tui_simple.py` action_next_tab, action_prev_tab, action_close_tab

### help_modal.tape
**Target:** Help modal open/close.
**Keystrokes:** (wait) → `?` → (wait 1s) → `Escape` → `q`
**Exercises:** `screens/help.py` HelpScreen mount/dismiss

### command_palette.tape
**Target:** Command palette open, search, dismiss.
**Keystrokes:** (wait) → `Ctrl+p` → (type "dash") → `Escape` → `q`
**Exercises:** `commands.py` MonitorCommands search, `tui_simple.py` command dispatch

## Coverage Targets

These tapes should cover most of the 190 missed lines in `tui_simple.py` (119 miss) + `app_base.py` (71 miss), plus the 30 lines in `context_menu.py` and remaining `settings.py` modal lines. Combined with existing pytest coverage (83%), target is 88-90%.
