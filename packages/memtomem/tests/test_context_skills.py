"""Tests for context/skills.py — canonical ⇄ runtime skill fan-out."""

import shutil

import pytest

from memtomem.context.detector import detect_skill_dirs
from memtomem.context.skills import (
    CANONICAL_SKILL_ROOT,
    SKILL_GENERATORS,
    SkillSyncResult,
    copy_skill,
    diff_skills,
    extract_skills_to_canonical,
    generate_all_skills,
    list_canonical_skills,
)

SAMPLE_SKILL_MD = """---
name: code-review
description: Reviews staged changes for quality.
---

Review the staged diff and report issues.
"""

SAMPLE_SCRIPT = "#!/usr/bin/env bash\necho hi\n"


def _make_canonical_skill(project_root, name, body=SAMPLE_SKILL_MD, with_scripts=False):
    skill = project_root / CANONICAL_SKILL_ROOT / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(body)
    if with_scripts:
        (skill / "scripts").mkdir()
        (skill / "scripts" / "run.sh").write_text(SAMPLE_SCRIPT)
    return skill


class TestCanonicalDiscovery:
    def test_list_empty(self, tmp_path):
        assert list_canonical_skills(tmp_path) == []

    def test_list_single(self, tmp_path):
        _make_canonical_skill(tmp_path, "code-review")
        skills = list_canonical_skills(tmp_path)
        assert len(skills) == 1
        assert skills[0].name == "code-review"

    def test_list_sorted(self, tmp_path):
        _make_canonical_skill(tmp_path, "zeta")
        _make_canonical_skill(tmp_path, "alpha")
        names = [s.name for s in list_canonical_skills(tmp_path)]
        assert names == ["alpha", "zeta"]

    def test_skips_dirs_without_manifest(self, tmp_path):
        (tmp_path / CANONICAL_SKILL_ROOT / "not-a-skill").mkdir(parents=True)
        assert list_canonical_skills(tmp_path) == []


class TestCopySkill:
    def test_copies_manifest_only(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        dst = tmp_path / "dst"
        copy_skill(src, dst)
        assert (dst / "SKILL.md").read_text() == SAMPLE_SKILL_MD

    def test_copies_subdirectories(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        (src / "scripts").mkdir()
        (src / "scripts" / "run.sh").write_text(SAMPLE_SCRIPT)
        (src / "references").mkdir()
        (src / "references" / "note.md").write_text("note")
        dst = tmp_path / "dst"
        copy_skill(src, dst)
        assert (dst / "SKILL.md").read_text() == SAMPLE_SKILL_MD
        assert (dst / "scripts" / "run.sh").read_text() == SAMPLE_SCRIPT
        assert (dst / "references" / "note.md").read_text() == "note"

    def test_missing_manifest_raises(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        with pytest.raises(FileNotFoundError):
            copy_skill(src, dst)

    def test_refuses_to_overwrite_non_skill_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text(SAMPLE_SKILL_MD)

        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "unrelated.txt").write_text("do not delete")

        with pytest.raises(IsADirectoryError):
            copy_skill(src, dst)
        assert (dst / "unrelated.txt").read_text() == "do not delete"

    def test_replaces_existing_skill_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("new content")

        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "SKILL.md").write_text("old content")
        (dst / "stale.md").write_text("leftover")

        copy_skill(src, dst)
        assert (dst / "SKILL.md").read_text() == "new content"
        # removed files propagate (stale files disappear)
        assert not (dst / "stale.md").exists()


class TestGenerateAllSkills:
    def test_fans_out_to_all_runtimes(self, tmp_path):
        _make_canonical_skill(tmp_path, "code-review", with_scripts=True)
        result = generate_all_skills(tmp_path)
        assert isinstance(result, SkillSyncResult)
        # 3 runtimes × 1 skill (claude + gemini + codex)
        assert len(result.generated) == 3
        for runtime_root in (".claude/skills", ".gemini/skills", ".agents/skills"):
            assert (tmp_path / runtime_root / "code-review/SKILL.md").exists()
            assert (tmp_path / runtime_root / "code-review/scripts/run.sh").exists()

    def test_no_canonical_no_op(self, tmp_path):
        result = generate_all_skills(tmp_path)
        assert result.generated == []
        assert result.skipped == [("<all>", "no canonical skills")]

    def test_respects_runtime_filter(self, tmp_path):
        _make_canonical_skill(tmp_path, "a")
        result = generate_all_skills(tmp_path, runtimes=["claude_skills"])
        assert all(r[0] == "claude_skills" for r in result.generated)
        assert (tmp_path / ".claude/skills/a").exists()
        assert not (tmp_path / ".gemini/skills/a").exists()
        assert not (tmp_path / ".agents/skills/a").exists()

    def test_unknown_runtime_reported(self, tmp_path):
        _make_canonical_skill(tmp_path, "a")
        result = generate_all_skills(tmp_path, runtimes=["claude_skills", "unknown"])
        assert ("unknown", "unknown runtime") in result.skipped

    def test_generator_registry_contents(self):
        assert "claude_skills" in SKILL_GENERATORS
        assert "gemini_skills" in SKILL_GENERATORS
        assert "codex_skills" in SKILL_GENERATORS


class TestDetectSkillDirs:
    def test_detects_claude_skills(self, tmp_path):
        skill = tmp_path / ".claude/skills/a"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        found = detect_skill_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "claude_skills"
        assert found[0].kind == "skill_dir"
        assert found[0].path == skill

    def test_detects_gemini_skills(self, tmp_path):
        skill = tmp_path / ".gemini/skills/b"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        found = detect_skill_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "gemini_skills"

    def test_detects_codex_skills(self, tmp_path):
        # .agents/skills/ is Codex CLI's primary project-scope path.
        skill = tmp_path / ".agents/skills/c"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        found = detect_skill_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "codex_skills"

    def test_ignores_dirs_without_manifest(self, tmp_path):
        (tmp_path / ".claude/skills/broken").mkdir(parents=True)
        found = detect_skill_dirs(tmp_path)
        assert found == []

    def test_empty_project(self, tmp_path):
        assert detect_skill_dirs(tmp_path) == []


class TestExtractSkills:
    def test_imports_from_claude(self, tmp_path):
        skill = tmp_path / ".claude/skills/code-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1
        assert (tmp_path / CANONICAL_SKILL_ROOT / "code-review/SKILL.md").exists()
        assert result.skipped == []

    def test_duplicate_across_runtimes_deduped(self, tmp_path):
        for runtime_dir in (".claude/skills", ".gemini/skills"):
            skill = tmp_path / runtime_dir / "shared"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1
        assert len(result.skipped) == 1
        assert result.skipped[0][0] == "shared"
        assert "already imported" in result.skipped[0][1]

    def test_does_not_overwrite_without_flag(self, tmp_path):
        src = tmp_path / ".claude/skills/existing"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("new")

        canonical = tmp_path / CANONICAL_SKILL_ROOT / "existing"
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("old")

        result = extract_skills_to_canonical(tmp_path)
        assert result.imported == []
        assert len(result.skipped) == 1
        assert "canonical exists" in result.skipped[0][1]
        assert (canonical / "SKILL.md").read_text() == "old"

    def test_overwrite_flag(self, tmp_path):
        src = tmp_path / ".claude/skills/existing"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("new")

        canonical = tmp_path / CANONICAL_SKILL_ROOT / "existing"
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("old")

        result = extract_skills_to_canonical(tmp_path, overwrite=True)
        assert len(result.imported) == 1
        assert (canonical / "SKILL.md").read_text() == "new"


class TestDiffSkills:
    def test_empty_project(self, tmp_path):
        assert diff_skills(tmp_path) == []

    def test_in_sync(self, tmp_path):
        _make_canonical_skill(tmp_path, "a")
        generate_all_skills(tmp_path)
        rows = diff_skills(tmp_path)
        assert rows  # non-empty
        assert all(status == "in sync" for _, _, status in rows)

    def test_out_of_sync(self, tmp_path):
        _make_canonical_skill(tmp_path, "a")
        generate_all_skills(tmp_path)
        (tmp_path / ".claude/skills/a/SKILL.md").write_text("mutated")
        rows = diff_skills(tmp_path)
        status_by_runtime = {runtime: status for runtime, _, status in rows}
        assert status_by_runtime["claude_skills"] == "out of sync"
        assert status_by_runtime["gemini_skills"] == "in sync"

    def test_missing_target(self, tmp_path):
        _make_canonical_skill(tmp_path, "orphan")
        rows = diff_skills(tmp_path)
        assert rows
        assert all(status == "missing target" for _, _, status in rows)

    def test_missing_canonical(self, tmp_path):
        skill = tmp_path / ".claude/skills/runtime-only"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD)
        rows = diff_skills(tmp_path)
        assert any(status == "missing canonical" for _, _, status in rows)


class TestRoundtrip:
    def test_canonical_to_runtime_to_canonical(self, tmp_path):
        _make_canonical_skill(tmp_path, "code-review", with_scripts=True)
        generate_all_skills(tmp_path)

        shutil.rmtree(tmp_path / CANONICAL_SKILL_ROOT)

        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1

        md = (tmp_path / CANONICAL_SKILL_ROOT / "code-review/SKILL.md").read_text()
        assert md == SAMPLE_SKILL_MD
        script = (tmp_path / CANONICAL_SKILL_ROOT / "code-review/scripts/run.sh").read_text()
        assert script == SAMPLE_SCRIPT
