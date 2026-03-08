"""HTTP API server for claude-monitor.

Runs on localhost:17233 in a background thread. Provides endpoints for
external tools (e.g. Telegram bot) to query TUI state and screenshots.
"""

import json
import logging
import os
import subprocess
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

from claude_monitor import __version__, API_PORT, API_PORT_FILE

log = logging.getLogger(__name__)

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
    except Exception:
        pass
    _PNG_FONT = "monospace"
    return _PNG_FONT


class MonitorHTTPHandler(BaseHTTPRequestHandler):
    """HTTP request handler for the monitor API.

    The `app` class attribute is set by `start_api_server()` before
    the server starts accepting requests.
    """

    app = None  # Set by start_api_server()
    _start_time = None  # Set by start_api_server()

    def log_message(self, format, *args):
        log.debug(f"API: {format % args}")

    def _send_json(self, data, status=200):
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_error(self, status, message):
        self._send_json({"error": message}, status)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")

        if path == "/health" or path == "":
            self._handle_health()
        elif path == "/screenshot":
            params = parse_qs(parsed.query)
            fmt = params.get("format", ["png"])[0]
            self._handle_screenshot(fmt)
        elif path == "/text":
            self._handle_text()
        else:
            self._send_error(404, "Not found")

    def _handle_health(self):
        uptime = int(time.time() - self._start_time) if self._start_time else 0
        self._send_json({
            "status": "ok",
            "version": __version__,
            "uptime": uptime,
        })

    def _handle_screenshot(self, fmt):
        if fmt not in ("png", "svg"):
            self._send_error(400, "format must be 'png' or 'svg'")
            return

        if not self.app:
            self._send_error(503, "App not available")
            return

        try:
            svg_text = self.app.call_from_thread(self.app.export_screenshot)
        except Exception as e:
            log.error(f"Screenshot export failed: {e}")
            self._send_error(503, f"Screenshot failed: {e}")
            return

        if fmt == "svg":
            body = svg_text.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            try:
                import cairosvg
                from PIL import Image
                from io import BytesIO

                font = _detect_monospace_font()
                if font != "Fira Code":
                    svg_text = svg_text.replace("Fira Code", font)
                raw_png = cairosvg.svg2png(bytestring=svg_text.encode("utf-8"))
                # Quantize to 256 colors (terminal UIs use few colors) + optimize
                # Convert back to RGB so the PNG is universally readable
                img = Image.open(BytesIO(raw_png))
                quantized = img.quantize(colors=256, method=2, dither=0).convert("RGB")
                buf = BytesIO()
                quantized.save(buf, format="PNG", optimize=True)
                png_bytes = buf.getvalue()
            except Exception as e:
                log.error(f"SVG to PNG conversion failed: {e}")
                self._send_error(503, f"PNG conversion failed: {e}")
                return

            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(png_bytes)))
            self.end_headers()
            self.wfile.write(png_bytes)

    def _handle_text(self):
        if not self.app:
            self._send_error(503, "App not available")
            return

        try:
            uptime = int(time.time() - self._start_time) if self._start_time else 0
            snapshot = self.app.call_from_thread(self.app.get_state_snapshot)
            snapshot["uptime"] = uptime
            self._send_json(snapshot)
        except Exception as e:
            log.error(f"Text endpoint failed: {e}")
            self._send_error(503, f"Failed to collect state: {e}")


def start_api_server(app, port=API_PORT):
    """Create and return an HTTPServer with the app reference stored on the handler."""
    MonitorHTTPHandler.app = app
    MonitorHTTPHandler._start_time = time.time()

    server = HTTPServer(("127.0.0.1", port), MonitorHTTPHandler)
    server.timeout = 1

    os.makedirs(os.path.dirname(API_PORT_FILE), exist_ok=True)
    with open(API_PORT_FILE, "w") as f:
        f.write(str(port))

    log.info(f"API server starting on http://127.0.0.1:{port}")
    return server
