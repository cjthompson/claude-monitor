# Session Hand-off Summaries

## Context

When Chris ends a day and returns the next morning, he can't remember what each
Claude Code session was working on, which agents were mid-flight, or where each
session stopped. Today he has to ask each agent "where did we leave off?".

claude-monitor already sees everything needed to answer that — it just throws it
away. Hook payloads carry `session_id`, `cwd`, `transcript_path`; `Stop` carries
`last_assistant_message`; `SubagentStop` carries `agent_type` +
`agent_transcript_path` + its own `last_assistant_message`; `SessionStart`/
`SessionEnd` are already registered. But everything lands in
`/tmp/claude-auto-accept/events.jsonl`, which is wiped on reboot and only tailed
live by the TUI (`app_base.py:518` `watch_events`).

Separately, `~/.claude/projects/<slug>/<session_id>.jsonl` transcripts are
durable and richer than expected: they contain Claude Code's own `ai-title`
lines (this session is already titled "claude-monitor session hand-off
summaries"), `last-prompt`, `mode`/`permission-mode`, `file-history-snapshot`,
and the full message history.

**Outcome:** a durable per-session hand-off record, captured automatically,
readable four ways (TUI tab, CLI, markdown file, injected back into the next
session), with every surface individually toggleable in Settings.

## Design decisions (agreed)

| Decision | Choice |
|---|---|
| Summary generation | Heuristic facts always; LLM prose optional |
| LLM transport | Pluggable Python provider layer (MiniMax default) + `claude -p` fallback |
| LLM credentials | `MINIMAX_API_KEY` env var |
| Capture triggers | SessionEnd, idle timeout, on demand |
| Surfaces | TUI tab, CLI, markdown, resume injection — each toggleable |
| Storage | `~/.config/claude-monitor/handoff/` (alongside `config.json`) |
| Injection rule | Any SessionStart in a known `cwd`, subject to a max-age cap |
| Retention | N most recent sessions per project (default 5) |
| Session scope | Monitor-seen sessions, enriched from transcripts |

## Architecture

Four new modules plus edits to five existing files.

```
hook.py ──SessionEnd──> handoff.capture()  ──> store (json)
        └─SessionStart─> handoff.injection_context()  <──┘

TUI (idle worker / keypress) ──> handoff.capture(+llm) ──> store ──> markdown
CLI (claude-monitor-handoff) ─────────────┬───────────────┘
                                          └─> llm.complete()  # pluggable provider
```

The store is the single source of truth; every surface is a renderer over it.

### New: `claude_monitor/transcript.py`

Pure, read-only parsing of `~/.claude/projects/**/<session_id>.jsonl`. No
knowledge of the store or settings. Functions:

- `find_transcript(session_id, cwd) -> Path | None` — prefer the
  `transcript_path` handed over by the hook; fall back to slugifying `cwd`.
- `parse(path) -> TranscriptFacts` — single pass, tolerant of malformed lines
  (transcripts are appended live). Extracts:
  - `ai_title` — last `type == "ai-title"` entry
  - `last_prompt` — last `type == "last-prompt"` entry
  - `first_user_prompt` — first `type == "user"` with
    `origin.kind == "human"` and `promptSource == "typed"` (skips the
    `isMeta` / `local-command-*` noise)
  - `last_assistant_message` — last assistant text block
  - `files_touched` — paths from `Edit`/`Write`/`NotebookEdit` tool_use inputs,
    cross-checked against `file-history-snapshot` entries
  - `agents` — `isSidechain` entries + Agent tool_use inputs → `(type, label)`
  - `open_items` — trailing `AskUserQuestion` / `ExitPlanMode` tool_use with no
    matching tool_result
  - `started_at` / `ended_at` from first/last timestamps
- Reads only the **tail** (last ~2 MB) for large transcripts; the fields that
  matter are all last-wins.

### New: `claude_monitor/handoff.py`

Store, capture, summarize, render. This is the feature's core.

**Data model** (`@dataclass HandoffEntry`, JSON-serialized):

```
session_id, project_path, project_slug, title, started_at, ended_at,
capture_reason           # session_end | idle | manual
last_prompt, last_assistant_message
git_branch, git_dirty, git_ahead
files_touched: list[str]
agents: list[{type, label, status, last_message}]
open_items: list[{kind, text}]      # askuserquestion | plan | deferred_permission
event_stats: {approved, deferred, agents_spawned}
summary: {goal, stopping_point, next_steps} | None
summary_model, summary_generated_at
```

**Storage layout** under `CONFIG_DIR` (`settings.py:22`):

```
~/.config/claude-monitor/handoff/
  sessions/<project-slug>/<session_id>.json    # one entry per session
  handoff.md                                   # global digest, regenerated
  projects/<project-slug>.md                   # per-project digest
```

Writes use the same atomic `mkstemp` + `os.replace` pattern as
`save_settings` (`settings.py:107-118`).

**Key functions:**

- `capture(session_id, cwd, transcript_path, reason, *, with_llm) -> HandoffEntry`
  — merges transcript facts, git state, and monitor event stats; writes the
  entry; prunes to `handoff_retain_per_project`; regenerates markdown if
  enabled. Heuristic-only path must complete in well under a second.
- `summarize(entry) -> dict | None` — the optional LLM step. Builds a compact
  prompt (title, first prompt, last prompt, last assistant message, files,
  agents, open items), asks for strict JSON, and calls `llm.complete()`.
  Returns `{goal, stopping_point, next_steps}`. Any failure returns `None` and
  the entry keeps its heuristic content — never fatal. `handoff.py` knows
  nothing about providers; it only imports `llm`.
- `latest_for_cwd(cwd, max_age_hours) -> HandoffEntry | None`
- `injection_context(cwd, exclude_session_id) -> str | None` — renders the
  latest entry as a short markdown block for SessionStart.
- `render_markdown(...)` — global digest grouped by project, plus per-project
  files.
- `prune(project_slug, keep)`.

**Git state** via `subprocess` with `cwd=` and a 2 s timeout —
`rev-parse --abbrev-ref HEAD`, `status --porcelain`. Treat any failure as
"not a repo".

### New: `claude_monitor/llm.py` (pluggable transport)

Deliberately **not** the Vercel AI SDK: that is TypeScript-only, and adopting it
would mean a Node runtime, an npm install step, a bundled JS script, and
subprocess IPC in an otherwise pure-Python project — reintroducing most of the
startup cost `claude -p` was rejected for. MiniMax's OpenAI-compatible endpoint
gives the same provider-pluggability directly from Python.

A tiny provider layer with **zero new runtime dependencies** (stdlib
`urllib.request` + `json`; the project currently depends only on cairosvg,
cryptography, iterm2, textual, websockets):

```python
class LLMError(Exception): ...

@dataclass
class Provider:
    name: str
    base_url: str
    api_key_env: str
    default_model: str
    extra_body: dict           # e.g. {"thinking": {"type": "disabled"}}

PROVIDERS = {
    "minimax":   Provider("minimax",   "https://api.minimax.io/v1",
                          "MINIMAX_API_KEY", "MiniMax-M3",
                          {"thinking": {"type": "disabled"}}),
    "openai":    Provider("openai",    "https://api.openai.com/v1",
                          "OPENAI_API_KEY", "gpt-5-mini", {}),
    "anthropic": Provider("anthropic", "https://api.anthropic.com/v1",
                          "ANTHROPIC_API_KEY", "claude-haiku-4-5-20251001", {}),
}

def complete(prompt, *, system=None, transport, model=None,
             timeout=30, max_tokens=800) -> str
```

- **OpenAI-compatible transports** (`minimax`, `openai`, and any future
  provider) share one code path: `POST {base_url}/chat/completions`,
  `Authorization: Bearer $<api_key_env>`, body `{model, messages, max_tokens,
  **extra_body}`. Adding a provider is a `PROVIDERS` entry, not new code.
  MiniMax's `thinking: {"type": "disabled"}` is set by default — a hand-off
  summary does not need reasoning tokens.
- **`claude_cli` transport** stays as a keyless fallback: shells out to
  `claude -p --model <model> --output-format json`. Slower, but works for
  anyone without an API key.
- **Anthropic** is registered but its messages endpoint differs enough
  (`x-api-key`, `anthropic-version`, `system` as a top-level field) that it
  needs a small adapter; build it only when actually wanted.
- Validates HTTP status **and** MiniMax's `base_resp.status_code` — MiniMax can
  return HTTP 200 with an error body. Raises `LLMError` on anything unexpected;
  `handoff.summarize()` catches it and degrades to heuristics.
- Never logs the API key.

Key resolution: read `os.environ[provider.api_key_env]` at call time. If unset,
raise `LLMError` with an actionable message. Note that the hook process may not
inherit the shell environment — so LLM summarization runs from the TUI and CLI
(which do), never from the SessionEnd hook path (which is heuristic-only
anyway).

### New: `claude_monitor/screens/handoff.py` (TUI surface)

A `HandoffPanel` widget: left column lists recent sessions
(`title — project — relative last-activity`), right pane shows the selected
entry rendered. Mounted as a **fixed `TabPane`**, mirroring the Background
Agents tab (`tui.py:449`). In simple mode, follow the toggleable-dashboard-tab
pattern (`tui_simple.py:785 action_toggle_dashboard_tab`).

Bindings/actions: `action_show_handoff` (open/focus tab),
`action_capture_handoff` (capture current session now, with LLM if enabled),
`action_refresh_handoff`. Register all three in
`MonitorCommands.COMMANDS_LIST` (`commands.py:9`) and in `screens/help.py`.

### Edits: `claude_monitor/hook.py`

Two new branches in `main()`, both gated on settings and both **lazily
imported** so the hot PermissionRequest path stays fast:

- `SessionEnd` → if `handoff_enabled and handoff_capture_on_session_end`, call
  `handoff.capture(..., reason="session_end", with_llm=False)`. Heuristic only:
  never block session teardown on an LLM call. Wrap in try/except — a hand-off
  failure must never break the hook.
- `SessionStart` → if `handoff_enabled and handoff_inject_on_start`, look up
  `injection_context(cwd, exclude_session_id=session_id)` and emit:

  ```json
  {"hookSpecificOutput": {"hookEventName": "SessionStart",
                          "additionalContext": "<markdown>"}}
  ```

  Excluding the current `session_id` avoids re-injecting a session's own
  summary on resume. Age-capped by `handoff_inject_max_age_hours`.

### Edits: `claude_monitor/app_base.py`

New `@work(thread=True)` worker `poll_handoff`, modelled on `poll_usage`
(`app_base.py:553`): every 60 s, for each tracked session whose last event is
older than `handoff_capture_idle_mins` and which has new activity since its
last capture, run `capture(reason="idle", with_llm=handoff_llm_enabled)`.
Guarded by `self._stop_event` like the other workers. Skipped entirely when
`handoff_capture_idle_mins == 0`.

The worker needs `session_id → (cwd, transcript_path, last_event_ts)`. Hook
events already carry all three; add a `self._session_meta: dict[str, dict]`
updated in the existing hook-event handler.

### Edits: `claude_monitor/settings.py`

New `Settings` fields (all default off, so the feature is opt-in):

```python
handoff_enabled: bool = False
handoff_capture_on_session_end: bool = True
handoff_capture_idle_mins: int = 0          # 0 = off
handoff_llm_enabled: bool = False
handoff_llm_transport: str = "minimax"      # minimax | openai | claude_cli
handoff_model: str = ""                     # "" = provider default
handoff_llm_timeout_secs: int = 30
handoff_inject_on_start: bool = False
handoff_inject_max_age_hours: int = 72
handoff_markdown_enabled: bool = True
handoff_retain_per_project: int = 5
```

Clamp the numerics and validate `handoff_llm_transport` against
`llm.PROVIDERS` in `__post_init__`, alongside the existing clamps
(`settings.py:80-86`). Add matching entries to `FIELD_DEFS`
(`settings.py:158`) — switches for the booleans, an `input` with
`input_type: "integer"` for the numerics, a `select` for
`handoff_llm_transport`, and a free-text `input` for `handoff_model`
(placeholder showing the selected provider's default). The Settings row for
`handoff_llm_transport` should surface whether the required API key env var is
currently visible, so a missing `MINIMAX_API_KEY` is obvious before first use.

### New: `claude-monitor-handoff` CLI

New entry point in `pyproject.toml` `[project.scripts]` →
`claude_monitor.cli_handoff:main`, following `cli_credentials.py`'s structure.
Subcommands:

- `list [--project <slug>] [--days N]` — table of recent sessions
- `show <session_id|latest>` — full entry
- `digest` — the standup-style cross-project view (what he reads in the morning)
- `capture [--session <id>] [--llm]` — manual capture
- `prune`

## Files

**New:** `claude_monitor/transcript.py`, `claude_monitor/handoff.py`,
`claude_monitor/llm.py`, `claude_monitor/cli_handoff.py`,
`claude_monitor/screens/handoff.py`, `tests/test_transcript.py`,
`tests/test_handoff.py`, `tests/test_llm.py`,
`tests/fixtures/transcript_sample.jsonl`

**Modified:** `claude_monitor/hook.py` (SessionStart/SessionEnd branches),
`claude_monitor/settings.py` (9 fields + `FIELD_DEFS`),
`claude_monitor/app_base.py` (`poll_handoff` worker, `_session_meta`),
`claude_monitor/tui.py` + `claude_monitor/tui_simple.py` (Hand-off tab,
bindings), `claude_monitor/commands.py` (3 palette entries),
`claude_monitor/screens/help.py` (key docs), `pyproject.toml` (entry point),
`README.md` + `CHANGELOG.md`

## Build order

1. `transcript.py` + tests against a checked-in fixture transcript.
2. `handoff.py` store/capture/render + tests (heuristic path only, LLM stubbed).
3. Settings fields + `FIELD_DEFS`.
4. `hook.py` SessionEnd capture, then SessionStart injection.
5. `cli_handoff.py` — first surface that's usable end to end.
6. `llm.py` + `summarize()` behind `handoff_llm_enabled`, MiniMax first, then
   the `claude_cli` fallback.
7. TUI tab + bindings + palette + help.
8. Markdown renderers.
9. README / CHANGELOG.

Steps 1–2 and 5 give a working feature before any TUI work lands.

## Risks

- **Hook latency.** `hook.py` runs on every event. Handoff imports must be
  local to the SessionStart/SessionEnd branches, and every handoff call wrapped
  in try/except so a bug can never block a session or a permission decision.
- **Context pollution on injection.** Injecting a stale or wrong-project
  summary is worse than injecting nothing. Hence: exclude the current
  `session_id`, cap by age, default the toggle off.
- **Transcript format drift.** `ai-title` / `last-prompt` are undocumented
  Claude Code internals. Every extractor returns `None` on absence and the
  entry degrades gracefully to `last_assistant_message` + files touched.
- **LLM cost/latency.** Never on the SessionEnd path. Default off, short
  timeout, failure is non-fatal and always falls back to heuristics.
- **Transcripts leave the machine.** Enabling `handoff_llm_enabled` sends
  session excerpts (prompts, assistant messages, file paths) to a third-party
  provider. Default off; the Settings row and README must say this plainly, and
  the prompt should send excerpts rather than whole transcripts.
- **Env var invisible to the hook.** `MINIMAX_API_KEY` may not be in the hook's
  environment. Mitigated by design — LLM calls only ever run from the TUI and
  CLI. Worth a Keychain fallback later if hook-side summarization is ever
  wanted.

## Verification

1. **Unit tests** — `.venv/bin/pytest tests/test_transcript.py
   tests/test_handoff.py tests/test_llm.py -v`. Cover: fixture parsing,
   malformed-line tolerance, store round-trip, retention pruning,
   `latest_for_cwd` age cap, injection excluding the current session, and for
   `llm.py` — request shape per provider (mock `urllib`), missing-API-key
   error, HTTP error, MiniMax `base_resp` error inside an HTTP 200, and
   `summarize()` degrading to heuristics on every one of those.
2. **Live LLM smoke test** — with `MINIMAX_API_KEY` set,
   `.venv/bin/claude-monitor-handoff capture --llm` on a real session and
   confirm a sane `goal` / `stopping_point` / `next_steps`. Repeat with
   `handoff_llm_transport=claude_cli` to verify the fallback path.
3. **Hook in isolation** — pipe a synthetic `SessionEnd` payload into
   `.venv/bin/claude-monitor-hook` and assert an entry appears under
   `~/.config/claude-monitor/handoff/sessions/`. Pipe a `SessionStart` payload
   for the same `cwd` and assert `additionalContext` on stdout.
4. **CLI** — `.venv/bin/claude-monitor-handoff digest` after a real session.
5. **TUI** — per `CLAUDE.md`: bump `__version__` beta, restart the TUI by
   sending `q` to its iTerm2 pane via `.venv/bin/python3` + the iTerm2 Python
   API, then `curl -s 'http://localhost:17233/screenshot' -o /tmp/tui-check.png`
   and confirm both the bumped version in the status bar and the Hand-off tab
   rendering entries.
6. **End to end** — enable everything in Settings, work a session, exit it,
   start a fresh session in the same directory, and confirm the new session
   already knows where the previous one left off.
7. Before commit: drop the `-beta.X` suffix and sync `pyproject.toml`.
