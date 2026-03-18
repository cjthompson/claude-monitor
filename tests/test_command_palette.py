"""Tests for command palette."""

import pytest

from claude_monitor.commands import MonitorCommands


class TestCommandPalette:
    """Test command palette provider."""

    def test_all_commands_listed(self):
        """MonitorCommands.COMMANDS_LIST should contain expected commands."""
        names = [name for name, _ in MonitorCommands.COMMANDS_LIST]
        assert any("Auto/Manual" in n for n in names)
        assert any("Choices" in n for n in names)
        assert any("Questions" in n for n in names)
        assert any("Settings" in n for n in names)
        assert any("Help" in n for n in names)
        assert any("Quit" in n for n in names)
        assert any("Next Tab" in n for n in names)
        assert any("Previous Tab" in n for n in names)

    def test_commands_match_actions(self):
        """Every command's action should be a plausible method name."""
        for name, action in MonitorCommands.COMMANDS_LIST:
            # action should be a valid Python identifier
            assert action.isidentifier(), f"Action {action!r} for {name!r} is not a valid identifier"
            # Should correspond to action_{action} method pattern
            assert not action.startswith("action_"), f"Action should not include 'action_' prefix: {action}"
