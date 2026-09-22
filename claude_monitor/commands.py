"""Command palette provider for claude-monitor TUI."""

from textual.command import DiscoveryHit, Hit, Hits, Provider


class MonitorCommands(Provider):
    """Command palette provider exposing all TUI actions."""

    COMMANDS_LIST = [
        ("Capture Hand-off Now", "capture_handoff"),
        ("Next Tab", "next_tab"),
        ("Open Settings", "open_settings"),
        ("Previous Tab", "prev_tab"),
        ("Quit", "quit"),
        ("Refresh Hand-off List", "refresh_handoff"),
        ("Refresh Layout", "refresh_layout"),
        ("Rotate Selected Hand-off", "rotate_selected_handoff"),
        ("Show Choices Log", "show_choices"),
        ("Show Hand-off Tab", "show_handoff"),
        ("Show Help", "show_help"),
        ("Show Questions Log", "show_questions"),
        ("Toggle Auto/Manual (global)", "toggle_pause"),
    ]

    async def startup(self) -> None:
        pass

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        for name, action in self.COMMANDS_LIST:
            score = matcher.match(name)
            if score > 0:
                yield Hit(
                    score,
                    matcher.highlight(name),
                    getattr(app, f"action_{action}", None) or (lambda: None),
                    help=f"action_{action}",
                )

    async def discover(self) -> Hits:
        app = self.app
        for name, action in self.COMMANDS_LIST:
            yield DiscoveryHit(
                name,
                getattr(app, f"action_{action}", None) or (lambda: None),
                help=f"action_{action}",
            )
