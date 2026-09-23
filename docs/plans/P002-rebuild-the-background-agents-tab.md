# Rebuild the Background Agents tab

## Context

The "Background Agents" tab in `claude-monitor` is unusable. Seven symptoms were
reported: dozens of apparently-stale agents; no history in any agent pane; panes
that collapse until only a border shows; no scrolling or overflow; no indication
of a sub-agent's parent; no way to jump to the parent's iTerm2 pane; and no
sensible layout when there are many agents.

Exploration found these are not seven bugs. They are three defects plus one
missing abstraction:

1. **The tab is misnamed and holds the wrong thing.** `claude_monitor/tui.py:454`
   mounts it as a fallback bucket for *Claude sessions whose iTerm2 pane could
   not be identified* — not for sub-agents. The live instance on this machine
   holds ~30 such panels, all sessions running in
   `~/dev/claude-monitor/.Codex/worktrees/p001-007-manual-handoff`. Because these
   are sessions rather than agents, there is no parent to show and no agent
   history to render, so reported items 2, 5, 6 and the grouping half of 7 cannot
   be satisfied by the current content at all.

2. **One CSS defect explains three of the symptoms.**
   `SessionPanel { height: 1fr; }` (`claude_monitor/widgets/session_panel.py:33`)
   with no `min-height`, inside a plain `Vertical` (`tui.py:456`) whose Textual
   default is `overflow: hidden hidden`. N panels each receive
   `tab_height / N` rows. Each spends 2 rows on its border plus 1 on the docked
   `.panel-status`, so past roughly a dozen panels you see a border and nothing
   else and the inner `RichLog` resolves to height 0. History *is* being written
   (`tui.py:846`) — it is squeezed to nothing. And a `Vertical` can never
   overflow, because `1fr` children always shrink to fit first. Real iTerm2 tabs
   escape this only because `_apply_sizes` (`tui.py:688-711`) writes explicit
   percentage heights from the iTerm2 geometry; the background container has no
   size source and no bound on N.

3. **Nothing ages out.** `_prune_ended_session` (`tui.py:804-813`) is the only
   removal path and fires only on `SessionEnd`. The event log on this machine has
   4042 `SessionStart` against 324 `SessionEnd` — about 92% of sessions never
   emit one. The `[N]` badge formula at `tui.py:1049-1064` additionally counts
   real, live panes hidden by `iterm_scope` or `iterm_hide_empty_tabs` as
   background agents, inflating the number further.

4. **The real sub-agent data is already delivered on every event and is 100%
   unread.** Every `SubagentStart`/`SubagentStop` hook payload carries
   `agent_transcript_path`, `description`, `last_assistant_message`, `prompt_id`,
   the parent's `session_id` and `_iterm_session_id`, and a `background_tasks[]`
   array that is an authoritative per-agent status roster. Only `agent_id` and
   `agent_type` are consumed (`tui.py:951-957`). On disk, each sub-agent has
   `~/.claude/projects/<slug>/<parent_session_id>/subagents/agent-<agent_id>.jsonl`
   plus a `.meta.json` carrying `agentType`, `description`, `toolUseId`,
   `spawnDepth` and `requestShape`. No code path reads any of it.

### Measured payload shapes (these drive the design)

Taken from the 20,000 most recent lines of `/tmp/claude-auto-accept/events.jsonl`:

- `SubagentStop` (n=2266) carries `agent_transcript_path` and
  `last_assistant_message` at 100%, and `background_tasks` at 98.6%. Roster
  entries are exactly `{id, type, status, description, agent_type}`.
- `SubagentStart` (n=303) carries **none** of those — no
  `agent_transcript_path`, no `last_assistant_message`, no `background_tasks`,
  no description. Only `session_id`, `transcript_path` (the parent's), `cwd`,
  `agent_id`, `agent_type`, `_iterm_session_id`, `_timestamp`, usually
  `prompt_id`.
- So for a *still-running* agent, description and history are not in the event
  stream. Both are recoverable from disk: the path is always
  `<dir of parent transcript_path>/<parent_session_id>/subagents/agent-<agent_id>.jsonl`
  (verified), and the sibling `agent-<agent_id>.meta.json` (verified to exist)
  carries `description`, `agentType`, `toolUseId`, `spawnDepth`, `requestShape`.
- Stops outnumber starts 2181 unique ids to 212 — but this is a **population**
  difference, not unreliability. Measured over the whole 17 MB log: of the 191
  distinct `background_tasks[]` entries with `type == "subagent"`, **191 have a
  `SubagentStart`. Zero counterexamples.** `SubagentStop` additionally fires for
  every *inline* `Agent` call, and those carry `agent_type == ""` (1637 of the
  stops), no `.jsonl`, no `.meta.json`, and an empty roster. Inline agents do not
  belong in a Background Agents tab, so this is a filter to apply, not a leak to
  fix.
- The roster also carries non-agents: 105 of 296 distinct roster ids are
  `type: "shell"` with a `command` key and 9-char ids. Excluded, but tracked so
  reconciliation doesn't mistake them for missing agents.
- `background_tasks[].status` is **always** the literal `"running"` — all 4026
  roster entries ever logged. Presence/absence is the only usable signal.
- The roster on an agent's own `SubagentStop` is a pre-stop snapshot — the
  stopping agent still shows `running`. Treat the id named by a stop event as
  finished regardless, and treat absence from a *later* roster for the same
  parent as the terminal signal.
- `.meta.json` is written **after** `SubagentStart` fires — measured at +16 ms to
  +29 ms across five live agents. A synchronous meta read in the start handler
  races and loses, so `description` and `spawnDepth` must be deferred-fill.
- The derived sidechain path holds for 2239 of 2270 cases (98.6%). All 31
  mismatches are **Codex** writing `SubagentStop` events into the same events
  file, with paths under `~/.codex/sessions/`. Gating every derived path through
  `transcript._within_projects_dir` (`transcript.py:88`) satisfies containment
  and drops the Codex population for free.
- The 50-line replay window covers only ~9 minutes of wall-clock at current
  volume, which a background agent routinely outlives.

### Separate finding worth acting on independently

In those same 20,000 lines, 2309 of 4049 `SessionStart` events came from
`~/dev/claude-monitor/.Codex/worktrees/p001-007-manual-handoff`, and that
project's transcript directory holds 2265 JSONL files. Something is spawning
Claude sessions in that Codex worktree in bulk. That churn — not sub-agents — is
what fills the tab today. Fixing the tab will stop it *displaying* badly, but the
churn itself is a separate problem outside this plan's scope.

**Intended outcome.** The tab becomes a genuine background sub-agent view: a
two-column list-and-detail layout listing live agents grouped by their parent,
showing each agent's prompt/tool/output history on the right, with stale and
completed agents reconciled away and a key to jump to the parent's iTerm2 pane.
The orphaned session panels move to their own tab with a container that scrolls
and panels that cannot collapse.

## Decisions taken

- **Content:** true sub-agent view built from hook payloads. Orphan session
  panels move to a separate "Unmatched" tab.
- **Layout:** two-column list + detail, one agent viewable at a time, left list
  grouped by parent.
- **Staleness:** reconcile against `background_tasks[]` on every event (no TTL
  guessing) plus an idle-age sweep; completed and stale agents hidden by default,
  revealable by a key.

## Slicing rule

Quoted verbatim from the user's standing instruction, and applied below:

> **Default: every PR is cut from the repo's default branch and targets it.** A
> PR is "independently shippable" only if it can be reviewed and merged on its
> own, in any order relative to my other open PRs, and contains working
> reviewable code (not a bare migration or empty stub). Never slice a single
> function across PRs.

This repo is under `~/dev`, so no `gh pr create` runs — the slicing and
one-branch-per-slice discipline still applies. Resolve the default branch with
`git fetch` then `git symbolic-ref --short refs/remotes/origin/HEAD` (verified:
`origin/main`) and cut every slice branch from that ref, never from another
slice branch.

> **Blocker to settle first.** `main` currently carries 9 uncommitted files of
> unrelated hand-off work (`cli_handoff.py`, `settings.py`, `install.py`,
> `CHANGELOG.md`, 5 test files) and is 2 commits ahead of `origin/main`. Clean
> slice branches cannot be cut while those sit in the tree. This was raised and
> not yet answered; resolve it before the first branch.

## Design decisions that the two design passes disagreed on

Recorded because the implementer will otherwise re-litigate them:

1. **`min-height` alone does not fix the collapse.** `height: 1fr` means "share of
   available space", so it can never overflow — N panels at `1fr` inside a
   `VerticalScroll` reproduce the bug plus a scrollbar that never appears.
   `height: auto` is equally dead because `SessionPanel RichLog` is itself `1fr`
   (`session_panel.py:41-45`), so auto resolves to ~0. The fix is an **explicit
   row height** scoped to the new container; `min-height` is only the regression
   guard.
2. **Shell tasks are excluded from the list.** `background_tasks[]` carries
   `type: "shell"` entries (105 distinct ids) with a `command` key instead of
   `agent_type`, and none emit `Subagent*` hooks. They are tracked so
   reconciliation doesn't mistake them for missing agents, and surfaced only as a
   count in the summary line.
3. **The tab lists all genuine sub-agents, not only `requestShape: "background"`
   ones.** Only 112 of 208 meta files carry that marker; filtering on it would
   hide 96 real sub-agents that have full transcripts. Background ones get a
   badge instead of being the sole admission criterion.
4. **Staleness threshold comes from `Settings`, not a widget constant** — one
   value, clamped, default 900s.
5. **Grouping uses real nesting where it exists.** `parentAgentId` is present on
   exactly the 28 `spawnDepth: 2` meta files, so a sub-agent spawned by another
   sub-agent groups under its parent *agent*; everything else groups under its
   parent *session*.

## Registry interface (the contract between the two layers)

Agreed shape, reconciling both designs:

- `generation() -> int` and `agents() -> list[BackgroundAgent]` must be
  **in-memory and O(n)** — the panel polls them every 2s on the event loop.
- `history(agent_id, *, limit=500) -> list[HistoryEvent]` **may do file I/O**.
  The panel calls it from `@work(thread=True, exclusive=True, group="agent-history")`
  so arrowing past a row cancels the in-flight read.
- `HistoryEvent.detail` is **plain text**; the panel applies `rich.markup.escape`.
  Tool details contain `[` from paths and JSON, which would otherwise corrupt
  markup.
- Also exposed: `get(agent_id)`, `for_parent_session(sid)`, `counts()`,
  `shell_task_count()`.

## Steps

### Step 1 — Unmatched tab and the panel-collapse fix

Fixes reported items 3 and 4, and is the only step that fixes a user-visible
symptom on its own. No dependency on anything below.

Rename `BACKGROUND_AGENTS_TAB_ID`/`BACKGROUND_AGENTS_CONTAINER_ID`
(`tui.py:256-257`) to `UNMATCHED_TAB_ID = "tab-unmatched"` /
`UNMATCHED_CONTAINER_ID = "unmatched-container"`. Change `tui.py:454-459` to
`TabPane("Unmatched", VerticalScroll(id=...), id=...)`, adding `VerticalScroll`
to the `textual.containers` import at `tui.py:20`. Add CSS to
`AutoAcceptTUI.CSS` beside the `RichLog` block (`tui.py:218-224`) giving
`#unmatched-container` `height: 1fr; min-height: 3` plus the scrollbar
convention, and `#unmatched-container SessionPanel` an explicit
`height: 12; min-height: 12; width: 1fr` — 2 border + 1 `.panel-status` +
1 `.countdown-bar` + 8 log lines. Scoping to the container keeps real iTerm2
tabs on `_apply_sizes`' percentage heights (`tui.py:688-711`).

Retarget `_resolve_panel`'s mount (`tui.py:786`) and **delete its
`#layout-root` escape hatch** — mounting a bare `SessionPanel` into the root
corrupts the real tab tree; fall back to the Unmatched `TabPane` itself, else
drop the panel and log. Move the tab-label block (`tui.py:1055-1062`) to the new
id, keeping the `[n]` suffix.

Tests — `tests/test_unmatched_tab.py`, harness B (bare `AutoAcceptTUI`,
`_layout_tabs` empty, per `test_phantom_panel_prevention.py:338-366`):
`test_fallback_panel_mounts_into_unmatched_container`,
`test_fallback_panel_never_mounts_into_layout_root`,
`test_fallback_panel_dropped_when_container_missing`. The discriminating
regression test needs a real mount: `test_thirty_panels_keep_min_height` mounts
30 panels at `size=(120, 40)` and asserts every `panel.size.height >= 12` **and**
`container.virtual_size.height > container.content_region.height` — the second
assertion is what proves the container overflows rather than clipping, which is
exactly what `1fr` or `auto` would fail. Update the two container-id assertions
in `tests/test_phantom_panel_prevention.py`.

### Step 2 — Fix the inflated tab badge

Standalone bug fix; no dependency on the registry. `tui.py:1049-1064` counts
*panels* absent from `_tab_session_ids`, and that map holds only tabs surviving
both `filter_tabs_by_scope` and `filter_tabs_hide_empty`. A real pane hidden by
`iterm_hide_empty_tabs` or excluded by `iterm_scope` is therefore counted as
background. Widen the denominator using the sets already refreshed every 3s at
`tui.py:296-303`:

```
known_real = real_tab_sids | self._out_of_scope_iterm_sids
                           | self._hidden_tab_iterm_sids
                           | self._removed_iterm_sids
```

Also fix the pre-existing `per_tab_width` off-by-one at `tui.py:1030`, which
divides by `num_tabs + 1` while there are two fixed panes (Hand-off was never
counted) — and three once the Unmatched tab exists.

Tests — `tests/test_tab_badge_counts.py`, harness B with `tc.get_tab` mocked:
`test_hidden_empty_tab_pane_not_counted_as_unmatched` (the reported bug),
`test_out_of_scope_pane_not_counted_as_unmatched`,
`test_removed_pane_not_counted_as_unmatched`,
`test_genuinely_unmatched_fallback_panel_is_counted`,
`test_per_tab_width_accounts_for_all_fixed_tabs`.

### Step 3 — Sub-agent test fixtures and payload factory

Split out because every later step consumes it, it touches only `tests/`, and it
unblocks Steps 4, 5, 6, 8, 9 and 10 in parallel.

Extend `conftest.py`'s `_make_subagent_event` (lines 131-149) with `cwd`,
`transcript_path`, `agent_transcript_path`, `background_tasks` and `prompt_id`
kwargs, keeping the current signature backward-compatible so existing callers
don't break. Note its current default is `agent_type="general_purpose"` with an
underscore, while real values are hyphenated (`lean-agents:main`,
`general-purpose`) — fix that too. Add a `make_subagent_event` fixture alongside
the existing `make_permission_event`/`make_notification_event` pair
(`conftest.py:145-160`), which today omits it.

Add `tests/fixtures/` payloads captured from the real event log: one
`SubagentStart`, one `SubagentStop` with a two-entry roster, one
`agent_type: ""` stop, one shell-task roster, one Codex-path stop, and a
truncated sidechain `.jsonl` + `.meta.json` pair.

> **Scrub before committing.** These are captured hook payloads that reference
> real local project paths and real session content. Rewrite every `cwd`,
> `transcript_path` and `agent_transcript_path` to `/tmp/test-project/...` and
> strip `last_assistant_message` bodies down to short synthetic text. The
> fixtures are git-tracked, so anything left in them is committed.

### Step 4 — The agent registry

New module `claude_monitor/agents.py`, deliberately free of any Textual import so
it is testable without a harness. Holds `BackgroundAgent` (dataclass),
`AgentRegistry`, and `is_background_candidate`. Status is a plain `str` with
module constants (`running`/`completed`/`gone`/`stale`), matching
`SessionPanel._state`'s existing convention rather than introducing the codebase's
first `Enum`.

`BackgroundAgent` fields and their sources: `agent_id` (registry key);
`parent_session_id`, `parent_iterm_sid` (via `extract_iterm_session_id`,
`__init__.py:35`), `cwd`, `parent_transcript_path` — all 100% present on both
event types; `agent_type` with precedence roster → meta `agentType` → payload
**if non-empty**; `description` from roster → meta; `parent_agent_id` from meta
`parentAgentId`; `spawn_depth`, `request_shape`, `request_non_interactive`,
`tool_use_id` from meta; `last_assistant_message`, `effort_level`,
`permission_mode` from the stop payload; `status` **derived, never read from
`background_tasks[].status`**; `started_at`, `started_at_is_estimate`,
`last_activity_at`, `ended_at`, `agent_transcript_path`, `first_seen_source`,
`meta_loaded`, `meta_attempts`, `admitted`.

**Admission filter** — the function that keeps 1637 inline agents out of the tab.
Admit when the id appeared in a roster entry with `type == "subagent"`, or a
`SubagentStart` was seen with `agent_type` non-empty and not `"default"`, or the
sibling meta file exists. Reject `agent_type == ""` (inline), `"default"` (all 52
UUID-shaped ids, no meta, never in a roster), `type == "shell"`, and any record
whose `parent_transcript_path` fails `transcript._within_projects_dir`
(`transcript.py:88`) — which drops the Codex population for free.

**`SubagentStart`** creates-or-updates and derives the sidechain path, but
**must not touch the filesystem** — meta lands 16-29 ms later, so the id goes on
`_pending_meta` for deferred fill. **`SubagentStop`** does three things in order:
self-retire the named `agent_id` (authoritative, overriding that same payload's
pre-stop roster); seed siblings from the roster; then reconcile — only when
`background_tasks` is *present* as a key, since a missing key means no
information while an empty list means nothing running. Reconciliation skips
records whose `last_activity_at` is within a 5s grace window.

**Named invariant:** roster reconciliation can never retire a parent session's
last agent, because a roster only arrives on some *other* agent's stop. When the
last one dies without a stop, only the sweep retires it — which is why the sweep
is mandatory.

**Sweep** (`sweep(now)`), throttled to 5s: deferred meta fill (capped at 3
attempts); age out `running` records past `agent_stale_after_secs`, but first
refresh `last_activity_at` from the sidechain file's mtime, because an agent that
is thinking hard emits no hook events for minutes while still appending to its
transcript; evict terminal records past `agent_retain_finished_secs`; hard-cap at
500 records with a bounded `_retired_ids` deque (maxlen 2000) so a late duplicate
stop cannot resurrect an evicted agent. **Do not use `session_pids`** for
liveness — it keys on the *parent's* pane, so it would wrongly reap agents whose
parent merely moved out of `iterm_scope`.

New `Settings` fields (`settings.py:52-88`, following the `handoff_*` clamping
convention in `__post_init__`): `agent_stale_after_secs = 900` (clamp
60..86400), `agent_retain_finished_secs = 600` (same clamp),
`agent_show_finished = False`.

Tests — `tests/test_agent_registry.py`, plain pytest with an injected clock. The
ones that pin the measured traps: `test_empty_agent_type_stop_is_not_admitted`,
`test_default_agent_type_is_not_admitted`,
`test_shell_roster_entry_is_not_admitted_but_is_counted`,
`test_codex_transcript_path_is_rejected`,
`test_stop_self_retires_despite_own_roster_listing_it_running`,
`test_stop_without_background_tasks_key_does_not_reconcile`,
`test_stop_does_not_overwrite_known_agent_type_with_empty_string`,
`test_start_does_not_read_meta_json_synchronously`,
`test_last_background_agent_needs_sweep_not_roster`,
`test_sweep_refreshes_activity_from_sidechain_mtime`,
`test_retired_id_is_not_resurrected_by_late_duplicate_stop`,
`test_ingest_is_idempotent_for_replayed_line`,
`test_mixed_id_shapes_are_all_keyed_opaquely`. Settings clamping extends the
pattern in `tests/test_settings_handoff.py`.

### Step 5 — The lazy history reader

New module `claude_monitor/subagent_transcript.py` exposing
`read_history(path, *, max_turns=200, projects_root=None) -> SubagentHistory`,
plus `SubagentTurn`. Independent of Step 4 — it takes a path and returns a
dataclass.

**A focused reader, not a generalization of `transcript.py`.** That module's
`_extract` (`transcript.py:229-307`) is built around a *parent* transcript's
vocabulary — `ai-title`, `last-prompt`, `file-history-snapshot`,
`promptSource == "typed"`, and `pending_agent_calls` keyed on `Agent` tool_use
blocks — none of which appear in a sidechain. Measured entry types in one live
453 KB sidechain: `attachment: 96, assistant: 37, user: 24`. Generalizing would
mean threading a mode flag through three functions and re-testing the 1127-line
hand-off path for no gain.

Reuse by import, don't copy: `_read_tail_lines`/`_read_head_lines`
(`transcript.py:157-183`), `_parse_timestamp` (185), `_first_text_block` (204),
`_tool_result_text` (216), `_within_projects_dir` (88), `_SAFE_SESSION_ID` (29),
`_EDIT_TOOL_NAMES` (34). `projects_root` is injectable because
`conftest.py`'s `isolated_state` does **not** patch
`transcript.CLAUDE_PROJECTS_DIR`, so a `tmp_path` fixture would otherwise fail
containment.

Extraction: read the sibling `.meta.json` first; filter on `isSidechain: true`;
take the `parentUuid: null` user entry as `initial_prompt`; walk
`message.content` for `text` and `tool_use` blocks; attach `is_error` from
`toolUseResult`; **skip `attachment` entries** — 96 of 157 in the live file, and
not skipping them is the single easiest way to make the detail pane unreadable.

Four cost bounds: read whole under 256 KB, else head 64 KB plus tail 512 KB with
`truncated = True` (live files measured 404-676 KB); cap turns; an
`(mtime, size)`-validated cache bounded to 32 entries; and a docstring contract
that it is **never called from the message thread**. Failures return a record
with `error` set and `turns = []` — never raise, never `None`.

Tests — `tests/test_subagent_transcript.py`, plain pytest with
`projects_root=tmp_path`: `test_attachment_entries_are_skipped`,
`test_non_sidechain_entries_are_skipped`,
`test_initial_prompt_from_parent_uuid_null_entry`,
`test_meta_json_fields_are_merged`, `test_missing_meta_json_degrades_gracefully`,
`test_large_file_reads_head_and_tail_and_sets_truncated`,
`test_turns_are_capped_at_max_turns`, `test_tool_error_flag_from_tool_use_result`,
`test_cache_hit_on_unchanged_mtime`, `test_cache_miss_after_append`,
`test_path_outside_projects_root_returns_unsafe_path`,
`test_agent_id_with_traversal_is_rejected`,
`test_malformed_json_lines_are_skipped`.

### Step 6 — Wire ingest, backscan and sweep

Needs Step 4 merged to compile. Assign `self.agents = AgentRegistry(...)` in
`MonitorApp.__init__` (`app_base.py:138-170`), right after `self._session_meta`
at line 169. That placement is the survival argument for the layout wipe:
`on_layout_changed` (`tui.py:565-646`) mutates only `panels`, `dashboard`, and
six `tui.py`-local attributes — it never touches a `MonitorApp` attribute, just
as it never touches `_session_meta`. Pin that with a test rather than trusting it.

Call `self.agents.ingest(data)` as the **first statement** of `on_hook_event` in
both `tui.py:815` and `tui_simple.py` — above the `if self._rebuilding: return`
guard and above the `_is_dashboard_event` branch at `tui.py:832`. With a 3s
layout poll, ingest placed after either guard silently drops events.
`self._record_session_meta(data)` must move above the rebuild guard for the same
reason. Add `self.agents.sweep()` to `_tick_status` in both apps
(`tui.py:486-492`, `tui_simple.py:358`); the registry's own throttle means the
1s tick costs a clock comparison four ticks out of five. Deliberately not a new
worker thread — single-threaded mutation is what keeps this synchronously
testable.

Add a bounded **tail backscan** to `MonitorApp.watch_events`
(`app_base.py:523-555`), mirroring the pre-scan `tui_simple.py:395-412` already
does and `tui.py` lacks. Before the 50-line replay, read the last
`AGENT_BACKSCAN_BYTES = 1 MB` with `transcript._read_tail_lines` (which already
handles the partial leading line) and feed `registry.seed_from_lines()`, then run
one `sweep(now)`. Measured: 1 MB ≈ 1092 lines ≈ 5 hours and 279 sub-agent
events, against a 50-line window that covers only ~9 minutes. The registry is
then correct on frame one. Expose registry state through `get_state_snapshot`
(`app_base.py:197`) so `curl localhost:17233/text` shows live agents with zero UI
work — which is what makes this step independently verifiable.

Tests — `tests/test_agent_ingest_wiring.py`:
`test_ingest_runs_while_rebuilding` (harness B, set `app._rebuilding = True`),
`test_ingest_runs_for_dashboard_session_event`,
`test_registry_survives_layout_rebuild` (populate, `app.panels = {}`, assert
`agents.live()` unchanged), `test_watch_events_backscan_seeds_registry`,
`test_backscan_is_bounded_to_configured_bytes`,
`test_tick_status_drives_sweep` (harness A), `test_state_snapshot_includes_agents`.

### Step 7 — iTerm2 session activation

Independently valuable and needs nothing above. Add `SessionActivator` to
`claude_monitor/iterm2_layout.py` directly after `KeystrokeSender` (line 401),
following its exact shape but returning `_iterm2_call(_do)` **without** the
`or False` — `None` means transport failure and `False` means session-not-found,
and collapsing them loses the only distinction the notification needs. One call
to `session.async_activate(select_tab=True, order_window_front=True)`
(`.venv/.../iterm2/session.py:622`) covers pane → tab → window, so
`tab.async_select`, `window.async_activate` and `App.async_activate` are all
unnecessary — we already run inside iTerm2.

Add one line to `_record_session_meta` (`app_base.py:576-594`):
`meta["iterm_session_id"] = extract_iterm_session_id(data.get("_iterm_session_id") or "")`.
Safe and cheap — that key is present on 100% of both event types. Do **not** read
`_iterm_to_panel` for this: its values are iTerm2 sids for matched sessions
(`tui.py:733`) but Claude session ids for fallback panels (`tui.py:780`), so they
are ambiguous.

Make the step user-visible on its own by binding `j`/`action_jump_to_pane` on
`SessionPanel`, focusing a monitored pane's real iTerm2 pane from its panel.
Route every `activate()` call through `@work(thread=True)` — `_iterm2_call`
blocks the caller up to ~15s (5s `_iterm2_ready.wait` plus 10s `future.result`,
`iterm2_layout.py:83-99`), which would freeze the TUI from an action handler.

Tests — `tests/test_iterm_activate.py`, monkeypatching `_iterm2_call` to drive
the inner coroutine against a fake app:
`test_activate_calls_async_activate_with_tab_and_window`,
`test_activate_returns_false_when_session_missing`,
`test_activate_returns_none_when_transport_unavailable`,
`test_record_session_meta_stores_iterm_session_id`.

### Step 8 — Parent linkage resolution

Needs Step 4. Add an `AgentParent` dataclass and
`MonitorApp.resolve_agent_parent(agent) -> AgentParent` returning
`claude_session_id, iterm_sid, panel_key, tab_id, tab_name, reachable, reason`,
so the panel never queries the app directly. The `MonitorApp` base implementation
is the `SimpleTUI` case (`panels` keyed by Claude session id, per
`app_base.py:144-146`, no tabs). Override in `AutoAcceptTUI`, which owns the
maps: `panel_key` from `_iterm_to_panel` (`tui.py:281`, which survives rebuilds
because `on_layout_changed:642` filters rather than clears it); `tab_id` from a
lazily-built reverse index over `_tab_session_ids` (`tui.py:289`), invalidated
in `on_layout_changed` after line 432; `tab_name` from `_tab_original_names`.

`reason` classifies in priority order — `ok`, `out_of_scope`, `removed`,
`hidden_tab`, `no_panel`, `unknown` — using the sets `tui.py` already maintains.
Two hard constraints, both departures from `_resolve_panel`: the resolver is
**read-only** (it never creates or mounts a panel, because it holds data rather
than widgets, so an agent is never dropped merely because its parent is
unreachable), and it must **not** call `self._do_refresh()` the way
`_resolve_panel:750-757` does for the hidden-tab case — resolution runs on every
render, and refreshing from a render path loops.

Tests — `tests/test_agent_parent_linkage.py`, harness B:
`test_out_of_scope_parent_is_classified_not_dropped`,
`test_removed_pane_parent_is_classified_not_dropped`,
`test_hidden_tab_parent_does_not_trigger_refresh` (assert `_do_refresh` never
called), `test_resolve_never_mounts_a_panel` (`app.mount` is a `MagicMock`),
`test_reverse_tab_index_invalidated_on_layout_change`,
`test_simple_tui_base_resolution_uses_claude_session_id`.

### Step 9 — The two-column Background Agents panel

Needs Steps 4, 5, 6 and 8. New widget `BackgroundAgentsPanel(Horizontal)` in
`claude_monitor/screens/agents.py`, following `screens/handoff.py`'s placement
and its "a broken store must not kill the tab" error discipline (`handoff.py:108`).

Left: a `Vertical` holding a one-row `#agents-summary` `Static` and a
`ListView`. Right: a `Vertical` holding a two-row detail header and a
`RichLog(markup=True, wrap=True, highlight=False)`. `#agents-left` is
`width: 42%; min-width: 28; border-right: solid $primary`; `#agents-right` is
`width: 1fr; padding: 0 1`; the panel itself is `height: 1fr; min-height: 10`
(safe because a *single* `1fr` child under the `auto` `TabPane` takes the whole
region — the collapse only happens at N>1).

`RichLog` over `Markdown` or a `VerticalScroll` of `Static`s because
`Markdown.update()` remounts a widget tree per call — arrowing down a 30-agent
list would remount hundreds of widgets per second — while `RichLog` bounds memory
via `max_lines`, inherits the global scrollbar CSS (`tui.py:218-224`), and speaks
the same Rich-markup vocabulary as `formatting.py:194-204`. Two gotchas:
`wrap=True` here, unlike `SessionPanel`'s `wrap=False`, because prompts are
prose; and `RichLog` has no replace-contents, so switching agents is `clear()`
then re-write.

`on_mount` sets a 2s interval that compares `registry.generation()` and returns
early when unchanged. On change, rebuild rows from a **pure** `build_rows(...)`,
then restore the cursor to the previously selected `agent_id`, else the first
agent row, **setting `lv.index` explicitly**.

Row label is the `description` (present on 208/208 meta files, a 3-6 word human
phrase), width-tiered in three steps: `glyph desc type age ← parent` at ≥60
columns, dropping the parent below 60, and `glyph desc` below 40. Fallback chain
`description → agent_type + id[:8] → id[:8]`. Status glyphs reuse
`SessionPanel._render_state_badge`'s vocabulary.

**Group headers are `ListItem(..., disabled=True)`,** which works because
`action_cursor_down`/`_up` skip disabled nodes and a disabled `ListItem` gets no
automatic dimming (`can_focus=False` defeats Textual's
`*:disabled:can-focus { opacity: 0.7 }`), so `.agent-group-header` fully controls
appearance. No tree widget needed; `ListView` already *is* a `VerticalScroll`.
Emit a header only when its group has a visible row, and never trailing.

> **Four `ListView` traps mean "copy `HandoffPanel` verbatim" is wrong.**
> `watch_index` posts `Highlighted(self, None)` for a disabled node while
> `event.list_view.index` still points at the header, so the handler must guard
> on `event.item is None` **and** on the row's kind — `handoff.py:144-148` reads
> only the index and would render the wrong agent. `validate_index` clamps
> without skipping disabled, and `action_cursor_down` sets `index = 0`
> unconditionally when index is `None`, and row 0 is always a header.
> `_on_list_item__child_clicked` sets the index from a click with no disabled
> check, so mouse clicks can land on headers. And `refresh_entries()` copied
> as-is jumps to entry 0 — harmless for Hand-off, which refreshes on demand, but
> this panel rebuilds on a timer and would yank the detail pane mid-read.

Sort order is a stability contract so the poll doesn't churn: groups by
`max(last_activity_at)` desc then parent label; within a group running → idle →
finished, then `last_activity_at` desc; every comparator ends in an `agent_id`
tie-break so identical snapshots produce identical orders.

Stale reveal is `e`/`action_toggle_stale` on `BackgroundAgentsPanel.BINDINGS`
(widget-scoped, so neither app's `BINDINGS` list changes and
`test_keyboard_actions.py:17-34`'s hardcoded `expected_keys` stays green).
Revealed rows get `.agent-row-stale` and a `✓`/`✗` glyph. The summary line is the
affordance: `3 running · 27 finished hidden — e to show`. Two distinct empty
states, each a disabled `ListItem`, mirroring `handoff.py:120-123`.

The global "focus this tab" action `b`/`action_show_agents` is the one thing
needing the three-place dance: both `BINDINGS` lists plus a
`("Show Background Agents Tab", "show_agents")` entry in
`MonitorCommands.COMMANDS_LIST` (`commands.py:9-22`). No `_KEY_DISPLAY` entry —
`b` is a plain letter. Reuse `action_show_handoff`'s parent-walk
(`app_base.py:666-686`) to switch `TabbedContent.active`.

**Close the help-screen gap in this same step:** `app_base.py:476` hardcodes
`HelpScreen(self.BINDINGS, SessionPanel.BINDINGS)`, so widget-level bindings on a
new panel would never appear in help. Change it to include
`BackgroundAgentsPanel.BINDINGS`, and give every new binding a non-empty
description — `_extract_bindings` (`help.py:150-166`) silently skips empty ones.

**Mount in both apps.** `AutoAcceptTUI` via `_mount_tabs`, and `SimpleTUI` in
`on_mount` (`tui_simple.py:320-325`) `before=self.HANDOFF_TAB_ID`, extending the
`before=` chain at `tui_simple.py:514` so new session tabs insert before it too.
`SimpleTUI` tracks `SubagentStart`/`Stop` identically (`tui_simple.py:617-622`),
and — decisively — `conftest.py`'s `app_fixture` builds `SimpleTUI`, so mounting
there makes the panel pilot-testable under harness A instead of MagicMock-only
under harness B. (The *Unmatched* tab stays `AutoAcceptTUI`-only: `SimpleTUI`
gives every session its own `TabPane` and has no orphan concept.)

Tests — `tests/test_agents_panel.py`, **pure helpers only, never mounting**,
mirroring `test_handoff_tui.py`: `test_groups_by_parent`,
`test_header_emitted_once_per_group`, `test_no_header_for_empty_group`,
`test_never_ends_with_header`, `test_stale_hidden_by_default`,
`test_nested_agent_grouped_under_parent_agent` (the 28 `spawnDepth: 2` cases),
`test_sort_stable_across_identical_snapshots`,
`test_falls_back_to_agent_id_when_description_missing`,
`test_narrow_width_drops_type_and_parent`,
`test_escapes_square_brackets_in_detail` (markup-corruption guard).
Plus `tests/test_agents_panel_mounted.py`, harness A at `size=(120, 40)`, whose
four tests exist specifically to pin the `ListView` traps:
`test_highlight_on_header_does_not_change_detail`,
`test_click_on_header_bounces_to_next_agent_row`,
`test_cursor_restores_to_same_agent_after_refresh`,
`test_toggle_stale_key_reveals_rows`. Update
`tests/test_command_palette.py` and `tests/test_handoff_tui.py:207` for the new
`COMMANDS_LIST` entry.

Pure helpers deliberately factored out so they test without mounting:
`build_rows`, `agent_row_label`, `status_glyph`, `history_line`, `_relative`,
`group_sort_key`, `agent_sort_key`.

### Step 10 — Jump to the parent's pane

Needs Steps 7, 8 and 9. Bind `j`/`action_jump_to_parent` on
`BackgroundAgentsPanel.BINDINGS`. Factor the decision into a pure
`resolve_jump_target(agent, *, self_iterm_sid, tracked_sids, out_of_scope_sids)`
returning `(kind, iterm_sid, message, severity)`, so the whole matrix tests with
no Textual and no iTerm2:

- no recorded parent iTerm2 sid → no call, warn that it was never recorded
- parent is the monitor's own pane (`== _self_session_id`, cf. `tui.py:802`) → no
  call; activating our own pane steals focus for nothing
- parent is a pane in this same TUI → **both**: switch `TabbedContent.active` to
  that `SessionPanel`'s `TabPane` and focus it (instant, no websocket) *and*
  activate the real iTerm2 session, since the monitor is itself only a pane
- parent out of `iterm_scope` → still activate (the pane exists, it just isn't
  mirrored) but notify, so the jump isn't a surprise
- `activate()` returns `False` → parent pane is no longer open
- `activate()` returns `None` → iTerm2 not reachable
- `ITERM2_AVAILABLE` false (`iterm2_layout.py:29-34`) → no call, warn

Tests — `tests/test_agents_jump.py`: one pure test per matrix row, plus
`test_jump_key_invokes_activator` under harness A with
`SessionActivator.activate` monkeypatched.

## Branches

Checkpoint 1 of the slicing rule — the slice list with one branch each, all cut
from the resolved default branch (`origin/main`, confirmed via
`git symbolic-ref --short refs/remotes/origin/HEAD`) after a `git fetch`. Never
from another slice branch, and never from whatever the working tree has checked
out.

| Step | Branch | Base |
|---|---|---|
| 1 | `ct/unmatched-tab-scroll` | `origin/main` |
| 2 | `ct/fix-agent-tab-badge` | `origin/main` |
| 3 | `ct/subagent-test-fixtures` | `origin/main` |
| 4 | `ct/agent-registry` | `origin/main` |
| 5 | `ct/subagent-transcript-reader` | `origin/main` |
| 6 | `ct/wire-agent-ingest` | `origin/main` |
| 7 | `ct/iterm-session-activator` | `origin/main` |
| 8 | `ct/agent-parent-linkage` | `origin/main` |
| 9 | `ct/background-agents-panel` | `origin/main` |
| 10 | `ct/agent-jump-to-parent` | `origin/main` |

Steps 1, 2, 3, 4, 5 and 7 are cuttable immediately and mergeable in any order —
their file sets are disjoint or purely additive. The rest carry genuine
compile-level dependencies, named here rather than asserted: Step 6 imports
`claude_monitor/agents.py`, so on a base without Step 4 it fails to import; Step
8 takes a `BackgroundAgent` parameter, same failure; Step 9 calls the registry
and `resolve_agent_parent`; Step 10 calls `SessionActivator` and
`resolve_jump_target`. Each of those is cut from `origin/main` *after* its
dependency merges, not stacked on the dependency's branch — stacking would put
the earlier diff inside the later one and force the chain.

Not dependencies, and not treated as such: the order the exploration happened
in, the narrative sequence of the steps, or reviewer convenience. Steps 1 and 2
both touch `tui.py` but in disjoint blocks — pane construction versus label
computation — so whichever lands second rebases trivially and neither blocks the
other.

Note the `ct/` prefix is the user's habit rather than a requirement here: that
rule is scoped to repos under `~/workspace`, and this repo is under `~/dev`.

> **Still unresolved.** The 9 uncommitted files and 2 unpushed commits on `main`
> block cutting any of these cleanly. Nothing below starts until that is settled.

## Verification

Per the project's mandatory loop in `CLAUDE.md`:

1. Bump `__version__` in `claude_monitor/__init__.py` (currently `1.1.1`;
   the running instance reports `1.1.2`, so reconcile before bumping).
2. Restart the TUI by sending Escape then `q` to its iTerm2 pane via
   `.venv/bin/python3` and the iTerm2 Python API.
3. Capture a screenshot and verify the bumped version in the top-right status
   bar.

> **The PNG screenshot needs a TUI restart to start working.** `cairo` is now
> installed (`/opt/homebrew/Cellar/cairo/1.18.4`, installed 2026-09-18 16:13) and
> `import cairosvg` succeeds in a fresh `.venv` shell, but the running TUI
> process started 2026-09-17 16:38 — before the install — and still returns HTTP
> 503 with a `dlopen` error from `generate_screenshot_png`
> (`claude_monitor/api.py:81-97`). No code change is needed; the first restart in
> the verify loop picks cairo up. Until then use
> `curl -s 'http://localhost:17233/screenshot?format=svg'`.
> `curl -s http://localhost:17233/text` returns a JSON state dump of every
> session and is the fastest way to confirm panel counts.

Test suite: `./test.sh` (wraps `.venv/bin/python -m pytest`). Note
`pyproject.toml` omits `claude_monitor/tui.py` and `iterm2_layout.py` from
coverage, so `tui.py` changes need explicit tests in the
`tests/test_phantom_panel_prevention.py` bare-instance style.

Load the `pre-push-check` skill before pushing. Because this repo is under
`~/dev` with no CI, only the local `fresheyes` review applies — skip the post-PR
Fresh Eyes watch.

## Tasks associated with P002

### #011 — Unmatched tab and the panel-collapse fix

Status: `in_progress`

---

### #012 — Fix the inflated tab badge

Status: `in_progress`

---

### #013 — Sub-agent test fixtures and payload factory

Status: `pending`

---

### #014 — The agent registry

Status: `pending`

---

### #015 — The lazy history reader

Status: `pending`

---

### #016 — Wire ingest, backscan and sweep

Status: `pending`

---

### #017 — Parent linkage resolution

Status: `pending`

---

### #018 — Two-column Background Agents panel

Status: `pending`
