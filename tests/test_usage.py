"""Tests for usage.py — OAuth token resolution, usage fetching, formatting."""

import json
import os
import time
from datetime import datetime, timezone, timedelta
from unittest.mock import patch, MagicMock

import pytest

from claude_monitor.usage import (
    _mask_token,
    _parse_window,
    _parse_oauth_json,
    _extract_oauth_from_code_env,
    _extract_oauth_from_dot_env,
    _extract_oauth_from_env,
    _load_disk_cache,
    _save_disk_cache,
    _usage_from_disk,
    _bar,
    _format_countdown,
    _format_local_time,
    _strip_markup,
    _quota,
    format_usage_inline,
    UsageData,
    UsageManager,
    WindowUsage,
    USAGE_CACHE_FILE,
    USAGE_MAX_AGE,
    TOKEN_EXPIRY_BUFFER,
)


# ---------------------------------------------------------------------------
# _mask_token
# ---------------------------------------------------------------------------

class TestMaskToken:
    def test_empty(self):
        assert _mask_token("") == "(empty)"

    def test_short(self):
        assert _mask_token("abc") == "***"
        assert _mask_token("123456789012") == "***"

    def test_long(self):
        result = _mask_token("abcdefghijklmnop")
        assert result.startswith("abcdefgh")
        assert result.endswith("mnop")
        assert "***" in result


# ---------------------------------------------------------------------------
# _parse_window
# ---------------------------------------------------------------------------

class TestParseWindow:
    def test_basic(self):
        w = _parse_window({"utilization": 42.5, "resets_at": "2024-01-01T12:00:00Z"})
        assert w.utilization == 42.5
        assert w.resets_at is not None

    def test_string_utilization(self):
        w = _parse_window({"utilization": "75.5"})
        assert w.utilization == 75.5

    def test_none_utilization(self):
        w = _parse_window({"utilization": None})
        assert w.utilization == 0

    def test_missing_utilization(self):
        w = _parse_window({})
        assert w.utilization == 0

    def test_invalid_resets_at(self):
        w = _parse_window({"resets_at": "not-a-date"})
        assert w.resets_at is None

    def test_no_resets_at(self):
        w = _parse_window({})
        assert w.resets_at is None


# ---------------------------------------------------------------------------
# _parse_oauth_json
# ---------------------------------------------------------------------------

class TestParseOauthJson:
    def test_valid_full(self):
        data = json.dumps({"access_token": "tok", "refresh_token": "ref", "expires_at": 1234567890.0})
        result = _parse_oauth_json(data)
        assert result == ("tok", "ref", 1234567890.0)

    def test_valid_minimal(self):
        data = json.dumps({"access_token": "tok"})
        result = _parse_oauth_json(data)
        assert result is not None
        assert result[0] == "tok"
        assert result[1] == ""
        # expires_at defaults to ~now+3600
        assert result[2] > time.time() - 10

    def test_no_access_token(self):
        data = json.dumps({"refresh_token": "ref"})
        assert _parse_oauth_json(data) is None

    def test_invalid_json(self):
        assert _parse_oauth_json("not json") is None

    def test_empty_string(self):
        assert _parse_oauth_json("") is None


# ---------------------------------------------------------------------------
# _extract_oauth_from_code_env
# ---------------------------------------------------------------------------

class TestExtractOauthFromCodeEnv:
    def test_present(self, monkeypatch):
        data = json.dumps({"access_token": "tok123", "refresh_token": "ref", "expires_at": 99999999999})
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", data)
        result = _extract_oauth_from_code_env()
        assert result is not None
        assert result[0] == "tok123"

    def test_missing(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        assert _extract_oauth_from_code_env() is None

    def test_empty(self, monkeypatch):
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
        assert _extract_oauth_from_code_env() is None


# ---------------------------------------------------------------------------
# _extract_oauth_from_dot_env
# ---------------------------------------------------------------------------

class TestExtractOauthFromDotEnv:
    def test_from_file(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        token_json = json.dumps({"access_token": "envtok"})
        env_file.write_text(f'CLAUDE_CODE_OAUTH_TOKEN={token_json}\n')
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / ".env") if p == "~/.env" else p)
        # The function checks two candidates: ~/.env and cwd/.env
        monkeypatch.chdir(tmp_path)
        result = _extract_oauth_from_dot_env()
        assert result is not None
        assert result[0] == "envtok"

    def test_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("os.path.expanduser", lambda p: str(tmp_path / "nonexistent") if p == "~/.env" else p)
        monkeypatch.chdir(tmp_path)
        assert _extract_oauth_from_dot_env() is None

    def test_comment_and_empty_lines(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        token_json = json.dumps({"access_token": "tok2"})
        env_file.write_text(f'# comment\n\nOTHER_VAR=hello\nCLAUDE_CODE_OAUTH_TOKEN={token_json}\n')
        monkeypatch.chdir(tmp_path)
        result = _extract_oauth_from_dot_env()
        assert result is not None
        assert result[0] == "tok2"

    def test_quoted_value(self, tmp_path, monkeypatch):
        env_file = tmp_path / ".env"
        token_json = json.dumps({"access_token": "qtok"})
        env_file.write_text(f'CLAUDE_CODE_OAUTH_TOKEN="{token_json}"\n')
        monkeypatch.chdir(tmp_path)
        result = _extract_oauth_from_dot_env()
        assert result is not None
        assert result[0] == "qtok"


# ---------------------------------------------------------------------------
# _extract_oauth_from_env
# ---------------------------------------------------------------------------

class TestExtractOauthFromEnv:
    def test_present(self, monkeypatch):
        data = json.dumps({"access_token": "envtok2"})
        monkeypatch.setenv("CLAUDE_OAUTH_TOKEN", data)
        result = _extract_oauth_from_env()
        assert result is not None
        assert result[0] == "envtok2"

    def test_missing(self, monkeypatch):
        monkeypatch.delenv("CLAUDE_OAUTH_TOKEN", raising=False)
        assert _extract_oauth_from_env() is None


# ---------------------------------------------------------------------------
# Disk cache
# ---------------------------------------------------------------------------

class TestDiskCache:
    def test_load_missing(self, monkeypatch):
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", "/tmp/nonexistent-test-cache.json")
        assert _load_disk_cache() == {}

    def test_save_and_load(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)
        now = time.time()
        resets = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        data = UsageData(
            five_hour=WindowUsage(utilization=50.0, resets_at=resets),
            seven_day=WindowUsage(utilization=30.0, resets_at=None),
        )
        _save_disk_cache(data, now)

        loaded = _load_disk_cache()
        assert "fetched_at" in loaded
        assert loaded["five_hour"]["utilization"] == 50.0

    def test_usage_from_disk_valid(self):
        entry = {
            "fetched_at": time.time(),
            "five_hour": {"utilization": 40.0},
            "seven_day": {"utilization": 20.0},
        }
        result = _usage_from_disk(entry)
        assert result is not None
        data, fetched_at = result
        assert data.five_hour.utilization == 40.0

    def test_usage_from_disk_invalid(self):
        assert _usage_from_disk({}) is None
        assert _usage_from_disk({"fetched_at": "not a number"}) is None

    def test_load_corrupt_json(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "corrupt-cache.json")
        with open(cache_file, "w") as f:
            f.write("not valid json{{{")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)
        assert _load_disk_cache() == {}


# ---------------------------------------------------------------------------
# Pure formatting helpers
# ---------------------------------------------------------------------------

class TestBar:
    def test_zero(self):
        result = _bar(0, 10)
        # Should contain empty background only
        assert len(_strip_markup(result)) == 10

    def test_full(self):
        result = _bar(100, 10)
        assert len(_strip_markup(result)) == 10

    def test_partial(self):
        result = _bar(50, 10)
        plain = _strip_markup(result)
        assert len(plain) == 10

    def test_paused_mode(self):
        result = _bar(50, 10, mode="paused")
        assert len(_strip_markup(result)) == 10

    def test_unknown_mode(self):
        result = _bar(50, 10, mode="unknown")
        # Falls back to "running" theme
        assert len(_strip_markup(result)) == 10


class TestFormatCountdown:
    def test_none(self):
        assert _format_countdown(None) == ""

    def test_past(self):
        past = datetime.now(timezone.utc) - timedelta(hours=1)
        assert _format_countdown(past) == "now"

    def test_future(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2, minutes=30)
        result = _format_countdown(future)
        assert "h" in result or "m" in result


class TestFormatLocalTime:
    def test_none(self):
        assert _format_local_time(None) == ""

    def test_today(self):
        # A time later today
        now = datetime.now(timezone.utc)
        later = now + timedelta(hours=2)
        result = _format_local_time(later)
        assert "AM" in result or "PM" in result

    def test_different_day(self):
        future = datetime.now(timezone.utc) + timedelta(days=2)
        result = _format_local_time(future)
        # Should include day name
        assert len(result) > 5


class TestStripMarkup:
    def test_no_markup(self):
        assert _strip_markup("hello world") == "hello world"

    def test_with_markup(self):
        assert _strip_markup("[bold]hello[/]") == "hello"

    def test_nested_markup(self):
        assert _strip_markup("[bold][red]hi[/][/]") == "hi"


class TestQuota:
    def test_basic_no_bar(self):
        w = WindowUsage(utilization=42.0, resets_at=None)
        result = _quota(w, "5h", None, "", "running")
        assert "5h" in result
        assert "42%" in result

    def test_with_bar(self):
        w = WindowUsage(utilization=75.0, resets_at=None)
        result = _quota(w, "7d", 8, "2h30m", "running")
        assert "7d" in result
        assert "75%" in result
        assert "2h30m" in result

    def test_paused_mode(self):
        w = WindowUsage(utilization=50.0, resets_at=None)
        result = _quota(w, "5h", 8, "", "paused")
        assert "5h" in result


class TestFormatUsageInline:
    def _make_data(self, h5=50.0, d7=30.0, h5_reset=None, d7_reset=None):
        return UsageData(
            five_hour=WindowUsage(utilization=h5, resets_at=h5_reset),
            seven_day=WindowUsage(utilization=d7, resets_at=d7_reset),
        )

    def test_wide_format(self):
        data = self._make_data()
        result = format_usage_inline(data, max_width=200)
        assert "5h" in result
        assert "7d" in result

    def test_narrow_format(self):
        data = self._make_data()
        result = format_usage_inline(data, max_width=30)
        assert "5h" in result

    def test_very_narrow(self):
        data = self._make_data()
        result = format_usage_inline(data, max_width=10)
        assert "5h" in result

    def test_with_resets(self):
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        data = self._make_data(h5_reset=future, d7_reset=future + timedelta(days=3))
        result = format_usage_inline(data, max_width=200)
        assert "5h" in result

    def test_paused_mode(self):
        data = self._make_data()
        result = format_usage_inline(data, max_width=200, mode="paused")
        assert "5h" in result

    def test_all_tiers(self):
        """Ensure every width tier returns something."""
        future = datetime.now(timezone.utc) + timedelta(hours=2)
        data = self._make_data(h5_reset=future, d7_reset=future + timedelta(days=3))
        for width in [200, 100, 80, 60, 50, 40, 30, 20, 10]:
            result = format_usage_inline(data, max_width=width)
            assert len(result) > 0


# ---------------------------------------------------------------------------
# UsageManager
# ---------------------------------------------------------------------------

class TestUsageManager:
    def test_set_oauth_json_invalidates_cache(self):
        mgr = UsageManager()
        mgr._token_cache = {"token": "old", "expires_at": time.time() + 9999}
        mgr.set_oauth_json('{"access_token": "new"}')
        assert mgr._token_cache == {}

    def test_set_oauth_json_same_no_invalidate(self):
        mgr = UsageManager()
        mgr.set_oauth_json('{"access_token": "tok"}')
        mgr._token_cache = {"token": "tok", "expires_at": time.time() + 9999}
        mgr.set_oauth_json('{"access_token": "tok"}')
        # Same JSON, cache not invalidated
        assert mgr._token_cache.get("token") == "tok"

    def test_set_on_token_refreshed(self):
        mgr = UsageManager()
        cb = lambda t, r, e: None
        mgr.set_on_token_refreshed(cb)
        assert mgr._on_token_refreshed is cb

    def test_get_token_from_settings_json(self):
        mgr = UsageManager()
        future = time.time() + 3600
        mgr.set_oauth_json(json.dumps({"access_token": "settok", "refresh_token": "ref", "expires_at": future}))
        token = mgr.get_token()
        assert token == "settok"

    def test_get_token_cached(self):
        mgr = UsageManager()
        future = time.time() + 3600
        mgr._token_cache = {"token": "cached", "refresh_token": "ref", "expires_at": future}
        assert mgr.get_token() == "cached"

    def test_get_token_from_env(self, monkeypatch):
        mgr = UsageManager()
        data = json.dumps({"access_token": "envtok", "expires_at": time.time() + 3600})
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", data)
        token = mgr.get_token()
        assert token == "envtok"

    def test_get_token_no_sources(self, monkeypatch):
        mgr = UsageManager()
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_OAUTH_TOKEN", raising=False)
        # Mock keychain to return None
        monkeypatch.setattr("claude_monitor.usage._read_keychain", lambda: None)
        # Mock dot env to return None
        monkeypatch.setattr("claude_monitor.usage._extract_oauth_from_dot_env", lambda: None)
        assert mgr.get_token() is None

    def test_get_token_expired_triggers_refresh(self, monkeypatch):
        mgr = UsageManager()
        past = time.time() - 100
        mgr.set_oauth_json(json.dumps({"access_token": "old", "refresh_token": "ref", "expires_at": past}))
        # Mock refresh to succeed
        monkeypatch.setattr(mgr, "_refresh_access_token", lambda rt: ("newtok", "newref", time.time() + 3600))
        token = mgr.get_token()
        assert token == "newtok"

    def test_get_token_expired_refresh_fails_returns_old(self, monkeypatch):
        mgr = UsageManager()
        past = time.time() - 100
        mgr.set_oauth_json(json.dumps({"access_token": "old", "refresh_token": "ref", "expires_at": past}))
        monkeypatch.setattr(mgr, "_refresh_access_token", lambda rt: None)
        token = mgr.get_token()
        assert token == "old"

    def test_get_token_cached_expired_refresh_via_cache(self, monkeypatch):
        mgr = UsageManager()
        past = time.time() - 100
        mgr._token_cache = {"token": "old", "refresh_token": "ref", "expires_at": past}
        monkeypatch.setattr(mgr, "_refresh_access_token", lambda rt: ("refreshed", "newref", time.time() + 3600))
        token = mgr.get_token()
        assert token == "refreshed"

    def test_invalidate_cache(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)
        # Create a cache file
        with open(cache_file, "w") as f:
            json.dump({"fetched_at": time.time()}, f)

        mgr = UsageManager()
        mgr._usage_cache = {"data": "something", "fetched_at": time.time()}
        mgr.invalidate_cache()
        assert mgr._usage_cache == {}
        assert not os.path.exists(cache_file)

    def test_invalidate_cache_no_file(self, tmp_path, monkeypatch):
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", str(tmp_path / "nonexistent.json"))
        mgr = UsageManager()
        mgr.invalidate_cache()  # Should not raise

    def test_fetch_returns_cached(self):
        mgr = UsageManager()
        data = UsageData(
            five_hour=WindowUsage(utilization=50.0, resets_at=None),
            seven_day=WindowUsage(utilization=30.0, resets_at=None),
        )
        mgr._usage_cache = {"data": data, "fetched_at": time.time()}
        result = mgr.fetch()
        assert result is data

    def test_fetch_from_disk_cache(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)
        now = time.time()
        with open(cache_file, "w") as f:
            json.dump({
                "fetched_at": now,
                "five_hour": {"utilization": 42.0},
                "seven_day": {"utilization": 20.0},
            }, f)

        mgr = UsageManager()
        # Mock get_token to avoid actual token resolution
        monkeypatch.setattr(mgr, "get_token", lambda: None)
        result = mgr.fetch()
        assert result is not None
        assert result.five_hour.utilization == 42.0

    def test_fetch_api_success(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)

        mgr = UsageManager()
        mgr._token_cache = {"token": "tok", "refresh_token": "", "expires_at": time.time() + 3600}

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({
            "five_hour": {"utilization": 60.0, "resets_at": "2024-06-15T12:00:00Z"},
            "seven_day": {"utilization": 25.0},
        }).encode()

        monkeypatch.setattr("claude_monitor.usage.subprocess.run", lambda *a, **kw: mock_result)

        result = mgr.fetch()
        assert result is not None
        assert result.five_hour.utilization == 60.0

    def test_fetch_api_error_returns_stale(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)

        stale_data = UsageData(
            five_hour=WindowUsage(utilization=40.0, resets_at=None),
            seven_day=WindowUsage(utilization=20.0, resets_at=None),
        )

        mgr = UsageManager()
        mgr._usage_cache = {"data": stale_data, "fetched_at": 0}  # expired cache
        mgr._token_cache = {"token": "tok", "refresh_token": "", "expires_at": time.time() + 3600}

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = b""
        monkeypatch.setattr("claude_monitor.usage.subprocess.run", lambda *a, **kw: mock_result)

        result = mgr.fetch()
        assert result is stale_data

    def test_fetch_api_error_response(self, tmp_path, monkeypatch):
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)

        mgr = UsageManager()
        mgr._token_cache = {"token": "tok", "refresh_token": "", "expires_at": time.time() + 3600}

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = json.dumps({"error": {"message": "unauthorized"}}).encode()
        monkeypatch.setattr("claude_monitor.usage.subprocess.run", lambda *a, **kw: mock_result)

        result = mgr.fetch()
        assert result is None  # No stale data available

    def test_fetch_no_token_returns_stale(self, monkeypatch):
        mgr = UsageManager()
        stale_data = UsageData(
            five_hour=WindowUsage(utilization=30.0, resets_at=None),
            seven_day=WindowUsage(utilization=10.0, resets_at=None),
        )
        mgr._usage_cache = {"data": stale_data, "fetched_at": 0}
        monkeypatch.setattr(mgr, "get_token", lambda: None)
        result = mgr.fetch()
        assert result is stale_data

    def test_refresh_access_token_no_refresh_token(self):
        mgr = UsageManager()
        assert mgr._refresh_access_token("") is None

    def test_refresh_access_token_success(self, monkeypatch):
        mgr = UsageManager()
        mgr._settings_oauth_json = '{"access_token": "old"}'

        # Mock urlopen
        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "access_token": "new_tok",
            "refresh_token": "new_ref",
            "expires_in": 3600,
        }).encode()

        monkeypatch.setattr("claude_monitor.usage.urlopen", lambda req, **kw: mock_resp)
        monkeypatch.setattr("claude_monitor.usage._read_keychain", lambda: None)

        called = []
        mgr.set_on_token_refreshed(lambda t, r, e: called.append((t, r, e)))

        result = mgr._refresh_access_token("old_ref")
        assert result is not None
        assert result[0] == "new_tok"
        assert result[1] == "new_ref"
        assert len(called) == 1
        # settings_oauth_json should be updated
        assert "new_tok" in mgr._settings_oauth_json

    def test_refresh_access_token_updates_keychain(self, monkeypatch):
        mgr = UsageManager()

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "access_token": "new_tok",
            "expires_in": 3600,
        }).encode()

        monkeypatch.setattr("claude_monitor.usage.urlopen", lambda req, **kw: mock_resp)

        keychain_data = {"claudeAiOauth": {"accessToken": "old", "refreshToken": "old_ref"}}
        monkeypatch.setattr("claude_monitor.usage._read_keychain", lambda: keychain_data)
        written = []
        monkeypatch.setattr("claude_monitor.usage._write_keychain", lambda d: written.append(d) or True)

        result = mgr._refresh_access_token("old_ref")
        assert result is not None
        assert len(written) == 1
        assert written[0]["claudeAiOauth"]["accessToken"] == "new_tok"

    def test_refresh_access_token_missing_access_token(self, monkeypatch):
        mgr = UsageManager()

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({}).encode()

        monkeypatch.setattr("claude_monitor.usage.urlopen", lambda req, **kw: mock_resp)
        assert mgr._refresh_access_token("ref") is None

    def test_refresh_access_token_network_error(self, monkeypatch):
        mgr = UsageManager()
        from urllib.error import URLError
        monkeypatch.setattr("claude_monitor.usage.urlopen", lambda req, **kw: (_ for _ in ()).throw(URLError("fail")))
        assert mgr._refresh_access_token("ref") is None

    def test_refresh_callback_error_swallowed(self, monkeypatch):
        mgr = UsageManager()
        mgr.set_on_token_refreshed(lambda t, r, e: 1/0)

        mock_resp = MagicMock()
        mock_resp.__enter__ = MagicMock(return_value=mock_resp)
        mock_resp.__exit__ = MagicMock(return_value=False)
        mock_resp.status = 200
        mock_resp.read.return_value = json.dumps({
            "access_token": "tok", "expires_in": 3600,
        }).encode()

        monkeypatch.setattr("claude_monitor.usage.urlopen", lambda req, **kw: mock_resp)
        monkeypatch.setattr("claude_monitor.usage._read_keychain", lambda: None)

        # Should not raise despite callback error
        result = mgr._refresh_access_token("ref")
        assert result is not None

    def test_fetch_disk_cache_expired_used_as_fallback(self, tmp_path, monkeypatch):
        """Expired disk cache is loaded as fallback if API fails."""
        cache_file = str(tmp_path / "usage-cache.json")
        monkeypatch.setattr("claude_monitor.usage.USAGE_CACHE_FILE", cache_file)
        old_time = time.time() - USAGE_MAX_AGE - 100  # expired
        with open(cache_file, "w") as f:
            json.dump({
                "fetched_at": old_time,
                "five_hour": {"utilization": 35.0},
                "seven_day": {"utilization": 15.0},
            }, f)

        mgr = UsageManager()
        monkeypatch.setattr(mgr, "get_token", lambda: None)

        result = mgr.fetch()
        # Should return the expired disk data as fallback
        assert result is not None
        assert result.five_hour.utilization == 35.0
