"""API usage for claude-monitor.

Fetches usage data from the Anthropic API using OAuth credentials
from the macOS Keychain, with caching for both token and usage data.
"""

import json
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from urllib.error import URLError
from urllib.request import Request, urlopen

from claude_monitor import fmt_duration

log = logging.getLogger(__name__)

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
KEYCHAIN_SERVICE = "Claude Code-credentials"
USAGE_MAX_AGE = 300  # 5 minutes
USAGE_CACHE_FILE = "/tmp/claude-auto-accept/usage-cache.json"

# In-memory caches
_token_cache: dict = {}  # {"token": str, "expires_at": float}
_usage_cache: dict = {}  # {"data": UsageData, "fetched_at": float}


@dataclass
class WindowUsage:
    utilization: float  # 0-100
    resets_at: datetime | None


@dataclass
class UsageData:
    five_hour: WindowUsage
    seven_day: WindowUsage


def _parse_window(w: dict) -> WindowUsage:
    util = w.get("utilization", 0)
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


def _extract_oauth_token() -> tuple[str, float] | None:
    """Extract OAuth token from macOS Keychain.

    Returns (token, expires_at_epoch) or None if unavailable.
    Parses the JSON keychain data to extract fields.
    """
    try:
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "Claude Code-credentials", "-w"],
            capture_output=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout:
            log.debug("No credentials found in Keychain")
            return None

        raw = result.stdout.decode("utf-8", errors="replace").strip()
        data = json.loads(raw)
        oauth = data.get("claudeAiOauth", {})
        token = oauth.get("accessToken")
        if not token:
            log.debug("No OAuth token found in credentials")
            return None

        expires_at = oauth.get("expiresAt")
        if expires_at:
            expires_at = expires_at / 1000
        else:
            expires_at = time.time() + 3600

        return token, expires_at
    except Exception as e:
        log.debug(f"OAuth token extraction failed: {e}")
        return None


def _get_token() -> str | None:
    """Get OAuth token, using cache if still valid."""
    global _token_cache
    if _token_cache and time.time() < _token_cache.get("expires_at", 0):
        return _token_cache["token"]

    result = _extract_oauth_token()
    if not result:
        return None

    token, expires_at = result
    _token_cache = {"token": token, "expires_at": expires_at}
    return token


def _load_disk_cache() -> dict:
    """Load usage cache from disk. Returns empty dict if missing/corrupt."""
    try:
        with open(USAGE_CACHE_FILE) as f:
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
                "resets_at": data.five_hour.resets_at.isoformat() if data.five_hour.resets_at else None,
            },
            "seven_day": {
                "utilization": data.seven_day.utilization,
                "resets_at": data.seven_day.resets_at.isoformat() if data.seven_day.resets_at else None,
            },
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
        return UsageData(five_hour=five_hour, seven_day=seven_day), fetched_at
    except (KeyError, TypeError, ValueError):
        return None


def invalidate_usage_cache() -> None:
    """Clear in-memory and disk usage cache so the next fetch hits the API."""
    global _usage_cache
    _usage_cache = {}
    try:
        os.remove(USAGE_CACHE_FILE)
    except FileNotFoundError:
        pass


def fetch_usage() -> UsageData | None:
    """Fetch usage data from the Anthropic API.

    Returns cached data if less than 5 minutes old (memory or disk).
    """
    global _usage_cache
    now = time.time()
    if _usage_cache and (now - _usage_cache.get("fetched_at", 0)) < USAGE_MAX_AGE:
        return _usage_cache.get("data")

    # Check disk cache before hitting the API
    if not _usage_cache:
        disk = _load_disk_cache()
        if disk:
            result = _usage_from_disk(disk)
            if result:
                data, fetched_at = result
                if (now - fetched_at) < USAGE_MAX_AGE:
                    _usage_cache = {"data": data, "fetched_at": fetched_at}
                    return data
                # Disk cache expired but use it as fallback if API fails
                _usage_cache = {"data": data, "fetched_at": 0}

    token = _get_token()
    if not token:
        return _usage_cache.get("data")

    try:
        req = Request(USAGE_API_URL)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("anthropic-beta", "oauth-2025-04-20")
        req.add_header("User-Agent", "claude-code/statusline")

        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        usage = UsageData(
            five_hour=_parse_window(data.get("five_hour", {})),
            seven_day=_parse_window(data.get("seven_day", {})),
        )
        _usage_cache = {"data": usage, "fetched_at": now}
        _save_disk_cache(usage, now)
        return usage
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.debug(f"Usage API fetch failed: {e}")
        # Back off for USAGE_MAX_AGE so we don't hammer the API on repeated failures
        existing_data = _usage_cache.get("data")
        _usage_cache = {"data": existing_data, "fetched_at": now}
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


_THEME = {
    "running": {
        "fill": "#50c878",    # bright emerald bar fill
        "empty": "#284130",   # dark green-gray empty
        "pct": "#c8f0d5",     # light mint percentage text
    },
    "paused": {
        "fill": "#dc7832",    # rust-orange bar fill
        "empty": "#482d1e",   # dark brown empty
        "pct": "#ebc8af",     # warm cream percentage text
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


def _quota(w: WindowUsage, label: str, bar_width: int | None, reset: str, mode: str = "running") -> str:
    """Format a single quota window."""
    pct_color = _THEME.get(mode, _THEME["running"])["pct"]
    s = f"[bold]{label}[/] [{pct_color}]{w.utilization:.0f}%[/]"
    if bar_width:
        s += f" {_bar(w.utilization, bar_width, mode)}"
    if reset:
        s += f" [dim]{reset}[/]"
    return s


def format_usage_inline(data: UsageData, max_width: int = 999, mode: str = "running") -> str:
    """Format usage data for the status bar, adapting to available width.

    Args:
        mode: "running" (auto/emerald) or "paused" (manual/rust) for theming.
    """
    SEP = " [dim]│[/] "
    h5 = data.five_hour
    d7 = data.seven_day

    h5_countdown = _format_countdown(h5.resets_at)
    h5_local = _format_local_time(h5.resets_at)
    d7_full = _format_local_time(d7.resets_at)

    h5_full_reset = f"{h5_countdown} ({h5_local})" if h5_countdown and h5_local else h5_countdown or h5_local

    tiers = [
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 12, h5_full_reset, mode),
            _quota(d7, "7d", 12, d7_full, mode),
        ])),
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 12, h5_local, mode),
            _quota(d7, "7d", 12, d7_full, mode),
        ])),
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 12, h5_local, mode),
            _quota(d7, "7d", 12, "", mode),
        ])),
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 8, h5_local, mode),
            _quota(d7, "7d", None, "", mode),
        ])),
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", None, h5_local, mode),
            _quota(d7, "7d", None, "", mode),
        ])),
        lambda: _quota(h5, "5h", 8, h5_local, mode),
        lambda: _quota(h5, "5h", None, "", mode),
    ]

    PILL_PAD = 2  # 1 space each side

    for tier in tiers:
        result = tier()
        if len(_strip_markup(result)) + PILL_PAD <= max_width:
            return f" {result} "

    return f" {tiers[-1]()} "
