# Undo: reduced session-start tokens

This document explains how to revert every change made by the "reduce session-start token load" task, in case the trimmed behavior loses something you actually need.

## What was changed

| # | File | Change | Why |
|---|---|---|---|
| 1 | `/Users/chris/dev/personal/claude-monitor/CLAUDE.md` | Trimmed from 10,444 → ~1,500 bytes | Removed reference material (project structure, entry points, how-it-works, state management, key technical details, dev workflow, dependencies) |
| 2 | `/Users/chris/.claude/projects/-Users-chris-dev-personal-claude-monitor/memory/MEMORY.md` | Slimmed from 1,130 → ~600 bytes | Dropped the "Verify restart via version number" entry (the rule moved into CLAUDE.md) |
| 3 | `/Users/chris/.claude/projects/-Users-chris-dev-personal-claude-monitor/memory/feedback_restart_verify.md` | **Deleted** | Content merged into the new CLAUDE.md version-bump rule |
| 4 | `/Users/chris/CLAUDE.md` | Trimmed from 1,319 → ~600 bytes | Tightened "Python Package Installation" and "Package Versions" sections |
| 5 | `/Users/chris/.claude/settings.json` | `enabledPlugins` — 9 plugins set to `false` | See table below |

## Plugin drops (settings.json)

Nine plugins were disabled to shrink always-on and on-invoke token cost:

| Plugin | always_on chars saved | on_invoke chars saved | Reason dropped |
|---|---:|---:|---|
| `rust-analyzer-lsp@claude-plugins-official` | 0 | 0 | Project is Python, not Rust |
| `typescript-lsp@claude-plugins-official` | 0 | 0 | Project is Python, not TypeScript |
| `plugin-dev@claude-plugins-official` | 1,566 | 36,064 | Only needed when authoring Claude Code plugins |
| `skill-creator@claude-plugins-official` | 75 | 7,922 | Only needed when authoring new skills |
| `frontend-design@claude-plugins-official` | 59 | 857 | TUI has no web UI |
| `prose-craft@prose-craft` | (separate marketplace) | — | Prose writing; not relevant to TUI code |
| `playwright@claude-plugins-official` | 0 | (MCP server) | Browser automation; not used in this project |
| `claude-md-management@claude-plugins-official` | 121 | 1,881 | CLAUDE.md maintenance helpers; the user is already proactive about it |
| `pr-review-toolkit@claude-plugins-official` | 1,404 | 6,628 | PR review tools; not used in this project |

Plugins still enabled (kept because they're relevant to a Python TUI project): `superpowers`, `code-review`, `code-simplifier`, `feature-dev`, `commit-commands`, `security-guidance`, `desktop-commander`.

## How to undo

### Revert a single file

```bash
# 1. Project CLAUDE.md — retrieve the trimmed-down version
git -C /Users/chris/dev/personal/claude-monitor log --oneline -- CLAUDE.md
# find the commit before "i want to reduce glittery pixel" (or before the trim), then:
git -C /Users/chris/dev/personal/claude-monitor show <commit>:CLAUDE.md > /Users/chris/dev/personal/claude-monitor/CLAUDE.md

# 2. Memory index
git -C /Users/chris/dev/personal/claude-monitor log --oneline -- ~/.claude/projects/-Users-chris-dev-personal-claude-monitor/memory/MEMORY.md
# then restore from the appropriate commit
```

### Re-enable a single plugin

Edit `/Users/chris/.claude/settings.json` and change the relevant `"<plugin>": false` to `true`. Example to re-enable the PR review toolkit:

```diff
-    "pr-review-toolkit@claude-plugins-official": false,
+    "pr-review-toolkit@claude-plugins-official": true,
```

Plugins list (set the matching entry to `true`):

- `rust-analyzer-lsp@claude-plugins-official`
- `typescript-lsp@claude-plugins-official`
- `plugin-dev@claude-plugins-official`
- `skill-creator@claude-plugins-official`
- `frontend-design@claude-plugins-official`
- `prose-craft@prose-craft`
- `playwright@claude-plugins-official`
- `claude-md-management@claude-plugins-official`
- `pr-review-toolkit@claude-plugins-official`

### Restore the deleted memory file

The `feedback_restart_verify.md` file's content was:

```markdown
---
name: Verify restart via version number
description: After restarting the TUI, confirm the version in the top-right status bar matches the bumped version before proceeding
type: feedback
---

After restarting the TUI, always verify the version number in the screenshot's top-right status bar matches the expected bumped version. If it doesn't match, the restart likely failed (e.g. a modal was open and `q` dismissed the modal instead of quitting the app).

**Why:** Pressing `q` when a modal is open (like HelpScreen) closes the modal instead of quitting the app. The TUI appears to restart but it's actually the same process.

**How to apply:** After every restart, take a screenshot and check the version string before sending test keystrokes. If version doesn't match, send Escape first to close any open modal, then send `q` again.
```

If you want it back:

```bash
mkdir -p /Users/chris/.claude/projects/-Users-chris-dev-personal-claude-monitor/memory
# paste the content above into:
# /Users/chris/.claude/projects/-Users-chris-dev-personal-claude-monitor/memory/feedback_restart_verify.md
# and add a line to MEMORY.md:
#   ## Verify restart via version number
#   [feedback_restart_verify.md](feedback_restart_verify.md) — ...
```

### Restore the full project CLAUDE.md

The pre-trim CLAUDE.md (10,444 bytes) lived at the commit prior to the "i want to reduce glittery pixel" task. To find it:

```bash
git -C /Users/chris/dev/personal/claude-monitor log --oneline -- CLAUDE.md
```

The trim commit message was about reducing session-start tokens. Run `git show <previous-commit>:CLAUDE.md > CLAUDE.md` to restore.

## When to undo

Consider undoing if you notice:
- Claude is repeatedly asking for project structure or "how things work" — re-add the project-structure and key-technical-details sections to CLAUDE.md.
- A specific plugin is actually needed mid-session — re-enable it in settings.json.
- You start a new project that needs the Python TUI structure notes — copy the relevant section into that project's CLAUDE.md.

The new CLAUDE.md has only the version-bump + verify-by-version rules. If you want the original full reference, restore it.
