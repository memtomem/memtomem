"""The skills engines stop working once their caller has given up (#2247).

`generate_all_skills` and `extract_skills_to_canonical` run in
`asyncio.to_thread` under a 60s route timeout, and a thread cannot be
cancelled: before this, a timed-out request returned 503 and the worker fanned
skills out (or imported them) anyway. The engines now poll an abort flag.

Where they poll is the contract these tests pin, and it differs by scope:

* ``project_shared`` is the all-or-nothing batch (#1229). Its last checkpoint
  is *before* the promote phase, never inside it, so an abandoned batch
  promotes every destination or none.
* Other scopes fan out one destination at a time, so the item is the boundary.
  Destinations promoted before the caller gave up stay promoted and reported —
  the abort suppresses, it does not roll back.
* Both check again *inside* an item, just before its promote, because the lock
  wait, recovery, staging, and the privacy scan all happen after the
  between-items check and a caller can give up during any of them.

Everything is driven directly through the flag, so no thread or cancelled task
is involved.
"""

from __future__ import annotations

import pytest

from memtomem.context import _skip_reasons as skip_codes
from memtomem.context import skills as skills_mod
from memtomem.context._abandon import abandon_sync_on_exit
from .helpers import set_home
from memtomem.context.skills import (
    CANONICAL_SKILL_ROOT,
    extract_skills_to_canonical,
    generate_all_skills,
)

SKILL_MD = """---
name: {name}
description: A skill.
---

Body.
"""


def _canonical(project_root, name):
    skill = project_root / CANONICAL_SKILL_ROOT / name
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(SKILL_MD.format(name=name), encoding="utf-8")
    return skill


def _abandoned_rows(result):
    return [row for row in result.skipped if row[2] == skip_codes.ABANDONED]


@pytest.fixture
def project(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    _canonical(root, "alpha")
    _canonical(root, "beta")
    return root


def _lock_sidecars(root):
    """Destination lock sidecars anywhere under *root* — acquiring one creates
    a file nothing ever removes, so their absence is what distinguishes a check
    that ran *before* a lock from one that ran after it."""
    return sorted(str(p) for p in root.rglob(".*.lock"))


def _fanned_out(project_root):
    """Every runtime destination that currently holds a skill tree."""
    return sorted(
        str(dst)
        for gen in skills_mod.SKILL_GENERATORS.values()
        for name in ("alpha", "beta")
        if (dst := gen.target_dir(project_root, name, scope="project_shared")) is not None
        and dst.exists()
    )


class TestGenerateProjectShared:
    """The all-or-nothing batch."""

    def test_a_sync_abandoned_before_it_starts_writes_nothing(self, project):
        with abandon_sync_on_exit() as abandoned:
            abandoned.set()
            result = generate_all_skills(project, scope="project_shared")

        assert result.generated == []
        assert [row[2] for row in result.skipped] == [skip_codes.ABANDONED]
        assert result.skipped[0][0] == "<all>"
        assert "abandoned by its caller" in result.skipped[0][1]
        assert _fanned_out(project) == []
        # The entry check, distinguished from the two later ones: they would
        # also leave the fan-out empty, but only stopping before any lock is
        # taken leaves no sidecar — and a sidecar is never removed.
        assert _lock_sidecars(project) == [], (
            "an abandoned sync locked destinations before noticing — the entry check did not fire"
        )

    def test_abandonment_during_staging_promotes_nothing(self, project, monkeypatch):
        """The pre-promote checkpoint, and the all-or-nothing contract with it.

        Staging and scanning the whole batch is long enough for a caller to
        give up. Because this scope promotes as one phase, the only correct
        outcome is that *no* destination is written — a partial fan-out is
        exactly what #1229 refuses.
        """
        real_stage = skills_mod._stage_skill

        with abandon_sync_on_exit() as abandoned:

            def _stage_then_abandon(*args, **kwargs):
                staging = real_stage(*args, **kwargs)
                abandoned.set()
                return staging

            monkeypatch.setattr(skills_mod, "_stage_skill", _stage_then_abandon)
            result = generate_all_skills(project, scope="project_shared")

        assert result.generated == [], "an abandoned batch promoted part of its fan-out"
        assert _abandoned_rows(result), result.skipped
        assert _fanned_out(project) == []

    def test_abandonment_during_recovery_stops_before_staging(self, project, monkeypatch):
        """The post-recovery checkpoint, distinguished from the pre-promote one.

        The pre-promote check would also promote nothing, so the outcome alone
        cannot tell them apart. What only this one gives is that the batch never
        starts staging — copying every skill tree for a caller that is already
        gone is pure waste, and each staging tree is a directory that has to be
        reaped afterwards.
        """
        real_recover = skills_mod._recover_and_reap_internal_dirs
        stages: list = []

        with abandon_sync_on_exit() as abandoned:

            def _recover_then_abandon(dst):
                out = real_recover(dst)
                abandoned.set()
                return out

            monkeypatch.setattr(
                skills_mod, "_recover_and_reap_internal_dirs", _recover_then_abandon
            )

            def _record_stage(*args, **kwargs):
                stages.append(args)
                raise AssertionError("staging must not start for an abandoned batch")

            monkeypatch.setattr(skills_mod, "_stage_skill", _record_stage)
            result = generate_all_skills(project, scope="project_shared")

        assert stages == [], (
            "the batch staged trees after its caller gave up during recovery — "
            "the post-recovery check did not fire"
        )
        assert result.generated == []
        assert _abandoned_rows(result), result.skipped

    def test_no_staging_trees_are_left_behind(self, project, monkeypatch):
        """The abort returns from inside the ``try``, so the reaper still runs."""
        real_stage = skills_mod._stage_skill

        with abandon_sync_on_exit() as abandoned:

            def _stage_then_abandon(*args, **kwargs):
                staging = real_stage(*args, **kwargs)
                abandoned.set()
                return staging

            monkeypatch.setattr(skills_mod, "_stage_skill", _stage_then_abandon)
            generate_all_skills(project, scope="project_shared")

        leftovers = [p for p in project.rglob("*") if p.is_dir() and ".mm-" in p.name]
        assert leftovers == [], f"abandoned batch left staging trees: {leftovers}"

    def test_a_sync_nobody_abandoned_fans_out_normally(self, project):
        result = generate_all_skills(project, scope="project_shared")

        assert result.generated, result
        assert not _abandoned_rows(result)
        assert _fanned_out(project) != []


class TestGeneratePerDestination:
    """Non-shared scopes: the item is the boundary, not the run.

    ``user`` is the only non-shared scope with a runtime fan-out
    (``project_local`` has none in ``RUNTIME_FANOUT_TABLE``), so these run
    under a redirected ``$HOME``. That redirect is load-bearing, not cosmetic:
    without it the engine would resolve the developer's real ``~/.claude`` —
    the #1903 home-guard failure, and the reason skills' worker-side
    ``expanduser()`` is tracked as its own #2211-shaped bug.
    """

    @pytest.fixture
    def user_project(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        root = tmp_path / "user-proj"
        root.mkdir()
        for name in ("alpha", "beta"):
            skill = home / ".memtomem" / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(SKILL_MD.format(name=name), encoding="utf-8")
        return root

    def test_destinations_written_before_the_abandonment_stay_written(
        self, user_project, monkeypatch
    ):
        """Cooperative, not transactional — and the remaining work is declined.

        The 503 already told the caller the sync failed, so what matters is
        that the engine stops, not that it undoes what it finished.
        """
        real_promote = skills_mod._promote_staging
        promotes: list = []

        with abandon_sync_on_exit() as abandoned:

            def _promote_then_abandon(staging, dst, **kwargs):
                real_promote(staging, dst, **kwargs)
                promotes.append(dst)
                abandoned.set()

            monkeypatch.setattr(skills_mod, "_promote_staging", _promote_then_abandon)
            result = generate_all_skills(user_project, scope="user")

        assert len(promotes) == 1, f"the engine kept promoting after the abort: {promotes}"
        assert len(result.generated) == 1, result.generated
        rows = _abandoned_rows(result)
        assert rows, result.skipped
        assert rows[0][0] == "<remaining>"
        assert "the remaining items were not written" in rows[0][1]

        # Which check stopped it matters. The in-item check would also have
        # produced the row above, but only after acquiring the next
        # destination's lock — and acquiring one creates a sidecar that is
        # never removed. An abandoned sync must leave no trace at a
        # destination it never touched, which only the pre-lock check gives.
        promoted = promotes[0]
        untouched = [
            dst
            for gen in skills_mod.SKILL_GENERATORS.values()
            for name in ("alpha", "beta")
            if (dst := gen.target_dir(user_project, name, scope="user")) is not None
            and dst != promoted
        ]
        assert untouched, "fixture should leave more than one destination"
        strays = [
            skills_mod._lock_path_for(dst)
            for dst in untouched
            if skills_mod._lock_path_for(dst).exists()
        ]
        assert strays == [], (
            f"an abandoned sync locked destinations it never wrote: {strays} — "
            "the pre-lock check did not fire"
        )

    def test_abandonment_inside_an_item_stops_before_its_promote(self, user_project, monkeypatch):
        """The in-item checkpoint: the scan is after the between-items check.

        Setting the flag during the privacy scan lands past the pre-lock check
        for that destination, so only the check just before the promote can
        stop it.
        """
        real_scan = skills_mod.scan_artifact_tree
        promotes: list = []

        with abandon_sync_on_exit() as abandoned:

            def _scan_then_abandon(*args, **kwargs):
                scan = real_scan(*args, **kwargs)
                abandoned.set()
                return scan

            monkeypatch.setattr(skills_mod, "scan_artifact_tree", _scan_then_abandon)
            monkeypatch.setattr(
                skills_mod,
                "_promote_staging",
                lambda *a, **k: promotes.append(a[1]),
            )
            result = generate_all_skills(user_project, scope="user")

        assert promotes == [], (
            "the engine promoted after its caller gave up during the scan — "
            "the in-item check did not fire"
        )
        assert result.generated == []
        assert _abandoned_rows(result), result.skipped


class TestExtract:
    """Import: per-skill boundary, same two checkpoints."""

    @pytest.fixture
    def runtime_project(self, tmp_path):
        """A project whose Claude runtime dir holds two importable skills."""
        root = tmp_path / "import-proj"
        for name in ("alpha", "beta"):
            skill = root / ".claude" / "skills" / name
            skill.mkdir(parents=True)
            (skill / "SKILL.md").write_text(SKILL_MD.format(name=name), encoding="utf-8")
        return root

    def test_an_import_abandoned_before_it_starts_imports_nothing(self, runtime_project):
        with abandon_sync_on_exit() as abandoned:
            abandoned.set()
            result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert result.imported == []
        assert [row[2] for row in result.skipped] == [skip_codes.ABANDONED]
        assert not (runtime_project / CANONICAL_SKILL_ROOT).exists()

    def test_skills_imported_before_the_abandonment_stay_imported(
        self, runtime_project, monkeypatch
    ):
        real_promote = skills_mod._promote_staging
        promotes: list = []

        with abandon_sync_on_exit() as abandoned:

            def _promote_then_abandon(staging, dst, **kwargs):
                real_promote(staging, dst, **kwargs)
                promotes.append(dst)
                abandoned.set()

            monkeypatch.setattr(skills_mod, "_promote_staging", _promote_then_abandon)
            result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert len(promotes) == 1, f"the engine kept importing after the abort: {promotes}"
        assert len(result.imported) == 1, result.imported
        rows = _abandoned_rows(result)
        assert rows and rows[0][0] == "<remaining>", result.skipped

    def test_abandonment_during_gate_a_leaves_no_trace(self, runtime_project, monkeypatch):
        """The post-Gate-A checkpoint, distinguished from the per-item one.

        Gate A walks every file in the skill and runs after the top-of-item
        check, so a caller giving up there is past it. Stopping the *promote*
        is not enough: the next two statements are ``dst.parent.mkdir`` and the
        destination lock, and a lock leaves a sidecar nothing ever removes. An
        abandoned import that reports ``imported=0`` while having created the
        canonical root and a lockfile has still changed the user's tree, so the
        check has to sit before them — which is what the last two assertions
        pin.
        """
        real_gate = skills_mod.apply_gate_a
        promotes: list = []

        with abandon_sync_on_exit() as abandoned:

            def _gate_then_abandon(*args, **kwargs):
                outcome = real_gate(*args, **kwargs)
                abandoned.set()
                return outcome

            monkeypatch.setattr(skills_mod, "apply_gate_a", _gate_then_abandon)
            monkeypatch.setattr(
                skills_mod,
                "_promote_staging",
                lambda *a, **k: promotes.append(a[1]),
            )
            result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert promotes == [], (
            "the import promoted after its caller gave up during Gate A — "
            "the post-Gate-A check did not fire"
        )
        assert result.imported == []
        assert _abandoned_rows(result), result.skipped

        canonical_root = runtime_project / CANONICAL_SKILL_ROOT
        assert not canonical_root.exists(), (
            "an abandoned import created the canonical root it never wrote into"
        )
        strays = sorted(str(p) for p in runtime_project.rglob(".*.lock"))
        assert strays == [], (
            f"an abandoned import left destination lock sidecars behind: {strays} — "
            "the check runs after the lock instead of before it"
        )

    def test_abandonment_between_skills_stops_before_the_next_gate_a(
        self, runtime_project, monkeypatch
    ):
        """The top-of-item checkpoint, distinguished from the post-Gate-A one.

        Both would leave the second skill unimported, so the outcome cannot
        tell them apart. What only the top-of-item check gives is that Gate A
        never runs for it — walking and scanning every file of a skill nobody
        is waiting for is the expensive half of an import.
        """
        real_promote = skills_mod._promote_staging
        gate_calls: list = []
        real_gate = skills_mod.apply_gate_a

        with abandon_sync_on_exit() as abandoned:

            def _counting_gate(*args, **kwargs):
                gate_calls.append(1)
                return real_gate(*args, **kwargs)

            def _promote_then_abandon(staging, dst, **kwargs):
                real_promote(staging, dst, **kwargs)
                abandoned.set()

            monkeypatch.setattr(skills_mod, "apply_gate_a", _counting_gate)
            monkeypatch.setattr(skills_mod, "_promote_staging", _promote_then_abandon)
            result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert len(result.imported) == 1, result.imported
        after_first = len(gate_calls)
        assert after_first == 1, (
            f"Gate A ran {after_first} times — it scanned a skill after the "
            "caller gave up, so the top-of-item check did not fire"
        )

    def test_abandonment_while_waiting_for_the_destination_lock_stages_nothing(
        self, runtime_project, monkeypatch
    ):
        """The post-lock checkpoint: the lock wait is after every earlier check.

        Acquiring the destination lock is the one stretch none of the earlier
        checks cover, and staging starts right after it.
        """
        real_lock = skills_mod._file_lock
        stages: list = []

        with abandon_sync_on_exit() as abandoned:

            def _lock_then_abandon(path, timeout):
                ctx = real_lock(path, timeout=timeout)
                abandoned.set()
                return ctx

            def _record_stage(*args, **kwargs):
                stages.append(args)
                raise AssertionError("staging must not start for an abandoned import")

            monkeypatch.setattr(skills_mod, "_file_lock", _lock_then_abandon)
            monkeypatch.setattr(skills_mod, "_stage_skill", _record_stage)
            result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert stages == [], (
            "the import staged after its caller gave up during the lock wait — "
            "the post-lock check did not fire"
        )
        assert result.imported == []
        assert _abandoned_rows(result), result.skipped

    def test_abandonment_during_staging_stops_before_the_promote(
        self, runtime_project, monkeypatch
    ):
        """The post-staging checkpoint: copying the tree is the long stretch.

        Staging mirrors every file of the skill, so a caller can give up inside
        it — past the post-lock check and one statement short of the promote
        that would land the import. The staging tree must be reaped on the way
        out, since nothing downstream reaps it (the cleanup lives in the
        promote's except arms).
        """
        real_stage = skills_mod._stage_skill
        promotes: list = []
        staged: list = []

        with abandon_sync_on_exit() as abandoned:

            def _stage_then_abandon(*args, **kwargs):
                staging = real_stage(*args, **kwargs)
                staged.append(staging)
                abandoned.set()
                return staging

            monkeypatch.setattr(skills_mod, "_stage_skill", _stage_then_abandon)
            monkeypatch.setattr(
                skills_mod, "_promote_staging", lambda *a, **k: promotes.append(a[1])
            )
            result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert promotes == [], (
            "the import promoted after its caller gave up during staging — "
            "the post-staging check did not fire"
        )
        assert result.imported == []
        assert _abandoned_rows(result), result.skipped
        leftovers = [p for p in staged if p.exists()]
        assert leftovers == [], f"the abandoned import left staging trees: {leftovers}"

    def test_a_dry_run_stops_enumerating_too(self, runtime_project):
        """``dry_run`` writes nothing anyway, but must not keep working either."""
        with abandon_sync_on_exit() as abandoned:
            abandoned.set()
            result = extract_skills_to_canonical(
                runtime_project, scope="project_shared", dry_run=True
            )

        assert result.imported == []
        assert [row[2] for row in result.skipped] == [skip_codes.ABANDONED]

    def test_an_import_nobody_abandoned_runs_normally(self, runtime_project):
        result = extract_skills_to_canonical(runtime_project, scope="project_shared")

        assert len(result.imported) == 2, result.imported
        assert not _abandoned_rows(result)
