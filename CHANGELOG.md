# Changelog

## 2026-07-14

### Fixes
- Stop overwriting user-renamed iTerm tab titles (#iterm2)

## 2026-06-10

### Features
- Encrypt `--send`/`--receive` credential transfer end-to-end (AES-256-CBC + HMAC-SHA256, shared passphrase via `CLAUDE_CREDENTIALS_PASSPHRASE`); wrong passphrase or tampered payload is rejected and the keychain is left untouched (#claude-credentials, #security)
- Both `--send` frontends (`claude-monitor-credentials` and `claude-credentials.sh`) perform the outbound TCP write via `/usr/bin/nc` so they work under macOS Local Network Privacy (a Homebrew/uv Python is blocked from LAN connections; `nc` is an exempt platform binary). `--receive` only listens, which isn't gated, so it stays in Python. Override with `CLAUDE_CREDENTIALS_NC` (#claude-credentials, #macos)
- Add `-v`/`--verbose` to `claude-monitor-credentials` for send/receive diagnostics (target, timing, and the underlying error on failure) (#claude-credentials)

### Fixes
- Both `--receive` frontends (`claude-monitor-credentials` and `claude-credentials.sh`) now reject an authenticated-but-undecryptable frame (valid HMAC, but bad block length/padding) cleanly instead of crashing with a traceback; the keychain is left unchanged (#claude-credentials, #security)
- Both `--receive` frontends now reject a decrypted payload that isn't a valid credential blob — raw JSON or hex-encoded JSON, the two forms Claude Code stores — before it can overwrite the keychain entry; empty, truncated, or garbage payloads (even with a valid HMAC) are refused. A data-loss guard that still accepts hex-encoded full transfers (#claude-credentials, #security)

## 2026-06-09

### Features
- Add LAN TCP transfer to claude-credentials.sh (--send / --receive) (#claude-credentials, #networking)
- Add `claude-monitor-credentials` console command — a pure-Python port of claude-credentials.sh (#claude-credentials)

## 2026-05-05

### Fixes
- Log view timestamps should all be the same width (#ui)

## 2026-03-17

### Features
- Close session tabs with `x` keybinding — dismissed tabs auto-recreate if the session is still active (#ui, #keybindings)

## 2026-03-16

### Fixes
- Add help command to command palette (#ui)
- Right-align first column in help modal (#ui)

### Tasks
- Help modal: split into Global and Instance sections with responsive layout (#ui, #help)
- Add ? shortcut key modal showing all keybindings (#ui, #keybindings)

## 2026-03-14

### Tasks
- Replace dashboard minimize toggle with unified resize/restore behavior (#dashboard, #ui)
- Add Dash+/Dash- resize commands to the command palette (#dashboard, #ui)
- Resizable dashboard pane in simple mode with minimum session log height (#dashboard, #ui)
- Share a single status line between expanded and minimized dashboard (#dashboard, #ui)
- Minimized dashboard: blue frame, up arrow on border, and Label: # text format (#dashboard, #ui)
- Minimized dashboard shows a one-line statistics summary (#dashboard, #ui)
