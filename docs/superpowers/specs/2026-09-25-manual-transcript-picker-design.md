# Manual transcript picker design

The Hand-off tab offers a manual action to load a historical Claude transcript, including one without a saved hand-off. The picker searches transcript files under `~/.claude/projects` by project folder or session ID. Opening the picker or submitting a search enumerates names and file metadata only; it does not parse transcript bodies or invoke an LLM. Results are sorted newest first and capped so the list remains responsive.

Selecting one candidate reads that transcript off the UI thread, validates its recorded project path against the Claude project folder, and displays the existing saved hand-off if one exists. Otherwise it builds a temporary heuristic preview without replacing saved entries or applying retention to an old session. It does not scan the monitor event log, invoke an LLM, or write a project rule index. The action is explicitly manual. Errors such as a missing, malformed, or moved transcript appear as a TUI notification. Existing saved entries and live-session rotation behavior remain available.

Tests cover discovery and search across projects, result caps, path confinement, selected-only parsing, and the Hand-off panel transition after a successful load.
