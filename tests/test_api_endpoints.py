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
    """Patch start_web_server to use a specific port + isolated port file."""
    import claude_monitor.web as web_mod
    import claude_monitor.app_base as app_base_mod

    original_start = web_mod.start_web_server

    async def patched_start(app, port=port, stop_event=None):
        web_mod.API_PORT_FILE = isolated_state["api_port_file"]
        await original_start(app, port=port, stop_event=stop_event)

    monkeypatch.setattr("claude_monitor.web.start_web_server", patched_start)
    monkeypatch.setattr("claude_monitor.app_base.start_web_server", patched_start)
    monkeypatch.setattr(app_base_mod, "API_PORT", port)


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


class TestAPIHelpers:
    """Unit tests for standalone helper functions extracted from MonitorHTTPHandler.

    These tests are intentionally written before the helpers exist (TDD).
    They will fail with ImportError until generate_health_response,
    app_state_snapshot, generate_screenshot_svg, and generate_screenshot_png
    are extracted as module-level functions in claude_monitor/api.py.
    """

    def test_generate_health_response_imports(self):
        from claude_monitor.api import generate_health_response  # noqa: F401

    def test_generate_health_response_structure(self):
        from claude_monitor.api import generate_health_response

        result = generate_health_response(start_time=1000.0, now=1060.0)
        assert isinstance(result, dict)
        assert result["status"] == "ok"
        assert "version" in result
        assert result["uptime"] == 60

    def test_generate_health_response_zero_uptime_when_no_start(self):
        from claude_monitor.api import generate_health_response

        result = generate_health_response(start_time=None, now=1000.0)
        assert result["uptime"] == 0

    def test_app_state_snapshot_imports(self):
        from claude_monitor.api import app_state_snapshot  # noqa: F401

    def test_app_state_snapshot_shape(self):
        from claude_monitor.api import app_state_snapshot

        class FakeApp:
            def get_state_snapshot(self):
                return {"global_mode": "auto", "sessions": []}

            def call_from_thread(self, fn):
                return fn()

        result = app_state_snapshot(app=FakeApp(), start_time=1000.0, now=1010.0)
        assert isinstance(result, dict)
        assert "global_mode" in result
        assert "sessions" in result
        assert "uptime" in result
        assert result["uptime"] == 10

    def test_generate_screenshot_svg_imports(self):
        from claude_monitor.api import generate_screenshot_svg  # noqa: F401

    def test_generate_screenshot_svg_callable(self):
        from claude_monitor.api import generate_screenshot_svg

        assert callable(generate_screenshot_svg)

    def test_generate_screenshot_png_imports(self):
        from claude_monitor.api import generate_screenshot_png  # noqa: F401

    def test_generate_screenshot_png_callable(self):
        from claude_monitor.api import generate_screenshot_png

        assert callable(generate_screenshot_png)
