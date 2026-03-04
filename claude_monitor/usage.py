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
    Uses xxd to decode the hex-encoded keychain data, then regex to
    extract fields (the raw data is not valid JSON).
    """
    try:
        result = subprocess.run(
            'security find-generic-password -s "Claude Code-credentials" -w | xxd -r -p',
            shell=True, capture_output=True, timeout=5,
        )
        if result.returncode != 0 or not result.stdout:
            log.debug("No credentials found in Keychain")
            return None

        raw = result.stdout.decode("utf-8", errors="replace")

        token_match = re.search(r'sk-ant-oat[^"]+', raw)
        if not token_match:
            log.debug("No OAuth token found in credentials")
            return None
        token = token_match.group()

        expires_match = re.search(r'"expiresAt"\s*:\s*(\d+)', raw)
        if expires_match:
            expires_at = int(expires_match.group(1)) / 1000
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


def fetch_usage() -> UsageData | None:
    """Fetch usage data from the Anthropic API.

    Returns cached data if less than 5 minutes old.
    """
    global _usage_cache
    now = time.time()
    if _usage_cache and (now - _usage_cache.get("fetched_at", 0)) < USAGE_MAX_AGE:
        return _usage_cache.get("data")

    token = _get_token()
    if not token:
        return _usage_cache.get("data")

    try:
        req = Request(USAGE_API_URL)
        req.add_header("Authorization", f"Bearer {token}")
        req.add_header("anthropic-beta", "oauth-2025-04-20")
        req.add_header("User-Agent", "claude-monitor")

        with urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())

        usage = UsageData(
            five_hour=_parse_window(data.get("five_hour", {})),
            seven_day=_parse_window(data.get("seven_day", {})),
        )
        _usage_cache = {"data": usage, "fetched_at": now}
        return usage
    except (URLError, json.JSONDecodeError, OSError) as e:
        log.debug(f"Usage API fetch failed: {e}")
        return _usage_cache.get("data")


def _color_for_pct(pct: float) -> str:
    if pct < 40:
        return "green"
    if pct < 60:
        return "yellow"
    if pct < 80:
        return "dark_orange"
    return "red"


def _bar(pct: float, width: int) -> str:
    filled = round(pct / 100 * width)
    filled = max(0, min(width, filled))
    empty = width - filled
    color = _color_for_pct(pct)
    return f"[{color}]{'█' * filled}[/][dim]{'░' * empty}[/]"


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


def _quota(w: WindowUsage, label: str, bar_width: int | None, reset: str) -> str:
    """Format a single quota window."""
    color = _color_for_pct(w.utilization)
    s = f"[bold]{label}[/] [{color}]{w.utilization:.0f}%[/]"
    if bar_width:
        s += f" {_bar(w.utilization, bar_width)}"
    if reset:
        s += f" [dim]{reset}[/]"
    return s


def format_usage_inline(data: UsageData, max_width: int = 999) -> str:
    """Format usage data for the status bar, adapting to available width.

    Uses progressive degradation tiers:
    1. Full bars + countdown + local time + cache age
    2. Full bars + countdown + local time
    3. 5h: local time only, 7d: full
    4. Drop 7d reset
    5. Drop 7d bar
    6. Drop 5h bar
    7. Drop 7d entirely
    8. Drop 5h reset
    """
    SEP = " [dim]│[/] "
    h5 = data.five_hour
    d7 = data.seven_day

    h5_countdown = _format_countdown(h5.resets_at)
    h5_local = _format_local_time(h5.resets_at)
    d7_full = _format_local_time(d7.resets_at)

    h5_full_reset = f"{h5_countdown} ({h5_local})" if h5_countdown and h5_local else h5_countdown or h5_local

    tiers = [
        # Tier 1: full bars + full resets + cache age
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 12, h5_full_reset),
            _quota(d7, "7d", 12, d7_full),
        ])),
        # Tier 2: 5h local time only, 7d full
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 12, h5_local),
            _quota(d7, "7d", 12, d7_full),
        ])),
        # Tier 3: drop 7d reset
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 12, h5_local),
            _quota(d7, "7d", 12, ""),
        ])),
        # Tier 4: drop 7d bar
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", 8, h5_local),
            _quota(d7, "7d", None, ""),
        ])),
        # Tier 5: drop 5h bar too
        lambda: SEP.join(filter(None, [
            _quota(h5, "5h", None, h5_local),
            _quota(d7, "7d", None, ""),
        ])),
        # Tier 6: drop 7d entirely
        lambda: _quota(h5, "5h", 8, h5_local),
        # Tier 7: just percentages
        lambda: _quota(h5, "5h", None, ""),
    ]

    for tier in tiers:
        result = tier()
        if len(_strip_markup(result)) <= max_width:
            return result

    return tiers[-1]()
