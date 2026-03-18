# Web UI — Real-Time Monitoring Dashboard

## Problem

The TUI is local-only. No way to monitor Claude Code sessions from a phone, tablet, or another machine. The existing HTTP API has `/screenshot` and `/text` but no real-time push or interactive controls.

## Solution

Add a WebSocket-powered web UI served from the existing HTTP server. Real-time event streaming, session monitoring, pause controls. Single `index.html` file with embedded CSS/JS — no build step.

## Architecture

### Server

Single server on port `17233` via `websockets` library. **Replaces `api.py`'s `http.server` entirely** — hard cutover, no coexistence mode. Serves both HTTP and WebSocket on the same port — `websockets.serve()` with a `process_request` handler routes HTTP requests (existing `/text`, `/health`, `/screenshot` + new `/web`) while upgrading `/ws` to WebSocket. Runs `asyncio.run()` inside a `@work(thread=True)` thread — creates an isolated asyncio event loop with no conflict with Textual's event loop.

**Bind address:** Defaults to `127.0.0.1` (localhost only). Set `web_lan_access: true` in settings (`~/.config/claude-monitor/config.json`) to bind `0.0.0.0` for LAN access. The setting is surfaced in the TUI settings screen (`s` key).

### Event Tailing

`web.py` owns its own independent file tail on `events.jsonl` — separate from `app_base.py`'s `watch_events()`. This keeps concerns cleanly separated: the TUI event path is untouched, and `web.py` is self-contained. Two readers on the same append-only JSONL file is harmless.

The tail runs as an asyncio task per WebSocket connection: open file, seek to end (or read last ~50 lines for the initial burst), then poll with `asyncio.sleep(0.2)` between `readline()` calls.

### TUI State Access

`web.py` receives the Textual app reference (typed as `AppStateProtocol`) and uses `app.call_from_thread()` to safely access TUI state from the asyncio thread — same proven pattern as the current `api.py`. Used for:
- `/text` endpoint → `app.call_from_thread(app.get_state_snapshot)`
- `/screenshot` endpoint → `app.call_from_thread(app.export_screenshot)`
- State change broadcasts → `app.call_from_thread(app.get_state_snapshot)` after writing `state.json`

### Client Lifecycle

Connected WebSocket clients are tracked in a `set[websockets.WebSocketServerProtocol]`. Added on connect, removed on disconnect or send error. `_broadcast()` iterates the set and catches per-client exceptions, removing dead connections silently.

**Connection limit:** Maximum 25 concurrent WebSocket connections. The 26th connection receives a close frame with code 1013 (Try Again Later) and message "Too many connections" before being closed. HTTP endpoints are unaffected by this limit.

### Data Flow

```
events.jsonl ──tail──▶ WebSocket server ──push──▶ Browser(s)
                              ▲                        │
                              │ control msgs           │
state.json ◀──write───────────┘◀───────────────────────┘
                                    (pause toggles)
```

1. Browser loads `/web` → gets `index.html`
2. JS fetches `/text` for initial state (sessions, dashboard, usage)
3. JS opens WebSocket to same host on `/ws`
4. Server sends initial burst: last ~50 events from `events.jsonl`
5. Server tails `events.jsonl`, pushes new events as JSON
6. Server pushes state changes (pause toggles from TUI) as `{"type": "state", ...}`
7. Browser sends control messages: `{"action": "toggle_pause", "session_id": "..."}` or `{"action": "toggle_global_pause"}`
8. Server writes `state.json`, broadcasts updated state to all clients

### WebSocket Protocol

**Server → Client messages:**
```json
{"type": "event", "data": { ...hook event... }}
{"type": "state", "data": {"global_paused": false, "paused_sessions": [...]}}
{"type": "snapshot", "data": { ...same as /text response... }}
```

**Client → Server messages:**
```json
{"action": "toggle_pause", "session_id": "claude-session-id"}
{"action": "toggle_global_pause"}
```

### Session ID Convention

The `/text` endpoint returns `sessions[].id` — this is the Claude session ID (from hook events' `session_id` field). Pause toggles from the web client write to `paused_claude_sessions` in `state.json`. This works regardless of which TUI variant is running because the hook checks both `paused_sessions` (iTerm UUIDs) and `paused_claude_sessions` (Claude session IDs).

### Port Discovery

Single port `17233` — same as existing `api-port` file. No additional port file needed. WebSocket connects to `/ws` on the same port.

## Frontend Design

### Aesthetic: Mission Control Console

Dense, atmospheric, functional. Not retro kitsch — refined developer monitoring.

### Typography
- **Data/events:** JetBrains Mono — loaded from Google Fonts CDN, with bundled woff2 in `static/fonts/` (~95KB) as offline fallback
- **Labels/headers:** IBM Plex Sans — same CDN-with-fallback approach (~95KB bundled)
- Fallback chain: `'Fira Code', 'SF Mono', 'Cascadia Code', ui-monospace, monospace`
- CDN link in `<head>`, `@font-face` fallback with `font-display: swap` pointing to bundled files

### Color Palette (CSS variables)
```css
--bg-deep: #0a0e14;        /* page background */
--bg-card: #111820;         /* card/panel background */
--bg-card-hover: #161e28;   /* card hover state */
--border: #1e2a38;          /* subtle card borders */
--border-active: #2a3a4e;   /* active session card border */
--text-primary: #c8d0da;    /* main text */
--text-secondary: #5a6a7e;  /* timestamps, labels */
--text-dim: #3a4a5e;        /* very dim text */
--green: #00ff88;           /* auto mode, healthy, allowed */
--green-dim: #00cc6a;       /* green variant for less emphasis */
--amber: #ffaa00;           /* manual mode, warnings */
--red: #ff4466;             /* errors, paused */
--blue: #4488ff;            /* dashboard, informational */
--interactive: #5588cc;     /* clickable elements, hover states */
--glow-green: 0 0 12px rgba(0, 255, 136, 0.3);  /* status glow */
--glow-amber: 0 0 12px rgba(255, 170, 0, 0.3);
```

### Background Texture
Subtle CSS noise overlay on `--bg-deep`:
```css
body::before {
  content: '';
  position: fixed; inset: 0;
  background: url("data:image/svg+xml,...") repeat; /* tiny noise pattern */
  opacity: 0.03;
  pointer-events: none;
  z-index: 0;
}
```
Plus faint horizontal grid lines on card backgrounds for a scan-line feel.

### Layout

```
┌─────────────────────────────────────────────────────────────┐
│ ▪ Claude Monitor            AUTO  ■■■■□□ 5h 10%    v1.0.31 │  ← top bar
├──────────┬──────────┬──────────┬────────────────────────────┤
│ Sessions │ Agents   │ Approved │ Uptime                     │  ← stats row
│    3     │   2      │   47     │ 2h 15m                     │  (big readout numbers)
├──────────┴──────────┴──────────┴────────────────────────────┤
│ ┌─────────────────────────┐ ┌─────────────────────────────┐ │
│ │ claude-monitor          │ │ other-project               │ │  ← session cards
│ │ AUTO ▸ active  ag:0     │ │ MANUAL ▸ idle  ag:2         │ │     (responsive grid)
│ │─────────────────────────│ │─────────────────────────────│ │
│ │ 11:24 ALLOWED Bash `ls` │ │ 11:20 PAUSED  Edit foo.py  │ │
│ │ 11:24 ALLOWED Read ...  │ │ 11:18 AGENT+ general_purp  │ │
│ │ 11:25 ALLOWED Bash ...  │ │ 11:15 ALLOWED Bash `npm..  │ │
│ │                         │ │                             │ │
│ └─────────────────────────┘ └─────────────────────────────┘ │
│ ┌─────────────────────────┐                                 │
│ │ project-3               │                                 │
│ │ AUTO ▸ active  ag:1     │                                 │
│ │─────────────────────────│                                 │
│ │ 11:22 TIMEOUT AskUser   │                                 │
│ │ 11:21 ALLOWED Write ..  │                                 │
│ └─────────────────────────┘                                 │
├─────────────────────────────────────────────────────────────┤
│ Usage: ■■■■■□□□□□ 5h 10%  ■■■■■■■■□□ 7d 41%               │  ← usage bar
└─────────────────────────────────────────────────────────────┘
```

### Responsive Behavior
- CSS Grid: `grid-template-columns: repeat(auto-fill, minmax(380px, 1fr))` with `max-width: 600px` per card
- Mobile (<768): single column, stats row wraps to 2x2
- Pause buttons and interactive elements: minimum 44px touch targets for mobile

### Session Cards

Each card has:
- **Header:** project name (extracted from cwd), mode badge (AUTO green / MANUAL amber), state indicator (active/idle), agent count
- **Event log:** scrollable, newest at bottom, auto-scrolls. Max 100 events retained in DOM.
- **Pause button:** click to toggle per-session pause. Visual feedback: badge color change + brief pulse
- **Border:** left border color indicates state — green for auto+active, amber for manual, dim for idle

**Empty state:** When no sessions exist, show a centered message: "Waiting for Claude Code sessions..." in `--text-secondary` with a subtle breathing opacity animation.

### Event Rendering

Each event line:
```
[HH:MM:SS] LABEL  detail text
```
- `ALLOWED` → green badge
- `PAUSED` / `MANUAL` → amber badge
- `DEFERRED` → red badge
- `TIMEOUT` → cyan badge
- `AGENT+` / `AGENT-` → blue badge
- `IDLE` → dim text

Events slide in from left with a 150ms ease-out animation. New events briefly highlight the card border with a green pulse.

### Stats Row

Large monospace numbers with small labels below:
```css
.stat-value {
  font-family: 'JetBrains Mono';
  font-size: 2.5rem;
  color: var(--green);
  text-shadow: var(--glow-green);
}
.stat-label {
  font-family: 'IBM Plex Sans';
  font-size: 0.75rem;
  color: var(--text-secondary);
  text-transform: uppercase;
  letter-spacing: 0.1em;
}
```

### Usage Bars

Horizontal gauge bars matching TUI style but with glow effect:
- Fill color transitions: green (0-50%) → amber (50-80%) → red (80-100%)
- Percentage text right-aligned
- Reset countdown below each bar

### Top Bar

Fixed at top. Shows:
- Left: "Claude Monitor" + global mode badge (clickable to toggle)
- Center: usage summary (compact, single line)
- Right: version + connection status dot (green = WS connected with subtle breathing pulse animation, red = disconnected)

### Motion

- **Page load:** Cards stagger in with `animation-delay: calc(var(--i) * 80ms)`, fade-up from 10px
- **New event:** slides in from left, 150ms ease-out
- **Status change:** badge pulses once (scale 1→1.15→1, 300ms)
- **Connection dot:** subtle breathing pulse (opacity 0.6→1→0.6, 3s ease-in-out infinite) when connected
- **Connection lost:** top bar flashes red briefly, shows reconnecting indicator
- **Reconnect:** smooth fade back to normal, re-fetch full state

### Connection Handling

- Auto-reconnect with exponential backoff (1s, 2s, 4s, max 30s)
- On reconnect: fetch `/text` for fresh state, WS sends initial burst
- Show connection status in top bar
- If disconnected >10s, dim the UI slightly to indicate stale data

## Files

### New Files

- **`claude_monitor/web.py`** (~250 LOC)
  - `start_web_server(app)` — starts unified `websockets` server on `:17233` with `process_request` for HTTP routing. Called from `app_base.py`'s `serve_api()` method.
  - `_handle_http(path, headers)` — routes `/web`, `/text`, `/health`, `/screenshot`. Imports response generators from `api.py`. Uses `app.call_from_thread()` for TUI state access.
  - `_handle_ws(websocket)` — manages WebSocket connection lifecycle. Enforces 25-connection limit. Adds/removes from `_clients: set`. Spawns `_tail_events` task per connection.
  - `_tail_events(websocket)` — independent file tail on `events.jsonl`. Reads last ~50 lines for initial burst, then polls with `asyncio.sleep(0.2)`.
  - `_handle_control(msg, app)` — validates and handles pause toggle messages, writes `state.json`, triggers state broadcast
  - `_broadcast(clients, msg)` — sends to all connected clients, removes dead connections on error
  - Bind address from settings: `127.0.0.1` (default) or `0.0.0.0` (`web_lan_access: true`)
  - `MAX_WS_CONNECTIONS = 25` module-level constant

- **`claude_monitor/static/index.html`** (~650 LOC, single file)
  - Embedded `<style>` with all CSS (~280 lines)
  - Embedded `<script>` with all JS (~320 lines)
  - HTML template (~50 lines)
  - Inline SVG favicon (green circle monitor icon)
  - Dynamic `<title>`: "Claude Monitor (N)" where N = active session count

- **`claude_monitor/static/fonts/`** (offline fallback)
  - `JetBrainsMono-Regular.woff2` (~95KB)
  - `IBMPlexSans-Regular.woff2` (~95KB)
  - Primary load via Google Fonts CDN; these serve as `@font-face` fallback when offline

### Modified Files

- **`claude_monitor/api.py`**
  - Remove `HTTPServer`, `BaseHTTPRequestHandler`, `start_api_server()`, and the request handler class
  - Keep as a module with importable helpers: `generate_text_response(app)`, `generate_screenshot_png(app)`, `generate_screenshot_svg(app)`, and the `AppStateProtocol` type
  - `web.py` imports these helpers rather than reimplementing response generation

- **`claude_monitor/app_base.py`**
  - Replace `from claude_monitor.api import start_api_server` with `from claude_monitor.web import start_web_server`
  - Replace `serve_api()` method body: call `start_web_server(self)` instead of `start_api_server(self)`
  - No changes to `watch_events()` or any other method

- **`claude_monitor/__init__.py`**
  - No new port constants needed (reuses `API_PORT`)

- **`claude_monitor/settings.py`**
  - Add `web_lan_access: bool = False` to Settings dataclass
  - Add toggle in SettingsScreen

- **`pyproject.toml`**
  - Add `websockets>=13.0,<15` to dependencies
  - Add `[tool.setuptools.package-data]` to include `static/**/*`:
    ```toml
    [tool.setuptools.package-data]
    claude_monitor = ["static/*", "static/fonts/*"]
    ```

## Security

- Default bind `127.0.0.1` — localhost only. LAN access opt-in via `web_lan_access: true` in settings.
- **When LAN-enabled:** `/screenshot` exposes terminal content on the network. Acceptable for trusted networks; future versions should add optional bearer token auth gated behind a settings flag.
- WebSocket validates JSON messages, ignores unknown actions.
- No sensitive data exposed beyond what the TUI already shows.
- Dark theme only (non-goal: light theme for v1).

## Testing

API and WebSocket server tests only. No browser/UI testing for v1.

### Unit tests (`tests/test_web.py`)
- HTTP routing: `/web` serves HTML, `/text` returns JSON, `/health` returns JSON, `/screenshot` returns image
- WebSocket message parsing: valid control messages accepted, malformed JSON rejected, unknown actions ignored
- Connection limit: 26th WebSocket connection receives close frame 1013
- State mutation: `toggle_pause` writes correct session to `state.json`, `toggle_global_pause` flips `global_paused`

### Integration tests
- Full lifecycle: connect WS → receive initial burst → send control message → verify `state.json` updated → receive broadcast state update
- Reconnection: connect, disconnect, reconnect → receive fresh initial burst
- HTTP endpoint parity: existing `/text`, `/health`, `/screenshot` tests updated to use new server (verify no behavioral regression from `http.server` → `websockets` migration)

### Not in scope for v1
- Browser/Playwright testing
- Visual regression testing
- Load/stress testing
