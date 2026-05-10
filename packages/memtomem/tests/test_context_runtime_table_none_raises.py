"""ADR-0011 PR-E follow-up N1 — fail-loud parity for runtime-table None.

Each ``generate_all_*`` and ``diff_*`` function previously protected its
``target_file`` / ``target_dir`` contract with a bare ``assert x is not None``
that strips under ``python -O``. The follow-up converts those to explicit
``if x is None: raise RuntimeError(...)`` so the contract holds regardless
of optimization mode. These tests pin the new fail-loud behaviour by
forcing the runtime-fanout table to return ``None`` for one generator and
asserting ``RuntimeError`` propagates through the loop.

The branches are contractually unreachable on the default
``scope="project_shared"`` (the table never returns ``None`` for that
tuple) — see ``feedback_pin_test_mutation_validation.md``: pin the new
fail-loud branch with one mutation per kind so the contract isn't
silently weaker than the comment claims.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
from memtomem.context.skills import (
    SKILL_GENERATORS,
    diff_skills,
    generate_all_skills,
)


def _seed_canonical_agent(project_root: Path, name: str = "foo") -> None:
    canonical = project_root / ".memtomem" / "agents"
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    # diff_agents only reaches the target_file check when the name appears
    # in both canonical and runtime sets — seed the Claude runtime side too.
    runtime = project_root / ".claude" / "agents"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _seed_canonical_skill(project_root: Path, name: str = "foo") -> None:
    skill = project_root / ".memtomem" / "skills" / name
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    runtime_skill = project_root / ".claude" / "skills" / name
    runtime_skill.mkdir(parents=True, exist_ok=True)
    (runtime_skill / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


def _seed_canonical_command(project_root: Path, name: str = "foo") -> None:
    canonical = project_root / ".memtomem" / "commands"
    canonical.mkdir(parents=True, exist_ok=True)
    (canonical / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    runtime = project_root / ".claude" / "commands"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / f"{name}.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")


@pytest.mark.parametrize(
    "kind, seed, runner, runtime_key, registry, attr",
    [
        (
            "agents",
            _seed_canonical_agent,
            generate_all_agents,
            "claude_agents",
            AGENT_GENERATORS,
            "target_file",
        ),
        (
            "skills",
            _seed_canonical_skill,
            generate_all_skills,
            "claude_skills",
            SKILL_GENERATORS,
            "target_dir",
        ),
        (
            "commands",
            _seed_canonical_command,
            generate_all_commands,
            "claude_commands",
            COMMAND_GENERATORS,
            "target_file",
        ),
    ],
)
def test_generate_all_runtime_table_none_raises(
    kind: str,
    seed,
    runner,
    runtime_key: str,
    registry,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1 — generate_all_<kind> raises RuntimeError when the registered
    generator's ``target_*`` returns ``None``.

    Forces ``None`` by monkeypatching the registered generator's bound
    method, which simulates a future scope wiring where the table entry
    legitimately becomes ``NO_FANOUT`` for the active scope.
    """
    seed(tmp_path)
    gen = registry[runtime_key]
    monkeypatch.setattr(gen, attr, lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match=r"target_(file|dir) returned None"):
        runner(tmp_path, runtimes=[runtime_key])


@pytest.mark.parametrize(
    "kind, seed, runner, runtime_key, registry, attr",
    [
        (
            "agents",
            _seed_canonical_agent,
            diff_agents,
            "claude_agents",
            AGENT_GENERATORS,
            "target_file",
        ),
        (
            "skills",
            _seed_canonical_skill,
            diff_skills,
            "claude_skills",
            SKILL_GENERATORS,
            "target_dir",
        ),
        (
            "commands",
            _seed_canonical_command,
            diff_commands,
            "claude_commands",
            COMMAND_GENERATORS,
            "target_file",
        ),
    ],
)
def test_diff_runtime_table_none_raises(
    kind: str,
    seed,
    runner,
    runtime_key: str,
    registry,
    attr: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """N1 — diff_<kind> raises RuntimeError when ``target_*`` returns ``None``."""
    seed(tmp_path)
    gen = registry[runtime_key]
    monkeypatch.setattr(gen, attr, lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match=r"target_(file|dir) returned None"):
        runner(tmp_path)
