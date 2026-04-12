"""Tests for context/settings.py — canonical → runtime settings.json fan-out (Phase D)."""

from __future__ import annotations

import json
import os
import re

import pytest
from click.testing import CliRunner

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    ClaudeSettingsGenerator,
    SETTINGS_GENERATORS,
    SettingsSyncResult,
    diff_settings,
    generate_all_settings,
)


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
        content = {"hooks": []}
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
            {"hooks": [{"event": "PostToolUse", "name": "mm-log", "command": "echo ok"}]},
        )
        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert len(written["hooks"]) == 1
        assert written["hooks"][0]["name"] == "mm-log"


class TestClaudeSettingsMergeSemantic:
    """Existing keys not owned by memtomem are preserved semantically.

    Formatting (key order, indentation) is intentionally not preserved —
    see the "Resolved design decisions" in the Phase D plan.
    """

    def test_preserves_unrelated_keys(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        existing = {
            "permissions": {"allow": ["Read", "Edit"]},
            "env": {"FOO": "bar"},
            "mcpServers": {"example": {"command": "echo"}},
        }
        target.write_text(json.dumps(existing, indent=4) + "\n")

        _make_canonical_settings(tmp_path, {"hooks": []})
        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert written["permissions"] == existing["permissions"]
        assert written["env"] == existing["env"]
        assert written["mcpServers"] == existing["mcpServers"]
        assert written["hooks"] == []


class TestClaudeSettingsMergeAdditive:
    """Existing user hooks are preserved; memtomem hooks are appended."""

    def test_appends_without_touching_user_hooks(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        user_hook = {"event": "Notification", "name": "my-notify", "command": "say done"}
        target.write_text(json.dumps({"hooks": [user_hook]}) + "\n")

        mm_hook = {"event": "PostToolUse", "name": "mm-watch", "command": "mm watchdog"}
        _make_canonical_settings(tmp_path, {"hooks": [mm_hook]})

        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"

        written = _read_target(claude_home)
        assert len(written["hooks"]) == 2
        assert written["hooks"][0] == user_hook
        assert written["hooks"][1] == mm_hook


class TestClaudeSettingsMergeConflict:
    """Name collision → skip + emit warning.  User's hook wins."""

    def test_user_hook_wins_on_name_collision(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        user_hook = {"event": "Notification", "name": "mm-watch", "command": "custom"}
        target.write_text(json.dumps({"hooks": [user_hook]}) + "\n")

        mm_hook = {"event": "PostToolUse", "name": "mm-watch", "command": "mm watchdog"}
        _make_canonical_settings(tmp_path, {"hooks": [mm_hook]})

        results = generate_all_settings(tmp_path)
        r = results["claude_settings"]
        assert r.status == "ok"
        assert len(r.warnings) == 1

        written = _read_target(claude_home)
        assert len(written["hooks"]) == 1
        assert written["hooks"][0] == user_hook  # user wins

    def test_identical_hook_is_silently_skipped(self, claude_home, tmp_path):
        """If the user's hook is byte-identical, no warning is emitted."""
        hook = {"event": "PostToolUse", "name": "mm-watch", "command": "mm watchdog"}
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": [hook]}) + "\n")

        _make_canonical_settings(tmp_path, {"hooks": [hook]})
        results = generate_all_settings(tmp_path)
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].warnings == []


class TestClaudeSettingsMergeWarningContent:
    """Warning messages must contain the hook name, reason, and remediation."""

    def test_warning_includes_required_parts(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": [{"name": "mm-watch", "x": 1}]}) + "\n")

        _make_canonical_settings(tmp_path, {"hooks": [{"name": "mm-watch", "x": 2}]})
        results = generate_all_settings(tmp_path)
        w = results["claude_settings"].warnings[0]

        # (a) hook name verbatim
        assert "'mm-watch'" in w
        # (b) reason
        assert "already exists" in w
        # (c) concrete remediation step
        assert "rename or remove" in w
        assert "mm context sync --include=settings" in w


class TestClaudeSettingsMergeMalformed:
    """Existing settings.json is not valid JSON → skip, don't crash."""

    def test_malformed_target_returns_error(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text('{"hooks":[', encoding="utf-8")  # truncated

        _make_canonical_settings(tmp_path, {"hooks": []})
        results = generate_all_settings(tmp_path)
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not valid JSON" in r.reason

        # File should NOT have been modified
        assert target.read_text() == '{"hooks":['

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
        target.write_text(json.dumps({"hooks": []}) + "\n")

        _make_canonical_settings(
            tmp_path,
            {"hooks": [{"name": "new-hook", "event": "PostToolUse", "command": "echo"}]},
        )

        # Monkey-patch os.stat to simulate mtime change after first read.
        # We wrap generate_all_settings so the target file is modified
        # between the read and the write.
        original_text = target.read_text()
        call_count = {"stat": 0}
        orig_stat = target.stat

        # Simulate: after the generator reads the file, something else
        # writes to it, bumping the mtime.
        import memtomem.context.settings as settings_mod

        orig_read_with_mtime = settings_mod._read_with_mtime

        def patched_read_with_mtime(path):
            result = orig_read_with_mtime(path)
            if path == target:
                # Bump mtime after read by re-writing with a small change
                target.write_text(json.dumps({"hooks": [], "_bumped": True}) + "\n")
            return result

        import unittest.mock

        with unittest.mock.patch.object(settings_mod, "_read_with_mtime", patched_read_with_mtime):
            results = generate_all_settings(tmp_path)

        r = results["claude_settings"]
        assert r.status == "aborted"
        assert "modified by another process" in r.reason


class TestClaudeSettingsNoClaudeCodeInstalled:
    """``~/.claude/`` does not exist → skip, never create it."""

    def test_skips_when_claude_not_installed(self, claude_home_missing, tmp_path):
        _make_canonical_settings(tmp_path, {"hooks": []})
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
        _make_canonical_settings(tmp_path, {"hooks": [{"name": "x", "e": "PostToolUse"}]})
        results = diff_settings(tmp_path)
        assert results["claude_settings"].status == "missing target"

    def test_reports_in_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        content = {"hooks": [{"name": "x", "e": "PostToolUse"}]}
        target.write_text(json.dumps(content, indent=2) + "\n")

        _make_canonical_settings(tmp_path, content)
        results = diff_settings(tmp_path)
        assert results["claude_settings"].status == "in sync"

    def test_reports_out_of_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": []}) + "\n")

        _make_canonical_settings(
            tmp_path, {"hooks": [{"name": "new", "e": "PostToolUse"}]}
        )
        results = diff_settings(tmp_path)
        assert results["claude_settings"].status == "out of sync"

    def test_does_not_write(self, claude_home, tmp_path):
        """diff must never modify the target file."""
        target = claude_home / ".claude" / "settings.json"
        original = json.dumps({"hooks": []}) + "\n"
        target.write_text(original)

        _make_canonical_settings(
            tmp_path, {"hooks": [{"name": "new", "e": "PostToolUse"}]}
        )
        diff_settings(tmp_path)
        assert target.read_text() == original


# ── CLI integration ─────────────────────────────────────────────────


class TestClaudeSettingsCliInclude:
    """``mm context generate --include=settings`` end-to-end via CliRunner."""

    def test_generate_includes_settings(self, claude_home, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        # Create a minimal .git so _find_project_root works
        (tmp_path / ".git").mkdir()

        _make_canonical_settings(
            tmp_path,
            {"hooks": [{"name": "test-hook", "event": "PostToolUse", "command": "echo"}]},
        )

        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=settings"])
        assert result.exit_code == 0
        assert "Settings" in result.output or "settings" in result.output

        # Verify the file was actually written
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text())
        assert any(h.get("name") == "test-hook" for h in written.get("hooks", []))

    def test_include_settings_validation(self):
        """Unknown include values are rejected."""
        from memtomem.cli.context_cmd import context

        runner = CliRunner()
        result = runner.invoke(context, ["generate", "--include=bogus"])
        assert result.exit_code != 0
        assert "Unknown" in result.output or "bogus" in result.output
