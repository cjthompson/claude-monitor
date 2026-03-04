# claude-monitor

A Textual TUI that monitors and auto-accepts Claude Code permission prompts across iTerm2 panes.

## Versioning

Version is in `claude_monitor/__init__.py` (`__version__`) and displayed in the TUI status bar.

- **New session**: Bump the PATCH version and add `-beta.1` (e.g. `1.0.0` → `1.0.1-beta.1`).
- **Every restart with new changes**: Increment the beta number (e.g. `-beta.1` → `-beta.2`).
- **End of session**: Remove the `-beta.x` suffix, leaving just `x.x.x` (e.g. `1.0.1-beta.5` → `1.0.1`).
- Keep `pyproject.toml` version in sync when removing the beta suffix.

## Restarting the TUI

Use the iTerm2 Python API to send `q` to the TUI's pane (use `run.sh` wrapper for auto-restart).

To find the TUI's session ID, list all sessions and look for `run.sh` or `Python` job:
```python
import iterm2
async def main(connection):
    app = await iterm2.async_get_app(connection)
    for window in app.terminal_windows:
        for tab in window.tabs:
            for session in tab.sessions:
                name = await session.async_get_variable('name') or ''
                job = await session.async_get_variable('jobName') or ''
                print(f'{session.session_id}  name={name!r}  job={job!r}')
conn = iterm2.Connection()
conn.run_until_complete(main, retry=False)
if conn.loop: conn.loop.close()
```

Then send `q` to restart:
```python
import iterm2
async def main(connection):
    session = (await iterm2.async_get_app(connection)).get_session_by_id(SESSION_ID)
    if session: await session.async_send_text('q')
conn = iterm2.Connection()
conn.run_until_complete(main, retry=False)
if conn.loop: conn.loop.close()
```

## Project structure

```
claude_monitor/
  __init__.py      # Version, shared constants (SIGNAL_DIR, STATE_FILE, API_PORT, etc.), utilities
  api.py           # HTTP API server — /health, /screenshot, /text endpoints on localhost:17233
  hook.py          # Claude Code hook — auto-accepts permissions, logs events to JSONL
  tui.py           # Textual TUI — mirrors iTerm2 pane layout, displays events per session
  settings.py      # Settings dataclass, persistence, and SettingsScreen modal
  usage.py         # OAuth token extraction, API usage fetching, UsageBar widget
install.py         # Setup script — creates venv, installs package, configures hooks
run.sh             # Wrapper script — auto-restarts claude-monitor on quit
pyproject.toml     # Package config with entry points
```

## Entry points

- `claude-monitor` → `claude_monitor.tui:main` — launches the TUI
- `claude-monitor-hook` → `claude_monitor.hook:main` — hook called by Claude Code settings

Both are symlinked to `~/.local/bin/` for PATH access. The venv is at `.venv/`.

## How it works

1. **Hook** (`hook.py`): Claude Code calls this on PermissionRequest, Notification, SubagentStart, SubagentStop events via `~/.claude/settings.json` hooks config. It writes JSON events to `/tmp/claude-auto-accept/events.jsonl` and auto-allows permission requests unless paused (checked via `state.json`).

2. **TUI** (`tui.py`): Uses iTerm2 Python API to discover pane layout, then builds a matching Textual widget tree. A background worker tails the events JSONL file and routes events to the correct panel via `_iterm_session_id`. Layout is polled every 3 seconds to detect pane adds/removes; resizes update CSS only without rebuilding.

## State management

Two files in `/tmp/claude-auto-accept/`:

- **`state.json`** — Shared between hook and TUI. Contains `global_paused` (bool) and `paused_sessions` (list of iTerm UUIDs). The hook reads it; the TUI reads and writes it.
- **`events.jsonl`** — Append-only event log. The hook appends lines; the TUI tails them. Kept separate because append-only streaming doesn't suit a JSON state file.

Config lives at `~/.config/claude-monitor/config.json` (user preferences like theme, mode, scope).

## Key technical details

- **iTerm2 API**: `iterm2.Connection()` creates a websocket connection. Each call to `_fetch_layout_sync()` creates and closes a connection+event loop to avoid FD leaks.
- **Self-session detection**: Uses `ITERM_SESSION_ID` env var (format `w0t0p2:UUID`) to identify the TUI's own pane and show a Dashboard there instead of a SessionPanel.
- **Layout fingerprinting**: Two separate fingerprints — structure (session IDs, tab IDs, split directions) triggers full rebuilds; size (pixel dimensions) triggers lightweight CSS percentage updates. Tab names are excluded to avoid spurious rebuilds from dynamic title changes.
- **Event routing**: Hook events contain `_iterm_session_id` (UUID). `_resolve_panel()` maps Claude session IDs to iTerm2 session IDs on first event, then caches the mapping. Unmatched sessions get dynamically created fallback panels. Worktree sessions (detected via `/.worktrees/` or `/.claude/worktrees/` in cwd) get `WT:` prefixed titles and `.worktree` CSS class with distinct border styling.
- **Per-pane pause**: Each pane can be individually toggled between auto/manual mode. Click the status bar or focus a panel and press `m`. Global toggle (`a` key): from mixed/manual state → all auto; from all-auto → all manual. State persisted in `state.json` and pruned of stale sessions on startup.
- **Settings**: `s` key opens modal. Persists to `~/.config/claude-monitor/config.json`. Controls mode, theme, debug, iTerm scope, timestamps, usage bar.
- **Command palette**: `ctrl+p` opens palette with all commands. Custom `MonitorCommands` provider.
- **Usage bar**: Fetches from `api.anthropic.com/api/oauth/usage` every 5 min. OAuth token extracted from macOS Keychain (`Claude Code-credentials`) via `security` + `xxd`, cached until expiry. Status bar updates every 30s. Width-responsive rendering.
- **Status bar**: Native Textual `Horizontal` layout with left (`1fr`) and right (`auto`) `Static` widgets. Left shows mode + usage; right shows version + clock.
- **Multi-line commands**: Newlines in tool commands are collapsed to `↵` for single-line display in event logs.
- **Footer**: Standard Textual `Footer` widget at the bottom showing keybindings.
- **Refreshing state**: During `r` (refresh), the status bar shows "REFRESHING layout..." in accent color. On failure shows "REFRESH FAILED — iTerm2 not reachable".
- **HTTP API**: `api.py` runs an `http.server.HTTPServer` in a `@work(thread=True)` daemon on `localhost:17233`. Three endpoints: `/health` (JSON), `/screenshot` (PNG via Textual `export_screenshot` + cairosvg, or SVG via `?format=svg`), `/text` (structured JSON with sessions, dashboard, usage). Screenshots use 256-color quantization via Pillow for ~70% size reduction. Font detection swaps Textual's default "Fira Code" for an installed monospace font. Port written to `/tmp/claude-auto-accept/api-port` for discovery.
- **Clean exit**: `os._exit(0)` after `app.run()` because background threads (iterm2 websocket, file tail) can't be interrupted cleanly.
- **State transfer**: When layout rebuilds, `_build_widget_tree()` transfers state (event logs, agent counts, timers) from old panels to new ones. Active tab and focused panel are preserved across rebuilds.
- **Restarting**: Use `run.sh` wrapper for auto-restart on quit. The TUI can also be restarted programmatically by sending `q` to its iTerm2 session via the Python API.

## Development

```bash
# Install in editable mode
python3 -m venv .venv
.venv/bin/pip install -e .

# Or use the install script (also configures Claude Code hooks)
python3 install.py

# Run (auto-restarts on quit)
./run.sh

# Or run directly
claude-monitor

# Debug log
tail -f /tmp/claude-auto-accept/tui-debug.log
```

## Dependencies

- `textual>=1.0` — TUI framework
- `iterm2>=2.14` — iTerm2 Python API (websocket-based)
- `cairosvg` — SVG to PNG conversion (for HTTP API screenshots)
- Python 3.12+
