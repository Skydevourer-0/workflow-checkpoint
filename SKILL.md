---
name: workflow-checkpoint
description: Use when tracking tasks, pausing/resuming work across sessions, checking pending tasks, or managing the full task lifecycle with checkpointing
---

# Workflow Lifecycle

Manage task memories across sessions with checkpointing, pause/resume,
and automatic SessionStart pending-task detection.

## Model Rules

- **Pause rule:** When user signals intent to pause or end the current session, execute the Pause Protocol: identify active task → extract progress (CC TaskList → plan checkboxes → conversation context) → write checkpoint → run `task-cli.py pause <slug> --step "..." --next "..."`
- **Startup rule:** On session start, if the SessionStart hook injected pending-task context, use it. Otherwise, run `task-cli.py list --json` to check. If tasks exist, tell the user what's pending and ask if they want to resume any.

## Scope Detection

```
Starting from CWD, walk up directory tree.
  → .git found below $HOME → project scope
  → .git found at or above $HOME → ignore (dotfiles repo guard)
  → reached filesystem root without .git → global scope
```

## Core Commands

Scope is auto-detected from CWD (`.git` upward lookup). No `--global`/`--project`
flag needed. Use `--scope-dir <path>` to override for testing.

```bash
task-cli.py list [--json]
task-cli.py create <slug> <description> [--step "..."] [--next "..."] [--plan "..."]
task-cli.py pause <slug> [--step "..."] [--next "..."] [--plan "..."]
task-cli.py resume <slug>
task-cli.py close <slug> [--yes]
```

`list` sorts by **heat**: days since last file touch.
heat ≥ 7: yellow, heat ≥ 14: red — pure time, no state bias.

## Task Lifecycle

Tasks track multi-step work across sessions.
Task files live in `global/workflows/<slug>/` — each task occupies a subdirectory with `progress.json` (event/state) and `recovery.md` (model prose).

```
Create → Active ⇄ Paused → Done
              ↑            │
              └── resume ──┘
```

| State | `event` field | Meaning |
|-------|---------|---------|
| Active | `"active"` | Currently working |
| Paused | `"paused"` | Checkpoint written, awaiting resume |
| Done | — | Removed by `close --yes` |

### Creation (two entry points)

1. **Plan start** — When a plan is approved and implementation begins, create
   the task with `event: "active"`. The plan file's existence
   is the signal that this work is worth tracking.

2. **First pause** — When the user pauses and no task memory exists yet,
   review the conversation context to infer what the active work was, then
   create a new task memory. Pause itself is a strong signal.

### Slug Naming

One logical task = one task subdirectory. CC `TaskCreate` entries are execution
steps — they must NOT each become their own task memory.

1. Plan file name without date prefix (e.g. `task-classification-plan.md` → `task-classification`)
2. Conversation context — derive from dominant topic or description

### Recovery Section

Every task memory must include a `## Recovery` section:

```markdown
## Recovery

- **Source plan:** <path or `none`>
- **Current step:** <what is being worked on right now>
- **Last completed:** <key milestones reached>
- **Next action:** <first thing to do on resume>
- **Key files:** <paths of files involved>
```

### Update Rule

Recovery section is NOT updated during active work. The only update point is
pause — at that moment, extract the freshest progress from available sources.

### Completion

- Trigger: user signals the task is complete
- `task-cli.py close <slug>` — dry-run first to show what will be cleaned
- `task-cli.py close <slug> --yes` — complete task + delete referenced spec files

## Pause Protocol (mandatory)

When user signals intent to pause or end the current session:

```
Step 1 — Identify active task
  Run `task-cli.py list --json` and filter for `event: "active"`
  If found: proceed to Step 2 with the existing task
  If none: review current conversation to identify the active work
    → If work is identifiable: proceed to Step 2, will CREATE new task memory
    → If truly idle: report "no active task" and stop
    Idle test: did the conversation involve file edits, code generation,
    design decisions, or plan execution? If yes → not idle. If the
    conversation was purely information lookup, Q&A, or chat → idle.

Step 2 — Extract progress
  Collect from ALL available sources (not mutually exclusive):
    1. CC TaskList — if CC tasks exist, extract structured step statuses
       (merge all into one Recovery — CC tasks are steps, not separate files)
    2. Plan file — if referenced, read checkbox states, find first - [ ]
    3. Conversation context — always available as fallback
  Merge into single Recovery block: current step, last completed, next action,
  key files

Step 3 — Write checkpoint
  If task already exists: task-cli.py pause <slug> --step "..." --next "..."
  If task does not exist (first pause): task-cli.py create <slug> <desc> \
      --step "..." --next "..." (creates pre-paused; then Edit recovery.md)
  Report: "Checkpoint written to <tasks-dir>/<slug>/"
```

## Resume Protocol

**SessionStart check (silent):**

On session start, run `task-cli.py list --json` (scope auto-detected). If tasks
with `event` set to `"active"` or `"paused"` exist,
report pending tasks. If none, do nothing.

**Active query (user-initiated):**

When user asks to review pending tasks or resume work:
  Run `task-cli.py list --json`
  For each task with `event` set to `"active"` or `"paused"`:
    Read the task's `recovery.md` file
    Display: slug, description, event, current step, last completed
  If no tasks: report "No pending tasks."
  Ask which to resume (or none)
  On resume:
    Read Recovery section of selected task (from `recovery.md`)
    Run `task-cli.py resume <slug>` if currently paused
    Load referenced skill if present
    Begin execution from "Next action"

## Hook Setup

Run the cross-platform installer once:

```bash
python ~/.claude/skills/workflow-lifecycle/scripts/install.py
```

`install.py` generates the hook script (`.ps1` or `.sh`) from templates
embedded in the installer, substituting your Python path and the CLI path,
then registers it in `~/.claude/settings.json`. Use `--dry-run` to preview.

Re-run `install.py` after any update to `task-cli.py` to keep the hook in sync.
