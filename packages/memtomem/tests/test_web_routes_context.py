"""Tests for Context Gateway web routes (overview + skills + commands + agents).

Diff-payload contract pinned by ``TestDiffCommand`` / ``TestDiffAgent`` (#1256):

- **Paths** are normalized to POSIX separators on every platform. The shared
  ``_safe_rel`` helper in ``context_gateway`` (imported by
  ``context_commands`` / ``context_agents`` / ``context_skills`` /
  ``context_mcp_servers``) returns ``.as_posix()`` so the Web UI receives
  stable ``/``-separated paths; the diff tests assert literal POSIX strings (e.g.
  ``.claude/commands/ghost.md``) rather than ``str(Path(...))``, which would be
  backslash-joined on Windows. The literal-POSIX route assertions only fail on
  ``windows-latest`` (``str(PosixPath)`` already ``/``-joins on macOS/Linux);
  ``test_safe_rel_joins_with_posix_separators`` is the cross-platform guard —
  it drives ``_safe_rel`` with a ``PureWindowsPath`` so a regression to
  ``str()`` fails on any host (#1325, same shape as #1256).
- **Content** is LF, not platform-native. Canonical generators and sync write
  LF bytes, so fixtures here use ``_write_text_lf`` (raw bytes) instead of
  ``Path.write_text`` (text mode translates ``\\n`` -> ``\\r\\n`` on Windows).
  The original Windows CRLF breakage was that fixture artifact, not the route
  re-normalizing newlines.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PureWindowsPath
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.agents import AGENT_GENERATORS, parse_canonical_agent
from memtomem.context.commands import (
    COMMAND_GENERATORS,
    diff_commands,
    parse_canonical_command,
)
from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app

from .helpers import set_home


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def ctx_app(tmp_path: Path):
    """App with project_root pointing to a temp directory."""
    from memtomem.config import Mem2MemConfig

    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = tmp_path
    # Minimal stubs for deps the app might check
    application.state.storage = AsyncMock()
    # Real config so ``get_hooks_target_scope`` Depends resolves
    # ``cfg.hooks.target_scope`` (default "user").
    application.state.config = Mem2MemConfig()
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    return application


@pytest.fixture
async def client(ctx_app):
    transport = ASGITransport(app=ctx_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _make_skill(tmp_path: Path, name: str, content: str = "# Test skill\n") -> Path:
    """Create a canonical skill directory with SKILL.md."""
    skill_dir = tmp_path / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    _write_text_lf(skill_dir / SKILL_MANIFEST, content)
    return skill_dir


def _make_runtime_skill(
    tmp_path: Path,
    runtime_dir: str,
    name: str,
    content: str = "# Test skill\n",
) -> Path:
    """Create a runtime skill directory."""
    skill_dir = tmp_path / runtime_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    _write_text_lf(skill_dir / SKILL_MANIFEST, content)
    return skill_dir


def _write_text_lf(path: Path, content: str) -> None:
    """Write fixtures with sync-style LF bytes on every platform."""
    path.write_bytes(content.encode("utf-8"))


def test_safe_rel_joins_with_posix_separators() -> None:
    """Cross-platform guard for the skills ``_safe_rel`` POSIX contract (#1325).

    The route-level literal-POSIX assertions in this file only fail on
    ``windows-latest`` — ``str(PosixPath("a/b"))`` already yields ``"a/b"`` on
    macOS/Linux, so a regression to ``str()`` is invisible there. Driving the
    helper with a ``PureWindowsPath`` (whose ``str()`` is backslash-joined on
    every host) makes the assertion fail on any platform if ``.as_posix()``
    reverts to ``str()`` — the exact gap that let this bug slip past #1256.
    """
    from memtomem.web.routes.context_gateway import _safe_rel

    root = PureWindowsPath(r"C:\proj")
    nested = PureWindowsPath(r"C:\proj\.memtomem\skills\demo\SKILL.md")
    assert _safe_rel(nested, root) == ".memtomem/skills/demo/SKILL.md"
    # Outside project_root → absolute POSIX fallback, still backslash-free.
    outside = PureWindowsPath(r"C:\other\skills\x")
    assert "\\" not in _safe_rel(outside, root)


# ---------------------------------------------------------------------------
# Overview
# ---------------------------------------------------------------------------


class TestOverview:
    @pytest.mark.anyio
    async def test_empty_project(self, client: AsyncClient):
        r = await client.get("/api/context/overview")
        assert r.status_code == 200
        data = r.json()
        assert data["skills"]["total"] == 0
        # commands/agents may pick up user-scope Codex files from ~/.codex/
        assert "total" in data["commands"]
        assert "total" in data["agents"]

    @pytest.mark.anyio
    async def test_with_skills(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "code-review")
        r = await client.get("/api/context/overview")
        data = r.json()
        assert data["target_scope"] == "project_shared"
        assert data["skills"]["total"] >= 1

    @pytest.mark.anyio
    async def test_overview_exposes_project_root(self, client: AsyncClient, tmp_path: Path):
        r = await client.get("/api/context/overview")
        data = r.json()
        assert data["project_root"] == str(tmp_path)

    @pytest.mark.anyio
    async def test_overview_scans_run_off_the_event_loop(self, client: AsyncClient, monkeypatch):
        # #1518: the whole per-kind scan aggregation runs via asyncio.to_thread
        # (mirroring status-all, #1280) — pinned deterministically by the
        # executing thread of one representative diff engine.
        import threading

        from memtomem.context import skills as skills_mod

        real_diff = skills_mod.diff_skills
        ran_on_main: dict[str, bool] = {}

        def _record_thread(*args, **kwargs):
            ran_on_main["value"] = threading.current_thread() is threading.main_thread()
            return real_diff(*args, **kwargs)

        monkeypatch.setattr(skills_mod, "diff_skills", _record_thread)
        r = await client.get("/api/context/overview")
        assert r.status_code == 200
        assert ran_on_main.get("value") is False  # offloaded (#1518)

    @pytest.mark.anyio
    async def test_overview_detected_runtimes_shape(self, client: AsyncClient):
        r = await client.get("/api/context/overview")
        data = r.json()
        runtimes = data["detected_runtimes"]
        assert isinstance(runtimes, list)
        # One entry per ``KNOWN_RUNTIMES``; greyed chips for absent runtimes
        # are part of the contract — the frontend renders them as undetected.
        names = [r["name"] for r in runtimes]
        assert names == ["claude", "gemini", "codex", "kimi"]
        for entry in runtimes:
            assert isinstance(entry["available"], bool)

    @pytest.mark.anyio
    async def test_overview_detected_runtimes_flags_present_surfaces(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # Isolated home: the settings-availability probe (ADR-0009 §1,
        # #1247 id 54) reads Path.home(), so a dev machine's real ~/.codex
        # would otherwise flip the exact-flag assertion below.
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        # CLAUDE.md (top-level agent file) → claude detected
        (tmp_path / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        # .gemini/skills/foo/SKILL.md → gemini detected via skill dir
        gem_skill = tmp_path / ".gemini" / "skills" / "foo"
        gem_skill.mkdir(parents=True)
        _write_text_lf(gem_skill / SKILL_MANIFEST, "# g\n")

        r = await client.get("/api/context/overview")
        runtimes = {r["name"]: r["available"] for r in r.json()["detected_runtimes"]}
        # gemini is True via BOTH the skill dir and the settings probe
        # (project .gemini/ dir); claude via CLAUDE.md only.
        assert runtimes == {"claude": True, "gemini": True, "codex": False, "kimi": False}

    @pytest.mark.anyio
    async def test_overview_detects_kimi_runtime_config_without_importing_as_context(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        kimi_dir = tmp_path / ".kimi"
        kimi_dir.mkdir()
        (kimi_dir / "config.toml").write_text("[hooks]\n", encoding="utf-8")

        r = await client.get("/api/context/overview")
        runtimes = {r["name"]: r["available"] for r in r.json()["detected_runtimes"]}
        assert runtimes["kimi"] is True

    @pytest.mark.anyio
    async def test_project_local_overview_visible_only_with_explicit_target_scope(
        self, client: AsyncClient, tmp_path: Path
    ):
        local = tmp_path / ".memtomem" / "skills.local" / "draft"
        local.mkdir(parents=True)
        _write_text_lf(local / SKILL_MANIFEST, "# Draft\n")

        default = await client.get("/api/context/overview")
        assert default.json()["target_scope"] == "project_shared"
        assert default.json()["skills"]["total"] == 0

        explicit = await client.get(
            "/api/context/overview",
            params={"target_scope": "project_local"},
        )
        data = explicit.json()
        assert explicit.status_code == 200
        assert data["target_scope"] == "project_local"
        assert data["skills"]["total"] == 1
        assert data["skills"]["local_draft"] == 1

    # ----- Issue #832 / ADR-0009 §1.c — last-sync freshness -----

    @pytest.mark.anyio
    async def test_overview_last_synced_at_null_on_empty_project(self, client: AsyncClient):
        """A fresh / empty project has no canonical artifacts, so
        ``last_synced_at`` must be JSON-null rather than an epoch-zero
        string or a present-time fallback. The frontend uses null to
        suppress the "Last sync" header line entirely (clearer than
        rendering "Last sync: 56 years ago").
        """
        r = await client.get("/api/context/overview")
        data = r.json()
        # Key must be PRESENT in the response shape (even when null) so
        # older clients don't have to feature-detect — the dashboard
        # always reads ``data.last_synced_at`` with a string-type guard.
        assert "last_synced_at" in data
        assert data["last_synced_at"] is None

    @pytest.mark.anyio
    async def test_overview_last_synced_at_reflects_canonical_mtime(
        self, client: AsyncClient, tmp_path: Path
    ):
        """ADR-0009 §1.c: ``last_synced_at`` is derived from the canonical
        SKILL.md / command / agent file mtime — not a persisted log. A
        single skill drop yields its manifest mtime, formatted as
        ISO8601 UTC with a trailing ``Z`` (no ``+00:00`` ambiguity).
        """
        import re

        skill_dir = _make_skill(tmp_path, "code-review")
        manifest = skill_dir / SKILL_MANIFEST
        # Pin mtime explicitly so the assertion isn't time-of-test fragile.
        target_epoch = 1715000000.0  # 2024-05-06T12:53:20Z UTC
        os.utime(manifest, (target_epoch, target_epoch))

        r = await client.get("/api/context/overview")
        data = r.json()
        ts = data["last_synced_at"]
        assert isinstance(ts, str)
        assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", ts), (
            f"last_synced_at must be ISO8601 UTC with trailing Z; got {ts!r}"
        )
        # The exact pinned timestamp — confirms the helper read the
        # manifest mtime rather than directory mtime or current time.
        assert ts == "2024-05-06T12:53:20Z", (
            f"last_synced_at must equal the pinned SKILL.md mtime; got {ts!r}"
        )

    @pytest.mark.anyio
    async def test_overview_last_synced_at_returns_max_across_surfaces(
        self, client: AsyncClient, tmp_path: Path
    ):
        """When canonical artifacts exist across multiple surfaces
        (skills + agents), the freshness indicator must reflect the
        most-recent mtime — the user reads "Last sync: 5 min ago"
        as "something in the dashboard scope was synced 5 min ago,"
        not "the oldest of all the things."
        """
        older_skill = _make_skill(tmp_path, "older")
        os.utime(older_skill / SKILL_MANIFEST, (1700000000.0, 1700000000.0))

        # Drop a canonical agent file via the flat layout so the helper's
        # list_canonical_agents branch is exercised end-to-end.
        agents_dir = tmp_path / ".memtomem" / "agents"
        agents_dir.mkdir(parents=True)
        newer_agent = agents_dir / "fresh.md"
        newer_agent.write_text("---\nname: fresh\ndescription: x\n---\n# fresh\n", encoding="utf-8")
        newer_epoch = 1720000000.0  # 2024-07-03T09:46:40Z UTC
        os.utime(newer_agent, (newer_epoch, newer_epoch))

        r = await client.get("/api/context/overview")
        ts = r.json()["last_synced_at"]
        assert ts == "2024-07-03T09:46:40Z", (
            f"last_synced_at must be max(skill_mtime, agent_mtime); got {ts!r}"
        )

    # ── wiki_installs — the lockfile↔wiki staleness axis (0629 backlog c/d) ──

    def _install_pinned_skill(self, project: Path, wiki_root: Path, name: str) -> str:
        """Seed ``skills/<name>`` in the wiki, mirror it into *project*'s
        dest tree, and pin the lockfile at the seeding commit. Returns the
        pin SHA — advance the wiki afterwards to produce a ``behind`` row.
        """
        import subprocess

        from memtomem.context._atomic import installed_at_from_dest
        from memtomem.context.lockfile import Lockfile
        from memtomem.wiki.store import WikiStore

        store = WikiStore.at_default()
        if not (wiki_root / ".git").exists():
            store.init()
        skill_dir = wiki_root / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MANIFEST).write_bytes(b"v1\n")
        subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-m", f"add {name}"],
            check=True,
            capture_output=True,
        )
        pin = store.current_commit()

        dest = project / ".memtomem" / "skills" / name
        dest.mkdir(parents=True)
        (dest / SKILL_MANIFEST).write_bytes(b"v1\n")
        Lockfile.at(project).upsert_entry(
            "skills", name, wiki_commit=pin, installed_at=installed_at_from_dest(dest)
        )
        return pin

    @pytest.mark.anyio
    async def test_overview_wiki_installs_reports_behind_count(
        self, client: AsyncClient, tmp_path: Path, wiki_root: Path
    ):
        """An install pinned below wiki HEAD surfaces as ``behind`` in the
        overview's ``wiki_installs`` block — the count the header badge
        renders. Before the wiki advances the same install reads clean.
        """
        import subprocess

        self._install_pinned_skill(tmp_path, wiki_root, "pinned")

        r = await client.get("/api/context/overview")
        assert r.status_code == 200
        assert r.json()["wiki_installs"] == {"total": 1, "behind": 0}

        # Advance the wiki past the pin — the dest stays clean, so the
        # entry flips to ``behind`` ("update available").
        (wiki_root / "skills" / "pinned" / SKILL_MANIFEST).write_bytes(b"v2\n")
        subprocess.run(["git", "-C", str(wiki_root), "add", "."], check=True, capture_output=True)
        subprocess.run(
            ["git", "-C", str(wiki_root), "commit", "-m", "advance"],
            check=True,
            capture_output=True,
        )

        r = await client.get("/api/context/overview")
        assert r.json()["wiki_installs"] == {"total": 1, "behind": 1}

    @pytest.mark.anyio
    async def test_overview_wiki_installs_excludes_untracked_canonicals(
        self, client: AsyncClient, tmp_path: Path
    ):
        """Canonical artifacts with no lockfile entry (reverse-imported /
        migrated-in) have no wiki pin to fall behind — they must not
        inflate ``wiki_installs.total`` the way they join the skills tile.
        """
        _make_skill(tmp_path, "untracked-only")
        r = await client.get("/api/context/overview")
        data = r.json()
        assert data["skills"]["total"] >= 1
        assert data["wiki_installs"] == {"total": 0, "behind": 0}

    @pytest.mark.anyio
    async def test_overview_wiki_installs_placeholder_on_other_tiers(
        self, client: AsyncClient, tmp_path: Path, wiki_root: Path
    ):
        """Wiki installs are lockfile-tracked project_shared snapshots only;
        the user / project_local overviews carry the zero placeholder
        (mirror of the ``mcp_servers`` single-tier convention).
        """
        self._install_pinned_skill(tmp_path, wiki_root, "pinned")
        for tier in ("user", "project_local"):
            r = await client.get("/api/context/overview", params={"target_scope": tier})
            assert r.status_code == 200
            assert r.json()["wiki_installs"] == {"total": 0, "behind": 0}, tier


class TestSettingsCountShape:
    """Q-PR3 Visual-1 pins for the new ``settings`` count envelope.

    Pre-Q-PR3 the settings slot only carried ``{"status": ...}``, which
    forced the dashboard to render a glyph (✔ / ⚠) in the big-number slot
    while the other three tiles rendered ``${total}``. Visual-1 extends
    the envelope with ``total`` (count of applicable generators —
    excluding ``skipped``) plus per-status counts so the frontend can
    treat settings like the other tiles.

    Pin: the new fields exist with the right semantics, AND the legacy
    ``status`` field stays for backwards compat with any non-dashboard
    consumer.
    """

    @pytest.mark.anyio
    async def test_settings_envelope_carries_count_fields(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem.context.settings import SettingsSyncResult

        # Mixed shape: 1 in-sync + 1 out-of-sync. ``total`` should be 2
        # (both applicable), with per-status counts split 1/1.
        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            lambda *_a, **_k: {
                "claude_settings": SettingsSyncResult(status="in sync"),
                "codex_agents": SettingsSyncResult(status="out of sync"),
            },
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        assert settings["total"] == 2, settings
        assert settings["in_sync"] == 1, settings
        assert settings["out_of_sync"] == 1, settings
        assert settings["missing_target"] == 0, settings
        assert settings["error"] == 0, settings
        assert settings["status"] == "out_of_sync", settings
        # Negative pin: must NOT regress to the legacy status-only shape
        # (if total goes missing the dashboard's count rendering breaks).
        assert "total" in settings, "Q-PR3 envelope must carry total"

    @pytest.mark.anyio
    async def test_settings_missing_target_counted(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """The common first-use state: user has ``.memtomem/settings.json``
        but hasn't yet run ``mm context sync``, so the runtime settings
        target file (e.g., ``~/.claude/settings.json``) doesn't exist.

        ``diff_settings`` emits ``status="missing target"`` for that case
        (settings.py:403-404 — ``existing is None`` branch). The envelope
        must count it as a distinct category so the per-status counts
        sum to ``total_applicable``; folding it into ``out_of_sync``
        would conflate "drifted from canonical" with "never imported",
        which is the same distinction skills/commands/agents already
        carry via their own ``missing_target`` field. Any future per-
        status segment rendering would silently lose this slice if the
        envelope dropped it on the floor."""
        from memtomem.context.settings import SettingsSyncResult

        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            lambda *_a, **_k: {
                "claude_settings": SettingsSyncResult(status="missing target"),
            },
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        assert settings["total"] == 1, settings
        assert settings["missing_target"] == 1, settings
        assert settings["in_sync"] == 0, settings
        assert settings["out_of_sync"] == 0, settings
        assert settings["error"] == 0, settings
        # Status collapse: missing target is non-error/non-skipped/non-
        # in_sync, so the existing ``else`` branch puts it under
        # ``out_of_sync``. UX-wise the tile reads "1 / out of sync" which
        # is acceptable for the dashboard summary; per-runtime detail
        # lives on the leaf page. The count field carries the precise
        # state for any future renderer that wants to disambiguate.
        assert settings["status"] == "out_of_sync", settings

    @pytest.mark.anyio
    async def test_settings_count_conservation(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Invariant: ``in_sync + out_of_sync + missing_target + error ==
        total`` for every applicable-generator distribution. This is
        what makes the per-status counts safe to render as segments —
        if the contract slips (a status emitted by diff_settings is not
        counted in any field), a future consumer's segments silently
        drop the missing slice. Use a 4-way mix so every field is
        non-zero and the equality must hold."""
        from memtomem.context.settings import SettingsSyncResult

        # Four-way mix — every count field is non-zero.
        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            lambda *_a, **_k: {
                "a": SettingsSyncResult(status="in sync"),
                "b": SettingsSyncResult(status="out of sync"),
                "c": SettingsSyncResult(status="missing target"),
                "d": SettingsSyncResult(status="error"),
                "e": SettingsSyncResult(status="skipped"),  # not counted in total
            },
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        sub_sum = (
            settings["in_sync"]
            + settings["out_of_sync"]
            + settings["missing_target"]
            + settings["error"]
        )
        assert sub_sum == settings["total"], (
            f"per-status counts must sum to total (Q-PR3 invariant); "
            f"got total={settings['total']} sum={sub_sum} settings={settings}"
        )
        # Skipped is excluded from total: 5 generators, 1 skipped → 4.
        assert settings["total"] == 4, settings

    @pytest.mark.anyio
    async def test_settings_skipped_not_counted_in_total(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem.context.settings import SettingsSyncResult

        # All ``skipped`` → ``total = 0``. Skipped items represent
        # generators with no canonical source or no installed runtime;
        # counting them would inflate the denominator with N/A items.
        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            lambda *_a, **_k: {
                "claude_settings": SettingsSyncResult(status="skipped"),
                "codex_agents": SettingsSyncResult(status="skipped"),
            },
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        assert settings["total"] == 0, f"all-skipped must not count toward total; got: {settings}"
        # ``status`` stays "in_sync" via the existing collapse (skipped
        # is treated as a no-op alongside in_sync). The frontend's
        # ``isEmpty`` branch handles the resulting tile, not the
        # status-driven badge — verified separately in the Playwright
        # spec ``test_q_pr3_settings_zero_total_renders_empty``.
        assert settings["status"] == "in_sync", settings

    @pytest.mark.anyio
    async def test_settings_status_collapse_unchanged(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        """Backwards-compat anchor: the existing ``in_sync`` / ``error`` /
        ``out_of_sync`` collapse rules are unchanged; only the count
        fields are additive. A single in-sync entry plus skipped peers
        still collapses to ``status: "in_sync"``."""
        from memtomem.context.settings import SettingsSyncResult

        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            lambda *_a, **_k: {
                "claude_settings": SettingsSyncResult(status="in sync"),
                "codex_agents": SettingsSyncResult(status="skipped"),
            },
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        assert settings["status"] == "in_sync"
        assert settings["total"] == 1  # claude in-sync; codex skipped
        assert settings["in_sync"] == 1


# ---------------------------------------------------------------------------
# Overview — error taxonomy (issue #762)
# ---------------------------------------------------------------------------


def _raises(exc: BaseException):
    """Return a callable that raises ``exc`` when invoked."""

    def _fn(*_args, **_kwargs):
        raise exc

    return _fn


class TestContextOverviewErrorTaxonomy:
    """Pin the {parse, permission, missing, internal} classification.

    Patches the source modules (``memtomem.context.skills.diff_skills`` etc.)
    rather than the route-local re-imports — the route does function-level
    ``from ... import ...`` so the lookup hits the patched module attribute.
    """

    @pytest.mark.anyio
    async def test_skills_permission_error_classified_as_permission(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "memtomem.context.skills.diff_skills",
            _raises(PermissionError("denied")),
        )
        r = await client.get("/api/context/overview")
        assert r.status_code == 200
        skills = r.json()["skills"]
        assert skills["error"] is True
        assert skills["total"] == 0
        assert skills["error_kind"] == "permission"
        assert "denied" in skills["error_message"]

    @pytest.mark.anyio
    async def test_commands_file_not_found_classified_as_missing(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "memtomem.context.commands.diff_commands",
            _raises(FileNotFoundError("no such dir")),
        )
        r = await client.get("/api/context/overview")
        assert r.json()["commands"]["error_kind"] == "missing"

    @pytest.mark.anyio
    async def test_agents_unicode_decode_classified_as_parse(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "memtomem.context.agents.diff_agents",
            _raises(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")),
        )
        r = await client.get("/api/context/overview")
        assert r.json()["agents"]["error_kind"] == "parse"

    @pytest.mark.anyio
    async def test_skills_runtime_error_classified_as_internal(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "memtomem.context.skills.diff_skills",
            _raises(RuntimeError("boom")),
        )
        r = await client.get("/api/context/overview")
        assert r.json()["skills"]["error_kind"] == "internal"

    @pytest.mark.anyio
    async def test_skills_generic_oserror_classified_as_internal(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # Bare OSError with errno=EIO — not permission/missing — should fall
        # through to ``internal`` rather than be guessed as permission/missing.
        monkeypatch.setattr(
            "memtomem.context.skills.diff_skills",
            _raises(OSError(5, "Input/output error")),
        )
        r = await client.get("/api/context/overview")
        assert r.json()["skills"]["error_kind"] == "internal"

    @pytest.mark.anyio
    async def test_settings_bare_exception_uses_status_shape(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            _raises(RuntimeError("settings exploded")),
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        assert settings == {
            "status": "error",
            "error_kind": "internal",
            "error_message": "settings exploded",
        }

    @pytest.mark.anyio
    async def test_settings_inband_error_has_no_error_kind(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # In-band per-file error: diff_settings succeeds but returns a result
        # with status="error". Aggregated status stays "error" with no
        # error_kind — adding one would conflate per-file causes. After
        # Q-PR3 the success-path envelope also carries count fields
        # (parallel to skills/commands/agents).
        from memtomem.context.settings import SettingsSyncResult

        monkeypatch.setattr(
            "memtomem.context.settings.diff_settings",
            lambda *_a, **_k: {"claude": SettingsSyncResult(status="error")},
        )
        r = await client.get("/api/context/overview")
        settings = r.json()["settings"]
        assert settings["status"] == "error"
        assert "error_kind" not in settings
        # The bare-exception path uses ``shape="status"`` and emits the
        # error envelope; the in-band path stays on the count envelope.
        # Distinguishing factor: a bare exception sets ``error_message``,
        # the in-band path does not.
        assert "error_message" not in settings

    @pytest.mark.anyio
    async def test_error_message_truncated_and_homedir_collapsed(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        long_msg = str(Path.home()) + "/oops-" + "z" * 500
        monkeypatch.setattr(
            "memtomem.context.skills.diff_skills",
            _raises(RuntimeError(long_msg)),
        )
        r = await client.get("/api/context/overview")
        msg = r.json()["skills"]["error_message"]
        assert len(msg) <= 200
        assert msg.startswith("~/oops-")
        assert str(Path.home()) not in msg

    @pytest.mark.anyio
    async def test_back_compat_error_true_preserved(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # The new fields are additive — existing consumers reading `error`
        # boolean and `total` int must keep seeing them on the failure path.
        monkeypatch.setattr(
            "memtomem.context.skills.diff_skills",
            _raises(PermissionError("nope")),
        )
        r = await client.get("/api/context/overview")
        skills = r.json()["skills"]
        assert skills["error"] is True
        assert skills["total"] == 0
        assert "error_kind" in skills
        assert "error_message" in skills

    @pytest.mark.anyio
    async def test_error_message_redacts_api_key_assignment(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # ``internal`` is a catch-all and exception text may incidentally
        # contain secret-shape fragments. ``api_key=`` matches the privacy
        # scanner's assignment anchor, so the whole message is replaced
        # with the redaction marker — splicing alone would leave the
        # value (``hunter2``) intact.
        monkeypatch.setattr(
            "memtomem.context.skills.diff_skills",
            _raises(RuntimeError("parse failed near api_key=hunter2 in config")),
        )
        r = await client.get("/api/context/overview")
        msg = r.json()["skills"]["error_message"]
        assert "hunter2" not in msg
        assert "api_key" not in msg
        assert msg == "<redacted: secret-shape>"

    @pytest.mark.anyio
    async def test_error_message_redacts_provider_token(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # Provider-prefixed token (``sk-`` / ``ghp_`` / ``github_pat_``)
        # must not survive into ``error_message``.
        secret = "sk-" + "A" * 40
        monkeypatch.setattr(
            "memtomem.context.commands.diff_commands",
            _raises(RuntimeError(f"upstream returned {secret} in body")),
        )
        r = await client.get("/api/context/overview")
        msg = r.json()["commands"]["error_message"]
        assert secret not in msg
        assert msg == "<redacted: secret-shape>"

    @pytest.mark.anyio
    async def test_error_message_clean_message_passes_through(
        self, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
    ):
        # No secret-shape hits → message must pass through unchanged
        # (modulo the existing ``$HOME`` collapse + 200-char cap). This
        # pins the redaction so it doesn't accidentally swallow every
        # ``internal`` error.
        monkeypatch.setattr(
            "memtomem.context.agents.diff_agents",
            _raises(RuntimeError("disk full while reading agents dir")),
        )
        r = await client.get("/api/context/overview")
        msg = r.json()["agents"]["error_message"]
        assert msg == "disk full while reading agents dir"


# ---------------------------------------------------------------------------
# Skills — List
# ---------------------------------------------------------------------------


class TestListSkills:
    @pytest.mark.anyio
    async def test_empty(self, client: AsyncClient):
        r = await client.get("/api/context/skills")
        assert r.status_code == 200
        data = r.json()
        assert data["skills"] == []
        # GET also surfaces canonical_root + scanned_dirs so the empty-state
        # hint can pull from the wire instead of hardcoding the detector
        # layout client-side.
        assert data["canonical_root"] == ".memtomem/skills"
        assert ".claude/skills" in data["scanned_dirs"]
        assert ".gemini/skills" in data["scanned_dirs"]
        assert ".agents/skills" in data["scanned_dirs"]

    @pytest.mark.anyio
    async def test_with_items(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "alpha")
        _make_skill(tmp_path, "beta")
        r = await client.get("/api/context/skills")
        data = r.json()
        names = [s["name"] for s in data["skills"]]
        assert "alpha" in names
        assert "beta" in names
        assert {s["target_scope"] for s in data["skills"]} == {"project_shared"}

    @pytest.mark.anyio
    async def test_project_local_visible_only_with_explicit_target_scope(
        self, client: AsyncClient, tmp_path: Path
    ):
        local = tmp_path / ".memtomem" / "skills.local" / "draft"
        local.mkdir(parents=True)
        _write_text_lf(local / SKILL_MANIFEST, "# Draft\n")

        default = await client.get("/api/context/skills")
        assert all(s["name"] != "draft" for s in default.json()["skills"])

        explicit = await client.get(
            "/api/context/skills",
            params={"target_scope": "project_local"},
        )
        data = explicit.json()
        assert explicit.status_code == 200, explicit.text
        assert data["skills"][0]["name"] == "draft"
        assert data["skills"][0]["target_scope"] == "project_local"

    @pytest.mark.anyio
    async def test_invalid_target_scope_returns_422(self, client: AsyncClient):
        r = await client.get("/api/context/skills", params={"target_scope": "draft"})
        assert r.status_code == 422

    @pytest.mark.anyio
    async def test_includes_runtime_status(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "review")
        r = await client.get("/api/context/skills")
        skill = r.json()["skills"][0]
        assert skill["runtimes"]  # should have entries for each generator
        statuses = [rt["status"] for rt in skill["runtimes"]]
        # All should be "missing target" since we haven't synced
        assert all(s == "missing target" for s in statuses)


# ---------------------------------------------------------------------------
# Skills — Read
# ---------------------------------------------------------------------------


class TestReadSkill:
    @pytest.mark.anyio
    async def test_read(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "demo", "# Demo skill\nDetails here.\n")
        r = await client.get("/api/context/skills/demo")
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "demo"
        assert "Demo skill" in data["content"]
        assert int(data["mtime_ns"]) > 0

    @pytest.mark.anyio
    async def test_not_found(self, client: AsyncClient):
        r = await client.get("/api/context/skills/nonexistent")
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_bom_skill_description_parses(self, client: AsyncClient, tmp_path: Path):
        """A BOM-prefixed SKILL.md must surface its frontmatter description,
        not the literal frontmatter fence the anchored regex fell back to
        pre-fix (#1229). The content payload stays byte-faithful."""
        skill_dir = tmp_path / ".memtomem" / "skills" / "bom-skill"
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MANIFEST).write_bytes(
            b"\xef\xbb\xbf---\ndescription: Windows-authored skill\n---\n\nBody.\n"
        )
        r = await client.get("/api/context/skills/bom-skill")
        assert r.status_code == 200
        data = r.json()
        assert data["fields"]["description"] == "Windows-authored skill"
        assert data["content"].startswith("﻿")  # editor payload untouched

    @pytest.mark.anyio
    async def test_auxiliary_files(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "rich")
        scripts_dir = tmp_path / ".memtomem" / "skills" / "rich" / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        r = await client.get("/api/context/skills/rich")
        data = r.json()
        paths = [f["path"] for f in data["files"]]
        # Multi-segment intra-skill path pins POSIX separators — the inline
        # ``relative_to(skill_dir).as_posix()`` (not ``str``) at
        # context_skills.py would emit ``scripts\\run.sh`` on Windows (#1325).
        assert "scripts/run.sh" in paths


# ---------------------------------------------------------------------------
# Skills — Create
# ---------------------------------------------------------------------------


class TestCreateSkill:
    @pytest.mark.anyio
    async def test_create(self, client: AsyncClient, tmp_path: Path):
        r = await client.post(
            "/api/context/skills",
            json={"name": "new-skill", "content": "# New\n"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["name"] == "new-skill"
        # Multi-segment ``canonical_path`` pins POSIX separators — ``_safe_rel``
        # would emit ``.memtomem\\skills\\new-skill`` on Windows pre-fix (#1325).
        assert data["canonical_path"] == ".memtomem/skills/new-skill"
        # Verify file exists on disk
        assert (tmp_path / ".memtomem" / "skills" / "new-skill" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_create_duplicate(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "existing")
        r = await client.post(
            "/api/context/skills",
            json={"name": "existing", "content": "# Dup\n"},
        )
        # 409 Conflict, matching create_agent / create_command (a duplicate
        # name is a conflict, not a malformed request).
        assert r.status_code == 409

    @pytest.mark.anyio
    async def test_create_invalid_name(self, client: AsyncClient):
        r = await client.post(
            "/api/context/skills",
            json={"name": "../escape", "content": "bad"},
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_create_lone_surrogate_no_orphan_dir_wedge(
        self, client: AsyncClient, tmp_path: Path
    ):
        """A lone-surrogate body can't be UTF-8 encoded, so ``atomic_write_text``
        raises ``UnicodeEncodeError`` *after* ``skill_dir.mkdir()`` — pre-fix
        that left an orphan directory behind, wedging every retry on the 409
        "already exists" conflict. ``create_command`` / ``create_agent``
        pre-encode the body (clean 400 before mkdir) and roll the partial dir
        back on any failure; this route now mirrors that shape.

        httpx 0.28 encodes ``json=`` with ``ensure_ascii=False`` and would raise
        on the lone surrogate client-side, so the escaped ASCII JSON body is
        sent directly (stdlib json decodes the escape back to a lone surrogate
        server-side).
        """
        skill_dir = tmp_path / ".memtomem" / "skills" / "surrogate"
        body = json.dumps({"name": "surrogate", "content": "# bad \ud800\n"})
        r = await client.post(
            "/api/context/skills",
            content=body,
            headers={"Content-Type": "application/json"},
        )
        assert r.status_code == 400, r.text
        # No orphan directory left behind — the wedge source.
        assert not skill_dir.exists()
        # Wedge regression: a retry with valid content must succeed, not 409.
        r2 = await client.post(
            "/api/context/skills",
            json={"name": "surrogate", "content": "# good\n"},
        )
        assert r2.status_code == 200, r2.text
        assert (skill_dir / SKILL_MANIFEST).is_file()


# ---------------------------------------------------------------------------
# Skills — Update
# ---------------------------------------------------------------------------


class TestUpdateSkill:
    @pytest.mark.anyio
    async def test_update(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "upd")
        # Read to get mtime_ns
        r = await client.get("/api/context/skills/upd")
        mtime_ns = r.json()["mtime_ns"]

        r = await client.put(
            "/api/context/skills/upd",
            json={"content": "# Updated\n", "mtime_ns": mtime_ns},
        )
        assert r.status_code == 200
        assert r.json()["name"] == "upd"
        # Verify content changed
        content = (tmp_path / ".memtomem" / "skills" / "upd" / SKILL_MANIFEST).read_text(
            encoding="utf-8"
        )
        assert "Updated" in content

    @pytest.mark.anyio
    async def test_mtime_conflict(self, client: AsyncClient, tmp_path: Path):
        # ADR-0001 §5 c4: pin no-write semantics on the conflict path. The
        # response label alone would still pass a regression that wrote
        # body.content and *then* returned the abort envelope.
        _make_skill(tmp_path, "conflict")
        manifest_path = tmp_path / ".memtomem" / "skills" / "conflict" / SKILL_MANIFEST
        r = await client.put(
            "/api/context/skills/conflict",
            json={"content": "# Changed\n", "mtime_ns": "0"},  # wrong mtime_ns
        )
        assert r.status_code == 409
        data = r.json()
        assert data["status"] == "aborted"
        assert data["mtime_ns"] == str(manifest_path.stat().st_mtime_ns)
        assert manifest_path.read_text(encoding="utf-8") == "# Test skill\n"


# ---------------------------------------------------------------------------
# Skills — Delete
# ---------------------------------------------------------------------------


class TestDeleteSkill:
    @pytest.mark.anyio
    async def test_delete_canonical_only(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "del-me")
        r = await client.delete("/api/context/skills/del-me")
        assert r.status_code == 200
        assert not (tmp_path / ".memtomem" / "skills" / "del-me").exists()

    @pytest.mark.anyio
    async def test_delete_cascade(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "cascade")
        _make_runtime_skill(tmp_path, ".claude/skills", "cascade")
        r = await client.delete("/api/context/skills/cascade?cascade=true")
        assert r.status_code == 200
        assert not (tmp_path / ".memtomem" / "skills" / "cascade").exists()
        assert not (tmp_path / ".claude" / "skills" / "cascade").exists()
        # ``deleted`` entries are ``_safe_rel`` results — literal POSIX, not
        # ``.memtomem\\skills\\cascade`` (#1325; mirrors the commands route pin).
        data = r.json()
        assert ".memtomem/skills/cascade" in data["deleted"]
        assert ".claude/skills/cascade" in data["deleted"]

    @pytest.mark.anyio
    async def test_delete_missing_is_idempotent(self, client: AsyncClient):
        """DELETE of a missing skill succeeds with an empty result (idempotent)."""
        r = await client.delete("/api/context/skills/nope")
        assert r.status_code == 200
        assert r.json()["deleted"] == []


# ---------------------------------------------------------------------------
# Skills — Diff
# ---------------------------------------------------------------------------


class TestDiffSkill:
    @pytest.mark.anyio
    async def test_diff_missing_target(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "orphan")
        r = await client.get("/api/context/skills/orphan/diff")
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"] is not None
        assert any(rt["status"] == "missing target" for rt in data["runtimes"])

    @pytest.mark.anyio
    async def test_diff_in_sync(self, client: AsyncClient, tmp_path: Path):
        content = "# Synced\n"
        _make_skill(tmp_path, "synced", content)
        _make_runtime_skill(tmp_path, ".claude/skills", "synced", content)
        r = await client.get("/api/context/skills/synced/diff")
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "in sync"

    @pytest.mark.anyio
    async def test_diff_out_of_sync(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "diverged", "# V1\n")
        _make_runtime_skill(tmp_path, ".claude/skills", "diverged", "# V2\n")
        r = await client.get("/api/context/skills/diverged/diff")
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "out of sync"
        assert claude_rt["runtime_content"] == "# V2\n"

    @pytest.mark.anyio
    async def test_diff_user_tier_resolves_user_runtime(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``target_scope=user`` must probe the user-tier fan-out roots
        (``~/.claude/skills``), not the project's project_shared paths, so the
        detail panel agrees with the scope-aware list diff (#1229)."""
        home = tmp_path / "home"
        set_home(monkeypatch, home)
        content = "# User skill\n"
        skill_dir = home / ".memtomem" / "skills" / "scoped"
        skill_dir.mkdir(parents=True)
        _write_text_lf(skill_dir / SKILL_MANIFEST, content)
        rt_dir = home / ".claude" / "skills" / "scoped"
        rt_dir.mkdir(parents=True)
        _write_text_lf(rt_dir / SKILL_MANIFEST, content)

        r = await client.get("/api/context/skills/scoped/diff", params={"target_scope": "user"})
        assert r.status_code == 200
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "in sync"

    @pytest.mark.anyio
    async def test_diff_project_local_has_no_runtime_rows(
        self, client: AsyncClient, tmp_path: Path
    ):
        """project_local has no runtime fan-out (ADR-0011 §3): the detail diff
        must not fabricate runtime rows against project_shared paths (#1229)."""
        local_dir = tmp_path / ".memtomem" / "skills.local" / "draft"
        local_dir.mkdir(parents=True)
        _write_text_lf(local_dir / SKILL_MANIFEST, "# Draft\n")

        r = await client.get(
            "/api/context/skills/draft/diff", params={"target_scope": "project_local"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"] == "# Draft\n"
        assert data["runtimes"] == []

    @pytest.mark.anyio
    async def test_diff_non_utf8_canonical_does_not_abort(
        self, client: AsyncClient, tmp_path: Path
    ):
        """A stray non-UTF-8 byte in the canonical SKILL.md must not abort the
        per-name diff (pre-fix ``read_text(encoding="utf-8")`` raised
        ``UnicodeDecodeError`` → the app's ValueError handler turned it into a
        400 error, not a diff). The engine ``diff_skills`` reads bytes with
        ``errors="replace"``, so the detail pane must read leniently too —
        parity with ``diff_command`` / ``diff_agent`` (#1229/#1233), which this
        route never received."""
        skill_dir = tmp_path / ".memtomem" / "skills" / "cafe"
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MANIFEST).write_bytes(b"# caf\xe9 skill\n")
        r = await client.get("/api/context/skills/cafe/diff")
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["canonical_content"] is not None
        assert "�" in data["canonical_content"]  # U+FFFD replacement

    @pytest.mark.anyio
    async def test_diff_non_utf8_runtime_reports_drift_not_abort(
        self, client: AsyncClient, tmp_path: Path
    ):
        """A non-UTF-8 byte in a *runtime* copy is drift to display, not a
        diff-wide abort — the ``out of sync`` runtime read must go through the
        lenient reader (#1229/#1233)."""
        _make_skill(tmp_path, "diverged", "# clean\n")
        rt_dir = tmp_path / ".claude" / "skills" / "diverged"
        rt_dir.mkdir(parents=True)
        (rt_dir / SKILL_MANIFEST).write_bytes(b"# caf\xe9 drift\n")
        r = await client.get("/api/context/skills/diverged/diff")
        assert r.status_code == 200, r.text
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "out of sync"
        assert "�" in claude_rt["runtime_content"]

    @pytest.mark.anyio
    async def test_diff_non_utf8_runtime_missing_canonical_does_not_abort(
        self, client: AsyncClient, tmp_path: Path
    ):
        """A runtime-only skill whose SKILL.md carries a non-UTF-8 byte surfaces
        as ``missing canonical`` with a lenient runtime preview, not an abort
        (the third read site in ``diff_skill``)."""
        rt_dir = tmp_path / ".claude" / "skills" / "runtime-only"
        rt_dir.mkdir(parents=True)
        (rt_dir / SKILL_MANIFEST).write_bytes(b"# caf\xe9 only\n")
        r = await client.get("/api/context/skills/runtime-only/diff")
        assert r.status_code == 200, r.text
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_skills"][0]
        assert claude_rt["status"] == "missing canonical"
        assert "�" in claude_rt["runtime_content"]


# ---------------------------------------------------------------------------
# Skills — Sync
# ---------------------------------------------------------------------------


class TestSyncSkills:
    @pytest.mark.anyio
    async def test_sync(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "fan-out", "# Ready\n")
        r = await client.post("/api/context/skills/sync")
        assert r.status_code == 200
        data = r.json()
        assert len(data["generated"]) >= 3  # claude + gemini + codex
        # Verify files created
        assert (tmp_path / ".claude" / "skills" / "fan-out" / SKILL_MANIFEST).is_file()
        # PR1: response surfaces canonical_root so the empty-state UI can
        # tell users where to put canonical skills.
        assert data["canonical_root"] == ".memtomem/skills"

    @pytest.mark.anyio
    async def test_sync_empty(self, client: AsyncClient):
        r = await client.post("/api/context/skills/sync")
        assert r.status_code == 200
        data = r.json()
        assert data["skipped"]
        # PR1: empty-canonical case carries machine-readable reason_code so
        # the UI doesn't string-match on the human reason. canonical_root is
        # echoed so the UI can name the directory in the toast.
        assert any(s["reason_code"] == "no_canonical_root" for s in data["skipped"])
        assert data["canonical_root"] == ".memtomem/skills"


# ---------------------------------------------------------------------------
# Skills — Import
# ---------------------------------------------------------------------------


class TestImportSkills:
    @pytest.mark.anyio
    async def test_import(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_skill(tmp_path, ".claude/skills", "from-claude", "# Imported\n")
        r = await client.post(
            "/api/context/skills/import",
            json={"overwrite": False},
        )
        assert r.status_code == 200
        data = r.json()
        assert any(i["name"] == "from-claude" for i in data["imported"])
        assert (tmp_path / ".memtomem" / "skills" / "from-claude" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_import_dry_run_previews_without_writing(
        self, client: AsyncClient, tmp_path: Path
    ):
        # rank-10: ``?dry_run=1`` returns the would-import preview (with a
        # ``dry_run`` flag) and leaves canonical untouched; a follow-up real
        # import then writes.
        _make_runtime_skill(tmp_path, ".claude/skills", "from-claude", "# Imported\n")
        r = await client.post(
            "/api/context/skills/import?dry_run=1",
            json={"overwrite": False},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["dry_run"] is True
        assert any(i["name"] == "from-claude" for i in data["imported"])
        # Disk is untouched by the preview.
        assert not (tmp_path / ".memtomem" / "skills" / "from-claude").exists()

        # A real import (default dry_run) now writes and echoes dry_run=False.
        r2 = await client.post("/api/context/skills/import", json={"overwrite": False})
        assert r2.status_code == 200
        assert r2.json()["dry_run"] is False
        assert (tmp_path / ".memtomem" / "skills" / "from-claude" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_import_skips_existing(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "already")
        _make_runtime_skill(tmp_path, ".claude/skills", "already", "# Different\n")
        r = await client.post(
            "/api/context/skills/import",
            json={"overwrite": False},
        )
        data = r.json()
        assert any(s["name"] == "already" for s in data["skipped"])

    @pytest.mark.anyio
    async def test_import_empty(self, client: AsyncClient, tmp_path: Path):
        r = await client.post(
            "/api/context/skills/import",
            json={},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["imported"] == []
        # PR1: import response carries project_root + scanned_dirs so the UI
        # can tell users which paths were inspected when nothing was found.
        assert data["project_root"] == str(tmp_path)
        assert ".claude/skills" in data["scanned_dirs"]
        assert ".gemini/skills" in data["scanned_dirs"]
        assert ".agents/skills" in data["scanned_dirs"]

    @pytest.mark.anyio
    async def test_import_skipped_carries_reason_code(self, client: AsyncClient, tmp_path: Path):
        # Existing canonical + matching runtime → "canonical exists" skip.
        _make_skill(tmp_path, "already")
        _make_runtime_skill(tmp_path, ".claude/skills", "already", "# Different\n")
        r = await client.post(
            "/api/context/skills/import",
            json={"overwrite": False},
        )
        data = r.json()
        skipped_codes = {s["reason_code"] for s in data["skipped"]}
        assert "canonical_exists" in skipped_codes


class TestImportOneSkill:
    @pytest.mark.anyio
    async def test_import_single(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_skill(tmp_path, ".claude/skills", "alpha", "# A\n")
        _make_runtime_skill(tmp_path, ".claude/skills", "beta", "# B\n")
        r = await client.post("/api/context/skills/alpha/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert [i["name"] for i in data["imported"]] == ["alpha"]
        assert (tmp_path / ".memtomem" / "skills" / "alpha" / SKILL_MANIFEST).is_file()
        # Beta untouched — single-name import does not fan out.
        assert not (tmp_path / ".memtomem" / "skills" / "beta").exists()

    @pytest.mark.anyio
    async def test_import_single_kimi_only(self, client: AsyncClient, tmp_path: Path):
        """A kimi-only skill is importable — the extract loop skipped
        .kimi/skills, so this exact request 404'd while the diff pane showed
        'missing canonical' with an Import CTA (#1229)."""
        _make_runtime_skill(tmp_path, ".kimi/skills", "kimi-only", "# K\n")
        r = await client.post("/api/context/skills/kimi-only/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert [i["name"] for i in data["imported"]] == ["kimi-only"]
        assert (tmp_path / ".memtomem" / "skills" / "kimi-only" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_404_when_no_runtime_match(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_skill(tmp_path, ".claude/skills", "alpha", "# A\n")
        r = await client.post("/api/context/skills/ghost/import", json={})
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_400_on_invalid_name(self, client: AsyncClient):
        # Leading dash is rejected by validate_name before the FS is touched.
        r = await client.post("/api/context/skills/-bad/import", json={})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# Path traversal guard
# ---------------------------------------------------------------------------


class TestPathSafety:
    @pytest.mark.anyio
    async def test_slash_in_name(self, client: AsyncClient):
        """Names with slashes/backslashes are rejected before touching the FS."""
        r = await client.post(
            "/api/context/skills",
            json={"name": "sub/dir", "content": "bad"},
        )
        assert r.status_code == 400

    # Note: path tokens like "." / ".." are normalised by the HTTP layer before
    # reaching the route handler, so validate_name's reserved-token check is
    # exercised from the POST body in test_web_routes_context_mutators.py
    # (see test_POST_rejects_hostile_name with hostile_name in {".", ".."}).


# ===========================================================================
# Commands
# ===========================================================================

_CMD_CONTENT = """---
description: Review code
argument-hint: "[file-path]"
allowed-tools: [Read, Grep]
model: opus
---
Review the provided file and suggest improvements.
$ARGUMENTS
"""


def _make_command(tmp_path: Path, name: str, content: str = _CMD_CONTENT) -> Path:
    cmd_dir = tmp_path / ".memtomem" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / f"{name}.md"
    _write_text_lf(cmd_file, content)
    return cmd_file


def _make_runtime_command(
    tmp_path: Path, runtime_dir: str, name: str, ext: str = ".md", content: str = "# rt\n"
) -> Path:
    rt_dir = tmp_path / runtime_dir
    rt_dir.mkdir(parents=True, exist_ok=True)
    f = rt_dir / f"{name}{ext}"
    _write_text_lf(f, content)
    return f


class TestListCommands:
    @pytest.mark.anyio
    async def test_empty(self, client: AsyncClient):
        r = await client.get("/api/context/commands")
        assert r.status_code == 200
        data = r.json()
        # May include user-scope Codex prompts from ~/.codex/prompts/
        canonicals = [c for c in data["commands"] if c["canonical_path"] is not None]
        assert canonicals == []
        # GET surfaces canonical_root + scanned_dirs (PR1 review #1).
        assert data["canonical_root"] == ".memtomem/commands"
        assert ".claude/commands" in data["scanned_dirs"]
        assert ".gemini/commands" in data["scanned_dirs"]

    @pytest.mark.anyio
    async def test_with_items(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands")
        rows = r.json()["commands"]
        names = [c["name"] for c in rows]
        assert "review" in names
        assert {c["target_scope"] for c in rows if c["name"] == "review"} == {"project_shared"}

    @pytest.mark.anyio
    async def test_project_local_visible_only_with_explicit_target_scope(
        self, client: AsyncClient, tmp_path: Path
    ):
        local_dir = tmp_path / ".memtomem" / "commands.local"
        local_dir.mkdir(parents=True)
        _write_text_lf(local_dir / "draft.md", _CMD_CONTENT)

        default = await client.get("/api/context/commands")
        assert all(c["name"] != "draft" for c in default.json()["commands"])

        explicit = await client.get(
            "/api/context/commands",
            params={"target_scope": "project_local"},
        )
        rows = explicit.json()["commands"]
        assert explicit.status_code == 200, explicit.text
        assert rows[0]["name"] == "draft"
        assert rows[0]["target_scope"] == "project_local"


class TestReadCommand:
    @pytest.mark.anyio
    async def test_read_with_fields(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands/review")
        assert r.status_code == 200
        data = r.json()
        assert data["fields"]["description"] == "Review code"
        assert data["fields"]["model"] == "opus"

    @pytest.mark.anyio
    async def test_not_found(self, client: AsyncClient):
        r = await client.get("/api/context/commands/nope")
        assert r.status_code == 404


class TestRenderedCommand:
    @pytest.mark.anyio
    async def test_rendered_shows_dropped(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands/review/rendered")
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"]
        # Gemini drops argument-hint, allowed-tools, model
        gemini = [rt for rt in data["runtimes"] if rt["runtime"] == "gemini_commands"]
        if gemini:
            assert gemini[0]["dropped_fields"]
            assert gemini[0]["content"]  # rendered TOML

    @pytest.mark.anyio
    async def test_rendered_shows_field_map(self, client: AsyncClient, tmp_path: Path):
        # Mirrors ``TestRenderedAgent.test_rendered_shows_dropped_and_field_map``.
        # The matrix surfaces "field × runtime → kept?" so the UI can render
        # a single table across runtimes instead of forcing the user to scan
        # per-runtime ``dropped_fields`` chips and reconstruct the picture.
        _make_command(tmp_path, "review")
        r = await client.get("/api/context/commands/review/rendered")
        assert r.status_code == 200
        data = r.json()
        assert data["field_map"], "field_map must be present in /rendered response"
        # Required-fields-only rule: name + description are never tracked.
        assert "name" not in data["field_map"]
        assert "description" not in data["field_map"]
        # Optional fields all expected (hyphenated form matching what the
        # renderers emit on ``dropped_fields``).
        for expected in ("argument-hint", "allowed-tools", "model"):
            assert expected in data["field_map"], f"missing {expected!r} in field_map"
        # Pin the contract: gemini drops all three optional fields, claude
        # is pass-through. Without this assertion the field_map could ship
        # as ``{}`` for every field and the structural check above would
        # still pass.
        assert data["field_map"]["argument-hint"].get("gemini_commands") is False
        assert data["field_map"]["argument-hint"].get("claude_commands") is True
        assert data["field_map"]["model"].get("gemini_commands") is False
        assert data["field_map"]["model"].get("claude_commands") is True


class TestCommandCRUD:
    @pytest.mark.anyio
    async def test_create_update_delete(self, client: AsyncClient, tmp_path: Path):
        # Create
        r = await client.post(
            "/api/context/commands",
            json={"name": "test-cmd", "content": "---\ndescription: test\n---\nBody\n"},
        )
        assert r.status_code == 200

        # Read + update
        r = await client.get("/api/context/commands/test-cmd")
        mtime_ns = r.json()["mtime_ns"]
        r = await client.put(
            "/api/context/commands/test-cmd",
            json={
                "content": "---\ndescription: updated\n---\nNew body\n",
                "mtime_ns": mtime_ns,
            },
        )
        assert r.status_code == 200

        # Delete
        r = await client.delete("/api/context/commands/test-cmd")
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_mtime_conflict(self, client: AsyncClient, tmp_path: Path):
        # ADR-0001 §5 c4: pin no-write semantics on the conflict path. Asserting
        # the 409 response label alone would still pass a regression that wrote
        # body.content and *then* returned the abort envelope.
        cmd_file = _make_command(tmp_path, "conflict")
        r = await client.put(
            "/api/context/commands/conflict",
            json={"content": "---\ndescription: changed\n---\nNew\n", "mtime_ns": "0"},
        )
        assert r.status_code == 409
        data = r.json()
        assert data["status"] == "aborted"
        assert data["mtime_ns"] == str(cmd_file.stat().st_mtime_ns)
        assert cmd_file.read_text(encoding="utf-8") == _CMD_CONTENT


class TestDiffCommand:
    @pytest.mark.anyio
    async def test_diff_user_tier_resolves_user_runtime(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``target_scope=user`` must probe ``~/.claude/commands``, not the
        project's project_shared paths (#1229)."""
        home = tmp_path / "home"
        set_home(monkeypatch, home)
        cmd_dir = home / ".memtomem" / "commands"
        cmd_dir.mkdir(parents=True)
        cmd_file = cmd_dir / "scoped.md"
        _write_text_lf(cmd_file, _CMD_CONTENT)
        rt_dir = home / ".claude" / "commands"
        rt_dir.mkdir(parents=True)
        # The runtime side holds what sync would write (rendered output) —
        # the pane compares on the engine's basis, not raw canonical text
        # (#1247 id 30), and claude's render is near- but not byte-identity.
        rendered, _ = COMMAND_GENERATORS["claude_commands"].render(
            parse_canonical_command(cmd_file, layout="flat")
        )
        _write_text_lf(rt_dir / "scoped.md", rendered)

        r = await client.get("/api/context/commands/scoped/diff", params={"target_scope": "user"})
        assert r.status_code == 200
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_commands"][0]
        assert claude_rt["status"] == "in sync"

    @pytest.mark.anyio
    async def test_diff_project_local_has_no_runtime_rows(
        self, client: AsyncClient, tmp_path: Path
    ):
        """project_local has no runtime fan-out (ADR-0011 §3) — no fabricated
        rows against project_shared paths (#1229)."""
        local_dir = tmp_path / ".memtomem" / "commands.local"
        local_dir.mkdir(parents=True)
        _write_text_lf(local_dir / "draft.md", _CMD_CONTENT)

        r = await client.get(
            "/api/context/commands/draft/diff", params={"target_scope": "project_local"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"] == _CMD_CONTENT
        assert data["runtimes"] == []

    @pytest.mark.anyio
    async def test_diff_gemini_compares_rendered_not_raw(self, client: AsyncClient, tmp_path: Path):
        """The pane must compare on the engine's basis — rendered output —
        not the raw canonical text: gemini targets are TOML, so the raw
        compare pinned this pane to a permanent "out of sync" under an
        "in sync" list badge (#1247 id 30)."""
        path = _make_command(tmp_path, "review")
        rendered, _ = COMMAND_GENERATORS["gemini_commands"].render(
            parse_canonical_command(path, layout="flat")
        )
        _make_runtime_command(tmp_path, ".gemini/commands", "review", ".toml", rendered)

        r = await client.get("/api/context/commands/review/diff")
        assert r.status_code == 200
        gem = [rt for rt in r.json()["runtimes"] if rt["runtime"] == "gemini_commands"][0]
        assert gem["status"] == "in sync"
        assert gem["expected_content"] == rendered

    @pytest.mark.anyio
    async def test_diff_out_of_sync_expected_is_rendered(self, client: AsyncClient, tmp_path: Path):
        """``expected_content`` is what sync would write — the pane's diff
        baseline — so a drifted runtime diffs against the rendered output
        instead of md-vs-toml noise (#1247 id 30)."""
        path = _make_command(tmp_path, "review")
        rendered, _ = COMMAND_GENERATORS["gemini_commands"].render(
            parse_canonical_command(path, layout="flat")
        )
        stale = 'description = "stale"\n'
        _make_runtime_command(tmp_path, ".gemini/commands", "review", ".toml", stale)

        r = await client.get("/api/context/commands/review/diff")
        data = r.json()
        gem = [rt for rt in data["runtimes"] if rt["runtime"] == "gemini_commands"][0]
        assert gem["status"] == "out of sync"
        assert gem["expected_content"] == rendered
        assert gem["expected_content"] != data["canonical_content"]
        assert gem["runtime_content"] == stale

    @pytest.mark.anyio
    async def test_diff_override_carrying_command_in_sync(
        self, client: AsyncClient, tmp_path: Path
    ):
        """A vendor override replaces the rendered output at sync time
        (ADR-0008 Invariant 4) — the pane must compare against the override
        bytes like the engine, or override-carrying commands read permanently
        "out of sync" (#1247 id 30)."""
        art = tmp_path / ".memtomem" / "commands" / "ov"
        (art / "overrides").mkdir(parents=True)
        _write_text_lf(art / "command.md", _CMD_CONTENT)
        override = "# claude-specific replacement\n"
        _write_text_lf(art / "overrides" / "claude.md", override)
        _make_runtime_command(tmp_path, ".claude/commands", "ov", ".md", override)

        r = await client.get("/api/context/commands/ov/diff")
        claude_rt = [rt for rt in r.json()["runtimes"] if rt["runtime"] == "claude_commands"][0]
        assert claude_rt["status"] == "in sync"
        assert claude_rt["expected_content"] == override

    @pytest.mark.anyio
    async def test_diff_pane_status_matches_engine_badge(self, client: AsyncClient, tmp_path: Path):
        """Parity pin — the invariant #1247 id 30 is actually about: for the
        same rows, the per-item pane status equals the engine diff status
        that feeds the list badge."""
        path = _make_command(tmp_path, "parity")
        parsed = parse_canonical_command(path, layout="flat")
        claude_rendered, _ = COMMAND_GENERATORS["claude_commands"].render(parsed)
        _make_runtime_command(tmp_path, ".claude/commands", "parity", ".md", claude_rendered)
        _make_runtime_command(
            tmp_path, ".gemini/commands", "parity", ".toml", 'description = "stale"\n'
        )

        r = await client.get("/api/context/commands/parity/diff")
        pane = {rt["runtime"]: rt["status"] for rt in r.json()["runtimes"]}
        engine = {rt: status for rt, n, status in diff_commands(tmp_path) if n == "parity"}
        assert pane == engine
        assert engine == {"claude_commands": "in sync", "gemini_commands": "out of sync"}


class TestDeleteCommandCascade:
    @pytest.mark.anyio
    async def test_cascade_deletes_runtime_only_command(self, client: AsyncClient, tmp_path: Path):
        """``if cascade:`` was nested under ``resolved is not None`` — a
        runtime-only command + cascade=true silently no-opped with
        ``{deleted: [], skipped: []}`` (#1247 id 46; agents and skills run
        cascade as a sibling branch)."""
        rt = _make_runtime_command(tmp_path, ".claude/commands", "ghost", ".md", _CMD_CONTENT)
        r = await client.delete("/api/context/commands/ghost?cascade=true")
        assert r.status_code == 200
        data = r.json()
        assert ".claude/commands/ghost.md" in data["deleted"]
        assert data["skipped"] == []
        assert not rt.exists()


class TestDeleteSkipReasonSanitized:
    """A failed delete leg's ``skipped[].reason`` crosses the wire through
    ``sanitize_diff_reason`` — ``str(OSError)`` embeds the absolute target
    path, which used to ship raw (#1247 id 49; all three artifact types)."""

    @staticmethod
    def _refuse_unlink(self_path: Path, *args, **kwargs):
        raise OSError(13, "Permission denied", str(self_path))

    @pytest.mark.anyio
    async def test_command_unlink_failure_reason_sanitized(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        cmd_path = _make_command(tmp_path, "stuck")
        monkeypatch.setattr(Path, "unlink", self._refuse_unlink)
        r = await client.delete("/api/context/commands/stuck")
        assert r.status_code == 200
        data = r.json()
        assert data["deleted"] == []
        [skip] = data["skipped"]
        assert str(tmp_path) not in skip["reason"]
        assert "Permission denied" in skip["reason"]
        assert cmd_path.exists()

    @pytest.mark.anyio
    async def test_agent_cascade_failure_reasons_sanitized(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_agent(tmp_path, "stuck")
        _make_runtime_agent(tmp_path, ".claude/agents", "stuck")
        monkeypatch.setattr(Path, "unlink", self._refuse_unlink)
        r = await client.delete("/api/context/agents/stuck?cascade=true")
        assert r.status_code == 200
        skipped = r.json()["skipped"]
        # Canonical leg + at least the claude cascade leg both failed.
        assert len(skipped) >= 2
        for skip in skipped:
            assert str(tmp_path) not in skip["reason"]
            assert "Permission denied" in skip["reason"]

    @pytest.mark.anyio
    async def test_skill_rmtree_failure_reason_sanitized(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        skill_dir = _make_skill(tmp_path, "stuck")

        def _refuse_rmtree(path, *args, **kwargs):
            raise OSError(13, "Permission denied", str(path))

        import memtomem.web.routes.context_skills as skills_routes

        monkeypatch.setattr(skills_routes.shutil, "rmtree", _refuse_rmtree)
        r = await client.delete("/api/context/skills/stuck")
        assert r.status_code == 200
        [skip] = r.json()["skipped"]
        assert str(tmp_path) not in skip["reason"]
        assert "Permission denied" in skip["reason"]
        assert skill_dir.exists()


class TestSyncCommands:
    @pytest.mark.anyio
    async def test_sync_with_dropped(self, client: AsyncClient, tmp_path: Path):
        _make_command(tmp_path, "review")
        r = await client.post(
            "/api/context/commands/sync",
            json={"on_drop": "warn"},
        )
        assert r.status_code == 200
        data = r.json()
        assert data["generated"]
        # Some runtimes should have dropped fields (allowed-tools, model)
        assert data["dropped"]
        # PR1: canonical_root surfaced for empty-state UI.
        assert data["canonical_root"] == ".memtomem/commands"

    @pytest.mark.anyio
    async def test_sync_empty_carries_reason_code(self, client: AsyncClient):
        r = await client.post("/api/context/commands/sync", json={})
        assert r.status_code == 200
        data = r.json()
        assert any(s["reason_code"] == "no_canonical_root" for s in data["skipped"])


class TestImportCommands:
    @pytest.mark.anyio
    async def test_import_from_claude(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_command(tmp_path, ".claude/commands", "from-claude", ".md", _CMD_CONTENT)
        r = await client.post("/api/context/commands/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert any(i["name"] == "from-claude" for i in data["imported"])

    @pytest.mark.anyio
    async def test_import_empty_carries_meta(self, client: AsyncClient, tmp_path: Path):
        r = await client.post("/api/context/commands/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["project_root"] == str(tmp_path)
        assert ".claude/commands" in data["scanned_dirs"]
        assert ".gemini/commands" in data["scanned_dirs"]


class TestImportOneCommand:
    @pytest.mark.anyio
    async def test_import_single(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_command(tmp_path, ".claude/commands", "alpha", ".md", _CMD_CONTENT)
        _make_runtime_command(tmp_path, ".claude/commands", "beta", ".md", _CMD_CONTENT)
        r = await client.post("/api/context/commands/alpha/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert [i["name"] for i in data["imported"]] == ["alpha"]
        # New canonical lands in dir layout per ADR-0008.
        assert (tmp_path / ".memtomem" / "commands" / "alpha" / "command.md").is_file()
        assert not (tmp_path / ".memtomem" / "commands" / "beta" / "command.md").exists()

    @pytest.mark.anyio
    async def test_404_when_no_runtime_match(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_command(tmp_path, ".claude/commands", "alpha", ".md", _CMD_CONTENT)
        r = await client.post("/api/context/commands/ghost/import", json={})
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_400_on_invalid_name(self, client: AsyncClient):
        r = await client.post("/api/context/commands/-bad/import", json={})
        assert r.status_code == 400


# ===========================================================================
# Agents
# ===========================================================================

_AGENT_CONTENT = """---
name: reviewer
description: Code review agent
tools: [Read, Grep, Glob]
model: opus
skills: [code-review]
isolation: repo
---
You are a code review agent. Review files thoroughly.
"""


def _make_agent(tmp_path: Path, name: str, content: str = _AGENT_CONTENT) -> Path:
    agent_dir = tmp_path / ".memtomem" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / f"{name}.md"
    _write_text_lf(agent_file, content)
    return agent_file


def _make_runtime_agent(
    tmp_path: Path,
    runtime_dir: str,
    name: str,
    content: str = "---\nname: rt\ndescription: rt\n---\nBody\n",
) -> Path:
    rt_dir = tmp_path / runtime_dir
    rt_dir.mkdir(parents=True, exist_ok=True)
    f = rt_dir / f"{name}.md"
    _write_text_lf(f, content)
    return f


class TestListAgents:
    @pytest.mark.anyio
    async def test_empty(self, client: AsyncClient):
        r = await client.get("/api/context/agents")
        assert r.status_code == 200
        data = r.json()
        # Filter to canonical entries; runtime-only agents (e.g. orphan files
        # under .claude/agents) may still appear without a canonical_path.
        canonicals = [a for a in data["agents"] if a["canonical_path"] is not None]
        assert canonicals == []
        # GET surfaces canonical_root + scanned_dirs (PR1 review #1).
        assert data["canonical_root"] == ".memtomem/agents"
        assert ".claude/agents" in data["scanned_dirs"]
        assert ".gemini/agents" in data["scanned_dirs"]
        assert ".codex/agents" in data["scanned_dirs"]

    @pytest.mark.anyio
    async def test_with_items(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents")
        rows = r.json()["agents"]
        names = [a["name"] for a in rows]
        assert "reviewer" in names
        assert {a["target_scope"] for a in rows if a["name"] == "reviewer"} == {"project_shared"}

    @pytest.mark.anyio
    async def test_project_local_visible_only_with_explicit_target_scope(
        self, client: AsyncClient, tmp_path: Path
    ):
        local_dir = tmp_path / ".memtomem" / "agents.local"
        local_dir.mkdir(parents=True)
        _write_text_lf(local_dir / "draft.md", _AGENT_CONTENT)

        default = await client.get("/api/context/agents")
        assert all(a["name"] != "draft" for a in default.json()["agents"])

        explicit = await client.get(
            "/api/context/agents",
            params={"target_scope": "project_local"},
        )
        rows = explicit.json()["agents"]
        assert explicit.status_code == 200, explicit.text
        assert rows[0]["name"] == "draft"
        assert rows[0]["target_scope"] == "project_local"


class TestReadAgent:
    @pytest.mark.anyio
    async def test_read_with_fields(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents/reviewer")
        assert r.status_code == 200
        data = r.json()
        assert data["fields"]["description"] == "Code review agent"
        assert data["fields"]["model"] == "opus"
        assert "Read" in data["fields"]["tools"]

    @pytest.mark.anyio
    async def test_not_found(self, client: AsyncClient):
        r = await client.get("/api/context/agents/nope")
        assert r.status_code == 404


class TestRenderedAgent:
    @pytest.mark.anyio
    async def test_rendered_shows_dropped_and_field_map(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.get("/api/context/agents/reviewer/rendered")
        assert r.status_code == 200
        data = r.json()
        assert data["field_map"]
        # Codex should drop multiple fields
        codex = [rt for rt in data["runtimes"] if rt["runtime"] == "codex_agents"]
        if codex:
            assert len(codex[0]["dropped_fields"]) >= 3
        # Field map should show tools as False for codex
        if "tools" in data["field_map"] and "codex_agents" in data["field_map"]["tools"]:
            assert data["field_map"]["tools"]["codex_agents"] is False


class TestAgentCRUD:
    @pytest.mark.anyio
    async def test_create_update_delete(self, client: AsyncClient, tmp_path: Path):
        # Create
        r = await client.post(
            "/api/context/agents",
            json={
                "name": "test-agent",
                "content": "---\nname: test-agent\ndescription: test\n---\nBody\n",
            },
        )
        assert r.status_code == 200

        # Read + update
        r = await client.get("/api/context/agents/test-agent")
        mtime_ns = r.json()["mtime_ns"]
        r = await client.put(
            "/api/context/agents/test-agent",
            json={
                "content": "---\nname: test-agent\ndescription: updated\n---\nNew\n",
                "mtime_ns": mtime_ns,
            },
        )
        assert r.status_code == 200

        # Delete
        r = await client.delete("/api/context/agents/test-agent")
        assert r.status_code == 200

    @pytest.mark.anyio
    async def test_mtime_conflict_after_external_write(
        self,
        client: AsyncClient,
        tmp_path: Path,
    ):
        agent_path = _make_agent(tmp_path, "reviewer")

        sync = await client.post("/api/context/agents/sync", json={"on_drop": "warn"})
        assert sync.status_code == 200
        assert (tmp_path / ".claude" / "agents" / "reviewer.md").is_file()

        read = await client.get("/api/context/agents/reviewer")
        assert read.status_code == 200
        mtime_ns = read.json()["mtime_ns"]

        st = agent_path.stat()
        bumped_ns = st.st_mtime_ns + 1_000_000
        os.utime(agent_path, ns=(st.st_atime_ns, bumped_ns))

        r = await client.put(
            "/api/context/agents/reviewer",
            json={
                "content": "---\nname: reviewer\ndescription: overwritten\n---\nNew\n",
                "mtime_ns": mtime_ns,
            },
        )
        assert r.status_code == 409
        data = r.json()
        assert data["status"] == "aborted"
        assert data["mtime_ns"] == str(agent_path.stat().st_mtime_ns)
        assert "overwritten" not in agent_path.read_text(encoding="utf-8")


class TestDiffAgent:
    @pytest.mark.anyio
    async def test_malformed_canonical_reports_parse_error_with_reason(
        self, client: AsyncClient, tmp_path: Path
    ):
        """U7 (#1229): the per-name diff used to raw-compare a malformed
        canonical into 'out of sync'/'in sync' while the list badge said
        'parse error' — it now re-parses and emits the parse-error rows with
        a sanitized reason and the canonical_path for the fix-it hint."""
        _make_agent(tmp_path, "broken", "no frontmatter at all\n")
        rt_dir = tmp_path / ".claude" / "agents"
        rt_dir.mkdir(parents=True)
        _write_text_lf(rt_dir / "broken.md", _AGENT_CONTENT)

        r = await client.get("/api/context/agents/broken/diff")
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_path"] == ".memtomem/agents/broken.md"
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_agents"][0]
        assert claude_rt["status"] == "parse error"
        assert "frontmatter" in claude_rt["reason"]
        # Sanitized: the absolute tmp_path root never crosses the wire.
        assert str(tmp_path) not in claude_rt["reason"]
        # Runtime content still previews for the side-by-side view.
        assert "runtime_content" in claude_rt

    @pytest.mark.anyio
    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="needs POSIX permissions and a non-root user",
    )
    async def test_unreadable_canonical_diagnoses_instead_of_500(
        self, client: AsyncClient, tmp_path: Path
    ):
        """A PermissionError on the canonical must produce parse-error rows
        with an 'unreadable' reason — the engine diff reports the same file
        as a typed row, so a 500 here would contradict the list badge
        (Codex review on U7)."""
        path = _make_agent(tmp_path, "locked")
        path.chmod(0)
        try:
            r = await client.get("/api/context/agents/locked/diff")
        finally:
            path.chmod(0o644)
        assert r.status_code == 200
        data = r.json()
        rows = data["runtimes"]
        assert rows
        assert all(rt["status"] == "parse error" for rt in rows)
        assert all("unreadable" in rt["reason"] for rt in rows)

    @pytest.mark.anyio
    async def test_healthy_canonical_payload_has_canonical_path_and_no_reason(
        self, client: AsyncClient, tmp_path: Path
    ):
        _make_agent(tmp_path, "fine")
        r = await client.get("/api/context/agents/fine/diff")
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_path"] == ".memtomem/agents/fine.md"
        assert all("reason" not in rt for rt in data["runtimes"])

    @pytest.mark.anyio
    async def test_diff_user_tier_resolves_user_runtime(
        self, client: AsyncClient, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """``target_scope=user`` must probe ``~/.claude/agents``, not the
        project's project_shared paths (#1229)."""
        home = tmp_path / "home"
        set_home(monkeypatch, home)
        agent_dir = home / ".memtomem" / "agents"
        agent_dir.mkdir(parents=True)
        agent_file = agent_dir / "scoped.md"
        _write_text_lf(agent_file, _AGENT_CONTENT)
        rt_dir = home / ".claude" / "agents"
        rt_dir.mkdir(parents=True)
        # The runtime side holds what sync would write (rendered output) —
        # the pane compares on the engine's basis, not raw canonical text
        # (#1247 id 30), and claude's render is near- but not byte-identity.
        rendered, _ = AGENT_GENERATORS["claude_agents"].render(
            parse_canonical_agent(agent_file, layout="flat")
        )
        _write_text_lf(rt_dir / "scoped.md", rendered)

        r = await client.get("/api/context/agents/scoped/diff", params={"target_scope": "user"})
        assert r.status_code == 200
        data = r.json()
        claude_rt = [rt for rt in data["runtimes"] if rt["runtime"] == "claude_agents"][0]
        assert claude_rt["status"] == "in sync"

    @pytest.mark.anyio
    async def test_diff_project_local_has_no_runtime_rows(
        self, client: AsyncClient, tmp_path: Path
    ):
        """project_local has no runtime fan-out (ADR-0011 §3) — no fabricated
        rows against project_shared paths (#1229)."""
        local_dir = tmp_path / ".memtomem" / "agents.local"
        local_dir.mkdir(parents=True)
        _write_text_lf(local_dir / "draft.md", _AGENT_CONTENT)

        r = await client.get(
            "/api/context/agents/draft/diff", params={"target_scope": "project_local"}
        )
        assert r.status_code == 200
        data = r.json()
        assert data["canonical_content"] == _AGENT_CONTENT
        assert data["runtimes"] == []

    @pytest.mark.anyio
    async def test_diff_codex_kimi_compare_rendered_not_raw(
        self, client: AsyncClient, tmp_path: Path
    ):
        """Commands #1247 id 30 mirror: codex renders TOML and kimi renders
        YAML, so the raw canonical-vs-runtime compare pinned both panes to a
        permanent "out of sync" under an "in sync" list badge."""
        path = _make_agent(tmp_path, "shaped")
        parsed = parse_canonical_agent(path, layout="flat")
        for gen_name, runtime_dir, ext in (
            ("codex_agents", ".codex/agents", ".toml"),
            ("kimi_agents", ".kimi/agents", ".yaml"),
        ):
            rendered, _ = AGENT_GENERATORS[gen_name].render(parsed)
            rt_dir = tmp_path / runtime_dir
            rt_dir.mkdir(parents=True, exist_ok=True)
            _write_text_lf(rt_dir / f"shaped{ext}", rendered)

        r = await client.get("/api/context/agents/shaped/diff")
        assert r.status_code == 200
        rows = {rt["runtime"]: rt for rt in r.json()["runtimes"]}
        assert rows["codex_agents"]["status"] == "in sync"
        assert rows["kimi_agents"]["status"] == "in sync"

    @pytest.mark.anyio
    async def test_diff_override_carrying_agent_in_sync(self, client: AsyncClient, tmp_path: Path):
        """Override bytes are the expected side when present — engine parity
        (#1247 id 30; same contract as the commands sibling test)."""
        art = tmp_path / ".memtomem" / "agents" / "ov"
        (art / "overrides").mkdir(parents=True)
        _write_text_lf(art / "agent.md", _AGENT_CONTENT)
        override = "# claude-specific replacement\n"
        _write_text_lf(art / "overrides" / "claude.md", override)
        _make_runtime_agent(tmp_path, ".claude/agents", "ov", override)

        r = await client.get("/api/context/agents/ov/diff")
        claude_rt = [rt for rt in r.json()["runtimes"] if rt["runtime"] == "claude_agents"][0]
        assert claude_rt["status"] == "in sync"
        assert claude_rt["expected_content"] == override


class TestSyncAgents:
    @pytest.mark.anyio
    async def test_sync_with_dropped(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "reviewer")
        r = await client.post("/api/context/agents/sync", json={"on_drop": "warn"})
        assert r.status_code == 200
        data = r.json()
        assert data["generated"]
        assert data["dropped"]  # Codex/Gemini should drop fields
        # PR1: canonical_root surfaced.
        assert data["canonical_root"] == ".memtomem/agents"

    @pytest.mark.anyio
    async def test_sync_empty_carries_reason_code(self, client: AsyncClient):
        r = await client.post("/api/context/agents/sync", json={})
        assert r.status_code == 200
        data = r.json()
        assert any(s["reason_code"] == "no_canonical_root" for s in data["skipped"])


class TestImportAgents:
    @pytest.mark.anyio
    async def test_import_from_claude(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_agent(tmp_path, ".claude/agents", "from-claude")
        r = await client.post("/api/context/agents/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert any(i["name"] == "from-claude" for i in data["imported"])

    @pytest.mark.anyio
    async def test_import_empty_carries_meta(self, client: AsyncClient, tmp_path: Path):
        r = await client.post("/api/context/agents/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert data["project_root"] == str(tmp_path)
        assert ".claude/agents" in data["scanned_dirs"]
        assert ".gemini/agents" in data["scanned_dirs"]
        assert ".codex/agents" in data["scanned_dirs"]


class TestImportOneAgent:
    @pytest.mark.anyio
    async def test_import_single(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_agent(tmp_path, ".claude/agents", "alpha")
        _make_runtime_agent(tmp_path, ".claude/agents", "beta")
        r = await client.post("/api/context/agents/alpha/import", json={})
        assert r.status_code == 200
        data = r.json()
        assert [i["name"] for i in data["imported"]] == ["alpha"]
        # New canonical lands in dir layout per ADR-0008.
        assert (tmp_path / ".memtomem" / "agents" / "alpha" / "agent.md").is_file()
        assert not (tmp_path / ".memtomem" / "agents" / "beta" / "agent.md").exists()

    @pytest.mark.anyio
    async def test_404_when_no_runtime_match(self, client: AsyncClient, tmp_path: Path):
        _make_runtime_agent(tmp_path, ".claude/agents", "alpha")
        r = await client.post("/api/context/agents/ghost/import", json={})
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_400_on_invalid_name(self, client: AsyncClient):
        r = await client.post("/api/context/agents/-bad/import", json={})
        assert r.status_code == 400


# ---------------------------------------------------------------------------
# target_scope plumbing through item routes (#940 r3)
#
# Reads honor every tier; writes (create/update/delete/sync/import) accept
# the param, 400-reject ``project_local`` via ``_reject_project_local_write``,
# and since #1263 accept ``user`` behind the allow_host_writes confirm
# round-trip (pinned in test_web_routes_context_user_tier.py). Pins the
# contract from both sides so a regression on either gate shows up
# immediately.
# ---------------------------------------------------------------------------


def _make_local_skill(tmp_path: Path, name: str, content: str = "# Local\n") -> Path:
    skill_dir = tmp_path / ".memtomem" / "skills.local" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    _write_text_lf(skill_dir / SKILL_MANIFEST, content)
    return skill_dir


def _make_local_agent(tmp_path: Path, name: str, content: str = _AGENT_CONTENT) -> Path:
    agent_dir = tmp_path / ".memtomem" / "agents.local"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / f"{name}.md"
    _write_text_lf(agent_file, content)
    return agent_file


def _make_local_command(tmp_path: Path, name: str, content: str = _CMD_CONTENT) -> Path:
    cmd_dir = tmp_path / ".memtomem" / "commands.local"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / f"{name}.md"
    _write_text_lf(cmd_file, content)
    return cmd_file


class TestSkillTargetScopePlumbing:
    @pytest.mark.anyio
    async def test_read_project_local_returns_local_canonical(
        self, client: AsyncClient, tmp_path: Path
    ):
        _make_skill(tmp_path, "twin", content="# shared\n")
        _make_local_skill(tmp_path, "twin", content="# local draft\n")

        shared = await client.get("/api/context/skills/twin")
        assert shared.status_code == 200
        assert "# shared" in shared.json()["content"]

        local = await client.get(
            "/api/context/skills/twin", params={"target_scope": "project_local"}
        )
        assert local.status_code == 200
        assert "# local draft" in local.json()["content"]

    @pytest.mark.anyio
    async def test_create_rejects_non_shared(self, client: AsyncClient):
        r = await client.post(
            "/api/context/skills",
            params={"target_scope": "project_local"},
            json={"name": "draft", "content": "# x\n"},
        )
        assert r.status_code == 400
        assert "project_shared" in r.json()["detail"]["message"]
        assert r.json()["detail"]["error_kind"] == "validation"
        assert r.json()["detail"]["reason_code"] == "project_local_unsupported"

    @pytest.mark.anyio
    async def test_sync_rejects_project_local(self, client: AsyncClient):
        """user-tier sync is open since #1263 — project_local stays 400."""
        r = await client.post("/api/context/skills/sync", params={"target_scope": "project_local"})
        assert r.status_code == 400
        assert "project_shared" in r.json()["detail"]["message"]
        assert r.json()["detail"]["error_kind"] == "validation"
        assert r.json()["detail"]["reason_code"] == "project_local_unsupported"


class TestAgentTargetScopePlumbing:
    @pytest.mark.anyio
    async def test_read_project_local_returns_local_canonical(
        self, client: AsyncClient, tmp_path: Path
    ):
        _make_agent(tmp_path, "twin", _AGENT_CONTENT.replace("Code review agent", "shared agent"))
        _make_local_agent(
            tmp_path, "twin", _AGENT_CONTENT.replace("Code review agent", "local draft agent")
        )

        shared = await client.get("/api/context/agents/twin")
        assert shared.status_code == 200
        assert "shared agent" in shared.json()["content"]

        local = await client.get(
            "/api/context/agents/twin", params={"target_scope": "project_local"}
        )
        assert local.status_code == 200
        assert "local draft agent" in local.json()["content"]

    @pytest.mark.anyio
    async def test_delete_rejects_non_shared(self, client: AsyncClient, tmp_path: Path):
        _make_local_agent(tmp_path, "draft")
        r = await client.delete(
            "/api/context/agents/draft", params={"target_scope": "project_local"}
        )
        assert r.status_code == 400


class TestCommandTargetScopePlumbing:
    @pytest.mark.anyio
    async def test_read_project_local_returns_local_canonical(
        self, client: AsyncClient, tmp_path: Path
    ):
        _make_command(tmp_path, "twin", _CMD_CONTENT.replace("Review code", "shared cmd"))
        _make_local_command(
            tmp_path, "twin", _CMD_CONTENT.replace("Review code", "local draft cmd")
        )

        shared = await client.get("/api/context/commands/twin")
        assert shared.status_code == 200
        assert "shared cmd" in shared.json()["content"]

        local = await client.get(
            "/api/context/commands/twin", params={"target_scope": "project_local"}
        )
        assert local.status_code == 200
        assert "local draft cmd" in local.json()["content"]

    @pytest.mark.anyio
    async def test_update_rejects_project_local(self, client: AsyncClient, tmp_path: Path):
        """user-tier update is open since #1263 — project_local stays 400."""
        _make_local_command(tmp_path, "draft")
        # Need an mtime_ns; doesn't matter — the gate fires before mtime check.
        r = await client.put(
            "/api/context/commands/draft",
            params={"target_scope": "project_local"},
            json={"content": "# x\n", "mtime_ns": "0"},
        )
        assert r.status_code == 400
        assert "project_shared" in r.json()["detail"]["message"]
        assert r.json()["detail"]["error_kind"] == "validation"
        assert r.json()["detail"]["reason_code"] == "project_local_unsupported"


# ---------------------------------------------------------------------------
# Gate A surface attribution (#1229)
# ---------------------------------------------------------------------------


class TestImportSurfaceAttribution:
    """Web imports must reach the privacy audit log under their own surface
    string — pre-#1229 every ingress was misattributed to the CLI literal
    ``cli_context_init``."""

    def _spy_surfaces(self, monkeypatch) -> list[str]:
        from memtomem.privacy import WriteGuardResult

        surfaces: list[str] = []

        def spy(content_text, *, surface, **kw):
            surfaces.append(surface)
            return WriteGuardResult("pass", [])

        monkeypatch.setattr("memtomem.context._gate_a.privacy.enforce_write_guard", spy)
        return surfaces

    @pytest.mark.anyio
    async def test_bulk_imports_use_web_surfaces(
        self, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        _make_runtime_skill(tmp_path, ".claude/skills", "s1", "# S\n")
        agents_dir = tmp_path / ".claude" / "agents"
        agents_dir.mkdir(parents=True)
        _write_text_lf(agents_dir / "a1.md", "---\nname: a1\n---\nbody\n")
        commands_dir = tmp_path / ".claude" / "commands"
        commands_dir.mkdir(parents=True)
        _write_text_lf(commands_dir / "c1.md", "---\nname: c1\n---\nbody\n")

        surfaces = self._spy_surfaces(monkeypatch)

        r = await client.post("/api/context/skills/import", json={})
        assert r.status_code == 200
        assert set(surfaces) == {"web_context_skills_import"}

        surfaces.clear()
        r = await client.post("/api/context/agents/import", json={})
        assert r.status_code == 200
        assert set(surfaces) == {"web_context_agents_import"}

        surfaces.clear()
        r = await client.post("/api/context/commands/import", json={})
        assert r.status_code == 200
        assert set(surfaces) == {"web_context_commands_import"}

    @pytest.mark.anyio
    async def test_single_name_import_uses_web_surface(
        self, client: AsyncClient, tmp_path: Path, monkeypatch
    ):
        _make_runtime_skill(tmp_path, ".claude/skills", "solo", "# S\n")
        surfaces = self._spy_surfaces(monkeypatch)
        r = await client.post("/api/context/skills/solo/import", json={})
        assert r.status_code == 200
        assert set(surfaces) == {"web_context_skills_import"}


# ---------------------------------------------------------------------------
# U7 (#1229) — list endpoints carry sanitized diagnostic reasons
# ---------------------------------------------------------------------------


class TestListDiagnosticReasons:
    @pytest.mark.anyio
    async def test_parse_error_entry_carries_sanitized_reason(
        self, client: AsyncClient, tmp_path: Path
    ):
        _make_agent(tmp_path, "broken", "no frontmatter at all\n")
        r = await client.get("/api/context/agents")
        assert r.status_code == 200
        item = [a for a in r.json()["agents"] if a["name"] == "broken"][0]
        entries = [e for e in item["runtimes"] if e["status"] == "parse error"]
        assert entries
        for e in entries:
            assert "frontmatter" in e["reason"]
            assert "broken.md" in e["reason"]
            # Embedded absolute path stripped at the route boundary — the
            # engine reason text embeds str(source_path).
            assert str(tmp_path) not in e["reason"]

    @pytest.mark.anyio
    async def test_healthy_entries_have_no_reason_key(self, client: AsyncClient, tmp_path: Path):
        _make_agent(tmp_path, "fine")
        r = await client.get("/api/context/agents")
        item = [a for a in r.json()["agents"] if a["name"] == "fine"][0]
        assert all("reason" not in e for e in item["runtimes"])


# ---------------------------------------------------------------------------
# Gate A sync surface attribution (#1246)
# ---------------------------------------------------------------------------


class TestSyncSurfaceAttribution:
    """Web syncs must reach the privacy audit log under their own surface
    string — pre-#1246 every sync ingress was misattributed to the CLI
    literal ``cli_context_sync``. Sibling of the import-side
    ``TestImportSurfaceAttribution`` pins (#1229/#1242); the spy point
    differs because sync funnels through ``privacy_scan``, not ``_gate_a``.
    """

    def _spy_surfaces(self, monkeypatch) -> list[str]:
        from memtomem.privacy import WriteGuardResult

        surfaces: list[str] = []

        def spy(content_text, *, surface, **kw):
            surfaces.append(surface)
            return WriteGuardResult("pass", [])

        monkeypatch.setattr("memtomem.context.privacy_scan.privacy.enforce_write_guard", spy)
        return surfaces

    @pytest.mark.anyio
    async def test_syncs_use_web_surfaces(self, client: AsyncClient, tmp_path: Path, monkeypatch):
        _make_skill(tmp_path, "s1")
        _make_agent(tmp_path, "a1")
        _make_command(tmp_path, "c1")

        surfaces = self._spy_surfaces(monkeypatch)

        r = await client.post("/api/context/skills/sync")
        assert r.status_code == 200, r.text
        assert surfaces and set(surfaces) == {"web_context_skills_sync"}

        surfaces.clear()
        r = await client.post("/api/context/agents/sync", json={})
        assert r.status_code == 200, r.text
        assert surfaces and set(surfaces) == {"web_context_agents_sync"}

        surfaces.clear()
        r = await client.post("/api/context/commands/sync", json={})
        assert r.status_code == 200, r.text
        assert surfaces and set(surfaces) == {"web_context_commands_sync"}
