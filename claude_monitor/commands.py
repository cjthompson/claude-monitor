"""Command palette provider for claude-monitor TUI."""

from textual.command import DiscoveryHit, Hits, Provider


class MonitorCommands(Provider):
    """Command palette provider exposing all TUI actions."""

    COMMANDS_LIST = [
        ("Toggle Auto/Manual (global)", "toggle_pause"),
        ("Show Choices Log", "show_choices"),
        ("Show Questions Log", "show_questions"),
        ("Refresh Layout", "refresh_layout"),
        ("Open Settings", "open_settings"),
        ("Show Help", "show_help"),
        ("Dashboard: Grow", "grow_dashboard"),
        ("Dashboard: Shrink", "shrink_dashboard"),
        ("Next Tab", "next_tab"),
        ("Previous Tab", "prev_tab"),
        ("Quit", "quit"),
    ]

    async def startup(self) -> None:
        pass

    async def search(self, query: str) -> Hits:
        app = self.app
        matcher = self.matcher(query)
        for name, action in self.COMMANDS_LIST:
            score = matcher.match(name)
            if score > 0:
                yield DiscoveryHit(
                    name,
                    getattr(app, f"action_{action}", None) or (lambda: None),
                    help=f"action_{action}",
                    score=score,
                )

    async def discover(self) -> Hits:
        app = self.app
        for name, action in self.COMMANDS_LIST:
            yield DiscoveryHit(
                name,
                getattr(app, f"action_{action}", None) or (lambda: None),
                help=f"action_{action}",
            )
