"""Tests for install.symlink_to_path() — exposing console scripts on PATH."""

import sys
from pathlib import Path

# install.py is a root-level script, not a package module
sys.path.insert(0, str(Path(__file__).parent.parent))
import install  # noqa: E402

EXPECTED_COMMANDS = (
    "claude-monitor",
    "claude-monitor-hook",
    "claude-monitor-statusline",
    "claude-monitor-credentials",
)


def test_symlinks_every_console_script(tmp_path, monkeypatch, capsys):
    venv_bin = tmp_path / "venv" / "bin"
    venv_bin.mkdir(parents=True)
    local_bin = tmp_path / "local" / "bin"
    for name in EXPECTED_COMMANDS:
        (venv_bin / name).write_text("#!/bin/sh\n")

    monkeypatch.setattr(install, "VENV_DIR", venv_bin.parent)
    monkeypatch.setattr(install, "LOCAL_BIN", local_bin)

    install.symlink_to_path()

    for name in EXPECTED_COMMANDS:
        link = local_bin / name
        assert link.is_symlink(), f"{name} not symlinked onto PATH"
        assert link.resolve() == (venv_bin / name).resolve()


def test_symlink_credentials_matches_pyproject_entry_point():
    # The installer must expose exactly the console scripts declared in pyproject.
    pyproject = (Path(install.__file__).parent / "pyproject.toml").read_text()
    assert 'claude-monitor-credentials = "claude_monitor.cli_credentials:main"' in pyproject
