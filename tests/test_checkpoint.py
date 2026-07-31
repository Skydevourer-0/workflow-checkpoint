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
        md_path = checkpoint._generate_md(tmp_path, task_id, "seed note")
        assert md_path.exists()
        content = md_path.read_text(encoding="utf-8")
        assert "## Completed" in content
        assert "## Current" in content
        assert "## Decisions" in content
        assert "## Next" in content
        assert "## Key Files" in content

    def test_returns_correct_path(self, tmp_path):
        task_id = "20260629-120000-test-task"
        md_path = checkpoint._generate_md(tmp_path, task_id, "seed note")
        assert md_path.name == f"{task_id}.md"
        assert md_path.parent == tmp_path

    def test_create_seeds_initial_marker(self, tmp_path):
        task_id = "20260629-120000-test-task"
        md_path = checkpoint._generate_md(tmp_path, task_id, "seed note")
        content = md_path.read_text(encoding="utf-8")
        assert "<!-- stream:start:initial -->" in content
        assert "<!-- stream:end:initial -->" in content
        # The note sits between the markers inside ## Current.
        current = checkpoint._section_body(
            re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL), "## Current"
        )
        assert "seed note" in current
        # The seeded markers must be valid (no marker errors). Note: a brand-new
        # task's ## Completed is template-comment-only (empty after strip), so the
        # file does NOT pass full _validate_md until the model fills Completed —
        # that is pre-existing behavior, not a marker issue. Assert only that
        # marker validation is clean.
        marker_errors = checkpoint._validate_markers(content, task_id)
        assert marker_errors == [], marker_errors


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


# ── Unit: _section_body (module-level) ───────────────────────────────────────

class TestSectionBody:
    def test_extracts_body_between_headers(self):
        text = "## Completed\nbody here\n\n## Current\nother\n"
        assert checkpoint._section_body(text, "## Completed") == "body here"

    def test_returns_empty_on_missing_header(self):
        # Must not raise ValueError — module-level callers rely on "".
        assert checkpoint._section_body("no headers here", "## Current") == ""

    def test_extracts_last_section_to_eof(self):
        text = "## Key Files\nfile.py\n"
        assert checkpoint._section_body(text, "## Key Files") == "file.py"


# ── Unit: F2 pointer rule (## Completed) ─────────────────────────────────────

class TestF2PointerRule:
    def _full(self, completed_body: str) -> str:
        return (
            "## Completed\n\n" + completed_body + "\n\n"
            "## Current\nworking\n\n"
            "## Decisions\n\n"
            "## Next\nnext step\n\n"
            "## Key Files\n"
        )

    def test_pointer_only_completed_passes(self, tmp_path):
        # Pointer line < 100 chars but pointer present → passes.
        md_path = tmp_path / "20260730-120000-foo.md"
        md_path.write_text(self._full("History: 20260730-120000-foo_history.md"), encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        assert not any("Completed" in e for e in errors)

    def test_pointer_regex_anchored(self, tmp_path):
        # "History of the bug" has no _history.md token → does NOT match pointer.
        # Combined with < 100 chars → fails.
        md_path = tmp_path / "task.md"
        md_path.write_text(self._full("History of the bug: short"), encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        assert any("Completed" in e and ("pointer" in e.lower() or "short" in e.lower()) for e in errors)

    def test_real_summary_100_chars_passes(self, tmp_path):
        md_path = tmp_path / "task.md"
        md_path.write_text(self._full("x" * 100), encoding="utf-8")
        errors = checkpoint._validate_md(md_path)
        assert not any("Completed" in e for e in errors)

    def test_pointer_with_trailing_comment(self, tmp_path):
        # No `$` anchor → trailing comment still matches pointer.
        md_path = tmp_path / "20260730-120000-foo.md"
        md_path.write_text(
            self._full("History: 20260730-120000-foo_history.md (see notes)"),
            encoding="utf-8",
        )
        errors = checkpoint._validate_md(md_path)
        assert not any("Completed" in e for e in errors)


# ── Unit: _validate_markers (F3) ─────────────────────────────────────────────

class TestValidateMarkers:
    TASK_ID = "20260730-120000-foo"

    def _md(self, current_body: str, next_body: str = "next step") -> str:
        return (
            "## Completed\n\n" + ("x" * 100) + "\n\n"
            "## Current\n" + current_body + "\n\n"
            "## Decisions\n\n"
            "## Next\n" + next_body + "\n\n"
            "## Key Files\n"
        )

    def _errors(self, content: str):
        return checkpoint._validate_markers(content, self.TASK_ID)

    def test_well_formed_pair_passes(self):
        body = "<!-- stream:start:initial -->\nfinished work\n<!-- stream:end:initial -->"
        assert self._errors(self._md(body)) == []

    def test_mismatched_names_caught(self):
        body = "<!-- stream:start:foo -->\nwork\n<!-- stream:end:bar -->"
        errs = self._errors(self._md(body))
        assert any("no matching end:foo" in e for e in errs)

    def test_lone_start_caught(self):
        body = "<!-- stream:start:foo -->\nwork\n"
        errs = self._errors(self._md(body))
        assert any("no matching end:foo" in e for e in errs)

    def test_cross_section_pair_caught(self):
        # start in Current, end in Next → crosses boundary.
        content = (
            "## Completed\n\n" + ("x" * 100) + "\n\n"
            "## Current\n<!-- stream:start:foo -->\nwork\n\n"
            "## Decisions\n\n"
            "## Next\nmore work\n<!-- stream:end:foo -->\n\n"
            "## Key Files\n"
        )
        errs = self._errors(content)
        assert any("cross a section boundary" in e for e in errs)

    def test_marker_in_decisions_caught(self):
        content = (
            "## Completed\n\n" + ("x" * 100) + "\n\n"
            "## Current\nworking\n\n"
            "## Decisions\n<!-- stream:start:foo -->\nwork\n<!-- stream:end:foo -->\n\n"
            "## Next\nnext\n\n"
            "## Key Files\n"
        )
        errs = self._errors(content)
        assert any("outside ## Current/## Next" in e for e in errs)

    def test_duplicate_name_caught(self):
        body = (
            "<!-- stream:start:foo -->\na\n<!-- stream:end:foo -->\n"
            "<!-- stream:start:foo -->\nb\n<!-- stream:end:foo -->"
        )
        errs = self._errors(self._md(body))
        assert any("duplicate start:foo" in e for e in errs)

    def test_empty_body_caught(self):
        body = "<!-- stream:start:foo -->\n\n<!-- stream:end:foo -->"
        errs = self._errors(self._md(body))
        assert any("empty body for start:foo" in e for e in errs)

    def test_no_markers_passes(self):
        assert self._errors(self._md("just working")) == []

    def test_marker_in_code_block_ignored(self):
        body = "```\n<!-- stream:start:foo -->\nwork\n<!-- stream:end:bar -->\n```"
        # Literal markers inside a code fence are not real markers → no error.
        assert self._errors(self._md(body)) == []

    def test_missing_close_dashdash_survives(self):
        # `<!-- stream:start:foo` with no `-->` does not match the marker pattern
        # → not counted as a marker → no error (harmless literal text).
        body = "<!-- stream:start:foo\nwork\n"
        assert self._errors(self._md(body)) == []


# ── Unit: archive-stream command ─────────────────────────────────────────────

class TestArchiveStream:
    TASK_ID = "20260730-120000-foo"

    def _setup(self, tmp_path, current_body, completed_body=None):
        """Create wf_dir with a workflows.jsonl record and a .md file."""
        wf_dir = tmp_path / "wf"
        wf_dir.mkdir()
        record = _make_valid_record(self.TASK_ID)
        (wf_dir / "workflows.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        completed = completed_body if completed_body is not None else ("x" * 100)
        md = (
            "## Completed\n\n" + completed + "\n\n"
            "## Current\n" + current_body + "\n\n"
            "## Decisions\n\n"
            "## Next\nnext step\n\n"
            "## Key Files\n"
        )
        (wf_dir / f"{self.TASK_ID}.md").write_text(md, encoding="utf-8")
        return wf_dir

    def _args(self, stream, **kw):
        from argparse import Namespace
        defaults = {"memory": None, "commit": None, "yes": False, "force": False, "range": None, "name": None}
        defaults.update(kw)
        return Namespace(id=self.TASK_ID, stream=stream, **defaults)

    def test_dry_run_no_write(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->")
        before = (wf / f"{self.TASK_ID}.md").read_text()
        checkpoint.cmd_archive_stream(wf, self._args("s1"))
        assert (wf / f"{self.TASK_ID}.md").read_text() == before
        assert not (wf / f"{self.TASK_ID}_history.md").exists()
        out = capsys.readouterr().out
        assert "Archive actions:" in out
        assert "Run `archive-stream" in out

    def test_apply_deletes_body_writes_summary(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nfinished diagnostic\n<!-- stream:end:s1 -->\nmore active work")
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        md = (wf / f"{self.TASK_ID}.md").read_text()
        assert "finished diagnostic" not in md
        assert "<!-- stream:start:s1 -->" not in md
        assert "more active work" in md
        hist = (wf / f"{self.TASK_ID}_history.md").read_text()
        assert "- s1: finished diagnostic" in hist
        assert "History: 20260730-120000-foo_history.md" in md

    def test_pointer_added_when_absent(self, tmp_path):
        # Completed has real content (>= 100), no pointer → pointer prepended.
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        md = (wf / f"{self.TASK_ID}.md").read_text()
        # Pointer is the first line after ## Completed, before the x*100 content.
        completed = checkpoint._section_body(
            re.sub(r"<!--.*?-->", "", md, flags=re.DOTALL), "## Completed"
        )
        assert completed.startswith("History: 20260730-120000-foo_history.md")
        assert "x" * 100 in completed  # original content preserved after pointer

    def test_pointer_not_duplicated_when_present(self, tmp_path):
        wf = self._setup(
            tmp_path,
            "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive",
            completed_body="History: 20260730-120000-foo_history.md",
        )
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        md = (wf / f"{self.TASK_ID}.md").read_text()
        assert md.count("History: 20260730-120000-foo_history.md") == 1

    def test_lazy_history_creation(self, tmp_path):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive")
        assert not (wf / f"{self.TASK_ID}_history.md").exists()
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert (wf / f"{self.TASK_ID}_history.md").exists()

    def test_empty_section_refusal(self, tmp_path, capsys):
        # Archiving the ONLY Current content would empty Current → refuse.
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nonly content\n<!-- stream:end:s1 -->")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        # File unchanged.
        assert "only content" in (wf / f"{self.TASK_ID}.md").read_text()
        assert "would empty ## Current" in capsys.readouterr().err

    def test_cross_section_refusal(self, tmp_path, capsys):
        content = (
            "## Completed\n\n" + ("x" * 100) + "\n\n"
            "## Current\n<!-- stream:start:s1 -->\nwork\n\n"
            "## Decisions\n\n"
            "## Next\nmore\n<!-- stream:end:s1 -->\n\n"
            "## Key Files\n"
        )
        wf = tmp_path / "wf"
        wf.mkdir()
        record = _make_valid_record(self.TASK_ID)
        (wf / "workflows.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        (wf / f"{self.TASK_ID}.md").write_text(content, encoding="utf-8")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "crosses a section boundary" in capsys.readouterr().err

    def test_closed_task_not_found(self, tmp_path, capsys):
        # No record in workflows.jsonl (simulating a closed task in archive.jsonl).
        wf = tmp_path / "wf"
        wf.mkdir()
        (wf / "workflows.jsonl").write_text("", encoding="utf-8")
        (wf / f"{self.TASK_ID}.md").write_text("## Completed\nx\n", encoding="utf-8")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "not found" in capsys.readouterr().err

    def test_missing_start_marker_errors(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "no markers here, just active work")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("nonexistent", yes=True))
        assert "No stream 'start:nonexistent'" in capsys.readouterr().err

    def test_missing_end_marker_errors(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "no end marker" in capsys.readouterr().err

    def test_memory_and_commit_in_summary(self, tmp_path):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True, memory="onnx-shape", commit="abc1234"))
        hist = (wf / f"{self.TASK_ID}_history.md").read_text()
        assert "[mem:onnx-shape]" in hist
        assert "@abc1234" in hist

    def test_commit_omitted_global_scope(self, tmp_path):
        # _find_project_root returns None when CWD is not a project repo (the test
        # runs under the skill dir, which is skipped). With no --commit and no
        # project root, the summary omits @<commit>.
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        hist = (wf / f"{self.TASK_ID}_history.md").read_text()
        assert "@" not in hist

    def test_nested_pair_deletes_inner(self, tmp_path):
        body = (
            "<!-- stream:start:outer -->\nouter body\n"
            "<!-- stream:start:inner -->\ninner body\n<!-- stream:end:inner -->\n"
            "<!-- stream:end:outer -->\nactive work"
        )
        wf = self._setup(tmp_path, body)
        checkpoint.cmd_archive_stream(wf, self._args("outer", yes=True))
        md = (wf / f"{self.TASK_ID}.md").read_text()
        assert "outer body" not in md
        assert "inner body" not in md
        assert "<!-- stream:start:inner -->" not in md
        assert "active work" in md

    def test_record_updates_only_updated(self, tmp_path):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive")
        original_updated = json.loads((wf / "workflows.jsonl").read_text())["updated"]
        original_skill = json.loads((wf / "workflows.jsonl").read_text())["skill"]
        import time as _time
        _time.sleep(0.01)
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        rec = json.loads((wf / "workflows.jsonl").read_text())
        assert rec["updated"] != original_updated
        assert rec["skill"] == original_skill
        assert rec["source_docs"] == []

    # ── Active-content guard scan (Step 2) ────────────────────────────────────

    def test_strong_paused_refuses(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\n**PAUSED: pending review**\n<!-- stream:end:s1 -->\nactive")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "active marker" in capsys.readouterr().err
        assert "PAUSED" in (wf / f"{self.TASK_ID}.md").read_text()  # unchanged

    def test_strong_paused_emoji_refuses(self, tmp_path, capsys):
        # Regression: ⏸️ (U+23F8 + U+FE0F) must match despite \b issues.
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nstatus ⏸️ pending\n<!-- stream:end:s1 -->\nactive")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "active marker" in capsys.readouterr().err

    def test_weak_todo_refuses_without_force(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nfixed the TODO handling\n<!-- stream:end:s1 -->\nactive")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "TODO" in capsys.readouterr().err

    def test_weak_todo_proceeds_with_force(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nfixed the TODO handling\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True, force=True))
        assert "TODO handling" not in (wf / f"{self.TASK_ID}.md").read_text()
        assert "warning" in capsys.readouterr().err

    def test_no_active_content_archives(self, tmp_path):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nstream 9 done, all delivered\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True))
        assert "stream 9 done" not in (wf / f"{self.TASK_ID}.md").read_text()

    def test_strong_refuse_not_overridden_by_force(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\n**PAUSED: pending**\n<!-- stream:end:s1 -->\nactive")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._args("s1", yes=True, force=True))
        assert "PAUSED" in (wf / f"{self.TASK_ID}.md").read_text()  # still there

    # ── --range mode (Step 3) ──────────────────────────────────────────────────

    def _range_args(self, range_spec, **kw):
        from argparse import Namespace
        defaults = {"memory": None, "commit": None, "yes": False, "force": False, "name": None}
        defaults.update(kw)
        return Namespace(id=self.TASK_ID, stream=None, range=range_spec, **defaults)

    def test_range_deletes_span_writes_summary(self, tmp_path):
        # Current body has 3 prose lines + a keep line (so Current isn't emptied).
        wf = self._setup(tmp_path, "prose line one\nprose line two\nprose line three\nkeep this active")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        start = next(i for i, l in enumerate(md_lines, 1) if l == "prose line one")
        end = next(i for i, l in enumerate(md_lines, 1) if l == "prose line three")
        checkpoint.cmd_archive_stream(wf, self._range_args(f"{start}:{end}", yes=True))
        md = (wf / f"{self.TASK_ID}.md").read_text()
        assert "prose line one" not in md
        assert "prose line two" not in md
        assert "prose line three" not in md
        assert "keep this active" in md
        hist = (wf / f"{self.TASK_ID}_history.md").read_text()
        assert "- range-1: prose line one" in hist
        assert "History: 20260730-120000-foo_history.md" in md

    def test_range_1_indexed_inclusive(self, tmp_path):
        wf = self._setup(tmp_path, "keep this\nDELETE ME\nkeep this too")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        target = next(i for i, l in enumerate(md_lines, 1) if l == "DELETE ME")
        checkpoint.cmd_archive_stream(wf, self._range_args(f"{target}:{target}", yes=True))
        md = (wf / f"{self.TASK_ID}.md").read_text()
        assert "DELETE ME" not in md
        assert "keep this" in md
        assert "keep this too" in md

    def test_range_cross_section_refuses(self, tmp_path, capsys):
        # Range spanning Current→Next crosses ## Decisions.
        content = (
            "## Completed\n\n" + ("x" * 100) + "\n\n"
            "## Current\nstart here\n\n"
            "## Decisions\n\n"
            "## Next\nend here\n\n"
            "## Key Files\n"
        )
        wf = tmp_path / "wf"
        wf.mkdir()
        record = _make_valid_record(self.TASK_ID)
        (wf / "workflows.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
        (wf / f"{self.TASK_ID}.md").write_text(content, encoding="utf-8")
        md_lines = content.splitlines()
        s = next(i for i, l in enumerate(md_lines, 1) if l == "start here")
        e = next(i for i, l in enumerate(md_lines, 1) if l == "end here")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._range_args(f"{s}:{e}", yes=True))
        assert "crosses a section boundary" in capsys.readouterr().err

    def test_range_empty_section_refuses(self, tmp_path, capsys):
        # Current has only one line; archiving it empties Current.
        wf = self._setup(tmp_path, "only current line")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        target = next(i for i, l in enumerate(md_lines, 1) if l == "only current line")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._range_args(f"{target}:{target}", yes=True))
        assert "would empty" in capsys.readouterr().err

    def test_range_marker_overlap_refuses(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        s = next(i for i, l in enumerate(md_lines, 1) if "stream:start" in l)
        e = next(i for i, l in enumerate(md_lines, 1) if "stream:end" in l)
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._range_args(f"{s}:{e}", yes=True))
        assert "overlaps an existing stream marker" in capsys.readouterr().err

    def test_range_default_name(self, tmp_path):
        wf = self._setup(tmp_path, "first prose\nsecond prose\nkeep")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        s = next(i for i, l in enumerate(md_lines, 1) if l == "first prose")
        e = next(i for i, l in enumerate(md_lines, 1) if l == "second prose")
        checkpoint.cmd_archive_stream(wf, self._range_args(f"{s}:{e}", yes=True))
        assert "- range-1: first prose" in (wf / f"{self.TASK_ID}_history.md").read_text()

    def test_range_named(self, tmp_path):
        wf = self._setup(tmp_path, "first prose\nkeep")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        s = next(i for i, l in enumerate(md_lines, 1) if l == "first prose")
        checkpoint.cmd_archive_stream(wf, self._range_args(f"{s}:{s}", yes=True, name="custom-name"))
        assert "- custom-name: first prose" in (wf / f"{self.TASK_ID}_history.md").read_text()

    def test_range_dry_run_shows_span_edges(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "first prose\nmiddle\nlast prose\nkeep")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        s = next(i for i, l in enumerate(md_lines, 1) if l == "first prose")
        e = next(i for i, l in enumerate(md_lines, 1) if l == "last prose")
        checkpoint.cmd_archive_stream(wf, self._range_args(f"{s}:{e}"))
        out = capsys.readouterr().out
        assert "first:" in out and "first prose" in out
        assert "last:" in out and "last prose" in out
        assert "lines" in out
        assert not (wf / f"{self.TASK_ID}_history.md").exists()  # no write

    def test_range_neither_stream_nor_range_errors(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "some content")
        from argparse import Namespace
        args = Namespace(id=self.TASK_ID, stream=None, range=None, memory=None, commit=None, yes=True, force=False, name=None)
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, args)
        assert "must specify" in capsys.readouterr().err

    def test_range_active_content_refuses(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "done work\n**PAUSED: pending**\nmore\nkeep")
        md_lines = (wf / f"{self.TASK_ID}.md").read_text().splitlines()
        s = next(i for i, l in enumerate(md_lines, 1) if l == "done work")
        e = next(i for i, l in enumerate(md_lines, 1) if l == "more")
        with pytest.raises(SystemExit):
            checkpoint.cmd_archive_stream(wf, self._range_args(f"{s}:{e}", yes=True))
        assert "active marker" in capsys.readouterr().err

    # ── Dry-run enhancement (Step 4) ───────────────────────────────────────────

    def test_dry_run_shows_first_last_count(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nfirst line\nmiddle\nlast line\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1"))
        out = capsys.readouterr().out
        assert "first:" in out and "first line" in out
        assert "last:" in out and "last line" in out
        assert "lines" in out

    def test_dry_run_single_line_span(self, tmp_path, capsys):
        wf = self._setup(tmp_path, "<!-- stream:start:s1 -->\nsolo line\n<!-- stream:end:s1 -->\nactive")
        checkpoint.cmd_archive_stream(wf, self._args("s1"))
        out = capsys.readouterr().out
        assert "first:" in out and "solo line" in out
        assert "last:" in out and "solo line" in out  # first==last


# ── Unit: _audit_md (non-blocking warnings) ──────────────────────────────────

class TestAuditMd:
    TASK_ID = "20260730-120000-foo"

    def _md_path(self, tmp_path, current_body, next_body="next step"):
        md = (
            "## Completed\n\n" + ("x" * 100) + "\n\n"
            "## Current\n" + current_body + "\n\n"
            "## Decisions\n\n"
            "## Next\n" + next_body + "\n\n"
            "## Key Files\n"
        )
        md_path = tmp_path / f"{self.TASK_ID}.md"
        md_path.write_text(md, encoding="utf-8")
        return md_path

    def test_next_over_threshold_warns(self, tmp_path):
        long_next = "x" * 400
        md_path = self._md_path(tmp_path, "active", next_body=long_next)
        warnings = checkpoint._audit_md(md_path)
        assert any("## Next is" in w and "300" in w for w in warnings)

    def test_current_over_threshold_warns(self, tmp_path):
        long_current = "x" * 1300
        md_path = self._md_path(tmp_path, long_current)
        warnings = checkpoint._audit_md(md_path)
        assert any("## Current is" in w and "1200" in w for w in warnings)

    def test_current_under_threshold_no_warn(self, tmp_path):
        md_path = self._md_path(tmp_path, "small active state")
        warnings = checkpoint._audit_md(md_path)
        assert warnings == []

    def test_warning_non_blocking(self, tmp_path):
        # Via cmd_pause: warnings print to stderr but pause still succeeds.
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Audit Test", "--note", "n", scope_dir=td)
            task_id = result.stdout.strip().split("\n")[0].split()[-1]
            long_next = "x" * 400
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                "## Current\nworking\n\n"
                "## Decisions\n\n"
                "## Next\n" + long_next + "\n\n"
                "## Key Files\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            result = _run("pause", task_id, scope_dir=td)
            assert result.returncode == 0  # non-blocking
            assert "## Next is" in result.stderr
            assert "Paused" in result.stdout


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
            result = _run("create", "My Test Task", "--note", "My Test Task note", scope_dir=td)
            assert "Created" in result.stdout
            assert "My Test Task" in result.stdout

            # List
            result = _run("list", scope_dir=td)
            assert "My Test Task" in result.stdout


class TestCliCreateDuplicate:
    def test_create_duplicate_fails(self):
        with tempfile.TemporaryDirectory() as td:
            _run("create", "Duplicate Task", "--note", "Duplicate Task note", scope_dir=td)
            # Create same title within same second — id is deterministic by timestamp,
            # so we need to extract the id or create quickly
            result = _run("create", "Duplicate Task", "--note", "Duplicate Task note", scope_dir=td)
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
            result = _run("create", "Pause Test", "--note", "Pause Test note", scope_dir=td)
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
            result = _run("create", "Pause Test", "--note", "Pause Test note", scope_dir=td)
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
            result = _run("create", "Close Test", "--note", "Close Test note", scope_dir=td)
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
            assert "Archive actions" in result.stdout
            assert "archive.jsonl" in result.stdout
            assert task_id in result.stdout

    def test_close_yes(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Close Test", "--note", "Close Test note", scope_dir=td)
            lines = result.stdout.strip().split("\n")
            task_id = lines[0].split()[-1]

            # Fill .md with valid content (close --yes validates same as pause)
            content = (
                "## Completed\n\n"
                + ("x" * 100) + "\n\n"
                + "## Current\nWorking on tests\n\n"
                + "## Decisions\nChose JSONL\n\n"
                + "## Next\nRun tests\n\n"
                + "## Key Files\ncheckpoint.py\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")

            result = _run("close", task_id, "--yes", scope_dir=td)
            assert result.returncode == 0
            assert "Closed" in result.stdout
            assert "archived" in result.stdout

            # .md moved to archived/ (not deleted)
            md_path = Path(td) / f"{task_id}.md"
            assert not md_path.exists()
            archived_md = Path(td) / "archived" / f"{task_id}.md"
            assert archived_md.exists()

            # Removed from workflows.jsonl
            records = checkpoint._read_jsonl(Path(td))
            assert len(records) == 0

            # Present in archive.jsonl with status=closed
            archive = checkpoint._read_archive(Path(td))
            assert len(archive) == 1
            assert archive[0]["id"] == task_id
            assert archive[0]["status"] == "closed"
            assert "closed_at" in archive[0]

    def test_close_moves_history(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Hist Close", "--note", "note", scope_dir=td)
            task_id = result.stdout.strip().split("\n")[0].split()[-1]
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                "## Current\n<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive\n\n"
                "## Decisions\n\n"
                "## Next\nrun tests\n\n"
                "## Key Files\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            # Archive a stream so a history file is created.
            assert _run("archive-stream", task_id, "s1", "--yes", scope_dir=td).returncode == 0
            hist_path = Path(td) / f"{task_id}_history.md"
            assert hist_path.exists()

            # Fill Completed so close --yes passes validation.
            md = (Path(td) / f"{task_id}.md").read_text(encoding="utf-8")
            md = md.replace("## Completed\n\n", "## Completed\n\n" + ("x" * 100) + "\n\n", 1)
            (Path(td) / f"{task_id}.md").write_text(md, encoding="utf-8")

            result = _run("close", task_id, "--yes", scope_dir=td)
            assert result.returncode == 0
            assert not hist_path.exists()  # moved out of wf dir
            archived_hist = Path(td) / "archived" / f"{task_id}_history.md"
            assert archived_hist.exists()
            archived_md = Path(td) / "archived" / f"{task_id}.md"
            assert archived_md.exists()

    def test_close_dry_run_lists_history(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Hist Dry", "--note", "note", scope_dir=td)
            task_id = result.stdout.strip().split("\n")[0].split()[-1]
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                "## Current\n<!-- stream:start:s1 -->\nwork\n<!-- stream:end:s1 -->\nactive\n\n"
                "## Decisions\n\n"
                "## Next\nrun tests\n\n"
                "## Key Files\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            assert _run("archive-stream", task_id, "s1", "--yes", scope_dir=td).returncode == 0

            result = _run("close", task_id, scope_dir=td)
            assert result.returncode == 0
            assert "_history.md" in result.stdout
            assert "Archive actions" in result.stdout

    def test_close_no_history_no_change(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "No Hist", "--note", "note", scope_dir=td)
            task_id = result.stdout.strip().split("\n")[0].split()[-1]
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                "## Current\nworking\n\n"
                "## Decisions\n\n"
                "## Next\nrun tests\n\n"
                "## Key Files\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            assert not (Path(td) / f"{task_id}_history.md").exists()

            result = _run("close", task_id, "--yes", scope_dir=td)
            assert result.returncode == 0
            # No history file was ever created; close behaves as before.
            assert not (Path(td) / "archived" / f"{task_id}_history.md").exists()
            assert (Path(td) / "archived" / f"{task_id}.md").exists()

    def test_list_closed_shows_archived(self):
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Archive List Test", "--note", "Archive List Test note", scope_dir=td)
            lines = result.stdout.strip().split("\n")
            task_id = lines[0].split()[-1]
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                + "## Current\nDone\n\n## Next\nNothing\n\n## Key Files\nf.py\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            _run("close", task_id, "--yes", scope_dir=td)

            # list (pending) should be empty
            result = _run("list", scope_dir=td)
            assert "No tasks" in result.stdout

            # list --closed should show the archived task
            result = _run("list", "--closed", scope_dir=td)
            assert "Archived tasks" in result.stdout
            assert task_id in result.stdout
            assert "closed" in result.stdout.lower()

    def test_list_closed_dedupes_duplicate_ids(self):
        """If archive.jsonl has duplicate ids (manual edit corruption),
        list --closed shows each id only once."""
        with tempfile.TemporaryDirectory() as td:
            result = _run("create", "Dedup Test", "--note", "Dedup Test note", scope_dir=td)
            task_id = result.stdout.strip().split("\n")[0].split()[-1]
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                + "## Current\nDone\n\n## Next\nNothing\n\n## Key Files\nf.py\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            _run("close", task_id, "--yes", scope_dir=td)

            # Corrupt archive.jsonl: duplicate the record (simulate manual edit error)
            archive_path = Path(td) / "archive.jsonl"
            original_line = archive_path.read_text(encoding="utf-8")
            archive_path.write_text(original_line + original_line, encoding="utf-8")

            result = _run("list", "--closed", scope_dir=td)
            # task_id should appear exactly once in the listing
            count = result.stdout.count(task_id)
            assert count == 1, f"expected task_id once, got {count}\n{result.stdout}"

    def test_close_keeps_source_docs(self):
        with tempfile.TemporaryDirectory() as td:
            # Set up a fake project root with a source doc
            project_root = Path(td) / "proj"
            docs_dir = project_root / "docs" / "superpowers" / "plans"
            docs_dir.mkdir(parents=True)
            doc = docs_dir / "2026-07-29-test-plan.md"
            doc.write_text("# plan", encoding="utf-8")

            result = _run("create", "Doc Keep Test", "--note", "Doc Keep Test note", scope_dir=td)
            task_id = result.stdout.strip().split("\n")[0].split()[-1]
            content = (
                "## Completed\n\n" + ("x" * 100) + "\n\n"
                + "## Current\nDone\n\n## Next\nNothing\n\n## Key Files\nf.py\n"
            )
            (Path(td) / f"{task_id}.md").write_text(content, encoding="utf-8")
            # Attach the source doc via pause, then close
            _run("pause", task_id, "--source-docs", str(doc), scope_dir=td)
            _run("close", task_id, "--yes", scope_dir=td)

            # source doc must still exist (not deleted)
            assert doc.exists(), "source doc should be kept after close"

