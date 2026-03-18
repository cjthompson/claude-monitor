"""Tests for state persistence (state.json and config.json)."""

import json
import os

import pytest

from claude_monitor.settings import Settings, save_settings, load_settings


class TestStatePersistence:
    """Test state.json and config.json read/write."""

    async def test_state_json_exists_after_mount(self, app_fixture, isolated_state, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            assert os.path.exists(isolated_state["state_file"])

    async def test_state_json_updated_on_pause(self, app_fixture, isolated_state, inject_event):
        async with app_fixture.run_test(size=(120, 40)) as pilot:
            await pilot.pause()

            await pilot.press("a")
            await pilot.pause()

            with open(isolated_state["state_file"]) as f:
                state = json.load(f)
            assert state["global_paused"] is True

    def test_config_json_saved_on_settings(self, isolated_state):
        s = Settings(theme="nord", debug=True, default_mode="manual")
        save_settings(s)

        with open(isolated_state["config_file"]) as f:
            data = json.load(f)
        assert data["theme"] == "nord"
        assert data["debug"] is True
        assert data["default_mode"] == "manual"

    def test_config_json_excludes_oauth(self, isolated_state):
        """oauth_json should not be persisted to disk."""
        s = Settings(oauth_json='{"access_token": "secret123"}')
        save_settings(s)

        with open(isolated_state["config_file"]) as f:
            data = json.load(f)
        assert "oauth_json" not in data

    def test_settings_load_defaults_when_missing(self, isolated_state):
        """Loading from non-existent config returns defaults."""
        # Make sure config doesn't exist
        config_file = isolated_state["config_file"]
        if os.path.exists(config_file):
            os.remove(config_file)

        s = load_settings()
        assert s.theme == "textual-dark"
        assert s.default_mode == "auto"
        assert s.debug is False

    def test_settings_load_ignores_unknown_fields(self, isolated_state):
        """Unknown fields in config.json should be ignored, not cause errors."""
        data = {
            "theme": "dracula",
            "unknown_field": True,
            "another_unknown": [1, 2, 3],
        }
        with open(isolated_state["config_file"], "w") as f:
            json.dump(data, f)

        s = load_settings()
        assert s.theme == "dracula"
        # Should not have unknown attributes
        assert not hasattr(s, "unknown_field")
