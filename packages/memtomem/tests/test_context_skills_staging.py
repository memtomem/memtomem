"""ADR-0011 PR-E3 — skills staging-dir-first scan + atomic promote.

Contract pins for the new ``_stage_skill`` + ``_promote_staging`` pair
that replaced the inline ``shutil.rmtree(dst); copy_tree_atomic`` in
``copy_skill``:

* Same-fs precondition — staging lives at ``dst.parent / .staging-…tmp``
  so :func:`os.replace` is atomic.
* Promote happy path — ``dst`` ends up byte-equal to staging; the
  staging path no longer exists post-promote.
* Block + cleanup — when ``scan_artifact_tree`` blocks, the staging
  tree is removed and any pre-existing ``dst`` content is unchanged.
* Override-only-touches-SKILL.md invariant
  (``test_context_override.py:317``) preserved through the new flow:
  ``scripts/`` etc. stay byte-equal to canonical even when an override
  is staged for ``SKILL.md``.
* Mid-promote rollback — staging promotes atomically; an ``os.replace``
  failure on the second swap restores the previous ``dst`` tree.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import (
    SKILL_GENERATORS,
    SKILL_MANIFEST,
    _promote_staging,
    _stage_skill,
    canonical_skills_root,
    copy_skill,
    generate_all_skills,
)

from .helpers import set_home

SECRET = "api_key=AKIA1234567890ABCDEF"


def _seed_canonical_skill(
    project_root: Path,
    name: str = "foo",
    *,
    scope: str = "project_shared",
    skill_md: str = "---\nname: foo\n---\nbody\n",
    extras: dict[str, str] | None = None,
) -> Path:
    """Seed a canonical skill at the requested ``scope`` location.

    ``scope="project_shared"`` (default) seeds at
    ``<project_root>/.memtomem/skills/<name>``. ``scope="user"`` requires
    the caller to have already overridden HOME to a tmp dir so that
    ``canonical_artifact_dir("skills", "user", project_root)`` resolves
    to a tmp path (never the real ``~/.memtomem``).
    """
    canonical = canonical_artifact_dir("skills", scope, project_root)  # type: ignore[arg-type]
    skill_dir = canonical / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(skill_md, encoding="utf-8")
    if extras:
        for rel, content in extras.items():
            p = skill_dir / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
    return skill_dir


class TestStageSkill:
    def test_staging_under_dst_parent_same_fs(self, tmp_path: Path) -> None:
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        staging = _stage_skill(src, dst)
        # Same-fs invariant: staging lives in dst.parent (so os.replace
        # is atomic, not a fall-back copy+delete).
        assert staging.parent == dst.parent
        assert staging.exists()
        assert staging.name.startswith(f".staging-{dst.name}-")
        assert staging.name.endswith(".tmp")
        # Staging contains the canonical bytes byte-equal.
        assert (staging / SKILL_MANIFEST).read_bytes() == (src / SKILL_MANIFEST).read_bytes()

    def test_staging_includes_aux_files(self, tmp_path: Path) -> None:
        src = _seed_canonical_skill(
            tmp_path,
            name="foo",
            extras={"scripts/run.sh": "#!/bin/bash\necho hi\n"},
        )
        dst = tmp_path / ".claude" / "skills" / "foo"
        staging = _stage_skill(src, dst)
        assert (staging / "scripts" / "run.sh").read_bytes() == (
            src / "scripts" / "run.sh"
        ).read_bytes()


class TestPromoteStaging:
    def test_promote_consumes_staging(self, tmp_path: Path) -> None:
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        staging = _stage_skill(src, dst)
        # Sanity: dst doesn't exist before promote.
        assert not dst.exists()
        _promote_staging(staging, dst)
        # Negative marker: staging path is gone (consumed by os.replace).
        assert not staging.exists()
        # Positive marker: dst contents byte-equal to canonical.
        assert (dst / SKILL_MANIFEST).read_bytes() == (src / SKILL_MANIFEST).read_bytes()

    def test_promote_replaces_existing_skill_dst(self, tmp_path: Path) -> None:
        src = _seed_canonical_skill(
            tmp_path,
            name="foo",
            skill_md="---\nname: foo\n---\nNEW BODY\n",
        )
        dst = tmp_path / ".claude" / "skills" / "foo"
        # Pre-existing dst with old SKILL.md
        dst.mkdir(parents=True)
        (dst / SKILL_MANIFEST).write_text("---\nname: foo\n---\nold body\n", encoding="utf-8")
        staging = _stage_skill(src, dst)
        _promote_staging(staging, dst)
        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == (
            "---\nname: foo\n---\nNEW BODY\n"
        )
        # No leftover .old- or .staging- directories.
        leftovers = [
            p.name for p in dst.parent.iterdir() if p.name.startswith((".old-", ".staging-"))
        ]
        assert leftovers == []

    def test_promote_refuses_nonempty_non_skill_dst(self, tmp_path: Path) -> None:
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        dst.mkdir(parents=True)
        (dst / "user_data.txt").write_text("user file\n", encoding="utf-8")
        staging = _stage_skill(src, dst)
        with pytest.raises(IsADirectoryError):
            _promote_staging(staging, dst)
        # Staging not cleaned up by _promote_staging — caller's responsibility.
        # Pre-existing dst contents preserved.
        assert (dst / "user_data.txt").read_text(encoding="utf-8") == "user file\n"

    def test_rollback_restores_dst_on_inner_replace_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Force the second os.replace (staging → dst) to fail; rollback
        # must restore the previous dst tree byte-for-byte.
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        dst.mkdir(parents=True)
        (dst / SKILL_MANIFEST).write_text("ORIGINAL CONTENT\n", encoding="utf-8")
        staging = _stage_skill(src, dst)

        original_replace = os.replace
        call_count = {"n": 0}

        def fail_second_replace(a, b):
            call_count["n"] += 1
            if call_count["n"] == 2:
                raise OSError("forced second-replace failure")
            return original_replace(a, b)

        monkeypatch.setattr(os, "replace", fail_second_replace)
        with pytest.raises(OSError, match="forced second-replace failure"):
            _promote_staging(staging, dst)
        # Negative marker: dst was rolled back to ORIGINAL CONTENT.
        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"


class TestCopySkillBackCompat:
    def test_copy_skill_still_works(self, tmp_path: Path) -> None:
        # ``copy_skill`` is now a thin wrapper around stage+promote.
        # Pin the public-API contract that it still mirrors src → dst.
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        copy_skill(src, dst)
        assert (dst / SKILL_MANIFEST).read_bytes() == (src / SKILL_MANIFEST).read_bytes()


class TestGenerateAllSkillsStagingFlow:
    def test_clean_canonical_promotes(self, tmp_path: Path) -> None:
        _seed_canonical_skill(tmp_path, name="hello")
        result = generate_all_skills(tmp_path, runtimes=["claude_skills"])
        # Positive: generated entry present.
        runtimes = [r for r, _ in result.generated]
        assert "claude_skills" in runtimes
        gen = SKILL_GENERATORS["claude_skills"]
        target = gen.target_dir(tmp_path, "hello")
        assert target is not None
        # Negative: no leftover staging in dst.parent.
        leftovers = [
            p.name for p in target.parent.iterdir() if p.name.startswith((".staging-", ".old-"))
        ]
        assert leftovers == []

    def test_secret_in_scripts_blocks_user_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # User-scope canonical lives under HOME — isolate.
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_canonical_skill(
            tmp_path,
            name="leak",
            scope="user",
            extras={"scripts/leak.sh": f"#!/bin/bash\necho {SECRET}\n"},
        )
        # User-scope runtime fan-out target lives under HOME too —
        # pre-create with sentinel content; blocked sync must NOT
        # overwrite it.
        gen = SKILL_GENERATORS["claude_skills"]
        dst = gen.target_dir(tmp_path, "leak", scope="user")
        assert dst is not None
        dst.mkdir(parents=True)
        sentinel_path = dst / SKILL_MANIFEST
        sentinel_path.write_text("PRE-EXISTING\n", encoding="utf-8")
        result = generate_all_skills(tmp_path, runtimes=["claude_skills"], scope="user")
        # Positive: skip emitted with PRIVACY_BLOCKED code.
        privacy_skips = [s for s in result.skipped if s[2] == skip_codes.PRIVACY_BLOCKED]
        assert len(privacy_skips) == 1, result.skipped
        assert privacy_skips[0][0] == "leak"
        # Negative: dst contents UNTOUCHED.
        assert sentinel_path.read_text(encoding="utf-8") == "PRE-EXISTING\n"
        # Negative: no orphan staging dir under dst.parent.
        leftovers = [
            p.name for p in dst.parent.iterdir() if p.name.startswith((".staging-", ".old-"))
        ]
        assert leftovers == [], leftovers

    def test_secret_in_scripts_raises_project_shared(self, tmp_path: Path) -> None:
        _seed_canonical_skill(
            tmp_path,
            name="leak",
            extras={"scripts/leak.sh": f"#!/bin/bash\necho {SECRET}\n"},
        )
        gen = SKILL_GENERATORS["claude_skills"]
        dst = gen.target_dir(tmp_path, "leak")
        assert dst is not None
        # Pre-create with sentinel.
        dst.mkdir(parents=True)
        sentinel_path = dst / SKILL_MANIFEST
        sentinel_path.write_text("PRE-EXISTING\n", encoding="utf-8")
        # project_shared → ClickException raised.
        import click

        with pytest.raises(click.ClickException) as exc_info:
            generate_all_skills(tmp_path, runtimes=["claude_skills"], scope="project_shared")
        assert "Gate A" in exc_info.value.message
        assert "leak.sh" in exc_info.value.message
        # Negative: dst untouched.
        assert sentinel_path.read_text(encoding="utf-8") == "PRE-EXISTING\n"
        # Negative: no orphan staging.
        leftovers = [
            p.name for p in dst.parent.iterdir() if p.name.startswith((".staging-", ".old-"))
        ]
        assert leftovers == []


class TestOverridePreservation:
    def test_override_only_touches_skill_md_through_staging(self, tmp_path: Path) -> None:
        # Mirror ``test_context_override.py:317`` against the new
        # staging+promote flow. The override file replaces SKILL.md in
        # staging BEFORE the scan; auxiliary files (scripts/) stay from
        # canonical's copy_tree_atomic. Post-promote, dst has the
        # override SKILL.md AND the canonical scripts/.
        _seed_canonical_skill(
            tmp_path,
            name="foo",
            skill_md="---\nname: foo\n---\ncanonical body\n",
            extras={"scripts/run.sh": "#!/bin/bash\necho canonical\n"},
        )
        # Canonical-side override file at <canonical>/foo/overrides/claude.md.
        canonical = canonical_skills_root(tmp_path)
        override_dir = canonical / "foo" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "claude.md").write_text(
            "---\nname: foo\n---\nclaude only\n", encoding="utf-8"
        )

        result = generate_all_skills(tmp_path, runtimes=["claude_skills"])
        # generation succeeded
        assert any(r == "claude_skills" for r, _ in result.generated)
        gen = SKILL_GENERATORS["claude_skills"]
        target = gen.target_dir(tmp_path, "foo")
        assert target is not None
        # SKILL.md is the override.
        assert (target / SKILL_MANIFEST).read_text(encoding="utf-8") == (
            "---\nname: foo\n---\nclaude only\n"
        )
        # scripts/ comes from canonical (untouched by override).
        assert (target / "scripts" / "run.sh").read_text(encoding="utf-8") == (
            "#!/bin/bash\necho canonical\n"
        )

    def test_secret_in_override_blocks_before_promote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin: override bytes are scanned (since they replace the
        # canonical SKILL.md before write). A clean canonical + dirty
        # override must still block. Use scope=user with HOME isolation
        # so the block path emits a skip (project_shared raises).
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_canonical_skill(
            tmp_path,
            name="foo",
            scope="user",
            skill_md="---\nname: foo\n---\nclean canonical\n",
        )
        canonical = canonical_skills_root(tmp_path, scope="user")
        override_dir = canonical / "foo" / "overrides"
        override_dir.mkdir(parents=True)
        (override_dir / "claude.md").write_text(
            f"---\nname: foo\n---\nleaked: {SECRET}\n", encoding="utf-8"
        )
        gen = SKILL_GENERATORS["claude_skills"]
        dst = gen.target_dir(tmp_path, "foo", scope="user")
        assert dst is not None

        result = generate_all_skills(tmp_path, runtimes=["claude_skills"], scope="user")
        # Positive: skip emitted.
        privacy_skips = [s for s in result.skipped if s[2] == skip_codes.PRIVACY_BLOCKED]
        assert len(privacy_skips) == 1, result.skipped
        # Negative: dst not created.
        assert not dst.exists()
