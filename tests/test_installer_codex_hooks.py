"""Tests for install.configure_codex_hooks()."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
import install  # noqa: E402

NEW_CMD = "/new/venv/bin/claude-monitor-hook"
OLD_CMD = "/old/venv/bin/claude-monitor-hook"

NEW_CODEX_HOOKS_CONFIG = {
    "PermissionRequest": [
        {
            "hooks": [
                {
                    "type": "command",
                    "command": NEW_CMD,
                    "timeout": 300,
                    "statusMessage": "Checking claude-monitor approval state",
                }
            ]
        }
    ]
}


@pytest.fixture
def hooks_file(tmp_path):
    return tmp_path / "hooks.json"


def _run(hooks_file: Path, inputs: list[str]) -> None:
    answers = iter(inputs)
    with (
        patch.object(install, "CODEX_HOOKS_FILE", hooks_file),
        patch.object(install, "HOOK_COMMAND", NEW_CMD),
        patch.object(install, "CODEX_HOOKS_CONFIG", NEW_CODEX_HOOKS_CONFIG),
        patch("builtins.input", side_effect=lambda _: next(answers)),
    ):
        install.configure_codex_hooks()


def _load(hooks_file: Path) -> dict:
    return json.loads(hooks_file.read_text())


class TestCodexHookInstaller:
    def test_decline_does_not_create_file(self, hooks_file):
        _run(hooks_file, ["n"])
        assert not hooks_file.exists()

    def test_creates_codex_hooks_file(self, hooks_file):
        _run(hooks_file, ["y"])
        assert _load(hooks_file) == {"hooks": NEW_CODEX_HOOKS_CONFIG}

    def test_preserves_unrelated_permission_hook(self, hooks_file):
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PermissionRequest": [
                            {"hooks": [{"type": "command", "command": "/unrelated/hook"}]}
                        ]
                    }
                }
            )
        )

        _run(hooks_file, ["y"])

        commands = [
            hook["command"]
            for group in _load(hooks_file)["hooks"]["PermissionRequest"]
            for hook in group["hooks"]
        ]
        assert "/unrelated/hook" in commands
        assert NEW_CMD in commands

    def test_replaces_stale_monitor_hook_when_accepted(self, hooks_file):
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PermissionRequest": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": OLD_CMD,
                                        "timeout": 300,
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )

        _run(hooks_file, ["y", "y"])

        commands = [
            hook["command"]
            for group in _load(hooks_file)["hooks"]["PermissionRequest"]
            for hook in group["hooks"]
        ]
        assert NEW_CMD in commands
        assert OLD_CMD not in commands

    def test_keeps_stale_monitor_hook_when_declined(self, hooks_file):
        hooks_file.write_text(
            json.dumps(
                {
                    "hooks": {
                        "PermissionRequest": [
                            {
                                "hooks": [
                                    {
                                        "type": "command",
                                        "command": OLD_CMD,
                                        "timeout": 300,
                                    }
                                ]
                            }
                        ]
                    }
                }
            )
        )

        _run(hooks_file, ["y", "n"])

        commands = [
            hook["command"]
            for group in _load(hooks_file)["hooks"]["PermissionRequest"]
            for hook in group["hooks"]
        ]
        assert OLD_CMD in commands
        assert NEW_CMD not in commands
