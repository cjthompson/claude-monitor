"""Tests for the EADDRINUSE handler in ``app_base``.

Covers:
- Helper functions (``_find_port_holder``, ``_process_cmdline``, ``_kill_pid``).
- ``MonitorApp._handle_port_in_use`` decision branches: missing holder,
  self-PID guard, cmdline whitelist, and the user-confirmed kill flow.

The modal handshake (``call_from_thread`` → ``push_screen`` → user click) is
exercised by stubbing ``call_from_thread`` to invoke the response callback
synchronously. Driving a real Textual screen would require a running app
loop, which is overkill for unit-testing the gating logic.
"""

import os
import socket
import subprocess
import sys
import time

import pytest

from claude_monitor.app_base import (
    MonitorApp,
    _find_port_holder,
    _kill_pid,
    _process_cmdline,
)


def _bind_free_port() -> tuple[socket.socket, int]:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    return sock, sock.getsockname()[1]


def _unused_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class TestFindPortHolder:
    def test_returns_pid_of_listener(self):
        sock, port = _bind_free_port()
        try:
            assert _find_port_holder(port) == os.getpid()
        finally:
            sock.close()

    def test_returns_none_when_unbound(self):
        assert _find_port_holder(_unused_port()) is None


class TestProcessCmdline:
    def test_returns_command_for_self(self):
        cmd = _process_cmdline(os.getpid())
        assert cmd is not None
        # Running under pytest, so the cmdline contains python or pytest.
        assert "python" in cmd.lower() or "pytest" in cmd.lower()

    def test_returns_none_for_nonexistent_pid(self):
        # PID 0 is reserved on Unix; ps won't return its cmdline.
        # Use a likely-unused PID (very high number).
        assert _process_cmdline(2_000_000) is None


class TestKillPid:
    def test_kills_running_subprocess(self):
        # In production the holder's parent (run.sh) reaps the SIGTERM'd child
        # within microseconds, so `os.kill(pid, 0)` correctly returns
        # ProcessLookupError. In tests the parent IS this process, so a dead
        # child lingers as a zombie and `os.kill(pid, 0)` still succeeds —
        # masking the kill. Ignoring SIGCHLD asks the kernel to auto-reap so
        # the test environment matches production.
        import signal
        prev = signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(60)"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            assert _kill_pid(proc.pid) is True
        finally:
            signal.signal(signal.SIGCHLD, prev)
            if proc.poll() is None:
                proc.kill()
                try:
                    proc.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    pass

    def test_returns_true_for_already_dead_process(self):
        # Spawn and reap, then try to kill the (now defunct) PID.
        proc = subprocess.Popen([sys.executable, "-c", "pass"])
        proc.wait()
        # The PID may be reused by the OS; that's not a concern here because
        # _kill_pid simply reports True on ProcessLookupError.
        assert _kill_pid(proc.pid) is True


class _StubApp:
    """Minimal stand-in for ``MonitorApp`` for testing ``_handle_port_in_use``.

    Instead of pushing a real modal, ``call_from_thread`` immediately invokes
    the ``on_response`` callback with a pre-configured answer. ``None`` means
    "never call back" — i.e. simulate the modal timeout.
    """

    def __init__(self, response: bool | None = True):
        self._response = response
        self.modal_pushed = False
        self.modal_pid: int | None = None

    def push_screen(self, screen, callback=None):
        # Production code never calls this directly — call_from_thread does.
        # We define it as an attribute so the bound method exists when
        # _handle_port_in_use looks up ``self.push_screen``.
        pass

    def call_from_thread(self, fn, *args, **kwargs):
        # Signature in production: call_from_thread(self.push_screen, screen, on_response)
        screen = args[0]
        on_response = args[1]
        self.modal_pushed = True
        self.modal_pid = getattr(screen, "pid", None)
        if self._response is not None:
            on_response(self._response)


class TestHandlePortInUse:
    """Branch coverage for ``MonitorApp._handle_port_in_use``."""

    def test_returns_false_when_no_holder_found(self, monkeypatch):
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: None)
        stub = _StubApp(response=True)
        assert MonitorApp._handle_port_in_use(stub, 17233) is False
        assert stub.modal_pushed is False

    def test_returns_false_when_holder_is_self(self, monkeypatch):
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: os.getpid())
        stub = _StubApp(response=True)
        assert MonitorApp._handle_port_in_use(stub, 17233) is False
        assert stub.modal_pushed is False

    def test_returns_false_when_holder_is_unrelated_process(self, monkeypatch):
        """Cmdline whitelist must reject anything that isn't claude-monitor."""
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: 9999)
        monkeypatch.setattr(
            "claude_monitor.app_base._process_cmdline",
            lambda pid: "/usr/local/bin/redis-server *:6379",
        )
        kill_called = []
        monkeypatch.setattr(
            "claude_monitor.app_base._kill_pid",
            lambda pid: kill_called.append(pid) or True,
        )
        stub = _StubApp(response=True)
        assert MonitorApp._handle_port_in_use(stub, 17233) is False
        assert stub.modal_pushed is False, "Unrelated process must NOT prompt"
        assert kill_called == [], "Unrelated process must NOT be killed"

    def test_returns_false_when_cmdline_unavailable(self, monkeypatch):
        """If ``ps`` returns nothing, refuse to kill (we can't verify identity)."""
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: 9999)
        monkeypatch.setattr("claude_monitor.app_base._process_cmdline", lambda pid: None)
        stub = _StubApp(response=True)
        assert MonitorApp._handle_port_in_use(stub, 17233) is False
        assert stub.modal_pushed is False

    def test_kills_holder_when_user_confirms(self, monkeypatch):
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: 12345)
        monkeypatch.setattr(
            "claude_monitor.app_base._process_cmdline",
            lambda pid: "/Users/x/.venv/bin/claude-monitor --simple",
        )
        killed = []
        monkeypatch.setattr(
            "claude_monitor.app_base._kill_pid",
            lambda pid: killed.append(pid) or True,
        )
        # Skip the post-kill sleep so the test stays fast.
        monkeypatch.setattr("claude_monitor.app_base.time.sleep", lambda _: None)

        stub = _StubApp(response=True)
        assert MonitorApp._handle_port_in_use(stub, 17233) is True
        assert stub.modal_pushed is True
        assert stub.modal_pid == 12345
        assert killed == [12345]

    def test_does_not_kill_when_user_declines(self, monkeypatch):
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: 12345)
        monkeypatch.setattr(
            "claude_monitor.app_base._process_cmdline",
            lambda pid: "claude-monitor --simple",
        )
        killed = []
        monkeypatch.setattr(
            "claude_monitor.app_base._kill_pid",
            lambda pid: killed.append(pid) or True,
        )

        stub = _StubApp(response=False)
        assert MonitorApp._handle_port_in_use(stub, 17233) is False
        assert stub.modal_pushed is True
        assert killed == []

    def test_returns_false_when_kill_fails(self, monkeypatch):
        """If SIGTERM/SIGKILL can't reach the process, don't claim success."""
        monkeypatch.setattr("claude_monitor.app_base._find_port_holder", lambda port: 12345)
        monkeypatch.setattr(
            "claude_monitor.app_base._process_cmdline",
            lambda pid: "claude-monitor",
        )
        monkeypatch.setattr("claude_monitor.app_base._kill_pid", lambda pid: False)

        stub = _StubApp(response=True)
        assert MonitorApp._handle_port_in_use(stub, 17233) is False
