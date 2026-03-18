"""Helper functions for HTTP API endpoints.

The unified HTTP+WebSocket server (web.py) imports these response generators.
AppStateProtocol defines the interface that TUI applications must implement.
"""

import logging
import subprocess
from typing import Protocol

from claude_monitor import __version__

log = logging.getLogger(__name__)


class AppStateProtocol(Protocol):
    """Interface the HTTP handler expects from the TUI app.

    Implementations (AutoAcceptTUI, SimpleTUI) must provide these two methods.
    Both are called via ``call_from_thread()`` so they run on the Textual event
    loop, not the HTTP-server thread.
    """

    def get_state_snapshot(self) -> dict[str, object]:
        """Return a JSON-serialisable snapshot of current TUI state."""
        ...

    def export_screenshot(self) -> str:
        """Export the current screen as an SVG string."""
        ...


# Textual exports SVG with "Fira Code" but it may not be installed.
# Detect the best available monospace font for PNG rendering.
_PNG_FONT = None


def _detect_monospace_font():
    """Find the best installed monospace font for SVG→PNG rendering."""
    global _PNG_FONT
    if _PNG_FONT is not None:
        return _PNG_FONT
    # Preference order: Fira Code (Textual default), JetBrains Mono, Menlo, Courier New
    preferred = ["Fira Code", "JetBrainsMono Nerd Font Mono", "JetBrains Mono", "Menlo", "Courier New"]
    try:
        result = subprocess.run(["fc-list", ":", "family"], capture_output=True, text=True, timeout=5)
        installed = set(f.strip() for f in result.stdout.split("\n") if f.strip())
        for font in preferred:
            if any(font in f for f in installed):
                _PNG_FONT = font
                log.debug(f"PNG font: {_PNG_FONT}")
                return _PNG_FONT
    except (FileNotFoundError, subprocess.TimeoutExpired, OSError):
        pass
    _PNG_FONT = "monospace"
    return _PNG_FONT


def generate_health_response(start_time, now) -> dict:
    """Return a JSON-serialisable health response dict."""
    uptime = int(now - start_time) if start_time is not None else 0
    return {
        "status": "ok",
        "version": __version__,
        "uptime": uptime,
    }


def generate_screenshot_svg(app) -> str:
    """Export the current screen as an SVG string using the app's export_screenshot."""
    return app.call_from_thread(app.export_screenshot)


def generate_screenshot_png(svg_text: str) -> bytes:
    """Convert an SVG string to optimised PNG bytes (256-color quantised)."""
    import cairosvg
    from PIL import Image
    from io import BytesIO

    font = _detect_monospace_font()
    if font != "Fira Code":
        svg_text = svg_text.replace("Fira Code", font)
    raw_png = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"))
    img = Image.open(BytesIO(raw_png))
    quantized = img.quantize(colors=256, method=2, dither=0).convert("RGB")
    buf = BytesIO()
    quantized.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def app_state_snapshot(app, start_time, now) -> dict:
    """Return a JSON-serialisable snapshot of TUI state with uptime injected."""
    uptime = int(now - start_time) if start_time is not None else 0
    snapshot = app.call_from_thread(app.get_state_snapshot)
    snapshot["uptime"] = uptime
    return snapshot
