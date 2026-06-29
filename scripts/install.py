#!/usr/bin/env python3
"""Cross-platform installer for workflow-checkpoint SessionStart hook.

    python install.py             # install
    python install.py --dry-run   # preview only

Registers a direct python command in ~/.claude/settings.json.
No generated .ps1/.sh scripts, no CLAUDE.md injection.
"""

import json
import sys
from pathlib import Path

HOME = Path.home()
SETTINGS = HOME / ".claude" / "settings.json"
CHECKPOINT = HOME / ".claude" / "skills" / "workflow-checkpoint" / "scripts" / "checkpoint.py"
PYTHON = sys.executable  # the Python running right now — zero search needed


def main():
    dry_run = "--dry-run" in sys.argv

    cmd = f"{PYTHON} {CHECKPOINT} list --hook"

    hook_json = {
        "type": "command",
        "async": False,
        "command": cmd,
    }

    if dry_run:
        print(f"Python:      {PYTHON}")
        print(f"Checkpoint:  {CHECKPOINT}")
        print(f"Hook cmd:    {cmd}")
        print(f"Settings:    {SETTINGS}")
        print()
        print("[DRY-RUN] Run without --dry-run to install.")
        return

    if not SETTINGS.exists():
        print(f"settings.json not found at {SETTINGS}. Run Claude Code once first.")
        sys.exit(1)

    cfg = json.loads(SETTINGS.read_text(encoding="utf-8"))

    # Register SessionStart hook
    session_hooks = cfg.setdefault("hooks", {}).setdefault("SessionStart", [])

    # Remove any previous workflow-checkpoint hook from the first entry
    if session_hooks:
        entry = session_hooks[0]
        hooks_list = entry.setdefault("hooks", [])
        existing = [h for h in hooks_list if "workflow-checkpoint" in h.get("command", "")]
        for h in existing:
            hooks_list.remove(h)

    # Add new hook
    if session_hooks:
        session_hooks[0].setdefault("hooks", []).append(hook_json)
    else:
        session_hooks.append({
            "matcher": "startup|clear|compact",
            "hooks": [hook_json],
        })

    SETTINGS.write_text(json.dumps(cfg, indent=2, ensure_ascii=False), encoding="utf-8")

    print("Installed.")
    print(f"  Hook: {cmd}")
    print(f"  Settings: {SETTINGS}")


if __name__ == "__main__":
    main()
