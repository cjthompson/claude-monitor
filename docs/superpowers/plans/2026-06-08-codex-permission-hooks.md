# Codex Permission Hooks Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add optional Codex `PermissionRequest` hook support that uses the same auto/manual approval state as the existing Claude Code hook.

**Architecture:** Keep one hook executable and add an adapter layer inside `claude_monitor/hook.py`. The adapter detects Claude Code versus Codex input, normalizes permission events into the existing internal shape, reuses `decide_permission()`, logs one JSONL event stream, and emits the source-specific permission response. Installer support writes Codex hook configuration to `~/.codex/hooks.json` without changing the existing Claude Code settings flow.

**Tech Stack:** Python 3.12, JSON command hooks, pytest, existing Textual TUI state files under `/tmp/claude-auto-accept/`.

**Design Source:** User-approved conversation design on 2026-06-08.

---

## File Structure

### Modified Files
- `claude_monitor/hook.py` - add hook source detection, input normalization, and source-aware output emission.
- `install.py` - add Codex hook configuration support for `~/.codex/hooks.json`.
- `README.md` - document Codex permission hook setup and behavior.
- `CLAUDE.md` - update project architecture notes to mention the shared Claude Code/Codex hook entry point.

### Test Files
- `tests/test_hook.py` - add unit coverage for Codex payload normalization and Codex permission decisions.
- `tests/test_hook_tui_probe.py` - add end-to-end coverage that Codex requests defer when the monitor is not running.
- `tests/test_installer_codex_hooks.py` - add installer coverage for `~/.codex/hooks.json`.

---

## Task 1: Add Hook Source Detection and Normalization

**Files:**
- Modify: `claude_monitor/hook.py`
- Test: `tests/test_hook.py`

- [ ] **Step 1: Write failing tests for Codex detection and normalization**

Add this test class to `tests/test_hook.py` after `_run_hook()` and before `TestHookDecisionLogic`:

```python
class TestHookSourceNormalization:
    def test_detects_claude_code_from_existing_hook_event_name(self):
        from claude_monitor.hook import detect_hook_source

        data = {
            "hook_event_name": "PermissionRequest",
            "tool_name": "Bash",
            "session_id": "claude-session",
        }

        assert detect_hook_source(data) == "claude_code"

    def test_detects_codex_from_turn_scoped_fields(self):
        from claude_monitor.hook import detect_hook_source

        data = {
            "hook_event_name": "PermissionRequest",
            "turn_id": "turn-123",
            "permission_mode": "default",
            "tool_name": "Bash",
            "tool_input": {"command": "git status"},
        }

        assert detect_hook_source(data) == "codex"

    def test_normalizes_codex_permission_request(self):
        from claude_monitor.hook import normalize_hook_event

        raw = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-session",
            "turn_id": "turn-123",
            "permission_mode": "default",
            "cwd": "/tmp/project",
            "model": "gpt-5.1-codex",
            "tool_name": "Bash",
            "tool_input": {
                "command": "git status --short",
                "description": "Inspect repository status",
            },
        }

        event = normalize_hook_event(raw)

        assert event["_source"] == "codex"
        assert event["hook_event_name"] == "PermissionRequest"
        assert event["session_id"] == "codex-session"
        assert event["tool_name"] == "Bash"
        assert event["tool_input"]["command"] == "git status --short"
        assert event["turn_id"] == "turn-123"
        assert event["permission_mode"] == "default"

    def test_normalizes_existing_claude_code_payload_without_changing_fields(self):
        from claude_monitor.hook import normalize_hook_event

        raw = {
            "hook_event_name": "PermissionRequest",
            "session_id": "claude-session",
            "cwd": "/tmp/project",
            "tool_name": "Bash",
            "tool_input": {"command": "ls"},
        }

        event = normalize_hook_event(raw)

        assert event["_source"] == "claude_code"
        assert event["hook_event_name"] == "PermissionRequest"
        assert event["session_id"] == "claude-session"
        assert event["tool_name"] == "Bash"
        assert event["tool_input"]["command"] == "ls"
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_hook.py::TestHookSourceNormalization -v
```

Expected: FAIL with import errors for `detect_hook_source` and `normalize_hook_event`.

- [ ] **Step 3: Add source constants and detection helpers**

In `claude_monitor/hook.py`, add these constants and helpers after the imports:

```python
SOURCE_CLAUDE_CODE = "claude_code"
SOURCE_CODEX = "codex"


def detect_hook_source(data: dict) -> str:
    """Return the hook producer for a raw hook payload."""
    if data.get("turn_id") is not None or data.get("permission_mode") is not None:
        return SOURCE_CODEX
    return SOURCE_CLAUDE_CODE


def normalize_hook_event(data: dict) -> dict:
    """Convert a raw hook payload into claude-monitor's internal event shape."""
    source = detect_hook_source(data)
    event = dict(data)
    event["_source"] = source

    if source == SOURCE_CODEX:
        event["hook_event_name"] = data.get("hook_event_name") or data.get(
            "hookEventName", "PermissionRequest"
        )
        event.setdefault("session_id", data.get("session_id", ""))
        event.setdefault("tool_name", data.get("tool_name", ""))
        event.setdefault("tool_input", data.get("tool_input") or {})

    return event
```

- [ ] **Step 4: Run the normalization tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest tests/test_hook.py::TestHookSourceNormalization -v
```

Expected: PASS.

- [ ] **Step 5: Run existing hook unit tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_hook.py tests/test_hook_extended.py -v
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add claude_monitor/hook.py tests/test_hook.py
git commit -m "feat: normalize Codex permission hook payloads"
```

---

## Task 2: Emit Source-Aware Permission Decisions

**Files:**
- Modify: `claude_monitor/hook.py`
- Test: `tests/test_hook.py`
- Test: `tests/test_hook_tui_probe.py`

- [ ] **Step 1: Add Codex allow/defer tests**

Add these tests to `TestHookDecisionLogic` in `tests/test_hook.py`:

```python
    def test_codex_permission_request_auto_allow(self, isolated_state, monkeypatch):
        """Codex PermissionRequest uses the same allow path when monitor is running."""
        import claude_monitor.hook as hook

        monkeypatch.setattr(hook, "_tui_is_running", lambda: True)

        data = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-session",
            "turn_id": "turn-123",
            "permission_mode": "default",
            "cwd": "/tmp/test",
            "model": "gpt-5.1-codex",
            "tool_name": "Bash",
            "tool_input": {"command": "git status --short"},
        }

        stdout, events = _run_hook(data, monkeypatch)
        result = json.loads(stdout)
        logged = json.loads(events.strip().split("\n")[-1])

        assert result["hookSpecificOutput"]["hookEventName"] == "PermissionRequest"
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"
        assert logged["_source"] == "codex"
        assert logged["_decision"] == "allowed"

    def test_codex_permission_request_paused_global_defers(self, isolated_state, monkeypatch):
        """Global manual mode leaves Codex's normal approval prompt intact."""
        import claude_monitor.hook as hook

        monkeypatch.setattr(hook, "_tui_is_running", lambda: True)

        state = {"global_paused": True, "paused_sessions": [], "paused_claude_sessions": []}
        with open(isolated_state["state_file"], "w") as f:
            json.dump(state, f)

        data = {
            "hook_event_name": "PermissionRequest",
            "session_id": "codex-session",
            "turn_id": "turn-123",
            "permission_mode": "default",
            "cwd": "/tmp/test",
            "tool_name": "Bash",
            "tool_input": {"command": "git commit"},
        }

        stdout, events = _run_hook(data, monkeypatch)
        logged = json.loads(events.strip().split("\n")[-1])

        assert stdout.strip() == ""
        assert logged["_source"] == "codex"
        assert logged["_decision"] == "deferred"
```

Add this test to `TestHookEndToEnd` in `tests/test_hook_tui_probe.py`:

```python
    def test_codex_no_monitor_when_nothing_listening(self, isolated_state, monkeypatch):
        """Codex hook stays silent when the TUI is not reachable."""
        monkeypatch.setattr("claude_monitor.hook.API_PORT", _free_port_no_listener())
        out = self._run(
            {
                "hook_event_name": "PermissionRequest",
                "session_id": "codex-session",
                "turn_id": "turn-123",
                "permission_mode": "default",
                "cwd": "/tmp",
                "tool_name": "Bash",
                "tool_input": {"command": "git status"},
            },
            monkeypatch,
        )
        assert out == ""

        with open(isolated_state["events_file"]) as f:
            event = json.loads(f.readline())
        assert event["_source"] == "codex"
        assert event["_decision"] == "no_monitor"
```

- [ ] **Step 2: Run the new decision tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_hook.py::TestHookDecisionLogic::test_codex_permission_request_auto_allow \
  tests/test_hook.py::TestHookDecisionLogic::test_codex_permission_request_paused_global_defers \
  tests/test_hook_tui_probe.py::TestHookEndToEnd::test_codex_no_monitor_when_nothing_listening \
  -v
```

Expected: FAIL because `main()` does not call `normalize_hook_event()` yet.

- [ ] **Step 3: Add source-aware output helpers**

In `claude_monitor/hook.py`, add these helpers after `normalize_hook_event()`:

```python
def build_permission_allow_output(source: str) -> dict:
    """Build the allow response for the hook producer."""
    return {
        "hookSpecificOutput": {
            "hookEventName": "PermissionRequest",
            "decision": {"behavior": "allow"},
        }
    }


def write_permission_allow(source: str) -> None:
    """Write an allow decision to stdout for a permission request."""
    json.dump(build_permission_allow_output(source), sys.stdout)
```

The `source` argument is kept even though the current Claude Code and Codex allow shapes match. This keeps the output boundary explicit if either producer diverges.

- [ ] **Step 4: Refactor `main()` to normalize before processing**

In `claude_monitor/hook.py`, replace the first lines of `main()` after `os.makedirs(...)` with this:

```python
    raw_data = json.load(sys.stdin)
    data = normalize_hook_event(raw_data)
    source = data.get("_source", SOURCE_CLAUDE_CODE)
    event_name = data.get("hook_event_name", "")
    data["_timestamp"] = time.time()
    data["_tty"] = os.ttyname(sys.stderr.fileno()) if sys.stderr.isatty() else None
    raw = os.environ.get("ITERM_SESSION_ID", "")
    data["_iterm_session_id"] = extract_iterm_session_id(raw) or None
```

At the end of `main()`, replace the inline `json.dump(...)` allow block with:

```python
    write_permission_allow(source)
```

- [ ] **Step 5: Run the new decision tests and verify they pass**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_hook.py::TestHookDecisionLogic::test_codex_permission_request_auto_allow \
  tests/test_hook.py::TestHookDecisionLogic::test_codex_permission_request_paused_global_defers \
  tests/test_hook_tui_probe.py::TestHookEndToEnd::test_codex_no_monitor_when_nothing_listening \
  -v
```

Expected: PASS.

- [ ] **Step 6: Run hook regression tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_hook.py tests/test_hook_extended.py tests/test_hook_tui_probe.py -v
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add claude_monitor/hook.py tests/test_hook.py tests/test_hook_tui_probe.py
git commit -m "feat: approve Codex permission requests from monitor state"
```

---

## Task 3: Add Codex Hook Installer Support

**Files:**
- Modify: `install.py`
- Test: `tests/test_installer_codex_hooks.py`

- [ ] **Step 1: Write installer tests for Codex hooks**

Create `tests/test_installer_codex_hooks.py`:

```python
"""Tests for install.configure_codex_hooks()."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import install  # noqa: E402

NEW_CMD = "/new/venv/bin/claude-monitor-hook"
OLD_CMD = "/old/venv/bin/claude-monitor-hook"

NEW_CODEX_HOOKS_CONFIG = {
    "PermissionRequest": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": NEW_CMD,
                    "timeout": 300,
                    "statusMessage": "Checking claude-monitor approval state",
                }
            ]
        }
    ]
}


@pytest.fixture
def hooks_file(tmp_path):
    return tmp_path / "hooks.json"


def _run(hooks_file: Path, inputs: list[str]) -> None:
    answers = iter(inputs)
    with (
        patch.object(install, "CODEX_HOOKS_FILE", hooks_file),
        patch.object(install, "HOOK_COMMAND", NEW_CMD),
        patch.object(install, "CODEX_HOOKS_CONFIG", NEW_CODEX_HOOKS_CONFIG),
        patch("builtins.input", side_effect=lambda _: next(answers)),
    ):
        install.configure_codex_hooks()


def _load(hooks_file: Path) -> dict:
    return json.loads(hooks_file.read_text())


class TestCodexHookInstaller:
    def test_decline_does_not_create_file(self, hooks_file):
        _run(hooks_file, ["n"])
        assert not hooks_file.exists()

    def test_creates_codex_hooks_file(self, hooks_file):
        _run(hooks_file, ["y"])
        assert _load(hooks_file) == {"hooks": NEW_CODEX_HOOKS_CONFIG}

    def test_preserves_unrelated_permission_hook(self, hooks_file):
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PermissionRequest": [
                            {"hooks": [{"type": "command", "command": "/unrelated/hook"}]}
                        ]
                    }
                }
            )
        )

        _run(hooks_file, ["y"])

        commands = [
            hook["command"]
            for group in _load(hooks_file)["hooks"]["PermissionRequest"]
            for hook in group["hooks"]
        ]
        assert "/unrelated/hook" in commands
        assert NEW_CMD in commands

    def test_replaces_stale_monitor_hook_when_accepted(self, hooks_file):
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PermissionRequest": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": OLD_CMD,
                                        "timeout": 300,
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )

        _run(hooks_file, ["y", "y"])

        commands = [
            hook["command"]
            for group in _load(hooks_file)["hooks"]["PermissionRequest"]
            for hook in group["hooks"]
        ]
        assert NEW_CMD in commands
        assert OLD_CMD not in commands

    def test_keeps_stale_monitor_hook_when_declined(self, hooks_file):
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PermissionRequest": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": OLD_CMD,
                                        "timeout": 300,
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )

        _run(hooks_file, ["y", "n"])

        commands = [
            hook["command"]
            for group in _load(hooks_file)["hooks"]["PermissionRequest"]
            for hook in group["hooks"]
        ]
        assert OLD_CMD in commands
        assert NEW_CMD not in commands
```

- [ ] **Step 2: Run the installer tests and verify they fail**

Run:

```bash
.venv/bin/python -m pytest tests/test_installer_codex_hooks.py -v
```

Expected: FAIL with `AttributeError: module 'install' has no attribute 'configure_codex_hooks'`.

- [ ] **Step 3: Add Codex hook constants**

In `install.py`, add these constants near the existing `SETTINGS_FILE` and `HOOKS_CONFIG` definitions:

```python
CODEX_HOOKS_FILE = Path.home() / ".codex" / "hooks.json"

CODEX_HOOKS_CONFIG = {
    "PermissionRequest": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": HOOK_COMMAND,
                    "timeout": 300,
                    "statusMessage": "Checking claude-monitor approval state",
                }
            ]
        }
    ]
}
```

- [ ] **Step 4: Extract a reusable hook merge helper**

In `install.py`, add this helper above `configure_hooks()`:

```python
def _merge_monitor_hooks(settings: dict, desired_config: dict) -> tuple[list, list, list]:
    """Return hook merge actions for a settings-like object with a top-level hooks key."""
    if "hooks" not in settings:
        settings["hooks"] = {}

    to_skip = []
    to_add = []
    to_replace = []

    for event_type, desired_groups in desired_config.items():
        existing_groups = settings["hooks"].get(event_type)

        if existing_groups is None:
            to_add.append((event_type, desired_groups, None))
            continue

        exact = any(
            h.get("command") == HOOK_COMMAND
            for group in existing_groups
            for h in group.get("hooks", [])
        )
        if exact:
            to_skip.append(event_type)
            continue

        stale = _find_monitor_hooks(existing_groups)
        if stale:
            to_replace.append((event_type, existing_groups, stale, desired_groups))
        else:
            to_add.append((event_type, desired_groups, existing_groups))

    return to_skip, to_add, to_replace
```

Update `configure_hooks()` so its existing two-pass analysis uses:

```python
    to_skip, to_add, to_replace = _merge_monitor_hooks(settings, HOOKS_CONFIG)
```

Keep the existing apply/write logic in `configure_hooks()` unchanged after that assignment.

- [ ] **Step 5: Add `configure_codex_hooks()`**

In `install.py`, add this function after `configure_hooks()`:

```python
def configure_codex_hooks():
    print()
    print(f"Codex hooks file: {CODEX_HOOKS_FILE}")
    print()
    print("This will configure the following Codex hook:")
    print("  - PermissionRequest  (auto-accept permissions when claude-monitor is running)")
    print()
    print(f"Hook command: {HOOK_COMMAND}")
    print()

    answer = input("Configure Codex hooks in ~/.codex/hooks.json? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped. You can add Codex hooks manually later.")
        return

    if CODEX_HOOKS_FILE.exists():
        with open(CODEX_HOOKS_FILE) as f:
            settings = json.load(f)
    else:
        CODEX_HOOKS_FILE.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    to_skip, to_add, to_replace = _merge_monitor_hooks(settings, CODEX_HOOKS_CONFIG)

    overwrite = False
    if to_replace:
        print()
        print("Found existing claude-monitor Codex hooks with a different path:")
        for event_type, _, stale, _ in to_replace:
            for _, _, h in stale:
                print(f"  {event_type}: {h['command']}")
        print(f"New path would be: {HOOK_COMMAND}")
        ans = input("Replace with new path? [y/N] ").strip().lower()
        overwrite = ans in ("y", "yes")

    for event_type in to_skip:
        print(f"  {event_type}: already configured correctly, skipping")

    for event_type, desired_groups, existing_groups in to_add:
        if existing_groups is None:
            settings["hooks"][event_type] = list(desired_groups)
        else:
            for desired_group in desired_groups:
                desired_matcher = desired_group.get("matcher")
                merge_target = next(
                    (g for g in existing_groups if g.get("matcher") == desired_matcher),
                    None,
                )
                if merge_target is not None:
                    merge_target.setdefault("hooks", []).extend(desired_group.get("hooks", []))
                else:
                    existing_groups.append(desired_group)
        print(f"  {event_type}: added")

    for event_type, existing_groups, stale, desired_groups in to_replace:
        if overwrite:
            replacement_hook = desired_groups[0]["hooks"][0]
            for gi, hi, _ in stale:
                existing_groups[gi]["hooks"][hi] = replacement_hook
            settings["hooks"][event_type] = existing_groups
            print(f"  {event_type}: updated hook path")
        else:
            print(f"  {event_type}: skipped (keeping existing path)")

    changed = bool(to_add) or (bool(to_replace) and overwrite)
    if not changed and not to_skip:
        return

    if not changed:
        print("\nNo Codex hook changes needed.")
        return

    with open(CODEX_HOOKS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"\nCodex hooks written to {CODEX_HOOKS_FILE}")
```

- [ ] **Step 6: Call Codex hook setup from installer main flow**

In `install.py`, update `main()` near the existing setup calls:

```python
    configure_hooks()
    configure_codex_hooks()
    configure_statusline()
```

- [ ] **Step 7: Run installer tests**

Run:

```bash
.venv/bin/python -m pytest tests/test_installer_hooks.py tests/test_installer_codex_hooks.py -v
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add install.py tests/test_installer_codex_hooks.py
git commit -m "feat: configure Codex permission hooks"
```

---

## Task 4: Document Codex Hook Setup and Behavior

**Files:**
- Modify: `README.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Update install documentation**

In `README.md`, update the install section bullet that currently says the installer configures Claude Code hooks. Use:

```markdown
3. Configures Claude Code hooks in `~/.claude/settings.json` and, optionally, Codex hooks in `~/.codex/hooks.json` (interactive - asks before writing)
```

- [ ] **Step 2: Add manual Codex hook config**

In `README.md`, after the Claude Code manual hook configuration paragraph, add:

````markdown
### Codex Permission Hook

Codex can use the same hook executable for permission requests. Add this to `~/.codex/hooks.json`:

```json
{
  "hooks": {
    "PermissionRequest": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "/path/to/claude-monitor/.venv/bin/claude-monitor-hook",
            "timeout": 300,
            "statusMessage": "Checking claude-monitor approval state"
          }
        ]
      }
    ]
  }
}
```

After adding or changing a Codex hook, open `/hooks` in Codex and trust the hook definition. When `claude-monitor` is running and the session is in auto mode, the hook returns an allow decision for Codex permission requests. In manual mode, or when the monitor is not reachable, it returns no decision so Codex shows its normal approval prompt.
````

- [ ] **Step 3: Update How It Works**

In `README.md`, update the Hook section introduction to:

```markdown
Claude Code calls `claude-monitor-hook` via `~/.claude/settings.json`. Codex can call the same executable via `~/.codex/hooks.json` for `PermissionRequest` events.
```

Add this row to the event table:

```markdown
| Codex `PermissionRequest` | Codex wants approval for a tool or sandbox/network escalation |
```

Update the paragraph after the event table to:

```markdown
The hook normalizes Claude Code and Codex payloads into one internal event shape, writes every event as a JSON line to `events.jsonl`, and tags the producer with `_source`. For permission requests, it reads `state.json` to check global and per-session pause state. If paused, or if the monitor is not running, it exits without a decision so the original tool shows its normal approval prompt. Otherwise, it responds with an allow decision.
```

- [ ] **Step 4: Update project architecture notes**

In `CLAUDE.md`, replace the hook architecture sentence with:

```markdown
1. **Hook** (`hook.py`): Claude Code calls this via `~/.claude/settings.json`, and Codex can call it via `~/.codex/hooks.json` for `PermissionRequest` events. It normalizes source-specific payloads, writes JSON events to `/tmp/claude-auto-accept/events.jsonl`, and auto-allows permission requests unless paused or the TUI is not reachable (checked via `state.json` and the local API port).
```

- [ ] **Step 5: Run documentation grep checks**

Run:

```bash
rg -n "Codex|~/.codex/hooks.json|_source|PermissionRequest" README.md CLAUDE.md
```

Expected: Output includes the new Codex setup, behavior description, and architecture note.

- [ ] **Step 6: Commit**

```bash
git add README.md CLAUDE.md
git commit -m "docs: document Codex permission hook support"
```

---

## Task 5: Full Verification

**Files:**
- No code changes expected.

- [ ] **Step 1: Run focused tests**

Run:

```bash
.venv/bin/python -m pytest \
  tests/test_hook.py \
  tests/test_hook_extended.py \
  tests/test_hook_tui_probe.py \
  tests/test_installer_hooks.py \
  tests/test_installer_codex_hooks.py \
  -v
```

Expected: PASS.

- [ ] **Step 2: Run the existing full test suite**

Run:

```bash
.venv/bin/python -m pytest
```

Expected: PASS.

- [ ] **Step 3: Inspect the final diff**

Run:

```bash
git diff --stat HEAD~4..HEAD
git diff HEAD~4..HEAD -- claude_monitor/hook.py install.py README.md CLAUDE.md tests/test_hook.py tests/test_hook_tui_probe.py tests/test_installer_codex_hooks.py
```

Expected: Diff only contains Codex hook support, installer support, docs, and related tests.

- [ ] **Step 4: Manual smoke test Codex hook output**

Run:

```bash
printf '%s\n' '{"hook_event_name":"PermissionRequest","session_id":"codex-session","turn_id":"turn-1","permission_mode":"default","cwd":"/tmp","tool_name":"Bash","tool_input":{"command":"git status --short"}}' \
  | .venv/bin/claude-monitor-hook
```

Expected when `claude-monitor` is not running: no stdout output, and the newest `/tmp/claude-auto-accept/events.jsonl` entry has `_source: "codex"` and `_decision: "no_monitor"`.

- [ ] **Step 5: Manual smoke test installer output**

Run:

```bash
.venv/bin/python install.py
```

Expected: installer asks separately about Claude Code hooks, Codex hooks, and Claude Code statusLine. If Codex hook setup is accepted, `~/.codex/hooks.json` contains a `PermissionRequest` command hook pointing to `.venv/bin/claude-monitor-hook`.

- [ ] **Step 6: Final commit if verification changed files**

If formatting or docs edits changed files during verification, commit those changes:

```bash
git add claude_monitor/hook.py install.py README.md CLAUDE.md tests/test_hook.py tests/test_hook_tui_probe.py tests/test_installer_codex_hooks.py
git commit -m "chore: finalize Codex permission hook support"
```

Expected: No commit is created if verification did not change files.

---

## Behavior Contract

- Claude Code behavior remains unchanged.
- Codex `PermissionRequest` events are tagged with `_source: "codex"` in `events.jsonl`.
- Existing TUI auto/manual state controls Codex permission decisions.
- If `claude-monitor` is not running, Codex receives no hook decision and displays its normal approval prompt.
- If global or per-session manual mode is active, Codex receives no hook decision and displays its normal approval prompt.
- If auto mode is active and the monitor is reachable, Codex receives an allow decision.
- Codex usage/quota polling is not part of this plan.
