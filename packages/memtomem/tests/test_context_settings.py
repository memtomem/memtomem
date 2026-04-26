"""Tests for context/settings.py — canonical → runtime settings.json fan-out (Phase D).

Uses record-format hooks (Claude Code ≥ 2.1.104):
    {"hooks": {"EventName": [{"matcher": "...", "hooks": [...]}]}}
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    diff_settings,
    generate_all_settings,
)


# ── Helpers ────────────────────────────────────────────────────────


def _rule(matcher: str = "", command: str = "echo ok", timeout: int = 5000) -> dict:
    """Build a single hook rule in record format."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    }


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def claude_home(tmp_path, monkeypatch):
    """Redirect HOME so writes target a temp dir.  Creates ``~/.claude/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    (fake_home / ".claude").mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    return fake_home


@pytest.fixture
def claude_home_missing(tmp_path, monkeypatch):
    """Redirect HOME **without** creating ``~/.claude/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    return fake_home


def _make_canonical_settings(project_root, content: dict | str | None = None):
    """Write ``.memtomem/settings.json`` with the given content."""
    if content is None:
        content = {"hooks": {}}
    path = project_root / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, str):
        path.write_text(content, encoding="utf-8")
    else:
        path.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")
    return path


def _read_target(claude_home) -> dict:
    """Read the merged settings.json from the fake HOME."""
    path = claude_home / ".claude" / "settings.json"
    return json.loads(path.read_text(encoding="utf-8"))


# ── Merge tests ─────────────────────────────────────────────────────


class TestClaudeSettingsMergeEmpty:
    """No existing settings.json — merge creates a new file."""

    def test_creates_file_from_canonical(self, claude_home, tmp_path):
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo ok")]}},
        )
        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert "PostToolUse" in written["hooks"]
        assert len(written["hooks"]["PostToolUse"]) == 1
        assert written["hooks"]["PostToolUse"][0]["matcher"] == "Write"


class TestClaudeSettingsMergeSemantic:
    """Existing keys not owned by memtomem are preserved semantically."""

    def test_preserves_unrelated_keys(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        existing = {
            "permissions": {"allow": ["Read", "Edit"]},
            "env": {"FOO": "bar"},
            "mcpServers": {"example": {"command": "echo"}},
        }
        target.write_text(json.dumps(existing, indent=4) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert written["permissions"] == existing["permissions"]
        assert written["env"] == existing["env"]
        assert written["mcpServers"] == existing["mcpServers"]
        assert written["hooks"] == {}


class TestClaudeSettingsMergeAdditive:
    """Existing user rules are preserved; memtomem rules are appended."""

    def test_appends_without_touching_user_rules(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("", "say done")
        target.write_text(json.dumps({"hooks": {"Stop": [user_rule]}}) + "\n", encoding="utf-8")

        mm_rule = _rule("Write", "mm index")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [mm_rule]}})

        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert written["hooks"]["Stop"] == [user_rule]
        assert written["hooks"]["PostToolUse"] == [mm_rule]

    def test_appends_rule_to_same_event(self, claude_home, tmp_path):
        """Multiple rules under the same event with different matchers."""
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("Bash", "echo user")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule]}}) + "\n", encoding="utf-8"
        )

        mm_rule = _rule("Write", "mm index")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [mm_rule]}})

        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert len(written["hooks"]["PostToolUse"]) == 2
        assert written["hooks"]["PostToolUse"][0] == user_rule
        assert written["hooks"]["PostToolUse"][1] == mm_rule


class TestClaudeSettingsMergeConflict:
    """Same (event, matcher) → skip + emit warning.  User's rule wins."""

    def test_user_rule_wins_on_matcher_collision(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        user_rule = _rule("Write", "echo custom")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule]}}) + "\n", encoding="utf-8"
        )

        mm_rule = _rule("Write", "mm index")
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [mm_rule]}})

        results = generate_all_settings(tmp_path)
        r = results["claude_settings"]
        assert r.status == "ok"
        assert len(r.warnings) == 1

        written = _read_target(claude_home)
        assert len(written["hooks"]["PostToolUse"]) == 1
        assert written["hooks"]["PostToolUse"][0] == user_rule  # user wins

    def test_identical_rule_is_silently_skipped(self, claude_home, tmp_path):
        """If the user's rule is byte-identical, no warning is emitted."""
        rule = _rule("Write", "mm index")
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {"PostToolUse": [rule]}}) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [rule]}})
        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].warnings == []


class TestClaudeSettingsMergeWarningContent:
    """Warning messages must contain the rule label, reason, and remediation."""

    def test_warning_includes_required_parts(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [_rule("Write", "old")]}}) + "\n", encoding="utf-8"
        )

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "new")]}},
        )
        results = generate_all_settings(tmp_path)
        w = results["claude_settings"].warnings[0]

        # (a) rule label
        assert "PostToolUse:Write" in w
        # (b) reason
        assert "already exists" in w
        # (c) concrete remediation step
        assert "remove" in w
        assert "mm context sync --include=settings" in w


class TestClaudeSettingsMergeMalformed:
    """Existing settings.json is not valid JSON → skip, don't crash."""

    def test_malformed_target_returns_error(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text('{"hooks":{', encoding="utf-8")  # truncated

        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path)
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not valid JSON" in r.reason

        # File should NOT have been modified
        assert target.read_text(encoding="utf-8") == '{"hooks":{'

    def test_malformed_canonical_returns_error(self, claude_home, tmp_path):
        _make_canonical_settings(tmp_path, "{bad json")
        results = generate_all_settings(tmp_path)
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not valid JSON" in r.reason


class TestClaudeSettingsMergeConcurrent:
    """Mtime changed between read and write → abort."""

    def test_aborts_on_mtime_change(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo")]}},
        )

        import memtomem.context.settings as settings_mod

        orig_read_with_mtime = settings_mod._read_with_mtime

        def patched_read_with_mtime(path):
            result = orig_read_with_mtime(path)
            if path == target:
                target.write_text(
                    json.dumps({"hooks": {}, "_bumped": True}) + "\n", encoding="utf-8"
                )
            return result

        import unittest.mock

        with unittest.mock.patch.object(settings_mod, "_read_with_mtime", patched_read_with_mtime):
            results = generate_all_settings(tmp_path)

        r = results["claude_settings"]
        assert r.status == "aborted"
        assert "modified by another process" in r.reason


class TestClaudeSettingsAtomicWrite:
    """_write_json is atomic — a crash between open() and replace() leaves the
    pre-existing settings.json untouched instead of producing a truncated file
    that reloads as 'no hooks configured' (issue #275)."""

    def test_crash_mid_replace_preserves_old_settings(self, claude_home, tmp_path, monkeypatch):
        target = claude_home / ".claude" / "settings.json"
        original = {"hooks": {"PostToolUse": [_rule("Write", "echo original")]}}
        target.write_text(json.dumps(original, indent=2) + "\n", encoding="utf-8")

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Edit", "echo new")]}},
        )

        def _boom(*_args, **_kwargs):
            raise OSError("simulated crash mid-replace")

        monkeypatch.setattr("memtomem.context._atomic.os.replace", _boom)

        with pytest.raises(OSError, match="simulated crash"):
            generate_all_settings(tmp_path)

        # Old file survives, no .tmp sibling leaked.
        assert json.loads(target.read_text(encoding="utf-8")) == original
        siblings = [p for p in target.parent.iterdir() if p.name.startswith(".settings.json.")]
        assert siblings == []

    def test_mode_is_0o600(self, claude_home, tmp_path):
        import stat as _stat

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Edit", "echo")]}},
        )
        generate_all_settings(tmp_path)

        target = claude_home / ".claude" / "settings.json"
        assert _stat.S_IMODE(target.stat().st_mode) == 0o600


class TestClaudeSettingsNoClaudeCodeInstalled:
    """``~/.claude/`` does not exist → skip, never create it."""

    def test_skips_when_claude_not_installed(self, claude_home_missing, tmp_path):
        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path)
        r = results["claude_settings"]
        assert r.status == "skipped"
        assert "not installed" in r.reason

        # Must NOT have created ~/.claude/
        assert not (claude_home_missing / ".claude").exists()


# ── Diff tests ──────────────────────────────────────────────────────


class TestClaudeSettingsDryRun:
    """diff_settings reports merge plan without writing."""

    def test_reports_missing_target(self, claude_home, tmp_path):
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write")]}},
        )
        results = diff_settings(tmp_path)
        assert results["claude_settings"].status == "missing target"

    def test_reports_in_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        content = {"hooks": {"PostToolUse": [_rule("Write")]}}
        target.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, content)
        results = diff_settings(tmp_path)
        assert results["claude_settings"].status == "in sync"

    def test_reports_out_of_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write")]}})
        results = diff_settings(tmp_path)
        assert results["claude_settings"].status == "out of sync"

    def test_does_not_write(self, claude_home, tmp_path):
        """diff must never modify the target file."""
        target = claude_home / ".claude" / "settings.json"
        original = json.dumps({"hooks": {}}) + "\n"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write")]}})
        diff_settings(tmp_path)
        assert target.read_text(encoding="utf-8") == original


# ── CLI integration ─────────────────────────────────────────────────


class TestClaudeSettingsCliInclude:
    """``mm context generate --include=settings`` end-to-end via CliRunner."""

    def test_generate_includes_settings(self, claude_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a minimal .git so _find_project_root works
        (tmp_path / ".git").mkdir()

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo test")]}},
        )

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings"])
        assert result.exit_code == 0
        assert "Settings" in result.output or "settings" in result.output

        # Verify the file was actually written
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "PostToolUse" in written.get("hooks", {})

    def test_include_settings_validation(self):
        """Unknown include values are rejected."""
        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=bogus"])
        assert result.exit_code != 0
        assert "Unknown" in result.output or "bogus" in result.output


class TestClaudeSettingsHostWritePrompt:
    """``mm context {generate,sync} --include=settings`` confirms before
    mutating files outside the project root (e.g. ``~/.claude/settings.json``).
    Without confirmation users couldn't tell that running the command from a
    worktree silently edited their real home directory.

    The default ``claude_home`` fixture places fake $HOME *inside* tmp_path,
    which would defeat the ``is_relative_to(root)`` check; these tests use a
    sibling project dir so the project and the fake home don't overlap.
    """

    def _setup(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        _make_canonical_settings(
            project,
            {"hooks": {"PostToolUse": [_rule("Write", "echo test")]}},
        )
        return project

    def test_sync_prompts_before_host_write(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        # Decline the prompt → no file written.
        result = runner.invoke(context, ["sync", "--include=settings"], input="n\n")
        assert result.exit_code == 0
        assert "modify the following files outside this project" in result.output
        assert str(claude_home / ".claude" / "settings.json") in result.output
        assert "Skipped settings sync (declined)" in result.output
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_sync_proceeds_when_user_confirms(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["sync", "--include=settings"], input="y\n")
        assert result.exit_code == 0
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "PostToolUse" in written["hooks"]

    def test_sync_yes_flag_bypasses_prompt(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        # No stdin input — would block on prompt without --yes.
        result = runner.invoke(context, ["sync", "--include=settings", "--yes"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
        assert (claude_home / ".claude" / "settings.json").is_file()

    def test_generate_prompts_before_host_write(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings"], input="n\n")
        assert result.exit_code == 0
        assert "modify the following files outside this project" in result.output
        assert "Skipped settings sync (declined)" in result.output
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_generate_yes_flag_bypasses_prompt(self, claude_home, tmp_path, monkeypatch):
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings", "-y"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
        assert (claude_home / ".claude" / "settings.json").is_file()

    def test_no_prompt_when_canonical_missing(self, claude_home, tmp_path, monkeypatch):
        """No canonical settings → nothing to write → no prompt, no error."""
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        monkeypatch.chdir(project)

        from memtomem.context.parser import CONTEXT_FILENAME

        (project / ".memtomem").mkdir(exist_ok=True)
        (project / CONTEXT_FILENAME).write_text("# Project\n", encoding="utf-8")

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        # No input — would deadlock if a prompt fired.
        result = runner.invoke(context, ["sync", "--include=settings"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_no_prompt_when_runtime_unavailable(self, claude_home_missing, tmp_path, monkeypatch):
        """Runtime not installed → generator skips → no prompt."""
        project = self._setup(tmp_path)
        monkeypatch.chdir(project)

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["sync", "--include=settings"])
        assert result.exit_code == 0
        assert "modify the following files outside this project" not in result.output
