# claude-monitor

A Textual TUI that monitors and auto-accepts Claude Code permission prompts across iTerm2 panes.

## MANDATORY: Version bump + restart after every code change

1. Bump `__version__` in `claude_monitor/__init__.py` (increment beta: `-beta.10` → `-beta.11`; if no beta, add `-beta.1` to the patch).
2. Restart the TUI by sending `q` to its iTerm2 pane (use `.venv/bin/python3` + iTerm2 Python API).
3. Take a screenshot via `curl -s 'http://localhost:17233/screenshot' -o /tmp/tui-check.png` and visually verify.
4. Before any git commit: remove the `-beta.X` suffix from `__version__` and sync `pyproject.toml`.

**Verify by version number:** after restart, check the version in the screenshot's top-right status bar matches the bumped version. `q` may close a modal (HelpScreen) instead of quitting — press Escape first, then `q`.

### Version scheme
- New session: `x.x.0` → `x.x.1-beta.1`
- Every restart: increment beta (`-beta.1` → `-beta.2`)
- End of session (before commit): drop the `-beta.x` suffix; sync `pyproject.toml`
