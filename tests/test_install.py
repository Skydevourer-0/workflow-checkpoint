"""Tests for scripts/install.py — workflow-checkpoint SessionStart hook installer.

Tests use subprocess with HOME env var overridden to tmp_path so that install.py
operates on a temporary settings.json without touching the real one.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS_DIR = Path(__file__).resolve().parent.parent / "scripts"
INSTALL_PY = SCRIPTS_DIR / "install.py"


def _run(*args, home_dir: Path):
    """Run install.py as subprocess with HOME overridden, return CompletedProcess."""
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    return subprocess.run(
        [sys.executable, str(INSTALL_PY), *args],
        capture_output=True,
        text=True,
        timeout=30,
        env=env,
    )


def _create_settings(home_dir: Path, content: dict = None):
    """Create settings.json under home_dir/.claude/settings.json."""
    settings_dir = home_dir / ".claude"
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_path = settings_dir / "settings.json"
    if content is not None:
        settings_path.write_text(json.dumps(content, indent=2), encoding="utf-8")
    else:
        settings_path.write_text("{}\n", encoding="utf-8")
    return settings_path


def _create_dummy_checkpoint(home_dir: Path):
    """Create a dummy checkpoint.py under the expected skill path."""
    cp_dir = home_dir / ".claude" / "skills" / "workflow-checkpoint" / "scripts"
    cp_dir.mkdir(parents=True, exist_ok=True)
    cp_path = cp_dir / "checkpoint.py"
    cp_path.write_text("#!/usr/bin/env python3\nprint('dummy')\n", encoding="utf-8")
    return cp_path


# ── Dry-run tests ────────────────────────────────────────────────────────────


class TestDryRun:
    def test_dry_run_output(self, tmp_path):
        """--dry-run prints Python, checkpoint, hook cmd, settings paths and [DRY-RUN] notice."""
        result = _run("--dry-run", home_dir=tmp_path)
        assert result.returncode == 0
        assert "Python:" in result.stdout
        assert "Checkpoint:" in result.stdout
        assert "Hook cmd:" in result.stdout
        assert "Settings:" in result.stdout
        assert "[DRY-RUN]" in result.stdout

    def test_dry_run_does_not_modify(self, tmp_path):
        """--dry-run must NOT create or modify any settings file."""
        result = _run("--dry-run", home_dir=tmp_path)
        assert result.returncode == 0
        # No settings.json should exist after dry-run
        settings_path = tmp_path / ".claude" / "settings.json"
        assert not settings_path.exists()

    def test_dry_run_does_not_require_settings(self, tmp_path):
        """--dry-run succeeds even when settings.json does not exist."""
        # Do NOT create settings.json — dry-run should succeed regardless
        result = _run("--dry-run", home_dir=tmp_path)
        assert result.returncode == 0
        assert "[DRY-RUN]" in result.stdout


# ── Install tests ────────────────────────────────────────────────────────────


class TestInstall:
    def test_install_adds_hook(self, tmp_path):
        """Install adds SessionStart hook with a command containing workflow-checkpoint."""
        settings_path = _create_settings(tmp_path, {})
        _create_dummy_checkpoint(tmp_path)

        result = _run(home_dir=tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = cfg.get("hooks", {})
        assert "SessionStart" in hooks
        session_hooks = hooks["SessionStart"]
        assert len(session_hooks) >= 1

        entry = session_hooks[0]
        assert "hooks" in entry
        hook_list = entry["hooks"]
        assert len(hook_list) >= 1

        hook_cmd = hook_list[0].get("command", "")
        assert "workflow-checkpoint" in hook_cmd
        assert "checkpoint.py" in hook_cmd
        assert "list --hook" in hook_cmd

    def test_install_idempotent(self, tmp_path):
        """Running install twice produces exactly one workflow-checkpoint hook entry."""
        settings_path = _create_settings(tmp_path, {})
        _create_dummy_checkpoint(tmp_path)

        result1 = _run(home_dir=tmp_path)
        assert result1.returncode == 0

        result2 = _run(home_dir=tmp_path)
        assert result2.returncode == 0

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        hook_list = cfg["hooks"]["SessionStart"][0]["hooks"]
        wc_hooks = [h for h in hook_list if "workflow-checkpoint" in h.get("command", "")]
        assert len(wc_hooks) == 1, f"Expected 1 workflow-checkpoint hook, got {len(wc_hooks)}: {wc_hooks}"

    def test_install_removes_old_hook(self, tmp_path):
        """Old workflow-checkpoint hook style is replaced, not duplicated."""
        old_cmd = "bash ~/.claude/skills/workflow-checkpoint/hooks/check-pending-tasks.sh"
        old_settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|clear|compact",
                        "hooks": [
                            {
                                "type": "command",
                                "async": False,
                                "command": old_cmd,
                            }
                        ],
                    }
                ]
            }
        }
        settings_path = _create_settings(tmp_path, old_settings)
        _create_dummy_checkpoint(tmp_path)

        result = _run(home_dir=tmp_path)
        assert result.returncode == 0

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        hook_list = cfg["hooks"]["SessionStart"][0]["hooks"]

        # Old hook should be gone
        assert not any(old_cmd in h.get("command", "") for h in hook_list)

        # New hook should be present
        assert any("workflow-checkpoint" in h.get("command", "") for h in hook_list)

        # Only one workflow-checkpoint hook
        wc_hooks = [h for h in hook_list if "workflow-checkpoint" in h.get("command", "")]
        assert len(wc_hooks) == 1

    def test_install_no_settings(self, tmp_path):
        """Without settings.json, install exits with code 1 and prints error."""
        # Do NOT create settings.json
        result = _run(home_dir=tmp_path)
        assert result.returncode == 1
        assert "settings.json not found" in result.stdout

    def test_install_creates_matcher_on_empty(self, tmp_path):
        """When SessionStart list is empty, install creates entry with matcher."""
        settings_path = _create_settings(tmp_path, {})
        _create_dummy_checkpoint(tmp_path)

        _run(home_dir=tmp_path)

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        entry = cfg["hooks"]["SessionStart"][0]
        assert entry.get("matcher") == "startup|clear|compact"
        assert len(entry["hooks"]) == 1
