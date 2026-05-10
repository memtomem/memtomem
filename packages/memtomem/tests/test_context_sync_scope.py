"""ADR-0011 PR-E3 — sync --scope thread-through end-to-end.

Pins the per-scope behavior of ``generate_all_agents`` /
``generate_all_commands`` (the canonical → runtime fan-out surface) at
each of the three scopes:

* ``project_shared`` — Gate A fail-fast hard-refusal on first secret.
* ``user`` — Gate A skip-and-warn (no force_unsafe valve in sync).
* ``project_local`` — every runtime emits ``NO_PROJECT_FANOUT_FOR_RUNTIME``
  per :data:`RUNTIME_FANOUT_TABLE` (ADR §3 — gitignored draft tier has
  no runtime equivalent).

Skills are exercised in ``test_context_skills_staging.py``; this file
covers the agents + commands code paths and the project_local
NO_FANOUT contract that applies uniformly across all three artifact
kinds.
"""

from __future__ import annotations

from pathlib import Path

import click
import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context.agents import (
    AGENT_GENERATORS,
    diff_agents,
    generate_all_agents,
)
from memtomem.context.commands import (
    COMMAND_GENERATORS,
    diff_commands,
    generate_all_commands,
)
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import diff_skills

from .helpers import set_home

SECRET = "api_key=AKIA1234567890ABCDEF"


def _clean_agent_body(name: str) -> str:
    return f"---\nname: {name}\ndescription: example\n---\nbody\n"


def _clean_command_body() -> str:
    return "---\ndescription: example\n---\nbody\n"


def _seed_agent(
    project_root: Path,
    name: str,
    *,
    scope: str,
    body: str,
) -> Path:
    canonical = canonical_artifact_dir("agents", scope, project_root)  # type: ignore[arg-type]
    canonical.mkdir(parents=True, exist_ok=True)
    path = canonical / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


def _seed_command(
    project_root: Path,
    name: str,
    *,
    scope: str,
    body: str,
) -> Path:
    canonical = canonical_artifact_dir("commands", scope, project_root)  # type: ignore[arg-type]
    canonical.mkdir(parents=True, exist_ok=True)
    path = canonical / f"{name}.md"
    path.write_text(body, encoding="utf-8")
    return path


class TestAgentsSyncScopeMatrix:
    def test_project_shared_secret_raises(self, tmp_path: Path) -> None:
        _seed_agent(
            tmp_path,
            "leak",
            scope="project_shared",
            body=f"---\nname: leak\n---\nuses {SECRET}\n",
        )
        gen = AGENT_GENERATORS["claude_agents"]
        dst = gen.target_file(tmp_path, "leak", scope="project_shared")
        with pytest.raises(click.ClickException) as exc_info:
            generate_all_agents(tmp_path, runtimes=["claude_agents"], scope="project_shared")
        assert "Gate A" in exc_info.value.message
        assert "leak" in exc_info.value.message
        # Negative pin: dst NOT created.
        assert dst is not None
        assert not dst.exists()

    def test_user_secret_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_agent(
            tmp_path,
            "leak",
            scope="user",
            body=f"---\nname: leak\n---\nuses {SECRET}\n",
        )
        # Add a clean agent to verify "other agents fan out OK".
        _seed_agent(tmp_path, "ok", scope="user", body=_clean_agent_body("ok"))
        gen = AGENT_GENERATORS["claude_agents"]
        dst_leak = gen.target_file(tmp_path, "leak", scope="user")
        dst_ok = gen.target_file(tmp_path, "ok", scope="user")
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], scope="user")
        # Positive: skip emitted with PRIVACY_BLOCKED code.
        privacy_skips = [s for s in result.skipped if s[2] == skip_codes.PRIVACY_BLOCKED]
        assert len(privacy_skips) == 1, result.skipped
        assert privacy_skips[0][0] == "leak"
        # Positive: clean agent fanned out.
        names_generated = [path.stem for _runtime, path in result.generated]
        assert "ok" in names_generated
        # Negative: leaked agent not on disk.
        assert dst_leak is not None
        assert not dst_leak.exists()
        # Positive: clean agent on disk.
        assert dst_ok is not None
        assert dst_ok.exists()

    def test_project_local_emits_no_fanout(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path, "ok", scope="project_local", body=_clean_agent_body("ok"))
        result = generate_all_agents(tmp_path, runtimes=["claude_agents"], scope="project_local")
        # Positive: NO_PROJECT_FANOUT_FOR_RUNTIME emitted.
        no_fanout = [s for s in result.skipped if s[2] == skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME]
        assert len(no_fanout) == 1, result.skipped
        # Negative: nothing generated.
        assert result.generated == []


class TestCommandsSyncScopeMatrix:
    def test_project_shared_secret_raises(self, tmp_path: Path) -> None:
        _seed_command(
            tmp_path,
            "leak",
            scope="project_shared",
            body=f"---\ndescription: leak\n---\nuses {SECRET}\n",
        )
        gen = COMMAND_GENERATORS["claude_commands"]
        dst = gen.target_file(tmp_path, "leak", scope="project_shared")
        with pytest.raises(click.ClickException) as exc_info:
            generate_all_commands(tmp_path, runtimes=["claude_commands"], scope="project_shared")
        assert "Gate A" in exc_info.value.message
        assert dst is not None
        assert not dst.exists()

    def test_user_secret_skips(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_command(
            tmp_path,
            "leak",
            scope="user",
            body=f"---\ndescription: leak\n---\nuses {SECRET}\n",
        )
        _seed_command(tmp_path, "ok", scope="user", body=_clean_command_body())
        result = generate_all_commands(tmp_path, runtimes=["claude_commands"], scope="user")
        privacy_skips = [s for s in result.skipped if s[2] == skip_codes.PRIVACY_BLOCKED]
        assert len(privacy_skips) == 1, result.skipped
        names_generated = [path.stem for _runtime, path in result.generated]
        assert "ok" in names_generated
        assert "leak" not in names_generated


class TestDiffScopeProjectLocal:
    """diff_* at scope=project_local emits zero rows for every runtime."""

    def test_diff_agents_project_local(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path, "ok", scope="project_local", body=_clean_agent_body("ok"))
        rows = diff_agents(tmp_path, scope="project_local")
        # All registered agents runtimes return None for project_local
        # per RUNTIME_FANOUT_TABLE → upstream NO_FANOUT guard short-
        # circuits → empty result.
        assert rows == []

    def test_diff_commands_project_local(self, tmp_path: Path) -> None:
        _seed_command(tmp_path, "ok", scope="project_local", body=_clean_command_body())
        rows = diff_commands(tmp_path, scope="project_local")
        assert rows == []

    def test_diff_skills_project_local(self, tmp_path: Path) -> None:
        # Seed a project_local skill canonical.
        canonical = canonical_artifact_dir("skills", "project_local", tmp_path)
        skill_dir = canonical / "ok"
        skill_dir.mkdir(parents=True)
        (skill_dir / "SKILL.md").write_text("---\nname: ok\n---\nbody\n", encoding="utf-8")
        rows = diff_skills(tmp_path, scope="project_local")
        assert rows == []


class TestDefaultScopePreservation:
    """Sync at the default scope (no --scope) reads project_shared canonical."""

    def test_default_kwarg_matches_explicit_project_shared(self, tmp_path: Path) -> None:
        _seed_agent(tmp_path, "ok", scope="project_shared", body=_clean_agent_body("ok"))
        # No scope= kwarg.
        result_default = generate_all_agents(tmp_path, runtimes=["claude_agents"])
        # Explicit project_shared — must produce the same generated set.
        # Re-seed in a fresh tree because generated state is on-disk.
        result_explicit = generate_all_agents(
            tmp_path, runtimes=["claude_agents"], scope="project_shared"
        )
        names_default = [p.stem for _r, p in result_default.generated]
        names_explicit = [p.stem for _r, p in result_explicit.generated]
        assert names_default == names_explicit
