#!/usr/bin/env python3
"""Codex installer for the workflow-checkpoint SessionStart hook.

    python <skill>/.codex/install.py            # install
    python <skill>/.codex/install.py --dry-run  # preview only

Registers `checkpoint.py list --hook` in $CODEX_HOME/hooks.json (default
~/.codex/hooks.json) under SessionStart matcher "startup|resume|clear|compact".
Read-merge-write: removes this skill's old entries first; other hooks are kept.

After installing, run /hooks in Codex and approve the hook. Re-approve after a
reinstall — Codex trusts hooks by command hash, and the path changes on upgrade.
"""

import json
import os
import re
import sys
from pathlib import Path

SKILL_NAME = "workflow-checkpoint"
MATCHER = "startup|resume|clear|compact"
CHECKPOINT = Path(__file__).resolve().parent.parent / "scripts" / "checkpoint.py"
PYTHON = sys.executable


def _home_dir() -> Path:
    env_home = os.environ.get("HOME")
    return Path(env_home) if env_home else Path.home()


def codex_home() -> Path:
    env = os.environ.get("CODEX_HOME")
    return Path(env) if env else _home_dir() / ".codex"


def _hooks_path() -> Path:
    return codex_home() / "hooks.json"


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


def _clean_event_hooks(event_hooks: list) -> None:
    """Remove this skill's hooks from a matcher group list; drop emptied groups."""
    for group in event_hooks:
        group["hooks"] = [
            h for h in group.get("hooks", []) if not _is_ours(h.get("command", ""))
        ]
    event_hooks[:] = [g for g in event_hooks if g.get("hooks")]


def _warn_inline_hooks() -> None:
    """config.toml with an inline [hooks] section merges with hooks.json and
    triggers a Codex startup warning — surface it instead of hiding it."""
    config_toml = codex_home() / "config.toml"
    if not config_toml.exists():
        return
    text = config_toml.read_text(encoding="utf-8")
    if re.search(r"(?m)^\s*\[hooks\]\s*$", text):
        print(
            "  ! warning: config.toml has an inline [hooks] section — Codex merges "
            "it with hooks.json and logs a startup warning. Prefer hooks.json."
        )


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    hook_json = {
        "type": "command",
        "command": _hook_command(),
    }

    if dry_run:
        print(f"Codex home:  {codex_home()}")
        print(f"Hook cmd:    {_hook_command()}")
        print(f"Hooks file:  {_hooks_path()}")
        print()
        print("[DRY-RUN] Run without --dry-run to install.")
        return

    hooks_path = _hooks_path()
    if hooks_path.exists():
        cfg = json.loads(hooks_path.read_text(encoding="utf-8"))
    else:
        cfg = {}

    all_events = cfg.setdefault("hooks", {})
    # Remove this skill's old entries from every event (e.g. SessionStart).
    for event_hooks in all_events.values():
        if isinstance(event_hooks, list):
            _clean_event_hooks(event_hooks)

    session_hooks = all_events.setdefault("SessionStart", [])
    group = next((g for g in session_hooks if g.get("matcher") == MATCHER), None)
    if group is None:
        group = {"matcher": MATCHER, "hooks": []}
        session_hooks.append(group)
    group["hooks"].append(hook_json)

    _write_json_atomic(hooks_path, cfg)

    _warn_inline_hooks()
    print("Installed.")
    print(f"  Hook: {_hook_command()}")
    print(f"  Hooks file: {hooks_path}")
    print()
    print("Next: run /hooks in a Codex session and approve the workflow-checkpoint")
    print("SessionStart hook. After a reinstall, re-approve - hooks are trusted by")
    print("command hash, and the command path changes with the skill location.")


if __name__ == "__main__":
    main()

