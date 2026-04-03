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
IS_MACOS = sys.platform == "darwin"
VENV_DIR = REPO_DIR / ".venv"
LOCAL_BIN = Path.home() / ".local" / "bin"
SETTINGS_FILE = Path.home() / ".claude" / "settings.json"

HOOK_COMMAND = str(VENV_DIR / "bin" / "claude-monitor-hook")
STATUSLINE_COMMAND = str(VENV_DIR / "bin" / "claude-monitor-statusline")

HOOKS_CONFIG = {
    "PermissionRequest": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 300}]}
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
    "PostToolUse": [
        {
            "matcher": "AskUserQuestion",
            "hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}],
        }
    ],
    # New hooks (CC 2.1.62+)
    "SessionStart": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "SessionEnd": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    # New hooks (CC 2.1.70+)
    "StopFailure": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "PostCompact": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "TaskCreated": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "PermissionDenied": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
    "CwdChanged": [
        {"hooks": [{"type": "command", "command": HOOK_COMMAND, "timeout": 5}]}
    ],
}


def run(cmd, **kwargs):
    print(f"  $ {' '.join(cmd)}")
    subprocess.check_call(cmd, **kwargs)


def has_uv():
    """Check if uv is available on PATH."""
    import shutil as _shutil

    return _shutil.which("uv") is not None


def setup_venv():
    venv_python = VENV_DIR / "bin" / "python"

    if has_uv():
        # uv handles venv creation and editable installs reliably
        if not VENV_DIR.exists():
            print(f"Creating venv at {VENV_DIR} (using uv) ...")
            run(["uv", "venv", str(VENV_DIR), "--python", ">=3.12"])
        elif not venv_python.exists():
            print(f"venv at {VENV_DIR} is broken, recreating (using uv) ...")
            import shutil

            shutil.rmtree(VENV_DIR)
            run(["uv", "venv", str(VENV_DIR), "--python", ">=3.12"])
        else:
            print(f"venv already exists at {VENV_DIR}")

        print("Installing claude-monitor in editable mode (using uv) ...")
        run(["uv", "pip", "install", "-e", str(REPO_DIR), "--python", str(venv_python)])
    else:
        # Fallback to pip — requires Python 3.12+
        if sys.version_info < (3, 12):
            print(f"Error: Python {sys.version_info.major}.{sys.version_info.minor} detected, but 3.12+ is required.")
            print()
            print("Install uv (recommended) and re-run — it will fetch the right Python automatically:")
            print("  Install with Homebrew:  brew install uv")
            print("  Or install from shell:  curl -LsSf https://astral.sh/uv/install.sh | sh")
            print()
            print("Or install Python 3.12+ manually and re-run with:")
            print("  python3.12 install.py")
            sys.exit(1)

        if VENV_DIR.exists():
            try:
                subprocess.check_call(
                    [str(venv_python), "-c", "import pip"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"venv already exists at {VENV_DIR}")
            except (subprocess.CalledProcessError, FileNotFoundError):
                print(f"venv at {VENV_DIR} is broken, recreating ...")
                import shutil

                shutil.rmtree(VENV_DIR)
                run([sys.executable, "-m", "venv", str(VENV_DIR)])
        else:
            print(f"Creating venv at {VENV_DIR} ...")
            run([sys.executable, "-m", "venv", str(VENV_DIR)])

        print("Installing claude-monitor in editable mode ...")
        run([str(venv_python), "-m", "pip", "install", "-e", str(REPO_DIR)])

    if not IS_MACOS:
        print()
        print("Note (Linux): cairosvg requires system packages for PNG screenshot support.")
        print("  Debian/Ubuntu: sudo apt-get install libcairo2-dev")
        print("  Fedora/RHEL:   sudo dnf install cairo-devel")


def symlink_to_path():
    print()
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    for name in ("claude-monitor", "claude-monitor-hook", "claude-monitor-statusline"):
        src = VENV_DIR / "bin" / name
        dst = LOCAL_BIN / name
        if dst.is_symlink() or dst.exists():
            dst.unlink()
        dst.symlink_to(src)
        print(f"  {dst} → {src}")
    print(f"Symlinked to {LOCAL_BIN} (on PATH)")


def _find_monitor_hooks(groups):
    """Return (group_idx, hook_idx, hook) for every hook whose command contains claude-monitor-hook."""
    results = []
    for gi, group in enumerate(groups):
        for hi, hook in enumerate(group.get("hooks", [])):
            if "claude-monitor-hook" in hook.get("command", ""):
                results.append((gi, hi, hook))
    return results


def configure_hooks():
    print()
    print(f"Claude Code settings file: {SETTINGS_FILE}")
    print()
    print("This will configure the following hooks:")
    print("  - PermissionRequest  (auto-accept permissions)")
    print("  - Notification       (log idle/permission prompts)")
    print("  - SubagentStart      (track agent spawns)")
    print("  - SubagentStop       (track agent completions)")
    print("  - PostToolUse        (capture AskUserQuestion answers)")
    print()
    print(f"Hook command: {HOOK_COMMAND}")
    print()

    answer = input("Configure hooks in ~/.claude/settings.json? [y/N] ").strip().lower()
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

    if "hooks" not in settings:
        settings["hooks"] = {}

    # Analyse what needs to happen for each event type (two-pass to ask once)
    to_skip = []    # already configured correctly
    to_add = []     # (event_type, desired_groups, existing_groups_or_None)
    to_replace = [] # (event_type, existing_groups, stale_list, desired_groups)

    for event_type, desired_groups in HOOKS_CONFIG.items():
        existing_groups = settings["hooks"].get(event_type)

        if existing_groups is None:
            # Event type absent entirely — create it
            to_add.append((event_type, desired_groups, None))
            continue

        # Already configured exactly as desired?
        exact = any(
            h.get("command") == HOOK_COMMAND
            for group in existing_groups
            for h in group.get("hooks", [])
        )
        if exact:
            to_skip.append(event_type)
            continue

        # Claude-monitor hook present but with a different path?
        stale = _find_monitor_hooks(existing_groups)
        if stale:
            to_replace.append((event_type, existing_groups, stale, desired_groups))
        else:
            # Unrelated hooks exist — append ours without touching theirs
            to_add.append((event_type, desired_groups, existing_groups))

    # Ask once if any replacements are needed
    overwrite = False
    if to_replace:
        print()
        print("Found existing claude-monitor hooks with a different path:")
        for event_type, _, stale, _ in to_replace:
            for _, _, h in stale:
                print(f"  {event_type}: {h['command']}")
        print(f"New path would be: {HOOK_COMMAND}")
        ans = input("Replace with new path? [y/N] ").strip().lower()
        overwrite = ans in ("y", "yes")

    # Apply changes
    for event_type in to_skip:
        print(f"  {event_type}: already configured correctly, skipping")

    for event_type, desired_groups, existing_groups in to_add:
        if existing_groups is None:
            settings["hooks"][event_type] = list(desired_groups)
        else:
            # Merge into an existing group that shares the same matcher rather
            # than creating a duplicate matcher entry (per Claude Code hook schema)
            for desired_group in desired_groups:
                desired_matcher = desired_group.get("matcher")
                merge_target = next(
                    (g for g in existing_groups if g.get("matcher") == desired_matcher),
                    None,
                )
                if merge_target is not None:
                    merge_target.setdefault("hooks", []).extend(desired_group.get("hooks", []))
                else:
                    existing_groups.append(desired_group)
        print(f"  {event_type}: added")

    for event_type, existing_groups, stale, desired_groups in to_replace:
        if overwrite:
            replacement_hook = desired_groups[0]["hooks"][0]
            for gi, hi, _ in stale:
                existing_groups[gi]["hooks"][hi] = replacement_hook
            settings["hooks"][event_type] = existing_groups
            print(f"  {event_type}: updated hook path")
        else:
            print(f"  {event_type}: skipped (keeping existing path)")

    changed = bool(to_add) or (bool(to_replace) and overwrite)
    if not changed and not to_skip:
        # Nothing to do at all
        return

    if not changed:
        print("\nNo changes needed.")
        return

    # Write back
    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"\nHooks written to {SETTINGS_FILE}")


def configure_statusline():
    """Configure claude-monitor-statusline as Claude Code's statusLine provider.

    Reads rate_limits data (CC 2.1.80+) from Claude Code and feeds it to the TUI.
    NOTE: This replaces Claude Code's default status bar with a compact usage display.
    """
    print()
    print("Statusline integration (optional, requires CC 2.1.80+)")
    print("  Configures claude-monitor-statusline as Claude Code's status line provider.")
    print("  Shows rate limit usage in Claude Code's status bar (5h/7d percentages).")
    print("  Also feeds live rate-limit data to the TUI without polling the Anthropic API.")
    print("  NOTE: Replaces Claude Code's default status bar display.")
    print()

    answer = input("Configure statusLine in ~/.claude/settings.json? [y/N] ").strip().lower()
    if answer not in ("y", "yes"):
        print("Skipped.")
        return

    if SETTINGS_FILE.exists():
        with open(SETTINGS_FILE) as f:
            settings = json.load(f)
    else:
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        settings = {}

    existing = settings.get("statusLine", "")
    if existing == STATUSLINE_COMMAND:
        print("  statusLine: already configured correctly, skipping")
        return

    if existing and "claude-monitor-statusline" not in existing:
        print(f"  Existing statusLine: {existing!r}")
        ans = input("  Overwrite? [y/N] ").strip().lower()
        if ans not in ("y", "yes"):
            print("  Skipped.")
            return

    settings["statusLine"] = STATUSLINE_COMMAND

    with open(SETTINGS_FILE, "w") as f:
        json.dump(settings, f, indent=2)
        f.write("\n")

    print(f"  statusLine written to {SETTINGS_FILE}")


def main():
    print("=== claude-monitor setup ===")
    print()

    setup_venv()

    symlink_to_path()
    configure_hooks()
    configure_statusline()

    print()
    if IS_MACOS:
        print("Done! Run `claude-monitor` to launch the TUI.")
    else:
        print("Done! Run `claude-monitor` to launch the TUI.")
        print("Note (Linux): iTerm2 integration is not available; pane mirroring is disabled.")


if __name__ == "__main__":
    main()
