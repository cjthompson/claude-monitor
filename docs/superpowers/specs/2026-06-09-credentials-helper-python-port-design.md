# Pure-Python credentials helper

## Context

`claude-credentials.sh` manages the Claude Code OAuth blob in the macOS Keychain
and transfers it between machines (export/import + `--send`/`--receive` over LAN
UDP, with `--oauth-only` to strip machine-specific `mcpOAuth`). It is a bash
script that internally shells out to `python3`, `jq`, `xxd`, `curl`, and
`security` — a bash+python mix in a project that is otherwise pure Python.

This project already contains the same keychain primitives in pure Python:
`claude_monitor/usage.py` has `_read_keychain`, `_write_keychain`,
`_extract_oauth_tokens`, the constants (`KEYCHAIN_SERVICE`, `TOKEN_URL`,
`CLIENT_ID`), and an OAuth token-refresh POST (`usage.py:520-601`). So a Python
port is mostly *consolidation* of logic that already exists, not new logic.

Goal: a strict-stdlib `credentials-helper.py` with the same CLI surface and
behavior as the bash script, sharing one keychain/OAuth core with the TUI.

## Decisions (confirmed)

- **Form:** root-level executable `credentials-helper.py` (`#!/usr/bin/env python3`,
  stdlib only), sibling to `claude-credentials.sh`.
- **Old script:** keep `claude-credentials.sh` as-is (both ship).
- **Reuse:** consolidate keychain/OAuth logic into one shared module that both
  `usage.py` and the new CLI import. This touches live TUI code, so the
  mandatory version-bump + restart + screenshot verification applies.

## Architecture

Two units, built as two stacked PRs.

### Unit A — `claude_monitor/credentials.py` (shared core)

Stdlib only (`subprocess`, `json`, `urllib.request`). Public API:

| Function | Purpose |
|---|---|
| `KEYCHAIN_SERVICE`, `TOKEN_URL`, `CLIENT_ID` | constants (moved here; `usage.py` imports) |
| `class CredentialsError(Exception)` | raised when keychain is missing/unwritable |
| `read_raw() -> str` | verbatim `security … -w` stdout (default-export bytes) |
| `read_json() -> dict` | robust hex-**or**-JSON decode → parsed dict |
| `find_account() -> str \| None` | parse `acct` from `security find-generic-password` |
| `write(content: str) -> None` | discover account + `add-generic-password -U` |
| `oauth_only_json() -> str` | compact `{"claudeAiOauth": …}` (no trailing newline) |
| `extract_oauth_tokens() -> tuple[str,str,float] \| None` | for `usage.py` + `--simple`/`--refresh` |
| `refresh_tokens(refresh_token) -> tuple[str,str,int] \| None` | OAuth POST → (access, refresh, expires_in) |

`read_json` upgrade: bash detects hex vs JSON (`^{` → JSON, else `xxd -r -p`).
`usage.py:_read_keychain` currently assumes JSON only. The shared `read_json`
replicates the bash logic (try JSON, else `bytes.fromhex`), a safe superset.

`usage.py` rewiring (behavior-preserving):
- Constants import from `credentials`.
- `_read_keychain`/`_write_keychain`/`_extract_oauth_tokens` become thin
  wrappers over the shared functions (return `None`/`False` on `CredentialsError`
  to preserve current contracts and `log.debug` lines).
- The refresh method (`usage.py:520-601`) keeps its `self`-coupled orchestration
  (masked-token debug logs, settings cache, `_on_token_refreshed` callback,
  keychain write-back) but calls `credentials.refresh_tokens()` for the network
  POST instead of inlining `urlopen`.

### Unit B — `credentials-helper.py` (root CLI)

`#!/usr/bin/env python3`, stdlib only (`argparse`, `socket`, `json`, `sys`).
`import claude_monitor.credentials as creds` (works when run from repo root /
editable install — repo root is on `sys.path`). Implements every bash mode with
identical exit codes and stderr messages:

- **default** → `print(creds.read_raw())` (raw passthrough)
- `--simple` → 4 lines (access_token/refresh_token/expires_at/expires-local)
- `--refresh` → `refresh_tokens` + keychain write-back, then `--simple` output
- `--oauth-only` → `print(creds.oauth_only_json())`
- `--import <file|->` → read stdin/file, trim, `creds.write()`
- `--send <host> [--send-port N]` → UDP datagram; payload is
  `oauth_only_json()` when `--oauth-only` else `read_raw()`; status line appends
  `(oauth-only)`; fire-and-forget, exit 0
- `--receive [--port N]` → bind one datagram, trim, `creds.write()`

Guards: `--simple/--refresh/--import/--send/--receive` mutually exclusive;
`--oauth-only` only alone or with `--send` — same as the current bash.

Carry-over behavior notes (match bash):
- oauth-only payload is compact JSON with **no** trailing newline (so it equals
  the bash `$(...)`-captured send payload).
- full `--send` can exceed macOS `net.inet.udp.maxdgram` (9216 B); `EMSGSIZE`
  surfaces as a clear error and non-zero exit. `--send --oauth-only` (~475 B) is
  the reliable cross-machine path.

## Testing

- **New** `tests/test_credentials_helper.py`, mirroring
  `tests/test_claude_credentials.py`: mock `security` via a temp `bin/` on
  `PATH`, drive the script with `python3 credentials-helper.py …`. For
  `--send`/`--receive`, bind a **real** loopback UDP socket and assert on the
  datagram (no `python3` mock needed — there's no embedded interpreter anymore).
  Cover: default raw, `--oauth-only` (alone + with `--send`, both orders),
  guard rejections, `--import`/`--receive` round-trip, `--send` byte count +
  status line.
- **New** unit tests for `claude_monitor/credentials.py`: `read_json` handles
  both hex and JSON; `oauth_only_json` strips non-oauth keys and has no trailing
  newline; `write` discovers account and calls `add-generic-password -U`
  (mock `security`).
- **Regression:** existing `tests/test_claude_credentials.py` (bash) stays
  untouched and green. A small test confirms `usage.py` still extracts tokens
  through the shared module.

## Build sequence

1. **PR1 — extract shared core.** Add `credentials.py`, rewire `usage.py`, add
   core unit tests. Verify: `pytest`, then **bump `__version__`, restart TUI,
   screenshot** — confirm the usage bar still renders (it depends on the moved
   keychain/refresh code).
2. **PR2 — CLI (stacked on PR1).** Add `credentials-helper.py` (chmod +x) +
   `tests/test_credentials_helper.py`. Verify: `pytest`, `python3 -c "import ast;
   ast.parse(open('credentials-helper.py').read())"`, and a manual loopback
   `--send --oauth-only` ↔ live keychain check. No TUI change in this PR.

## Out of scope

- Deleting or modifying `claude-credentials.sh` (kept per decision).
- A `keyring` dependency (continue shelling out to `security`, matching
  `usage.py`).
- A `[project.scripts]` console entry point (root script per decision).
