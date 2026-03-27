"""Tests for install.configure_hooks() — smart hook merging logic."""

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

_FIXTURES_DIR = Path(__file__).parent / "fixtures" / "installer_hooks"

# install.py is a root-level script, not a package module
sys.path.insert(0, str(Path(__file__).parent.parent))
import install  # noqa: E402

# ── constants used across tests ───────────────────────────────────────────────

NEW_CMD = "/new/venv/bin/claude-monitor-hook"
OLD_CMD = "/old/venv/bin/claude-monitor-hook"

# A HOOKS_CONFIG built around NEW_CMD (mirrors install.HOOKS_CONFIG structure)
NEW_HOOKS_CONFIG = {
    "PermissionRequest": [
        {"hooks": [{"type": "command", "command": NEW_CMD, "timeout": 300}]}
    ],
    "Notification": [
        {
            "matcher": "permission_prompt|idle_prompt",
            "hooks": [{"type": "command", "command": NEW_CMD, "timeout": 5}],
        }
    ],
    "SubagentStart": [
        {"hooks": [{"type": "command", "command": NEW_CMD, "timeout": 5}]}
    ],
    "SubagentStop": [
        {"hooks": [{"type": "command", "command": NEW_CMD, "timeout": 5}]}
    ],
    "PostToolUse": [
        {
            "matcher": "AskUserQuestion",
            "hooks": [{"type": "command", "command": NEW_CMD, "timeout": 5}],
        }
    ],
}


def _stale_config():
    """Return a hooks block identical to NEW_HOOKS_CONFIG but with OLD_CMD."""
    result = {}
    for event_type, groups in NEW_HOOKS_CONFIG.items():
        result[event_type] = [
            {k: v for k, v in group.items() if k != "hooks"}
            | {"hooks": [{**h, "command": OLD_CMD} for h in group["hooks"]]}
            for group in groups
        ]
    return result


# ── helpers ───────────────────────────────────────────────────────────────────

def _run(sf: Path, inputs: list[str]) -> None:
    """Patch install globals and run configure_hooks() with simulated input."""
    it = iter(inputs)
    with (
        patch.object(install, "SETTINGS_FILE", sf),
        patch.object(install, "HOOK_COMMAND", NEW_CMD),
        patch.object(install, "HOOKS_CONFIG", NEW_HOOKS_CONFIG),
        patch("builtins.input", side_effect=lambda _: next(it)),
    ):
        install.configure_hooks()


def _load(sf: Path) -> dict:
    return json.loads(sf.read_text())


def _all_commands(hooks_block: dict) -> list[str]:
    return [
        h["command"]
        for groups in hooks_block.values()
        for group in groups
        for h in group.get("hooks", [])
    ]


# ── fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture
def sf(tmp_path):
    return tmp_path / "settings.json"


# ── user declines ─────────────────────────────────────────────────────────────

class TestUserDeclines:
    def test_no_skips_file_creation(self, sf):
        _run(sf, ["n"])
        assert not sf.exists()

    def test_empty_answer_means_no(self, sf):
        _run(sf, [""])
        assert not sf.exists()

    def test_existing_file_untouched_when_user_declines(self, sf):
        sf.write_text(json.dumps({"theme": "dark"}))
        _run(sf, ["n"])
        assert _load(sf) == {"theme": "dark"}


# ── no existing file ──────────────────────────────────────────────────────────

class TestNoExistingFile:
    def test_creates_file_with_all_event_types(self, sf):
        _run(sf, ["y"])
        hooks = _load(sf)["hooks"]
        assert set(hooks) == set(NEW_HOOKS_CONFIG)

    def test_created_hooks_use_new_cmd(self, sf):
        _run(sf, ["y"])
        cmds = _all_commands(_load(sf)["hooks"])
        assert all(c == NEW_CMD for c in cmds)
        assert len(cmds) == len(NEW_HOOKS_CONFIG)

    def test_notification_matcher_preserved(self, sf):
        _run(sf, ["y"])
        notif = _load(sf)["hooks"]["Notification"]
        assert notif[0]["matcher"] == "permission_prompt|idle_prompt"

    def test_posttooluse_matcher_preserved(self, sf):
        _run(sf, ["y"])
        ptu = _load(sf)["hooks"]["PostToolUse"]
        assert ptu[0]["matcher"] == "AskUserQuestion"


# ── empty / partial hooks section ─────────────────────────────────────────────

class TestEmptyOrPartialHooks:
    def test_empty_hooks_dict_adds_all(self, sf):
        sf.write_text(json.dumps({"hooks": {}}))
        _run(sf, ["y"])
        assert set(_load(sf)["hooks"]) == set(NEW_HOOKS_CONFIG)

    def test_no_hooks_key_adds_all(self, sf):
        sf.write_text(json.dumps({"apiKeyHelper": "foo"}))
        _run(sf, ["y"])
        assert set(_load(sf)["hooks"]) == set(NEW_HOOKS_CONFIG)

    def test_other_top_level_keys_preserved(self, sf):
        sf.write_text(json.dumps({"theme": "dark", "autoUpdaterEnabled": True}))
        _run(sf, ["y"])
        settings = _load(sf)
        assert settings["theme"] == "dark"
        assert settings["autoUpdaterEnabled"] is True


# ── exact match (already configured) ─────────────────────────────────────────

class TestExactMatch:
    def test_all_exact_no_file_change_content(self, sf):
        sf.write_text(json.dumps({"hooks": NEW_HOOKS_CONFIG}))
        original_mtime = sf.stat().st_mtime
        _run(sf, ["y"])
        # Content should be identical to NEW_HOOKS_CONFIG
        assert _load(sf)["hooks"] == NEW_HOOKS_CONFIG

    def test_exact_match_no_duplicate_groups(self, sf):
        sf.write_text(json.dumps({"hooks": NEW_HOOKS_CONFIG}))
        _run(sf, ["y"])
        hooks = _load(sf)["hooks"]
        for event_type, groups in NEW_HOOKS_CONFIG.items():
            assert len(hooks[event_type]) == len(groups), (
                f"{event_type}: expected {len(groups)} group(s), got {len(hooks[event_type])}"
            )

    def test_partial_exact_match_adds_missing(self, sf):
        # Only PermissionRequest is already correct; others are absent
        sf.write_text(json.dumps({
            "hooks": {
                "PermissionRequest": NEW_HOOKS_CONFIG["PermissionRequest"]
            }
        }))
        _run(sf, ["y"])
        hooks = _load(sf)["hooks"]
        assert set(hooks) == set(NEW_HOOKS_CONFIG)
        # PermissionRequest still exactly one group
        assert len(hooks["PermissionRequest"]) == 1


# ── stale claude-monitor hook (different path) ────────────────────────────────

class TestStaleHook:
    def test_user_accepts_replaces_all_commands(self, sf):
        sf.write_text(json.dumps({"hooks": _stale_config()}))
        _run(sf, ["y", "y"])  # yes configure, yes replace
        cmds = _all_commands(_load(sf)["hooks"])
        assert all(c == NEW_CMD for c in cmds)

    def test_user_accepts_removes_old_cmd(self, sf):
        sf.write_text(json.dumps({"hooks": _stale_config()}))
        _run(sf, ["y", "y"])
        cmds = _all_commands(_load(sf)["hooks"])
        assert OLD_CMD not in cmds

    def test_user_declines_keeps_old_cmd(self, sf):
        sf.write_text(json.dumps({"hooks": _stale_config()}))
        _run(sf, ["y", "n"])  # yes configure, no replace
        cmds = _all_commands(_load(sf)["hooks"])
        assert all(c == OLD_CMD for c in cmds)

    def test_asks_only_once_regardless_of_event_count(self, sf):
        sf.write_text(json.dumps({"hooks": _stale_config()}))
        call_count = [0]
        answers = iter(["y", "y"])

        def _mock_input(prompt):
            call_count[0] += 1
            return next(answers)

        with (
            patch.object(install, "SETTINGS_FILE", sf),
            patch.object(install, "HOOK_COMMAND", NEW_CMD),
            patch.object(install, "HOOKS_CONFIG", NEW_HOOKS_CONFIG),
            patch("builtins.input", side_effect=_mock_input),
        ):
            install.configure_hooks()

        # Exactly 2 prompts: "Configure?" + "Replace?" (asked once for all events)
        assert call_count[0] == 2

    def test_stale_single_event_replaced_others_added(self, sf):
        sf.write_text(json.dumps({
            "hooks": {
                "PermissionRequest": [
                    {"hooks": [{"type": "command", "command": OLD_CMD, "timeout": 300}]}
                ]
            }
        }))
        _run(sf, ["y", "y"])
        hooks = _load(sf)["hooks"]
        # Replaced
        pr_cmd = hooks["PermissionRequest"][0]["hooks"][0]["command"]
        assert pr_cmd == NEW_CMD
        # All other event types created fresh
        for event_type in NEW_HOOKS_CONFIG:
            assert event_type in hooks

    def test_stale_hook_keeps_other_hook_fields(self, sf):
        """Replacing the command should leave timeout and type from desired config."""
        sf.write_text(json.dumps({"hooks": _stale_config()}))
        _run(sf, ["y", "y"])
        hooks = _load(sf)["hooks"]
        pr_hook = hooks["PermissionRequest"][0]["hooks"][0]
        assert pr_hook["type"] == "command"
        assert pr_hook["timeout"] == 300


# ── appending to existing unrelated hooks ─────────────────────────────────────

class TestAppendToExisting:
    def test_unrelated_hook_preserved(self, sf):
        sf.write_text(json.dumps({
            "hooks": {
                "PermissionRequest": [
                    {"hooks": [{"type": "command", "command": "/unrelated/tool"}]}
                ]
            }
        }))
        _run(sf, ["y"])
        pr_cmds = [
            h["command"]
            for g in _load(sf)["hooks"]["PermissionRequest"]
            for h in g["hooks"]
        ]
        assert "/unrelated/tool" in pr_cmds
        assert NEW_CMD in pr_cmds

    def test_monitor_hook_merged_into_existing_group(self, sf):
        # Same matcher (none) → hooks merged into one group, not two groups
        sf.write_text(json.dumps({
            "hooks": {
                "SubagentStart": [
                    {"hooks": [{"type": "command", "command": "/other/hook"}]}
                ]
            }
        }))
        _run(sf, ["y"])
        groups = _load(sf)["hooks"]["SubagentStart"]
        assert len(groups) == 1  # merged into single group
        cmds = [h["command"] for h in groups[0]["hooks"]]
        assert "/other/hook" in cmds
        assert NEW_CMD in cmds

    def test_empty_event_type_list_gets_monitor_hook(self, sf):
        # Event type key exists but has empty list
        sf.write_text(json.dumps({"hooks": {"PermissionRequest": []}}))
        _run(sf, ["y"])
        hooks = _load(sf)["hooks"]
        pr_cmds = [h["command"] for g in hooks["PermissionRequest"] for h in g["hooks"]]
        assert NEW_CMD in pr_cmds


# ── mixed scenario ────────────────────────────────────────────────────────────

class TestMixedScenario:
    def test_exact_stale_unrelated_missing_all_handled(self, sf):
        initial = {
            "hooks": {
                # exact match → skip
                "PermissionRequest": NEW_HOOKS_CONFIG["PermissionRequest"],
                # stale → replace (if accepted)
                "Notification": _stale_config()["Notification"],
                # unrelated hook → append
                "SubagentStart": [
                    {"hooks": [{"type": "command", "command": "/unrelated"}]}
                ],
                # SubagentStop and PostToolUse absent → create
            }
        }
        sf.write_text(json.dumps(initial))
        _run(sf, ["y", "y"])  # yes configure, yes replace
        hooks = _load(sf)["hooks"]

        # Exact match: unchanged, no duplicates
        assert len(hooks["PermissionRequest"]) == 1
        assert hooks["PermissionRequest"][0]["hooks"][0]["command"] == NEW_CMD

        # Stale replaced
        notif_cmds = [h["command"] for g in hooks["Notification"] for h in g["hooks"]]
        assert NEW_CMD in notif_cmds
        assert OLD_CMD not in notif_cmds

        # Unrelated preserved, monitor merged into same group (no matcher on either)
        assert len(hooks["SubagentStart"]) == 1
        sa_cmds = [h["command"] for h in hooks["SubagentStart"][0]["hooks"]]
        assert "/unrelated" in sa_cmds
        assert NEW_CMD in sa_cmds

        # Missing event types created
        assert "SubagentStop" in hooks
        assert "PostToolUse" in hooks

    def test_mixed_stale_declines_but_missing_still_added(self, sf):
        """If user declines stale replacement, missing event types are still added."""
        initial = {
            "hooks": {
                "PermissionRequest": _stale_config()["PermissionRequest"],
                # All others absent
            }
        }
        sf.write_text(json.dumps(initial))
        _run(sf, ["y", "n"])  # yes configure, no replace
        hooks = _load(sf)["hooks"]

        # Stale hook untouched
        pr_cmd = hooks["PermissionRequest"][0]["hooks"][0]["command"]
        assert pr_cmd == OLD_CMD

        # Other event types still added
        for event_type in ("Notification", "SubagentStart", "SubagentStop", "PostToolUse"):
            assert event_type in hooks
            cmds = [h["command"] for g in hooks[event_type] for h in g["hooks"]]
            assert NEW_CMD in cmds


# ── JSON fixture-driven tests ─────────────────────────────────────────────────
#
# Each file in tests/fixtures/installer_hooks/ describes one scenario as JSON:
#
#   initial_settings  — the settings.json content before the run (null = no file)
#   user_inputs       — ordered list of answers to input() calls
#   expected          — the full settings.json content expected after the run
#                       (null = file should not exist, e.g. user declined)
#
# Open any .json file to read the scenario; run this test to verify it.

def _fixture_files():
    return sorted(_FIXTURES_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_path", _fixture_files(), ids=lambda p: p.stem)
def test_fixture(fixture_path, sf):
    case = json.loads(fixture_path.read_text())

    if case["initial_settings"] is not None:
        sf.write_text(json.dumps(case["initial_settings"]))

    _run(sf, case["user_inputs"])

    if case["expected"] is None:
        assert not sf.exists(), "Expected no settings file to exist after run"
    else:
        assert sf.exists(), "Expected settings file to be written"
        assert _load(sf) == case["expected"]
