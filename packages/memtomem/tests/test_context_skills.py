"""Tests for context/skills.py — canonical ⇄ runtime skill fan-out."""

import os
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
    (skill / "SKILL.md").write_text(body, encoding="utf-8")
    if with_scripts:
        (skill / "scripts").mkdir()
        (skill / "scripts" / "run.sh").write_text(SAMPLE_SCRIPT, encoding="utf-8")
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

    def test_skips_staging_leftovers(self, tmp_path):
        """A crashed import leaves ``.staging-*.tmp`` under the canonical
        root with a full SKILL.md mirror — listing it would fan the junk out
        to every runtime on the next sync (#1229)."""
        for leftover in (".staging-a-99999-abc123.tmp", ".old-a-99999-abc123.tmp"):
            d = tmp_path / CANONICAL_SKILL_ROOT / leftover
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        assert list_canonical_skills(tmp_path) == []


class TestCopySkill:
    def test_copies_manifest_only(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        dst = tmp_path / "dst"
        copy_skill(src, dst)
        assert (dst / "SKILL.md").read_text(encoding="utf-8") == SAMPLE_SKILL_MD

    def test_copies_subdirectories(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        (src / "scripts").mkdir()
        (src / "scripts" / "run.sh").write_text(SAMPLE_SCRIPT, encoding="utf-8")
        (src / "references").mkdir()
        (src / "references" / "note.md").write_text("note", encoding="utf-8")
        dst = tmp_path / "dst"
        copy_skill(src, dst)
        assert (dst / "SKILL.md").read_text(encoding="utf-8") == SAMPLE_SKILL_MD
        assert (dst / "scripts" / "run.sh").read_text(encoding="utf-8") == SAMPLE_SCRIPT
        assert (dst / "references" / "note.md").read_text(encoding="utf-8") == "note"

    def test_missing_manifest_raises(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        dst = tmp_path / "dst"
        with pytest.raises(FileNotFoundError):
            copy_skill(src, dst)

    def test_refuses_to_overwrite_non_skill_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "unrelated.txt").write_text("do not delete", encoding="utf-8")

        with pytest.raises(IsADirectoryError):
            copy_skill(src, dst)
        assert (dst / "unrelated.txt").read_text(encoding="utf-8") == "do not delete"

    def test_replaces_existing_skill_dir(self, tmp_path):
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_text("new content", encoding="utf-8")

        dst = tmp_path / "dst"
        dst.mkdir()
        (dst / "SKILL.md").write_text("old content", encoding="utf-8")
        (dst / "stale.md").write_text("leftover", encoding="utf-8")

        copy_skill(src, dst)
        assert (dst / "SKILL.md").read_text(encoding="utf-8") == "new content"
        # removed files propagate (stale files disappear)
        assert not (dst / "stale.md").exists()


class TestGenerateAllSkills:
    def test_fans_out_to_all_runtimes(self, tmp_path):
        _make_canonical_skill(tmp_path, "code-review", with_scripts=True)
        result = generate_all_skills(tmp_path)
        assert isinstance(result, SkillSyncResult)
        # 4 runtimes × 1 skill (claude + gemini + codex + kimi)
        assert len(result.generated) == 4
        for runtime_root in (".claude/skills", ".gemini/skills", ".agents/skills", ".kimi/skills"):
            assert (tmp_path / runtime_root / "code-review/SKILL.md").exists()
            assert (tmp_path / runtime_root / "code-review/scripts/run.sh").exists()

    def test_no_canonical_no_op(self, tmp_path):
        result = generate_all_skills(tmp_path)
        assert result.generated == []
        assert result.skipped == [("<all>", "no canonical skills", "no_canonical_root")]

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
        assert ("unknown", "unknown runtime", "unknown_runtime") in result.skipped

    def test_generator_registry_contents(self):
        assert "claude_skills" in SKILL_GENERATORS
        assert "gemini_skills" in SKILL_GENERATORS
        assert "codex_skills" in SKILL_GENERATORS


class TestUnreadableSources:
    """OSError on canonical stage or override read becomes a typed
    ``PARSE_ERROR`` skip, mirroring ``agents.py`` / ``commands.py``
    ``read_bytes`` failure handling. Per-item skip rather than batch abort
    — only privacy blocks escalate to a raise (#900 inventory matrix gap)."""

    def test_unreadable_canonical_is_typed_skip_and_keeps_batch_running(
        self, tmp_path, monkeypatch
    ):
        # Two canonicals — "alpha" healthy, "broken" simulates an OSError
        # from _stage_skill (e.g. unreadable file inside the canonical tree).
        _make_canonical_skill(tmp_path, "alpha")
        _make_canonical_skill(tmp_path, "broken")

        from memtomem.context import skills as skills_module

        real_stage = skills_module._stage_skill

        def fake_stage(src, dst, **kwargs):
            if src.name == "broken":
                raise PermissionError("permission denied")
            return real_stage(src, dst, **kwargs)

        monkeypatch.setattr(skills_module, "_stage_skill", fake_stage)

        result = generate_all_skills(tmp_path, runtimes=["claude_skills"])

        # alpha promoted, broken typed-skipped, batch not aborted.
        assert ("claude_skills", tmp_path / ".claude/skills/alpha") in result.generated
        assert (tmp_path / ".claude/skills/alpha/SKILL.md").is_file()
        assert not (tmp_path / ".claude/skills/broken").exists()
        assert any(
            entry[0] == "broken"
            and entry[1].startswith("unreadable:")
            and entry[2] == "parse_error"
            for entry in result.skipped
        )
        # No orphaned staging tree under the runtime fan-out parent.
        assert list((tmp_path / ".claude/skills").glob(".staging-*.tmp")) == []

    def test_unreadable_override_is_typed_skip_and_keeps_batch_running(self, tmp_path, monkeypatch):
        # Two skills — "alpha" healthy, "withoverride" has a resolved override
        # whose bytes cannot be read (resolve returns a Path; read_bytes raises).
        _make_canonical_skill(tmp_path, "alpha")
        _make_canonical_skill(tmp_path, "withoverride")

        from memtomem.context import skills as skills_module

        real_resolve = skills_module._override.resolve
        broken_override = tmp_path / "no-such-dir" / "override.md"

        def fake_resolve(project_root, kind, name, vendor, *, scope=None):
            if name == "withoverride":
                return broken_override
            return real_resolve(project_root, kind, name, vendor, scope=scope)

        monkeypatch.setattr(skills_module._override, "resolve", fake_resolve)

        result = generate_all_skills(tmp_path, runtimes=["claude_skills"])

        # alpha promoted, withoverride typed-skipped, no orphaned staging.
        assert ("claude_skills", tmp_path / ".claude/skills/alpha") in result.generated
        assert (tmp_path / ".claude/skills/alpha/SKILL.md").is_file()
        assert not (tmp_path / ".claude/skills/withoverride").exists()
        assert any(
            entry[0] == "withoverride"
            and entry[1].startswith("override unreadable:")
            and entry[2] == "parse_error"
            for entry in result.skipped
        )
        assert list((tmp_path / ".claude/skills").glob(".staging-*.tmp")) == []

    def test_unreadable_canonical_in_user_scope_is_typed_skip(self, tmp_path, monkeypatch):
        # Exercises the non-project_shared (per-pair) path. Layout parallels
        # the project_shared canonical test but with scope="user", which
        # routes through canonical_skills_root → ~/.memtomem/skills/.
        from memtomem.context import skills as skills_module
        from memtomem.context.skills import canonical_skills_root

        from .helpers import set_home

        fake_home = tmp_path / "home"
        fake_home.mkdir()
        set_home(monkeypatch, fake_home)

        user_root = canonical_skills_root(tmp_path, scope="user")
        user_root.mkdir(parents=True, exist_ok=True)
        for name in ("alpha", "broken"):
            skill = user_root / name
            skill.mkdir()
            (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        real_stage = skills_module._stage_skill

        def fake_stage(src, dst, **kwargs):
            if src.name == "broken":
                raise PermissionError("permission denied")
            return real_stage(src, dst, **kwargs)

        monkeypatch.setattr(skills_module, "_stage_skill", fake_stage)

        result = generate_all_skills(tmp_path, runtimes=["claude_skills"], scope="user")

        # alpha promoted under ~/.claude/skills; broken typed-skipped.
        claude_user_dir = fake_home / ".claude/skills"
        assert (claude_user_dir / "alpha/SKILL.md").is_file()
        assert not (claude_user_dir / "broken").exists()
        assert any(
            entry[0] == "broken"
            and entry[1].startswith("unreadable:")
            and entry[2] == "parse_error"
            for entry in result.skipped
        )


class TestDetectSkillDirs:
    def test_detects_claude_skills(self, tmp_path):
        skill = tmp_path / ".claude/skills/a"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        found = detect_skill_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "claude_skills"
        assert found[0].kind == "skill_dir"
        assert found[0].path == skill

    def test_detects_gemini_skills(self, tmp_path):
        skill = tmp_path / ".gemini/skills/b"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        found = detect_skill_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "gemini_skills"

    def test_detects_codex_skills(self, tmp_path):
        # .agents/skills/ is Codex CLI's primary project-scope path.
        skill = tmp_path / ".agents/skills/c"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        found = detect_skill_dirs(tmp_path)
        assert len(found) == 1
        assert found[0].agent == "codex_skills"

    def test_ignores_dirs_without_manifest(self, tmp_path):
        (tmp_path / ".claude/skills/broken").mkdir(parents=True)
        found = detect_skill_dirs(tmp_path)
        assert found == []

    def test_ignores_staging_leftovers(self, tmp_path):
        """Crash-leftover staging trees carry a SKILL.md mirror — they must
        not inflate detect/init preview counts (#1229)."""
        d = tmp_path / ".claude/skills/.staging-a-99999-abc123.tmp"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        assert detect_skill_dirs(tmp_path) == []

    def test_empty_project(self, tmp_path):
        assert detect_skill_dirs(tmp_path) == []


class TestExtractSkills:
    def test_imports_from_claude(self, tmp_path):
        skill = tmp_path / ".claude/skills/code-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1
        assert (tmp_path / CANONICAL_SKILL_ROOT / "code-review/SKILL.md").exists()
        assert result.skipped == []

    def test_duplicate_across_runtimes_deduped(self, tmp_path):
        for runtime_dir in (".claude/skills", ".gemini/skills"):
            skill = tmp_path / runtime_dir / "shared"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1
        assert len(result.skipped) == 1
        assert result.skipped[0][0] == "shared"
        assert "already imported" in result.skipped[0][1]

    def test_imports_from_kimi(self, tmp_path):
        """A kimi-only skill is importable — diff reported it as 'missing
        canonical' while the extract loop never read .kimi/skills, making
        Import a silent no-op with no skip record (#1229)."""
        skill = tmp_path / ".kimi/skills/kimi-only"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1
        assert (tmp_path / CANONICAL_SKILL_ROOT / "kimi-only/SKILL.md").exists()
        assert result.skipped == []

    def test_kimi_loses_dedup_to_claude(self, tmp_path):
        """Order pin: kimi is scanned LAST, so existing first-wins outcomes
        are unchanged — a claude copy beats a kimi copy of the same name."""
        for runtime_dir, body in ((".claude/skills", "claude wins\n"), (".kimi/skills", "kimi\n")):
            skill = tmp_path / runtime_dir / "shared"
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(body, encoding="utf-8")
        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1
        canonical = tmp_path / CANONICAL_SKILL_ROOT / "shared/SKILL.md"
        assert canonical.read_text(encoding="utf-8") == "claude wins\n"
        assert len(result.skipped) == 1
        assert "already imported" in result.skipped[0][1]

    def test_kimi_missing_canonical_diff_row_is_importable(self, tmp_path):
        """Diff↔extract parity: a 'missing canonical' row for a kimi-only
        skill must be actionable by import (the regression shape from #1229
        — the UI offered an Import CTA that 404'd)."""
        skill = tmp_path / ".kimi/skills/kimi-only"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        rows = diff_skills(tmp_path)
        assert ("kimi_skills", "kimi-only", "missing canonical") in rows

        result = extract_skills_to_canonical(tmp_path, only_name="kimi-only")
        assert len(result.imported) == 1

    def test_staging_leftover_not_imported(self, tmp_path):
        """A crash-leftover staging tree under a runtime root contains a full
        SKILL.md mirror and passes validate_name — it must not round-trip
        into canonical (#1229). Silent skip: no skip record, no warning."""
        for leftover in (
            ".staging-code-review-99999-abc123.tmp",
            ".old-code-review-99999-abc123.tmp",
        ):
            d = tmp_path / ".claude/skills" / leftover
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        result = extract_skills_to_canonical(tmp_path)
        assert result.imported == []
        assert result.skipped == []
        assert list_canonical_skills(tmp_path) == []

    def test_does_not_overwrite_without_flag(self, tmp_path):
        src = tmp_path / ".claude/skills/existing"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("new", encoding="utf-8")

        canonical = tmp_path / CANONICAL_SKILL_ROOT / "existing"
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("old", encoding="utf-8")

        result = extract_skills_to_canonical(tmp_path)
        assert result.imported == []
        assert len(result.skipped) == 1
        assert "canonical exists" in result.skipped[0][1]
        assert (canonical / "SKILL.md").read_text(encoding="utf-8") == "old"

    def test_overwrite_flag(self, tmp_path):
        src = tmp_path / ".claude/skills/existing"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("new", encoding="utf-8")

        canonical = tmp_path / CANONICAL_SKILL_ROOT / "existing"
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("old", encoding="utf-8")

        result = extract_skills_to_canonical(tmp_path, overwrite=True)
        assert len(result.imported) == 1
        assert (canonical / "SKILL.md").read_text(encoding="utf-8") == "new"

    def test_only_name_filters_to_one(self, tmp_path):
        for name in ("alpha", "beta"):
            d = tmp_path / ".claude/skills" / name
            d.mkdir(parents=True)
            (d / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        result = extract_skills_to_canonical(tmp_path, only_name="alpha")
        assert [p.name for p in result.imported] == ["alpha"]
        assert result.skipped == []
        assert not (tmp_path / CANONICAL_SKILL_ROOT / "beta").exists()

    def test_only_name_no_match_returns_empty(self, tmp_path):
        d = tmp_path / ".claude/skills/alpha"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        result = extract_skills_to_canonical(tmp_path, only_name="ghost")
        assert result.imported == []
        assert result.skipped == []

    def test_dry_run_reports_without_writing(self, tmp_path):
        # rank-10: dry_run lists the would-import destination but never
        # touches disk, and a real run afterwards still imports normally.
        skill = tmp_path / ".claude/skills/code-review"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        preview = extract_skills_to_canonical(tmp_path, dry_run=True)
        assert [p.name for p in preview.imported] == ["code-review"]
        assert preview.skipped == []
        # Nothing written — the canonical dir does not exist yet.
        assert not (tmp_path / CANONICAL_SKILL_ROOT / "code-review").exists()

        # A real run produces the identical import set and now writes.
        applied = extract_skills_to_canonical(tmp_path)
        assert [p.name for p in applied.imported] == ["code-review"]
        assert (tmp_path / CANONICAL_SKILL_ROOT / "code-review/SKILL.md").exists()

    def test_dry_run_reports_would_skip_existing(self, tmp_path):
        # An existing canonical entry is reported as a (would-)skip in dry_run
        # mode, and the existing content is left untouched.
        src = tmp_path / ".claude/skills/existing"
        src.mkdir(parents=True)
        (src / "SKILL.md").write_text("new", encoding="utf-8")

        canonical = tmp_path / CANONICAL_SKILL_ROOT / "existing"
        canonical.mkdir(parents=True)
        (canonical / "SKILL.md").write_text("old", encoding="utf-8")

        preview = extract_skills_to_canonical(tmp_path, dry_run=True)
        assert preview.imported == []
        assert len(preview.skipped) == 1
        assert "canonical exists" in preview.skipped[0][1]
        assert (canonical / "SKILL.md").read_text(encoding="utf-8") == "old"


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
        (tmp_path / ".claude/skills/a/SKILL.md").write_text("mutated", encoding="utf-8")
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
        (skill / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        rows = diff_skills(tmp_path)
        assert any(status == "missing canonical" for _, _, status in rows)

    def test_staging_leftovers_produce_no_phantom_rows(self, tmp_path):
        """Crash-leftover staging/move-aside trees under either root must not
        surface as phantom 'missing canonical' / 'missing target' rows
        (#1229)."""
        runtime_leftover = tmp_path / ".claude/skills/.staging-a-99999-abc123.tmp"
        runtime_leftover.mkdir(parents=True)
        (runtime_leftover / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        canonical_leftover = tmp_path / CANONICAL_SKILL_ROOT / ".old-a-99999-abc123.tmp"
        canonical_leftover.mkdir(parents=True)
        (canonical_leftover / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")

        assert diff_skills(tmp_path) == []

    @pytest.mark.skipif(
        os.name == "nt" or os.geteuid() == 0,
        reason="needs POSIX permissions and a non-root user",
    )
    def test_unreadable_runtime_file_reports_drift_not_crash(self, tmp_path):
        """A PermissionError inside either tree must not abort the whole diff
        — parity can't be asserted, so report drift, never mask it (#1229)."""
        _make_canonical_skill(tmp_path, "a")
        generate_all_skills(tmp_path)
        manifest = tmp_path / ".claude/skills/a/SKILL.md"
        manifest.chmod(0)
        try:
            rows = diff_skills(tmp_path)
        finally:
            manifest.chmod(0o644)
        status_by_runtime = {runtime: status for runtime, _, status in rows}
        assert status_by_runtime["claude_skills"] == "out of sync"
        assert status_by_runtime["gemini_skills"] == "in sync"


class TestRoundtrip:
    def test_canonical_to_runtime_to_canonical(self, tmp_path):
        _make_canonical_skill(tmp_path, "code-review", with_scripts=True)
        generate_all_skills(tmp_path)

        shutil.rmtree(tmp_path / CANONICAL_SKILL_ROOT)

        result = extract_skills_to_canonical(tmp_path)
        assert len(result.imported) == 1

        md = (tmp_path / CANONICAL_SKILL_ROOT / "code-review/SKILL.md").read_text(encoding="utf-8")
        assert md == SAMPLE_SKILL_MD
        script = (tmp_path / CANONICAL_SKILL_ROOT / "code-review/scripts/run.sh").read_text(
            encoding="utf-8"
        )
        assert script == SAMPLE_SCRIPT


class TestExtractTargetConflict:
    """#1229: ``--overwrite`` onto a canonical destination holding non-skill
    content used to crash extract mid-batch with IsADirectoryError (the web
    import routes surfaced it as HTTP 500). It is now a typed
    ``target_conflict`` skip, and the dry-run preview reports the same skip
    so the preview matches the real run."""

    def _seed_runtime_skill(self, tmp_path, name="foo"):
        d = tmp_path / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text(SAMPLE_SKILL_MD, encoding="utf-8")
        return d

    def _seed_conflicted_canonical(self, tmp_path, name="foo"):
        canonical = tmp_path / CANONICAL_SKILL_ROOT / name
        canonical.mkdir(parents=True)
        (canonical / "junk.txt").write_text("keep me", encoding="utf-8")
        return canonical

    def test_overwrite_onto_non_skill_canonical_is_typed_skip(self, tmp_path):
        self._seed_runtime_skill(tmp_path)
        canonical = self._seed_conflicted_canonical(tmp_path)

        result = extract_skills_to_canonical(tmp_path, overwrite=True)  # must not raise

        assert result.imported == []
        conflicts = [s for s in result.skipped if s[2] == "target_conflict"]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert str(canonical) in conflicts[0][1]
        # The conflicting canonical content is untouched.
        assert (canonical / "junk.txt").read_text(encoding="utf-8") == "keep me"
        assert not (canonical / "SKILL.md").exists()

    def test_dry_run_previews_the_same_conflict_skip(self, tmp_path):
        """dry_run parity: the preview reports the conflict exactly like the
        real run would (rank-10 contract: skip decisions identical)."""
        self._seed_runtime_skill(tmp_path)
        canonical = self._seed_conflicted_canonical(tmp_path)

        result = extract_skills_to_canonical(tmp_path, overwrite=True, dry_run=True)

        assert result.imported == []
        conflicts = [s for s in result.skipped if s[2] == "target_conflict"]
        assert len(conflicts) == 1, result.skipped
        assert (canonical / "junk.txt").read_text(encoding="utf-8") == "keep me"

    def test_without_overwrite_existing_canonical_still_wins(self, tmp_path):
        """Ordering pin: without --overwrite the long-standing
        ``canonical_exists`` skip fires first — the conflict skip only
        applies to the overwrite path."""
        self._seed_runtime_skill(tmp_path)
        self._seed_conflicted_canonical(tmp_path)

        result = extract_skills_to_canonical(tmp_path, overwrite=False)

        codes = [s[2] for s in result.skipped]
        assert codes == ["canonical_exists"], result.skipped
