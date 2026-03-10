# Changelog

## 2026-03-10

### Tasks
- Trim tab labels to fit all tabs visible; investigate tab bar wrapping (#ui, #tui)
- Add global keybindings to switch to next/prev tab (#ui, #keybindings)
- Document that TUI restart must use venv Python for iterm2 module (#documentation, #debugging)

## 2026-03-09

### Tasks
- AskUserQuestion countdown modal overlay (#ui, #tui)

## 2026-03-06

### Fixes
- AskUserQuestion timeout fires too early due to hook timeout (#bug, #hooks, #timeout)

### Tasks
- Position context menu at mouse click location (#ui, #context-menu)

## 2026-03-05

### Fixes
- Account usage bar at the top doesn't work (#ui, #usage)

### Tasks
- Move context menu trigger to title bar click (#ui, #ux, #context-menu)
- Position context menu at mouse click location (#ui, #context-menu)
- Decouple API /text endpoint from private panel attributes (#api, #refactor)
- Add logging to bare except blocks in tui.py (#debugging, #quality)
- Split _format_event into pure formatting and side effects (#refactor, #dry)
- Add AskUserQuestion filter to permissions log (#ui, #filtering)
- Pretty-format AskUserQuestion events in log (#ui, #formatting)
- Add configurable AUTO mode with AskUserQuestion handling (#settings, #auto-mode)

## 2026-03-04

### Fixes
- Verify and fix os._exit(0) on clean exit (#bug, #exit)
- Remove dead code _current_layout_ids and _collect_all_session_ids (#dead-code, #cleanup)
- Remove duplicate tier in format_usage_inline (#dead-code, #usage)

### Tasks
- Vertical scrollbar active thumb uses wrong glyph (#ui, #scrollbar)
- Reverse sort order of choices and questions logs (#ui, #ux)
- Set scrollbar background color to match pane background (#ui, #scrollbar)
- Change vertical scrollbar to use right-half block character (#ui, #scrollbar)
- Replace horizontal scrollbar with bottom-half block character (#ui, #scrollbar)
- Replace full-width scrollbars with half-block character (#ui, #scrollbar)
- Extract _iterm_sid_from_event helper to DRY 4 call sites (#dry, #refactor)
- Unify _format_countdown with fmt_duration (#dry, #usage)
- Commit HTTP API feature and update CLAUDE.md (#api, #release)
- Update docs/TODO.md or remove it (#docs, #cleanup)
- Add HTTP API with /health, /screenshot, /text endpoints (#api, #telegram)
- Add font detection for PNG screenshot rendering (#api, #screenshot)
- Optimize PNG screenshot size with 256-color quantization (#api, #screenshot, #performance)
- Add cairosvg dependency for SVG to PNG conversion (#api, #deps)

## 2026-03-02

### Fixes
- 13 issues from code review (#bugs, #review)

## 2026-03-01

### Fixes
- Per-pane auto/manual mode, consolidate state, fix layout rebuilds (#ui, #state)
- Deduplicate code, fix bugs, remove dead code (#refactor)

## 2026-02-28

### Fixes
- Multi-tab support, settings, command palette, and usage bar (#ui, #settings, #usage)
