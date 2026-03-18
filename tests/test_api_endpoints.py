"""Tests for the HTTP API server."""

import asyncio
import socket

import httpx
import pytest

from tests.conftest import _make_permission_event


def _get_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _patch_api_port(monkeypatch, isolated_state, port):
    """Patch start_api_server to use a specific port + isolated port file.

    Note: app_fixture_with_api already wraps start_api_server for fast timeout.
    We re-wrap whatever is currently in place to inject the desired port.
    """
    import claude_monitor.api as api_mod

    # Grab the already-wrapped version (from app_fixture_with_api's fast_timeout_start)
    already_wrapped = api_mod.start_api_server

    def patched_start(app, port=port):
        old = api_mod.API_PORT_FILE
        api_mod.API_PORT_FILE = isolated_state["api_port_file"]
        try:
            return already_wrapped(app, port=port)
        finally:
            api_mod.API_PORT_FILE = old

    monkeypatch.setattr("claude_monitor.api.start_api_server", patched_start)
    monkeypatch.setattr("claude_monitor.app_base.start_api_server", patched_start)


class TestAPIEndpoints:

    async def test_health_endpoint(self, app_fixture_with_api, isolated_state, monkeypatch, inject_event):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
            assert resp.status_code == 200
            data = resp.json()
            assert data["status"] == "ok"
            assert "version" in data

    async def test_health_uptime(self, app_fixture_with_api, isolated_state, monkeypatch, inject_event):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/health")
            data = resp.json()
            assert "uptime" in data
            assert isinstance(data["uptime"], int)
            assert data["uptime"] >= 0

    async def test_screenshot_svg(self, app_fixture_with_api, isolated_state, monkeypatch, inject_event):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/screenshot?format=svg")
            assert resp.status_code == 200
            assert "svg" in resp.headers.get("content-type", "").lower()

    async def test_screenshot_png(self, app_fixture_with_api, isolated_state, monkeypatch, inject_event):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/screenshot?format=png")
            assert resp.status_code == 200
            assert resp.headers.get("content-type", "") == "image/png"

    async def test_text_endpoint(self, app_fixture_with_api, isolated_state, monkeypatch, inject_event):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/text")
            assert resp.status_code == 200
            data = resp.json()
            assert "global_mode" in data
            assert "sessions" in data
            assert data["global_mode"] == "auto"

    async def test_text_with_sessions(self, app_fixture_with_api, isolated_state, monkeypatch):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        from claude_monitor.messages import HookEvent

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            # Post directly — watch_events is patched out, so file injection won't work.
            app_fixture_with_api.post_message(HookEvent(_make_permission_event(session_id="sess-api-test")))
            for _ in range(20):
                await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/text")
            data = resp.json()
            assert len(data["sessions"]) >= 1
            sess = data["sessions"][0]
            assert "id" in sess
            assert "mode" in sess

    async def test_404_on_unknown(self, app_fixture_with_api, isolated_state, monkeypatch, inject_event):
        port = _get_free_port()
        _patch_api_port(monkeypatch, isolated_state, port)

        async with app_fixture_with_api.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await asyncio.sleep(0.5)

            async with httpx.AsyncClient() as client:
                resp = await client.get(f"http://127.0.0.1:{port}/nonexistent")
            assert resp.status_code == 404
