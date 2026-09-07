#!/usr/bin/env python3
"""Workflow checkpoint CLI — JSONL flat-file storage.

Usage:
  checkpoint.py list [--hook]
  checkpoint.py create <title> --note <context>
  checkpoint.py pause <id> [--source-docs <path,...>] [--skill <name>]
  checkpoint.py close <id> [--yes]

Scope is auto-detected from CWD via .git upward lookup.
Use --scope-dir <path> to override (for testing).
"""

import json
import os
import re
import shutil
import subprocess
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple, Union

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

HOME = Path.home()

# Data root (SSOT layout shared with memory-lifecycle):
#   ~/.cc-switch/workflows/global/           (no project .git found)
#   ~/.cc-switch/workflows/projects/<slug>/  (project .git found)
WORKFLOWS_ROOT = HOME / ".cc-switch" / "workflows"

# Path-boundary special cases: dotfiles/skill roots, never a project scope.
CLAUDE_DIR = (HOME / ".claude").resolve()
CC_SWITCH_SKILLS = (HOME / ".cc-switch" / "skills").resolve()

RED = "\033[91m"
YELLOW = "\033[93m"
RESET = "\033[0m"

# Hook additionalContext budget (shared spec with memory-lifecycle HOTLIST).
HOOK_BUDGET = 1200


# ── Slug (unified spec, shared with memory-lifecycle) ───────────────────────

def project_slug(path: Union[str, Path]) -> str:
    """Project slug: realpath -> lowercase -> non-[a-z0-9] -> '-' -> collapse.

    Shared spec with memory-lifecycle (no leading dash, lowercase):
      C:\\Users\\a\\proj  ->  c-users-a-proj
    """
    value = os.path.realpath(str(path)).lower()
    value = re.sub(r"[^a-z0-9]", "-", value)
    value = re.sub(r"-+", "-", value)
    return value


# ── Scope ───────────────────────────────────────────────────────────────────

def _is_within(child: Path, parent: Path) -> bool:
    """Boundary-safe containment: True when child == parent or under parent."""
    child_s = os.path.normcase(str(child))
    parent_s = os.path.normcase(str(parent))
    if child_s == parent_s:
        return True
    return child_s.startswith(parent_s.rstrip("/\\") + os.sep)


def _is_global_path(probe: Path) -> bool:
    """Global-scope roots: $HOME, ~/.claude/, ~/.cc-switch/skills/ (boundaries)."""
    return (
        os.path.normcase(str(probe)) == os.path.normcase(str(HOME))
        or _is_within(probe, CLAUDE_DIR)
        or _is_within(probe, CC_SWITCH_SKILLS)
    )


def _has_git_marker(path: Path) -> bool:
    """True when path/.git is a git dir, or a gitdir-pointer file
    (worktree/submodule). The pointer target must exist."""
    git_path = path / ".git"
    if git_path.is_dir():
        return True
    if git_path.is_file():
        try:
            content = git_path.read_text(encoding="utf-8")
        except OSError:
            return False
        m = re.search(r"(?m)^gitdir:\s*(.+?)\s*$", content)
        if not m:
            return False
        target = Path(m.group(1))
        if not target.is_absolute():
            target = path / target
        return target.exists()
    return False


def detect_scope_dir() -> Path:
    """Walk up from CWD looking for a project git root. Return workflows dir.

    Global scope: no project .git found, or inside $HOME / ~/.claude/ /
    ~/.cc-switch/skills/ (dotfiles, skill repos). Project scope: nearest git
    root — .git dir or worktree/submodule .git file.
    """
    probe = Path.cwd().resolve()
    while True:
        if _has_git_marker(probe) and not _is_global_path(probe):
            return WORKFLOWS_ROOT / "projects" / project_slug(probe)
        parent = probe.parent
        if parent == probe:
            break  # reached filesystem root
        probe = parent
    return WORKFLOWS_ROOT / "global"


def _resolve(args: Any) -> Path:
    if hasattr(args, "scope_dir") and args.scope_dir:
        return Path(args.scope_dir)
    return detect_scope_dir()


def _find_project_root() -> Optional[Path]:
    """Nearest git root (dir or worktree .git file), same guard rules as
    detect_scope_dir. Returns None for global scope."""
    probe = Path.cwd().resolve()
    while True:
        if _has_git_marker(probe) and not _is_global_path(probe):
            return probe
        parent = probe.parent
        if parent == probe:
            break
        probe = parent
    return None


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


def _archive_path(wf_dir: Path) -> Path:
    """Path to the archive JSONL (closed tasks)."""
    return wf_dir / "archive.jsonl"


def _archived_md_dir(wf_dir: Path) -> Path:
    """Directory holding archived .md recovery files."""
    return wf_dir / "archived"


def _read_archive(wf_dir: Path) -> List[Dict]:
    """Read all closed records from archive.jsonl."""
    fp = _archive_path(wf_dir)
    if not fp.exists():
        return []
    records: List[Dict] = []
    for line in fp.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            records.append(json.loads(line))
    return records


def _append_archive(wf_dir: Path, record: Dict) -> None:
    """Append a single closed record to archive.jsonl (atomic: temp + rename)."""
    wf_dir.mkdir(parents=True, exist_ok=True)
    fp = _archive_path(wf_dir)
    existing = _read_archive(wf_dir)
    existing.append(record)
    lines = [json.dumps(r, ensure_ascii=False) + "\n" for r in existing]
    tmp = wf_dir / ".archive.jsonl.tmp"
    tmp.write_text("".join(lines), encoding="utf-8")
    tmp.replace(fp)


def _now_iso() -> str:
    """Current time as ISO 8601 with timezone."""
    return datetime.now(timezone.utc).isoformat()


# ── .md read (CRLF normalization) ───────────────────────────────────────────

def _read_md(path: Path) -> str:
    """Read a recovery .md, normalizing CRLF -> LF for stable offsets.

    Windows editors introduce CRLF; archive-stream's --range line offsets
    (_line_start) and distill's Evidence-block removal both assume LF.
    """
    return path.read_text(encoding="utf-8").replace("\r\n", "\n")


# ── Cross-process file lock ─────────────────────────────────────────────────

def _lock_path(wf_dir: Path) -> Path:
    """Path to the per-scope advisory lock file."""
    return wf_dir / ".lock"


@contextmanager
def _with_lock(wf_dir: Path):
    """Exclusive advisory lock around a read-modify-write critical section.

    POSIX: fcntl.flock(LOCK_EX). Windows: msvcrt.locking on byte 0 of a file
    opened 'a+b'. If neither is available, fail-open with a warning
    (single-user local, best-effort). The lock file is never deleted.
    """
    wf_dir.mkdir(parents=True, exist_ok=True)
    lp = _lock_path(wf_dir)
    fh = open(lp, "a+b")
    locked = False
    try:
        if fcntl is not None:
            fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
            locked = True
        elif msvcrt is not None:
            if os.path.getsize(lp) == 0:
                fh.write(b"\0")
                fh.flush()
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_LOCK, 1)
            locked = True
        else:
            print("! warning: no file locking available; proceeding unlocked (best-effort)", file=sys.stderr)
        yield
    finally:
        if locked:
            try:
                if fcntl is not None:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
                elif msvcrt is not None:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            except OSError:
                pass
        fh.close()


# ── Ledger (evidence) I/O ───────────────────────────────────────────────────

def _ledger_path(wf_dir: Path) -> Path:
    """Path to the per-scope evidence ledger."""
    return wf_dir / "ledger.jsonl"


def _read_ledger_raw(wf_dir: Path) -> List[str]:
    """Read ledger.jsonl lines verbatim (including malformed lines)."""
    fp = _ledger_path(wf_dir)
    if not fp.exists():
        return []
    return fp.read_text(encoding="utf-8").splitlines()


def _read_ledger(wf_dir: Path) -> List[Dict]:
    """Lenient parse of ledger.jsonl: skip + warn on malformed lines."""
    records: List[Dict] = []
    for line in _read_ledger_raw(wf_dir):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as exc:
            print(f"! warning: skipping malformed ledger line ({exc}): {line[:60]}...", file=sys.stderr)
    return records


def _write_ledger_lines(wf_dir: Path, lines: List[str]) -> None:
    """Atomic write of raw ledger lines (tmp + replace)."""
    wf_dir.mkdir(parents=True, exist_ok=True)
    tmp = wf_dir / ".ledger.jsonl.tmp"
    tmp.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    tmp.replace(_ledger_path(wf_dir))


def _scope_from_dir(wf_dir: Path) -> str:
    """Derive the scope string from a workflows dir path."""
    if wf_dir.resolve() == (WORKFLOWS_ROOT / "global").resolve():
        return "global"
    return f"projects/{wf_dir.name}"


def _all_ledger_dirs() -> List[Path]:
    """All per-scope dirs to aggregate for report/ledger list."""
    dirs = [WORKFLOWS_ROOT / "global"]
    projects = WORKFLOWS_ROOT / "projects"
    if projects.is_dir():
        dirs.extend(sorted(p for p in projects.iterdir() if p.is_dir()))
    return dirs


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


def _generate_md(wf_dir: Path, task_id: str, note: str) -> Path:
    """Write recovery .md with note seeded into ## Current."""
    # Seed note into ## Current; use repr() to avoid injection but keep readable
    lines = _TEMPLATE.splitlines()
    out: List[str] = []
    in_current = False
    for line in lines:
        if line.startswith("## Current"):
            out.append(line)
            out.append("")  # blank after header
            # Wrap the note in an initial stream marker pair so streams are
            # first-class from t=0 (docs/spec-archive-stream.md §1.3).
            out.append("<!-- stream:start:initial -->")
            out.append(note)
            out.append("<!-- stream:end:initial -->")
            out.append("")
            in_current = True
        elif in_current and line.startswith("<!--"):
            continue  # skip the template comment for Current
        elif in_current and line.startswith("## "):
            in_current = False
            out.append(line)
        elif not in_current:
            out.append(line)
    md_path = wf_dir / f"{task_id}.md"
    md_path.write_text("\n".join(out), encoding="utf-8")
    return md_path


def _validate_markers(content: str, task_id: str) -> List[str]:
    """Validate stream markers in RAW .md content (before comment stripping).

    Returns a list of error messages (empty = valid). Markers are HTML comments
    of the form `<!-- stream:start:<name> -->` / `<!-- stream:end:<name> -->`.
    See docs/spec-archive-stream.md §2.2 for the 4 assertions.

    Runs on RAW content (callers must invoke before the L246 comment-strip);
    markers are HTML comments and would be stripped otherwise.
    """
    errors: List[str] = []
    suffix = f"in {task_id}.md — edit {task_id}.md"

    # Split out fenced code blocks so literal markers inside ``` are ignored.
    MARKER_RE = re.compile(r"<!-- stream:(start|end):([a-z0-9-]+) -->")
    HEADER_RE = re.compile(r"^## ", re.MULTILINE)

    # Build a list of (line_index, kind, name, section) for markers outside
    # fenced code blocks, tracking which ## section each marker lives in.
    in_fence = False
    current_section: Optional[str] = None
    markers: List[Tuple[int, str, str, Optional[str]]] = []  # (line_no, kind, name, section)
    for line_no, line in enumerate(content.splitlines()):
        stripped = line.strip()
        if stripped.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        if stripped.startswith("## "):
            # `## Evidence: <stream>` blocks are transparent to marker
            # section-tracking (they live at end-of-file; see spec §2.3).
            if not stripped.startswith("## Evidence:"):
                current_section = stripped
            continue
        m = MARKER_RE.search(line)
        if m:
            markers.append((line_no, m.group(1), m.group(2), current_section))

    ALLOWED_SECTIONS = {"## Current", "## Next"}

    starts: Dict[str, List[Tuple[int, Optional[str]]]] = {}
    ends: Dict[str, List[Tuple[int, Optional[str]]]] = {}
    for _ln, kind, name, section in markers:
        if section not in ALLOWED_SECTIONS:
            errors.append(
                f"Malformed stream marker: marker outside ## Current/## Next {suffix} "
                f"to move it into Current/Next, re-run pause"
            )
            # still record it so pairing checks can run
        if kind == "start":
            starts.setdefault(name, []).append((_ln, section))
        else:
            ends.setdefault(name, []).append((_ln, section))

    # 1. Balanced pairs by name + 2. section-local pairing + 4. non-empty body.
    matched_ends: set = set()  # (name, end_line_no) consumed
    # For each start, find a matching end with the same name AFTER it.
    # Track section membership for the cross-section check.
    for name, start_list in starts.items():
        if name not in ends:
            for _s_ln, _s_sec in start_list:
                errors.append(
                    f"Malformed stream marker: start:{name} has no matching end:{name} {suffix} "
                    f"to pair markers, re-run pause"
                )
            continue
        end_list = ends[name]
        for s_ln, s_sec in start_list:
            # Find the earliest unmatched end after this start.
            match = None
            for e_entry in end_list:
                e_ln, e_sec = e_entry
                if e_ln > s_ln and (name, e_ln) not in matched_ends:
                    match = e_entry
                    break
            if match is None:
                errors.append(
                    f"Malformed stream marker: start:{name} has no matching end:{name} {suffix} "
                    f"to pair markers, re-run pause"
                )
                continue
            e_ln, e_sec = match
            matched_ends.add((name, e_ln))
            # Section-local pairing: start and end must be in the SAME section.
            if s_sec != e_sec:
                errors.append(
                    f"Malformed stream marker: start:{name}/end:{name} cross a section boundary {suffix} "
                    f"to keep the pair inside one section, re-run pause"
                )
            # Non-empty body: text between start and end lines (exclusive).
            lines = content.splitlines()
            body_lines = lines[s_ln + 1:e_ln]
            if not "".join(body_lines).strip():
                errors.append(
                    f"Malformed stream marker: empty body for start:{name} {suffix} "
                    f"to add body or remove the pair, re-run pause"
                )

    # 3. Unique names: no duplicate start:<name>.
    for name, start_list in starts.items():
        if len(start_list) > 1:
            errors.append(
                f"Malformed stream marker: duplicate start:{name} {suffix} "
                f"to use unique names, re-run pause"
            )

    # Orphan ends (end with no matching start) — report as unbalanced.
    for name, end_list in ends.items():
        consumed = sum(1 for (n, e_ln) in matched_ends if n == name)
        if len(end_list) > consumed:
            errors.append(
                f"Malformed stream marker: end:{name} has no matching start:{name} {suffix} "
                f"to pair markers, re-run pause"
            )

    return errors


def _section_body(text: str, header: str) -> str:
    """Extract section body: text between ## Header and the next ## or EOF.

    Returns "" if the header is absent (does not raise), so module-level callers
    (archive-stream's emptiness guard, _audit_md) are crash-safe on files missing
    a section.
    """
    try:
        idx = text.index(header) + len(header)
    except ValueError:
        return ""
    rest = text[idx:]
    next_marker = re.search(r"\n## ", rest)
    if next_marker:
        return rest[:next_marker.start()].strip()
    return rest.strip()


def _validate_md(md_path: Path, task_id: Optional[str] = None) -> List[str]:
    """Validate recovery .md content. Returns list of error messages (empty = valid)."""
    if not md_path.exists():
        return [f"File not found: {md_path}"]

    content = _read_md(md_path)

    # Marker assertions run on RAW content (before the comment strip below),
    # because markers are HTML comments and would be stripped away.
    task_id = task_id or md_path.stem
    errors = _validate_markers(content, task_id)

    # Strip HTML comments before validation so template scaffolding
    # doesn't count toward content minima
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

    # 5 section headers must all exist
    required_headers = ["## Completed", "## Current", "## Decisions", "## Next", "## Key Files"]
    for h in required_headers:
        if h not in content:
            errors.append(f"Missing section header: {h}")

    if errors:
        return errors

    completed = _section_body(content, "## Completed")
    current = _section_body(content, "## Current")
    next_ = _section_body(content, "## Next")

    # Completed is valid if a history pointer line is present OR body >= 100
    # non-whitespace chars. The pointer (`History: <id>_history.md`) lets a
    # long task's Completed shrink to a pointer once streams are archived.
    completed_chars = len(re.sub(r"\s+", "", completed))
    pointer_present = bool(re.search(r"^History:\s+\S+_history\.md", completed, re.MULTILINE))
    if not (pointer_present or completed_chars >= 100):
        errors.append(f"## Completed too short: {completed_chars} non-whitespace chars (need >= 100)")

    # Current must be non-empty
    if not current:
        errors.append("## Current must not be empty")

    # Next must be non-empty
    if not next_:
        errors.append("## Next must not be empty")

    return errors


def _audit_md(md_path: Path) -> List[str]:
    """Non-blocking bloat warnings for a recovery .md (docs/spec §6).

    Returns a list of warning strings (empty = no warnings). Called ONLY from
    cmd_pause AFTER _validate_md passes. Warnings are advisory; they never block.
    """
    if not md_path.exists():
        return []
    content = _read_md(md_path)
    content = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)

    task_id = md_path.stem
    warnings: List[str] = []
    next_body = _section_body(content, "## Next")
    current_body = _section_body(content, "## Current")

    next_chars = len(re.sub(r"\s+", "", next_body))
    if next_chars > 300:
        warnings.append(
            f"## Next is {next_chars} chars (guideline ~300). "
            f"Run archive-stream {task_id} <stream> to fold finished streams."
        )

    current_chars = len(re.sub(r"\s+", "", current_body))
    if current_chars > 1200:
        warnings.append(
            f"## Current is {current_chars} chars (guideline ~1200). "
            f"Run archive-stream {task_id} <stream> to fold finished streams."
        )

    return warnings


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
    # --closed: list archived (closed) tasks instead of pending
    if getattr(args, "closed", False):
        records = _read_archive(wf_dir)
        if not records:
            print("No archived tasks.")
            return
        # Dedupe by id (defend against manual-edit corruption of archive.jsonl);
        # keep first occurrence, preserving read order.
        seen = set()
        entries = []
        for r in records:
            if r["id"] not in seen:
                seen.add(r["id"])
                entries.append(r)
        # Sort by closed_at descending (most recently closed first)
        entries.sort(key=lambda r: r.get("closed_at", r.get("updated", "")), reverse=True)
        print(f"Archived tasks ({len(entries)})")
        for r in entries:
            closed_at = r.get("closed_at", r.get("updated", "?"))
            # Trim to date for display
            closed_date = closed_at[:10] if len(closed_at) >= 10 else closed_at
            print(f"  {r['id']} — {r['title']}  (closed {closed_date})")
        return

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
        if len(ctx) > HOOK_BUDGET:
            ctx = ctx[:HOOK_BUDGET]
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
        rels = r.get("relations", [])
        line = f"  {r['id']} — {r['title']}  (heat={h}, {age}d)"
        if rels:
            line += "  [" + ", ".join(f"{x['to']}({x['type']})" for x in rels) + "]"
        if c:
            line = c + line + RESET
        print(line)


def cmd_create(wf_dir: Path, args: Any) -> None:
    title: str = args.title
    note: str = args.note
    if not note.strip():
        print("--note is required and must not be empty.", file=sys.stderr)
        sys.exit(1)
    task_id = _generate_id(title)
    now = _now_iso()

    # Check for duplicate id
    records = _read_jsonl(wf_dir)
    _, existing = _find_record(records, task_id)
    if existing:
        print(f"Task id '{task_id}' already exists.", file=sys.stderr)
        sys.exit(1)

    # Resolve scope for source-doc scanning
    project_root = _find_project_root()

    # Parse timestamps for source-doc scan
    created_ts = _parse_ts_from_id(task_id)
    updated_ts = datetime.fromisoformat(now)

    # Auto-scan source docs
    candidates = _scan_doc_candidates(wf_dir, task_id, created_ts, updated_ts, project_root)

    # Build record
    record = {
        "id": task_id,
        "title": title,
        "note": note.strip(),
        "created": now,
        "updated": now,
        "skill": None,
        "source_docs": [],
        "relations": [],
    }

    records.append(record)
    _write_jsonl(wf_dir, records)

    # Generate .md with note seeded into ## Current
    md_path = _generate_md(wf_dir, task_id, note.strip())

    print(f"Created {task_id}")
    print(f"  title: {title}")
    print(f"  note: {note.strip()}")
    print(f"  md: {md_path}")
    if candidates:
        print(f"  source-doc candidates:")
        for c in candidates:
            print(f"    {c}")
    else:
        print(f"  source-doc candidates: (none)")


VALID_RELATION_TYPES = ("blocks", "depends-on", "related")


def cmd_link(wf_dir: Path, args: Any) -> None:
    """Create a typed relation between two tasks (mnemon-style typed edges)."""
    task_id: str = args.id
    target_id: str = args.target
    rel_type: str = args.type
    records = _read_jsonl(wf_dir)
    _, record = _find_record(records, task_id)
    if record is None:
        print(f"Task '{task_id}' not found.", file=sys.stderr)
        sys.exit(1)
    if target_id == task_id:
        print("Self-relation not allowed.", file=sys.stderr)
        sys.exit(1)
    _, target = _find_record(records, target_id)
    if target is None:
        print(f"Target task '{target_id}' not found.", file=sys.stderr)
        sys.exit(1)
    relations = record.get("relations", [])
    relations = [r for r in relations if not (r.get("to") == target_id and r.get("type") == rel_type)]
    relations.append({"to": target_id, "type": rel_type})
    record["relations"] = relations
    _write_jsonl(wf_dir, records)
    print(f"Linked {task_id} -[{rel_type}]-> {target_id}")


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

    # Non-blocking bloat warnings (advisory; never block pause).
    warnings = _audit_md(md_path)
    if warnings:
        for w in warnings:
            print(f"  ! {w}", file=sys.stderr)

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
    project_root = _find_project_root()

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
    md_exists = md_path.exists()
    hist_path = wf_dir / f"{task_id}_history.md"
    hist_exists = hist_path.exists()

    # Dry-run
    if not args.yes:
        print(f"Task: {task_id}")
        print(f"Title: {record['title']}")
        print(f"Created: {record['created']}")
        print(f"Updated: {record['updated']}")
        if record.get("source_docs"):
            print(f"Source docs (kept in place):")
            for d in record["source_docs"]:
                print(f"  {d}")
        else:
            print("Source docs: (none)")
        print(f"\nArchive actions:")
        if md_exists:
            print(f"  {md_path} -> {_archived_md_dir(wf_dir) / md_path.name}")
        else:
            print(f"  {md_path} (.md recovery — MISSING)")
        if hist_exists:
            print(f"  {hist_path} -> {_archived_md_dir(wf_dir) / hist_path.name}")
        print(f"  workflows.jsonl line -> archive.jsonl (status=closed)")
        print("\n提醒: 如有可复用的技术知识请先通过 memory skill 沉淀。")
        if md_exists and _has_evidence_blocks(md_path):
            print(f"\n! 注意: .md 含未蒸馏的 ## Evidence 块 — close 后仍可 `distill {task_id} <stream> --yes` 补做。")
        print("\nRun `close <id> --yes` to archive (no deletion).")
        return

    # 1. Validate .md if present (warn on issues, but still archive)
    if md_exists:
        errors = _validate_md(md_path)
        if errors:
            print("Warning: .md validation issues:", file=sys.stderr)
            for e in errors:
                print(f"  - {e}", file=sys.stderr)

    # 1b. Warn loudly on undistilled Evidence blocks (non-blocking: distill
    #     still works after close via archived/).
    if md_exists and _has_evidence_blocks(md_path):
        print("! WARNING: .md contains undistilled ## Evidence blocks — "
              "distill after close via `distill <id> <stream> --yes`.",
              file=sys.stderr)

    # 2. Move .md to archived/ subdirectory
    archived_dir = _archived_md_dir(wf_dir)
    archived_dir.mkdir(parents=True, exist_ok=True)
    archived_md = archived_dir / md_path.name
    if md_exists:
        shutil.move(str(md_path), str(archived_md))
        print(f"  ARCHIVED: {md_path} -> {archived_md}")
    else:
        print(f"  WARNING: .md recovery file not found: {md_path}", file=sys.stderr)

    # 2b. Move <id>_history.md to archived/ if it exists (archive-stream sink).
    if hist_exists:
        archived_hist = archived_dir / hist_path.name
        shutil.move(str(hist_path), str(archived_hist))
        print(f"  ARCHIVED: {hist_path} -> {archived_hist}")

    # 3. Move record from workflows.jsonl to archive.jsonl (with status + closed_at)
    del records[idx]
    _write_jsonl(wf_dir, records)
    record["status"] = "closed"
    record["closed_at"] = _now_iso()
    _append_archive(wf_dir, record)

    # 4. source_docs are KEPT in place (no deletion) for traceability
    if record.get("source_docs"):
        print(f"  Source docs kept in place ({len(record['source_docs'])} file(s))")

    print(f"Closed {task_id} (archived)")


def _resolve_commit_hash(args: Any) -> Optional[str]:
    """Best-effort short commit hash for an archive-stream summary.

    Returns --commit if provided; else `git rev-parse --short HEAD` in the
    project root (None for global scope / non-git dir / failure).
    """
    if getattr(args, "commit", None):
        return args.commit
    project_root = _find_project_root()
    if project_root is None:
        return None
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip() or None
    except (subprocess.SubprocessError, OSError):
        pass
    return None


def cmd_archive_stream(wf_dir: Path, args: Any) -> None:
    """Fold a finished stream's narrative out of Current/Next into history.

    See docs/spec-archive-stream.md §4.
    """
    task_id: str = args.id
    stream: str = args.stream
    records = _read_jsonl(wf_dir)
    idx, record = _find_record(records, task_id)
    if record is None:
        # Closed tasks live in archive.jsonl, not workflows.jsonl — do not look
        # them up. archive-stream operates on pending tasks only.
        print(f"Task '{task_id}' not found.", file=sys.stderr)
        sys.exit(1)

    md_path = wf_dir / f"{task_id}.md"
    if not md_path.exists():
        print(f"Recovery file not found: {md_path}", file=sys.stderr)
        sys.exit(1)

    raw = _read_md(md_path)

    # ── Resolve span source: named (marker pair) or --range (line range) ──────
    # Both paths produce (start_idx, end_after, body_lines, label) consumed by the
    # shared downstream logic (cross-section refusal, summary, guards, dry-run).
    range_spec = getattr(args, "range", None)
    if range_spec:
        # --range mode: 1-indexed inclusive line range, no markers needed.
        try:
            s_str, e_str = range_spec.split(":")
            s, e = int(s_str), int(e_str)
        except ValueError:
            print(f"Invalid --range '{range_spec}' (expected <start>:<end>)", file=sys.stderr)
            sys.exit(1)
        raw_lines = raw.splitlines()
        if s < 1 or e > len(raw_lines) or s > e:
            print(
                f"Range {s}:{e} out of bounds (file has {len(raw_lines)} lines)",
                file=sys.stderr,
            )
            sys.exit(1)

        def _line_start(lines, lineno_1indexed):
            return sum(len(lines[j]) + 1 for j in range(lineno_1indexed - 1))

        start_idx = _line_start(raw_lines, s)
        # end_after = start of line e+1 (includes line e's trailing newline), or
        # len(raw) if e is the last line. Including the trailing \n avoids leaving
        # a blank line after deletion (verified by execution).
        end_after = _line_start(raw_lines, e + 1) if e < len(raw_lines) else len(raw)
        body_lines = raw_lines[s - 1:e]
        name_arg = getattr(args, "name", None)
        if name_arg:
            label = name_arg
        else:
            hist_path_for_count = wf_dir / f"{task_id}_history.md"
            n = 1
            if hist_path_for_count.exists():
                existing = hist_path_for_count.read_text(encoding="utf-8")
                n = len(re.findall(r"^- range-", existing, re.MULTILINE)) + 1
            label = f"range-{n}"
        start_marker = ""  # no marker; span_offset = start_idx
        stream = None  # range mode has no stream name
    elif stream:
        # Named mode: locate marker pair via string search (names are [a-z0-9-]+,
        # no metachars; string search avoids interpolation risk).
        start_marker = f"<!-- stream:start:{stream} -->"
        end_marker = f"<!-- stream:end:{stream} -->"
        start_idx = raw.find(start_marker)
        if start_idx == -1:
            print(f"No stream 'start:{stream}' marker in {task_id}.md", file=sys.stderr)
            sys.exit(1)
        end_idx = raw.find(end_marker, start_idx + len(start_marker))
        if end_idx == -1:
            print(
                f"Stream '{stream}' has start but no end marker in {task_id}.md — "
                f"add the end marker or use a complete pair",
                file=sys.stderr,
            )
            sys.exit(1)
        end_after = end_idx + len(end_marker)
        body_lines = raw[start_idx + len(start_marker):end_idx].splitlines()
        label = stream
    else:
        print("must specify a stream name or --range", file=sys.stderr)
        sys.exit(1)

    # ── Shared downstream ────────────────────────────────────────────────────
    span = raw[start_idx:end_after]

    # Marker-overlap refusal (range mode only): refuse if the span contains a
    # stream marker from the other mode — mixing leaves unbalanced markers.
    if range_spec and re.search(r"<!-- stream:", span):
        print(
            f"Refusing: range overlaps an existing stream marker — use the named "
            f"form for marked content, or remove the markers first",
            file=sys.stderr,
        )
        sys.exit(1)

    # Defense in depth: refuse cross-section spans (§2.2 assertion 2 blocks these
    # at pause for markers, but archive-stream checks independently for both modes).
    if re.search(r"\n## ", span):
        print(
            f"Refusing: span for '{label}' crosses a section boundary — "
            f"split into section-local pairs/ranges first",
            file=sys.stderr,
        )
        sys.exit(1)

    # Determine which section the span lives in (for the emptiness guard).
    preceding = raw[:start_idx]
    section_headers_before = re.findall(r"^## .+$", preceding, re.MULTILINE)
    section = section_headers_before[-1].strip() if section_headers_before else None

    # Summary: drop leading blanks; first remaining line, truncated to 120 chars.
    summary_line = next((ln for ln in body_lines if ln.strip()), f"({label})")
    summary_text = summary_line.strip()[:120]

    commit_hash = _resolve_commit_hash(args)
    memory = getattr(args, "memory", None)
    summary = f"- {label}: {summary_text}"
    if commit_hash:
        summary += f" @{commit_hash}"
    if memory:
        summary += f" [mem:{memory}]"

    hist_path = wf_dir / f"{task_id}_history.md"

    # Check whether the pointer would need to be added.
    stripped = re.sub(r"<!--.*?-->", "", raw, flags=re.DOTALL)
    completed_body = _section_body(stripped, "## Completed")
    pointer_present = bool(re.search(r"^History:\s+\S+_history\.md", completed_body, re.MULTILINE))
    pointer_would_add = not pointer_present

    # Active-content guard scan (spec §1): refuse if the span contains active work.
    # STRONG signals (PAUSED|⏸️) refuse unconditionally; WEAK (TODO|in progress)
    # refuse unless --force. NO \b boundaries — U+FE0F variation selector on ⏸️
    # breaks \b after a space, silently missing the pattern (round-1 review blocker).
    span_text = "\n".join(body_lines)
    span_offset = (start_idx + len(start_marker)) if stream else start_idx  # for N
    STRONG_RE = re.compile(r"(PAUSED|⏸️)", re.IGNORECASE)
    WEAK_RE = re.compile(r"(TODO|in progress)", re.IGNORECASE)
    strong = STRONG_RE.search(span_text)
    if strong:
        n_line = raw[:span_offset + strong.start()].count("\n") + 1
        print(
            f"Refusing: span for '{label}' contains active "
            f"marker '{strong.group()}' at line ~{n_line} — move the active item out "
            f"of the span before archiving.",
            file=sys.stderr,
        )
        sys.exit(1)
    weak = WEAK_RE.search(span_text)
    if weak and not getattr(args, "force", False):
        n_line = raw[:span_offset + weak.start()].count("\n") + 1
        print(
            f"Refusing: span for '{label}' contains "
            f"'{weak.group()}' at line ~{n_line} (may be a completed-context mention). "
            f"Re-run with --force to archive anyway.",
            file=sys.stderr,
        )
        sys.exit(1)
    if weak and getattr(args, "force", False):
        print(f"  ! warning: span contains '{weak.group()}' — archiving with --force.", file=sys.stderr)

    run_arg = f"--range {range_spec}" if range_spec else stream  # for dry-run + refuse msgs

    # Dry-run (default): show span edges so an over-broad range/marker is visible.
    if not args.yes:
        nonblank = [ln for ln in body_lines if ln.strip()]
        first_line = (nonblank[0][:80] + "...") if nonblank and len(nonblank[0]) > 80 else (nonblank[0] if nonblank else "(empty)")
        last_line = (nonblank[-1][:80] + "...") if nonblank and len(nonblank[-1]) > 80 else (nonblank[-1] if nonblank else "(empty)")
        print(f"Task: {task_id}")
        print(f"Span: {label}")
        print(f"\nArchive actions:")
        print(f"  delete span from {md_path} ({section})")
        print(f"    first: {first_line}")
        print(f"    last:  {last_line}")
        print(f"    {len(body_lines)} lines")
        print(f"  append to {hist_path}:")
        print(f"    {summary}")
        if pointer_would_add:
            print(f"  add pointer to ## Completed: History: {task_id}_history.md")
        print(f"\nRun `archive-stream {task_id} {run_arg} --yes` to apply.")
        evidence_blocks = _has_evidence_blocks(md_path)
        if evidence_blocks:
            print(f"\n! hint: 检测到未蒸馏的 ## Evidence 块: {', '.join(evidence_blocks)}")
            print(f"  运行 `distill {task_id} <stream> --yes` 晋升到台账。")
        return

    # Apply.
    new_raw = raw[:start_idx] + raw[end_after:]

    # Emptiness guard: would the section the span lived in now be empty?
    if section in ("## Current", "## Next"):
        if _section_body(new_raw, section) == "":
            print(
                f"Refusing: archiving '{label}' would empty {section}.\n"
                f"Seed the next step in {section} first (a concrete next action, "
                f">= 1 line), then re-run: checkpoint archive-stream {task_id} {run_arg}",
                file=sys.stderr,
            )
            sys.exit(1)

    # Add the pointer line to ## Completed if not already present.
    if pointer_would_add:
        completed_header = "## Completed\n"
        new_raw = new_raw.replace(
            completed_header,
            completed_header + f"History: {task_id}_history.md\n",
            1,
        )

    md_path.write_text(new_raw, encoding="utf-8")

    # Lazy-create history file and append the summary.
    with hist_path.open("a", encoding="utf-8") as fh:
        fh.write(summary + "\n")

    # Update ONLY record["updated"]; do not touch skill/source_docs.
    now = _now_iso()
    record["updated"] = now
    _write_jsonl(wf_dir, records)

    print(f"Archived stream '{stream}' from {task_id}.md -> {hist_path.name}")
    print(f"  {summary}")
    print(f"  updated: {now}")


# ── Evidence ledger (distill / report / ledger) ─────────────────────────────

EVIDENCE_LINE_RE = re.compile(r"^- (headline|change|decision|evidence|theme|done_at): (.*)$")
EVIDENCE_HEADER_RE = re.compile(r"^## Evidence: ([a-z0-9-]+)[ \t]*$", re.MULTILINE)
EVIDENCE_KIND_RE = re.compile(r"^(file|command|url):.+$")
DONE_AT_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _parse_evidence_block(content: str, stream: str) -> Optional[Dict]:
    """Parse the `## Evidence: <stream>` block into a dict, or None if absent.

    The block is structurally bound to a stream name (header carries it), so a
    block cannot be double-counted under a different stream.
    """
    target = None
    for m in EVIDENCE_HEADER_RE.finditer(content):
        if m.group(1) == stream:
            target = m
            break
    if target is None:
        return None
    body = content[target.end():]
    next_h = re.search(r"\n## ", body)
    if next_h:
        body = body[:next_h.start()]
    entry: Dict[str, Any] = {"evidence": [], "themes": []}
    for line in body.splitlines():
        lm = EVIDENCE_LINE_RE.match(line)
        if not lm:
            continue
        key, value = lm.group(1), lm.group(2).strip()
        if key == "evidence":
            entry["evidence"].append(value)
        elif key == "theme":
            entry["themes"].append(value)
        elif key == "decision":
            entry["decision"] = value
        else:
            entry[key] = value
    return entry


def _validate_evidence(entry: Dict) -> List[str]:
    """Hard validation of a distilled evidence entry. Returns error list."""
    errors: List[str] = []
    headline = (entry.get("headline") or "").strip()
    if not headline:
        errors.append("headline is required and must be non-empty")
    elif len(headline) > 140:
        errors.append(f"headline must be <= 140 chars (got {len(headline)})")

    change = (entry.get("change") or "").strip()
    if not change:
        errors.append("change is required and must be non-empty")

    evidence = entry.get("evidence", [])
    if not evidence:
        errors.append("at least one evidence entry is required")
    else:
        for ev in evidence:
            if not EVIDENCE_KIND_RE.match(ev):
                errors.append(f"evidence must be 'file:|command:|url:' with a non-empty value: {ev!r}")

    themes = entry.get("themes", [])
    if not themes:
        errors.append("at least one theme is required")

    done_at = entry.get("done_at")
    if not done_at or not DONE_AT_RE.match(done_at):
        errors.append(f"done_at is required and must be YYYY-MM-DD (got {done_at!r})")

    return errors


def _find_md_for_distill(wf_dir: Path, task_id: str) -> Optional[Path]:
    """Locate a task's .md for distill: pending first, then archived/."""
    md = wf_dir / f"{task_id}.md"
    if md.exists():
        return md
    archived = wf_dir / "archived" / f"{task_id}.md"
    if archived.exists():
        return archived
    return None


def _find_task_title(wf_dir: Path, task_id: str) -> Optional[str]:
    """Read task title: pending (workflows.jsonl) then closed (archive.jsonl)."""
    for r in _read_jsonl(wf_dir):
        if r.get("id") == task_id:
            return r.get("title")
    for r in _read_archive(wf_dir):
        if r.get("id") == task_id:
            return r.get("title")
    return None


def _has_evidence_blocks(md_path: Path) -> List[str]:
    """Return stream names of `## Evidence: <stream>` blocks in a .md."""
    if not md_path.exists():
        return []
    return [m.group(1) for m in EVIDENCE_HEADER_RE.finditer(_read_md(md_path))]


def _remove_evidence_block(content: str, stream: str) -> str:
    """Remove a `## Evidence: <stream>` block (header + body) from .md content."""
    lines = content.split("\n")
    hdr_idx = None
    for i, l in enumerate(lines):
        if l.strip() == f"## Evidence: {stream}":
            hdr_idx = i
            break
    if hdr_idx is None:
        return content
    end_idx = len(lines)
    for i in range(hdr_idx + 1, len(lines)):
        if lines[i].startswith("## "):
            end_idx = i
            break
    return "\n".join(lines[:hdr_idx] + lines[end_idx:])


def cmd_distill(wf_dir: Path, args: Any) -> None:
    """Promote a stream's `## Evidence: <stream>` block into ledger.jsonl."""
    task_id: str = args.id
    stream: str = args.stream
    if not re.fullmatch(r"[a-z0-9-]+", stream):
        print(f"Invalid stream name '{stream}' (must match [a-z0-9-]+).", file=sys.stderr)
        sys.exit(1)

    scope = _scope_from_dir(wf_dir)
    md_path = _find_md_for_distill(wf_dir, task_id)
    if md_path is None:
        print(f"Task '{task_id}' not found (pending or archived).", file=sys.stderr)
        sys.exit(1)

    raw = _read_md(md_path)
    entry = _parse_evidence_block(raw, stream)
    if entry is None:
        existing = _has_evidence_blocks(md_path)
        print(f"No '## Evidence: {stream}' block in {md_path}.", file=sys.stderr)
        if existing:
            print(f"  existing blocks: {', '.join(existing)}", file=sys.stderr)
        else:
            print("  no ## Evidence: blocks found", file=sys.stderr)
        sys.exit(1)

    errors = _validate_evidence(entry)
    if errors:
        print("Evidence validation failed:", file=sys.stderr)
        for e in errors:
            print(f"  - {e}", file=sys.stderr)
        sys.exit(1)

    new_entry = {
        "id": f"{scope}:{task_id}:{stream}",
        "task_id": task_id,
        "task_title": _find_task_title(wf_dir, task_id),
        "scope": scope,
        "stream": stream,
        "headline": entry["headline"].strip(),
        "change": entry["change"].strip(),
        "decision": entry.get("decision"),
        "evidence": entry.get("evidence", []),
        "themes": entry.get("themes", []),
        "done_at": entry["done_at"],
        "commit": _resolve_commit_hash(args),
        "memory": None,
        "ts": _now_iso(),
    }

    if not args.yes:
        print(f"Task: {task_id}")
        print(f"Stream: {stream}")
        print(f"\nLedger entry (upsert by id {new_entry['id']}):")
        print(json.dumps(new_entry, ensure_ascii=False, indent=2))
        print(f"\nWill delete block '## Evidence: {stream}' from {md_path}")
        print("\nRun `distill <id> <stream> --yes` to apply.")
        return

    # Upsert into ledger (line-preserving: malformed lines kept verbatim).
    lines = _read_ledger_raw(wf_dir)
    new_line = json.dumps(new_entry, ensure_ascii=False)
    replaced = False
    out: List[str] = []
    for line in lines:
        if not line.strip():
            out.append(line)
            continue
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            out.append(line)  # keep malformed line verbatim
            continue
        if parsed.get("id") == new_entry["id"]:
            out.append(new_line)
            replaced = True
        else:
            out.append(line)
    if not replaced:
        out.append(new_line)
    _write_ledger_lines(wf_dir, out)

    # Consume the Evidence block from the .md.
    md_path.write_text(_remove_evidence_block(raw, stream), encoding="utf-8")

    print(f"Distilled '{stream}' from {task_id} -> {_ledger_path(wf_dir).name}")
    print(f"  {new_entry['headline']}")


def _load_all_ledger_entries() -> List[Dict]:
    entries: List[Dict] = []
    for d in _all_ledger_dirs():
        entries.extend(_read_ledger(d))
    return entries


def _ledger_entries_for(args: Any, wf_dir: Path) -> List[Dict]:
    """Ledger entries for report/ledger: single scope if --scope-dir is given
    (testing), else aggregate across all scopes."""
    if getattr(args, "scope_dir", None):
        return _read_ledger(wf_dir)
    return _load_all_ledger_entries()


def _print_ledger_entry(e: Dict) -> None:
    print(f"- {e.get('headline', '')}  [{e.get('done_at', '')}]")
    if e.get("change"):
        print(f"    change: {e.get('change')}")
    if e.get("decision"):
        print(f"    decision: {e.get('decision')}")
    for ev in e.get("evidence", []):
        print(f"    evidence: {ev}")
    print(f"    ref: {e.get('scope', '')} · {e.get('task_id', '')} · {e.get('stream', '')}")
    print()


def cmd_report(wf_dir: Path, args: Any) -> None:
    entries = _ledger_entries_for(args, wf_dir)
    since = getattr(args, "since", None)
    until = getattr(args, "until", None)
    themes = getattr(args, "theme", None) or []

    filtered = []
    for e in entries:
        done = e.get("done_at", "")
        if since and done < since:
            continue
        if until and done > until:
            continue
        if themes:
            if not set(e.get("themes", [])).intersection(themes):
                continue
        filtered.append(e)

    filtered.sort(key=lambda e: (e.get("done_at", ""), e.get("ts", "")))

    if getattr(args, "json", False):
        # JSON is the machine-consumption path: force UTF-8 stdout (Windows
        # otherwise emits the locale codepage and garbles CJK for consumers).
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
        print(json.dumps(filtered, ensure_ascii=False, indent=2))
        return

    if not filtered:
        print("No ledger entries.")
        return

    if themes:
        groups: Dict[str, List[Dict]] = {}
        for e in filtered:
            et = set(e.get("themes", []))
            matched = [t for t in themes if t in et]
            key = matched[0] if matched else (sorted(et)[0] if et else "(none)")
            groups.setdefault(key, []).append(e)
        for theme in sorted(groups):
            print(f"## {theme}")
            for e in groups[theme]:
                _print_ledger_entry(e)
    else:
        for e in filtered:
            _print_ledger_entry(e)


def cmd_ledger_list(wf_dir: Path, args: Any) -> None:
    entries = _ledger_entries_for(args, wf_dir)
    themes = getattr(args, "theme", None) or []
    if themes:
        entries = [e for e in entries if set(e.get("themes", [])).intersection(themes)]
    entries.sort(key=lambda e: e.get("ts", ""))
    if not entries:
        print("No ledger entries.")
        return
    print(f"Ledger entries ({len(entries)})")
    for e in entries:
        print(f"  {e.get('id', '?')} — {e.get('done_at', '')} — {e.get('headline', '')}")


# ── CLI ─────────────────────────────────────────────────────────────────────

WRITE_COMMANDS = ("create", "pause", "close", "archive-stream", "link", "distill")


def _dispatch(command: str, wf_dir: Path, args: Any) -> None:
    if command == "list":
        cmd_list(wf_dir, args)
    elif command == "create":
        cmd_create(wf_dir, args)
    elif command == "pause":
        cmd_pause(wf_dir, args)
    elif command == "close":
        cmd_close(wf_dir, args)
    elif command == "archive-stream":
        cmd_archive_stream(wf_dir, args)
    elif command == "link":
        cmd_link(wf_dir, args)
    elif command == "distill":
        cmd_distill(wf_dir, args)
    elif command == "report":
        cmd_report(wf_dir, args)
    elif command == "ledger":
        if getattr(args, "ledger_command", None) == "list":
            cmd_ledger_list(wf_dir, args)
        else:
            print("Usage: checkpoint.py ledger list [--theme T]", file=sys.stderr)
    else:
        print(f"Unknown command: {command}", file=sys.stderr)


def main() -> None:
    import argparse
    p = argparse.ArgumentParser(prog="checkpoint.py", description="Workflow checkpoint CLI")
    p.add_argument("--scope-dir", type=str, help="Override auto-detected directory")

    sub = p.add_subparsers(dest="command")

    sp = sub.add_parser("list", help="List pending tasks (sorted by heat)")
    sp.add_argument("--hook", action="store_true", help="Output SessionStart JSON for hook consumption")
    sp.add_argument("--closed", action="store_true", help="List archived (closed) tasks instead of pending")

    sp = sub.add_parser("create", help="Create a new task")
    sp.add_argument("title", type=str, help="Human-readable task title")
    sp.add_argument("--note", type=str, required=True, help="Context for resume (what prompted this, what to do)")

    sp = sub.add_parser("pause", help="Validate .md and refresh updated timestamp")
    sp.add_argument("id", type=str, help="Task id (yyyyMMdd-HHmmss-slug)")
    sp.add_argument("--source-docs", type=str, help="Comma-separated additional source doc paths")
    sp.add_argument("--skill", type=str, help="Skill name to load on resume")

    sp = sub.add_parser("close", help="Close a task (dry-run by default)")
    sp.add_argument("id", type=str, help="Task id")
    sp.add_argument("--yes", action="store_true", help="Execute deletion")

    sp = sub.add_parser("archive-stream", help="Fold a finished stream into history")
    sp.add_argument("id", type=str, help="Task id (yyyyMMdd-HHmmss-slug)")
    sp.add_argument("stream", nargs="?", type=str, help="Stream name (matches <!-- stream:start:<name> -->)")
    sp.add_argument("--range", type=str, help="Archive a 1-indexed inclusive line range (legacy cleanup, no markers)")
    sp.add_argument("--name", type=str, help="Summary label for --range (default: range-N)")
    sp.add_argument("--memory", type=str, help="Memory slug to reference in the summary line")
    sp.add_argument("--commit", type=str, help="Commit hash (default: best-effort git HEAD)")
    sp.add_argument("--force", action="store_true", help="Override WEAK active-content signals (TODO/in progress)")
    sp.add_argument("--yes", action="store_true", help="Apply (default is dry-run)")

    sp = sub.add_parser("link", help="Create a typed relation between two tasks")
    sp.add_argument("id", type=str, help="Source task id")
    sp.add_argument("target", type=str, help="Target task id")
    sp.add_argument("--type", type=str, default="related", choices=list(VALID_RELATION_TYPES), help="Relation type")

    sp = sub.add_parser("distill", help="Promote a stream's Evidence block into the ledger")
    sp.add_argument("id", type=str, help="Task id (yyyyMMdd-HHmmss-slug)")
    sp.add_argument("stream", type=str, help="Stream name (matches '## Evidence: <stream>')")
    sp.add_argument("--commit", type=str, help="Commit hash (default: best-effort git HEAD)")
    sp.add_argument("--yes", action="store_true", help="Apply (default is dry-run)")

    sp = sub.add_parser("report", help="Generate an evidence report from the ledger")
    sp.add_argument("--since", type=str, help="Start date YYYY-MM-DD (inclusive)")
    sp.add_argument("--until", type=str, help="End date YYYY-MM-DD (inclusive)")
    sp.add_argument("--theme", action="append", help="Filter/group by theme (repeatable)")
    sp.add_argument("--json", action="store_true", help="Output JSON")

    sp = sub.add_parser("ledger", help="Raw evidence-ledger view")
    lsub = sp.add_subparsers(dest="ledger_command")
    lsp = lsub.add_parser("list", help="List ledger entries")
    lsp.add_argument("--theme", action="append", help="Filter by theme (repeatable)")

    args = p.parse_args()
    if not args.command:
        p.print_help()
        return

    wf_dir = _resolve(args)

    if args.command == "list" and getattr(args, "hook", False):
        # Hook mode: force UTF-8 stdout + fail-open (any error -> stderr,
        # empty stdout, exit 0) so a corrupt store never breaks the session.
        try:
            sys.stdout.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
        try:
            cmd_list(wf_dir, args)
        except Exception as exc:
            print(f"checkpoint hook error: {exc}", file=sys.stderr)
            return
        return

    if args.command in WRITE_COMMANDS:
        with _with_lock(wf_dir):
            _dispatch(args.command, wf_dir, args)
        return
    _dispatch(args.command, wf_dir, args)


if __name__ == "__main__":
    main()
