DSH adaptation (native hook + tools)

Two surfaces:
1. SessionStart hook — run `checkpoint.py list --hook` on session start and
   inject pending tasks as context (what the Claude/Codex SessionStart hook does).
2. Evidence-ledger tools — `checkpoint_report` and `checkpoint_ledger` read the
   evidence ledger (all scopes) so the agent can generate 述职/报告 material
   without shelling out. These two aggregate every scope and don't need the
   session cwd; `distill` and the lifecycle commands remain shell-driven via
   SKILL.md (they resolve scope from the session cwd).

Does NOT use a hook bridge and does NOT change scripts/checkpoint.py.

Install (replace the path with this directory's absolute path):
  dsh plugin add link:C:/Users/ruanletian/.cc-switch/skills/workflow-checkpoint/.dsh

Host code changes require a DSH restart to take effect.
