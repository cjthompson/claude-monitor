"""Extended tests for settings.py — validation, edge cases, save/load errors."""

import json
import os

import pytest

from claude_monitor.settings import (
    Settings,
    load_settings,
    save_settings,
    _mask_oauth_json,
    _widget_id,
    THEMES,
)


class TestSettingsDefaults:
    def test_defaults(self):
        s = Settings()
        assert s.default_mode == "auto"
        assert s.theme == "textual-dark"
        assert s.debug is False
        assert s.excluded_tools == []
        assert s.ask_user_timeout == 0

    def test_none_excluded_tools(self):
        s = Settings(excluded_tools=None)
        assert s.excluded_tools == []


class TestSettingsValidation:
    def test_invalid_default_mode(self):
        s = Settings(default_mode="invalid")
        assert s.default_mode == "auto"

    def test_invalid_iterm_scope(self):
        s = Settings(iterm_scope="invalid")
        assert s.iterm_scope == "current_tab"

    def test_invalid_timestamp_style(self):
        s = Settings(timestamp_style="invalid")
        assert s.timestamp_style == "24hr"

    def test_invalid_tab_close_mode(self):
        s = Settings(tab_close_mode="invalid")
        assert s.tab_close_mode == "none"

    def test_ask_user_timeout_clamped_high(self):
        s = Settings(ask_user_timeout=999)
        assert s.ask_user_timeout == 300

    def test_ask_user_timeout_clamped_low(self):
        s = Settings(ask_user_timeout=-5)
        assert s.ask_user_timeout == 0

    def test_sparkline_bucket_secs_min(self):
        s = Settings(sparkline_bucket_secs=0)
        assert s.sparkline_bucket_secs == 1

    def test_dashboard_height_min(self):
        s = Settings(dashboard_height=1)
        assert s.dashboard_height == 3

    def test_tab_idle_timeout_clamped(self):
        s = Settings(tab_idle_timeout_secs=5)
        assert s.tab_idle_timeout_secs == 10
        s = Settings(tab_idle_timeout_secs=9999)
        assert s.tab_idle_timeout_secs == 3600


class TestSettingsPersistence:
    def test_save_and_load(self, isolated_state):
        s = Settings(theme="nord", debug=True, ask_user_timeout=30)
        save_settings(s)
        loaded = load_settings()
        assert loaded.theme == "nord"
        assert loaded.debug is True
        assert loaded.ask_user_timeout == 30

    def test_load_missing_file(self, isolated_state):
        s = load_settings()
        assert s.default_mode == "auto"

    def test_load_corrupt_file(self, isolated_state):
        with open(isolated_state["config_file"], "w") as f:
            f.write("not json{{{")
        s = load_settings()
        assert s.default_mode == "auto"

    def test_save_excludes_oauth_json(self, isolated_state):
        s = Settings(oauth_json='{"access_token": "secret"}')
        save_settings(s)
        with open(isolated_state["config_file"]) as f:
            data = json.load(f)
        assert "oauth_json" not in data

    def test_load_ignores_unknown_fields(self, isolated_state):
        with open(isolated_state["config_file"], "w") as f:
            json.dump({"theme": "monokai", "unknown_field": True}, f)
        s = load_settings()
        assert s.theme == "monokai"

    def test_save_creates_dir(self, tmp_path, monkeypatch):
        import claude_monitor.settings as settings_mod
        new_dir = str(tmp_path / "newdir")
        new_file = os.path.join(new_dir, "config.json")
        monkeypatch.setattr(settings_mod, "CONFIG_DIR", new_dir)
        monkeypatch.setattr(settings_mod, "CONFIG_FILE", new_file)
        save_settings(Settings())
        assert os.path.exists(new_file)


class TestMaskOauthJson:
    def test_empty(self):
        assert _mask_oauth_json("") == ""

    def test_valid_json(self):
        raw = json.dumps({"access_token": "abcdefghijklmnop", "refresh_token": "1234567890abcdef"})
        result = _mask_oauth_json(raw)
        parsed = json.loads(result)
        assert parsed["access_token"].startswith("abcdefgh")
        assert "••••••••" in parsed["access_token"]

    def test_short_token(self):
        raw = json.dumps({"access_token": "short"})
        result = _mask_oauth_json(raw)
        parsed = json.loads(result)
        assert parsed["access_token"] == "••••••••"

    def test_invalid_json(self):
        assert _mask_oauth_json("not json") == "not json"

    def test_no_tokens(self):
        raw = json.dumps({"other": "value"})
        result = _mask_oauth_json(raw)
        assert "other" in result


class TestWidgetId:
    def test_basic(self):
        assert _widget_id("default_mode") == "field-default-mode"

    def test_no_underscores(self):
        assert _widget_id("theme") == "field-theme"


class TestWebLanAccess:
    def test_default_is_false(self):
        s = Settings()
        assert s.web_lan_access is False

    def test_persistence(self, isolated_state):
        s = Settings(web_lan_access=True)
        save_settings(s)
        loaded = load_settings()
        assert loaded.web_lan_access is True

    def test_persistence_false(self, isolated_state):
        s = Settings(web_lan_access=False)
        save_settings(s)
        with open(isolated_state["config_file"]) as f:
            data = json.load(f)
        assert data["web_lan_access"] is False
