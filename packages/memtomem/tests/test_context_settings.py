"""Tests for context/settings.py — canonical → runtime settings.json fan-out (Phase D).

Uses record-format hooks (Claude Code ≥ 2.1.104):
    {"hooks": {"EventName": [{"matcher": "...", "hooks": [...]}]}}
"""

from __future__ import annotations

import json
import sys
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    ClaudeSettingsGenerator,
    resolve_scope_path,
    diff_settings,
    generate_all_settings,
    host_write_targets,
)
from memtomem.web.app import create_app
from .helpers import set_home


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
    set_home(monkeypatch, fake_home)
    return fake_home


@pytest.fixture
def claude_home_missing(tmp_path, monkeypatch):
    """Redirect HOME **without** creating ``~/.claude/``."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_home(monkeypatch, fake_home)
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
        results = generate_all_settings(tmp_path, scope="user")
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
        results = generate_all_settings(tmp_path, scope="user")
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

        results = generate_all_settings(tmp_path, scope="user")
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

        results = generate_all_settings(tmp_path, scope="user")
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

        results = generate_all_settings(tmp_path, scope="user")
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
        results = generate_all_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].warnings == []

    def test_dedup_when_user_has_multiple_same_matcher_rules(self, claude_home, tmp_path):
        """Pin: existing rules with the same matcher should not collapse during indexing.

        Claude Code allows two rules under the same event to share a matcher
        (or omit it). Earlier indexing keyed ``existing_by_matcher`` as
        ``dict[str, dict]``, so the second rule silently shadowed the first.
        A byte-identical contribution that matched the *first* rule then
        emitted a spurious warning by comparing against the second.
        """
        target = claude_home / ".claude" / "settings.json"
        existing_a = _rule("", "echo first")
        existing_b = _rule("", "echo second")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [existing_a, existing_b]}}) + "\n",
            encoding="utf-8",
        )

        # Contribution exactly matches the first user rule.
        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [existing_a]}})

        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "ok"
        assert r.warnings == []  # was 1 before the fix

        written = _read_target(claude_home)
        # Both user rules preserved verbatim, no contribution appended.
        assert written["hooks"]["PostToolUse"] == [existing_a, existing_b]


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
        results = generate_all_settings(tmp_path, scope="user")
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
        results = generate_all_settings(tmp_path, scope="user")
        r = results["claude_settings"]
        assert r.status == "error"
        assert "not valid JSON" in r.reason

        # File should NOT have been modified
        assert target.read_text(encoding="utf-8") == '{"hooks":{'

    def test_malformed_canonical_returns_error(self, claude_home, tmp_path):
        _make_canonical_settings(tmp_path, "{bad json")
        results = generate_all_settings(tmp_path, scope="user")
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
                # Two writes inside the same system-clock tick can report the
                # same ``st_mtime_ns`` on Windows (NTFS native resolution is
                # 100ns but ``WriteFile``-induced metadata updates use the
                # system clock, which often advances at ~15.6ms). Bump
                # explicitly so the simulated concurrent-writer is
                # distinguishable regardless of OS timer granularity — a
                # real-world second writer is naturally well above this
                # threshold, so this only covers the test-induced race.
                import os as _os

                st = target.stat()
                _os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        import unittest.mock

        with unittest.mock.patch.object(settings_mod, "_read_with_mtime", patched_read_with_mtime):
            results = generate_all_settings(tmp_path, scope="user")

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
            generate_all_settings(tmp_path, scope="user")

        # Old file survives, no .tmp sibling leaked.
        assert json.loads(target.read_text(encoding="utf-8")) == original
        siblings = [p for p in target.parent.iterdir() if p.name.startswith(".settings.json.")]
        assert siblings == []

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX file mode (stat.S_IMODE) — Windows ignores POSIX permission bits",
    )
    def test_mode_is_0o600(self, claude_home, tmp_path):
        import stat as _stat

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Edit", "echo")]}},
        )
        generate_all_settings(tmp_path, scope="user")

        target = claude_home / ".claude" / "settings.json"
        assert _stat.S_IMODE(target.stat().st_mode) == 0o600


class TestClaudeSettingsNoClaudeCodeInstalled:
    """``~/.claude/`` does not exist → skip, never create it."""

    def test_skips_when_claude_not_installed(self, claude_home_missing, tmp_path):
        _make_canonical_settings(tmp_path, {"hooks": {}})
        results = generate_all_settings(tmp_path, scope="user")
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
        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "missing target"

    def test_reports_in_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        content = {"hooks": {"PostToolUse": [_rule("Write")]}}
        target.write_text(json.dumps(content, indent=2) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, content)
        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "in sync"

    def test_reports_out_of_sync(self, claude_home, tmp_path):
        target = claude_home / ".claude" / "settings.json"
        target.write_text(json.dumps({"hooks": {}}) + "\n", encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write")]}})
        results = diff_settings(tmp_path, scope="user")
        assert results["claude_settings"].status == "out of sync"

    def test_does_not_write(self, claude_home, tmp_path):
        """diff must never modify the target file."""
        target = claude_home / ".claude" / "settings.json"
        original = json.dumps({"hooks": {}}) + "\n"
        target.write_text(original, encoding="utf-8")

        _make_canonical_settings(tmp_path, {"hooks": {"PostToolUse": [_rule("Write")]}})
        diff_settings(tmp_path, scope="user")
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


class TestGenerateAllSettingsHostWriteGate:
    """``generate_all_settings(allow_host_writes=False)`` — the library-level
    gate every front-end (CLI, MCP, Web) routes through. Without this gate
    living inside the I/O boundary the CLI confirm could be bypassed by any
    caller that imports the function directly (audit P0 review item 1)."""

    def _setup(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        canonical = project / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text(
            json.dumps({"hooks": {"PostToolUse": [_rule("Write", "echo test")]}}, indent=2) + "\n",
            encoding="utf-8",
        )
        return project

    def test_default_refuses_host_write(self, claude_home, tmp_path):
        """Default ``allow_host_writes=False`` → status=needs_confirmation,
        no file written."""
        project = self._setup(tmp_path)
        results = generate_all_settings(project, scope="user")
        r = results["claude_settings"]
        assert r.status == "needs_confirmation"
        target = claude_home / ".claude" / "settings.json"
        assert str(target) in r.reason
        assert r.target == target
        assert not target.exists()

    def test_allow_host_writes_true_proceeds(self, claude_home, tmp_path):
        """``allow_host_writes=True`` → previous behavior, file written."""
        project = self._setup(tmp_path)
        results = generate_all_settings(project, scope="user", allow_host_writes=True)
        r = results["claude_settings"]
        assert r.status == "ok"
        target = claude_home / ".claude" / "settings.json"
        assert target.is_file()
        written = json.loads(target.read_text(encoding="utf-8"))
        assert "PostToolUse" in written["hooks"]

    def test_diff_unaffected_by_gate(self, claude_home, tmp_path):
        """``diff_settings`` is read-only — host-write gate must not make it
        report ``needs_confirmation``."""
        project = self._setup(tmp_path)
        results = diff_settings(project, scope="user")
        assert results["claude_settings"].status in {"in sync", "out of sync", "missing target"}

    def test_no_runtime_installed_skips_not_needs_confirmation(self, claude_home_missing, tmp_path):
        project = self._setup(tmp_path)
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "skipped"

    def test_no_canonical_skips_not_needs_confirmation(self, claude_home, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "skipped"

    def test_host_write_targets_lists_pending_paths(self, claude_home, tmp_path):
        """``host_write_targets()`` mirrors what ``generate_all_settings``
        would refuse — used by every front-end to surface the pending
        paths to the user."""
        project = self._setup(tmp_path)
        pending = host_write_targets(project, scope="user")
        target = claude_home / ".claude" / "settings.json"
        assert pending == [target]

    def test_host_write_targets_empty_when_no_canonical(self, claude_home, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        assert host_write_targets(project, scope="user") == []

    def test_host_write_targets_empty_when_runtime_missing(self, claude_home_missing, tmp_path):
        project = self._setup(tmp_path)
        assert host_write_targets(project, scope="user") == []

    @pytest.mark.requires_symlinks
    def test_symlink_target_is_treated_as_host_write(self, claude_home, tmp_path):
        """A symlink whose project-relative path *appears* under the project
        root must be classified by the resolved location (review item 4):
        ``Path.resolve()`` follows the symlink, so the gate still trips."""
        project = self._setup(tmp_path)
        # Symlink <project>/.claude → claude_home/.claude
        (claude_home / ".claude").mkdir(exist_ok=True)
        (project / ".claude").symlink_to(claude_home / ".claude")

        # The generator's target_file still computes Path.home() / .claude /
        # settings.json (the host path), and that resolves outside ``project``.
        results = generate_all_settings(project, scope="user")
        assert results["claude_settings"].status == "needs_confirmation"


# ── ADR-0010 §3 hooks.target_scope plumbing (issue #870) ────────────────────


class TestTargetScopeResolution:
    """6 path resolutions: 3 scope values × 2 entry points.

    Pins ADR-0010 §3 path math at both the resolver function and the
    end-to-end ``generate_all_settings`` call, so a regression in either
    layer surfaces. Mutation-validation per
    ``feedback_pin_test_mutation_validation``: swap the resolver's
    ``"user"`` branch return and at least one of these tests must fail.
    """

    @pytest.mark.parametrize(
        "scope,expected_relpath",
        [
            ("user", None),  # special: lives under HOME, computed at call time
            ("project_shared", ".claude/settings.json"),
            ("project_local", ".claude/settings.local.json"),
        ],
    )
    def test_resolver_function(self, claude_home, tmp_path, scope, expected_relpath):
        path = resolve_scope_path(tmp_path, scope)
        if scope == "user":
            assert path == claude_home / ".claude" / "settings.json"
        else:
            assert path == tmp_path / expected_relpath

    @pytest.mark.parametrize(
        "scope,expected_relpath",
        [
            ("user", None),
            ("project_shared", ".claude/settings.json"),
            ("project_local", ".claude/settings.local.json"),
        ],
    )
    def test_generator_target_file(self, claude_home, tmp_path, scope, expected_relpath):
        gen = ClaudeSettingsGenerator()
        path = gen.target_file(tmp_path, scope)
        if scope == "user":
            assert path == claude_home / ".claude" / "settings.json"
        else:
            assert path == tmp_path / expected_relpath

    @pytest.mark.parametrize(
        "scope,relpath",
        [
            ("project_shared", ".claude/settings.json"),
            ("project_local", ".claude/settings.local.json"),
        ],
    )
    def test_generate_writes_to_resolved_project_tier(self, claude_home, tmp_path, scope, relpath):
        """End-to-end: project-tier scopes write under the project root and
        do *not* touch ``~/.claude/settings.json``."""
        (tmp_path / ".claude").mkdir(exist_ok=True)
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo project")]}},
        )

        results = generate_all_settings(tmp_path, scope=scope, allow_host_writes=False)
        assert results["claude_settings"].status == "ok"
        assert (tmp_path / relpath).is_file()
        # The user-tier path must remain untouched.
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_generate_writes_to_user_tier_with_user_scope(self, claude_home, tmp_path):
        """End-to-end: ``scope="user"`` lands the merge at
        ``~/.claude/settings.json`` (the existing-install behavior)."""
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo user")]}},
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["claude_settings"].status == "ok"
        assert (claude_home / ".claude" / "settings.json").is_file()
        # No project-tier file written.
        assert not (tmp_path / ".claude" / "settings.json").exists()
        assert not (tmp_path / ".claude" / "settings.local.json").exists()


class TestIsAvailableLoosened:
    """ADR-0010 §3: loosen ``is_available`` so the tile shows up if Claude
    Code has *any* settings home for this project (user-tier or
    project-tier). Pins the case the loosening is for: the user has only
    project-local settings."""

    def test_user_only(self, claude_home, tmp_path):
        gen = ClaudeSettingsGenerator()
        assert gen.is_available(tmp_path) is True

    def test_project_only(self, claude_home_missing, tmp_path):
        gen = ClaudeSettingsGenerator()
        # Only project tier exists (with the local-tier settings file).
        (tmp_path / ".claude").mkdir()
        (tmp_path / ".claude" / "settings.local.json").write_text("{}", encoding="utf-8")
        assert gen.is_available(tmp_path) is True

    def test_neither_returns_false(self, claude_home_missing, tmp_path):
        gen = ClaudeSettingsGenerator()
        assert gen.is_available(tmp_path) is False


class TestDefaultScopePreservation:
    """Pin: an install with no ``hooks.target_scope`` config — and no env
    var override — defaults to ``user`` and writes to
    ``~/.claude/settings.json``. This is the contract for "zero behavior
    change for existing installs" in ADR-0010 §2."""

    def test_default_scope_writes_to_user_home(self, claude_home, tmp_path, monkeypatch):
        from memtomem.config import Mem2MemConfig

        # Deterministic env: drop any host-shell scope override.
        monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)
        # ``feedback_path_home_cross_platform``: also pin USERPROFILE on
        # Windows so the test is deterministic across platforms.
        monkeypatch.setenv("USERPROFILE", str(claude_home))

        cfg = Mem2MemConfig()
        assert cfg.hooks.target_scope == "user"

        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write")]}},
        )
        results = generate_all_settings(
            tmp_path, scope=cfg.hooks.target_scope, allow_host_writes=True
        )
        assert results["claude_settings"].status == "ok"
        assert results["claude_settings"].target == claude_home / ".claude" / "settings.json"


class TestCliScopeFlag:
    """``mm context sync --scope=project_local`` writes to
    ``<project>/.claude/settings.local.json`` and leaves
    ``~/.claude/settings.json`` untouched. The codex-review regression
    pin: per-invocation override flows end-to-end through the click
    pipeline (``_SCOPE_OPTION`` → ``_resolve_cli_scope`` → both
    ``_confirm_settings_host_writes`` and ``_print_settings_generate``).
    """

    def test_sync_with_scope_project_local(self, claude_home, tmp_path, monkeypatch):
        # ``mm context sync`` walks up from cwd; chdir into the project so
        # ``_find_project_root`` lands here.
        (tmp_path / ".git").mkdir()
        (tmp_path / ".claude").mkdir()
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo flag")]}},
        )
        monkeypatch.chdir(tmp_path)

        from memtomem.cli.context_cmd import sync_cmd

        result = CliRunner().invoke(
            sync_cmd, ["--include=settings", "--scope=project_local", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".claude" / "settings.local.json").is_file()
        assert not (claude_home / ".claude" / "settings.json").exists()

    def test_generate_with_scope_project_local(self, claude_home, tmp_path, monkeypatch):
        """Symmetric pin for ``mm context generate --scope=…``. Both commands
        share ``_SCOPE_OPTION`` but a future drift in only one of them would
        slip past a sync-only test (codex review test-gap)."""
        (tmp_path / ".git").mkdir()
        (tmp_path / ".claude").mkdir()
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo gen-flag")]}},
        )
        monkeypatch.chdir(tmp_path)

        from memtomem.cli.context_cmd import generate_cmd

        result = CliRunner().invoke(
            generate_cmd, ["--include=settings", "--scope=project_local", "--yes"]
        )
        assert result.exit_code == 0, result.output
        assert (tmp_path / ".claude" / "settings.local.json").is_file()
        assert not (claude_home / ".claude" / "settings.json").exists()


class TestTargetScopeValidation:
    """Pin: an unknown ``hooks.target_scope`` is rejected at construction
    time by Pydantic's ``Literal`` validator. This is the safety net
    behind ``_resolve_scope_path`` raising ``ValueError`` only as a
    defense-in-depth — production code never reaches that branch
    because Pydantic catches the typo first (codex review #5)."""

    def test_garbage_scope_raises_validation_error(self):
        from pydantic import ValidationError

        from memtomem.config import HooksConfig

        with pytest.raises(ValidationError):
            HooksConfig(target_scope="garbage")

    def test_resolver_rejects_unknown_scope(self, tmp_path):
        from memtomem.context.settings import resolve_scope_path

        with pytest.raises(ValueError, match="Unknown target_scope"):
            resolve_scope_path(tmp_path, "garbage")


class TestResolveScopeMigrationFree:
    """Pin: ``_resolve_cli_scope`` and ``_resolve_mcp_scope`` MUST NOT
    trigger the auto-discover migration (per
    ``feedback_doctor_no_migration_loader``). Scope reads are read-only
    diagnostic surfaces; touching disk as a side effect is the same
    footgun PR #838 hit."""

    def test_cli_scope_resolver_does_not_invoke_migration(self, monkeypatch):
        from memtomem.cli import context_cmd

        called: list[bool] = []

        def fake_migrate(_cfg):
            called.append(True)

        monkeypatch.setattr("memtomem.config._migrate_auto_discover_once", fake_migrate)
        # Drop env override so config-load path runs fully.
        monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)

        scope = context_cmd._resolve_cli_scope(None)
        assert scope in ("user", "project_shared", "project_local")
        assert called == []

    def test_mcp_scope_resolver_does_not_invoke_migration(self, monkeypatch):
        from memtomem.server.tools import context as mcp_ctx

        called: list[bool] = []

        def fake_migrate(_cfg):
            called.append(True)

        monkeypatch.setattr("memtomem.config._migrate_auto_discover_once", fake_migrate)
        monkeypatch.delenv("MEMTOMEM_HOOKS__TARGET_SCOPE", raising=False)

        scope = mcp_ctx._resolve_mcp_scope()
        assert scope in ("user", "project_shared", "project_local")
        assert called == []


# ── HTTP-route contract tests (RFC #761 PR-2 — ADR-0001 §5 c2/c4) ────────────


class TestSettingsHttpLayer:
    """HTTP-route contract for ``/api/context/settings/*``.

    Pins the FastAPI surface that wires up ``settings_sync`` per
    ADR-0001 §5 c2 (round-trip, unidirectional shape) and §5 c4
    (conflict path covered — soft-abort response). Helper-level merge
    and mtime behavior is already covered by the merge / concurrent
    classes above; these two tests pin the route layer that production
    UI depends on.

    PR-3 of RFC #761 will move ``settings_sync`` from
    ``_DEV_ONLY_ROUTERS`` to ``_PROD_ROUTERS`` — these tests use
    ``mode="dev"`` to stay correct against current ``main`` and remain
    correct after that move (the router is mounted in either mode).
    """

    @pytest.fixture
    def app(self, claude_home, tmp_path):
        from memtomem.config import Mem2MemConfig

        application = create_app(lifespan=None, mode="dev")
        application.state.project_root = tmp_path
        application.state.storage = AsyncMock()
        # Real config so ``get_hooks_target_scope`` Depends can read
        # ``cfg.hooks.target_scope`` (default "user").
        application.state.config = Mem2MemConfig()
        application.state.search_pipeline = None
        application.state.index_engine = None
        application.state.embedder = None
        application.state.dedup_scanner = None
        application.state.last_reload_error = None
        return application

    @pytest.fixture
    async def client(self, app):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            yield c

    async def test_sync_route_round_trip_unidirectional(self, client, claude_home, tmp_path):
        """ADR-0001 §5 c2 — unidirectional round-trip via the route layer.

        Settings has no reverse-import API by design (additive merge
        cannot distinguish canonical-authored from user-authored
        entries), so the round-trip is: write canonical → POST sync
        route → GET diff confirms ``in_sync`` AND target file on disk
        preserves user-authored hooks + non-hook keys.
        """
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write|Edit", "echo canonical")]}},
        )
        target = claude_home / ".claude" / "settings.json"
        user_authored = {
            "permissions": {"allow": ["Read"]},
            "env": {"FOO": "bar"},
            "hooks": {
                "PreToolUse": [_rule("Bash", "echo user")],
            },
        }
        target.write_text(json.dumps(user_authored, indent=2) + "\n", encoding="utf-8")

        resp = await client.post(
            "/api/context/settings/sync",
            json={"allow_host_writes": True},
        )
        assert resp.status_code == 200
        results = resp.json()["results"]
        claude_result = next(r for r in results if r["name"] == "claude_settings")
        assert claude_result["status"] == "ok"

        diff = await client.get("/api/context/settings")
        assert diff.status_code == 200
        diff_body = diff.json()
        assert diff_body["status"] == "in_sync"
        assert any(
            h["event"] == "PostToolUse" and h["matcher"] == "Write|Edit"
            for h in diff_body["hooks"]["synced"]
        )

        merged = json.loads(target.read_text(encoding="utf-8"))
        assert merged["permissions"] == {"allow": ["Read"]}
        assert merged["env"] == {"FOO": "bar"}
        assert any(r.get("matcher") == "Bash" for r in merged["hooks"]["PreToolUse"])
        assert any(r.get("matcher") == "Write|Edit" for r in merged["hooks"]["PostToolUse"])

    async def test_resolve_route_returns_soft_abort_on_stale_mtime(
        self, client, claude_home, tmp_path, monkeypatch
    ):
        """ADR-0001 §5 c4 — soft-abort response pinned at the route layer.

        Mirrors the helper-level pattern from
        ``TestClaudeSettingsMergeConcurrent.test_aborts_on_mtime_change``
        but exercises ``POST /api/context/settings/resolve``: the
        route captures ``target_path.stat().st_mtime_ns`` before the
        load and rechecks before write. We bump mtime as a side effect
        of the load so the recheck mismatches → HTTP 200 + ``{"status":
        "aborted", "reason": <... modified by another process ...>,
        "mtime_ns": <current st_mtime_ns>}``.

        Asserts the same triplet the Skills/Commands/Agents conflict
        tests pin (status / no-write content / current mtime_ns echo)
        — without all three a regression that wrote the proposed rule
        before returning aborted, or dropped the mtime_ns echo from
        the envelope, would still pass.
        """
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo canonical")]}},
        )
        target = claude_home / ".claude" / "settings.json"
        target.write_text(
            json.dumps(
                {"hooks": {"PostToolUse": [_rule("Write", "echo user")]}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        from memtomem.web.routes import settings_sync as routes_sync_mod

        orig_load = routes_sync_mod._safe_load_json

        def bumping_load(path):
            result = orig_load(path)
            if path == target:
                # Same 1ms bump as the helper-level test — clears
                # Windows NTFS WriteFile mtime granularity (~15.6ms
                # tick) so the recheck reliably observes a mismatch.
                import os as _os

                st = path.stat()
                _os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))
            return result

        monkeypatch.setattr(routes_sync_mod, "_safe_load_json", bumping_load)

        resp = await client.post(
            "/api/context/settings/resolve",
            json={
                "event": "PostToolUse",
                "matcher": "Write",
                "action": "use_proposed",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "aborted"
        assert "modified by another process" in body["reason"]
        # mtime_ns echo — clients refresh local state without an extra
        # round-trip; matches Skills/Commands/Agents 409 envelopes.
        assert body["mtime_ns"] == str(target.stat().st_mtime_ns)
        # No-write content — pinned both ways: the original user rule
        # survives, and the canonical rule is *not* persisted to disk.
        # A regression that wrote before returning aborted would flip
        # the second assertion.
        on_disk = target.read_text(encoding="utf-8")
        assert "echo user" in on_disk
        assert "echo canonical" not in on_disk

    async def test_resolve_respects_target_scope(self, app, client, claude_home, tmp_path):
        """Codex blocker #2 regression pin: ``POST /settings-sync/resolve``
        must use ``hooks.target_scope`` from ``app.state.config``, not
        the hardcoded user-tier path. With ``target_scope="project_local"``
        a resolve must mutate ``<project>/.claude/settings.local.json``
        and leave ``~/.claude/settings.json`` untouched.

        Without the fix, ``_claude_target()`` was a parameterless helper
        that always returned ``Path.home() / .claude / settings.json``
        and the resolve handler would have written there regardless of
        the configured scope.
        """
        # Configure project_local scope on the live app state.
        app.state.config.hooks.target_scope = "project_local"

        # Seed canonical with a hook differing from the user-authored rule
        # so the route surfaces a conflict.
        _make_canonical_settings(
            tmp_path,
            {"hooks": {"PostToolUse": [_rule("Write", "echo canonical")]}},
        )

        # Project-local target: <project>/.claude/settings.local.json
        (tmp_path / ".claude").mkdir()
        project_local_target = tmp_path / ".claude" / "settings.local.json"
        project_local_target.write_text(
            json.dumps(
                {"hooks": {"PostToolUse": [_rule("Write", "echo user")]}},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        # User-tier target: ``~/.claude/settings.json`` — must remain
        # byte-identical after the resolve.
        user_target = claude_home / ".claude" / "settings.json"
        user_authored = (
            json.dumps(
                {"hooks": {"PreToolUse": [_rule("Bash", "echo untouched")]}},
                indent=2,
            )
            + "\n"
        )
        user_target.write_text(user_authored, encoding="utf-8")

        resp = await client.post(
            "/api/context/settings/resolve",
            json={"event": "PostToolUse", "matcher": "Write", "action": "use_proposed"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "ok", body

        # The project-local file mutated to the canonical rule.
        merged = json.loads(project_local_target.read_text(encoding="utf-8"))
        assert any(
            "echo canonical" in r["hooks"][0]["command"] for r in merged["hooks"]["PostToolUse"]
        )
        # The user-tier file was NOT touched.
        assert user_target.read_text(encoding="utf-8") == user_authored
