"""Tests for settings_doctor — duplicate-tier hook detection (ADR-0010 §4).

Covers four surfaces:

* Pure detector unit tests (``settings_doctor.detect_duplicate_tiers``)
* CLI ``mm context settings-doctor`` subcommand (clean/duplicates exit
  codes, ``--json`` schema)
* CLI sync warning wired through ``_print_settings_generate`` /
  ``_print_settings_diff``
* Web ``/api/settings-sync`` GET / POST response payloads
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.context.settings_doctor import (
    HookSignature,
    detect_duplicate_tiers,
    load_canonical_signatures,
)
from memtomem.web.app import create_app
from .helpers import set_home


# ── Helpers ────────────────────────────────────────────────────────


def _rule(matcher: str = "", command: str = "mm session start") -> dict:
    """Build a single hook rule in record format."""
    return {
        "matcher": matcher,
        "hooks": [{"type": "command", "command": command, "timeout": 5000}],
    }


def _write_settings(path, hooks: dict) -> None:
    """Write a settings.json file with the given hooks record."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"hooks": hooks}, indent=2) + "\n", encoding="utf-8")


def _write_canonical(project_root, hooks: dict) -> None:
    """Write ``.memtomem/settings.json`` with the given hooks record."""
    canonical_path = project_root / CANONICAL_SETTINGS_FILE
    _write_settings(canonical_path, hooks)


def _bundled_hook() -> dict:
    """A canonical-shape memtomem-managed hook record."""
    return {"PostToolUse": [_rule("Edit|Write", "mm session start")]}


# ── Fixtures ────────────────────────────────────────────────────────


@pytest.fixture
def fake_home(tmp_path, monkeypatch):
    """HOME pointing into tmp_path so the user-tier settings.json is
    isolated. Mirrors ``test_context_settings.claude_home`` but does not
    create ``.claude/`` — individual tests opt into populating it.
    """
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project_root(tmp_path):
    """Project root with ``.git`` so ``_find_project_root`` lands here."""
    root = tmp_path / "project"
    root.mkdir()
    (root / ".git").mkdir()
    (root / ".claude").mkdir()
    return root


# ── Detector unit tests ────────────────────────────────────────────


class TestDetectorCanonical:
    """``load_canonical_signatures`` extracts signatures from
    ``.memtomem/settings.json``."""

    def test_returns_empty_when_canonical_missing(self, project_root):
        assert load_canonical_signatures(project_root) == set()

    def test_returns_empty_when_canonical_malformed(self, project_root):
        canonical = project_root / CANONICAL_SETTINGS_FILE
        canonical.parent.mkdir(parents=True, exist_ok=True)
        canonical.write_text("{not valid json", encoding="utf-8")
        assert load_canonical_signatures(project_root) == set()

    def test_extracts_signatures(self, project_root):
        _write_canonical(project_root, _bundled_hook())
        signatures = load_canonical_signatures(project_root)
        assert (
            HookSignature(
                event="PostToolUse",
                matcher="Edit|Write",
                command_shape="mm session start",
            )
            in signatures
        )


class TestDetectDuplicateTiers:
    """Core detector behavior."""

    def test_no_canonical_means_no_duplicates(self, project_root, fake_home):
        # Even though user tier has a hook, no canonical means nothing to
        # compare against → empty list (the doctor doesn't invent matches).
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        assert detect_duplicate_tiers(project_root, active_scope="project_local") == []

    def test_no_duplicates_when_active_tier_only(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        # Active scope = user; hook lives in user tier → not a duplicate.
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        assert detect_duplicate_tiers(project_root, active_scope="user") == []

    def test_duplicate_in_user_when_active_is_project_local(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1
        assert duplicates[0].tier == "user"
        assert duplicates[0].path == fake_home / ".claude" / "settings.json"
        assert len(duplicates[0].entries) == 1

    def test_duplicate_in_project_shared_when_active_is_user(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="user")
        assert len(duplicates) == 1
        assert duplicates[0].tier == "project_shared"

    def test_duplicates_in_two_other_tiers(self, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        tiers = {dup.tier for dup in duplicates}
        assert tiers == {"user", "project_shared"}

    def test_canonical_signature_whitespace_robustness(self, project_root, fake_home):
        """Internal-whitespace variants still match (ADR-0010 §4)."""
        _write_canonical(project_root, _bundled_hook())
        # Same command but with extra spaces — must still be detected.
        variant = {"PostToolUse": [_rule("Edit|Write", "mm   session   start  ")]}
        _write_settings(fake_home / ".claude" / "settings.json", variant)
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1
        assert duplicates[0].entries[0].command_shape == "mm session start"

    def test_canonical_signature_matcher_whitespace_strip(self, project_root, fake_home):
        """Leading/trailing whitespace in matcher unifies."""
        _write_canonical(project_root, _bundled_hook())
        variant = {"PostToolUse": [_rule("  Edit|Write  ", "mm session start")]}
        _write_settings(fake_home / ".claude" / "settings.json", variant)
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1

    def test_missing_matcher_unified_with_empty(self, project_root, fake_home):
        """Missing matcher key is equivalent to ``matcher=""``."""
        _write_canonical(
            project_root,
            {"SessionStart": [_rule("", "mm index")]},
        )
        # Other tier omits the matcher key entirely.
        rule_no_matcher = {"hooks": [{"type": "command", "command": "mm index", "timeout": 5000}]}
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"SessionStart": [rule_no_matcher]},
        )
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        assert len(duplicates) == 1

    def test_non_canonical_hook_in_other_tier_ignored(self, project_root, fake_home):
        """A hook in the user tier that doesn't match the canonical
        signature is NOT reported — only canonical-matched entries
        count as duplicates per ADR-0010 §4."""
        _write_canonical(project_root, _bundled_hook())
        # User has a totally unrelated hand-authored hook.
        _write_settings(
            fake_home / ".claude" / "settings.json",
            {"PreToolUse": [_rule("Bash", "echo something")]},
        )
        assert detect_duplicate_tiers(project_root, active_scope="project_local") == []

    def test_malformed_other_tier_skipped(self, project_root, fake_home):
        """Malformed JSON in a non-active tier doesn't crash — that
        tier is skipped, the rest still classify."""
        _write_canonical(project_root, _bundled_hook())
        bad = fake_home / ".claude" / "settings.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text("{not valid", encoding="utf-8")
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        # User tier was malformed → skipped. Project_shared still reported.
        tiers = {dup.tier for dup in duplicates}
        assert tiers == {"project_shared"}

    def test_missing_other_tier_silently_skipped(self, project_root, fake_home):
        """A non-existent tier file is not a duplicate (nothing to flag)."""
        _write_canonical(project_root, _bundled_hook())
        # User tier file does not exist; project_shared has the duplicate.
        _write_settings(project_root / ".claude" / "settings.json", _bundled_hook())
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        tiers = {dup.tier for dup in duplicates}
        assert tiers == {"project_shared"}

    def test_active_scope_excluded_even_when_path_resolves_via_symlink(
        self, project_root, fake_home, tmp_path
    ):
        """Symlink dedup: when ``project_shared`` symlinks into
        ``~/.claude/`` the same real file MUST NOT be reported twice.
        """
        # Make project_root/.claude a symlink to fake_home/.claude.
        (project_root / ".claude").rmdir()
        (fake_home / ".claude").mkdir(exist_ok=True)
        (project_root / ".claude").symlink_to(fake_home / ".claude")

        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())

        # Active scope = project_local. Both 'user' and 'project_shared'
        # are non-active; both resolve through .claude/, but the
        # settings.json filenames differ (settings.json vs
        # settings.local.json), so only the user-tier file actually
        # exists. project_shared's resolved path matches user's.
        duplicates = detect_duplicate_tiers(project_root, active_scope="project_local")
        # Only one duplicate emitted, not two — the symlink dedup kept
        # the second iteration from re-reporting the same real file.
        assert len(duplicates) == 1


# ── CLI doctor subcommand ──────────────────────────────────────────


class TestSettingsDoctorCli:
    """``mm context settings-doctor`` exit codes + JSON schema."""

    def test_clean_exit_zero(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        # Active scope = user; the user-tier match is the active scope,
        # not a duplicate → clean.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 0, result.output
        assert "No memtomem-managed hooks duplicated" in result.output

    def test_duplicates_exit_one(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, [])
        assert result.exit_code == 1, result.output
        assert "user" in result.output
        assert "settings-migrate" in result.output

    def test_scope_flag_overrides_config(self, project_root, fake_home, monkeypatch):
        """Per-invocation ``--scope=`` wins over the user config field."""
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        # Config says active=user, but flag says active=project_local
        # so the user-tier match becomes a duplicate.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--scope=project_local"])
        assert result.exit_code == 1, result.output

    def test_json_clean_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "user")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload == {
            "status": "clean",
            "active_scope": "user",
            "duplicates": [],
        }

    def test_json_duplicates_schema(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import settings_doctor_cmd

        result = CliRunner().invoke(settings_doctor_cmd, ["--json"])
        assert result.exit_code == 1, result.output
        payload = json.loads(result.output)
        assert payload["status"] == "duplicates"
        assert payload["active_scope"] == "project_local"
        assert len(payload["duplicates"]) == 1
        dup = payload["duplicates"][0]
        assert dup["tier"] == "user"
        # ``str.endswith("/.claude/settings.json")`` would break on Windows
        # (backslash separator). Compare via ``Path.parts`` for
        # cross-platform stability per ``feedback_path_comparison_relative_to``.
        dup_path = Path(dup["path"])
        assert dup_path.name == "settings.json"
        assert dup_path.parent.name == ".claude"
        assert len(dup["entries"]) == 1
        entry = dup["entries"][0]
        assert entry == {
            "event": "PostToolUse",
            "matcher": "Edit|Write",
            "command_preview": "mm session start",
        }


# ── CLI sync warning ───────────────────────────────────────────────


class TestSyncWarning:
    """``mm context sync --include=settings`` emits the duplicate-tier
    warning before write and does NOT block the sync (ADR-0010 §4
    "informational, non-blocking")."""

    def test_sync_emits_warning_for_other_tier(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        # Pre-populate user tier with the duplicate.
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import sync_cmd

        # Active scope = project_local via flag; user-tier becomes a
        # duplicate.
        result = CliRunner().invoke(
            sync_cmd,
            ["--include=settings", "--scope=project_local", "--yes"],
        )
        assert result.exit_code == 0, result.output
        # Sync still runs (file was written).
        assert (project_root / ".claude" / "settings.local.json").is_file()
        # Warning appears in the (mixed) output.
        assert "memtomem-managed hook" in result.output
        assert "user" in result.output

    def test_diff_emits_warning_for_other_tier(self, project_root, fake_home, monkeypatch):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        # Bare diff command runs through ``_print_settings_diff`` which
        # also wires the warning. Default scope = user, so user-tier
        # entry is NOT a duplicate; flip the env to make project_local
        # active so user-tier becomes a duplicate for this invocation.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        monkeypatch.chdir(project_root)

        from memtomem.cli.context_cmd import diff_cmd

        result = CliRunner().invoke(diff_cmd, ["--include=settings"])
        assert result.exit_code == 0, result.output
        assert "memtomem-managed hook" in result.output


# ── Web route response ─────────────────────────────────────────────


class TestWebDuplicateTierWarnings:
    """``GET /api/settings-sync`` and ``POST /api/settings-sync`` both
    expose ``duplicate_tier_warnings`` (ADR-0010 §4 data surface). The
    frontend banner that consumes this is gated for a follow-up PR;
    these tests pin the data layer."""

    @pytest.fixture
    def app(self, project_root, fake_home, monkeypatch):
        from memtomem.config import Mem2MemConfig

        # Web settings routes are request-scoped. Keep the env override in
        # place as a regression guard: the route must follow
        # ?target_scope=..., not config.hooks.target_scope.
        monkeypatch.setenv("MEMTOMEM_HOOKS__TARGET_SCOPE", "project_local")
        application = create_app(lifespan=None, mode="dev")
        application.state.project_root = project_root
        application.state.storage = AsyncMock()
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

    async def test_get_includes_empty_warnings_when_clean(self, client, project_root):
        _write_canonical(project_root, _bundled_hook())
        # No other tiers populated → empty list.
        response = await client.get("/api/settings-sync?target_scope=project_local")
        assert response.status_code == 200
        data = response.json()
        assert data["duplicate_tier_warnings"] == []

    async def test_get_includes_warnings_when_duplicates(self, client, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        response = await client.get("/api/settings-sync?target_scope=project_local")
        assert response.status_code == 200
        data = response.json()
        warnings = data["duplicate_tier_warnings"]
        assert len(warnings) == 1
        assert warnings[0]["tier"] == "user"
        assert warnings[0]["entries"][0]["event"] == "PostToolUse"

    async def test_post_includes_warnings(self, client, project_root, fake_home):
        _write_canonical(project_root, _bundled_hook())
        _write_settings(fake_home / ".claude" / "settings.json", _bundled_hook())
        response = await client.post(
            "/api/settings-sync?target_scope=project_local",
            json={"allow_host_writes": False},
        )
        assert response.status_code == 200
        data = response.json()
        assert "results" in data
        assert "duplicate_tier_warnings" in data
        assert len(data["duplicate_tier_warnings"]) == 1
