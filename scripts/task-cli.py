#!/usr/bin/env python3
"""Task workflow CLI — per-task directory storage with JSON index.

Usage:
  task-cli.py list [--json]
  task-cli.py create <slug> <description>
  task-cli.py pause <slug> [--step "<text>"] [--next "<text>"] [--plan "<path>"]
  task-cli.py resume <slug>
  task-cli.py close <slug> [--yes]
  task-cli.py sync

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

HOME = Path.home()

SLUG_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")

RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"


# ── Scope ───────────────────────────────────────────────────────────────────

def detect_scope_dir():
    """Walk up from CWD looking for .git. Return (workflows_dir, is_project)."""
    scope_dir = Path.cwd().resolve()
    home = HOME.resolve()

    while True:
        if (scope_dir / ".git").exists():
            if scope_dir == home or home not in scope_dir.parents:
                pass  # dotfiles repo — ignore
            else:
                slug = str(scope_dir).replace(":\\", "--").replace("\\", "-").replace("/", "-").replace(":", "-")
                return HOME / ".claude" / "projects" / slug / "workflows"
        parent = scope_dir.parent
        if parent == scope_dir or scope_dir == home:
            break
        scope_dir = parent
    return HOME / ".claude" / "global" / "workflows"


def _resolve(args):
    if hasattr(args, "scope_dir") and args.scope_dir:
        return Path(args.scope_dir)
    return detect_scope_dir()


# ── JSON I/O ────────────────────────────────────────────────────────────────

def _read_index(wf_dir):
    """Read index.json. Returns list of task dicts."""
    fp = wf_dir / "index.json"
    if not fp.exists():
        return []
    return json.loads(fp.read_text(encoding="utf-8"))


def _write_index(wf_dir, tasks):
    wf_dir.mkdir(parents=True, exist_ok=True)
    (wf_dir / "index.json").write_text(
        json.dumps(tasks, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_progress(wf_dir, slug):
    fp = wf_dir / slug / "progress.json"
    if not fp.exists():
        return None
    return json.loads(fp.read_text(encoding="utf-8"))


def _write_progress(wf_dir, slug, data):
    d = wf_dir / slug
    d.mkdir(parents=True, exist_ok=True)
    data["ts"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    (d / "progress.json").write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _rebuild_index(wf_dir):
    """Rebuild index.json from all progress.json files."""
    tasks = []
    if not wf_dir.exists():
        return tasks
    for d in sorted(wf_dir.iterdir()):
        if not d.is_dir():
            continue
        pf = d / "progress.json"
        if not pf.exists():
            continue
        prog = json.loads(pf.read_text(encoding="utf-8"))
        tasks.append({
            "slug": d.name,
            "description": prog.get("description", ""),
            "skill": prog.get("skill"),
            "event": prog.get("event", "active"),
        })
    _write_index(wf_dir, tasks)
    return tasks


# ── Heat ────────────────────────────────────────────────────────────────────

def _heat(wf_dir, slug):
    pf = wf_dir / slug / "progress.json"
    if not pf.exists():
        return 0
    days = (time.time() - pf.stat().st_mtime) / 86400
    return round(max(0, days), 1)


def _color(heat):
    if heat >= 14:
        return RED
    if heat >= 7:
        return YELLOW
    return ""


# ── Commands ────────────────────────────────────────────────────────────────

def cmd_list(wf_dir, args):
    tasks = _read_index(wf_dir)
    if getattr(args, "json", False):
        result = []
        for t in tasks:
            h = _heat(wf_dir, t["slug"])
            result.append({**t, "heat": h})
        print(json.dumps(result, indent=2))
        return
    if not tasks:
        print("No tasks.")
        return
    entries = [(t, _heat(wf_dir, t["slug"])) for t in tasks]
    entries.sort(key=lambda x: (-x[1], x[0]["slug"]))
    print(f"Tasks ({len(entries)})")
    for t, h in entries:
        state = t["event"]
        c = _color(h)
        age = round(h)
        line = f"  [{state}] {t['slug']} — {t['description']}  (heat={h}, {age}d)"
        if c:
            line = c + line + RESET
        print(line)


def cmd_create(wf_dir, args):
    slug, desc = args.slug, args.desc
    if not SLUG_RE.match(slug):
        print("Slug must be kebab-case.", file=sys.stderr); sys.exit(1)
    d = wf_dir / slug
    if d.exists():
        print(f"Task '{slug}' already exists.", file=sys.stderr); sys.exit(1)

    # Determine state: if step/next provided → paused immediately (first-pause)
    step = getattr(args, "step", None)
    is_paused = step or getattr(args, "next", None)
    prog = {"ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "event": "paused" if is_paused else "active",
            "description": desc, "skill": None,
            "step": step or "", "next": getattr(args, "next", None) or "",
            "source_plan": getattr(args, "plan", None) or ""}
    _write_progress(wf_dir, slug, prog)
    (d / "recovery.md").write_text(
        "## Recovery\nTask just created. Details will be filled on first pause.\n",
        encoding="utf-8")
    _rebuild_index(wf_dir)
    print(f"Created {d}")


def cmd_pause(wf_dir, args):
    slug = args.slug
    prog = _read_progress(wf_dir, slug)
    if prog is None:
        print(f"Task '{slug}' not found.", file=sys.stderr); sys.exit(1)
    if prog["event"] == "paused":
        print("Already paused."); return
    prog["event"] = "paused"
    if getattr(args, "step", None):
        prog["step"] = args.step
    if getattr(args, "next", None):
        prog["next"] = args.next
    if getattr(args, "plan", None):
        prog["source_plan"] = args.plan
    _write_progress(wf_dir, slug, prog)
    _rebuild_index(wf_dir)
    print(f"Paused {wf_dir / slug}")


def cmd_resume(wf_dir, args):
    slug = args.slug
    prog = _read_progress(wf_dir, slug)
    if prog is None:
        print(f"Task '{slug}' not found.", file=sys.stderr); sys.exit(1)
    if prog["event"] == "active":
        print("Already in progress."); return
    prog["event"] = "active"
    _write_progress(wf_dir, slug, prog)
    _rebuild_index(wf_dir)
    print(f"Resumed {wf_dir / slug}")


def cmd_close(wf_dir, args):
    slug = args.slug
    prog = _read_progress(wf_dir, slug)
    if prog is None:
        print(f"Task '{slug}' not found.", file=sys.stderr); sys.exit(1)

    # Dry-run
    if not args.yes:
        print(f"Task: {slug}")
        print(f"Status: {prog['event']}")
        plan = prog.get("source_plan", "")
        if plan:
            ref = Path(plan).expanduser()
            print(f"Source plan: {plan} {'(exists)' if ref.exists() else '(missing)'}")
        print("\nRun `close <slug> --yes` to complete and clean up.")
        return

    # Delete referenced spec
    plan = prog.get("source_plan", "")
    if plan:
        ref = Path(plan).expanduser()
        if ref.exists() and (".claude/specs/" in str(ref) or ref.name.endswith(("-plan.md", "-design.md"))):
            ref.unlink()
            print(f"  DELETED: {ref}")

    # Delete task directory
    shutil.rmtree(wf_dir / slug)
    _rebuild_index(wf_dir)
    print(f"Closed {wf_dir / slug}")


# ── CLI ─────────────────────────────────────────────────────────────────────

def main():
    import argparse
    p = argparse.ArgumentParser(prog="task-cli.py", description="Task workflow CLI")
    p.add_argument("--scope-dir", type=str, help="Override auto-detected directory")

    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("list", help="List tasks")
    sp.add_argument("--json", action="store_true")

    sp = sub.add_parser("create", help="Create a task (add --step to create paused)")
    sp.add_argument("slug", type=str)
    sp.add_argument("desc", type=str)
    sp.add_argument("--step", type=str)
    sp.add_argument("--next", type=str)
    sp.add_argument("--plan", type=str)

    sp = sub.add_parser("pause", help="Pause a task")
    sp.add_argument("slug", type=str)
    sp.add_argument("--step", type=str)
    sp.add_argument("--next", type=str)
    sp.add_argument("--plan", type=str)

    sp = sub.add_parser("resume", help="Resume a paused task")
    sp.add_argument("slug", type=str)

    sp = sub.add_parser("close", help="Close a task (dry-run by default)")
    sp.add_argument("slug", type=str)
    sp.add_argument("--yes", action="store_true")

    sp = sub.add_parser("sync", help="Rebuild index.json")

    args = p.parse_args()
    if not args.command:
        p.print_help(); return

    wf_dir = _resolve(args)

    if args.command == "list":
        cmd_list(wf_dir, args)
    elif args.command == "create":
        cmd_create(wf_dir, args)
    elif args.command == "pause":
        cmd_pause(wf_dir, args)
    elif args.command == "resume":
        cmd_resume(wf_dir, args)
    elif args.command == "close":
        cmd_close(wf_dir, args)
    elif args.command == "sync":
        _rebuild_index(wf_dir)
        print(f"index.json rebuilt ({len(_read_index(wf_dir))} tasks)")


if __name__ == "__main__":
    main()
