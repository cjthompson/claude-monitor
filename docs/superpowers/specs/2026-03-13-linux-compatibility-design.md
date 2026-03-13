# Linux Compatibility Design

## Goal

Make claude-monitor work on Linux (and any non-iTerm2 environment) by adding a simple TUI that reuses all existing widgets and logic, with iTerm2 detection at the entrypoint to select the right app.

## Detection

- Check `ITERM_SESSION_ID` environment variable at startup
- Present → launch full `MonitorApp` (existing iTerm2 version)
- Absent → launch `SimpleTUI` (new simple version)
- This means macOS users in Terminal.app/kitty also get the simple version
- Add `--simple` CLI flag to force simple mode (useful for testing/debugging)

## File Structure Changes

### New files

- **`claude_monitor/tui_common.py`** — shared widgets and logic extracted from `tui.py`
- **`claude_monitor/tui_simple.py`** — simple TUI app

### Modified files

- **`claude_monitor/tui.py`** — imports shared code from `tui_common.py`, keeps only iTerm2-specific logic
- **`claude_monitor/hook.py`** — support Claude session ID in pause mechanism (not just iTerm2 UUIDs)
- **`claude_monitor/usage.py`** — add `.env` file support for `CLAUDE_CODE_OAUTH_TOKEN`
- **`claude_monitor/install.py`** — detect Linux, skip iTerm2-specific messaging
- **`pyproject.toml`** — make `iterm2` dependency optional (macOS only), note `cairosvg` system deps for Linux

## Shared Code (`tui_common.py`)

Extracted from `tui.py`, used by both apps identically:

### Widgets
- **`SessionPanel`** — event log display, per-panel AUTO/MANUAL toggle, agent count, timers, worktree detection, all event formatting (newline collapse to `↵`, tool names, etc.)
- **`Dashboard`** — session summary, global stats, usage bar integration
- **`StatusBar`** — left (mode + usage) and right (version + clock) sections
- **`FixedWidthSparkline`** — sparkline widget used in panels
- **`HorizontalScrollBarRender` / `VerticalScrollBarRender`** — custom scrollbar renderers

### Modals and Screens
- **`PaneContextMenu`** — right-click context menu for panels
- **`ChoicesScreen`** — AskUserQuestion choices modal
- **`QuestionsScreen`** — AskUserQuestion text input modal

### Messages
- **`HookEvent`** — Textual message class for hook events

### Providers
- **`MonitorCommands`** — command palette provider (commands that apply to both apps)

### Utilities
- **`_safe_css_id` / `_safe_tab_css_id`** — CSS ID sanitization
- **`_format_ask_user_question_inline`** and related formatting helpers
- **Event parsing** — JSONL tail reading, event deserialization

### State Management
- Read/write `state.json`, pause/resume logic, stale session pruning

### Common CSS
- Shared styles for panels, dashboard, status bar

Both apps render identical panel content. The only difference is layout and session discovery.

### Event Routing — NOT Shared

`_resolve_panel()` has fundamentally different implementations:
- **Full version**: maps Claude session IDs → iTerm2 session IDs, creates fallback panels by mounting into `#layout-root`
- **Simple version**: maps Claude session IDs → tabs, creates new tabs on-the-fly

Each app implements its own `_resolve_panel()`.

## Simple TUI (`tui_simple.py`)

### Layout

Vertical split: `TabbedContent` on top, `Dashboard` below.

### Dashboard States

Three states, toggled by keybinds:

1. **Expanded** (default) — full dashboard panel below sessions (~30% height). Shows session list, per-session status, usage bar, global stats. Has minimize and to-tab buttons.
2. **Minimized** — single-line summary bar. Shows session count, active/paused counts, usage percentage. Sessions get more vertical space. `d` key to toggle.
3. **Tab mode** — dashboard detaches from bottom, becomes a tab in the `TabbedContent`. Full vertical space for whichever tab is active. `D` (shift+d) key to toggle.

Transitions: expanded ↔ minimized (`d`), any state ↔ tab mode (`D`). State remembered across the session.

### Session Discovery

- No iTerm2 API — sessions discovered from hook events as they arrive
- Key off Claude session ID from event data (not `_iterm_session_id`)
- New tab auto-created on first event from an unknown session
- Tab title shows working directory when available from events, falls back to truncated session ID
- No periodic layout polling needed (reactive discovery only)
- No cleanup of idle sessions (tabs persist until app restart)

### Keybindings

Same as full version where applicable: `a` (global auto/manual toggle), `m` (per-panel toggle), `s` (settings), `q` (quit), `ctrl+p` (command palette). Plus `d`/`D` for dashboard states.

## Hook Changes

### Per-session pause on Linux

Currently `state.json` stores `paused_sessions` as a list of iTerm2 session UUIDs. The hook checks `_iterm_session_id in paused_sessions`. On Linux, `_iterm_session_id` is always `None`, so per-session pause never matches.

Fix: the hook also checks `paused_claude_sessions` — a new field in `state.json` containing Claude session IDs. The simple TUI writes Claude session IDs to this field. The hook checks both lists:

```python
iterm_paused = iterm_sid in state.get("paused_sessions", [])
claude_paused = claude_session_id in state.get("paused_claude_sessions", [])
if iterm_paused or claude_paused:
    # deny permission
```

The full iTerm2 version continues to use `paused_sessions` as before.

### Auto-approve keystroke limitation

The hook's `PermissionRequest` handler (returns `{"result": "allow"}`) works cross-platform — this is the core auto-accept mechanism. The iTerm2 keystroke sending (`_send_keystroke_sync` for `permission_prompt` Notification events) is a secondary UX enhancement that dismisses the prompt in the terminal after the hook has already accepted. On Linux, this keystroke sending does not work. Known limitation — the prompt may briefly flash in the terminal before being auto-accepted. No workaround needed for initial release.

## OAuth Token on Linux

`usage.py` token resolution chain (in order):
1. Settings JSON (user-pasted in TUI)
2. `CLAUDE_CODE_OAUTH_TOKEN` environment variable (new, for Linux)
3. `.env` file in home directory (new, for Linux)
4. `CLAUDE_OAUTH_TOKEN` environment variable (existing fallback)
5. macOS Keychain via `security` CLI (existing, macOS only — skipped on Linux)

## Install Script

- Detect platform (`sys.platform`)
- Linux: skip iTerm2 references in output messages, same venv/symlink/hooks setup
- `iterm2` pip package: only install on macOS (conditional in `pyproject.toml` via platform marker)
- Note in output that `cairosvg` requires system packages on Linux (`libcairo2-dev` on Debian/Ubuntu, `cairo-devel` on Fedora) for the screenshot API endpoint

## HTTP API

No changes needed. Already platform-agnostic (Textual-based screenshots, JSON text export). Font detection via `fc-list` already works on Linux with fallback.

## Settings

- `~/.config/claude-monitor/config.json` follows XDG convention, works on both platforms
- `iterm_scope` setting: hidden from `SettingsScreen` when running in simple mode (irrelevant without iTerm2)
