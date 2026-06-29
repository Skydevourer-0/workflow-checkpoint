"""Tests for scripts/migrate_v2.py — one-shot v2-to-v3 migration."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT_DIR = Path(__file__).resolve().parent.parent / "scripts"
MIGRATE_SCRIPT = SCRIPT_DIR / "migrate_v2.py"


def _run_migrate(scope_dir: Path) -> subprocess.CompletedProcess:
    """Run migrate_v2.py --scope-dir <scope_dir> and return result."""
    return subprocess.run(
        [sys.executable, str(MIGRATE_SCRIPT), "--scope-dir", str(scope_dir)],
        capture_output=True,
        text=True,
    )


def _create_v2_dir(
    wf_dir: Path,
    slug: str,
    description: str = None,
    skill: str = None,
    ts: str = None,
    source_plan: str = None,
    recovery: str = None,
) -> Path:
    """Create a simulated v2 workflow subdirectory inside wf_dir."""
    task_dir = wf_dir / slug
    task_dir.mkdir(parents=True, exist_ok=True)

    prog = {"description": description or slug}
    if skill:
        prog["skill"] = skill
    if ts:
        prog["ts"] = ts
    if source_plan:
        prog["source_plan"] = source_plan

    (task_dir / "progress.json").write_text(json.dumps(prog), encoding="utf-8")

    if recovery is not None:
        (task_dir / "recovery.md").write_text(recovery, encoding="utf-8")

    return task_dir


class TestMigrateV2:
    """Tests for the v2 -> v3 migration script."""

    def test_migrate_normal(self, tmp_path):
        """Migrate a normal v2 task with progress.json and recovery.md."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        _create_v2_dir(
            wf_dir,
            slug="my-task",
            description="My Task",
            skill="python",
            ts="2026-06-28T10:00:00Z",
            source_plan="/path/to/plan.md",
            recovery="## Recovery\n\nDone some work.",
        )

        result = _run_migrate(wf_dir)

        assert result.returncode == 0
        assert "Migrated: my-task" in result.stdout
        assert "Migrated 1 task" in result.stdout

        # Verify JSONL record
        jsonl_path = wf_dir / "workflows.jsonl"
        assert jsonl_path.exists(), "workflows.jsonl should exist after migration"

        records = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1

        record = records[0]
        assert record["title"] == "My Task"
        assert record["skill"] == "python"
        assert record["source_docs"] == ["/path/to/plan.md"]
        assert "20260628" in record["id"]
        assert record["created"] == "2026-06-28T10:00:00+00:00"
        assert record["updated"] == record["created"]

        # Verify .md file
        md_path = wf_dir / f"{record['id']}.md"
        assert md_path.exists(), ".md recovery file should exist"

        content = md_path.read_text(encoding="utf-8")
        assert "## Completed" in content
        assert "## Current" in content
        assert "## Decisions" in content
        assert "## Next" in content
        assert "## Key Files" in content
        assert "Done some work." in content

        # Verify old dir moved to _v2_backup
        assert not (wf_dir / "my-task").exists(), "original v2 dir should be moved"
        backup_dir = wf_dir / "_v2_backup" / "my-task"
        assert backup_dir.exists(), "v2 dir should be under _v2_backup"
        assert (backup_dir / "progress.json").exists()
        assert (backup_dir / "recovery.md").exists()

    def test_migrate_empty(self, tmp_path):
        """Migration on empty directory should report no tasks and exit 0."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        result = _run_migrate(wf_dir)

        assert result.returncode == 0
        assert "No v2 tasks found" in result.stdout

        # No JSONL should be created
        assert not (wf_dir / "workflows.jsonl").exists()

    def test_migrate_already_exists(self, tmp_path):
        """Migration should refuse if workflows.jsonl already exists."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        # Create pre-existing jsonl
        (wf_dir / "workflows.jsonl").write_text("{}\n", encoding="utf-8")

        result = _run_migrate(wf_dir)

        assert result.returncode != 0
        assert "already exists" in result.stderr

    def test_migrate_z_timestamp(self, tmp_path):
        """Z suffix in v2 timestamps should be parsed correctly to +00:00."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        _create_v2_dir(
            wf_dir,
            slug="z-ts-task",
            description="Z TS Task",
            ts="2026-06-28T10:00:00Z",
        )

        result = _run_migrate(wf_dir)
        assert result.returncode == 0

        jsonl_path = wf_dir / "workflows.jsonl"
        assert jsonl_path.exists()

        records = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1
        # The Z should be converted to +00:00 in the output
        assert records[0]["created"] == "2026-06-28T10:00:00+00:00"

    def test_migrate_missing_recovery(self, tmp_path):
        """Migration should still work when recovery.md is missing."""
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        _create_v2_dir(
            wf_dir,
            slug="no-recovery",
            description="No Recovery",
        )

        result = _run_migrate(wf_dir)
        assert result.returncode == 0
        assert "Migrated: no-recovery" in result.stdout

        jsonl_path = wf_dir / "workflows.jsonl"
        assert jsonl_path.exists()

        records = [
            json.loads(line)
            for line in jsonl_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        assert len(records) == 1

        record = records[0]
        md_path = wf_dir / f"{record['id']}.md"
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        # Fallback text should be used
        assert "Task migrated from v2." in content
        assert "## Completed" in content
        assert "## Current" in content
        assert "## Decisions" in content
        assert "## Next" in content
        assert "## Key Files" in content
