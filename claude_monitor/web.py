"""Unified WebSocket + HTTP server for claude-monitor.

Replaces api.py's http.server. Serves HTTP endpoints (/health, /text,
/screenshot, /web) and WebSocket connections (/ws) on the same port.
"""

import asyncio
import json
import logging
import os
import time
from pathlib import Path
from typing import Any

import websockets
from websockets.datastructures import Headers
from websockets.http11 import Request, Response

from claude_monitor import __version__, API_PORT, API_PORT_FILE, EVENTS_FILE, STATE_FILE, read_state
from claude_monitor.api import (
    AppStateProtocol,
    generate_health_response,
    generate_screenshot_png,
)
from claude_monitor.settings import load_settings

log = logging.getLogger(__name__)

MAX_WS_CONNECTIONS = 25
_STATIC_DIR = Path(__file__).parent / "static"
_start_time: float = 0.0
_app: AppStateProtocol | None = None
_clients: set = set()

# Minimal inline HTML served when static/index.html doesn't exist yet
_FALLBACK_HTML = b"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Claude Monitor</title></head>
<body><h1>Claude Monitor</h1><p>Web UI loading...</p></body></html>"""


def _make_response(status: int, reason: str, content_type: str, body: bytes) -> Response:
    """Construct a websockets Response with the given fields."""
    return Response(status, reason, Headers({"Content-Type": content_type}), body)


def _error_response(status: int, message: str) -> Response:
    """Build a JSON error Response."""
    body = json.dumps({"error": message}).encode()
    reasons = {400: "Bad Request", 404: "Not Found", 503: "Service Unavailable"}
    reason = reasons.get(status, "Error")
    return _make_response(status, reason, "application/json", body)


async def _handle_http(connection: Any, request: Request) -> Response | None:
    """Route HTTP requests. Return Response to short-circuit; return None for /ws upgrade."""
    # Strip query string for routing
    raw_path = request.path
    path = raw_path.split("?", 1)[0].rstrip("/") or "/"

    # Let WebSocket upgrade proceed
    if path == "/ws":
        return None

    if path == "/health":
        body = json.dumps(generate_health_response(_start_time, time.time())).encode()
        return _make_response(200, "OK", "application/json", body)

    if path == "/text":
        if not _app:
            return _error_response(503, "App not available")
        try:
            snapshot = _app.call_from_thread(_app.get_state_snapshot)
            snapshot["uptime"] = int(time.time() - _start_time)
            body = json.dumps(snapshot).encode()
            return _make_response(200, "OK", "application/json", body)
        except Exception as e:
            log.error(f"text endpoint failed: {e}")
            return _error_response(503, str(e))

    if path == "/screenshot":
        if not _app:
            return _error_response(503, "App not available")
        try:
            svg = _app.call_from_thread(_app.export_screenshot)
            # Parse format query param
            fmt = "png"
            if "?" in raw_path:
                qs = raw_path.split("?", 1)[1]
                for part in qs.split("&"):
                    if part.startswith("format="):
                        fmt = part.split("=", 1)[1]
            if fmt == "svg":
                return _make_response(200, "OK", "image/svg+xml", svg.encode())
            else:
                png_bytes = generate_screenshot_png(svg)
                return _make_response(200, "OK", "image/png", png_bytes)
        except Exception as e:
            log.error(f"screenshot failed: {e}")
            return _error_response(503, str(e))

    if path in ("/", "/web"):
        html_path = _STATIC_DIR / "index.html"
        if html_path.exists():
            body = html_path.read_bytes()
        else:
            body = _FALLBACK_HTML
        return _make_response(200, "OK", "text/html; charset=utf-8", body)

    if path.startswith("/static/"):
        rel = path[len("/static/"):]
        file_path = _STATIC_DIR / rel
        try:
            resolved = file_path.resolve()
            # Path traversal protection
            if _STATIC_DIR.resolve() not in resolved.parents:
                return _error_response(404, "Not found")
        except OSError:
            return _error_response(404, "Not found")
        if file_path.exists() and file_path.is_file():
            suffix = file_path.suffix.lower()
            content_type = {
                ".woff2": "font/woff2",
                ".css": "text/css",
                ".js": "application/javascript",
                ".html": "text/html; charset=utf-8",
            }.get(suffix, "application/octet-stream")
            return _make_response(200, "OK", content_type, file_path.read_bytes())
        return _error_response(404, "Not found")

    return _error_response(404, "Not found")


async def start_web_server(
    app: AppStateProtocol,
    port: int = API_PORT,
    stop_event: asyncio.Event | None = None,
) -> None:
    """Start the unified HTTP+WebSocket server. Blocks until stop_event is set."""
    global _start_time, _app, _clients
    _start_time = time.time()
    _app = app
    _clients = set()  # reset for test isolation

    settings = load_settings()
    host = "0.0.0.0" if settings.web_lan_access else "127.0.0.1"

    if stop_event is None:
        stop_event = asyncio.Event()

    # Write port file for discovery
    os.makedirs(os.path.dirname(API_PORT_FILE), exist_ok=True)
    with open(API_PORT_FILE, "w") as f:
        f.write(str(port))

    async with websockets.serve(
        _handle_ws,
        host,
        port,
        process_request=_handle_http,
    ):
        log.info(f"Web server started on http://{host}:{port}")
        await stop_event.wait()


async def _handle_ws(websocket: websockets.ServerConnection) -> None:
    """Handle a single WebSocket connection."""
    if len(_clients) >= MAX_WS_CONNECTIONS:
        await websocket.close(1013, "Too many connections")
        return

    _clients.add(websocket)
    log.debug(f"WS connected ({len(_clients)} clients)")
    try:
        # Send current TUI state snapshot first so the client has session context
        if _app:
            try:
                snapshot = _app.call_from_thread(_app.get_state_snapshot)
                snapshot["uptime"] = int(time.time() - _start_time) if _start_time else 0
                snapshot["version"] = __version__
                await websocket.send(json.dumps({"type": "snapshot", "data": snapshot}))
            except Exception as e:
                log.debug(f"snapshot send failed: {e}")

        # Then send initial burst of recent events to populate logs
        tail_start_pos = await _send_initial_burst(websocket)

        # Spawn tail task and receive control messages concurrently
        tail_task = asyncio.create_task(_tail_events(websocket, tail_start_pos))
        try:
            async for message in websocket:
                await _handle_control(message)
        finally:
            tail_task.cancel()
            try:
                await tail_task
            except asyncio.CancelledError:
                pass
    finally:
        _clients.discard(websocket)
        log.debug(f"WS disconnected ({len(_clients)} clients)")


async def _send_initial_burst(websocket: websockets.ServerConnection) -> int:
    """Send the last ~50 events from EVENTS_FILE. Returns file position for tail."""
    try:
        if not os.path.exists(EVENTS_FILE):
            return 0
        with open(EVENTS_FILE, "r") as f:
            lines = f.readlines()
            end_pos = f.tell()
        recent = lines[-50:] if len(lines) > 50 else lines
        for line in recent:
            line = line.strip()
            if line:
                try:
                    data = json.loads(line)
                    await websocket.send(json.dumps({"type": "event", "data": data}))
                except json.JSONDecodeError:
                    pass
        return end_pos
    except OSError:
        return 0


async def _tail_events(websocket: websockets.ServerConnection, start_pos: int = 0) -> None:
    """Tail EVENTS_FILE from start_pos and push new events to the client."""
    try:
        if not os.path.exists(EVENTS_FILE):
            Path(EVENTS_FILE).touch(exist_ok=True)
        with open(EVENTS_FILE, "r") as f:
            f.seek(start_pos)
            while True:
                line = f.readline()
                if line:
                    line = line.strip()
                    if line:
                        try:
                            data = json.loads(line)
                            await websocket.send(json.dumps({"type": "event", "data": data}))
                        except json.JSONDecodeError:
                            pass
                else:
                    await asyncio.sleep(0.2)
    except asyncio.CancelledError:
        raise
    except Exception as e:
        log.debug(f"_tail_events error: {e}")


async def _handle_control(raw: str) -> None:
    """Handle a control message from a WebSocket client."""
    try:
        msg = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return

    action = msg.get("action")
    if action == "toggle_global_pause":
        state = read_state()
        state["global_paused"] = not state.get("global_paused", False)
        _write_state(state)
        await _broadcast_state(state)
    elif action == "toggle_pause":
        session_id = msg.get("session_id")
        if not session_id:
            return
        state = read_state()
        paused = state.get("paused_claude_sessions", [])
        if session_id in paused:
            paused.remove(session_id)
        else:
            paused.append(session_id)
        state["paused_claude_sessions"] = paused
        _write_state(state)
        await _broadcast_state(state)


def _write_state(state: dict) -> None:
    """Atomically write state dict to STATE_FILE."""
    try:
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except OSError as e:
        log.error(f"Failed to write state: {e}")


async def _broadcast_state(state: dict) -> None:
    """Broadcast a state update message to all connected WebSocket clients."""
    msg = json.dumps({
        "type": "state",
        "data": {
            "global_paused": state.get("global_paused", False),
            "paused_sessions": state.get("paused_claude_sessions", []),
        },
    })
    await _broadcast(msg)


async def _broadcast(message: str) -> None:
    """Send message to all connected clients; discard dead connections on error."""
    global _clients
    dead: set = set()
    for client in _clients.copy():
        try:
            await client.send(message)
        except Exception:
            dead.add(client)
    _clients -= dead
