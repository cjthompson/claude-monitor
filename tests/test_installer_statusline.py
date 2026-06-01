"""Tests for install.configure_statusline — chain/replace/skip handling."""

import json
from pathlib import Path
from unittest.mock import patch

import pytest

import install

STATUSLINE_CMD = "/path/to/.venv/bin/claude-monitor-statusline"


def _run(sf: Path, inputs: list[str]) -> None:
    """Patch install globals and run configure_statusline with simulated input."""
    it = iter(inputs)
    with (
        patch.object(install, "SETTINGS_FILE", sf),
        patch.object(install, "STATUSLINE_COMMAND", STATUSLINE_CMD),
        patch("builtins.input", side_effect=lambda _: next(it)),
    ):
        install.configure_statusline()


def _load(sf: Path) -> dict:
    return json.loads(sf.read_text())


@pytest.fixture
def sf(tmp_path):
    return tmp_path / "settings.json"


class TestUserDeclines:
    def test_top_level_no_skips(self, sf):
        _run(sf, ["n"])
        assert not sf.exists()

    def test_existing_file_untouched(self, sf):
        sf.write_text(json.dumps({"theme": "dark"}))
        _run(sf, ["n"])
        assert _load(sf) == {"theme": "dark"}


class TestNoExistingStatusLine:
    def test_no_file_writes_object_form(self, sf):
        _run(sf, ["y"])
        settings = _load(sf)
        assert settings["statusLine"] == {"type": "command", "command": STATUSLINE_CMD}

    def test_file_without_statusline_writes_object_form(self, sf):
        sf.write_text(json.dumps({"theme": "dark"}))
        _run(sf, ["y"])
        settings = _load(sf)
        assert settings["theme"] == "dark"
        assert settings["statusLine"] == {"type": "command", "command": STATUSLINE_CMD}


class TestExistingStatusLineChain:
    def test_object_form_chained_by_default(self, sf):
        sf.write_text(
            json.dumps(
                {"statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"}}
            )
        )
        _run(sf, ["y", ""])  # accept, then default (chain)
        cmd = _load(sf)["statusLine"]["command"]
        assert "CLAUDE_MONITOR_STATUSLINE_NEXT='bash ~/.claude/statusline.sh'" in cmd
        assert cmd.endswith(STATUSLINE_CMD)

    def test_string_form_chained(self, sf):
        sf.write_text(json.dumps({"statusLine": "bash ~/.claude/statusline.sh"}))
        _run(sf, ["y", "c"])
        cmd = _load(sf)["statusLine"]["command"]
        assert "CLAUDE_MONITOR_STATUSLINE_NEXT='bash ~/.claude/statusline.sh'" in cmd
        assert cmd.endswith(STATUSLINE_CMD)
        # Output is normalized to the object form
        assert _load(sf)["statusLine"]["type"] == "command"

    def test_command_with_single_quote_is_escaped(self, sf):
        sf.write_text(
            json.dumps({"statusLine": {"type": "command", "command": "bash -c 'echo hi'"}})
        )
        _run(sf, ["y", "c"])
        cmd = _load(sf)["statusLine"]["command"]
        # Single-quote inside the existing command must be safely shell-quoted
        assert "CLAUDE_MONITOR_STATUSLINE_NEXT='bash -c '\\''echo hi'\\''' " in cmd


class TestExistingStatusLineReplace:
    def test_replace_writes_bare_command(self, sf):
        sf.write_text(
            json.dumps(
                {"statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"}}
            )
        )
        _run(sf, ["y", "r"])
        assert _load(sf)["statusLine"] == {"type": "command", "command": STATUSLINE_CMD}


class TestExistingStatusLineSkip:
    def test_skip_leaves_file_unchanged(self, sf):
        original = {
            "theme": "dark",
            "statusLine": {"type": "command", "command": "bash ~/.claude/statusline.sh"},
        }
        sf.write_text(json.dumps(original))
        _run(sf, ["y", "s"])
        assert _load(sf) == original


class TestAlreadyConfigured:
    def test_chained_form_detected_and_skipped(self, sf):
        chained = (
            "CLAUDE_MONITOR_STATUSLINE_NEXT='bash ~/.claude/statusline.sh' " + STATUSLINE_CMD
        )
        original = {"statusLine": {"type": "command", "command": chained}}
        sf.write_text(json.dumps(original))
        _run(sf, ["y"])  # no second prompt because we short-circuit
        assert _load(sf) == original

    def test_bare_form_detected_and_skipped(self, sf):
        original = {"statusLine": {"type": "command", "command": STATUSLINE_CMD}}
        sf.write_text(json.dumps(original))
        _run(sf, ["y"])
        assert _load(sf) == original
