"""Gate A surface attribution for the sync/generate and migrate directions (#1246).

Sibling of the import-side pins in ``test_context_init_scope.py`` (#1242):
the ``surface=`` string dimensions the privacy ``record()`` counter and
tags the blocked audit log line, distinguishing every ingress surface.
Pre-#1246 the sync/generate and migrate engines hard-coded the CLI
literals (``cli_context_sync`` / ``cli_context_migrate``), so every Web-
or MCP-driven privacy decision was misattributed to the CLI.

Spy point: ``memtomem.context.privacy_scan.privacy.enforce_write_guard``
— the one chokepoint all three engines funnel through
(``generate_all_skills``'s two ``scan_artifact_tree`` sites, the atomic
agents/commands engine's two ``scan_text_content`` sites, and
``migrate_scope``'s staging scan).

Layer pins for the Web routes live in ``test_web_routes_context.py``;
the MCP tool pins live in ``test_server_tools_context_scope.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from memtomem.context.agents import generate_all_agents
from memtomem.context.commands import generate_all_commands
from memtomem.context.migrate import migrate_scope
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import SKILL_MANIFEST, generate_all_skills
from memtomem.privacy import WriteGuardResult

from .helpers import set_home


def _capture_guard_surfaces(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Spy ``privacy.enforce_write_guard`` and record every call's ``surface``."""
    surfaces: list[str] = []

    def spy(content_text: str, *, surface: str, **kw: Any) -> WriteGuardResult:
        surfaces.append(surface)
        return WriteGuardResult("pass", [])

    monkeypatch.setattr("memtomem.context.privacy_scan.privacy.enforce_write_guard", spy)
    return surfaces


def _seed_skill(project_root: Path, name: str, *, scope: str) -> Path:
    root = canonical_artifact_dir("skills", scope, project_root)  # type: ignore[arg-type]
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text("# test skill\n", encoding="utf-8")
    return skill_dir


def _seed_agent_dir_with_override(project_root: Path, name: str) -> None:
    """Dir-layout canonical + a claude override — exercises BOTH atomic-engine
    scan sites (canonical bytes and override bytes) in one fan-out."""
    agent_dir = canonical_artifact_dir("agents", "project_shared", project_root) / name
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "agent.md").write_text(
        f"---\nname: {name}\ndescription: example\n---\nbody\n", encoding="utf-8"
    )
    overrides = agent_dir / "overrides"
    overrides.mkdir()
    (overrides / "claude.md").write_text("# claude override\n", encoding="utf-8")


class TestSkillsSyncSurface:
    def test_project_shared_batch_site(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The project_shared all-or-nothing batch path: default pins the CLI
        literal; the kwarg threads through to the same scan site."""
        _seed_skill(tmp_path, "sk", scope="project_shared")
        surfaces = _capture_guard_surfaces(monkeypatch)

        generate_all_skills(tmp_path, runtimes=["claude_skills"])
        assert surfaces == ["cli_context_sync"]

        surfaces.clear()
        generate_all_skills(tmp_path, runtimes=["claude_skills"], surface="probe_skills_batch")
        assert surfaces == ["probe_skills_batch"]

    def test_per_destination_site(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """The non-shared per-destination path is a SECOND scan site in
        ``generate_all_skills`` — missing it would silently misattribute
        every user/project_local fan-out."""
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_skill(tmp_path, "sk", scope="user")
        surfaces = _capture_guard_surfaces(monkeypatch)

        generate_all_skills(tmp_path, runtimes=["claude_skills"], scope="user")
        assert surfaces == ["cli_context_sync"]

        surfaces.clear()
        generate_all_skills(
            tmp_path, runtimes=["claude_skills"], scope="user", surface="probe_skills_per_dst"
        )
        assert surfaces == ["probe_skills_per_dst"]


class TestAtomicEngineSurface:
    def test_agents_canonical_and_override_sites(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """The shared atomic engine scans canonical bytes AND per-vendor
        override bytes — both sites must carry the caller's surface."""
        _seed_agent_dir_with_override(tmp_path, "agt")
        surfaces = _capture_guard_surfaces(monkeypatch)

        generate_all_agents(tmp_path, runtimes=["claude_agents"])
        assert surfaces == ["cli_context_sync", "cli_context_sync"]  # canonical + override

        surfaces.clear()
        generate_all_agents(tmp_path, runtimes=["claude_agents"], surface="probe_agents")
        assert surfaces == ["probe_agents", "probe_agents"]

    def test_commands_wrapper_threads_surface(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Both thin wrappers bind the same engine; each must forward the
        kwarg (a wrapper that drops it would silently fall back to the
        CLI literal for that artifact kind only)."""
        commands = canonical_artifact_dir("commands", "project_shared", tmp_path)
        commands.mkdir(parents=True, exist_ok=True)
        (commands / "cmd.md").write_text("---\ndescription: example\n---\nbody\n", encoding="utf-8")
        surfaces = _capture_guard_surfaces(monkeypatch)

        generate_all_commands(tmp_path, runtimes=["claude_commands"])
        assert surfaces == ["cli_context_sync"]

        surfaces.clear()
        generate_all_commands(tmp_path, runtimes=["claude_commands"], surface="probe_commands")
        assert surfaces == ["probe_commands"]


class TestMigrateScopeSurface:
    def _seed_user_agent(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        user_agents = canonical_artifact_dir("agents", "user", tmp_path)
        user_agents.mkdir(parents=True, exist_ok=True)
        path = user_agents / "agt.md"
        path.write_text("---\nname: agt\ndescription: example\n---\nbody\n", encoding="utf-8")
        return path

    def test_default_surface_is_cli_literal(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        self._seed_user_agent(tmp_path, monkeypatch)
        surfaces = _capture_guard_surfaces(monkeypatch)

        result = migrate_scope(
            "agents",
            "agt",
            from_scope="user",
            to_scope="project_shared",
            project_root=tmp_path,
            apply_=True,
        )

        assert result.moved
        assert surfaces == ["cli_context_migrate"]

    def test_surface_kwarg_reaches_staging_scan(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._seed_user_agent(tmp_path, monkeypatch)
        surfaces = _capture_guard_surfaces(monkeypatch)

        result = migrate_scope(
            "agents",
            "agt",
            from_scope="user",
            to_scope="project_shared",
            project_root=tmp_path,
            apply_=True,
            surface="probe_migrate",
        )

        assert result.moved
        assert surfaces == ["probe_migrate"]
