# Changelog

## 2026-03-04

### Fixes
- Verify and fix os._exit(0) on clean exit (#bug, #exit)
- Remove dead code _current_layout_ids and _collect_all_session_ids (#dead-code, #cleanup)
- Remove duplicate tier in format_usage_inline (#dead-code, #usage)

### Tasks
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
