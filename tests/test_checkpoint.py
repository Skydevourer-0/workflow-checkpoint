"""Tests for scripts/checkpoint.py — Workflow checkpoint CLI, JSONL flat-file storage."""
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

# Add scripts/ to path for direct import
SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import checkpoint

HOME = Path.home()

# ── Helpers ──────────────────────────────────────────────────────────────────

def _run(*args, scope_dir=None):
    """Run checkpoint.py as subprocess, return CompletedProcess."""
    cmd = [sys.executable, str(SCRIPTS_DIR / "checkpoint.py")]
    if scope_dir is not None:
        cmd.extend(["--scope-dir", str(scope_dir)])
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=30)


def _make_valid_record(task_id="20260629-120000-test-task", title="Test Task"):
    """Return a minimal valid JSONL record."""
    now = datetime.now(timezone.utc).isoformat()
    return {
        "id": task_id,
        "title": title,
        "created": now,
        "updated": now,
        "skill": None,
        "source_docs": [],
    }


# ── Unit: slugify_project_key ────────────────────────────────────────────────

class TestSlugifyProjectKey:
    def test_normal_path(self):
        result = checkpoint.slugify_project_key("/home/user/my-project")
        assert result.startswith("-")
        assert "my-project" in result
        assert not result.endswith(" ")

    def test_empty_string_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            checkpoint.slugify_project_key("")

    def test_whitespace_only_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            checkpoint.slugify_project_key("   ")

    def test_special_chars(self):
        result = checkpoint.slugify_project_key("My Project (2024)!")
        # Should become something like "-My-Project-2024-"
        assert "(" not in result
        assert ")" not in result
        assert "!" not in result

    def test_leading_trailing_special_chars(self):
        result = checkpoint.slugify_project_key("---hello---")
        assert result == "-hello"


# ── Unit: _title_to_slug ─────────────────────────────────────────────────────

class TestTitleToSlug:
    def test_normal_title(self):
        result = checkpoint._title_to_slug("Hello World")
        assert result == "hello-world"

    def test_long_title_truncated(self):
        long_title = "a" * 50
        result = checkpoint._title_to_slug(long_title)
        assert len(result) <= 32

    def test_mixed_case(self):
        result = checkpoint._title_to_slug("Fix Auth Bug")
        assert result == "fix-auth-bug"

    def test_special_chars(self):
        result = checkpoint._title_to_slug("Hello!!! World???")
        assert result == "hello-world"

    def test_cjk_chars(self):
        # CJK characters should be removed (not a-z0-9)
        result = checkpoint._title_to_slug("你好-test-世界")
        # "你好" removed, "test" stays, "世界" removed
        assert "test" in result
        # CJK chars should not appear
        assert "你" not in result
        assert "好" not in result
        assert "世" not in result
        assert "界" not in result


# ── Unit: _generate_id ───────────────────────────────────────────────────────

class TestGenerateId:
    def test_format(self):
        task_id = checkpoint._generate_id("Test Task")
        # yyyyMMdd-HHmmss-slug
        parts = task_id.split("-")
        assert len(parts) >= 3  # date-HHmmss-slug...
        assert re.match(r"^\d{8}$", parts[0])  # yyyyMMdd
        assert re.match(r"^\d{6}$", parts[1])  # HHmmss
        assert "test-task" in task_id

    def test_deterministic_with_given_ts(self):
        ts = datetime(2026, 6, 29, 12, 0, 0)
        task_id = checkpoint._generate_id("Test Task", ts)
        assert task_id.startswith("20260629-120000-")
        assert task_id == "20260629-120000-test-task"


# ── Unit: _parse_ts_from_id ──────────────────────────────────────────────────

class TestParseTsFromId:
    def test_roundtrip(self):
        ts = datetime(2026, 6, 29, 12, 0, 0, tzinfo=timezone.utc)
        task_id = checkpoint._generate_id("Test Task", ts)
        parsed = checkpoint._parse_ts_from_id(task_id)
        assert parsed == ts
        assert parsed.tzinfo is not None  # must be timezone-aware

    def test_malformed_raises(self):
        with pytest.raises(ValueError, match="Cannot parse timestamp"):
            checkpoint._parse_ts_from_id("not-a-valid-id")

    def test_no_date_part_raises(self):
        with pytest.raises(ValueError, match="Cannot parse timestamp"):
            checkpoint._parse_ts_from_id("just-slug")


# ── Unit: JSONL I/O ──────────────────────────────────────────────────────────

class TestJsonlIO:
    def test_roundtrip(self, tmp_path):
        records = [
            _make_valid_record("20260629-120000-task-a", "Task A"),
            _make_valid_record("20260629-120001-task-b", "Task B"),
        ]
        checkpoint._write_jsonl(tmp_path, records)
        loaded = checkpoint._read_jsonl(tmp_path)
        assert len(loaded) == 2
        assert loaded[0]["id"] == "20260629-120000-task-a"
        assert loaded[1]["id"] == "20260629-120001-task-b"

    def test_empty_dir_returns_empty_list(self, tmp_path):
        loaded = checkpoint._read_jsonl(tmp_path)
        assert loaded == []

    def test_nonexistent_dir(self, tmp_path):
        loaded = checkpoint._read_jsonl(tmp_path / "nonexistent")
        assert loaded == []


class TestFindRecord:
    def test_found(self):
        records = [
            _make_valid_record("20260629-120000-task-a", "Task A"),
            _make_valid_record("20260629-120001-task-b", "Task B"),
        ]
        idx, record = checkpoint._find_record(records, "20260629-120001-task-b")
        assert idx == 1
        assert record["title"] == "Task B"

    def test_not_found(self):
        records = [_make_valid_record("20260629-120000-task-a")]
        idx, record = checkpoint._find_record(records, "nonexistent")
        assert idx == -1
        assert record is None


class TestNowIso:
    def test_returns_iso_format(self):
        result = checkpoint._now_iso()
        # Should parse as ISO datetime
        dt = datetime.fromisoformat(result)
        assert dt.tzinfo is not None  # has timezone
        # Should be recent
        now = datetime.now(timezone.utc)
        diff = abs((now - dt).total_seconds())
        assert diff < 10


# ── Unit: _generate_md ───────────────────────────────────────────────────────

class TestGenerateMd:
    def test_file_created_with_all_headers(self, tmp_path):
        task_id = "20260629-120000-test-task"
        md_path = checkpoint._generate_md(tmp_path, task_id)
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "## Completed" in content
        assert "## Current" in content
        assert "## Decisions" in content
        assert "## Next" in content
        assert "## Key Files" in content

    def test_returns_correct_path(self, tmp_path):
        task_id = "20260629-120000-test-task"
        md_path = checkpoint._generate_md(tmp_path, task_id)
        assert md_path.name == f"{task_id}.md"
        assert md_path.parent == tmp_path


# ── Unit: _validate_md ───────────────────────────────────────────────────────

class TestValidateMd:
    def test_empty_template_fails(self, tmp_path):
        md_path = tmp_path / "task.md"
        # Use truly empty template (headers present but with minimal/no body)
        # The stock _TEMPLATE has HTML comments with enough text to pass Completed length check,
        # so we use a stripped version. If the template comments provide >=100 chars,
        # the template alone passes validation — we need truly empty bodies.
        empty = "## Completed\n\n\n## Current\n\n\n## Decisions\n\n\n## Next\n\n\n## Key Files\n"
        md_path.write_text(empty, encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        assert len(errors) > 0
        # Should include Completed too short, Current empty, Next empty
        error_texts = [e.lower() for e in errors]

        # Check at least one of these failure conditions is present
        has_completed_short = any("completed" in e and "short" in e for e in error_texts)
        has_current_empty = any("current" in e and "empty" in e for e in error_texts)
        has_next_empty = any("next" in e and "empty" in e for e in error_texts)
        assert has_completed_short, f"Expected 'Completed too short' error, got: {errors}"
        assert has_current_empty, f"Expected 'Current must not be empty' error, got: {errors}"
        assert has_next_empty, f"Expected 'Next must not be empty' error, got: {errors}"

    def test_filled_template_passes(self, tmp_path):
        md_path = tmp_path / "task.md"
        content = (
            "## Completed\n\n"
            + ("x" * 100) + "\n"  # >= 100 non-whitespace chars
            + "\n## Current\nWorking on tests\n\n"
            + "## Decisions\nChose JSONL\n\n"
            + "## Next\nRun tests\n\n"
            + "## Key Files\ncheckpoint.py\n"
        )
        md_path.write_text(content, encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        assert errors == []

    def test_missing_headers_reported(self, tmp_path):
        md_path = tmp_path / "task.md"
        content = "Some content without proper headers."
        md_path.write_text(content, encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        # Should report missing headers
        assert len(errors) >= 5  # all 5 headers are missing

    def test_short_completed_caught(self, tmp_path):
        md_path = tmp_path / "task.md"
        content = (
            "## Completed\nshort\n\n"
            "## Current\nworking\n\n"
            "## Decisions\n\n"
            "## Next\nnext step\n\n"
            "## Key Files\n"
        )
        md_path.write_text(content, encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        assert any("short" in e.lower() for e in errors)


# ── Unit: _alpha_tokens ──────────────────────────────────────────────────────

class TestAlphaTokens:
    def test_extracts_tokens(self):
        tokens = checkpoint._alpha_tokens("2026-06-29-workflow-checkpoint-v3-design.md")
        expected = {"workflow", "checkpoint", "v", "design"}
        assert tokens == expected

    def test_no_alpha_returns_empty(self):
        tokens = checkpoint._alpha_tokens("2026-06-29.md")
        # Only numbers and hyphens/dots - no alpha tokens
        assert tokens == set()

    def test_single_word(self):
        tokens = checkpoint._alpha_tokens("myproject-plan.md")
        assert tokens == {"myproject", "plan"}


# ── Unit: _doc_date ──────────────────────────────────────────────────────────

class TestDocDate:
    def test_extracts_date(self):
        ts = checkpoint._doc_date(Path("2026-06-29-design-doc.md"))
        assert ts is not None
        dt = datetime.fromtimestamp(ts)
        assert dt.year == 2026
        assert dt.month == 6
        assert dt.day == 29

    def test_no_date_returns_none(self):
        assert checkpoint._doc_date(Path("just-a-doc.md")) is None

    def test_invalid_date_returns_none(self):
        assert checkpoint._doc_date(Path("9999-99-99-bad-date.md")) is None


# ── Unit: _scan_doc_candidates ────────────────────────────────────────────────

class TestScanDocCandidates:
    def test_doc_within_window_and_overlap_matches(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        now = datetime.now(timezone.utc)
        # Create a plan doc with matching slug tokens
        plans_dir = HOME / ".claude" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)

        doc_path = plans_dir / "2026-06-29-workflow-checkpoint-test.md"
        doc_path.write_text("test content", encoding="utf-8")
        # Set mtime to within window
        ts_float = now.replace(tzinfo=None).timestamp()
        os.utime(str(doc_path), (ts_float, ts_float))

        try:
            created_ts = datetime(2026, 6, 29, 0, 0, 0)
            updated_ts = now.replace(tzinfo=None)
            task_id = "20260629-120000-checkpoint-test"

            candidates = checkpoint._scan_doc_candidates(wf_dir, task_id, created_ts, updated_ts)
            assert len(candidates) > 0
            assert any("workflow-checkpoint-test" in c for c in candidates)
        finally:
            # Cleanup: remove test doc
            if doc_path.exists():
                doc_path.unlink()

    def test_doc_outside_time_window_excluded(self, tmp_path):
        wf_dir = tmp_path / "workflows"
        wf_dir.mkdir()

        now = datetime.now(timezone.utc)
        plans_dir = HOME / ".claude" / "plans"
        plans_dir.mkdir(parents=True, exist_ok=True)

        doc_path = plans_dir / "2020-01-01-old-doc.md"
        doc_path.write_text("old content", encoding="utf-8")

        try:
            # mtime will be now (file just created), but filename date is far outside the window
            created_ts = datetime(2025, 1, 1, 0, 0, 0)
            updated_ts = now.replace(tzinfo=None)
            task_id = "20250101-120000-something"

            candidates = checkpoint._scan_doc_candidates(wf_dir, task_id, created_ts, updated_ts)
            # Doc mtime is now, which is after updated_ts - should be excluded
            assert len([c for c in candidates if "old-doc" in c]) == 0
        finally:
            if doc_path.exists():
                doc_path.unlink()


# ── Unit: _heat_from_record ─────────────────────────────────────────────────

class TestHeatFromRecord:
    def test_returns_days(self):
        now = datetime.now(timezone.utc)
        record = {
            "updated": now.isoformat(),
        }
        heat = checkpoint._heat_from_record(record)
        assert heat <= 1.0  # updated just now, so < 1 day


# ── Unit: _color ─────────────────────────────────────────────────────────────

class TestColor:
    def test_hot(self):
        assert checkpoint.RED in checkpoint._color(14)

    def test_warm(self):
        assert checkpoint.YELLOW in checkpoint._color(7)

    def test_cool(self):
        assert checkpoint._color(6) == ""

    def test_cool_low(self):
        assert checkpoint._color(0) == ""


# ── CLI Integration Tests ───────────────────────────────────────────────────

class TestCliListEmpty:
    def test_list_empty(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("list", scope_dir=td)
            assert "No tasks" in result.stdout


class TestCliListHook:
    def test_hook_empty(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("list", "--hook", scope_dir=td)
            output = json.loads(result.stdout)
            assert "hookSpecificOutput" in output
            assert output["hookSpecificOutput"]["hookEventName"] == "SessionStart"
            assert output["hookSpecificOutput"]["additionalContext"] == ""


class TestCliCreateAndList:
    def test_create_and_list(self):
        with tempfile.TemporaryDirectory() as td:
            # Create
            result = _run("create", "My Test Task", scope_dir=td)
            assert "Created" in result.stdout
            assert "My Test Task" in result.stdout

            # List
            result = _run("list", scope_dir=td)
            assert "My Test Task" in result.stdout


class TestCliCreateDuplicate:
    def test_create_duplicate_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _run("create", "Duplicate Task", scope_dir=td)
            # Create same title within same second — id is deterministic by timestamp,
            # so we need to extract the id or create quickly
            result = _run("create", "Duplicate Task", scope_dir=td)
            # Either fails with non-zero exit or the id is different (different second)
            # The check is by generated id, not by title alone
            if result.returncode != 0:
                assert "already exists" in result.stderr
            else:
                # If it succeeded, a different id was generated (different second)
                records = checkpoint._read_jsonl(Path(td))
                assert len(records) == 2
                assert records[0]["id"] != records[1]["id"]


class TestCliPauseValidation:
    def test_pause_validation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Pause Test", scope_dir=td)
            # Extract task id from output
            lines = result.stdout.strip().split("\n")
            task_id = lines[0].split()[-1]

            # Overwrite with truly empty template (header + no body)
            md_path = Path(td) / f"{task_id}.md"
            empty = "## Completed\n\n\n## Current\n\n\n## Decisions\n\n\n## Next\n\n\n## Key Files\n"
            md_path.write_text(empty, encoding="utf-8")

            # Now pause with the empty template — should fail validation
            result = _run("pause", task_id, scope_dir=td)
            assert result.returncode != 0
            assert "Validation failed" in result.stderr

    def test_pause_validation_passes(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Pause Test", scope_dir=td)
            lines = result.stdout.strip().split("\n")
            task_id = lines[0].split()[-1]

            # Fill in the template with valid content
            content = (
                "## Completed\n\n"
                + ("x" * 100) + "\n\n"
                + "## Current\nWorking on tests\n\n"
                + "## Decisions\nChose JSONL\n\n"
                + "## Next\nRun tests\n\n"
                + "## Key Files\ncheckpoint.py\n"
            )
            md_path = Path(td) / f"{task_id}.md"
            md_path.write_text(content, encoding="utf-8")

            result = _run("pause", task_id, scope_dir=td)
            assert result.returncode == 0
            assert "Paused" in result.stdout


class TestCliClose:
    def test_close_dryrun(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Close Test", scope_dir=td)
            lines = result.stdout.strip().split("\n")
            task_id = lines[0].split()[-1]

            # Fill template so we can close
            content = (
                "## Completed\n\n"
                + ("x" * 100) + "\n\n"
                + "## Current\nWorking on tests\n\n"
                + "## Decisions\nChose JSONL\n\n"
                + "## Next\nRun tests\n\n"
                + "## Key Files\ncheckpoint.py\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")

            result = _run("close", task_id, scope_dir=td)
            assert result.returncode == 0
            assert "Files to delete" in result.stdout
            assert task_id in result.stdout

    def test_close_yes(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Close Test", scope_dir=td)
            lines = result.stdout.strip().split("\n")
            task_id = lines[0].split()[-1]

            result = _run("close", task_id, "--yes", scope_dir=td)
            # close --yes doesn't validate .md
            assert result.returncode == 0
            assert "Closed" in result.stdout

            # Verify .md is deleted
            md_path = Path(td) / f"{task_id}.md"
            assert not md_path.exists()

            # Verify removed from jsonl
            records = checkpoint._read_jsonl(Path(td))
            assert len(records) == 0


class TestCliMigrate:
    def test_migrate_v2_to_v3(self):
        with tempfile.TemporaryDirectory() as td:
            wf_dir = Path(td)

            # Create v2-style subdirectory
            slug = "my-v2-task"
            task_dir = wf_dir / slug
            task_dir.mkdir(parents=True)

            progress = {
                "state": "active",
                "description": "My V2 Migration Task",
                "skill": "my-skill",
                "ts": "2026-06-29T12:00:00Z",
                "source_plan": "docs/superpowers/plans/my-plan.md",
            }
            (task_dir / "progress.json").write_text(json.dumps(progress), encoding="utf-8")

            recovery = "## Old recovery content\n\nThis is the old v2 recovery."
            (task_dir / "recovery.md").write_text(recovery, encoding="utf-8")

            # Run migrate
            result = _run("migrate", scope_dir=td)
            assert result.returncode == 0
            assert "Migrated" in result.stdout

            # Verify v3 data
            records = checkpoint._read_jsonl(wf_dir)
            assert len(records) == 1
            assert records[0]["title"] == "My V2 Migration Task"
            assert records[0]["skill"] == "my-skill"
            assert "docs/superpowers/plans/my-plan.md" in records[0]["source_docs"]

            # Verify .md created
            md_files = list(wf_dir.glob("*.md"))
            assert len(md_files) == 1
            md_content = md_files[0].read_text(encoding="utf-8")
            assert "## Completed" in md_content
            assert "Old recovery content" in md_content

            # Verify backup created
            backup_dir = wf_dir / "_v2_backup"
            assert backup_dir.exists()
            assert (backup_dir / slug).exists()

    def test_migrate_nonexistent_dir(self):
        with tempfile.TemporaryDirectory() as td:
            # Use a subdirectory that doesn't exist, so cmd_migrate sees non-existent wf_dir
            nonexistent = Path(td) / "nonexistent_workflows"
            result = _run("migrate", scope_dir=str(nonexistent))
            assert result.returncode == 0
            assert "No v2 workflows directory found" in result.stdout

