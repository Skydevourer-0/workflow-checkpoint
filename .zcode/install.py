#!/usr/bin/env python3
"""ZCode installer for the workflow-checkpoint SessionStart hook.

    python <skill>/.zcode/install.py            # install
    python <skill>/.zcode/install.py --dry-run  # preview only

Registers `checkpoint.py list --hook` in ~/.zcode/cli/config.json (the ZCode
user configuration file) under SessionStart matcher
"startup|resume|clear|compact". Read-merge-write: removes this skill's old
entries first; other skills' hooks and unrelated keys are kept.

ZCode hook notes:
- Configuration-file hooks are disabled by default: the installer sets
  hooks.enabled = true (never downgrades an existing true).
- Hook entries use type "process" (command + args, no shell): a "command"
  hook accepts only command/shell/timeout/timeoutMs — an args field would be
  dropped — and a plain command string would run Windows paths through a
  shell parser that strips backslashes.
- Unlike Codex there is no /hooks trust gate; with enabled=true the hook runs
  unconditionally. Hook runs (fired/outcome/duration) are recorded in the
  ZCode log.
"""

import json
import os
import sys
import tempfile
from pathlib import Path

SKILL_NAME = "workflow-checkpoint"
MATCHER = "startup|resume|clear|compact"
CHECKPOINT = Path(__file__).resolve().parent.parent / "scripts" / "checkpoint.py"
PYTHON = sys.executable


def _config_path() -> Path:
    """ZCode user configuration file. $ZCODE_HOME is honored for parity with
    CODEX_HOME; the documented default is ~/.zcode/cli/config.json."""
    home = os.environ.get("ZCODE_HOME") or os.path.expanduser("~/.zcode")
    return Path(os.path.expanduser(home)) / "cli" / "config.json"


def _hook_payload(hook: dict) -> str:
    """Flatten command + args of a hook for matching (process and command
    types)."""
    if not isinstance(hook, dict):
        return ""
    return " ".join([str(hook.get("command", "")), *(str(a) for a in (hook.get("args") or []))])


def _is_ours(hook: dict) -> bool:
    return SKILL_NAME in _hook_payload(hook)


def _hook() -> dict:
    return {"type": "process", "command": PYTHON, "args": [str(CHECKPOINT), "list", "--hook"]}


def _clean_event_groups(events: dict) -> dict:
    """Remove our hooks from every event's matcher groups; drop emptied
    groups. Other skills' hooks are never touched."""
    for name, groups in list(events.items()):
        if not isinstance(groups, list):
            continue
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                continue
            group["hooks"] = [h for h in group["hooks"] if not _is_ours(h)]
        events[name] = [g for g in groups if isinstance(g, dict) and g.get("hooks")]
    return {k: v for k, v in events.items() if v}


def _write_json_atomic(path: Path, cfg: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.write("\n")
    except Exception:
        os.unlink(tmp)
        raise
    os.replace(tmp, path)


def main() -> None:
    dry_run = "--dry-run" in sys.argv
    hook_json = _hook()

    if dry_run:
        print(f"Python:       {PYTHON}")
        print(f"Checkpoint:   {CHECKPOINT}")
        print(f"Hook payload: {_hook_payload(hook_json)}")
        print(f"Config:       {_config_path()}")
        print()
        print("[DRY-RUN] Run without --dry-run to install.")
        return

    config_path = _config_path()
    if config_path.exists():
        cfg = json.loads(config_path.read_text(encoding="utf-8"))
    else:
        cfg = {}
        print(f"Creating {config_path}")

    hooks = cfg.setdefault("hooks", {})
    events = _clean_event_groups(hooks.setdefault("events", {}))

    session_groups = events.setdefault("SessionStart", [])
    group = next(
        (g for g in session_groups if isinstance(g, dict) and g.get("matcher") == MATCHER),
        None,
    )
    if group is None:
        group = {"matcher": MATCHER, "hooks": []}
        session_groups.append(group)
    group["hooks"].append(hook_json)
    hooks["events"] = events

    # Configuration-file hooks are disabled by default in ZCode; without
    # enabled=true nothing runs. Never downgrade an existing true.
    hooks["enabled"] = True

    _write_json_atomic(config_path, cfg)

    print("Installed.")
    print(f"  Hook payload: {_hook_payload(hook_json)}")
    print(f"  Config:       {config_path}")
    print("  hooks.enabled set to true (required for configuration hooks).")
    print("  No /hooks approval needed (unlike Codex). Hook runs are logged")
    print("  in the ZCode log.")


if __name__ == "__main__":
    main()
