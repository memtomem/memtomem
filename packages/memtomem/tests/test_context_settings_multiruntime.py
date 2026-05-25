"""Tests for multi-runtime hook fan-out — Codex + Gemini settings generators.

Companion to ``test_context_settings.py`` (Claude). Pins the ADR-0010
multi-runtime extension: canonical ``.memtomem/settings.json`` (Claude-shaped
hooks record) fans out to Codex ``.codex/hooks.json`` (near-identity) and
Gemini ``.gemini/settings.json`` (event + tool-name remap, drop-with-warning
for anything that can't convert faithfully).

Mappings were verified against official docs (developers.openai.com/codex/hooks,
gemini-cli docs/hooks/writing-hooks.md).
"""

from __future__ import annotations

import json

import pytest

from memtomem.context.settings import (
    CANONICAL_SETTINGS_FILE,
    CodexSettingsGenerator,
    GeminiSettingsGenerator,
    diff_settings,
    generate_all_settings,
    host_write_targets,
)

from .helpers import set_home


def _rule(matcher: str = "", command: str = "echo ok", timeout: int = 5000) -> dict:
    """A single hook rule in canonical (Claude) record format."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": timeout}],
    }


def _canonical(project_root, hooks: dict) -> None:
    """Write ``.memtomem/settings.json`` with the given ``hooks`` record."""
    path = project_root / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


@pytest.fixture
def all_home(tmp_path, monkeypatch):
    """Redirect HOME and create ``~/.claude``, ``~/.codex``, ``~/.gemini`` so
    all three runtimes register as installed."""
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    for marker in (".claude", ".codex", ".gemini"):
        (fake_home / marker).mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


# ── Codex (near-identity) ───────────────────────────────────────────


class TestCodexGenerator:
    def test_target_file_scopes(self, tmp_path, all_home):
        gen = CodexSettingsGenerator()
        assert gen.target_file(tmp_path, "user") == all_home / ".codex" / "hooks.json"
        assert gen.target_file(tmp_path, "project_shared") == tmp_path / ".codex" / "hooks.json"
        # Codex has no project_local hooks target.
        assert gen.target_file(tmp_path, "project_local") is None

    def test_near_identity_fanout(self, tmp_path, all_home):
        """Supported events + Bash/Edit/Write matchers pass through verbatim."""
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "echo hi")]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["codex_settings"].status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        rule = written["hooks"]["PreToolUse"][0]
        assert rule["matcher"] == "Bash"  # NOT remapped — Codex accepts it natively
        assert rule["hooks"][0]["command"] == "echo hi"

    def test_unsupported_events_dropped_with_warning(self, tmp_path, all_home):
        """Events Codex lacks (Notification, SessionEnd) are dropped + warned."""
        _canonical(
            tmp_path,
            {
                "PreToolUse": [_rule("Bash", "ok")],
                "Notification": [_rule("", "n")],
                "SessionEnd": [_rule("", "e")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["codex_settings"]
        assert r.status == "ok"

        written = json.loads((all_home / ".codex" / "hooks.json").read_text(encoding="utf-8"))
        assert "PreToolUse" in written["hooks"]
        assert "Notification" not in written["hooks"]
        assert "SessionEnd" not in written["hooks"]
        assert any("Notification" in w for w in r.warnings)
        assert any("SessionEnd" in w for w in r.warnings)

    def test_additive_merge_preserves_user_codex_rules(self, tmp_path, all_home):
        target = all_home / ".codex" / "hooks.json"
        user_rule = _rule("apply_patch", "user codex")
        target.write_text(
            json.dumps({"hooks": {"PostToolUse": [user_rule]}}) + "\n", encoding="utf-8"
        )
        _canonical(tmp_path, {"PostToolUse": [_rule("Bash", "mm")]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["codex_settings"].status == "ok"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert user_rule in written["hooks"]["PostToolUse"]
        assert any(rr["matcher"] == "Bash" for rr in written["hooks"]["PostToolUse"])


# ── Gemini (event + tool-name remap) ─────────────────────────────────


class TestGeminiGenerator:
    def test_target_file_scopes(self, tmp_path, all_home):
        gen = GeminiSettingsGenerator()
        assert gen.target_file(tmp_path, "user") == all_home / ".gemini" / "settings.json"
        assert gen.target_file(tmp_path, "project_shared") == tmp_path / ".gemini" / "settings.json"
        assert gen.target_file(tmp_path, "project_local") is None

    def test_event_and_tool_name_mapping(self, tmp_path, all_home):
        _canonical(
            tmp_path,
            {
                "PreToolUse": [_rule("Bash", "b"), _rule("Edit|Write", "w")],
                "PostToolUse": [_rule("Read", "r")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        # Event names remapped.
        assert "BeforeTool" in hooks and "AfterTool" in hooks
        assert "PreToolUse" not in hooks and "PostToolUse" not in hooks
        # Tool-name matchers remapped: Bash→run_shell_command; Edit|Write→
        # replace|write_file (Edit = in-place → replace, Write = create → write_file).
        before_matchers = {rr["matcher"] for rr in hooks["BeforeTool"]}
        assert before_matchers == {"run_shell_command", "replace|write_file"}
        assert hooks["AfterTool"][0]["matcher"] == "read_file"
        # Handler name synthesized (Gemini handlers carry a name).
        assert hooks["BeforeTool"][0]["hooks"][0].get("name")

    def test_empty_matcher_maps_to_star(self, tmp_path, all_home):
        _canonical(tmp_path, {"PreToolUse": [_rule("", "all-tools")]})
        generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        assert hooks["BeforeTool"][0]["matcher"] == "*"

    def test_lifecycle_events_best_effort_mapped(self, tmp_path, all_home):
        """UserPromptSubmit→BeforeAgent and Stop→AfterAgent are best-effort
        lifecycle mappings (approximate timing) — they must be emitted, not
        dropped, so memtomem's context-injection / session-close hook paths
        still fire on Gemini."""
        _canonical(
            tmp_path,
            {
                "UserPromptSubmit": [_rule("", "inject")],
                "Stop": [_rule("", "close")],
            },
        )
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"

        hooks = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))[
            "hooks"
        ]
        assert "BeforeAgent" in hooks and "AfterAgent" in hooks
        assert "UserPromptSubmit" not in hooks and "Stop" not in hooks

    def test_unmapped_event_dropped_with_warning(self, tmp_path, all_home):
        # SubagentStop has no Gemini equivalent (UserPromptSubmit/Stop are now
        # best-effort-mapped to BeforeAgent/AfterAgent).
        _canonical(tmp_path, {"SubagentStop": [_rule("", "x")]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["gemini_settings"]
        assert r.status == "ok"
        assert any("SubagentStop" in w for w in r.warnings)
        written = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert written.get("hooks", {}) == {}

    def test_unmapped_matcher_token_dropped_with_warning(self, tmp_path, all_home):
        # WebFetch is a Claude tool with no Gemini equivalent — the rule would
        # never fire, so it is dropped (not silently emitted with a dead matcher).
        _canonical(tmp_path, {"PreToolUse": [_rule("WebFetch", "x")]})
        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        r = results["gemini_settings"]
        assert any("WebFetch" in w for w in r.warnings)
        written = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert written.get("hooks", {}) == {}

    def test_preserves_other_settings_keys(self, tmp_path, all_home):
        target = all_home / ".gemini" / "settings.json"
        target.write_text(
            json.dumps({"theme": "dark", "mcpServers": {"x": {"command": "y"}}}) + "\n",
            encoding="utf-8",
        )
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b")]})

        results = generate_all_settings(tmp_path, scope="user", allow_host_writes=True)
        assert results["gemini_settings"].status == "ok"

        written = json.loads(target.read_text(encoding="utf-8"))
        assert written["theme"] == "dark"
        assert written["mcpServers"] == {"x": {"command": "y"}}
        assert "BeforeTool" in written["hooks"]


# ── None fan-out (project_local) skip semantics ──────────────────────


class TestProjectLocalNoneSkip:
    def test_generate_skips_codex_gemini_at_project_local(self, tmp_path, all_home):
        (tmp_path / ".claude").mkdir()
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b")]})

        results = generate_all_settings(tmp_path, scope="project_local", allow_host_writes=False)
        # Claude has a project_local target (.claude/settings.local.json).
        assert results["claude_settings"].status == "ok"
        assert (tmp_path / ".claude" / "settings.local.json").is_file()
        # Codex/Gemini have no project_local target → skipped, dirs not created.
        assert results["codex_settings"].status == "skipped"
        assert "no fan-out target" in results["codex_settings"].reason
        assert results["gemini_settings"].status == "skipped"
        assert not (tmp_path / ".codex").exists()
        assert not (tmp_path / ".gemini").exists()

    def test_diff_skips_codex_gemini_at_project_local(self, tmp_path, all_home):
        (tmp_path / ".claude").mkdir()
        _canonical(tmp_path, {"PreToolUse": [_rule("Bash", "b")]})

        results = diff_settings(tmp_path, scope="project_local")
        assert results["codex_settings"].status == "skipped"
        assert results["gemini_settings"].status == "skipped"


# ── host-write gate across runtimes ──────────────────────────────────


class TestHostWriteMultiRuntime:
    def _setup(self, tmp_path):
        project = tmp_path / "proj"
        project.mkdir()
        canonical = project / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True)
        canonical.write_text(
            json.dumps({"hooks": {"PreToolUse": [_rule("Bash", "b")]}}) + "\n", encoding="utf-8"
        )
        return project

    def test_all_three_markers_yields_three_host_paths(self, tmp_path, all_home):
        project = self._setup(tmp_path)
        pending = host_write_targets(project, scope="user")
        assert set(pending) == {
            all_home / ".claude" / "settings.json",
            all_home / ".codex" / "hooks.json",
            all_home / ".gemini" / "settings.json",
        }

    def test_only_present_markers_are_listed(self, tmp_path, monkeypatch):
        """``is_available`` skips runtimes with no home/project marker, so the
        host-write list reflects only installed runtimes."""
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        (fake_home / ".codex").mkdir()  # only Codex installed
        set_home(monkeypatch, fake_home)

        project = self._setup(tmp_path)
        pending = host_write_targets(project, scope="user")
        assert pending == [fake_home / ".codex" / "hooks.json"]


class TestIsAvailable:
    def test_codex_available_via_project_marker(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        set_home(monkeypatch, fake_home)
        (tmp_path / ".codex").mkdir()
        assert CodexSettingsGenerator().is_available(tmp_path) is True
        assert GeminiSettingsGenerator().is_available(tmp_path) is False

    def test_neither_marker_unavailable(self, tmp_path, monkeypatch):
        fake_home = tmp_path / "home"
        fake_home.mkdir()
        set_home(monkeypatch, fake_home)
        assert CodexSettingsGenerator().is_available(tmp_path) is False
        assert GeminiSettingsGenerator().is_available(tmp_path) is False


# ── End-to-end through the CLI ───────────────────────────────────────


class TestCliMultiRuntimeSync:
    """``mm context sync --include=settings`` fans out to every installed
    runtime (end-to-end through the CLI wrapper, not just the engine)."""

    def test_sync_fans_out_to_three_runtimes(self, tmp_path, all_home, monkeypatch):
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        # Project is a sibling of the fake HOME so user-tier targets resolve
        # *outside* the project root (host writes) — ``--yes`` bypasses the
        # confirmation that would otherwise block the three home writes.
        project = tmp_path / "proj"
        project.mkdir()
        (project / ".git").mkdir()
        _canonical(project, {"PreToolUse": [_rule("Bash", "echo e2e")]})
        monkeypatch.chdir(project)

        result = CliRunner().invoke(context, ["sync", "--include=settings", "--yes"])
        assert result.exit_code == 0, result.output

        assert (all_home / ".claude" / "settings.json").is_file()
        assert (all_home / ".codex" / "hooks.json").is_file()
        assert (all_home / ".gemini" / "settings.json").is_file()
        # Gemini event remap (PreToolUse → BeforeTool) landed end-to-end.
        gemini = json.loads((all_home / ".gemini" / "settings.json").read_text(encoding="utf-8"))
        assert "BeforeTool" in gemini["hooks"]
        assert gemini["hooks"]["BeforeTool"][0]["matcher"] == "run_shell_command"


# ── _map_gemini_matcher edge cases (Codex review Major 2) ────────────


class TestGeminiMatcherEdgeCases:
    """A whitespace-only or separator-only matcher must map to ``"*"`` (all
    tools), not an empty string — an empty Gemini matcher is invalid."""

    def test_empty_blank_and_separator_only_map_to_star(self):
        from memtomem.context.settings import _map_gemini_matcher

        assert _map_gemini_matcher("") == ("*", [])
        assert _map_gemini_matcher("   ") == ("*", [])
        assert _map_gemini_matcher("|") == ("*", [])
        assert _map_gemini_matcher(" | ") == ("*", [])

    def test_real_tokens_still_map_and_dedupe(self):
        from memtomem.context.settings import _map_gemini_matcher

        assert _map_gemini_matcher("Bash") == ("run_shell_command", [])
        # Edit and Write map to DIFFERENT Gemini tools (replace vs write_file).
        assert _map_gemini_matcher("Edit|Write") == ("replace|write_file", [])
        # Edit and MultiEdit both → replace → deduped to a single token.
        assert _map_gemini_matcher("Edit|MultiEdit") == ("replace", [])

    def test_unmapped_token_returns_none(self):
        from memtomem.context.settings import _map_gemini_matcher

        mapped, unmapped = _map_gemini_matcher("WebFetch")
        assert mapped is None
        assert unmapped == ["WebFetch"]
