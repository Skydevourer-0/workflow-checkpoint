"""Tests for the workflow-checkpoint installers.

Claude: <skill>/.claude/install.py  -> ~/.claude/settings.json
Codex:  <skill>/.codex/install.py   -> $CODEX_HOME/hooks.json (default ~/.codex)
Legacy: scripts/install.py          -> forwards to one of the above.

Tests run installers as subprocesses with HOME / CODEX_HOME env overrides so
they operate on temporary files only.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent.parent
CLAUDE_INSTALL_PY = SKILL_DIR / ".claude" / "install.py"
CODEX_INSTALL_PY = SKILL_DIR / ".codex" / "install.py"
COMPAT_INSTALL_PY = SKILL_DIR / "scripts" / "install.py"

MATCHER = "startup|resume|clear|compact"


def _run(script, *args, home_dir: Path, codex_home_dir: Path = None):
    """Run an installer as subprocess with HOME/CODEX_HOME overridden."""
    env = os.environ.copy()
    env["HOME"] = str(home_dir)
    if codex_home_dir is not None:
        env["CODEX_HOME"] = str(codex_home_dir)
    return subprocess.run(
        [sys.executable, str(script), *args],
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


def _wc_hooks(cfg):
    """All SessionStart hooks whose flattened command+args mention
    workflow-checkpoint (handles exec-form hooks and legacy command strings)."""
    def _flat(h):
        return " ".join([h.get("command", ""), *(h.get("args") or [])])
    return [
        h
        for group in cfg.get("hooks", {}).get("SessionStart", [])
        for h in group.get("hooks", [])
        if "workflow-checkpoint" in _flat(h)
    ]


# ── Claude installer (.claude/install.py) ────────────────────────────────────


class TestClaudeInstaller:
    def test_install_adds_hook(self, tmp_path):
        settings_path = _create_settings(tmp_path, {})
        result = _run(CLAUDE_INSTALL_PY, home_dir=tmp_path)
        assert result.returncode == 0, f"stderr: {result.stderr}"

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        hooks = cfg.get("hooks", {})
        assert "SessionStart" in hooks
        entry = hooks["SessionStart"][0]
        assert entry["matcher"] == MATCHER
        hook_list = entry["hooks"]
        assert len(hook_list) >= 1

        hook = hook_list[0]
        # exec (args) form: `command` is the interpreter, script + args follow
        # in `args`. Never a shell command string (Git Bash strips backslashes).
        assert hook.get("type") == "command"
        assert hook.get("async") is False
        assert hook.get("command") == sys.executable
        args = hook.get("args") or []
        assert any("workflow-checkpoint" in a and "checkpoint.py" in a for a in args)
        assert "list" in args and "--hook" in args

    def test_install_creates_settings_if_missing(self, tmp_path):
        # New installer semantics: idempotent init — creates settings.json.
        result = _run(CLAUDE_INSTALL_PY, home_dir=tmp_path)
        assert result.returncode == 0
        settings_path = tmp_path / ".claude" / "settings.json"
        assert settings_path.exists()
        assert len(_wc_hooks(json.loads(settings_path.read_text(encoding="utf-8")))) == 1

    def test_install_idempotent(self, tmp_path):
        _create_settings(tmp_path, {})
        assert _run(CLAUDE_INSTALL_PY, home_dir=tmp_path).returncode == 0
        assert _run(CLAUDE_INSTALL_PY, home_dir=tmp_path).returncode == 0

        cfg = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        wc = _wc_hooks(cfg)
        assert len(wc) == 1, f"Expected 1 workflow-checkpoint hook, got {len(wc)}: {wc}"

    def test_install_removes_old_hook(self, tmp_path):
        old_cmd = "bash ~/.claude/skills/workflow-checkpoint/hooks/check-pending-tasks.sh"
        old_python_cmd = '"/usr/bin/python3" "~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py" list --hook'
        old_settings = {
            "hooks": {
                "SessionStart": [
                    {
                        "matcher": "startup|clear|compact",
                        "hooks": [
                            {"type": "command", "async": False, "command": old_cmd},
                            {"type": "command", "async": False, "command": old_python_cmd},
                        ],
                    }
                ]
            }
        }
        settings_path = _create_settings(tmp_path, old_settings)
        result = _run(CLAUDE_INSTALL_PY, home_dir=tmp_path)
        assert result.returncode == 0

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        wc = _wc_hooks(cfg)
        assert len(wc) == 1, f"old hooks must be replaced, got {len(wc)}: {wc}"
        assert not any(old_cmd in h.get("command", "") for h in wc)

    def test_install_preserves_other_hooks(self, tmp_path):
        other = {"type": "command", "async": False, "command": 'echo "other-skill"' }
        settings_path = _create_settings(tmp_path, {"hooks": {"SessionStart": [{"matcher": MATCHER, "hooks": [other]}]}})
        result = _run(CLAUDE_INSTALL_PY, home_dir=tmp_path)
        assert result.returncode == 0

        cfg = json.loads(settings_path.read_text(encoding="utf-8"))
        all_hooks = [h for g in cfg["hooks"]["SessionStart"] for h in g["hooks"]]
        assert any("other-skill" in h.get("command", "") for h in all_hooks)
        assert len(_wc_hooks(cfg)) == 1

    def test_dry_run_does_not_modify(self, tmp_path):
        result = _run(CLAUDE_INSTALL_PY, "--dry-run", home_dir=tmp_path)
        assert result.returncode == 0
        assert "[DRY-RUN]" in result.stdout
        assert not (tmp_path / ".claude" / "settings.json").exists()


# ── Codex installer (.codex/install.py) ──────────────────────────────────────


class TestCodexInstaller:
    def test_install_creates_hooks_json(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        result = _run(CODEX_INSTALL_PY, home_dir=tmp_path, codex_home_dir=codex_home)
        assert result.returncode == 0, f"stderr: {result.stderr}"

        hooks_path = codex_home / "hooks.json"
        assert hooks_path.exists()
        cfg = json.loads(hooks_path.read_text(encoding="utf-8"))
        entry = cfg["hooks"]["SessionStart"][0]
        assert entry["matcher"] == MATCHER
        assert len(_wc_hooks(cfg)) == 1

    def test_install_idempotent(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        assert _run(CODEX_INSTALL_PY, home_dir=tmp_path, codex_home_dir=codex_home).returncode == 0
        assert _run(CODEX_INSTALL_PY, home_dir=tmp_path, codex_home_dir=codex_home).returncode == 0

        cfg = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        assert len(_wc_hooks(cfg)) == 1

    def test_install_removes_old_and_keeps_other_events(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        hooks_path = codex_home / "hooks.json"
        old_wc = {"type": "command", "command": 'python "~/.claude/skills/workflow-checkpoint/scripts/checkpoint.py" list --hook'}
        other_ptu = {
            "matcher": "apply_patch",
            "hooks": [{"type": "command", "command": 'python "other-skill/scripts/sync.py" sync-and-hint'}],
        }
        hooks_path.write_text(
            json.dumps({"hooks": {"SessionStart": [{"matcher": "startup|clear|compact", "hooks": [old_wc]}], "PostToolUse": [other_ptu]}}),
            encoding="utf-8",
        )

        result = _run(CODEX_INSTALL_PY, home_dir=tmp_path, codex_home_dir=codex_home)
        assert result.returncode == 0

        cfg = json.loads(hooks_path.read_text(encoding="utf-8"))
        assert len(_wc_hooks(cfg)) == 1  # old replaced, not duplicated
        ptu_hooks = [h for g in cfg["hooks"].get("PostToolUse", []) for h in g["hooks"]]
        assert any("other-skill" in h.get("command", "") for h in ptu_hooks)

    def test_install_warns_on_inline_hooks_config(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        (codex_home / "config.toml").write_text("[hooks]\n", encoding="utf-8")
        result = _run(CODEX_INSTALL_PY, home_dir=tmp_path, codex_home_dir=codex_home)
        assert result.returncode == 0
        assert "warning" in result.stdout
        assert "[hooks]" in result.stdout

    def test_dry_run_does_not_modify(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        result = _run(CODEX_INSTALL_PY, "--dry-run", home_dir=tmp_path, codex_home_dir=codex_home)
        assert result.returncode == 0
        assert "[DRY-RUN]" in result.stdout
        assert not (codex_home / "hooks.json").exists()


# ── Legacy compat entry (scripts/install.py) ─────────────────────────────────


class TestCompatInstaller:
    def test_forwards_to_claude_installer(self, tmp_path):
        _create_settings(tmp_path, {})
        result = _run(COMPAT_INSTALL_PY, home_dir=tmp_path)
        assert result.returncode == 0
        cfg = json.loads((tmp_path / ".claude" / "settings.json").read_text(encoding="utf-8"))
        assert len(_wc_hooks(cfg)) == 1

    def test_forwards_to_codex_installer(self, tmp_path):
        codex_home = tmp_path / "codex"
        codex_home.mkdir()
        result = _run(COMPAT_INSTALL_PY, home_dir=tmp_path, codex_home_dir=codex_home)
        assert result.returncode == 0
        cfg = json.loads((codex_home / "hooks.json").read_text(encoding="utf-8"))
        assert len(_wc_hooks(cfg)) == 1

    def test_no_target_prints_usage(self, tmp_path):
        result = _run(COMPAT_INSTALL_PY, home_dir=tmp_path)
        assert result.returncode == 0
        assert "No target environment detected" in result.stdout

