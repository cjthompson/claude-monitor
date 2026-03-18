"""Command palette provider for claude-monitor TUI."""

from textual.command import DiscoveryHit, Hit, Hits, Provider


class MonitorCommands(Provider):
    """Command palette provider exposing all TUI actions."""

    COMMANDS_LIST = [
        ("Next Tab", "next_tab"),
        ("Open Settings", "open_settings"),
        ("Previous Tab", "prev_tab"),
        ("Quit", "quit"),
        ("Refresh Layout", "refresh_layout"),
        ("Show Choices Log", "show_choices"),
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
