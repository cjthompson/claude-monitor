#!/usr/bin/env python3
"""Setup script for claude-monitor.

Creates a venv, installs the package, and optionally configures
Claude Code hooks in ~/.claude/settings.json.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_DIR = Path(__file__).resolve().parent
VENV_DIR = REPO_DIR / ".venv"
LOCAL_BIN = Path.home() / ".local" / "bin"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

HOOK_COMMAND = str(VENV_DIR / "bin" / "claude-monitor-hook")

HOOKS_CONFIG = {
    "PermissionRequest": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "Notification": [
        {
            "matcher": "permission_prompt|idle_prompt",
            "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}],
        }
    ],
    "SubagentStart": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "SubagentStop": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
}


def run(cmd, **kwargs):
    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd, **kwargs)


def setup_venv():
    if VENV_DIR.exists():
        print(f"venv already exists at {VENV_DIR}")
    else:
        print(f"Creating venv at {VENV_DIR} ...")
        run([sys.executable, "-m", "venv", str(VENV_DIR)])

    print("Installing claude-monitor in editable mode ...")
    run([str(VENV_DIR / "bin" / "pip"), "install", "-e", str(REPO_DIR)])


def symlink_to_path():
    print()
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    for name in ("claude-monitor", "claude-monitor-hook"):
        src = VENV_DIR / "bin" / name
        dst = LOCAL_BIN / name
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)
        print(f"  {dst} → {src}")
    print(f"Symlinked to {LOCAL_BIN} (on PATH)")


def configure_hooks():
    print()
    print(f"Claude Code settings file: {SETTINGS_FILE}")
    print()
    print("This will add the following hooks to your settings:")
    print("  - PermissionRequest  (auto-accept permissions)")
    print("  - Notification       (log idle/permission prompts)")
    print("  - SubagentStart      (track agent spawns)")
    print("  - SubagentStop       (track agent completions)")
    print()
    print(f"Hook command: {HOOK_COMMAND}")
    print()

    answer = input("Add hooks to ~/.claude/settings.json? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped. You can add hooks manually later.")
        return

    # Load or create settings
    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    else:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    # Check for existing hooks
    existing = settings.get("hooks", {})
    conflicts = [k for k in HOOKS_CONFIG if k in existing]
    if conflicts:
        print()
        print(f"Warning: existing hooks found for: {', '.join(conflicts)}")
        answer = input("Overwrite these hooks? [y/N] ").strip().lower()
        if answer not in ("y", "yes"):
            print("Skipped. Existing hooks left unchanged.")
            return

    # Merge hooks
    if "hooks" not in settings:
        settings["hooks"] = {}
    settings["hooks"].update(HOOKS_CONFIG)

    # Write back
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"Hooks written to {SETTINGS_FILE}")


def main():
    print("=== claude-monitor setup ===")
    print()

    setup_venv()

    symlink_to_path()
    configure_hooks()

    print()
    print("Done! Run `claude-monitor` to launch the TUI.")


if __name__ == "__main__":
    main()
