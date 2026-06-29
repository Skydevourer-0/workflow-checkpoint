#!/usr/bin/env python3
"""One-shot migration from v2 per-slug subdirectories to v3 flat-file format.

Usage:
  python migrate_v2.py [--scope-dir <path>]
"""

import argparse
import json
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

# Import helpers from the main checkpoint module
sys.path.insert(0, str(Path(__file__).resolve().parent))
from checkpoint import (  # noqa: E402
    _generate_id,
    _read_jsonl,
    _resolve,
    _write_jsonl,
)


def cmd_migrate(wf_dir: Path) -> int:
    """Migrate v2 <slug>/ subdirectory data to v3 flat-file format.
    Returns number of tasks migrated (0 = none, >0 = success, no special meaning)."""
    if not wf_dir.exists():
        print("No v2 workflows directory found.")
        return 0

    # Check if v3 data already exists
    if (wf_dir / "workflows.jsonl").exists():
        print(
            "workflows.jsonl already exists. Migration may have already run.",
            file=sys.stderr,
        )
        print(
            "Delete workflows.jsonl first if you want to re-migrate.",
            file=sys.stderr,
        )
        return 1

    # Find v2 subdirectories (those containing progress.json)
    migrated = 0
    backup_dir = wf_dir / "_v2_backup"
    backup_dir.mkdir(exist_ok=True)

    for d in sorted(wf_dir.iterdir()):
        if not d.is_dir() or d.name.startswith("_"):
            continue
        progress_file = d / "progress.json"
        if not progress_file.exists():
            continue

        # Read v2 data
        prog = json.loads(progress_file.read_text(encoding="utf-8"))
        slug = d.name
        title = prog.get("description", slug)
        skill = prog.get("skill")
        ts_str = prog.get(
            "ts",
            datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )
        # Handle Python 3.8 incompatibility with Z suffix
        ts_str = ts_str.replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(ts_str)
        except ValueError:
            ts = datetime.now(timezone.utc)

        # Generate v3 id
        task_id = _generate_id(title, ts)

        # Build v3 record
        record = {
            "id": task_id,
            "title": title,
            "created": ts.isoformat(),
            "updated": ts.isoformat(),
            "skill": skill,
            "source_docs": [prog["source_plan"]] if prog.get("source_plan") else [],
        }

        # Read recovery.md and wrap in v3 template
        recovery_file = d / "recovery.md"
        if recovery_file.exists():
            original = recovery_file.read_text(encoding="utf-8")
        else:
            original = "Task migrated from v2."

        md_content = (
            f"## Completed\n\n{original}\n\n"
            f"## Current\n\n(see Completed)\n\n"
            f"## Decisions\n\n\n## Next\n\n\n## Key Files\n"
        )
        (wf_dir / f"{task_id}.md").write_text(md_content, encoding="utf-8")

        # Write to jsonl
        records = _read_jsonl(wf_dir)
        records.append(record)
        _write_jsonl(wf_dir, records)

        # Move old directory to backup
        shutil.move(str(d), str(backup_dir / d.name))
        migrated += 1
        print(f"  Migrated: {slug} -> {task_id}")

    if migrated == 0:
        print("No v2 tasks found to migrate.")
    else:
        print(f"\nMigrated {migrated} task(s). Old data moved to {backup_dir}")
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        prog="migrate_v2.py",
        description="Migrate v2 workflow data to v3 flat-file format",
    )
    p.add_argument(
        "--scope-dir",
        type=str,
        help="Override auto-detected directory",
    )
    args = p.parse_args()

    wf_dir = Path(args.scope_dir) if args.scope_dir else _resolve(args)
    sys.exit(cmd_migrate(wf_dir))


if __name__ == "__main__":
    main()
