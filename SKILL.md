---
name: workflow-checkpoint
description: Use when tracking tasks, pausing/resuming work across sessions, checking pending tasks, or managing the full task lifecycle with checkpointing
---

# Workflow Checkpoint

JSONL flat-file task checkpoint. Script owns all writes; model only edits `.md` recovery files.
Scope is auto-detected from CWD. Use `--scope-dir <path>` only for testing.

Let `CP` = `$HOME/.claude/skills/workflow-checkpoint/scripts/checkpoint.py`

## Commands

```
python3 $CP list [--hook] [--closed]
python3 $CP create <title> --note <context>
python3 $CP pause <id> [--source-docs <path,...>] [--skill <name>]
python3 $CP archive-stream <id> <stream> [--memory <slug>] [--commit <sha>] [--force] [--yes]
python3 $CP archive-stream <id> --range <start>:<end> [--name <name>] [--memory <slug>] [--commit <sha>] [--force] [--yes]
python3 $CP close <id> [--yes]
```

`list` flags are mutually exclusive in effect: `--closed` lists archived tasks (text); `--hook` emits SessionStart JSON for pending tasks. If both are passed, `--closed` wins and emits text — the SessionStart hook is configured with `--hook` only (see `install.py`), so this never triggers in practice.

## When to Use Each Command

**Start a task or capture a quick idea:** `create "short descriptive title" --note "what prompted this and what to do"`. `--note` is required. The note seeds `## Current` in the `.md` so the model has context on resume — one sentence is enough for a quick mid-task capture. The script prints the generated id; remember it.

**Check pending tasks:** `list` — sorts by heat (days since `updated`). >=7 days yellow, >=14 red. `list --closed` shows archived (closed) tasks with their close date — for traceability, closed tasks are kept (not deleted).

**Pause/save progress:**
1. Identify the task id (from `list` or context)
2. Edit `~/.claude/{global|projects/<slug>}/workflows/<id>.md` — replace each `<!-- comment -->` with real content
3. `pause <id> [--source-docs <paths>] [--skill <name>]`
   - `--source-docs`: comma-separated paths to add to auto-scanned results
   - `--skill`: skill name to load on resume
4. If validation fails, fix the .md and re-run

**Fold a finished sub-stream out of the active .md:** when a sub-stream finishes and its full narrative no longer needs to live in `## Current`/`## Next`, wrap it in markers and archive it:
1. Wrap the stream's narrative (BOTH markers in the SAME section — Current or Next only, never crossing `## ` boundaries):
   ```
   <!-- stream:start:<name> -->
   ...stream narrative...
   <!-- stream:end:<name> -->
   ```
   `<name>` matches `[a-z0-9-]+`. `create` seeds the first pair (`<!-- stream:start:initial -->` around the note). Add marker pairs by hand when opening new streams.
2. `archive-stream <id> <name>` (dry-run) → `archive-stream <id> <name> --yes` to apply.
3. The marker pair and its body are DELETED from the `.md`; a one-line summary is appended to `<id>_history.md`, and a pointer `History: <id>_history.md` is added to `## Completed`. The active `.md` shrinks.
- `--memory <slug>`: references a memory name in the summary line (optional; sediment reusable knowledge via the memory skill first).
- `--commit <sha>`: defaults to best-effort `git rev-parse --short HEAD` in the project root (omitted for global scope / non-git).
- `--force`: overrides WEAK active-content signals (`TODO`/`in progress` in the span). STRONG signals (`PAUSED`/`⏸️`) always refuse — they unambiguously mark pending work. Use `--force` only when a completed narrative legitimately contains "TODO" (e.g. "fixed the TODO handling").
- **Active-content guard:** before archiving, the span is scanned for `PAUSED`/`⏸️` (refuse) and `TODO`/`in progress` (refuse unless `--force`). This prevents a pending item from being silently swept into an archived span.
- Refuses if archiving would empty `## Current`/`## Next` (seed the next step first) or if a pair crosses a section boundary.
- A stream whose narrative spans BOTH Current and Next (separated by `## Decisions`) must be archived as TWO calls (one per section), because cross-section pairs are forbidden.

**Legacy cleanup (unmarked finished prose):** for a `.md` with finished streams that were NEVER marked (legacy files, or streams you forgot to mark mid-task), use `--range <start>:<end>` instead of adding markers retroactively:
1. Read the `.md` with line numbers (`grep -n` or editor), identify the finished stream's line range (1-indexed, inclusive).
2. `archive-stream <id> --range 12:47` (dry-run shows first/last/count of the span) → `--range 12:47 --yes` to apply.
3. Same effect as the named form: span deleted, one-line summary appended to `<id>_history.md`, pointer added to `## Completed`.
- `--name <name>`: sets the summary label (default `range-N`, monotonic).
- `--range` inherits ALL guards (cross-section refusal, empty-section refusal, active-content scan) and refuses if the span overlaps an existing `<!-- stream:` marker (mixing modes).
- **Forward work should still use markers** — a marker name is a stable handle across edits (line numbers drift as the file is edited; a marker resolves by name regardless). Use `--range` only for unmarked legacy cleanup, not as the forward default.
- Batch: for many `--range` calls, loop in shell (`for r in 12:47 50:61; do ...; done`).

**Resume a task:** Read `~/.claude/{global|projects/<slug>}/workflows/<id>.md` directly. Load `skill` if non-null. Start from `## Next`. Optional completed-stream context lives in `<id>_history.md` (pointer in `## Completed`).

**Close a task:** `close <id>` (dry-run, shows what will be archived) → if knowledge worth keeping, archive first via memory skill → `close <id> --yes` (validates .md same as pause, then archives). Closing does NOT delete: the record moves from `workflows.jsonl` to `archive.jsonl` (with `status=closed` + `closed_at`), the `.md` moves to an `archived/` subdirectory, and `source_docs` are kept in place. Recover with `list --closed` or by reading `archived/<id>.md` directly. The `<id>` keeps its `yyyyMMdd-HHmmss-<slug>` name, so archived work stays retrievable by date or topic via `find`.

**Retrieve closed work:**
- `list --closed` — lists archived tasks. Output format: `Archived tasks (N)` header, then one line per task: `<id> — <title>  (closed YYYY-MM-DD)`, sorted most-recently-closed first. Duplicate ids (from manual-edit corruption) are deduped automatically.
- By date: `find ~/.claude/{global,projects/*/}/workflows/archived/ -name "202607*.md"` (all July 2026 work).
- By topic: `find ~/.claude/{global,projects/*/}/workflows/archived/ -name "*thor-stage1*.md"`.
- Read a specific task: `~/.claude/{global|projects/<slug>}/workflows/archived/<id>.md`.

**Archiving vs memory:** Closing archives the *work record* (what was done, decisions, files) for traceability — it does NOT extract reusable technical knowledge. If the task produced a reusable conclusion (a debugging lesson, an operator finding, a cross-session engineering insight), sink it via the memory skill *before* `close --yes`. The archive is a log; memory is the knowledge base.

## Storage

```
~/.claude/global/workflows/<id>.md          (no .git found)
~/.claude/projects/<slug>/workflows/<id>.md (project .git found)
~/.claude/{...}/workflows/<id>_history.md  (archived-stream summaries — created by archive-stream, lazy)
~/.claude/{...}/workflows/workflows.jsonl   (pending tasks — script-owned, NEVER touch)
~/.claude/{...}/workflows/archive.jsonl     (closed tasks — script-owned, NEVER touch)
~/.claude/{...}/workflows/archived/<id>.md  (closed-task recovery files)
~/.claude/{...}/workflows/archived/<id>_history.md  (moved alongside on close)
```

## .md Template

Generated by `create`. Model fills in before `pause` or `close --yes`:

```
<!-- Write ALL sections in English. -->
## Completed          ← >= 100 non-whitespace chars OR a `History: <id>_history.md` pointer line (added by archive-stream)
## Current            ← required (non-empty); holds active streams wrapped in `<!-- stream:start/end:<name> -->`
## Decisions          ← optional
## Next               ← required (non-empty); one sentence — first action on resume
## Key Files          ← optional
```

Validation strips HTML comments first, then checks the rules above. Stream markers
(`<!-- stream:start:<name> -->` / `<!-- stream:end:<name> -->`) are validated on the
RAW content before stripping: pairs must be balanced by name, both in the SAME
section (Current or Next only — never Decisions/Key Files), unique, and non-empty.
Malformed markers block `pause` with a recovery path.

**Writing convention (keeps the .md bounded on long tasks):** finished streams
belong as one-line summaries in `<id>_history.md` (via `archive-stream`), NOT as
full narratives in Current/Next. `## Next` = one sentence, the first action on
resume. `pause` prints a non-blocking `! hint` if Current (>1200 chars) or Next
(>300 chars) exceed their guidelines.

## Source Docs

At `create` and `pause`, the script auto-scans for related documents:
- Global scope: `~/.claude/plans/`
- Project scope: `<root>/docs/superpowers/{plans,specs}/`

Dual filter: mtime within task time window + filename slug tokens overlap with task title.
Candidates shown on stdout; model can pass `--source-docs` to add paths manually.

## Setup

```bash
python3 $CP/../install.py
```
