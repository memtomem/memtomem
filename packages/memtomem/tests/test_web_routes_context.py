"""Tests for Context Gateway web routes (overview + skills + commands + agents)."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app


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
    (skill_dir / SKILL_MANIFEST).write_text(content, encoding="utf-8")
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
    (skill_dir / SKILL_MANIFEST).write_text(content, encoding="utf-8")
    return skill_dir


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
    async def test_overview_detected_runtimes_shape(self, client: AsyncClient):
        r = await client.get("/api/context/overview")
        data = r.json()
        runtimes = data["detected_runtimes"]
        assert isinstance(runtimes, list)
        # One entry per ``KNOWN_RUNTIMES``; greyed chips for absent runtimes
        # are part of the contract — the frontend renders them as undetected.
        names = [r["name"] for r in runtimes]
        assert names == ["claude", "gemini", "codex"]
        for entry in runtimes:
            assert isinstance(entry["available"], bool)

    @pytest.mark.anyio
    async def test_overview_detected_runtimes_flags_present_surfaces(
        self, client: AsyncClient, tmp_path: Path
    ):
        # CLAUDE.md (top-level agent file) → claude detected
        (tmp_path / "CLAUDE.md").write_text("# x\n", encoding="utf-8")
        # .gemini/skills/foo/SKILL.md → gemini detected via skill dir
        gem_skill = tmp_path / ".gemini" / "skills" / "foo"
        gem_skill.mkdir(parents=True)
        (gem_skill / SKILL_MANIFEST).write_text("# g\n", encoding="utf-8")

        r = await client.get("/api/context/overview")
        runtimes = {r["name"]: r["available"] for r in r.json()["detected_runtimes"]}
        assert runtimes == {"claude": True, "gemini": True, "codex": False}

    @pytest.mark.anyio
    async def test_project_local_overview_visible_only_with_explicit_target_scope(
        self, client: AsyncClient, tmp_path: Path
    ):
        local = tmp_path / ".memtomem" / "skills.local" / "draft"
        local.mkdir(parents=True)
        (local / SKILL_MANIFEST).write_text("# Draft\n", encoding="utf-8")

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
        (local / SKILL_MANIFEST).write_text("# Draft\n", encoding="utf-8")

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
    async def test_auxiliary_files(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "rich")
        scripts_dir = tmp_path / ".memtomem" / "skills" / "rich" / "scripts"
        scripts_dir.mkdir()
        (scripts_dir / "run.sh").write_text("#!/bin/bash\necho hello\n", encoding="utf-8")
        r = await client.get("/api/context/skills/rich")
        data = r.json()
        paths = [f["path"] for f in data["files"]]
        assert any("run.sh" in p for p in paths)


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
        # Verify file exists on disk
        assert (tmp_path / ".memtomem" / "skills" / "new-skill" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_create_duplicate(self, client: AsyncClient, tmp_path: Path):
        _make_skill(tmp_path, "existing")
        r = await client.post(
            "/api/context/skills",
            json={"name": "existing", "content": "# Dup\n"},
        )
        assert r.status_code == 400

    @pytest.mark.anyio
    async def test_create_invalid_name(self, client: AsyncClient):
        r = await client.post(
            "/api/context/skills",
            json={"name": "../escape", "content": "bad"},
        )
        assert r.status_code == 400


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
    cmd_file.write_text(content, encoding="utf-8")
    return cmd_file


def _make_runtime_command(
    tmp_path: Path, runtime_dir: str, name: str, ext: str = ".md", content: str = "# rt\n"
) -> Path:
    rt_dir = tmp_path / runtime_dir
    rt_dir.mkdir(parents=True, exist_ok=True)
    f = rt_dir / f"{name}{ext}"
    f.write_text(content, encoding="utf-8")
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
        (local_dir / "draft.md").write_text(_CMD_CONTENT, encoding="utf-8")

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
    agent_file.write_text(content, encoding="utf-8")
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
    f.write_text(content, encoding="utf-8")
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
        (local_dir / "draft.md").write_text(_AGENT_CONTENT, encoding="utf-8")

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
# the param but reject anything other than ``project_shared`` with HTTP 400
# via ``_reject_non_shared_write``. Pins the contract from both sides so a
# regression on either gate shows up immediately.
# ---------------------------------------------------------------------------


def _make_local_skill(tmp_path: Path, name: str, content: str = "# Local\n") -> Path:
    skill_dir = tmp_path / ".memtomem" / "skills.local" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(content, encoding="utf-8")
    return skill_dir


def _make_local_agent(tmp_path: Path, name: str, content: str = _AGENT_CONTENT) -> Path:
    agent_dir = tmp_path / ".memtomem" / "agents.local"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / f"{name}.md"
    agent_file.write_text(content, encoding="utf-8")
    return agent_file


def _make_local_command(tmp_path: Path, name: str, content: str = _CMD_CONTENT) -> Path:
    cmd_dir = tmp_path / ".memtomem" / "commands.local"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / f"{name}.md"
    cmd_file.write_text(content, encoding="utf-8")
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
        assert "project_shared" in r.json()["detail"]

    @pytest.mark.anyio
    async def test_sync_rejects_non_shared(self, client: AsyncClient):
        r = await client.post("/api/context/skills/sync", params={"target_scope": "user"})
        assert r.status_code == 400


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
    async def test_update_rejects_non_shared(self, client: AsyncClient, tmp_path: Path):
        _make_local_command(tmp_path, "draft")
        # Need an mtime_ns; doesn't matter — the gate fires before mtime check.
        r = await client.put(
            "/api/context/commands/draft",
            params={"target_scope": "user"},
            json={"content": "# x\n", "mtime_ns": "0"},
        )
        assert r.status_code == 400
