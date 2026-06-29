#!/usr/bin/env python3
"""Workflow checkpoint CLI — JSONL flat-file storage.

Usage:
  checkpoint.py list [--hook]
  checkpoint.py create <title>
  checkpoint.py pause <id> [--source-docs <path,...>] [--skill <name>]
  checkpoint.py close <id> [--yes]
  checkpoint.py migrate

Scope is auto-detected from CWD via .git upward lookup.
Use --scope-dir <path> to override (for testing).
"""

import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

HOME = Path.home()

RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# ── slugify_project_key (shared with cc-workflow / archived-memory-lifecycle) ──

def slugify_project_key(project_key: str) -> str:
    value = project_key.strip()
    if not value:
        raise ValueError("project_key must not be empty")
    return "-" + re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")


# ── Scope ───────────────────────────────────────────────────────────────────

def detect_scope_dir() -> Path:
    """Walk up from CWD looking for .git. Return workflows directory.
    .git in $HOME or ~/.claude/ is ignored (dotfiles, skill repos)."""
    claude = (HOME / ".claude").resolve()
    scope_dir = Path.cwd().resolve()
    home = HOME.resolve()

    while True:
        if (scope_dir / ".git").exists():
            if scope_dir == home:
                pass  # dotfiles repo at $HOME
            elif scope_dir == claude or claude in scope_dir.parents:
                pass  # inside ~/.claude/ — skill repo, not a project
            else:
                slug = slugify_project_key(str(scope_dir))
                return HOME / ".claude" / "projects" / slug / "workflows"
        parent = scope_dir.parent
        if parent == scope_dir:
            break  # reached filesystem root
        scope_dir = parent
    return HOME / ".claude" / "global" / "workflows"


def _resolve(args: Any) -> Path:
    if hasattr(args, "scope_dir") and args.scope_dir:
        return Path(args.scope_dir)
    return detect_scope_dir()


# ── ID Generation ───────────────────────────────────────────────────────────

def _title_to_slug(title: str) -> str:
    """Convert title to kebab-case slug, truncated to 32 chars."""
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:32]


def _generate_id(title: str, ts: Optional[datetime] = None) -> str:
    """Generate task id: yyyyMMdd-HHmmss-<title-slug>"""
    if ts is None:
        ts = datetime.now()
    date_part = ts.strftime("%Y%m%d-%H%M%S")
    slug = _title_to_slug(title)
    return f"{date_part}-{slug}"


def _parse_ts_from_id(task_id: str) -> datetime:
    """Extract created timestamp from id (yyyyMMdd-HHmmss-...)."""
    match = re.match(r"^(\d{8}-\d{6})-", task_id)
    if not match:
        raise ValueError(f"Cannot parse timestamp from id: {task_id}")
    return datetime.strptime(match.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


# ── JSONL I/O ───────────────────────────────────────────────────────────────

def _read_jsonl(wf_dir: Path) -> List[Dict]:
    """Read all records from workflows.jsonl. Returns list of dicts."""
    fp = wf_dir / "workflows.jsonl"
    if not fp.exists():
        return []
    records: List[Dict] = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _write_jsonl(wf_dir: Path, records: List[Dict]) -> None:
    """Write all records to workflows.jsonl (atomic: temp file + rename)."""
    wf_dir.mkdir(parents=True, exist_ok=True)
    lines = [json.dumps(r, ensure_ascii=False) + "\n" for r in records]
    content = "".join(lines)
    tmp = wf_dir / ".workflows.jsonl.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(wf_dir / "workflows.jsonl")


def _find_record(records: List[Dict], task_id: str) -> Tuple[int, Optional[Dict]]:
    """Find record by id. Returns (index, record) or (-1, None)."""
    for i, r in enumerate(records):
        if r["id"] == task_id:
            return i, r
    return -1, None


def _now_iso() -> str:
    """Current time as ISO 8601 with timezone."""
    return datetime.now(timezone.utc).isoformat()


# ── Recovery Template ───────────────────────────────────────────────────────

_TEMPLATE = """<!-- Write ALL sections in English. -->
## Completed
<!-- What was accomplished? Files, functions, key changes. Give enough detail for a fresh model to fully reconstruct context. -->


## Current
<!-- Where exactly are you stuck? What's in progress? What's the current state? -->


## Decisions
<!-- What did the user decide? Which approach? Why A over B? -->


## Next
<!-- One sentence. First action on resume. -->


## Key Files
<!-- Paths involved, one per line -->
"""


def _generate_md(wf_dir: Path, task_id: str) -> Path:
    """Write template <id>.md file."""
    md_path = wf_dir / f"{task_id}.md"
    md_path.write_text(_TEMPLATE, encoding="utf-8")
    return md_path


def _validate_md(md_path: Path) -> List[str]:
    """Validate recovery .md content. Returns list of error messages (empty = valid)."""
    if not md_path.exists():
        return [f"File not found: {md_path}"]

    content = md_path.read_text(encoding="utf-8")
    errors: List[str] = []

    # 5 section headers must all exist
    required_headers = ["## Completed", "## Current", "## Decisions", "## Next", "## Key Files"]
    for h in required_headers:
        if h not in content:
            errors.append(f"Missing section header: {h}")

    if errors:
        return errors

    # Extract section bodies (text between ## Header and next ## or EOF)
    def _section_body(text: str, header: str) -> str:
        idx = text.index(header) + len(header)
        rest = text[idx:]
        next_marker = re.search(r"\n## ", rest)
        if next_marker:
            return rest[:next_marker.start()].strip()
        return rest.strip()

    completed = _section_body(content, "## Completed")
    current = _section_body(content, "## Current")
    next_ = _section_body(content, "## Next")

    # Completed must have >= 100 non-whitespace chars
    completed_chars = len(re.sub(r"\s+", "", completed))
    if completed_chars < 100:
        errors.append(f"## Completed too short: {completed_chars} non-whitespace chars (need >= 100)")

    # Current must be non-empty
    if not current:
        errors.append("## Current must not be empty")

    # Next must be non-empty
    if not next_:
        errors.append("## Next must not be empty")

    return errors


# ── Source Docs Auto-Scan ────────────────────────────────────────────────────

def _doc_date(doc_path: Path) -> Optional[float]:
    """Extract YYYY-MM-DD from filename, return as timestamp or None."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", doc_path.name)
    if m:
        try:
            d = datetime.strptime(m.group(1), "%Y-%m-%d")
            return d.timestamp()
        except ValueError:
            pass
    return None


def _alpha_tokens(name: str) -> set:
    """Extract alpha-only lowercase tokens from a filename stem.
    e.g. '2026-06-29-workflow-checkpoint-v3-design.md' -> {'workflow','checkpoint','v','design'}"""
    base = Path(name).stem
    parts = re.split(r"[^a-zA-Z]+", base)
    return {p.lower() for p in parts if p and len(p) > 0}


def _scan_doc_candidates(
    wf_dir: Path,
    task_id: str,
    created_ts: datetime,
    updated_ts: datetime,
    project_root: Optional[Path] = None,
) -> List[str]:
    """Scan for source docs matching the task by time window + slug overlap.

    Scan roots:
      - global scope: ~/.claude/plans/
      - project scope: <project_root>/docs/superpowers/{plans,specs}/

    Dual filter:
      1. doc mtime must be within [created_ts.timestamp(), updated_ts.timestamp()]
      2. alpha tokens from doc filename must overlap with task title-slug alpha tokens
    """
    candidates: List[Path] = []

    # Determine scan roots
    if project_root:
        scan_roots = [
            project_root / "docs" / "superpowers" / "plans",
            project_root / "docs" / "superpowers" / "specs",
        ]
    else:
        scan_roots = [HOME / ".claude" / "plans"]

    # Collect .md files from scan roots
    for root in scan_roots:
        if not root.exists():
            continue
        for fp in root.rglob("*.md"):
            if fp.is_file():
                candidates.append(fp)

    # Extract title-slug tokens from task_id
    # task_id format: yyyyMMdd-HHmmss-<title-slug>
    parts = task_id.split("-", 2)  # ['20260629', '100510', 'compare-skills']
    title_slug = parts[2] if len(parts) > 2 else ""
    task_tokens = _alpha_tokens(title_slug)

    # Apply dual filter
    created_ts_float = created_ts.timestamp()
    updated_ts_float = updated_ts.timestamp()
    matched: List[str] = []

    for fp in candidates:
        # Filter 1: time window
        mtime = fp.stat().st_mtime
        if not (created_ts_float <= mtime <= updated_ts_float):
            # Also check filename date as auxiliary signal
            doc_d = _doc_date(fp)
            if doc_d is not None:
                # Filename date must be reasonably close to window (within 7 days)
                if abs(doc_d - created_ts_float) > 7 * 86400:
                    continue
            else:
                continue

        # Filter 2: slug overlap
        doc_tokens = _alpha_tokens(fp.name)
        if not task_tokens:
            # No title-slug tokens to match against — include if time matches
            matched.append(str(fp))
        elif task_tokens & doc_tokens:
            matched.append(str(fp))

    return matched


# ── Heat ────────────────────────────────────────────────────────────────────

def _heat_from_record(record: Dict) -> float:
    """Calculate heat (days since updated)."""
    updated = datetime.fromisoformat(record["updated"])
    seconds = (datetime.now(timezone.utc) - updated).total_seconds()
    days = seconds / 86400
    return round(max(0, days), 1)


def _color(heat: float) -> str:
    if heat >= 14:
        return RED
    if heat >= 7:
        return YELLOW
    return ""


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_list(wf_dir: Path, args: Any) -> None:
    records = _read_jsonl(wf_dir)

    # --hook: output SessionStart JSON to stdout
    if getattr(args, "hook", False):
        if not records:
            print(json.dumps({"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ""}}))
            return
        parts = []
        for r in records:
            h = _heat_from_record(r)
            age = round(h)
            parts.append(f"{r['id']} ({r['title']}, {age}d)")
        ctx = f"{len(records)} pending task(s): " + ", ".join(parts) + "."
        output = {"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": ctx}}
        print(json.dumps(output))
        return

    if not records:
        print("No tasks.")
        return

    entries = [(r, _heat_from_record(r)) for r in records]
    entries.sort(key=lambda x: (-x[1], x[0]["id"]))

    print(f"Tasks ({len(entries)})")
    for r, h in entries:
        c = _color(h)
        age = round(h)
        line = f"  {r['id']} — {r['title']}  (heat={h}, {age}d)"
        if c:
            line = c + line + RESET
        print(line)


def cmd_create(wf_dir: Path, args: Any) -> None:
    title: str = args.title
    task_id = _generate_id(title)
    now = _now_iso()

    # Check for duplicate id
    records = _read_jsonl(wf_dir)
    _, existing = _find_record(records, task_id)
    if existing:
        print(f"Task id '{task_id}' already exists.", file=sys.stderr)
        sys.exit(1)

    # Resolve scope for source-doc scanning
    project_root: Optional[Path] = None
    scope_path = wf_dir.resolve()
    if "global" not in str(scope_path):
        cwd = Path.cwd().resolve()
        probe = cwd
        while True:
            if (probe / ".git").exists():
                project_root = probe
                break
            parent = probe.parent
            if parent == probe:
                break
            probe = parent

    # Parse timestamps for source-doc scan
    created_ts = _parse_ts_from_id(task_id)
    updated_ts = datetime.fromisoformat(now)

    # Auto-scan source docs
    candidates = _scan_doc_candidates(wf_dir, task_id, created_ts, updated_ts, project_root)

    # Build record
    record = {
        "id": task_id,
        "title": title,
        "created": now,
        "updated": now,
        "skill": None,
        "source_docs": [],
    }

    records.append(record)
    _write_jsonl(wf_dir, records)

    # Generate .md template
    md_path = _generate_md(wf_dir, task_id)

    print(f"Created {task_id}")
    print(f"  title: {title}")
    print(f"  md: {md_path}")
    if candidates:
        print(f"  source-doc candidates:")
        for c in candidates:
            print(f"    {c}")
    else:
        print(f"  source-doc candidates: (none)")


def cmd_pause(wf_dir: Path, args: Any) -> None:
    task_id: str = args.id
    records = _read_jsonl(wf_dir)
    idx, record = _find_record(records, task_id)
    if record is None:
        print(f"Task '{task_id}' not found.", file=sys.stderr)
        sys.exit(1)

    # Validate .md
    md_path = wf_dir / f"{task_id}.md"
    errors = _validate_md(md_path)
    if errors:
        print("Validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Update record
    now = _now_iso()
    record["updated"] = now

    # Update skill if provided
    if getattr(args, "skill", None):
        record["skill"] = args.skill

    # Merge source_docs: existing + manual --source-docs + auto-scan
    existing_docs = set(record.get("source_docs", []))
    if getattr(args, "source_docs", None):
        for p in args.source_docs.split(","):
            p = p.strip()
            if p:
                existing_docs.add(p)

    # Auto-scan for new docs
    project_root: Optional[Path] = None
    cwd = Path.cwd().resolve()
    probe = cwd
    while True:
        if (probe / ".git").exists():
            project_root = probe
            break
        parent = probe.parent
        if parent == probe:
            break
        probe = parent

    created_ts = _parse_ts_from_id(task_id)
    updated_ts = datetime.fromisoformat(now)
    candidates = _scan_doc_candidates(wf_dir, task_id, created_ts, updated_ts, project_root)
    for c in candidates:
        existing_docs.add(c)

    record["source_docs"] = sorted(existing_docs)

    _write_jsonl(wf_dir, records)
    print(f"Paused {task_id}")
    print(f"  updated: {now}")
    if record["skill"]:
        print(f"  skill: {record['skill']}")
    if record["source_docs"]:
        print(f"  source_docs:")
        for d in record["source_docs"]:
            print(f"    {d}")


def cmd_close(wf_dir: Path, args: Any) -> None:
    task_id: str = args.id
    records = _read_jsonl(wf_dir)
    idx, record = _find_record(records, task_id)
    if record is None:
        print(f"Task '{task_id}' not found.", file=sys.stderr)
        sys.exit(1)

    md_path = wf_dir / f"{task_id}.md"

    # Dry-run
    if not args.yes:
        print(f"Task: {task_id}")
        print(f"Title: {record['title']}")
        print(f"Created: {record['created']}")
        print(f"Updated: {record['updated']}")
        if record.get("source_docs"):
            print(f"Source docs to delete:")
            for d in record["source_docs"]:
                print(f"  {d}")
        else:
            print("Source docs: (none)")
        print(f"\nFiles to delete:")
        print(f"  {md_path} (.md recovery)")
        print(f"  workflows.jsonl line for {task_id}")
        for d in record.get("source_docs", []):
            print(f"  {d}")
        print("\n提醒: 如有可复用的技术知识请先通过 memory skill 沉淀。")
        print("\nRun `close <id> --yes` to execute deletion.")
        return

    # Validate .md before deleting (same rules as pause)
    errors = _validate_md(md_path)
    if errors:
        print("Validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    # Execute deletion
    # 1. Delete .md file
    md_path.unlink()
    print(f"  DELETED: {md_path}")

    # 2. Delete source_docs (only docs/superpowers/ paths)
    for doc_path in record.get("source_docs", []):
        p = Path(doc_path)
        if not p.is_absolute():
            project_root: Optional[Path] = None
            cwd = Path.cwd().resolve()
            probe = cwd
            while True:
                if (probe / ".git").exists():
                    project_root = probe
                    break
                parent = probe.parent
                if parent == probe:
                    break
                probe = parent
            if project_root:
                p = project_root / p
            else:
                p = HOME / ".claude" / p
        if p.exists() and "docs/superpowers" in str(p):
            p.unlink()
            print(f"  DELETED: {p}")
        elif p.exists():
            print(f"  SKIPPED (outside docs/superpowers/): {p}")

    # 3. Remove record from jsonl
    del records[idx]
    _write_jsonl(wf_dir, records)

    print(f"Closed {task_id}")


def cmd_migrate(wf_dir: Path) -> None:
    """Migrate v2 <slug>/ subdirectory data to v3 flat-file format."""
    if not wf_dir.exists():
        print("No v2 workflows directory found.")
        return

    # Check if v3 data already exists
    if (wf_dir / "workflows.jsonl").exists():
        print("workflows.jsonl already exists. Migration may have already run.", file=sys.stderr)
        print("Delete workflows.jsonl first if you want to re-migrate.", file=sys.stderr)
        sys.exit(1)

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
        ts_str = prog.get("ts", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
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

        md_content = f"## Completed\n\n{original}\n\n## Current\n\n(see Completed)\n\n## Decisions\n\n\n## Next\n\n\n## Key Files\n"
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


# ── CLI ─────────────────────────────────────────────────────────────────────

def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="checkpoint.py", description="Workflow checkpoint CLI")
    p.add_argument("--scope-dir", type=str, help="Override auto-detected directory")

    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("list", help="List pending tasks (sorted by heat)")
    sp.add_argument("--hook", action="store_true", help="Output SessionStart JSON for hook consumption")

    sp = sub.add_parser("create", help="Create a new task")
    sp.add_argument("title", type=str, help="Human-readable task title")

    sp = sub.add_parser("pause", help="Validate .md and refresh updated timestamp")
    sp.add_argument("id", type=str, help="Task id (yyyyMMdd-HHmmss-slug)")
    sp.add_argument("--source-docs", type=str, help="Comma-separated additional source doc paths")
    sp.add_argument("--skill", type=str, help="Skill name to load on resume")

    sp = sub.add_parser("close", help="Close a task (dry-run by default)")
    sp.add_argument("id", type=str, help="Task id")
    sp.add_argument("--yes", action="store_true", help="Execute deletion")

    sp = sub.add_parser("migrate", help="Migrate v2 <slug>/ subdirectories to v3 flat files")

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return

    wf_dir = _resolve(args)

    if args.command == "list":
        cmd_list(wf_dir, args)
    elif args.command == "create":
        cmd_create(wf_dir, args)
    elif args.command == "pause":
        cmd_pause(wf_dir, args)
    elif args.command == "close":
        cmd_close(wf_dir, args)
    elif args.command == "migrate":
        cmd_migrate(wf_dir)


if __name__ == "__main__":
    main()
