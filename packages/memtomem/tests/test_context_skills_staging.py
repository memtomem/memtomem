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

    def test_rollback_failure_preserves_original_and_logs_breadcrumb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        # Both the staging→dst promote AND the old→dst rollback fail (issue
        # #1123 B3-4). The ORIGINAL promote error must propagate (not the
        # rollback error masking it), the original tree must survive at the
        # move-aside ``.old-*`` path, and a logger.error breadcrumb must name
        # it so an operator can recover manually.
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        dst.mkdir(parents=True)
        (dst / SKILL_MANIFEST).write_text("ORIGINAL CONTENT\n", encoding="utf-8")
        staging = _stage_skill(src, dst)

        original_replace = os.replace
        call_count = {"n": 0}

        def fail_promote_and_rollback(a, b):
            call_count["n"] += 1
            # 1: dst→old (succeeds); 2: staging→dst (fails); 3: old→dst rollback (fails).
            if call_count["n"] >= 2:
                raise OSError(f"forced replace failure #{call_count['n']}")
            return original_replace(a, b)

        monkeypatch.setattr(os, "replace", fail_promote_and_rollback)
        with caplog.at_level("ERROR", logger="memtomem.context.skills"):
            with pytest.raises(OSError) as exc_info:
                _promote_staging(staging, dst)

        # The ORIGINAL promote failure (#2) propagates, chained from the
        # rollback failure (#3) — the rollback error must not mask it.
        assert "forced replace failure #2" in str(exc_info.value)
        assert isinstance(exc_info.value.__cause__, OSError)
        assert "forced replace failure #3" in str(exc_info.value.__cause__)

        # The original tree survives at the move-aside path (recoverable).
        old_dirs = list(dst.parent.glob(".old-foo-*.tmp"))
        assert len(old_dirs) == 1
        assert (old_dirs[0] / SKILL_MANIFEST).read_text(encoding="utf-8") == "ORIGINAL CONTENT\n"

        # A breadcrumb names the preserved tree so it can be recovered.
        assert any(
            "rollback failed" in r.getMessage() and old_dirs[0].name in r.getMessage()
            for r in caplog.records
        )


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
        # project_shared → PrivacyBlockedError raised (Click-free so
        # non-CLI surfaces can translate; #895 P2 review fold).
        from memtomem.context.privacy_scan import PrivacyBlockedError

        with pytest.raises(PrivacyBlockedError) as exc_info:
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

    def test_project_shared_block_later_skill_leaves_no_partial_fanout(
        self, tmp_path: Path
    ) -> None:
        # #895 follow-up: project_shared is an all-or-nothing privacy
        # surface. A clean earlier skill must not be promoted before a
        # later skill fails Gate A.
        _seed_canonical_skill(tmp_path, name="clean")
        _seed_canonical_skill(
            tmp_path,
            name="leak",
            extras={"scripts/leak.sh": f"#!/bin/bash\necho {SECRET}\n"},
        )
        gen = SKILL_GENERATORS["claude_skills"]
        clean_dst = gen.target_dir(tmp_path, "clean")
        leak_dst = gen.target_dir(tmp_path, "leak")
        assert clean_dst is not None
        assert leak_dst is not None

        from memtomem.context.privacy_scan import PrivacyBlockedError

        with pytest.raises(PrivacyBlockedError):
            generate_all_skills(tmp_path, runtimes=["claude_skills"], scope="project_shared")

        assert not clean_dst.exists()
        assert not leak_dst.exists()
        assert not [
            p.name for p in clean_dst.parent.iterdir() if p.name.startswith((".staging-", ".old-"))
        ]


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


class TestLockBudget:
    """#1229 (review 2026-06-10): destination sidecar-lock acquisition in
    ``generate_all_skills`` is bounded by a whole-call budget so the engine can
    be offloaded to a worker thread by the web route without an unbounded
    cross-process lock wait blocking forever (the #1145 settings shape —
    ``asyncio.timeout`` on the caller's loop cannot fire while the loop thread
    itself is blocked, and ``asyncio.to_thread`` cannot cancel a wedged
    thread).
    """

    def test_held_lock_aborts_batch_within_bound_not_hangs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """project_shared batch path: a foreign holder on ANY destination lock
        aborts the WHOLE batch (all-or-nothing is preserved — locks are taken
        before any staging) with a typed ``lock_timeout`` skip, instead of
        blocking indefinitely."""
        import time as _time

        import memtomem.context.skills as skills_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        _seed_canonical_skill(tmp_path, name="foo")
        dst = SKILL_GENERATORS["claude_skills"].target_dir(tmp_path, "foo", scope="project_shared")
        assert dst is not None

        monkeypatch.setattr(skills_mod, "_SKILLS_LOCK_BUDGET_S", 0.2)
        start = _time.monotonic()
        # Foreign holder: separate open-file-description — portalocker
        # contends per-OFD even within one process.
        with _file_lock(_lock_path_for(dst)):
            result = generate_all_skills(tmp_path)
        elapsed = _time.monotonic() - start

        assert result.generated == []
        assert [s for s in result.skipped if s[2] == skip_codes.LOCK_TIMEOUT], result.skipped
        skip = next(s for s in result.skipped if s[2] == skip_codes.LOCK_TIMEOUT)
        assert skip[0] == "<all>"
        assert "acquisition budget" in skip[1]
        # Bounded: ~one budget + overhead, never an indefinite block. Loose
        # bound — CI runners are slow; the point is "seconds, not forever".
        assert elapsed < 10, f"abort took {elapsed:.1f}s — budget not applied?"
        # Nothing promoted while the lock was held.
        assert not dst.exists()

    def test_held_lock_skips_only_contended_destination_on_user_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """user-scope per-destination path: only the contended destination is
        skipped (typed ``lock_timeout``); the remaining runtimes still fan
        out — non-shared scopes carry no all-or-nothing batch contract."""
        import memtomem.context.skills as skills_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        _seed_canonical_skill(tmp_path, name="foo", scope="user")

        held_dst = SKILL_GENERATORS["claude_skills"].target_dir(tmp_path, "foo", scope="user")
        assert held_dst is not None

        monkeypatch.setattr(skills_mod, "_SKILLS_LOCK_BUDGET_S", 0.2)
        with _file_lock(_lock_path_for(held_dst)):
            result = generate_all_skills(tmp_path, scope="user")

        lock_skips = [s for s in result.skipped if s[2] == skip_codes.LOCK_TIMEOUT]
        assert len(lock_skips) == 1, result.skipped
        assert lock_skips[0][0] == "foo"
        # The contended claude destination was not written...
        assert not held_dst.exists()
        # ...but at least one other runtime destination was.
        other_runtimes = {rt for rt, _p in result.generated}
        assert other_runtimes, result.skipped
        assert "claude_skills" not in other_runtimes


class TestTargetConflict:
    """#1229: a pre-existing non-skill destination (a directory with content
    but no SKILL.md, or a plain file) made ``_promote_staging`` raise
    IsADirectoryError/NotADirectoryError out of ``generate_all_skills`` —
    an uncaught mid-batch crash that also left the project_shared batch
    partially promoted. The sync paths now preflight the same refusal
    predicate (``_target_conflict``) under the destination locks and convert
    it into a typed ``target_conflict`` skip; the promote calls keep a
    residual catch for non-gateway writers landing between preflight and
    promote (the sidecar lock only serializes gateway writers)."""

    def test_project_shared_preexisting_conflict_skips_only_that_destination(
        self, tmp_path: Path
    ) -> None:
        """Batch path: the conflicted destination becomes a typed skip BEFORE
        anything is promoted; every other destination still fans out and the
        conflicting user content is left byte-identical."""
        _seed_canonical_skill(tmp_path, name="foo")
        gemini_dst = SKILL_GENERATORS["gemini_skills"].target_dir(
            tmp_path, "foo", scope="project_shared"
        )
        assert gemini_dst is not None
        gemini_dst.mkdir(parents=True)
        (gemini_dst / "notes.txt").write_text("hand-made WIP", encoding="utf-8")

        result = generate_all_skills(tmp_path)  # must not raise

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert str(gemini_dst) in conflicts[0][1]
        # The conflicting directory is untouched.
        assert (gemini_dst / "notes.txt").read_text(encoding="utf-8") == "hand-made WIP"
        assert not (gemini_dst / SKILL_MANIFEST).exists()
        # All other runtimes promoted.
        promoted = {rt for rt, _p in result.generated}
        assert "gemini_skills" not in promoted
        assert {"claude_skills", "codex_skills", "kimi_skills"} <= promoted
        # No staging or move-aside leftovers anywhere.
        assert not list(gemini_dst.parent.glob(".staging-*"))
        assert not list(gemini_dst.parent.glob(".old-*"))

    def test_conflict_with_plain_file_destination(self, tmp_path: Path) -> None:
        """NotADirectoryError flavor: the destination path exists as a FILE."""
        _seed_canonical_skill(tmp_path, name="foo")
        kimi_dst = SKILL_GENERATORS["kimi_skills"].target_dir(
            tmp_path, "foo", scope="project_shared"
        )
        assert kimi_dst is not None
        kimi_dst.parent.mkdir(parents=True)
        kimi_dst.write_text("i am a file, not a skill dir", encoding="utf-8")

        result = generate_all_skills(tmp_path)  # must not raise

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert "not a directory" in conflicts[0][1]
        assert kimi_dst.read_text(encoding="utf-8") == "i am a file, not a skill dir"
        promoted = {rt for rt, _p in result.generated}
        assert "kimi_skills" not in promoted
        assert {"claude_skills", "codex_skills", "gemini_skills"} <= promoted

    def test_batch_promote_conflict_after_preflight_is_typed_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual race in the batch path: a NON-gateway writer (the sidecar
        lock only serializes gateway writers) lands conflicting content at a
        destination after the preflight but before the promote loop. The
        promote refusal is converted to the same typed skip and the rest of
        the batch still promotes — previously this exact shape crashed with
        earlier destinations already promoted."""
        import memtomem.context.skills as skills_mod

        _seed_canonical_skill(tmp_path, name="foo")
        gemini_dst = SKILL_GENERATORS["gemini_skills"].target_dir(
            tmp_path, "foo", scope="project_shared"
        )
        assert gemini_dst is not None

        orig_scan = skills_mod.scan_artifact_tree

        def planting_scan(staging, **kwargs):
            out = orig_scan(staging, **kwargs)
            if staging.parent == gemini_dst.parent:
                # Simulated external writer: drops content at the destination
                # AFTER its preflight ran (staging happens after preflight)
                # and BEFORE the promote loop (which runs after all scans).
                gemini_dst.mkdir(parents=True, exist_ok=True)
                (gemini_dst / "intruder.txt").write_text("external", encoding="utf-8")
            return out

        monkeypatch.setattr(skills_mod, "scan_artifact_tree", planting_scan)
        result = generate_all_skills(tmp_path)  # must not raise

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert (gemini_dst / "intruder.txt").read_text(encoding="utf-8") == "external"
        promoted = {rt for rt, _p in result.generated}
        assert "gemini_skills" not in promoted
        assert {"claude_skills", "codex_skills", "kimi_skills"} <= promoted
        assert not list(gemini_dst.parent.glob(".staging-*"))

    def test_user_scope_preexisting_conflict_is_typed_per_item_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Per-item path (non-shared scope): the conflicted destination is a
        typed skip before any stage/scan work; other runtimes still fan out."""
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        _seed_canonical_skill(tmp_path, name="foo", scope="user")
        claude_dst = SKILL_GENERATORS["claude_skills"].target_dir(tmp_path, "foo", scope="user")
        assert claude_dst is not None
        claude_dst.mkdir(parents=True)
        (claude_dst / "notes.txt").write_text("hand-made WIP", encoding="utf-8")

        result = generate_all_skills(tmp_path, scope="user")  # must not raise

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert (claude_dst / "notes.txt").read_text(encoding="utf-8") == "hand-made WIP"
        assert not (claude_dst / SKILL_MANIFEST).exists()
        promoted = {rt for rt, _p in result.generated}
        assert "claude_skills" not in promoted
        assert promoted, result.skipped  # other runtimes unaffected

    def test_user_scope_promote_conflict_after_preflight_is_typed_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Residual-race catch on the per-item promote (same shape as the
        batch-path race test above, exercising the second code path)."""
        import memtomem.context.skills as skills_mod

        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        _seed_canonical_skill(tmp_path, name="foo", scope="user")
        claude_dst = SKILL_GENERATORS["claude_skills"].target_dir(tmp_path, "foo", scope="user")
        assert claude_dst is not None

        orig_scan = skills_mod.scan_artifact_tree

        def planting_scan(staging, **kwargs):
            out = orig_scan(staging, **kwargs)
            if staging.parent == claude_dst.parent:
                claude_dst.mkdir(parents=True, exist_ok=True)
                (claude_dst / "intruder.txt").write_text("external", encoding="utf-8")
            return out

        monkeypatch.setattr(skills_mod, "scan_artifact_tree", planting_scan)
        result = generate_all_skills(tmp_path, scope="user")  # must not raise

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert (claude_dst / "intruder.txt").read_text(encoding="utf-8") == "external"
        promoted = {rt for rt, _p in result.generated}
        assert "claude_skills" not in promoted
        assert promoted, result.skipped
        assert not list(claude_dst.parent.glob(".staging-*"))
