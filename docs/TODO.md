# claude-monitor TODO

## Multi-window/tab support
- Iterate all iTerm2 windows and tabs (not just `current_tab`)
- Flatten all tabs into Textual `TabbedContent` — one `TabPane` per iTerm2 tab
- Use tab name as label
- If only one tab total, skip the tab UI and show layout directly
- Show all tabs initially; rename pane title to "Claude" once hook events arrive
- Filter out tabs with no Claude sessions? (deferred — "waiting" state is low noise)

## Settings popup
- Accessible via `s` keybinding and command palette
- Textual `ModalScreen` or `Screen` overlay
- Settings:
  - **Default mode**: Auto / Manual / Last Used
  - **Theme**: select from Textual themes (replaces standalone theme persistence)
  - **Debug**: on/off (toggle debug logging to `/tmp/claude-auto-accept/tui-debug.log`)
  - **iTerm scope**: Current tab only / All tabs in current window / All tabs in all windows
  - **Timestamp style**: 12hr / 24hr / Date+time / Auto (responsive based on width)
  - **Account usage**: on/off — show API usage bar using OAuth token
- Persist to `~/.config/claude-monitor/config.json`
- Load on startup, apply before TUI renders

## Command palette
- Fix `^p` — currently opens the menu, not the command palette
- Register custom commands as Textual command providers:
  - Auto/Manual toggle
  - Refresh layout
  - Settings
  - Quit
- Keybinding changes:
  - `a` for Auto/Manual toggle (not `p`)
  - `shift+tab` also toggles Auto/Manual (matches Claude Code)
  - `s` opens Settings

## API usage status bar
- Add a second status bar above the footer showing 5h and 7d API quota utilization
- Port logic from `~/.claude/statusline.sh` + `statusline-render.ts`
- Data source: `GET https://api.anthropic.com/api/oauth/usage` with OAuth token from macOS Keychain
  - Response has `five_hour` and `seven_day` windows with `utilization` (%) and `resets_at` (ISO timestamp)
- OAuth token: extract from Keychain via `security find-generic-password -s "Claude Code-credentials" -w`
  - Decode hex, parse JSON, extract `claudeAiOauth.accessToken` (sk-ant-oat...)
  - Cache token until `expiresAt`
- Cache usage response for 5 minutes (same as statusline)
- Render with Rich markup in a `Static` widget (dock: bottom, height: 1)
  - Progress bars using Unicode block chars (█░)
  - Color coding: green < 40%, yellow < 60%, orange < 80%, red >= 80%
  - Show reset time countdown and local time
- Width-responsive: use `self.size.width` to pick detail tier
  - Wide: `5h 42% ████████░░░░ 2h13m (3:45PM) │ 7d 18% ██░░░░░░░░░░ Thu 9:00AM`
  - Medium: drop bars or reset times
  - Narrow: just `5h 42% │ 7d 18%`
- Poll every 5 minutes in a background worker thread
