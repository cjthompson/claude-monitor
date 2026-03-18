# claude-monitor: Comprehensive Code Rewrite Plan

## Context

The claude-monitor codebase (5,463 lines across 9 Python files) has accumulated significant technical debt:
- **71% of code** is in 3 TUI files (tui.py, tui_simple.py, tui_common.py)
- **400+ lines duplicated** between tui.py and tui_simple.py (event handling, formatting, state, actions)
- **tui_common.py** is a 1,375-line grab bag (widgets, modals, formatting, scrollbars, commands)
- **usage.py** uses 4 module-level mutable globals instead of a class
- **settings.py** has 113 lines of repetitive widget construction
- **60+ bare `except Exception` blocks**, dead code, no tests

Goal: Full rewrite to Python 3.12+ best practices, modular design (<150 lines/file where possible), DRY, with comprehensive integration tests and multi-model code reviews.

## Orchestration Strategy

**Agent Teams not available** (TeamCreate tool absent). Falling back to:
- **Parallel agents with worktree isolation** for independent tasks
- **Sequential subagents** where file overlap or dependencies exist
- **Cherry-pick integration** onto a `rewrite/integration` branch

All work in worktrees. Nothing touches main without explicit permission.

**Model preference:** Use **Sonnet** for all agents by default (faster, cost-efficient). Use Opus only for code reviews and critical-path architectural decisions (e.g., Agent 2C splitting tui_common.py).

---

## Phase 0: Integration Tests — TDD Safety Net ✅ IN PROGRESS

**Worktree:** `.claude/worktrees/phase0-tests` (detached HEAD from main)
**Status:** Tests written (85 tests, 1,550 lines), infrastructure fixes applied. Passing tests still being validated.

### Test files created

```
tests/
  conftest.py           (179 lines) Fixtures: isolated dirs, event injection, factories, app_fixture
  test_api_endpoints.py (165 lines) 7 tests: /health, /screenshot (svg+png), /text, 404
  test_command_palette.py (29 lines) 2 tests: commands listed, actions exist
  test_dashboard.py     (122 lines) 10 tests: minimize/restore/grow/shrink/tab-mode/stats/sparkline
  test_event_formatting.py (93 lines) 8 tests: labels, tool details, timestamps, newline collapse
  test_event_routing.py  (85 lines) 5 tests: session creation, routing, worktree detection, dashboard
  test_hook.py          (238 lines) 10 tests: auto-allow, pause states, excluded tools, timeout
  test_keyboard_actions.py (105 lines) 6 tests: all bindings, tab cycling, quit
  test_modals.py        (132 lines) 10 tests: settings/choices/questions/help open/dismiss/save
  test_pause_modes.py   (126 lines) 8 tests: global/per-pane/ask/mixed toggle + persistence
  test_session_lifecycle.py (92 lines) 5 tests: create/active/idle/stop/timeout cleanup
  test_state_persistence.py (74 lines) 6 tests: state.json, config.json, oauth exclusion
  test_status_bar.py     (43 lines) 4 tests: AUTO/MANUAL/version/clock
  test_tab_management.py (67 lines) 4 tests: titles, worktree prefix, close, dashboard protected
```

### Key test infrastructure decisions (learned during implementation)

**Thread teardown:** SimpleTUI starts 3 background `@work(thread=True)` workers. Tests must force-stop them:
```python
# In app_fixture teardown (conftest.py)
app._stop_event.set()
if app._api_server:
    app._api_server.shutdown()   # unblocks handle_request()
    app._api_server.server_close()
```

**API port patching:** `start_api_server(app, port=API_PORT)` captures the default at definition time. Patching `claude_monitor.api.API_PORT` after import has no effect. Use `_patch_api_port()` helper in `test_api_endpoints.py` which patches `start_api_server` itself:
```python
def _patch_api_port(monkeypatch, isolated_state, port):
    import claude_monitor.api as api_mod
    original = api_mod.start_api_server
    def patched(app, port=port): ...
    monkeypatch.setattr("claude_monitor.api.start_api_server", patched)
    monkeypatch.setattr("claude_monitor.tui_simple.start_api_server", patched)
```

**Module-level import patching:** Constants imported with `from claude_monitor import EVENTS_FILE` are copied at import time. Must patch EVERY module: `claude_monitor`, `claude_monitor.tui_simple`, `claude_monitor.tui_common`, `claude_monitor.hook`, `claude_monitor.settings`, `claude_monitor.api`.

**Test timeout:** `pytest-timeout` with `timeout = 15` in pyproject.toml prevents hangs from background thread cleanup.

**Production port protection:** `isolated_state` fixture sets `API_PORT = 0` in `claude_monitor.api` so no test accidentally binds to 17233.

**Checkpoint 0:** All ~85 tests must pass before proceeding to Phase 1.

---

## Phase 1: Foundation (Sequential, 1 Sonnet agent)

**Worktree:** `.claude/worktrees/phase1-foundation`
**Depends on:** Checkpoint 0 complete.
**Goal:** Create the shared base class that eliminates 400+ lines of duplication between tui.py and tui_simple.py.

### Tasks

1. Create `claude_monitor/app_base.py` (~400 lines) — `MonitorApp(App)` base class:
   - **Shared state:** `_global_paused`, `_paused_claude_sessions`, `_paused_iterm_sessions`, `_ask_paused_sessions`, `panels`, `dashboard`, `settings`, `_stop_event`, `_api_server`, `_last_usage_data`
   - **Shared methods (move verbatim):** `get_state_snapshot()`, `_format_ts()`, `_update_status_bar()`, `_apply_settings()`, `_on_settings_closed()`, `_on_token_refreshed()`, `watch_events()`, `poll_usage()`, `_refresh_usage()`, `serve_api()`, `action_toggle_pause()`, `action_show_choices()`, `action_show_questions()`, `action_show_help()`, `action_quit()`
   - **Parameterized methods:** `on_hook_event()` and `_apply_event()` call `self._session_id_from_event(data)` — the one line that differs between modes
   - **Event routing with `match/case`** instead of if/elif chains
   - **Abstract methods** (subclasses implement): `_session_id_from_event(data) -> str`, `_resolve_panel(data) -> SessionPanel | None`, `is_pane_paused(sid) -> bool`, `is_ask_paused(sid) -> bool`, `action_next_tab()`, `action_prev_tab()`

2. Update `claude_monitor/__init__.py` — add type alias: `type EventData = dict[str, Any]`

**Checkpoint 1:** Multi-model review of `app_base.py`. Run Phase 0 tests — must pass.

---

## Phase 2: Independent Module Refactors (3 parallel Sonnet agents)

**Depends on:** Phase 1 merged to integration branch.
**Goal:** Refactor usage.py, settings.py, and split tui_common.py independently (no file overlap).

### Agent 2A: `usage.py` refactor
**Worktree:** `.claude/worktrees/phase2-usage`
**Files modified:** `usage.py` only
- Convert 4 module-level mutable globals to `UsageManager` class
- Instance methods: `get_token()`, `fetch()`, `format_inline()`, `invalidate_cache()`
- Module-level pure functions retained: `_bar()`, `_quota()`
- `match/case` for token source resolution (settings JSON → keychain → None)
- Target: ~350 lines (down from ~430)

### Agent 2B: `settings.py` refactor
**Worktree:** `.claude/worktrees/phase2-settings`
**Files modified:** `settings.py` only
- Declarative `FIELD_DEFS` list drives `SettingsScreen.compose()` — replaces 113 lines of repetitive widget construction
- `Settings.__post_init__()` validates and clamps all fields
- Single `on_change()` handler replaces 4 identical handlers
- Target: ~200 lines (down from ~450)

### Agent 2C: Split `tui_common.py` — use **Opus** (critical path)
**Worktree:** `.claude/worktrees/phase2-split-common`
**Files modified:** `tui_common.py` split into new subpackages:

```
claude_monitor/
  widgets/
    __init__.py          # Re-exports: SessionPanel, DashboardPanel, sparkline, scrollbars
    scrollbar.py  (~75)  # HalfBlockScrollBarRender, HalfBlockVerticalScrollBar, HalfBlockHorizontalScrollBar
    sparkline.py  (~85)  # FixedWidthSparkline widget
    session_panel.py (~300) # SessionPanel — decompose _render_status() (147 lines) into helpers
    dashboard_panel.py (~150) # DashboardPanel
  screens/
    __init__.py          # Re-exports: all screen classes
    context_menu.py (~80)  # PaneContextMenu
    choices.py    (~140)   # ChoicesScreen
    questions.py  (~130)   # QuestionsScreen
    help.py       (~175)   # HelpScreen
  formatting.py   (~100)   # _oneline(), _format_ask_user_question_*(), _safe_css_id()
  commands.py     (~50)    # MonitorCommands provider
  messages.py     (~15)    # HookEvent message dataclass
```

- `tui_common.py` becomes a thin re-export shim (kept until Phase 3 updates imports)
- `SessionPanel._render_status()` decomposed: `_render_uptime()`, `_render_agents()`, `_render_accepts()`, `_render_state_badge()`

**Merge order:** 2C first (establishes new module paths), then 2A and 2B (independent).

**Checkpoint 2:** All 3 branches merged to integration. Multi-model review. Phase 0 tests must pass.

---

## Phase 3: TUI Refactors (2 parallel Sonnet agents)

**Depends on:** Phase 1 + Phase 2 merged to integration branch.

### Agent 3A: `tui_simple.py` refactor
**Worktree:** `.claude/worktrees/phase3-simple`
**Files modified:** `tui_simple.py`
- Inherit from `MonitorApp` (Phase 1 base class)
- Remove all duplicated methods (now in base) — estimated 400+ line reduction
- Implement abstracts: `_session_id_from_event()` returns Claude `session_id`
- Keep simple-specific only: tab management, dashboard minimize/expand/tab-mode, session cleanup, `x` close, `d`/`D`/`=`/`-` bindings
- Update all imports to new `widgets/` and `screens/` paths
- Target: ~300 lines (down from 1,034)

### Agent 3B: `tui.py` + new `iterm2_layout.py`
**Worktree:** `.claude/worktrees/phase3-iterm`
**Files modified:** `tui.py`, NEW `claude_monitor/iterm2_layout.py`
- Extract all iTerm2 API code to `iterm2_layout.py` (~250 lines):
  - `LayoutFetcher`: connection management, pane tree traversal
  - `LayoutFingerprint`: structure + size fingerprinting
  - `WidgetTreeBuilder`: builds Textual widget tree from iTerm2 pane data
  - `KeystrokeSender`: `send_approve()`, `send_text()`
- `tui.py` inherits from `MonitorApp`, keeps only iTerm2-specific:
  - `_fetch_layout()`, `_poll_layout()`, `_send_approve()`
  - `_resolve_panel()` using iTerm2 UUID mapping
  - `action_next_tab()` / `action_prev_tab()` for iTerm2 tabs
  - Fallback panel creation
- Implement abstracts: `_session_id_from_event()` returns iTerm UUID from `_iterm_session_id`
- Target: `tui.py` ~450 lines, `iterm2_layout.py` ~250 lines (down from 1,504 combined)

**No merge conflicts** — different files.

**Checkpoint 3:** Phase 0 tests pass on integration branch. Both TUI modes start cleanly. Multi-model review.

---

## Phase 4: Cleanup (2 parallel Sonnet agents)

**Depends on:** Phase 3 merged to integration branch.

### Agent 4A: `hook.py`, `api.py`, final cleanup
**Worktree:** `.claude/worktrees/phase4-hook-api`
**Files:** `hook.py`, `api.py`, `install.py`
- `hook.py`: Extract `decide_permission(state, event) -> str` pure function; `match/case` for event routing; remove `tui_common.py` import shim
- `api.py`: Add `AppStateProtocol` Protocol type for `get_state_snapshot` interface; clean up screenshot handler
- Remove `tui_common.py` re-export shim (all consumers now use `widgets/`, `screens/`, `formatting`, etc.)
- Delete dead code: unused imports, unreachable branches, stale comments
- Target: `hook.py` ~100 lines, `api.py` ~150 lines

### Agent 4B: Exception cleanup + test path updates
**Worktree:** `.claude/worktrees/phase4-exceptions`
- Replace 60+ bare `except Exception` with specific types (`OSError`, `json.JSONDecodeError`, `KeyError`, etc.)
- Consistent logging: `log.debug` for expected conditions, `log.warning` for recoverable errors, `log.error` for failures
- Update test imports if any still reference old `tui_common` paths

**Checkpoint 4:** All 85 tests pass against final rewritten code. Manual smoke test both TUI modes.

---

## Final Module Structure (Target)

```
claude_monitor/
  __init__.py           (~50)   Version, constants, utilities, type aliases
  app_base.py           (~400)  MonitorApp shared base class
  messages.py           (~15)   HookEvent message dataclass
  formatting.py         (~100)  _oneline(), _format_ask_user_question_*(), _safe_css_id()
  commands.py           (~50)   MonitorCommands provider
  hook.py               (~100)  Claude Code hook + decide_permission()
  api.py                (~150)  HTTP API server + AppStateProtocol
  settings.py           (~200)  Settings dataclass + SettingsScreen (declarative)
  usage.py              (~350)  UsageManager class + pure format helpers
  iterm2_layout.py      (~250)  iTerm2 API: layout fetch, fingerprint, widget builder, keystroke
  tui.py                (~450)  AutoAcceptTUI (iTerm2 mode) + main()
  tui_simple.py         (~300)  SimpleTUI (simple/Linux mode) + main()
  widgets/
    __init__.py                 Re-exports
    scrollbar.py        (~75)   HalfBlockScrollBarRender + custom scrollbars
    sparkline.py        (~85)   FixedWidthSparkline
    session_panel.py    (~300)  SessionPanel widget
    dashboard_panel.py  (~150)  DashboardPanel widget
  screens/
    __init__.py                 Re-exports
    context_menu.py     (~80)   PaneContextMenu
    choices.py          (~140)  ChoicesScreen
    questions.py        (~130)  QuestionsScreen
    help.py             (~175)  HelpScreen

tests/
  conftest.py           (179)   Fixtures, factories, app_fixture with thread teardown
  test_hook.py          (238)   10 tests
  test_event_routing.py  (85)   5 tests
  test_pause_modes.py   (126)   8 tests
  test_keyboard_actions.py(105) 6 tests
  test_modals.py        (132)   10 tests
  test_dashboard.py     (122)   10 tests
  test_tab_management.py (67)   4 tests
  test_state_persistence.py(74) 6 tests
  test_api_endpoints.py (165)   7 tests
  test_status_bar.py     (43)   4 tests
  test_event_formatting.py(93)  8 tests
  test_session_lifecycle.py(92) 5 tests
  test_command_palette.py (29)  2 tests
```

**Estimated total: ~3,200 lines** (down from 5,463 — 41% reduction) across 20 modules instead of 9.

### Files over 150 lines (justified by tight cohesion)

| File | Lines | Justification |
|------|-------|---------------|
| `app_base.py` | ~400 | All shared TUI logic — splitting fragments the abstraction |
| `tui.py` | ~450 | iTerm2-specific app class — inherently complex |
| `tui_simple.py` | ~300 | Simple-mode app class |
| `usage.py` | ~350 | OAuth + API fetch + formatting — all usage-domain code |
| `session_panel.py` | ~300 | Single widget, decomposed but still cohesive |
| `settings.py` | ~200 | Dataclass + modal — tightly coupled |
| `iterm2_layout.py` | ~250 | All iTerm2 API code — inherently cohesive |
| `help.py` | ~175 | Help screen with full content |

---

## Multi-Model Code Review Protocol

At each checkpoint, 3 models review in parallel with different lenses:

| Model | Role | Focus areas |
|-------|------|-------------|
| **Opus** | Architecture & Correctness | State machine transitions, thread safety, race conditions between background workers and event loop, abstraction boundaries, inheritance design |
| **Sonnet** | Feature Parity | Every keybinding present, all CSS classes applied correctly, all 12 settings fields load/save/apply, every event type handled, API response shapes match |
| **Haiku** | Surface & Style | Imports correct and complete, string labels spelled right, Rich markup balanced, type annotations valid Python 3.12+, naming conventions consistent |

**Cross-check protocol:**
1. Each model reports: **BLOCKER** (must fix before proceeding) / **WARNING** (should fix) / **NOTE** (informational)
2. Any BLOCKER from any model halts integration
3. WARNINGs from 2+ models are promoted to BLOCKERs
4. Tiebreakers: Opus on architecture/correctness, Sonnet on feature completeness

---

## Verification

### Automated — 85 integration tests (written Phase 0, run every checkpoint)

```
pytest tests/
```

Must pass after every phase merge. Tests cover:
- Hook decision logic (10) — auto-allow, all pause states, excluded tools, timeout
- Event routing (5) — session creation, routing by ID, worktree detection, dashboard feed
- Pause modes (8) — global/per-pane/ask toggle, mixed state, state.json persistence
- Keyboard actions (6) — all bindings, tab cycle wrap, quit
- Modals (10) — all 4 screens open/dismiss/save correctly
- Dashboard (10) — all resize/move operations, stats update, sparkline accumulates
- Tab management (4) — titles from cwd, WT: prefix, close, dashboard protection
- State persistence (6) — state.json, config.json, oauth excluded from disk
- HTTP API (7) — /health, /screenshot PNG+SVG, /text, 404
- Status bar (4) — AUTO/MANUAL/version/clock display
- Event formatting (8) — all event types, newline collapse, timestamp styles
- Session lifecycle (5) — create/active/idle/stop/timeout
- Command palette (2) — completeness check

### Manual Smoke Test (after Checkpoint 3 and final)

1. `./run.sh` — SimpleTUI starts, processes events, pause toggles, settings persist
2. `./run.sh` in iTerm2 — AutoAcceptTUI mirrors panes, tab titles update
3. `curl localhost:17233/health` — returns version + uptime
4. `curl localhost:17233/screenshot` — returns valid PNG
5. `curl localhost:17233/text` — returns sessions with correct state
6. OAuth configured — usage bar appears and updates every 5 min
7. Worktree cwd — session panel shows WT: prefix and distinct border

### Pre-Merge to Main Checklist

- [ ] All 85 integration tests pass on `rewrite/integration` branch
- [ ] Both TUI modes start without errors or tracebacks
- [ ] All code review BLOCKERs and WARNINGs resolved
- [ ] No regressions against 85-item feature checklist
- [ ] `-beta.X` suffix removed from `__version__`
- [ ] `pyproject.toml` version synced
- [ ] `install.py` works cleanly on fresh setup
- [ ] `run.sh` auto-restart still functional
