"""API usage for claude-monitor.

Fetches usage data from the Anthropic API using OAuth credentials
from the macOS Keychain, with caching for both token and usage data.
"""

import json
import logging
import os
import re
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.error import URLError

from claude_monitor import RATE_LIMITS_CACHE_FILE, credentials, fmt_duration

log = logging.getLogger(__name__)

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_EXPIRY_BUFFER = 60  # refresh when within 60s of expiry
USAGE_MAX_AGE = 300  # 5 minutes
USAGE_CACHE_FILE = "/tmp/claude-auto-accept/usage-cache.json"


def _mask_token(t: str) -> str:
    """Mask a token for safe logging: show first 8 + last 4 chars."""
    if not t:
        return "(empty)"
    if len(t) <= 12:
        return "***"
    return f"{t[:8]}***{t[-4:]}"


@dataclass
class WindowUsage:
    utilization: float  # 0-100
    resets_at: datetime | None


@dataclass
class UsageData:
    five_hour: WindowUsage
    seven_day: WindowUsage
    # Credits used on accounts that have moved off the windowed rate-limit model
    # (Anthropic's /api/oauth/usage now returns five_hour: null / seven_day: null
    # for these accounts and reports usage via extra_usage.used_credits instead).
    credits_used: float | None = None


def _parse_window(w: dict | None) -> WindowUsage:
    if not isinstance(w, dict):
        return WindowUsage(utilization=0, resets_at=None)
    # API uses "utilization"; statusline cache uses "used_percentage" (CC 2.1.80+)
    util = w.get("utilization") if w.get("utilization") is not None else w.get("used_percentage", 0)
    if isinstance(util, str):
        util = float(util)
    elif util is None:
        util = 0
    resets_at = None
    raw_reset = w.get("resets_at")
    if raw_reset:
        try:
            resets_at = datetime.fromisoformat(raw_reset.replace("Z", "+00:00"))
        except (ValueError, TypeError):
            pass
    return WindowUsage(utilization=util, resets_at=resets_at)


def _parse_oauth_json(raw: str) -> tuple[str, str, float] | None:
    """Parse OAuth JSON: {"access_token": "...", "refresh_token": "...", "expires_at": ...}.

    access_token is required; refresh_token and expires_at are optional.
    Returns (access_token, refresh_token, expires_at_epoch) or None.
    """
    try:
        data = json.loads(raw)
        token = data.get("access_token")
        if not token:
            return None
        refresh = data.get("refresh_token", "")
        expires_at = data.get("expires_at")
        if expires_at is not None:
            expires_at = float(expires_at)
        else:
            expires_at = time.time() + 3600
        return token, refresh, expires_at
    except (json.JSONDecodeError, TypeError, ValueError):
        return None


def _extract_oauth_from_code_env() -> tuple[str, str, float] | None:
    """Extract OAuth tokens from CLAUDE_CODE_OAUTH_TOKEN env var (JSON or plain token)."""
    raw = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    if not raw:
        return None
    return _parse_oauth_json(raw)


def _extract_oauth_from_dot_env() -> tuple[str, str, float] | None:
    """Extract OAuth tokens from ~/.env or project .env file (CLAUDE_CODE_OAUTH_TOKEN=...)."""
    candidates = [
        os.path.expanduser("~/.env"),
        os.path.join(os.getcwd(), ".env"),
    ]
    for path in candidates:
        try:
            with open(path) as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("#") or "=" not in line:
                        continue
                    key, _, value = line.partition("=")
                    if key.strip() == "CLAUDE_CODE_OAUTH_TOKEN":
                        value = value.strip().strip('"').strip("'")
                        if value:
                            return _parse_oauth_json(value)
        except OSError:
            continue
    return None


def _extract_oauth_from_env() -> tuple[str, str, float] | None:
    """Extract OAuth tokens from CLAUDE_OAUTH_TOKEN env var (JSON)."""
    raw = os.environ.get("CLAUDE_OAUTH_TOKEN", "")
    if not raw:
        return None
    return _parse_oauth_json(raw)


def _read_keychain() -> dict | None:
    """Read the full credentials JSON from macOS Keychain."""
    try:
        return credentials.read_json()
    except (
        credentials.CredentialsError,
        OSError,
        subprocess.SubprocessError,
        json.JSONDecodeError,
        ValueError,
    ) as e:
        log.debug(f"Keychain read failed: {e}")
        return None


def _write_keychain(data: dict) -> bool:
    """Write credentials JSON back to macOS Keychain, preserving all fields."""
    try:
        credentials.write(json.dumps(data))
        return True
    except (credentials.CredentialsError, OSError, subprocess.SubprocessError) as e:
        log.debug(f"Keychain write failed: {e}")
        return False


def _extract_oauth_tokens() -> tuple[str, str, float] | None:
    """Extract OAuth tokens from macOS Keychain.

    Returns (access_token, refresh_token, expires_at_epoch) or None.
    """
    data = _read_keychain()
    if not data:
        return None
    tokens = credentials.tokens_from_data(data)
    if tokens is None:
        log.debug("No OAuth token found in credentials")
    return tokens


def _load_disk_cache() -> dict:
    """Load usage cache from disk. Returns empty dict if missing/corrupt."""
    try:
        with open(USAGE_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _load_rate_limits_cache() -> dict:
    """Load rate-limits cache written by the statusline handler.

    Returns empty dict if missing/corrupt.
    """
    try:
        with open(RATE_LIMITS_CACHE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def _save_disk_cache(data: UsageData, fetched_at: float) -> None:
    """Persist usage cache to disk."""
    try:
        payload = {
            "fetched_at": fetched_at,
            "five_hour": {
                "utilization": data.five_hour.utilization,
                "resets_at": data.five_hour.resets_at.isoformat()
                if data.five_hour.resets_at
                else None,
            },
            "seven_day": {
                "utilization": data.seven_day.utilization,
                "resets_at": data.seven_day.resets_at.isoformat()
                if data.seven_day.resets_at
                else None,
            },
            "credits_used": data.credits_used,
        }
        with open(USAGE_CACHE_FILE, "w") as f:
            json.dump(payload, f)
    except OSError as e:
        log.debug(f"Failed to save usage cache: {e}")


def _usage_from_disk(entry: dict) -> tuple[UsageData, float] | None:
    """Deserialize UsageData from a disk cache entry."""
    try:
        fetched_at = float(entry["fetched_at"])
        five_hour = _parse_window(entry["five_hour"])
        seven_day = _parse_window(entry["seven_day"])
        credits = entry.get("credits_used")
        if not isinstance(credits, (int, float)):
            credits = None
        return (
            UsageData(five_hour=five_hour, seven_day=seven_day, credits_used=credits),
            fetched_at,
        )
    except (KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Pure formatting helpers (no state)
# ---------------------------------------------------------------------------

_THEME = {
    "running": {
        "fill": "#50c878",  # bright emerald bar fill
        "empty": "#284130",  # dark green-gray empty
        "pct": "#c8f0d5",  # light mint percentage text
    },
    "paused": {
        "fill": "#dc7832",  # rust-orange bar fill
        "empty": "#482d1e",  # dark brown empty
        "pct": "#ebc8af",  # warm cream percentage text
    },
}


def _bar(pct: float, width: int, mode: str = "running") -> str:
    """Render a progress bar using background colors and fractional blocks.

    Full cells use fill-colored background with spaces. The fractional
    boundary cell uses fg=fill on bg=empty so the partial block blends.
    Empty cells use empty-colored background with spaces.
    """
    BLOCKS = " ▏▎▍▌▋▊▉█"  # index 0=empty, 8=full
    t = _THEME.get(mode, _THEME["running"])
    fill_color = t["fill"]
    empty_bg = t["empty"]

    fill_eighths = pct / 100 * width * 8
    fill_eighths = max(0, min(width * 8, fill_eighths))

    full_chars = int(fill_eighths // 8)
    remainder = int(fill_eighths % 8)

    parts = []
    if full_chars > 0:
        parts.append(f"[on {fill_color}]{' ' * full_chars}[/]")
    if remainder > 0 and full_chars < width:
        parts.append(f"[{fill_color} on {empty_bg}]{BLOCKS[remainder]}[/]")
        empty = width - full_chars - 1
    else:
        empty = width - full_chars
    if empty > 0:
        parts.append(f"[on {empty_bg}]{' ' * empty}[/]")

    return "".join(parts)


def _format_countdown(resets_at: datetime | None) -> str:
    if not resets_at:
        return ""
    now = datetime.now(timezone.utc)
    total_secs = int((resets_at - now).total_seconds())
    if total_secs <= 0:
        return "now"
    return fmt_duration(total_secs, compact=True)


def _format_local_time(resets_at: datetime | None) -> str:
    if not resets_at:
        return ""
    local = resets_at.astimezone()
    now = datetime.now().astimezone()
    hour = local.hour % 12 or 12
    ampm = "AM" if local.hour < 12 else "PM"
    time_str = f"{hour}:{local.minute:02d}{ampm}"
    if local.date() == now.date():
        return time_str
    return f"{local.strftime('%a')} {time_str}"


def _strip_markup(s: str) -> str:
    """Strip Rich markup tags to get plain text length."""
    return re.sub(r"\[/?[^\]]*\]", "", s)


def _quota(
    w: WindowUsage, label: str, bar_width: int | None, reset: str, mode: str = "running"
) -> str:
    """Format a single quota window. Returns empty string for no-data windows."""
    # Credits-plan accounts have null windows that _parse_window normalizes to
    # utilization=0/resets_at=None. Render nothing in that case so the segment
    # gets dropped by the caller's filter(None, ...).
    if w.utilization == 0 and w.resets_at is None:
        return ""
    pct_color = _THEME.get(mode, _THEME["running"])["pct"]
    s = f"[bold]{label}[/] [{pct_color}]{w.utilization:.0f}%[/]"
    if bar_width:
        s += f" {_bar(w.utilization, bar_width, mode)}"
    if reset:
        s += f" [dim]{reset}[/]"
    return s


def _credits_segment(credits: float | None, mode: str = "running") -> str:
    """Format the credits-used segment. Returns empty string when None."""
    if credits is None:
        return ""
    pct_color = _THEME.get(mode, _THEME["running"])["pct"]
    return f"[bold]credits[/] [{pct_color}]{credits:,.0f}[/]"


def format_usage_inline(data: UsageData, max_width: int = 999, mode: str = "running") -> str:
    """Format usage data for the status bar, adapting to available width.

    Args:
        mode: "running" (auto/emerald) or "paused" (manual/rust) for theming.
    """
    SEP = " [dim]│[/] "
    h5 = data.five_hour
    d7 = data.seven_day
    credits_seg = _credits_segment(data.credits_used, mode)

    h5_countdown = _format_countdown(h5.resets_at)
    h5_local = _format_local_time(h5.resets_at)
    d7_full = _format_local_time(d7.resets_at)

    h5_full_reset = (
        f"{h5_countdown} ({h5_local})" if h5_countdown and h5_local else h5_countdown or h5_local
    )

    # Each tier appends the credits segment if present. filter(None, ...) drops
    # empty quota segments (e.g. credits-plan accounts with null windows).
    tiers = [
        lambda: SEP.join(
            filter(
                None,
                [
                    _quota(h5, "5h", 12, h5_full_reset, mode),
                    _quota(d7, "7d", 12, d7_full, mode),
                    credits_seg,
                ],
            )
        ),
        lambda: SEP.join(
            filter(
                None,
                [
                    _quota(h5, "5h", 12, h5_local, mode),
                    _quota(d7, "7d", 12, d7_full, mode),
                    credits_seg,
                ],
            )
        ),
        lambda: SEP.join(
            filter(
                None,
                [
                    _quota(h5, "5h", 12, h5_local, mode),
                    _quota(d7, "7d", 12, "", mode),
                    credits_seg,
                ],
            )
        ),
        lambda: SEP.join(
            filter(
                None,
                [
                    _quota(h5, "5h", 8, h5_local, mode),
                    _quota(d7, "7d", None, "", mode),
                    credits_seg,
                ],
            )
        ),
        lambda: SEP.join(
            filter(
                None,
                [
                    _quota(h5, "5h", None, h5_local, mode),
                    _quota(d7, "7d", None, "", mode),
                    credits_seg,
                ],
            )
        ),
        lambda: SEP.join(filter(None, [_quota(h5, "5h", 8, h5_local, mode), credits_seg])),
        lambda: SEP.join(filter(None, [_quota(h5, "5h", None, "", mode), credits_seg])),
        lambda: credits_seg,  # fallback when nothing else fits but credits is set
    ]

    PILL_PAD = 2  # 1 space each side

    for tier in tiers:
        result = tier()
        if len(_strip_markup(result)) + PILL_PAD <= max_width:
            return f" {result} "

    return f" {tiers[-1]()} "


# ---------------------------------------------------------------------------
# UsageManager — encapsulates all mutable state
# ---------------------------------------------------------------------------


@dataclass
class UsageManager:
    """Manages OAuth token fetching and API usage caching.

    All mutable state is instance-local: token cache, usage cache,
    settings OAuth JSON, and the token-refreshed callback.
    """

    _token_cache: dict = field(default_factory=dict)
    # {"token": str, "refresh_token": str, "expires_at": float}

    _usage_cache: dict = field(default_factory=dict)
    # {"data": UsageData, "fetched_at": float}

    _settings_oauth_json: str = ""
    _on_token_refreshed: Callable[[str, str, float], None] | None = None

    # ------------------------------------------------------------------
    # Configuration helpers (called by TUI to inject settings)
    # ------------------------------------------------------------------

    def set_oauth_json(self, oauth_json: str) -> None:
        """Set the OAuth JSON string from settings. Invalidates token cache on change."""
        if oauth_json != self._settings_oauth_json:
            self._settings_oauth_json = oauth_json
            self._token_cache = {}

    def set_on_token_refreshed(self, callback: Callable[[str, str, float], None] | None) -> None:
        """Register a callback invoked when the OAuth token is refreshed."""
        self._on_token_refreshed = callback

    # ------------------------------------------------------------------
    # Token resolution
    # ------------------------------------------------------------------

    def _refresh_access_token(self, refresh_token: str) -> tuple[str, str, float] | None:
        """Use refresh token to get a new access token.

        Returns (new_access_token, new_refresh_token, new_expires_at) or None.
        """
        if not refresh_token:
            log.debug("No refresh token available")
            return None

        try:
            log.debug(
                "Token refresh request: POST %s refresh_token=%s",
                credentials.TOKEN_URL,
                _mask_token(refresh_token),
            )

            refreshed = credentials.refresh_tokens(refresh_token)
            if not refreshed:
                log.debug("Token refresh response missing access_token")
                return None

            new_access, new_refresh, expires_in = refreshed
            new_expires_at = time.time() + expires_in

            log.debug(
                "Token refresh response: access_token=%s refresh_token=%s expires_in=%s",
                _mask_token(new_access),
                _mask_token(new_refresh),
                expires_in,
            )

            # Update keychain with new tokens
            keychain_data = _read_keychain()
            if keychain_data:
                oauth = keychain_data.get("claudeAiOauth", {})
                oauth["accessToken"] = new_access
                oauth["refreshToken"] = new_refresh
                oauth["expiresAt"] = int(new_expires_at * 1000)
                keychain_data["claudeAiOauth"] = oauth
                _write_keychain(keychain_data)

            log.debug("OAuth token refreshed successfully")

            # Update _settings_oauth_json so subsequent token lookups use refreshed values
            if self._settings_oauth_json:
                self._settings_oauth_json = json.dumps(
                    {
                        "access_token": new_access,
                        "refresh_token": new_refresh,
                        "expires_at": new_expires_at,
                    }
                )

            if self._on_token_refreshed:
                try:
                    self._on_token_refreshed(new_access, new_refresh, new_expires_at)
                except (
                    Exception
                ) as e:  # Callback is user-supplied; catch all to avoid breaking token refresh
                    log.debug(f"Token refresh callback failed: {e}")

            return new_access, new_refresh, new_expires_at
        except (
            URLError,
            OSError,
            subprocess.SubprocessError,
            json.JSONDecodeError,
            KeyError,
            ValueError,
        ) as e:
            log.debug(f"Token refresh failed: {e}")
            return None

    def get_token(self) -> str | None:
        """Get OAuth token, refreshing if expired or near expiry.

        Resolution order:
          1) Settings JSON
          2) CLAUDE_CODE_OAUTH_TOKEN env var
          3) ~/.env or project .env file (CLAUDE_CODE_OAUTH_TOKEN)
          4) CLAUDE_OAUTH_TOKEN env var
          5) macOS Keychain (macOS only)
        """
        now = time.time()

        # Check if cached token is still valid (with buffer)
        if self._token_cache and now < self._token_cache.get("expires_at", 0) - TOKEN_EXPIRY_BUFFER:
            return self._token_cache["token"]

        # Try to refresh if we have a refresh token
        cached_refresh = self._token_cache.get("refresh_token")
        if self._token_cache and cached_refresh:
            result = self._refresh_access_token(cached_refresh)
            if result:
                token, new_refresh, expires_at = result
                self._token_cache = {
                    "token": token,
                    "refresh_token": new_refresh,
                    "expires_at": expires_at,
                }
                return token

        # Try each source in resolution order
        result = None
        if self._settings_oauth_json:
            result = _parse_oauth_json(self._settings_oauth_json)
        if not result:
            result = _extract_oauth_from_code_env()
        if not result:
            result = _extract_oauth_from_dot_env()
        if not result:
            result = _extract_oauth_from_env()
        if not result and sys.platform == "darwin":
            result = _extract_oauth_tokens()
        if not result:
            return None

        token, refresh_token, expires_at = result
        self._token_cache = {
            "token": token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
        }

        # If the token is already expired/near-expiry, try refreshing
        if now >= expires_at - TOKEN_EXPIRY_BUFFER and refresh_token:
            refreshed = self._refresh_access_token(refresh_token)
            if refreshed:
                token, new_refresh, expires_at = refreshed
                self._token_cache = {
                    "token": token,
                    "refresh_token": new_refresh,
                    "expires_at": expires_at,
                }
                return token

        return token

    # ------------------------------------------------------------------
    # Usage fetching
    # ------------------------------------------------------------------

    def invalidate_cache(self) -> None:
        """Clear in-memory and disk usage cache so the next fetch hits the API."""
        self._usage_cache = {}
        try:
            os.remove(USAGE_CACHE_FILE)
        except FileNotFoundError:
            pass

    def fetch(self) -> UsageData | None:
        """Fetch usage data from the Anthropic API.

        Returns cached data if less than 5 minutes old (memory or disk).
        """
        now = time.time()
        if self._usage_cache and (now - self._usage_cache.get("fetched_at", 0)) < USAGE_MAX_AGE:
            return self._usage_cache.get("data")

        # Check rate-limits cache (pushed by statusline handler — more current than API)
        if not self._usage_cache:
            rl = _load_rate_limits_cache()
            if rl:
                result = _usage_from_disk(rl)
                if result:
                    data, fetched_at = result
                    if (now - fetched_at) < USAGE_MAX_AGE:
                        self._usage_cache = {"data": data, "fetched_at": fetched_at}
                        return data

        # Check disk cache before hitting the API
        if not self._usage_cache:
            disk = _load_disk_cache()
            if disk:
                result = _usage_from_disk(disk)
                if result:
                    data, fetched_at = result
                    if (now - fetched_at) < USAGE_MAX_AGE:
                        self._usage_cache = {"data": data, "fetched_at": fetched_at}
                        return data
                    # Disk cache expired but use it as fallback if API fails
                    self._usage_cache = {"data": data, "fetched_at": 0}

        token = self.get_token()
        if not token:
            return self._usage_cache.get("data")

        try:
            result = subprocess.run(
                [
                    "curl",
                    "-s",
                    "-4",
                    "--max-time",
                    "15",
                    USAGE_API_URL,
                    "-H",
                    f"Authorization: Bearer {token}",
                    "-H",
                    "anthropic-beta: oauth-2025-04-20",
                    "-H",
                    "User-Agent: claude-code/statusline",
                ],
                capture_output=True,
                timeout=20,
            )
            if result.returncode != 0 or not result.stdout:
                raise OSError(f"curl failed: {result.returncode}")
            data = json.loads(result.stdout)

            # Reject only explicit error responses. The API now returns
            # five_hour: null / seven_day: null for credits-plan accounts;
            # _parse_window handles None and we surface extra_usage.used_credits.
            if "error" in data:
                raise OSError(
                    f"API returned error: {data.get('error', {}).get('message', 'unknown')}"
                )

            extra = data.get("extra_usage") or {}
            credits_used = extra.get("used_credits")
            usage = UsageData(
                five_hour=_parse_window(data.get("five_hour")),
                seven_day=_parse_window(data.get("seven_day")),
                credits_used=credits_used if isinstance(credits_used, (int, float)) else None,
            )
            self._usage_cache = {"data": usage, "fetched_at": now}
            _save_disk_cache(usage, now)
            return usage
        except (
            URLError,
            json.JSONDecodeError,
            OSError,
            subprocess.SubprocessError,
            AttributeError,
            TypeError,
            KeyError,
        ) as e:
            log.debug(f"Usage API fetch failed: {e}")
            # Back off for USAGE_MAX_AGE so we don't hammer the API on repeated failures
            existing_data = self._usage_cache.get("data")
            self._usage_cache = {"data": existing_data, "fetched_at": now}
            # Also update disk cache fetched_at so restarts don't immediately retry
            try:
                disk = _load_disk_cache()
                if disk:
                    disk["fetched_at"] = now
                    with open(USAGE_CACHE_FILE, "w") as f:
                        json.dump(disk, f)
            except OSError:
                pass
            return existing_data


# ---------------------------------------------------------------------------
# Module-level singleton + compatibility shims
# ---------------------------------------------------------------------------
# tui.py and tui_simple.py import these names directly. They delegate to a
# module-level UsageManager instance so callers don't need to change.

_manager = UsageManager()


def set_oauth_json(oauth_json: str) -> None:
    """Set the OAuth JSON string from settings. Called by the TUI."""
    _manager.set_oauth_json(oauth_json)


def set_on_token_refreshed(callback: Callable[[str, str, float], None] | None) -> None:
    """Register a callback for when the OAuth token is refreshed."""
    _manager.set_on_token_refreshed(callback)


def fetch_usage() -> UsageData | None:
    """Fetch usage data (delegates to module-level UsageManager singleton)."""
    return _manager.fetch()


def invalidate_usage_cache() -> None:
    """Clear in-memory and disk usage cache (delegates to module-level UsageManager singleton)."""
    _manager.invalidate_cache()
