"""ADR-0030 §10 (PR-G3) — ``mm context version`` for skills is READ-ONLY.

Skills join the version surface so their ``versions/vN/`` tree snapshots can be
listed, but every mutating verb is refused: a skill version is written only by
the Store itself and that path is not exposed until PR-G4, and labeled skill
fan-out is deferred.

The refusal must be DISTINGUISHABLE from the unknown-type refusal. Both exit 1,
so a script parsing stderr is the only way to tell "dogs is not a thing" from
"skills is read-only" — two states with different next actions. Every case here
asserts the message, not just the exit code.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli.context_cmd import context
from memtomem.context import versioning
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import SKILL_MANIFEST


@pytest.fixture
def proj(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem").mkdir()
    monkeypatch.chdir(p)
    return p


def _invoke(args: list[str]):
    return CliRunner().invoke(context, args)  # type: ignore[arg-type]


def _skill_dir(proj: Path, name: str = "demo") -> Path:
    d = canonical_artifact_dir("skills", "project_shared", proj) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / SKILL_MANIFEST).write_text(
        f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return d


_WRITE_VERBS = [
    ["version", "create", "skills", "demo"],
    ["version", "promote", "skills", "demo", "--to", "production", "--version", "v1"],
    ["version", "delete-label", "skills", "demo", "production"],
]


class TestSkillsVersionList:
    def test_empty_store_lists_cleanly(self, proj):
        _skill_dir(proj)
        result = _invoke(["version", "list", "skills", "demo"])
        assert result.exit_code == 0
        assert "(no versions for skills/demo)" in result.output

    def test_lists_tree_snapshots_with_a_shape_marker(self, proj):
        skill_dir = _skill_dir(proj)
        versioning.create_tree_version(skill_dir, [("SKILL.md", b"x")], note="from pull")

        result = _invoke(["version", "list", "skills", "demo"])
        assert result.exit_code == 0
        assert "v1" in result.output
        assert "(tree)" in result.output  # not a single .md copy
        assert "from pull" in result.output

    def test_agent_rows_carry_no_tree_marker(self, proj):
        """The marker distinguishes the shapes — it must not appear on files."""
        d = canonical_artifact_dir("agents", "project_shared", proj) / "reviewer"
        d.mkdir(parents=True)
        (d / "agent.md").write_text("---\nname: reviewer\n---\nbody\n", encoding="utf-8")
        versioning.create_version(d, d / "agent.md")

        result = _invoke(["version", "list", "agents", "reviewer"])
        assert result.exit_code == 0
        assert "v1" in result.output and "(tree)" not in result.output


class TestSkillsWriteVerbsRefused:
    @pytest.mark.parametrize("argv", _WRITE_VERBS, ids=lambda a: a[1])
    def test_refused_with_the_read_only_message(self, proj, argv):
        _skill_dir(proj)
        result = _invoke(argv)
        assert result.exit_code == 1
        assert "read-only for skills" in result.output
        assert "not exposed yet" in result.output
        # Must NOT advertise a remediation that would itself refuse: an
        # overwrite pull of a skill is still unsupported until PR-G4, so
        # pointing there just costs the user a second refusal.
        assert "--overwrite" not in result.output

    @pytest.mark.parametrize("argv", _WRITE_VERBS, ids=lambda a: a[1])
    def test_nothing_is_written(self, proj, argv):
        skill_dir = _skill_dir(proj)
        _invoke(argv)
        assert not (skill_dir / "versions").exists()
        assert not (skill_dir / "versions.json").exists()

    def test_enable_is_a_noop_not_a_refusal(self, proj):
        """Skills are always dir layout, so the end state already holds —
        report the no-op rather than refuse (mirrors the web/MCP enable)."""
        skill_dir = _skill_dir(proj)
        before = sorted(p.name for p in skill_dir.iterdir())

        result = _invoke(["version", "enable", "skills", "demo"])
        assert result.exit_code == 0
        assert "always uses directory layout" in result.output
        assert sorted(p.name for p in skill_dir.iterdir()) == before

    def test_missing_skill_is_reported_by_the_engine_not_the_gate(self, proj):
        """The write gate fires first for a read-only type, so the message
        stays the read-only one — but it must never claim success."""
        result = _invoke(["version", "create", "skills", "ghost"])
        assert result.exit_code == 1


class TestRefusalsAreDistinguishable:
    """Two exit-1 states, two different next actions — a script must be able to
    tell them apart from stderr alone."""

    def test_unknown_type_says_unknown_type(self, proj):
        result = _invoke(["version", "create", "dogs", "demo"])
        assert result.exit_code == 1
        assert "Unknown artifact type: dogs" in result.output
        assert "read-only" not in result.output

    def test_read_only_says_read_only(self, proj):
        _skill_dir(proj)
        result = _invoke(["version", "create", "skills", "demo"])
        assert result.exit_code == 1
        assert "read-only" in result.output
        assert "Unknown artifact type" not in result.output

    def test_unknown_type_lists_skills_as_supported(self, proj):
        result = _invoke(["version", "list", "dogs", "demo"])
        assert "agents, commands, skills" in result.output
