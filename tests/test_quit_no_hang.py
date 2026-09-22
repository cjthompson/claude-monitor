"""Regression test for Slice A: quitting must not hang on executor shutdown.

Must run out-of-process — the fix ends in os._exit(0), which would kill the
pytest process itself if exercised in-process.
"""

import subprocess
import sys

_HANG_APP_SRC = """
import asyncio
import os
import sys
import time
import traceback

from textual import work
from textual.app import App


class HangApp(App):
    @work(thread=True)
    def wedge(self) -> None:
        # Simulates a stuck @work(thread=True) worker: it runs on asyncio's
        # default executor, the same pool Runner.close() drains for up to
        # 300s on quit. Never joins, never checks a stop flag.
        time.sleep(10000)

    def on_mount(self) -> None:
        self.wedge()
        self.set_timer(0.5, self.action_quit)
"""

_CHILD_SCRIPT = (
    _HANG_APP_SRC
    + """

app = HangApp()


async def _run_and_exit() -> None:
    try:
        await app.run_async(headless=True)
    except BaseException:
        traceback.print_exc()
        sys.stdout.flush()
        sys.stderr.flush()
        os._exit(1)
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


asyncio.run(_run_and_exit())
"""
)

# Old pattern: app.run() (sync, blocking) followed by a trailing os._exit(0).
# This is what the fix replaced — it hangs because Runner.close() drains the
# default executor (up to 300s) before app.run() ever returns, so the
# trailing os._exit(0) never gets a chance to run.
_OLD_PATTERN_CHILD = (
    _HANG_APP_SRC
    + """

app = HangApp()
app.run(headless=True)
os._exit(0)
"""
)


def test_quit_with_wedged_thread_worker_exits_promptly():
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_SCRIPT],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        returncode = proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        raise AssertionError(
            "child process did not exit within 10s — quit hung on executor shutdown"
        )
    assert returncode == 0


def test_old_pattern_with_wedged_thread_worker_hangs():
    """Negative control for the test above.

    Canary: if this starts failing because the old pattern no longer hangs
    (e.g. a future textual release changes where the wedge lands), don't just
    delete it — the positive test above needs re-examination too, since it
    would no longer be proving anything either.
    """
    proc = subprocess.Popen(
        [sys.executable, "-c", _OLD_PATTERN_CHILD],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.PIPE,
    )
    try:
        rc = proc.wait(timeout=5)
        stderr = proc.stderr.read().decode(errors="replace") if proc.stderr else ""
        raise AssertionError(
            f"old pattern exited in <5s (rc={rc}); the wedge no longer lands on "
            f"asyncio's default executor, so the positive test is no longer "
            f"protecting anything. stderr: {stderr}"
        )
    except subprocess.TimeoutExpired:
        pass  # expected: old pattern hangs
    finally:
        proc.kill()
        proc.communicate()
