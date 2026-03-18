"""Tests for the unified WebSocket+HTTP server (web.py).

This file is written BEFORE claude_monitor.web exists — it will fail with
    ImportError: No module named 'claude_monitor.web'
until web.py is created (Task 4 Step 3).
"""

import asyncio
import socket

import httpx
import pytest

import json as _json

import websockets
from websockets.exceptions import ConnectionClosed


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


class TestWebSocket:
    """WebSocket lifecycle, control, and limit tests."""

    async def test_connect_receives_snapshot(self, web_server):
        """First connection should yield a snapshot message (possibly after event messages)."""
        port = web_server["port"]
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            msg = None
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                data = _json.loads(raw)
                if data["type"] == "snapshot":
                    msg = data
                    break
            assert msg is not None, "No snapshot message received"
            assert msg["type"] == "snapshot"
            assert "global_mode" in msg["data"]

    async def test_initial_burst_from_events(self, web_server, isolated_state):
        """Writing 5 events before connecting should cause 5 event messages in the burst."""
        port = web_server["port"]
        events_file = isolated_state["events_file"]

        events = [{"hook_event_name": "Notification", "idx": i} for i in range(5)]
        with open(events_file, "w") as f:
            for ev in events:
                f.write(_json.dumps(ev) + "\n")

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            event_messages = []
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                except asyncio.TimeoutError:
                    break
                data = _json.loads(raw)
                if data["type"] == "event":
                    event_messages.append(data)
                elif data["type"] == "snapshot":
                    break

        assert len(event_messages) == 5

    async def test_toggle_global_pause(self, web_server, isolated_state):
        """Sending toggle_global_pause should broadcast a state message and update state.json."""
        port = web_server["port"]
        state_file = isolated_state["state_file"]

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    data = _json.loads(raw)
                    if data["type"] == "snapshot":
                        break
                except asyncio.TimeoutError:
                    break

            await ws.send(_json.dumps({"action": "toggle_global_pause"}))

            state_msg = None
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                data = _json.loads(raw)
                if data["type"] == "state":
                    state_msg = data
                    break

        assert state_msg is not None, "No state broadcast received"
        assert "global_paused" in state_msg["data"]
        assert state_msg["data"]["global_paused"] is True

        with open(state_file) as f:
            persisted = _json.load(f)
        assert persisted["global_paused"] is True

    async def test_toggle_session_pause(self, web_server, isolated_state):
        """Sending toggle_pause with session_id updates paused_sessions in broadcast."""
        port = web_server["port"]

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    data = _json.loads(raw)
                    if data["type"] == "snapshot":
                        break
                except asyncio.TimeoutError:
                    break

            await ws.send(_json.dumps({"action": "toggle_pause", "session_id": "session-abc"}))

            state_msg = None
            for _ in range(10):
                raw = await asyncio.wait_for(ws.recv(), timeout=2)
                data = _json.loads(raw)
                if data["type"] == "state":
                    state_msg = data
                    break

        assert state_msg is not None, "No state broadcast received"
        assert "paused_sessions" in state_msg["data"]
        assert "session-abc" in state_msg["data"]["paused_sessions"]

    async def test_malformed_json_ignored(self, web_server):
        """Sending garbage JSON should not close the connection."""
        port = web_server["port"]
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    data = _json.loads(raw)
                    if data["type"] == "snapshot":
                        break
                except asyncio.TimeoutError:
                    break

            await ws.send("not valid json {{{{")
            await asyncio.wait_for(ws.ping(), timeout=2)

    async def test_unknown_action_ignored(self, web_server):
        """Sending an unknown action should not close the connection."""
        port = web_server["port"]
        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    data = _json.loads(raw)
                    if data["type"] == "snapshot":
                        break
                except asyncio.TimeoutError:
                    break

            await ws.send(_json.dumps({"action": "do_something_unknown"}))
            await asyncio.wait_for(ws.ping(), timeout=2)

    async def test_connection_limit(self, web_server):
        """Opening 25 connections should succeed; the 26th should be closed with code 1013."""
        port = web_server["port"]
        conns = []
        try:
            for _ in range(25):
                ws = await websockets.connect(f"ws://127.0.0.1:{port}/ws")
                conns.append(ws)

            await asyncio.sleep(0.5)

            ws26 = await websockets.connect(f"ws://127.0.0.1:{port}/ws")
            try:
                with pytest.raises(ConnectionClosed) as exc_info:
                    await asyncio.wait_for(ws26.recv(), timeout=3)
                assert exc_info.value.rcvd.code == 1013
            finally:
                await ws26.close()
        finally:
            for ws in conns:
                await ws.close()

    async def test_event_tail_pushes_new_events(self, web_server, isolated_state):
        """After connecting, a new event written to EVENTS_FILE should be pushed via tail."""
        port = web_server["port"]
        events_file = isolated_state["events_file"]

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws:
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=1)
                    data = _json.loads(raw)
                    if data["type"] == "snapshot":
                        break
                except asyncio.TimeoutError:
                    break

            new_event = {"hook_event_name": "Notification", "message": "tail-test"}
            with open(events_file, "a") as f:
                f.write(_json.dumps(new_event) + "\n")

            tail_msg = None
            for _ in range(20):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    data = _json.loads(raw)
                    if data["type"] == "event" and data["data"].get("message") == "tail-test":
                        tail_msg = data
                        break
                except asyncio.TimeoutError:
                    break

        assert tail_msg is not None, "Tail task did not push new event"
        assert tail_msg["data"]["hook_event_name"] == "Notification"

    async def test_broadcast_to_multiple_clients(self, web_server, isolated_state):
        """Both clients should receive a state broadcast when one sends toggle_global_pause."""
        port = web_server["port"]

        async with websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws1, \
                   websockets.connect(f"ws://127.0.0.1:{port}/ws") as ws2:

            async def drain(ws):
                for _ in range(20):
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=1)
                        data = _json.loads(raw)
                        if data["type"] == "snapshot":
                            return
                    except asyncio.TimeoutError:
                        return

            await asyncio.gather(drain(ws1), drain(ws2))

            await ws1.send(_json.dumps({"action": "toggle_global_pause"}))

            async def get_state_msg(ws):
                for _ in range(10):
                    raw = await asyncio.wait_for(ws.recv(), timeout=2)
                    data = _json.loads(raw)
                    if data["type"] == "state":
                        return data
                return None

            msg1, msg2 = await asyncio.gather(
                get_state_msg(ws1),
                get_state_msg(ws2),
            )

        assert msg1 is not None, "ws1 did not receive state broadcast"
        assert msg2 is not None, "ws2 did not receive state broadcast"
        assert msg1["data"]["global_paused"] is True
        assert msg2["data"]["global_paused"] is True
