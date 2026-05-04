"""Tests for ``hook._tui_is_running`` — the file-less TCP liveness probe.

Earlier the probe read ``/tmp/claude-auto-accept/api-port`` and could be
fooled when an unrelated process deleted that file while the TUI was still
listening. The current implementation just attempts a TCP connect to the
hardcoded ``API_PORT``; the bind itself acts as the mutex.
"""

import io
import json
import os
import socket
import threading

import pytest


def _bind_free_port() -> tuple[socket.socket, int]:
    """Bind a listening socket on a free localhost port and return (sock, port)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _free_port_no_listener() -> int:
    """Find a free port and return it WITHOUT keeping a listener."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestTuiIsRunningProbe:
    """Direct tests of the probe behaviour."""

    def test_returns_true_when_someone_is_listening(self, monkeypatch):
        sock, port = _bind_free_port()
        try:
            monkeypatch.setattr("claude_monitor.hook.API_PORT", port)
            from claude_monitor.hook import _tui_is_running
            assert _tui_is_running() is True
        finally:
            sock.close()

    def test_returns_false_when_nothing_is_listening(self, monkeypatch):
        port = _free_port_no_listener()
        monkeypatch.setattr("claude_monitor.hook.API_PORT", port)
        from claude_monitor.hook import _tui_is_running
        assert _tui_is_running() is False

    def test_does_not_depend_on_a_port_file(self, monkeypatch, tmp_path):
        """Removing any incidental port file must not change the answer.

        The old implementation read a port file; this regression-guards against
        any reintroduction of file-based liveness.
        """
        sock, port = _bind_free_port()
        try:
            monkeypatch.setattr("claude_monitor.hook.API_PORT", port)
            stale_file = tmp_path / "api-port"
            assert not stale_file.exists()
            from claude_monitor.hook import _tui_is_running
            assert _tui_is_running() is True
        finally:
            sock.close()


class TestHookEndToEnd:
    """End-to-end through ``hook.main``: probe outcome shapes the decision."""

    def _run(self, input_data, monkeypatch):
        import claude_monitor.hook as hook

        stdin = io.StringIO(json.dumps(input_data))
        stdout = io.StringIO()
        monkeypatch.setattr("sys.stdin", stdin)
        monkeypatch.setattr("sys.stdout", stdout)
        mock_stderr = io.StringIO()
        mock_stderr.isatty = lambda: True
        mock_stderr.fileno = lambda: 2
        monkeypatch.setattr("sys.stderr", mock_stderr)
        monkeypatch.setattr("claude_monitor.hook.os.ttyname", lambda fd: "/dev/ttys999")
        monkeypatch.delenv("ITERM_SESSION_ID", raising=False)
        hook.main()
        return stdout.getvalue()

    def test_no_monitor_when_nothing_listening(self, isolated_state, monkeypatch):
        """Hook stays silent (no allow output) when the TUI is not reachable."""
        monkeypatch.setattr("claude_monitor.hook.API_PORT", _free_port_no_listener())
        out = self._run(
            {
                "hook_event_name": "PermissionRequest",
                "tool_name": "Read",
                "session_id": "s1",
                "cwd": "/tmp",
                "tool_input": {},
            },
            monkeypatch,
        )
        assert out == ""

        with open(isolated_state["events_file"]) as f:
            event = json.loads(f.readline())
        assert event["_decision"] == "no_monitor"

    def test_allow_when_listener_present(self, isolated_state, monkeypatch):
        """Hook outputs ``allow`` when the probe finds a listener."""
        sock, port = _bind_free_port()
        try:
            monkeypatch.setattr("claude_monitor.hook.API_PORT", port)
            out = self._run(
                {
                    "hook_event_name": "PermissionRequest",
                    "tool_name": "Read",
                    "session_id": "s1",
                    "cwd": "/tmp",
                    "tool_input": {},
                },
                monkeypatch,
            )
        finally:
            sock.close()

        result = json.loads(out)
        assert result["hookSpecificOutput"]["decision"]["behavior"] == "allow"

        with open(isolated_state["events_file"]) as f:
            event = json.loads(f.readline())
        assert event["_decision"] == "allowed"
