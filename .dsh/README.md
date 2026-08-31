DSH adaptation (native hook)

Uses the DSH native Cordis extension point agent/session-start to do what the
Claude Code / Codex SessionStart hook does: run checkpoint.py list --hook on
session start and inject pending tasks as context.

Does NOT use a hook bridge and does NOT change scripts/checkpoint.py.

Install (replace the path with this directory's absolute path):
  dsh plugin add link:C:/Users/ruanletian/.cc-switch/skills/workflow-checkpoint/.dsh
