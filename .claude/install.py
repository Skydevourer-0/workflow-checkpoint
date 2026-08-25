#!/usr/bin/env python3
"""Claude Code installer for the workflow-checkpoint SessionStart hook.

    python <skill>/.claude/install.py            # install
    python <skill>/.claude/install.py --dry-run  # preview only

Registers `checkpoint.py list --hook` in ~/.claude/settings.json under
SessionStart matcher "startup|resume|clear|compact". Removes this skill's old
hook entries first (deduped by command); never touches other skills' hooks.
"""

import json
import os
import sys
from pathlib import Path

SKILL_NAME = "workflow-checkpoint"
MATCHER = "startup|resume|clear|compact"
CHECKPOINT = Path(__file__).resolve().parent.parent / "scripts" / "checkpoint.py"
PYTHON = sys.executable


def _home_dir() -> Path:
    env_home = os.environ.get("HOME")
    return Path(env_home) if env_home else Path.home()


def _settings_path() -> Path:
    return _home_dir() / ".claude" / "settings.json"


def _hook_command() -> str:
    return _fmt_cmd(PYTHON, str(CHECKPOINT), "list", "--hook")


def _fmt_cmd(python_exe: str, script: str, *args: str) -> str:
    """Build a hook command. Quote a path only when it contains spaces:
    Windows cmd / CreateProcess fails when the executable name itself is
    quoted (e.g. Codex hooks), while a quoted path with spaces works on
    Claude Code. Unquoted paths work everywhere."""
    def _q(p: str) -> str:
        return f'"{p}"' if " " in p else p
    return " ".join([_q(python_exe), _q(script), *args])


def _is_ours(command: str) -> bool:
    return SKILL_NAME in command


def _write_json_atomic(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def _clean_session_hooks(session_hooks: list) -> None:
    """Remove this skill's hooks from every SessionStart group; drop emptied groups."""
    for group in session_hooks:
        group["hooks"] = [
            h for h in group.get("hooks", []) if not _is_ours(h.get("command", ""))
        ]
    session_hooks[:] = [g for g in session_hooks if g.get("hooks")]


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    hook_json = {
        "type": "command",
        "async": False,
        "command": _hook_command(),
    }

    if dry_run:
        print(f"Python:      {PYTHON}")
        print(f"Checkpoint:  {CHECKPOINT}")
        print(f"Hook cmd:    {_hook_command()}")
        print(f"Settings:    {_settings_path()}")
        print()
        print("[DRY-RUN] Run without --dry-run to install.")
        return

    settings = _settings_path()
    if settings.exists():
        cfg = json.loads(settings.read_text(encoding="utf-8"))
    else:
        cfg = {}

    session_hooks = cfg.setdefault("hooks", {}).setdefault("SessionStart", [])
    _clean_session_hooks(session_hooks)

    group = next((g for g in session_hooks if g.get("matcher") == MATCHER), None)
    if group is None:
        group = {"matcher": MATCHER, "hooks": []}
        session_hooks.append(group)
    group["hooks"].append(hook_json)

    _write_json_atomic(settings, cfg)

    print("Installed.")
    print(f"  Hook: {_hook_command()}")
    print(f"  Settings: {settings}")


if __name__ == "__main__":
    main()
