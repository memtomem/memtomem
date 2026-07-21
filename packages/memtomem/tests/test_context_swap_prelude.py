"""The swap-recovery prelude — ADR-0030 §10 / PR-G4a-3.

PR-G4a-2 shipped ``context/_dir_swap.py`` with no caller and stated the
ordering contract in its module docstring: under C0, ``recover_pending_swaps``
runs FIRST, and only then may anything reap crash leftovers, skipping any
transient a live marker still claims.

**Prose cannot fail CI.** These tests drive the recovery rows through the
*real* ``skills._recover_and_reap_internal_dirs`` — not a stand-in — because
until something calls it the contract exists only on paper. The regression
being pinned by the row-4 case is specific and permanent: deleting the claimed
staging tree turns the fail-closed "all three present" row into the
"``dst`` + ``old``" row, whose recovery then deletes ``old`` — the only copy of
the artifact.

Assertions are about **convergence**, never merely "the original survived": a
fail-closed row 4 also leaves the original in place, so survival alone cannot
distinguish a working prelude from a broken one.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._dir_swap import SwapForeignDestination, SwapRecoveryError
from memtomem.context._names import InvalidNameError
from memtomem.context.skills import (
    SKILL_MANIFEST,
    _iter_own_internal_dirs,
    _reap_move_aside,
    _recover_and_reap_internal_dirs,
)

SUFFIX = "999999-abc123"


def _tree(path: Path, content: str) -> Path:
    """A minimal skill tree whose bytes identify which copy survived."""
    path.mkdir(parents=True)
    (path / SKILL_MANIFEST).write_text(content, encoding="utf-8")
    return path


def _paths(root: Path, name: str = "skill", suffix: str = SUFFIX) -> dict[str, Path]:
    return {
        "dst": root / name,
        "old": root / f".old-{name}-{suffix}.tmp",
        "staging": root / f".staging-{name}-{suffix}.tmp",
        "marker": root / f".swap-{name}-{suffix}.json",
    }


def _write_marker(root: Path, name: str = "skill", suffix: str = SUFFIX) -> dict[str, Path]:
    """Write a well-formed marker for ``(name, suffix)`` and return its paths.

    Deliberately hand-rolled rather than produced by ``swap_dir_tree``: these
    states are what a CRASH leaves behind, and no successful forward path can
    construct them.
    """
    p = _paths(root, name, suffix)
    p["marker"].write_text(
        json.dumps(
            {
                "version": 1,
                "name": name,
                "suffix": suffix,
                "dst": p["dst"].name,
                "old": p["old"].name,
                "staging": p["staging"].name,
                "created_at": "2026-07-21T00:00:00Z",
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return p


def _residue(root: Path) -> list[str]:
    return sorted(
        e.name for e in root.iterdir() if e.name.startswith((".swap-", ".staging-", ".old-"))
    )


@pytest.fixture
def store(tmp_path: Path) -> Path:
    root = tmp_path / ".memtomem" / "skills"
    root.mkdir(parents=True)
    return root


class TestPreludeRecoversBeforeReaping:
    """The recovery rows, driven through the real prelude."""

    def test_row_2_converges_forward(self, store: Path) -> None:
        """Crashed between the renames: ``dst`` absent, ``old`` holds the
        pre-image, ``staging`` holds the complete replacement.

        The marker is written only after ``staging`` is complete, so
        marker-present implies staging-complete and forwarding loses nothing a
        rollback would save. Convergence, not survival: the artifact IS the
        staging tree afterwards and no transient is left behind.
        """
        p = _write_marker(store)
        _tree(p["old"], "original")
        _tree(p["staging"], "replacement")

        _recover_and_reap_internal_dirs(p["dst"])

        assert p["dst"].is_dir()
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "replacement"
        assert _residue(store) == [], "recovery left transients behind"

    def test_row_5_converges_backward(self, store: Path) -> None:
        """``dst`` and ``staging`` both gone: restore the known pre-image.

        Included alongside row 2 because it is the state a *gap* writer (one
        that takes C0 without running this prelude) would otherwise turn into
        data loss by materializing ``dst`` — recovery would then read the
        "``dst`` + ``old``" row and delete ``old``.
        """
        p = _write_marker(store)
        _tree(p["old"], "original")

        _recover_and_reap_internal_dirs(p["dst"])

        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "original"
        assert _residue(store) == []

    def test_row_4_fails_closed_and_stays_row_4(self, store: Path) -> None:
        """All three present: provenance is genuinely ambiguous, so nothing is
        touched — and it must STILL be row 4 afterwards.

        This is the regression the whole prelude ordering exists for. A reap
        that deleted the claimed ``staging`` would collapse this into the
        "``dst`` + ``old``" row, whose recovery deletes ``old``. Re-running the
        prelude therefore has to reproduce the same refusal, not a different
        one.
        """
        p = _write_marker(store)
        _tree(p["dst"], "candidate-a")
        _tree(p["old"], "candidate-b")
        _tree(p["staging"], "replacement")
        before = _residue(store)

        with pytest.raises(SwapForeignDestination) as first:
            _recover_and_reap_internal_dirs(p["dst"])

        assert _residue(store) == before, "a fail-closed row must delete nothing"
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert (p["old"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-b"
        # Both paths named, neither claimed authoritative — asserting one would
        # talk an operator into deleting the good tree.
        assert str(p["dst"]) in str(first.value)
        assert str(p["old"]) in str(first.value)

        with pytest.raises(SwapForeignDestination):
            _recover_and_reap_internal_dirs(p["dst"])
        assert _residue(store) == before

    def test_recovery_runs_before_the_reap(self, store: Path) -> None:
        """Row 1 (``dst`` + marked ``staging``): the staging tree is the one
        recovery drops, and ``dst`` — the ORIGINAL — is untouched.

        If the reap ran first it would refuse the marked staging tree and leave
        the marker behind, so a surviving marker is the tell.
        """
        p = _write_marker(store)
        _tree(p["dst"], "original")
        _tree(p["staging"], "abandoned")

        _recover_and_reap_internal_dirs(p["dst"])

        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "original"
        assert _residue(store) == []


class TestMarkerOwnedTransientsAreNobodysToDelete:
    """§4.1 at the one enumeration both reaping sites go through."""

    def test_enumeration_skips_a_claimed_transient(self, store: Path) -> None:
        """A ``.staging-*``/``.old-*`` whose suffix a live marker claims is
        never yielded to a reaper, while an unmarked sibling still is.

        Both kinds, deliberately: the ``.old-*`` half is the dangerous one — it
        holds the pre-image — and a staging-only predicate would have covered
        the cheap tree and missed the expensive one.
        """
        p = _write_marker(store)
        _tree(p["dst"], "canonical")
        _tree(p["old"], "pre-image")
        _tree(p["staging"], "replacement")
        unmarked_old = _tree(store / ".old-skill-111111-def456.tmp", "debris")
        unmarked_staging = _tree(store / ".staging-skill-111111-def456.tmp", "debris")

        reachable = {path for _kind, path in _iter_own_internal_dirs(p["dst"])}

        assert reachable == {unmarked_old, unmarked_staging}

    def test_post_promote_reaper_keeps_a_claimed_move_aside(self, store: Path) -> None:
        """``_reap_move_aside`` runs after a promote and never recovers, so the
        marker filter is the only thing standing between it and a pre-image.

        Driven end-to-end rather than through the enumeration so the wiring —
        not just the predicate — is pinned.
        """
        p = _write_marker(store)
        _tree(p["dst"], "canonical")
        _tree(p["old"], "pre-image")
        unmarked = _tree(store / ".old-skill-111111-def456.tmp", "debris")

        _reap_move_aside(p["dst"])

        assert p["old"].is_dir(), "a marker-owned pre-image was reaped"
        assert not unmarked.exists(), "unmarked debris should still be collected"


class TestPreludeNameContract:
    def test_prelude_rejects_a_name_production_cannot_produce(self, store: Path) -> None:
        """``InvalidNameError`` (a ``ValueError``, NOT an ``OSError``)
        propagates instead of degrading into a per-item skip.

        Every production call site derives ``dst`` from an already-validated
        artifact — ``list_canonical_skills`` and the reverse-import discovery
        both *skip* non-conforming directory entries with a warning — so an
        invalid name reaching here is a programming error and must crash
        loudly. Pinned so a future caller that iterates raw directory entries
        has to make that choice explicitly rather than inherit this one.

        Nothing is created on disk, which is the stronger assertion anyway:
        validation has to happen BEFORE any filesystem work, since its whole
        job is to stop a bad name from being joined onto a root. It also has to
        be this way to run on Windows, where ``*`` is not a legal filename
        character at all — an earlier version of this test called ``mkdir`` and
        died with ``WinError 123`` before reaching the assertion.
        """
        with pytest.raises(InvalidNameError):
            _recover_and_reap_internal_dirs(store / "foo*")

    def test_a_clean_destination_is_a_no_op(self, store: Path) -> None:
        """No marker, no transients: the prelude neither raises nor writes."""
        dst = _tree(store / "skill", "canonical")

        _recover_and_reap_internal_dirs(dst)

        assert _residue(store) == []
        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "canonical"


class TestPullSurfacesTheRefusal:
    """A wedged canonical must reach the Pull surfaces as its own status."""

    def test_commit_pull_returns_swap_recovery_pending(self, store: Path) -> None:
        """Not ``target_conflict`` (nothing the user put there caused it) and
        not ``write_failed`` (nothing was written, and a 500 would report an
        infrastructure fault for a state that needs an operator)."""
        from memtomem.context.pull_apply import PullPlan, commit_pull

        p = _write_marker(store)
        _tree(p["dst"], "candidate-a")
        _tree(p["old"], "candidate-b")
        _tree(p["staging"], "replacement")
        project_root = store.parent.parent

        plan = PullPlan(
            kind="skills",
            name="skill",
            scope="project_shared",
            project_root=project_root,
            selected_runtime="claude",
            captured=(("SKILL.md", b"new\n"),),
            overwrite=False,
            store_present=False,
            expected_store_digest=None,
            duplicate_runtimes=(),
            gate=None,
            surface="test",
        )
        result = commit_pull(plan, lock_timeout=5.0)

        assert result.status == "swap_recovery_pending"
        assert result.reason_code == skip_codes.SWAP_RECOVERY_PENDING
        # Nothing written, nothing deleted.
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert p["old"].is_dir() and p["staging"].is_dir()

    @pytest.mark.parametrize("bad", ["../other/x", "a/b", "..", ""])
    def test_the_engine_validates_its_own_name(self, tmp_path: Path, bad: str) -> None:
        """``prepare_pull`` / ``commit_pull` build ``canonical_root / name``.

        A separator-carrying name yields a perfectly ordinary *basename* while
        pointing the parent somewhere else, so a downstream basename check
        cannot catch it — and the commit path now renames and deletes trees.
        Every shipping surface validates, but a defense that holds only while
        every caller remembers is not a defense for that.
        """
        from memtomem.context.pull_apply import PullPlan, commit_pull, prepare_pull

        with pytest.raises(InvalidNameError):
            prepare_pull("skills", bad, scope="project_shared", project_root=tmp_path)

        plan = PullPlan(
            kind="skills",
            name=bad,
            scope="project_shared",
            project_root=tmp_path,
            selected_runtime="claude",
            captured=(("SKILL.md", b"new\n"),),
            overwrite=False,
            store_present=False,
            expected_store_digest=None,
            duplicate_runtimes=(),
            gate=None,
            surface="test",
        )
        with pytest.raises(InvalidNameError):
            commit_pull(plan, lock_timeout=5.0)


class TestStagingCollisionRespectsTheMarker:
    def test_stage_skill_refuses_a_marker_owned_staging_path(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``_stage_skill`` used to `rmtree` a colliding staging path on the
        reasoning "the leftover tree is from us".

        §4.1 retires that inference: the directory swap uses the same
        ``.staging-<name>-<pid>-<hex>.tmp`` grammar, so a collision can name a
        transient a live marker still claims — and deleting one is exactly the
        collapse the prelude exists to prevent. Reaching it needs pid reuse AND
        a 3-byte hex collision, so the suffix is forced rather than waited for;
        the guard is what is under test, not the odds.
        """
        from memtomem.context import skills as skills_mod

        p = _write_marker(store)
        _tree(p["staging"], "claimed")
        src = _tree(store.parent / "src", "source")
        pid, hexpart = SUFFIX.split("-")
        monkeypatch.setattr(skills_mod.os, "getpid", lambda: int(pid))
        monkeypatch.setattr(skills_mod.secrets, "token_hex", lambda _n: hexpart)

        with pytest.raises(SwapRecoveryError):
            skills_mod._stage_skill(src, p["dst"])

        assert (p["staging"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "claimed"
        assert p["marker"].is_file()

    def test_an_unclaimed_collision_is_still_cleared(
        self, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The guard must not turn ordinary crash debris into a hard failure —
        an unmarked leftover at the same path is still ours to remove.
        """
        from memtomem.context import skills as skills_mod

        p = _paths(store)
        _tree(p["staging"], "debris")
        src = _tree(store.parent / "src", "source")
        pid, hexpart = SUFFIX.split("-")
        monkeypatch.setattr(skills_mod.os, "getpid", lambda: int(pid))
        monkeypatch.setattr(skills_mod.secrets, "token_hex", lambda _n: hexpart)

        staging = skills_mod._stage_skill(src, p["dst"])

        assert staging == p["staging"]
        assert (staging / SKILL_MANIFEST).read_text(encoding="utf-8") == "source"
