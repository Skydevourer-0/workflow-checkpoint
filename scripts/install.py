#!/usr/bin/env python3
"""Legacy compatibility entry — detects the target environment and forwards.

    python scripts/install.py              # auto: Claude Code or Codex installer
    python scripts/install.py --dry-run    # preview only

Forwards to .claude/install.py when ~/.claude/settings.json exists, else to
.codex/install.py when the Codex home exists, else prints usage.
New installs should call the per-platform installer directly:
  Claude Code:  python <skill>/.claude/install.py
  Codex:        python <skill>/.codex/install.py
"""

import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent


def _home_dir() -> Path:
    env_home = os.environ.get("HOME")
    return Path(env_home) if env_home else Path.home()


def _codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else _home_dir() / ".codex"


def main() -> None:
    args = sys.argv[1:]

    if (_home_dir() / ".claude" / "settings.json").exists():
        target = SKILL_DIR / ".claude" / "install.py"
    elif _codex_home().exists():
        target = SKILL_DIR / ".codex" / "install.py"
    else:
        print("No target environment detected.")
        print("  Claude Code:  python <skill>/.claude/install.py")
        print("  Codex:        python <skill>/.codex/install.py")
        return

    result = subprocess.run([sys.executable, str(target), *args])
    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
