"""ADR-0011 PR-E2 — per-scope source/destination pinning for ``extract_*_to_canonical``.

Mirrors ``test_context_generator_scope_kwarg.py`` but on the import side:
each scope reads from a different runtime root (user vs project) and writes
to a different canonical root.

Source seeded under both ``$HOME/.claude/...`` and ``<proj>/.claude/...``;
each test verifies the scope kwarg picks the right source AND lands at the
right canonical destination.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context import _skip_reasons as skip_codes
from .helpers import set_home
from memtomem.context.agents import extract_agents_to_canonical
from memtomem.context.commands import extract_commands_to_canonical
from memtomem.context.skills import extract_skills_to_canonical


# ── Common fixtures ────────────────────────────────────────────────────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, str(h))
    return h


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    p.mkdir()
    (p / ".git").mkdir()
    return p


def _write(path: Path, content: str = "---\nname: foo\n---\nbody\n") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


# ── Default kwarg matches project_shared ────────────────────────────────


def test_extract_agents_default_kwarg_matches_project_shared(home: Path, proj: Path) -> None:
    """``extract_agents_to_canonical(proj)`` must equal scope=project_shared."""
    _write(proj / ".claude" / "agents" / "foo.md")
    default_run = extract_agents_to_canonical(proj)
    # Repeat at project_shared with overwrite to get the same imported list.
    overwrite_run = extract_agents_to_canonical(proj, overwrite=True, scope="project_shared")
    assert [p for p, _ in default_run.imported] == [p for p, _ in overwrite_run.imported]


# ── Agents — per-scope source/destination ──────────────────────────────


def test_extract_agents_user_reads_user_runtime(home: Path, proj: Path) -> None:
    _write(home / ".claude" / "agents" / "foo.md")
    _write(proj / ".claude" / "agents" / "bar.md")

    result = extract_agents_to_canonical(proj, scope="user")
    paths = [p for p, _ in result.imported]
    # foo (user runtime) is imported into user canonical.
    assert any("foo" in str(p) for p in paths)
    # bar (project runtime) is NOT imported.
    assert not any("bar" in str(p) for p in paths)
    # Destination is user canonical.
    for p in paths:
        assert (home / ".memtomem" / "agents") in p.parents


def test_extract_agents_project_shared_reads_project_runtime(home: Path, proj: Path) -> None:
    _write(home / ".claude" / "agents" / "foo.md")
    _write(proj / ".claude" / "agents" / "bar.md")

    result = extract_agents_to_canonical(proj, scope="project_shared")
    paths = [p for p, _ in result.imported]
    assert any("bar" in str(p) for p in paths)
    assert not any("foo" in str(p) for p in paths)
    for p in paths:
        assert (proj / ".memtomem" / "agents") in p.parents


def test_extract_agents_project_local_no_fanout(home: Path, proj: Path) -> None:
    """project_local has no runtime → no source to read from → empty imports."""
    _write(home / ".claude" / "agents" / "foo.md")
    _write(proj / ".claude" / "agents" / "bar.md")

    result = extract_agents_to_canonical(proj, scope="project_local")
    assert result.imported == []
    codes = [c for _, _, c in result.skipped]
    assert skip_codes.NO_PROJECT_FANOUT_FOR_RUNTIME in codes


# ── Skills — per-scope source/destination ──────────────────────────────


def _write_skill(root: Path, name: str) -> Path:
    sk = root / name
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(f"---\nname: {name}\n---\nbody\n", encoding="utf-8")
    return sk


def test_extract_skills_user_reads_user_runtime(home: Path, proj: Path) -> None:
    _write_skill(home / ".claude" / "skills", "user_skill")
    _write_skill(proj / ".claude" / "skills", "proj_skill")

    result = extract_skills_to_canonical(proj, scope="user")
    names = [p.name for p in result.imported]
    assert "user_skill" in names
    assert "proj_skill" not in names
    # Destination user-tier.
    for p in result.imported:
        assert (home / ".memtomem" / "skills") in p.parents


def test_extract_skills_project_shared_reads_project_runtime(home: Path, proj: Path) -> None:
    _write_skill(home / ".claude" / "skills", "user_skill")
    _write_skill(proj / ".claude" / "skills", "proj_skill")

    result = extract_skills_to_canonical(proj, scope="project_shared")
    names = [p.name for p in result.imported]
    assert "proj_skill" in names
    assert "user_skill" not in names
    for p in result.imported:
        assert (proj / ".memtomem" / "skills") in p.parents


def test_extract_skills_user_reads_kimi_user_runtime(home: Path, proj: Path) -> None:
    """Kimi joined the skills extract order (#1229) — at user scope the
    source is ``~/.kimi/skills`` and the destination stays user-tier."""
    _write_skill(home / ".kimi" / "skills", "kimi_user_skill")

    result = extract_skills_to_canonical(proj, scope="user")
    names = [p.name for p in result.imported]
    assert "kimi_user_skill" in names
    for p in result.imported:
        assert (home / ".memtomem" / "skills") in p.parents


def test_extract_skills_project_local_no_fanout(home: Path, proj: Path) -> None:
    _write_skill(home / ".claude" / "skills", "user_skill")
    result = extract_skills_to_canonical(proj, scope="project_local")
    assert result.imported == []


# ── Skills — source_scope decoupling (project runtime → user library) ────
#
# The cross-tier flow behind the web "import to user library" route: read the
# PROJECT runtime but write the USER canonical, so a project-runtime skill that
# trips Gate A's false-positive secret heuristic has a force-bypassable path
# (project_shared dest is hard-blocked with no bypass).


def test_extract_skills_source_scope_project_runtime_to_user_canonical(
    home: Path, proj: Path
) -> None:
    _write_skill(home / ".claude" / "skills", "user_skill")
    _write_skill(proj / ".claude" / "skills", "proj_skill")

    result = extract_skills_to_canonical(proj, scope="user", source_scope="project_shared")
    names = [p.name for p in result.imported]
    # The PROJECT skill is read (source_scope), not the user-runtime one...
    assert "proj_skill" in names
    assert "user_skill" not in names
    # ...and written into the USER canonical store (dest scope=user).
    for p in result.imported:
        assert (home / ".memtomem" / "skills") in p.parents


def test_extract_skills_source_scope_user_dest_is_force_bypassable(home: Path, proj: Path) -> None:
    """A project skill that trips Gate A is skipped on a user dest but proceeds
    with force — the block keys off the DEST scope (user), not the source."""
    sk = proj / ".claude" / "skills" / "flagged"
    sk.mkdir(parents=True)
    (sk / "SKILL.md").write_text(
        "---\nname: flagged\n---\nclass S:\n    api_key: str\n", encoding="utf-8"
    )

    blocked = extract_skills_to_canonical(proj, scope="user", source_scope="project_shared")
    assert blocked.imported == []
    assert any(c == skip_codes.PRIVACY_BLOCKED for _, _, c in blocked.skipped)

    forced = extract_skills_to_canonical(
        proj, scope="user", source_scope="project_shared", force_unsafe_import=True
    )
    assert [p.name for p in forced.imported] == ["flagged"]
    for p in forced.imported:
        assert (home / ".memtomem" / "skills") in p.parents


def test_extract_skills_source_scope_none_keeps_dest_coupling(home: Path, proj: Path) -> None:
    """Default source_scope=None keeps source==dest (historical behavior):
    scope=user still reads the USER runtime, not the project's."""
    _write_skill(home / ".claude" / "skills", "user_skill")
    _write_skill(proj / ".claude" / "skills", "proj_skill")

    result = extract_skills_to_canonical(proj, scope="user")  # source_scope defaults to None
    names = [p.name for p in result.imported]
    assert "user_skill" in names
    assert "proj_skill" not in names


# ── Commands — per-scope, both branches ────────────────────────────────


def test_extract_commands_user_reads_user_runtime(home: Path, proj: Path) -> None:
    _write(home / ".claude" / "commands" / "user_cmd.md", "---\nname: x\n---\nbody\n")
    _write(proj / ".claude" / "commands" / "proj_cmd.md", "---\nname: x\n---\nbody\n")

    result = extract_commands_to_canonical(proj, scope="user")
    names = [p.parent.name if layout == "dir" else p.stem for p, layout in result.imported]
    assert "user_cmd" in names
    assert "proj_cmd" not in names


def test_extract_commands_gemini_user_reads_user_runtime(home: Path, proj: Path) -> None:
    _write(
        home / ".gemini" / "commands" / "ucmd.toml",
        'description = "u"\nprompt = "user prompt"\n',
    )
    result = extract_commands_to_canonical(proj, scope="user")
    names = [p.parent.name if layout == "dir" else p.stem for p, layout in result.imported]
    assert "ucmd" in names


def test_extract_commands_project_shared_reads_project_runtime(home: Path, proj: Path) -> None:
    _write(home / ".claude" / "commands" / "user_cmd.md", "---\nname: x\n---\nbody\n")
    _write(proj / ".claude" / "commands" / "proj_cmd.md", "---\nname: x\n---\nbody\n")

    result = extract_commands_to_canonical(proj, scope="project_shared")
    names = [p.parent.name if layout == "dir" else p.stem for p, layout in result.imported]
    assert "proj_cmd" in names
    assert "user_cmd" not in names


def test_extract_commands_project_local_no_fanout(home: Path, proj: Path) -> None:
    _write(home / ".claude" / "commands" / "x.md", "---\nname: x\n---\nbody\n")
    result = extract_commands_to_canonical(proj, scope="project_local")
    assert result.imported == []
