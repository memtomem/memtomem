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

import errno
import logging
import os
import sys
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.context.skills import (
    SKILL_GENERATORS,
    SKILL_MANIFEST,
    _iter_scannable_skill_files,
    _promote_staging,
    _reap_move_aside,
    _recover_and_reap_internal_dirs,
    _stage_skill,
    canonical_skills_root,
    copy_skill,
    extract_skills_to_canonical,
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

    def test_promote_no_replace_consumes_staging(self, tmp_path: Path) -> None:
        src = _seed_canonical_skill(tmp_path, name="foo")
        dst = tmp_path / ".claude" / "skills" / "foo"
        staging = _stage_skill(src, dst)

        _promote_staging(staging, dst, replace_existing=False)

        assert not staging.exists()
        assert (dst / SKILL_MANIFEST).read_bytes() == (src / SKILL_MANIFEST).read_bytes()

    @pytest.mark.parametrize("valid_skill", [False, True], ids=["empty-dir", "valid-skill"])
    def test_promote_no_replace_preserves_existing_destination(
        self, tmp_path: Path, valid_skill: bool
    ) -> None:
        src = _seed_canonical_skill(
            tmp_path,
            name="foo",
            skill_md="---\nname: foo\n---\nimport candidate\n",
        )
        dst = tmp_path / ".claude" / "skills" / "foo"
        dst.mkdir(parents=True)
        if valid_skill:
            (dst / SKILL_MANIFEST).write_text("external writer\n", encoding="utf-8")
        staging = _stage_skill(src, dst)

        with pytest.raises(FileExistsError):
            _promote_staging(staging, dst, replace_existing=False)

        assert dst.is_dir()
        assert staging.is_dir(), "collision cleanup belongs to the caller"
        if valid_skill:
            assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "external writer\n"
        else:
            assert list(dst.iterdir()) == []
        assert not list(dst.parent.glob(".old-foo-*.tmp"))

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


class TestStaleLeftoverReaping:
    """Crash-leftover staging/move-aside trees are reaped under the held dst
    lock and are recognized by the shared discovery filter (#1229)."""

    def test_staging_names_match_internal_predicate(self, tmp_path: Path) -> None:
        """Construction↔predicate parity pin: the exact names _stage_skill and
        _promote_staging produce must satisfy is_internal_artifact_dir — if
        either f-string shape changes, this fails before the filters drift."""
        from memtomem.context._names import is_internal_artifact_dir

        src = _seed_canonical_skill(tmp_path, name="parity")
        dst = tmp_path / ".claude/skills/parity"
        staging = _stage_skill(src, dst)
        try:
            assert is_internal_artifact_dir(staging.name), staging.name
        finally:
            import shutil

            shutil.rmtree(staging, ignore_errors=True)
        # _promote_staging's move-aside shape (not exercised without a crash):
        assert is_internal_artifact_dir(".old-parity-12345-abc123.tmp")
        # Negative pins: real skills and user dot-dirs never match — including
        # valid user names that mimic the prefix/suffix but lack the generated
        # pid+rand shape (Codex review: a looser match would hide and even
        # delete them).
        assert not is_internal_artifact_dir("parity")
        assert not is_internal_artifact_dir(".hidden-skill")
        assert not is_internal_artifact_dir(".staging-notes.tmp")
        assert not is_internal_artifact_dir(".old-archive.tmp")
        assert not is_internal_artifact_dir(".staging-parity-notes.tmp")
        assert not is_internal_artifact_dir(".staging-parity-12345-xyz.tmp")

    def test_a_trailing_newline_is_not_an_internal_artifact(self) -> None:
        """Python's ``$`` also matches immediately before a final newline, and
        a newline is a legal POSIX filename character — so an anchored
        ``…\\.tmp$`` would classify ``.old-parity-12345-abc123.tmp\\n`` as our
        own leftover and hand it to the reaper. We never create such a name, so
        anything wearing one belongs to somebody else."""
        from memtomem.context._names import internal_artifact_owner, is_internal_artifact_dir

        assert not is_internal_artifact_dir(".old-parity-12345-abc123.tmp\n")
        assert internal_artifact_owner(".staging-parity-12345-abc123.tmp\n") is None

    def test_owner_parse_splits_at_the_anchored_suffix(self) -> None:
        """Pins the owner/suffix split itself, not just its consequences.

        `internal_artifact_owner` is what makes reaping exact. The interesting
        input is a name carrying a second suffix-shaped run, which is where a
        weaker pattern would pick the wrong owner and reap a neighbour.

        This pins the OUTCOME, and deliberately does not name a mechanism.
        Measured against `.old-foo-123-abc123-456-def789.tmp`, the anchor and
        the greedy quantifier are **independently sufficient** — only dropping
        the trailing `.tmp\\Z` *and* making `.+` lazy flips the parse to `foo`,
        and either single mutation leaves this green. Two earlier versions of
        this docstring each credited one of them; the first was wrong, and the
        second "verified" its claim with a mutation that changed both at once.
        """
        from memtomem.context._names import internal_artifact_owner, is_internal_artifact_dir

        assert internal_artifact_owner(".old-foo-999999-abc123.tmp") == "foo"
        assert internal_artifact_owner(".staging-foo-bar-999999-abc123.tmp") == "foo-bar"
        # Two suffix-shaped runs: the LAST one is the suffix, the rest is the
        # owner. A leftover carries exactly one pid+rand, so a skill genuinely
        # named `foo-123-abc123` is the only way to produce this.
        assert internal_artifact_owner(".old-foo-123-abc123-456-def789.tmp") == "foo-123-abc123"
        # Not internal-shaped at all -> no owner (the #1229 rule).
        assert internal_artifact_owner(".staging-parity-notes.tmp") is None
        assert internal_artifact_owner("parity") is None
        # The two predicates are one match, so they cannot disagree.
        for name in (
            ".old-foo-999999-abc123.tmp",
            ".staging-parity-notes.tmp",
            "parity",
            ".old-archive.tmp",
        ):
            assert is_internal_artifact_dir(name) == (internal_artifact_owner(name) is not None)

    def test_reap_spares_user_dirs_matching_glob_but_not_shape(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A user skill dir whose name matches the reap GLOB but not the
        generated pid+rand shape must survive the sync-time reaper."""
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_canonical_skill(tmp_path, name="hello")
        gen = SKILL_GENERATORS["claude_skills"]
        dst = gen.target_dir(tmp_path, "hello")
        assert dst is not None
        dst.parent.mkdir(parents=True, exist_ok=True)
        user_dir = dst.parent / ".staging-hello-notes.tmp"
        user_dir.mkdir()
        (user_dir / SKILL_MANIFEST).write_text("user content\n", encoding="utf-8")

        generate_all_skills(tmp_path, runtimes=["claude_skills"])

        assert user_dir.is_dir()
        assert (user_dir / SKILL_MANIFEST).read_text(encoding="utf-8") == "user content\n"

    def test_discovery_keeps_user_dirs_mimicking_prefix(self, tmp_path: Path) -> None:
        """Discovery loops must still LIST a user skill named like the
        staging prefix without the pid+rand shape."""
        from memtomem.context.skills import list_canonical_skills

        d = tmp_path / ".memtomem/skills/.staging-notes.tmp"
        d.mkdir(parents=True)
        (d / SKILL_MANIFEST).write_text("user content\n", encoding="utf-8")
        assert [s.name for s in list_canonical_skills(tmp_path)] == [".staging-notes.tmp"]

    def test_reaping_is_scoped_to_the_destination_it_holds_the_lock_for(
        self, tmp_path: Path
    ) -> None:
        """The lock covers one destination, so the reaper must only delete
        trees that provably belong to it.

        A prefix glob does not prove it: with ``dst.name == "foo"``,
        ``.old-foo-*.tmp`` also matches ``.old-foo-bar-<pid>-<rand>.tmp``,
        which belongs to the valid skill ``foo-bar``. Syncing ``foo`` therefore
        deleted ``foo-bar``'s in-flight rollback and staging trees while
        holding the wrong lock (Codex review; live since #1229). Hyphenated
        skill names are the norm, so this is an ordinary-input bug, not an
        adversarial one.
        """
        from memtomem.context._names import is_internal_artifact_dir

        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        dst.mkdir()
        (dst / SKILL_MANIFEST).write_text("foo\n", encoding="utf-8")
        neighbor_old = root / ".old-foo-bar-999999-abc123.tmp"
        neighbor_old.mkdir()
        (neighbor_old / SKILL_MANIFEST).write_text("foo-bar's only copy\n", encoding="utf-8")
        neighbor_staging = root / ".staging-foo-bar-999999-abc123.tmp"
        neighbor_staging.mkdir()
        own = root / ".old-foo-999999-abc123.tmp"
        own.mkdir()
        # Both leftovers are internal-shaped; shape alone cannot separate them.
        assert is_internal_artifact_dir(neighbor_old.name)
        assert is_internal_artifact_dir(own.name)

        _recover_and_reap_internal_dirs(dst)

        assert not own.exists(), "own leftover not reaped"
        assert neighbor_old.is_dir(), "reaped a neighbouring skill's move-aside tree"
        assert neighbor_staging.is_dir(), "reaped a neighbouring skill's staging tree"
        assert (neighbor_old / SKILL_MANIFEST).read_text(encoding="utf-8") == (
            "foo-bar's only copy\n"
        )

    # Each victim is chosen to actually MATCH its metacharacter — a victim the
    # pattern could never reach would pass with or without escaping and pin
    # nothing (mutation-verified).
    @pytest.mark.parametrize(
        ("weird", "victim_name"),
        [
            # ``*`` and ``?`` are reserved on Windows — the destination cannot
            # be created there at all, so the case does not exist. ``[`` is
            # legal, so that parameter still runs on every OS.
            pytest.param(
                "foo*",
                "foobar",
                marks=pytest.mark.skipif(
                    sys.platform == "win32", reason="'*' is a reserved Windows filename character"
                ),
            ),
            pytest.param(
                "foo?",
                "foox",
                marks=pytest.mark.skipif(
                    sys.platform == "win32", reason="'?' is a reserved Windows filename character"
                ),
            ),
            pytest.param("foo[a]", "fooa"),
        ],
    )
    def test_glob_metacharacters_in_dst_cannot_reach_another_destination(
        self, tmp_path: Path, weird: str, victim_name: str
    ) -> None:
        """A destination whose own name contains a glob metacharacter must not
        reach another destination's leftovers either: unescaped, ``foo*``
        matched — and deleted — ``foobar``'s trees.

        This pins the OUTCOME, not one mechanism. Removing ``glob.escape``
        alone does not fail it (mutation-verified), because the owner equality
        above already rejects the widened matches; escaping is scan narrowing.
        The assertion is still worth keeping — metacharacter names are a real
        input class, and this is the test that would catch it if the owner
        check were ever loosened for them.
        """
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        victim = root / f".old-{victim_name}-999999-abc123.tmp"
        victim.mkdir()
        (victim / SKILL_MANIFEST).write_text("victim\n", encoding="utf-8")
        victim_staging = root / f".staging-{victim_name}-999999-abc123.tmp"
        victim_staging.mkdir()
        dst = root / weird
        dst.mkdir()
        (dst / SKILL_MANIFEST).write_text("canonical\n", encoding="utf-8")

        _recover_and_reap_internal_dirs(dst)

        assert victim.is_dir(), f"{weird!r} reached another destination's move-aside tree"
        assert victim_staging.is_dir(), f"{weird!r} reached another destination's staging tree"

    @pytest.mark.requires_symlinks
    def test_symlink_leftover_is_unlinked_not_silently_kept(self, tmp_path: Path) -> None:
        """``shutil.rmtree`` refuses a symlink, and ``ignore_errors=True``
        refuses *silently*. A destination that was a symlink therefore left a
        dead ``.old-…`` link nothing ever removed — one per run, forever, for a
        setup that recreates a managed symlink before each push. Removal
        dispatches on ``lstat`` instead.
        """
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / SKILL_MANIFEST).write_text("linked\n", encoding="utf-8")
        dst = root / "foo"
        dst.mkdir()
        old = root / ".old-foo-999999-abc123.tmp"
        old.symlink_to(elsewhere, target_is_directory=True)

        _recover_and_reap_internal_dirs(dst)

        assert not old.is_symlink(), "dead move-aside symlink survived the reaper"
        assert not old.exists()
        # The link's target is user data one directory over — never followed.
        assert (elsewhere / SKILL_MANIFEST).read_text(encoding="utf-8") == "linked\n"

    def test_regular_file_leftover_is_preserved_not_unlinked(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The symlink fix must not widen into "unlink every non-directory".

        A regular file at an artifact-shaped path survives today (``rmtree``
        refuses it), and it can be an out-of-band writer's file that the
        promote moved aside between its conflict check and its rename. Deleting
        it would be unrecoverable, so an unexpected type is preserved and
        logged instead (Codex review).
        """
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        dst.mkdir()
        stray = root / ".old-foo-999999-abc123.tmp"
        stray.write_text("someone else's bytes\n", encoding="utf-8")

        with caplog.at_level(logging.WARNING, logger="memtomem.context.skills"):
            _recover_and_reap_internal_dirs(dst)

        assert stray.is_file(), "a regular leftover was unlinked"
        assert stray.read_text(encoding="utf-8") == "someone else's bytes\n"
        assert any(str(stray) in r.getMessage() for r in caplog.records), caplog.text

    @pytest.mark.requires_symlinks
    def test_symlink_dst_promote_leaves_no_residue(self, tmp_path: Path) -> None:
        """End-to-end: a symlinked destination survives ``_target_conflict``
        (whose checks all follow links), so the promote genuinely moves a
        *symlink* aside and must clean that up too."""
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / SKILL_MANIFEST).write_text("linked\n", encoding="utf-8")
        dst = root / "foo"
        dst.symlink_to(elsewhere, target_is_directory=True)
        staging = root / ".staging-foo-111111-abc123.tmp"
        staging.mkdir()
        (staging / SKILL_MANIFEST).write_text("new\n", encoding="utf-8")

        _promote_staging(staging, dst, replace_existing=True)

        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "new\n"
        assert not dst.is_symlink()
        residue = [p.name for p in root.iterdir() if p.name.startswith((".old-", ".staging-"))]
        assert residue == [], residue
        assert (elsewhere / SKILL_MANIFEST).read_text(encoding="utf-8") == "linked\n"

    def test_copy_skill_holds_the_destination_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``copy_skill`` creates ``.old-*`` trees like every other writer, so
        it has to contend on the same lock they do (ADR-0030 §6).

        It used to skip the lock, making it the one path able to park a
        move-aside no other writer knew about — and a concurrent gateway flow
        reaping that destination would delete the tree this copy was about to
        roll back onto (Codex review). Probed from inside the promote, where
        the lock must already be held.
        """
        import memtomem.context.skills as skills_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        src = _seed_canonical_skill(tmp_path, name="hello")
        dst = tmp_path / ".claude/skills/hello"
        dst.parent.mkdir(parents=True, exist_ok=True)
        contended: list[bool] = []
        orig_promote = skills_mod._promote_staging

        def probing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            try:
                with _file_lock(_lock_path_for(dst), timeout=0):
                    contended.append(False)
            except TimeoutError:
                contended.append(True)
            orig_promote(staging, dst, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(skills_mod, "_promote_staging", probing_promote)
        skills_mod.copy_skill(src, dst)

        assert contended == [True], "copy_skill promoted without holding the destination lock"
        assert (dst / SKILL_MANIFEST).is_file()

    def test_copy_skill_rejects_a_bad_source_without_touching_the_destination(
        self, tmp_path: Path
    ) -> None:
        """Acquiring the lock has side effects — it creates `dst.parent` and a
        `.{name}.lock` sidecar there — so the source is preflighted first.

        Otherwise a call that previously created nothing at all would start
        leaving a directory and a lock file behind whenever `src` was wrong.
        """
        root = tmp_path / "workspace"
        src = root / "nonexistent"
        dst = root / "runtime" / "skills" / "hello"

        with pytest.raises(FileNotFoundError):
            copy_skill(src, dst)

        assert not dst.parent.exists(), "a rejected copy created the destination directory"
        assert not root.exists(), "a rejected copy left artifacts behind"

    @pytest.mark.parametrize("scope", ["project_shared", "user"])
    def test_generate_reaps_stale_leftovers(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
    ) -> None:
        """Pre-existing stale staging/old trees next to a destination are
        removed by a sync that holds the dst sidecar lock, and the real skill
        still promotes.

        Both clear in ONE operation, but by different mechanisms, and that
        distinction is the ADR-0030 §10 rule. The prelude runs before the
        promote, so it sees an absent ``dst`` and keeps the move-aside tree —
        at that instant indistinguishable from the only surviving copy of an
        interrupted promote. The promote then installs ``dst``, and its
        success path reaps it under the same lock.
        """
        home = tmp_path / "home"
        set_home(monkeypatch, str(home))
        _seed_canonical_skill(tmp_path, name="hello", scope=scope)
        gen = SKILL_GENERATORS["claude_skills"]
        dst = gen.target_dir(tmp_path, "hello", scope=scope)  # type: ignore[arg-type]
        assert dst is not None
        dst.parent.mkdir(parents=True, exist_ok=True)
        stale_staging = dst.parent / ".staging-hello-999999-abc123.tmp"
        stale_staging.mkdir()
        (stale_staging / SKILL_MANIFEST).write_text("stale\n", encoding="utf-8")
        stale_old = dst.parent / ".old-hello-999999-abc123.tmp"
        stale_old.mkdir()
        (stale_old / SKILL_MANIFEST).write_text("stale\n", encoding="utf-8")

        result = generate_all_skills(tmp_path, runtimes=["claude_skills"], scope=scope)  # type: ignore[arg-type]

        assert ("claude_skills", dst) in result.generated
        assert not stale_staging.exists()
        assert not stale_old.exists()
        assert (dst / SKILL_MANIFEST).is_file()


class TestMoveAsideReapingIsStateAware:
    """ADR-0030 §10: an ``.old-*`` is reaped only while the canonical is
    present.

    The reaper used to delete every ``.old-*`` it owned, unconditionally. But
    :func:`_promote_staging` parks the ORIGINAL tree there for the instant
    between its two renames — POSIX cannot atomically replace a non-empty
    directory — so a crash in that window left the only copy of a canonical
    skill to be destroyed by the next run. A staging tree is a copy whose
    source is still on disk; a move-aside tree is not. These pin the
    asymmetry, and that the keep does not become a permanent leak.
    """

    @staticmethod
    def _seed_old(parent: Path, name: str) -> Path:
        old = parent / f".old-{name}-999999-abc123.tmp"
        old.mkdir(parents=True)
        (old / SKILL_MANIFEST).write_text("the only copy\n", encoding="utf-8")
        return old

    def test_old_survives_when_dst_absent(self, tmp_path: Path) -> None:
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        old = self._seed_old(root, "foo")

        _recover_and_reap_internal_dirs(dst)

        assert old.is_dir()
        assert (old / SKILL_MANIFEST).read_text(encoding="utf-8") == "the only copy\n"

    def test_old_is_reaped_when_dst_present(self, tmp_path: Path) -> None:
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        dst.mkdir()
        (dst / SKILL_MANIFEST).write_text("canonical\n", encoding="utf-8")
        old = self._seed_old(root, "foo")

        _recover_and_reap_internal_dirs(dst)

        assert not old.exists()
        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "canonical\n"

    def test_staging_is_reaped_even_when_dst_absent(self, tmp_path: Path) -> None:
        """The relaxation is scoped to ``.old-*``. A staging tree is never the
        only copy of anything, so its reaping stays unconditional — otherwise
        a crashed sync would leak one until the next successful sync."""
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        staging = root / ".staging-foo-999999-abc123.tmp"
        staging.mkdir()
        (staging / SKILL_MANIFEST).write_text("partial\n", encoding="utf-8")

        _recover_and_reap_internal_dirs(dst)

        assert not staging.exists()

    @pytest.mark.requires_symlinks
    def test_symlinked_dst_does_not_license_reaping(self, tmp_path: Path) -> None:
        """Presence is tested with ``lstat``. A symlink at the canonical path
        is not the canonical tree, and letting it stand in would delete the
        real one on the strength of a link placed by accident or otherwise."""
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        (elsewhere / SKILL_MANIFEST).write_text("decoy\n", encoding="utf-8")
        dst = root / "foo"
        dst.symlink_to(elsewhere, target_is_directory=True)
        old = self._seed_old(root, "foo")

        _recover_and_reap_internal_dirs(dst)

        assert old.is_dir()

    def test_kept_leftover_is_logged_with_both_paths(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A silent leak is indistinguishable from no leak. The breadcrumb has
        to name the surviving tree AND the canonical it belongs to, or an
        operator cannot act on it."""
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        old = self._seed_old(root, "foo")

        with caplog.at_level(logging.WARNING, logger="memtomem.context.skills"):
            _recover_and_reap_internal_dirs(dst)

        assert any(
            str(old) in rec.getMessage() and str(dst) in rec.getMessage()
            for rec in caplog.records
            if rec.levelno >= logging.WARNING
        ), caplog.text

    def test_promote_reaps_the_tree_the_prelude_had_to_keep(self, tmp_path: Path) -> None:
        """The keep-branch must not become a permanent leak.

        For the reverse-import path nothing ever comes back: a second import
        refuses at the pre-lock overwrite check without re-acquiring the lock.
        So the promote's success path is the only thing standing between "kept
        for safety" and "kept forever".
        """
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        old = self._seed_old(root, "foo")

        _recover_and_reap_internal_dirs(dst)
        assert old.is_dir(), "prelude reaped it while dst was absent"

        # Staged after the prelude, as a real writer does — the prelude reaps
        # staging trees unconditionally, so seeding one first would be eaten.
        staging = root / ".staging-foo-111111-abc123.tmp"
        staging.mkdir()
        (staging / SKILL_MANIFEST).write_text("new\n", encoding="utf-8")

        _promote_staging(staging, dst, replace_existing=False, reap_move_aside=True)

        assert not old.exists(), "move-aside tree outlived the promote that made dst present"
        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "new\n"

    def test_reaping_is_off_by_default(self, tmp_path: Path) -> None:
        """Reaping is opt-in because it is safe only under the destination
        lock: it cannot tell an in-flight move-aside from an abandoned one, so
        an unsynchronized writer would delete a tree another writer is
        mid-rollback on.

        Every caller holds the lock today. This pins the *default*, so a future
        caller who forgets leaks a directory rather than losing a tree — the
        failure direction is the point.
        """
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        old = self._seed_old(root, "foo")
        staging = root / ".staging-foo-111111-abc123.tmp"
        staging.mkdir()
        (staging / SKILL_MANIFEST).write_text("new\n", encoding="utf-8")

        _promote_staging(staging, dst, replace_existing=False)  # no reap_move_aside

        assert old.is_dir(), "promote reaped without being asked to"

    def test_post_promote_reap_rechecks_presence_absent(self, tmp_path: Path) -> None:
        """ "We just created ``dst``" is not "``dst`` is there now".

        The lock does not serialize editors and shells, so one can remove the
        destination between the promote's rename and this reap — and then the
        move-aside tree is once more the only copy. The precondition has to be
        evaluated where the deletion happens (Codex review).
        """
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        old = self._seed_old(root, "foo")

        _reap_move_aside(dst)  # dst never existed — stand-in for "removed in the window"

        assert old.is_dir()

    @pytest.mark.requires_symlinks
    def test_post_promote_reap_rechecks_presence_symlink(self, tmp_path: Path) -> None:
        """Same window, but the destination is replaced by a symlink rather
        than removed — which ``exists()``-style probes would accept."""
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        dst = root / "foo"
        dst.symlink_to(elsewhere, target_is_directory=True)
        old = self._seed_old(root, "foo")

        _reap_move_aside(dst)

        assert old.is_dir()

    def test_reaping_stays_scoped_to_its_own_destination(self, tmp_path: Path) -> None:
        """The post-promote reap is a second deletion site, so it inherits the
        ownership rule rather than re-deriving it: promoting ``foo`` must not
        touch ``foo-bar``'s move-aside tree."""
        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        neighbor = self._seed_old(root, "foo-bar")
        own = self._seed_old(root, "foo")
        staging = root / ".staging-foo-111111-abc123.tmp"
        staging.mkdir()
        (staging / SKILL_MANIFEST).write_text("new\n", encoding="utf-8")

        _promote_staging(staging, dst, replace_existing=False, reap_move_aside=True)

        assert not own.exists()
        assert neighbor.is_dir(), "post-promote reap crossed into another destination"

    def test_post_promote_reap_failure_does_not_fail_the_promote(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A collector that runs after the commit must not be able to report a
        committed write as an error.

        ``Path.glob`` can raise mid-iteration (ENOTDIR, EIO, EACCES), and the
        individual removals were already best-effort while the enumeration
        around them was not. Escaping here reaches
        ``pull_apply._commit_skills``'s ``except OSError`` and turns an
        installed skill into a ``write_failed`` refusal with the privacy gate's
        success unrecorded (Codex review).

        The WARNING is pinned for the same reason the keep-branch's is: a
        swallowed failure that says nothing is indistinguishable from one that
        never happened, and the new ``except`` is exactly where a later edit
        would drop the log.
        """
        import memtomem.context.skills as skills_mod

        root = tmp_path / ".memtomem/skills"
        root.mkdir(parents=True)
        dst = root / "foo"
        staging = root / ".staging-foo-111111-abc123.tmp"
        staging.mkdir()
        (staging / SKILL_MANIFEST).write_text("new\n", encoding="utf-8")

        def exploding_scan(*args: object, **kwargs: object) -> Iterator[tuple[str, Path]]:
            raise OSError(errno.EIO, "Input/output error", str(root))
            yield  # pragma: no cover — generator marker

        monkeypatch.setattr(skills_mod, "_iter_own_internal_dirs", exploding_scan)

        with caplog.at_level("WARNING", logger="memtomem.context.skills"):
            _promote_staging(staging, dst, replace_existing=False, reap_move_aside=True)

        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "new\n"
        assert any(
            "could not reap move-aside trees" in r.getMessage() and str(dst) in r.getMessage()
            for r in caplog.records
        ), "the swallowed failure left no breadcrumb"


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

    @pytest.mark.parametrize("scope", ["project_shared", "user"])
    def test_sync_still_replaces_existing_runtime_skill(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, scope: str
    ) -> None:
        if scope == "user":
            set_home(monkeypatch, tmp_path / "home")
        _seed_canonical_skill(
            tmp_path,
            name="hello",
            scope=scope,
            skill_md="---\nname: hello\n---\nnew canonical\n",
            extras={"scripts/new.txt": "new auxiliary\n"},
        )
        dst = SKILL_GENERATORS["claude_skills"].target_dir(
            tmp_path,
            "hello",
            scope=scope,  # type: ignore[arg-type]
        )
        assert dst is not None
        dst.mkdir(parents=True)
        (dst / SKILL_MANIFEST).write_text("old runtime\n", encoding="utf-8")
        (dst / "stale.txt").write_text("remove me\n", encoding="utf-8")

        result = generate_all_skills(
            tmp_path,
            runtimes=["claude_skills"],
            scope=scope,  # type: ignore[arg-type]
        )

        assert result.generated == [("claude_skills", dst)]
        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == (
            "---\nname: hello\n---\nnew canonical\n"
        )
        assert (dst / "scripts/new.txt").read_text(encoding="utf-8") == "new auxiliary\n"
        assert not (dst / "stale.txt").exists()

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


def _seed_runtime_skill(
    project_root: Path,
    runtime_dir: str = ".claude/skills",
    name: str = "foo",
    body: str = "---\nname: foo\n---\nbody\n",
) -> Path:
    """Seed a runtime-side skill for reverse-import tests."""
    skill_dir = project_root / runtime_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(body, encoding="utf-8")
    return skill_dir


class TestExtractLock:
    """#1247 id 18 — the reverse import promotes into canonical under the
    same per-destination sidecar flock the sync paths hold. Without it, two
    parallel importers could interleave their ``dst → .old-* → staging → dst``
    swaps: the racing promote raised a plain ``OSError`` (ENOTEMPTY) that
    escaped the refusal-pair catch and aborted the whole import, and a failed
    rollback stranded the only copy of the canonical tree in ``.old-*``.
    """

    def test_held_lock_skips_only_contended_destination(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A foreign holder on one canonical destination lock produces a
        typed ``lock_timeout`` skip for that skill only — the other skill
        still imports, and the call stays bounded (never blocks forever)."""
        import time as _time

        import memtomem.context.skills as skills_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        _seed_runtime_skill(tmp_path, name="foo")
        _seed_runtime_skill(tmp_path, name="bar", body="---\nname: bar\n---\nbody\n")
        canonical = canonical_skills_root(tmp_path)

        monkeypatch.setattr(skills_mod, "_SKILLS_LOCK_BUDGET_S", 0.2)
        start = _time.monotonic()
        with _file_lock(_lock_path_for(canonical / "foo")):
            result = extract_skills_to_canonical(tmp_path)
        elapsed = _time.monotonic() - start

        lock_skips = [s for s in result.skipped if s[2] == skip_codes.LOCK_TIMEOUT]
        assert len(lock_skips) == 1, result.skipped
        assert lock_skips[0][0] == "foo"
        assert "acquisition budget" in lock_skips[0][1]
        assert not (canonical / "foo").exists()
        assert [p.name for p in result.imported] == ["bar"]
        assert elapsed < 10, f"abort took {elapsed:.1f}s — budget not applied?"

    def test_lock_timeout_leaves_no_seen_mark(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Contention is transient and destination-specific, so a timed-out
        name is NOT marked ``seen``: the later runtime's copy gets its own
        (fail-fast) attempt instead of a misleading ``already pulled``
        skip. Deterministic pin: with copies in two runtimes and the lock
        held throughout, BOTH attempts surface as ``lock_timeout``."""
        import memtomem.context.skills as skills_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        _seed_runtime_skill(tmp_path, runtime_dir=".claude/skills", name="foo")
        _seed_runtime_skill(tmp_path, runtime_dir=".gemini/skills", name="foo")
        canonical = canonical_skills_root(tmp_path)

        monkeypatch.setattr(skills_mod, "_SKILLS_LOCK_BUDGET_S", 0.2)
        with _file_lock(_lock_path_for(canonical / "foo")):
            result = extract_skills_to_canonical(tmp_path)

        codes = [s[2] for s in result.skipped]
        assert codes.count(skip_codes.LOCK_TIMEOUT) == 2, result.skipped
        assert skip_codes.ALREADY_IMPORTED not in codes, result.skipped
        assert result.imported == []

    def test_promote_race_oserror_typed_skip_continues(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A racing promote's ENOTEMPTY (plain OSError — NOT the refusal
        pair) becomes a typed ``target_conflict`` skip; the remaining imports
        proceed and the orphaned staging tree is cleaned. Pre-fix this
        escaped the loop and aborted the whole import."""
        import errno as _errno

        import memtomem.context.skills as skills_mod

        _seed_runtime_skill(tmp_path, name="foo")
        _seed_runtime_skill(tmp_path, name="bar", body="---\nname: bar\n---\nbody\n")
        canonical = canonical_skills_root(tmp_path)

        orig_promote = skills_mod._promote_staging

        def racing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            if dst.name == "foo":
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(dst))
            orig_promote(staging, dst, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(skills_mod, "_promote_staging", racing_promote)
        result = extract_skills_to_canonical(tmp_path)

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert [p.name for p in result.imported] == ["bar"]
        assert not list(canonical.glob(".staging-*")), "orphaned staging tree left behind"

    def test_promote_nonrace_oserror_reraises_loud(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Non-race promote failures (permissions, ENOSPC, …) must NOT be
        demoted to a skip — they re-raise after staging cleanup."""
        import errno as _errno

        import memtomem.context.skills as skills_mod

        _seed_runtime_skill(tmp_path, name="foo")
        canonical = canonical_skills_root(tmp_path)

        def failing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            raise PermissionError(_errno.EACCES, "Permission denied", str(dst))

        monkeypatch.setattr(skills_mod, "_promote_staging", failing_promote)
        with pytest.raises(PermissionError):
            extract_skills_to_canonical(tmp_path)
        assert not list(canonical.glob(".staging-*")), "orphaned staging tree left behind"

    def test_promote_rollback_failure_chain_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The #1123 rollback-failure chain (``raise promote_exc from
        rollback_exc``) means the original tree is stranded in ``.old-*`` —
        even a race-shaped errno must stay loud, never a skip. The
        ``__cause__`` marker is the classifier's contract."""
        import errno as _errno

        import memtomem.context.skills as skills_mod

        _seed_runtime_skill(tmp_path, name="foo")

        def stranding_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            try:
                raise OSError(_errno.EACCES, "rollback rename failed")
            except OSError as rollback_exc:
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(dst)) from rollback_exc

        monkeypatch.setattr(skills_mod, "_promote_staging", stranding_promote)
        with pytest.raises(OSError) as excinfo:
            extract_skills_to_canonical(tmp_path)
        assert excinfo.value.errno == _errno.ENOTEMPTY
        assert excinfo.value.__cause__ is not None

    def test_stage_oserror_parse_error_skip_allows_runtime_fallback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable SOURCE is runtime-specific: typed ``parse_error``
        skip with no ``seen`` mark, so a clean same-name copy in a later
        runtime still imports (agents/commands parity). Pre-fix the OSError
        crashed the whole import."""
        import errno as _errno

        import memtomem.context.skills as skills_mod

        _seed_runtime_skill(tmp_path, runtime_dir=".claude/skills", name="foo")
        _seed_runtime_skill(
            tmp_path, runtime_dir=".gemini/skills", name="foo", body="---\nname: foo\n---\ngemini\n"
        )
        canonical = canonical_skills_root(tmp_path)

        orig_stage = skills_mod._stage_skill

        def failing_stage(src: Path, dst: Path, **kwargs):
            if ".claude" in src.parts:
                raise OSError(_errno.EIO, "Input/output error", str(src))
            return orig_stage(src, dst, **kwargs)

        monkeypatch.setattr(skills_mod, "_stage_skill", failing_stage)
        result = extract_skills_to_canonical(tmp_path)

        parse_skips = [s for s in result.skipped if s[2] == skip_codes.PARSE_ERROR]
        assert len(parse_skips) == 1, result.skipped
        assert parse_skips[0][0] == "foo"
        assert "unreadable" in parse_skips[0][1]
        # The gemini copy won the fallback — not an ``already pulled`` skip.
        assert [p.name for p in result.imported] == ["foo"]
        text = (canonical / "foo" / SKILL_MANIFEST).read_text(encoding="utf-8")
        assert text == "---\nname: foo\n---\ngemini\n"
        codes = [s[2] for s in result.skipped]
        assert skip_codes.ALREADY_IMPORTED not in codes, result.skipped

    def test_reaps_canonical_crash_leftovers_under_lock(self, tmp_path: Path) -> None:
        """Canonical-side ``.old-*``/``.staging-*`` crash leftovers were
        previously never reaped (reaping is lock-gated and the import path
        held no lock — only discovery filtering hid them). With the lock
        held the import now GCs them; non-internal-shaped siblings survive."""
        _seed_runtime_skill(tmp_path, name="foo")
        canonical = canonical_skills_root(tmp_path)
        stale = canonical / ".old-foo-99999-abc123.tmp"
        stale.mkdir(parents=True)
        (stale / SKILL_MANIFEST).write_text("stale", encoding="utf-8")
        user_dir = canonical / ".old-foo-notes"
        user_dir.mkdir(parents=True)
        (user_dir / "keep.txt").write_text("keep", encoding="utf-8")

        result = extract_skills_to_canonical(tmp_path)

        assert [p.name for p in result.imported] == ["foo"]
        assert not stale.exists(), "crash leftover not reaped"
        assert (user_dir / "keep.txt").read_text(encoding="utf-8") == "keep"

    def test_overwrite_false_recheck_under_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A parallel importer landing ``dst`` between the lock-free
        preflight and our lock acquisition must NOT be silently replaced
        when ``overwrite=False`` — the contract is re-checked under the
        lock. Simulated by planting dst from the Gate A hook (which runs
        after the preflight, before the lock)."""
        import memtomem.context.skills as skills_mod

        _seed_runtime_skill(tmp_path, name="foo")
        canonical = canonical_skills_root(tmp_path)
        racer_dst = canonical / "foo"

        orig_gate = skills_mod.apply_gate_a

        def planting_gate(**kwargs):
            out = orig_gate(**kwargs)
            if not racer_dst.exists():
                racer_dst.mkdir(parents=True)
                (racer_dst / SKILL_MANIFEST).write_text("racer won\n", encoding="utf-8")
            return out

        monkeypatch.setattr(skills_mod, "apply_gate_a", planting_gate)
        result = extract_skills_to_canonical(tmp_path)

        exists_skips = [s for s in result.skipped if s[2] == skip_codes.CANONICAL_EXISTS]
        assert len(exists_skips) == 1, result.skipped
        assert exists_skips[0][0] == "foo"
        assert result.imported == []
        text = (racer_dst / SKILL_MANIFEST).read_text(encoding="utf-8")
        assert text == "racer won\n", "racing importer's tree was replaced despite overwrite=False"

    @pytest.mark.parametrize("overwrite", [False, True])
    def test_new_import_no_replace_preserves_skill_created_during_staging(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        overwrite: bool,
    ) -> None:
        """A valid skill created after the under-lock re-check wins the race.

        ``overwrite=True`` is included because #1838 still classifies an absent
        destination as a new import; it must not turn the final promote into a
        replace operation merely because the flag was supplied (#1839).
        """
        import memtomem.context.skills as skills_mod

        _seed_runtime_skill(tmp_path, name="foo", body="runtime candidate\n")
        canonical = canonical_skills_root(tmp_path)
        racer_dst = canonical / "foo"
        staged = Event()
        release_promote = Event()
        orig_stage = skills_mod._stage_skill

        def pausing_stage(src: Path, dst: Path, **kwargs) -> Path:
            staging = orig_stage(src, dst, **kwargs)
            staged.set()
            if not release_promote.wait(timeout=5):
                raise AssertionError("test did not release the paused skill promote")
            return staging

        monkeypatch.setattr(skills_mod, "_stage_skill", pausing_stage)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                extract_skills_to_canonical,
                tmp_path,
                overwrite,
            )
            try:
                assert staged.wait(timeout=5), "import never reached the staging barrier"
                racer_dst.mkdir(parents=True)
                (racer_dst / SKILL_MANIFEST).write_text("external writer won\n", encoding="utf-8")
                (racer_dst / "sentinel.txt").write_text("preserve me\n", encoding="utf-8")
            finally:
                release_promote.set()
            result = future.result(timeout=5)

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        assert result.imported == []
        assert (racer_dst / SKILL_MANIFEST).read_text(encoding="utf-8") == ("external writer won\n")
        assert (racer_dst / "sentinel.txt").read_text(encoding="utf-8") == "preserve me\n"
        assert not list(canonical.glob(".staging-foo-*.tmp"))
        assert not list(canonical.glob(".old-foo-*.tmp"))

    def test_promote_runs_under_destination_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Direct pin of the serialization property: at promote time the
        destination sidecar lock is HELD (a non-blocking acquisition from
        a second open-file-description fails)."""
        import memtomem.context.skills as skills_mod
        from memtomem.context._atomic import _file_lock, _lock_path_for

        _seed_runtime_skill(tmp_path, name="foo")
        contended: list[bool] = []

        orig_promote = skills_mod._promote_staging

        def probing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            try:
                with _file_lock(_lock_path_for(dst), timeout=0):
                    contended.append(False)
            except TimeoutError:
                contended.append(True)
            orig_promote(staging, dst, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(skills_mod, "_promote_staging", probing_promote)
        result = extract_skills_to_canonical(tmp_path)

        assert [p.name for p in result.imported] == ["foo"]
        assert contended == [True], "promote ran without the destination sidecar lock held"

    def test_dry_run_takes_no_lock_and_mutates_nothing(self, tmp_path: Path) -> None:
        """rank-10 preview contract: dry_run must not create the canonical
        root, the sidecar lockfile, or any staging artifact — the lock is
        a disk mutation too."""
        _seed_runtime_skill(tmp_path, name="foo")
        canonical = canonical_skills_root(tmp_path)

        result = extract_skills_to_canonical(tmp_path, dry_run=True)

        assert [p.name for p in result.imported] == ["foo"]
        assert not canonical.exists(), "dry_run touched disk"


class TestExtractGateAFailClosed:
    """The reverse-import Gate A must FAIL CLOSED on a source file it cannot
    READ. The old ``except OSError: continue`` skipped just that file's scan
    and copied the whole skill anyway, so a file transiently unreadable at
    scan time but readable by copy time promoted into the canonical UNSCANNED
    — into ``project_shared`` (git history is forever). The sync side
    (``scan_artifact_tree`` → ``PrivacyScanReadError``) and the agents/commands
    importers already fail closed on the same OSError; this pins parity.
    """

    @staticmethod
    def _patch_secret_unreadable(monkeypatch: pytest.MonkeyPatch) -> None:
        import errno as _errno
        import pathlib

        orig_read_text = pathlib.Path.read_text

        def failing_read_text(self: Path, *args: object, **kwargs: object) -> str:
            # Only the would-be secret file is unreadable at scan time; its
            # bytes (read via read_bytes during the copy) are NOT patched, so
            # pre-fix the unscanned secret would still copy into canonical.
            if self.name == "secret.py":
                raise OSError(_errno.EIO, "Input/output error", str(self))
            return orig_read_text(self, *args, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(pathlib.Path, "read_text", failing_read_text)

    def test_unreadable_source_at_scan_does_not_promote_unscanned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable-at-scan source file aborts the whole skill (typed
        ``parse_error`` skip) and nothing lands in the ``project_shared``
        canonical — the unscanned secret never reaches git-tracked storage."""
        skill_dir = _seed_runtime_skill(tmp_path, name="foo")
        secret_file = skill_dir / "scripts" / "secret.py"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(f"TOKEN = {SECRET!r}\n", encoding="utf-8")
        canonical = canonical_skills_root(tmp_path)

        self._patch_secret_unreadable(monkeypatch)
        result = extract_skills_to_canonical(tmp_path)  # default scope=project_shared

        assert [p.name for p in result.imported] == []
        assert not (canonical / "foo").exists(), "unscanned skill promoted — Gate A bypass"
        parse_skips = [s for s in result.skipped if s[2] == skip_codes.PARSE_ERROR]
        assert len(parse_skips) == 1, result.skipped
        assert parse_skips[0][0] == "foo"
        assert parse_skips[0][1].startswith("unreadable:")

    def test_unreadable_source_at_scan_surfaces_in_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The scan runs in dry-run too, so the fail-closed skip shows in the
        preview — preview and apply agree (pre-fix the OSError only surfaced
        at real-run copy time, so the preview wrongly looked importable)."""
        skill_dir = _seed_runtime_skill(tmp_path, name="foo")
        secret_file = skill_dir / "scripts" / "secret.py"
        secret_file.parent.mkdir(parents=True, exist_ok=True)
        secret_file.write_text(f"TOKEN = {SECRET!r}\n", encoding="utf-8")

        self._patch_secret_unreadable(monkeypatch)
        result = extract_skills_to_canonical(tmp_path, dry_run=True)

        assert [p.name for p in result.imported] == []
        parse_skips = [s for s in result.skipped if s[2] == skip_codes.PARSE_ERROR]
        assert len(parse_skips) == 1 and parse_skips[0][0] == "foo", result.skipped

    def test_unreadable_source_at_scan_leaves_no_seen_mark(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Source-runtime-specific: the unreadable claude copy fails closed
        without marking the name ``seen``, so the readable+clean gemini copy
        of the same skill still imports (agents/commands fallback parity)."""
        claude = _seed_runtime_skill(tmp_path, runtime_dir=".claude/skills", name="foo")
        (claude / "scripts").mkdir(parents=True, exist_ok=True)
        (claude / "scripts" / "secret.py").write_text(f"TOKEN = {SECRET!r}\n", encoding="utf-8")
        _seed_runtime_skill(
            tmp_path,
            runtime_dir=".gemini/skills",
            name="foo",
            body="---\nname: foo\n---\ngemini clean\n",
        )
        canonical = canonical_skills_root(tmp_path)

        self._patch_secret_unreadable(monkeypatch)
        result = extract_skills_to_canonical(tmp_path)

        assert [p.name for p in result.imported] == ["foo"]
        text = (canonical / "foo" / SKILL_MANIFEST).read_text(encoding="utf-8")
        assert text == "---\nname: foo\n---\ngemini clean\n"
        parse_skips = [s for s in result.skipped if s[2] == skip_codes.PARSE_ERROR]
        assert len(parse_skips) == 1 and parse_skips[0][0] == "foo", result.skipped
        assert skip_codes.ALREADY_IMPORTED not in [s[2] for s in result.skipped], result.skipped


class TestScannableSkillFilesWalker:
    """``_iter_scannable_skill_files`` replaces ``Path.rglob`` for the Gate A
    enumeration. ``rglob`` SUPPRESSED a per-directory ``OSError`` (an
    unreadable subtree silently vanished from the scan while
    ``copy_tree_atomic`` re-walked and still copied it — a Gate A bypass) and
    did not apply the copier's skip rules. The walker must fail LOUD on an
    enumeration error and mirror the copier's exact surface.
    """

    def test_propagates_oserror_from_unreadable_subdir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unreadable subtree raises out of the walker (the caller then
        fails closed) — it is NOT silently dropped the way ``rglob`` did."""
        import errno as _errno
        import pathlib

        root = tmp_path / "skill"
        (root / "scripts").mkdir(parents=True)
        (root / SKILL_MANIFEST).write_text("---\nname: x\n---\n", encoding="utf-8")
        (root / "scripts" / "run.sh").write_text("echo hi\n", encoding="utf-8")

        orig_iterdir = pathlib.Path.iterdir

        def failing_iterdir(self: Path):
            if self.name == "scripts":
                raise OSError(_errno.EACCES, "Permission denied", str(self))
            return orig_iterdir(self)

        monkeypatch.setattr(pathlib.Path, "iterdir", failing_iterdir)
        with pytest.raises(OSError):
            list(_iter_scannable_skill_files(root))

    def test_mirrors_copier_surface_includes_bak_excludes_skip_names(self, tmp_path: Path) -> None:
        """Scans exactly what ``_stage_skill``'s copy mirrors: ``.bak`` IS
        included (the copy uses an empty ``skip_suffixes``, so dropping it
        like ``iter_installed_files`` would leave a ``secret.bak``
        unscanned-but-copied); ``COPY_SKIP_NAMES`` dirs are excluded."""
        root = tmp_path / "skill"
        (root / "scripts").mkdir(parents=True)
        (root / "__pycache__").mkdir()
        (root / SKILL_MANIFEST).write_text("a", encoding="utf-8")
        (root / "secret.bak").write_text("b", encoding="utf-8")
        (root / "scripts" / "run.sh").write_text("c", encoding="utf-8")
        (root / "__pycache__" / "x.pyc").write_text("d", encoding="utf-8")

        rels = sorted(p.relative_to(root).as_posix() for p in _iter_scannable_skill_files(root))
        assert rels == [SKILL_MANIFEST, "scripts/run.sh", "secret.bak"]


class TestSyncPromoteRaceClassification:
    """#1247 id 18 same-shape sweep: the sync promote catches were limited to
    the refusal pair, so a NON-gateway writer's mid-swap race (ENOTEMPTY)
    crashed the fan-out mid-batch — same isolation break #1229 fixed for the
    refusal types. Verified race shapes now convert to typed skips at all
    three promote sites; everything else (ENOSPC, permissions, the #1123
    rollback-failure chain) still re-raises."""

    def test_project_shared_race_oserror_typed_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno as _errno

        import memtomem.context.skills as skills_mod

        _seed_canonical_skill(tmp_path, name="foo")
        _seed_canonical_skill(tmp_path, name="bar", skill_md="---\nname: bar\n---\nbody\n")

        orig_promote = skills_mod._promote_staging

        def racing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            if dst.name == "foo":
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(dst))
            orig_promote(staging, dst, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(skills_mod, "_promote_staging", racing_promote)
        result = generate_all_skills(tmp_path, runtimes=["claude_skills"])

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        generated_names = {p.name for _rt, p in result.generated}
        assert generated_names == {"bar"}, result.generated

    def test_project_shared_nonrace_oserror_reraises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno as _errno

        import memtomem.context.skills as skills_mod

        _seed_canonical_skill(tmp_path, name="foo")

        def failing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            raise PermissionError(_errno.EACCES, "Permission denied", str(dst))

        monkeypatch.setattr(skills_mod, "_promote_staging", failing_promote)
        with pytest.raises(PermissionError):
            generate_all_skills(tmp_path, runtimes=["claude_skills"])

    def test_user_scope_race_oserror_typed_skip(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import errno as _errno

        import memtomem.context.skills as skills_mod

        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        _seed_canonical_skill(tmp_path, name="foo", scope="user")
        claude_dst = SKILL_GENERATORS["claude_skills"].target_dir(tmp_path, "foo", scope="user")
        assert claude_dst is not None

        orig_promote = skills_mod._promote_staging

        def racing_promote(staging: Path, dst: Path, **kwargs: object) -> None:
            if dst == claude_dst:
                raise OSError(_errno.ENOTEMPTY, "Directory not empty", str(dst))
            orig_promote(staging, dst, **kwargs)  # type: ignore[arg-type]

        monkeypatch.setattr(skills_mod, "_promote_staging", racing_promote)
        result = generate_all_skills(tmp_path, scope="user")  # must not raise

        conflicts = [s for s in result.skipped if s[2] == skip_codes.TARGET_CONFLICT]
        assert len(conflicts) == 1, result.skipped
        assert conflicts[0][0] == "foo"
        promoted = {rt for rt, _p in result.generated}
        assert "claude_skills" not in promoted
        assert promoted, result.skipped
