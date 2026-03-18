"""Tests for the unified WebSocket+HTTP server (web.py).

This file is written BEFORE claude_monitor.web exists — it will fail with
    ImportError: No module named 'claude_monitor.web'
until web.py is created (Task 4 Step 3).
"""

import asyncio
import socket

import httpx
import pytest


def _get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
async def web_server(isolated_state, monkeypatch):
    """Start web.py server on a free port with a mock app.

    Patches EVENTS_FILE, STATE_FILE, and API_PORT_FILE in the web module
    to use isolated temp-dir paths from the isolated_state fixture.
    """
    import claude_monitor.web as web_mod
    monkeypatch.setattr(web_mod, "EVENTS_FILE", isolated_state["events_file"])
    monkeypatch.setattr(web_mod, "STATE_FILE", isolated_state["state_file"])
    monkeypatch.setattr(web_mod, "API_PORT_FILE", isolated_state["api_port_file"])

    port = _get_free_port()

    class MockApp:
        def call_from_thread(self, fn, *args):
            return fn(*args) if args else fn()

        def get_state_snapshot(self):
            return {"global_mode": "auto", "sessions": [], "dashboard": None, "usage": None}

        def export_screenshot(self):
            return "<svg></svg>"

    app = MockApp()
    stop_event = asyncio.Event()

    from claude_monitor.web import start_web_server
    server_task = asyncio.create_task(
        start_web_server(app, port=port, stop_event=stop_event)
    )
    await asyncio.sleep(0.3)  # let server bind and start accepting

    yield {"port": port, "app": app, "stop_event": stop_event}

    stop_event.set()
    server_task.cancel()
    try:
        await server_task
    except asyncio.CancelledError:
        pass


class TestWebHTTP:
    async def test_health(self, web_server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{web_server['port']}/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ok"

    async def test_text(self, web_server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{web_server['port']}/text")
        assert resp.status_code == 200
        data = resp.json()
        assert "global_mode" in data

    async def test_web_serves_html(self, web_server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{web_server['port']}/web")
        assert resp.status_code == 200
        assert "text/html" in resp.headers.get("content-type", "")

    async def test_404_unknown(self, web_server):
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"http://127.0.0.1:{web_server['port']}/nonexistent")
        assert resp.status_code == 404
