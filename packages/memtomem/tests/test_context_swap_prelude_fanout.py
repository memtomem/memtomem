"""The recovery prelude at the ten C0 holders that never ran it — ADR-0030 §10 / PR-G4a-3b.

G4a-3a made ``skills._recover_and_reap_internal_dirs`` recovery-first and wired
its four existing call sites. Ten OTHER writers took C0 and never recovered:
the three web skill CRUD routes, the three wiki install/update paths, the
cross-scope transfer, the two validation-seeder writes, and ``copy_skill``.
Each is a concrete data-loss path — a row-5 crash state followed by any of them
materializes ``dst``, after which recovery reads the "``dst`` + ``old``" row and
deletes ``old``, the only copy.

These tests drive the **real** surfaces (route handler, engine entry point) over
hand-built crash states. Assertions are about **convergence** — the artifact is
the expected tree, the marker is gone, no ``.old-*``/``.staging-*`` residue —
because "the original survived" is also true of a fail-closed row 4 and so
cannot tell a working prelude from a broken one.

Row 4 (all three present) is asserted only where it is REACHABLE: it leaves a
real directory at ``dst``, so web create, fresh wiki install and the transfer
*destination* refuse on their unlocked pre-checks before C0 is ever acquired.
Those refusals are true and write nothing; the sites that do reach the lock —
transfer source, update, delete, ``copy_skill``, the seeder — must map it to
``swap_recovery_pending``. Both halves are pinned below so the boundary is
encoded rather than assumed.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context._dir_swap import (
    SwapForeignDestination,
    SwapRecoveryError,
    has_pending_swap,
    swap_failure_text,
)
from memtomem.context.install import (
    AlreadyInstalledError,
    StaleInstallError,
    _apply_pinned_install,
    _classify_for_install_all,
    install_skill,
    update_skill,
)
from memtomem.context.migrate import ArtifactNotFoundError, _detect_source_scope
from memtomem.context.skills import SKILL_MANIFEST, copy_skill, run_swap_prelude
from memtomem.context.transfer import (
    TransferCollisionError,
    TransferRecoveryError,
    transfer_artifact,
)
from memtomem.web.app import create_app
from memtomem.wiki.store import WikiStore

from .helpers import seed_multi_runtime, set_home

# ``wiki_root`` / ``git_identity`` come from ``_wiki_fixtures`` via conftest.

SUFFIX = "999999-abc123"


def _tree(path: Path, content: str) -> Path:
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
    """A well-formed marker for ``(name, suffix)`` — the shape a CRASH leaves.

    Hand-rolled for the same reason ``test_context_swap_prelude`` hand-rolls it:
    no successful forward path can construct these states.
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


def _row_2(root: Path, name: str = "skill") -> dict[str, Path]:
    """Crashed between the renames: no ``dst``, ``old`` = pre-image, ``staging`` = replacement."""
    p = _write_marker(root, name)
    _tree(p["old"], "original")
    _tree(p["staging"], "replacement")
    return p


def _row_4(root: Path, name: str = "skill") -> dict[str, Path]:
    """All three present — ambiguous provenance, fail-closed."""
    p = _write_marker(root, name)
    _tree(p["dst"], "candidate-a")
    _tree(p["old"], "candidate-b")
    _tree(p["staging"], "replacement")
    return p


def _row_5(root: Path, name: str = "skill") -> dict[str, Path]:
    """``dst`` and ``staging`` gone: only the known pre-image can be restored."""
    p = _write_marker(root, name)
    _tree(p["old"], "original")
    return p


def _row_6(root: Path, name: str = "skill") -> dict[str, Path]:
    """No pre-image: the complete staged replacement is the only tree."""
    p = _write_marker(root, name)
    _tree(p["staging"], "replacement")
    return p


def _assert_converged(root: Path, dst: Path, content: str) -> None:
    assert dst.is_dir(), f"{dst} did not converge to a directory"
    assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == content
    assert _residue(root) == [], "recovery left a marker or transient behind"


def _isolate_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """Point the user tier at *home*, and PROVE it moved.

    Every user-tier case here writes through ``Path.home()``. A bare
    ``monkeypatch.setenv("HOME", …)`` is silently ignored on Windows — which
    reads ``USERPROFILE`` first — so these tests wrote into the runner's real
    home there, collided with each other, and failed only on Windows CI.
    ``set_home`` sets both, and the assertion is what keeps this from
    regressing quietly: on a platform where the override does not take, the
    test says so instead of mutating a real user's Store.
    """
    set_home(monkeypatch, home)
    assert Path.home().resolve() == home.resolve(), (
        "the user tier still resolves outside the test sandbox — refusing to write to a real home"
    )
    return home


def _wiki_commit(wiki_root_path: Path, name: str, body: bytes) -> None:
    """Commit new bytes for ``name`` so an update has work to do.

    Without a second commit the update returns a no-op BEFORE taking the
    lock (HEAD already matches the recorded pin), and the test would prove
    nothing about the prelude.
    """
    (wiki_root_path / "skills" / name / SKILL_MANIFEST).write_bytes(body)
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"update {name}"],
        check=True,
        capture_output=True,
    )


def _wiki_with(wiki_root_path: Path, name: str) -> None:
    store = WikiStore.at_default()
    store.init()
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_bytes(b"# from wiki\n")
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
        check=True,
        capture_output=True,
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".memtomem" / "skills").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(project: Path) -> Path:
    return project / ".memtomem" / "skills"


class TestSwapFailureText:
    """The wire sentence: errno prefix dropped, offending path KEPT."""

    def test_the_path_survives_when_the_sentence_does_not_name_it(self, store: Path) -> None:
        """Most raises carry the path in ``filename`` alone (PR review).

        Dropping it leaves an operator holding "swap marker is not a JSON
        object" with two canonical roots in play and no way to tell which side
        needs repairing.
        """
        p = _paths(store)
        p["marker"].write_text("not json at all", encoding="utf-8")

        with pytest.raises(SwapRecoveryError) as exc:
            run_swap_prelude(store, "skill", kind="skills")

        text = swap_failure_text(exc.value)
        assert not text.startswith("[Errno"), text
        assert str(p["marker"]) in text, text
        assert text.count(str(p["marker"])) == 1, f"path repeated: {text}"

    def test_a_sentence_that_already_names_its_paths_is_not_padded(self, store: Path) -> None:
        """Row 4 names both trees in the prose; re-appending would duplicate one."""
        p = _row_4(store)

        with pytest.raises(SwapForeignDestination) as exc:
            run_swap_prelude(store, "skill", kind="skills")

        text = swap_failure_text(exc.value)
        assert text.count(str(p["dst"])) == 1, text
        assert str(p["old"]) in text


class TestKindGating:
    """``run_swap_prelude`` is a no-op for every kind but skills.

    Not an optimization. The flat kinds address their canonical as
    ``<root>/<name>.md``, whose ``Path.name`` carries a dot and would fail
    ``validate_name`` — so a kind-blind prelude would crash the wiki install of
    every agent and command.
    """

    @pytest.mark.parametrize("kind", ["agents", "commands", "mcp_servers"])
    def test_non_skills_kinds_are_untouched(self, store: Path, kind: str) -> None:
        p = _row_2(store)
        run_swap_prelude(store, "skill", kind=kind)
        assert _residue(store) == sorted([p["marker"].name, p["old"].name, p["staging"].name])
        assert not p["dst"].exists()

    def test_a_flat_name_would_not_be_validated(self, store: Path) -> None:
        """The dotted name a flat kind carries never reaches the validator."""
        run_swap_prelude(store, "agent.md", kind="agents")  # must not raise

    def test_skills_kind_recovers(self, store: Path) -> None:
        p = _row_2(store)
        run_swap_prelude(store, "skill", kind="skills")
        _assert_converged(store, p["dst"], "replacement")


class TestCopySkill:
    """``copy_skill`` — the tenth site, the one the by-surface census missed."""

    def test_row_2_converges_then_copies(self, tmp_path: Path, store: Path) -> None:
        p = _row_2(store)
        src = _tree(tmp_path / "src-skill", "incoming")

        copy_skill(src, p["dst"])

        # The copy wins (it is the write the caller asked for), and the
        # interrupted transaction left nothing behind.
        _assert_converged(store, p["dst"], "incoming")

    def test_row_5_converges_then_copies(self, tmp_path: Path, store: Path) -> None:
        p = _row_5(store)
        src = _tree(tmp_path / "src-skill", "incoming")

        copy_skill(src, p["dst"])

        _assert_converged(store, p["dst"], "incoming")

    def test_row_4_refuses_and_deletes_nothing(self, tmp_path: Path, store: Path) -> None:
        p = _row_4(store)
        src = _tree(tmp_path / "src-skill", "incoming")
        before = _residue(store)

        with pytest.raises(SwapRecoveryError):
            copy_skill(src, p["dst"])

        assert _residue(store) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert (p["old"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-b"

    @pytest.mark.parametrize("name", ["my skill", "-skill", "s" * 65])
    def test_preserves_the_legacy_arbitrary_destination_basename_contract(
        self, tmp_path: Path, name: str
    ) -> None:
        """Only canonical basenames can own swaps; ``copy_skill`` accepts more.

        The recovery prelude must not turn that public path API into the
        canonical name API, including after the destination lock has created
        its parent and sidecar.
        """
        src = _tree(tmp_path / "src-skill", "incoming")
        dst = tmp_path / "runtime" / name

        copy_skill(src, dst)

        assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == "incoming"


class TestTransferDiscovery:
    """§2.1.1 — a live marker is evidence of residency, opt-in and read-only."""

    def test_marker_only_source_is_invisible_by_default(self, project: Path, store: Path) -> None:
        """The default is unchanged, so ``migrate_scope`` and friends keep today's behavior."""
        _row_2(store)
        with pytest.raises(ArtifactNotFoundError):
            _detect_source_scope("skills", "skill", project, "project_shared")

    def test_marker_only_source_resolves_when_opted_in(self, project: Path, store: Path) -> None:
        p = _row_2(store)
        scope, src_path, layout = _detect_source_scope(
            "skills", "skill", project, "project_shared", marker_counts_as_presence=True
        )
        assert (scope, src_path, layout) == ("project_shared", p["dst"], "dir")
        # Read-only: the probe must not have recovered anything.
        assert has_pending_swap(store, "skill")

    def test_probe_is_side_effect_free(self, project: Path, store: Path) -> None:
        p = _row_2(store)
        before = _residue(store)
        _detect_source_scope("skills", "skill", project, None, marker_counts_as_presence=True)
        assert _residue(store) == before
        assert not p["dst"].exists()


class TestTransferApply:
    """The apply path recovers BOTH roots, then re-verifies the source contract."""

    def _transfer(self, project: Path, **kw: object) -> object:
        return transfer_artifact(
            "skills",
            "skill",
            src_project_root=project,
            from_scope="project_shared",
            dst_project_root=None,
            to_scope="user",
            mode="move",
            apply_=True,
            **kw,  # type: ignore[arg-type]
        )

    def test_source_row_2_is_recovered_and_moved(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_home(monkeypatch, tmp_path / "home")
        p = _row_2(store)

        self._transfer(project)

        assert not p["dst"].exists(), "the move should have consumed the recovered tree"
        assert _residue(store) == [], "recovery left transients behind"

    def test_source_row_5_is_recovered_and_moved(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        _isolate_home(monkeypatch, tmp_path / "home")
        p = _row_5(store)

        self._transfer(project)

        assert not p["dst"].exists()
        assert _residue(store) == []

    def test_source_row_4_refuses_as_swap_recovery_pending(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reachable here — the source tree is what discovery found, not a collision."""
        _isolate_home(monkeypatch, tmp_path / "home")
        p = _row_4(store)
        before = _residue(store)

        with pytest.raises(TransferRecoveryError):
            self._transfer(project)

        assert _residue(store) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"

    def test_destination_row_2_is_recovered_then_collides(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The DESTINATION prelude, which the source cases prove nothing about.

        A user-tier destination mid-swap has no ``dst``, so the pre-lock
        collision check passes. Recovery under the lock materializes the
        destination — and the in-lock re-check then refuses, correctly, on the
        tree that actually exists. Delete the destination prelude and this
        transfer instead lands on top of a live transaction.
        """
        home = tmp_path / "home"
        _isolate_home(monkeypatch, home)
        user_store = home / ".memtomem" / "skills"
        user_store.mkdir(parents=True)
        dp = _row_2(user_store)
        _tree(store / "skill", "source")

        with pytest.raises(TransferCollisionError):
            self._transfer(project)

        _assert_converged(user_store, dp["dst"], "replacement")
        assert (store / "skill" / SKILL_MANIFEST).read_text(encoding="utf-8") == "source", (
            "a refused move must leave the source intact"
        )

    @pytest.mark.parametrize(
        "row_builder", [_row_2, _row_5, _row_6], ids=["row-2", "row-5", "row-6"]
    )
    def test_destination_mid_swap_dry_run_warns_apply_may_refuse(
        self,
        project: Path,
        store: Path,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        row_builder: Callable[[Path], dict[str, Path]],
    ) -> None:
        """An absent destination can still be materialized by apply recovery."""
        home = tmp_path / "home"
        _isolate_home(monkeypatch, home)
        user_store = home / ".memtomem" / "skills"
        user_store.mkdir(parents=True)
        destination = row_builder(user_store)
        _tree(store / "skill", "source")

        preview = transfer_artifact(
            "skills",
            "skill",
            src_project_root=project,
            from_scope="project_shared",
            dst_project_root=None,
            to_scope="user",
            mode="move",
            apply_=False,
        )

        assert any("destination has an interrupted directory swap" in n for n in preview.notes)
        assert not destination["dst"].exists(), "dry-run recovered the destination"
        assert has_pending_swap(user_store, "skill"), "dry-run removed the swap marker"

    def test_destination_row_4_is_refused_before_the_lock(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """The narrowed contract at the destination, encoded.

        Row 4 leaves a real ``dst``, which the unlocked pre-flight sees, so the
        transfer refuses as ``destination_exists`` and the prelude is never
        reached. True, and it writes nothing — the source and all three
        destination trees are exactly as they were. The destination prelude is
        proven by the row-2 case above, which is the state that DOES pass the
        pre-flight.
        """
        home = tmp_path / "home"
        _isolate_home(monkeypatch, home)
        user_store = home / ".memtomem" / "skills"
        user_store.mkdir(parents=True)
        _tree(store / "skill", "source")
        _row_4(user_store)
        before = _residue(user_store)

        with pytest.raises(TransferCollisionError) as exc:
            self._transfer(project)

        assert not isinstance(exc.value, TransferRecoveryError)
        assert _residue(user_store) == before
        assert (store / "skill" / SKILL_MANIFEST).read_text(encoding="utf-8") == "source"

    def test_a_marker_only_preview_says_its_classifications_are_provisional(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Preview and apply must not disagree silently (PR review).

        A marker-only source has no tree for the dry run to read, so
        ``_plan_provenance`` sees a missing source and reports it unprovable —
        while apply, which recovers first, can classify the very same artifact
        clean and carry its lockfile entry. Nothing changed in between; the two
        answers just came from different states. The preview therefore says so,
        and the applied result repeats it so a result read on its own carries
        the same caveat.
        """
        _isolate_home(monkeypatch, tmp_path / "home")
        _row_2(store)

        preview = transfer_artifact(
            "skills",
            "skill",
            src_project_root=project,
            from_scope="project_shared",
            dst_project_root=None,
            to_scope="user",
            mode="move",
            apply_=False,
        )
        assert any("interrupted directory swap" in n for n in preview.notes), preview.notes
        assert not (store / "skill").exists(), "a preview must not recover anything"

        applied = transfer_artifact(
            "skills",
            "skill",
            src_project_root=project,
            from_scope="project_shared",
            dst_project_root=None,
            to_scope="user",
            mode="move",
            apply_=True,
        )
        assert any("interrupted directory swap" in n for n in applied.notes), applied.notes

    def test_a_recovered_tree_without_its_manifest_is_not_transferred(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Recovery authenticates the marker↔path relation, never the payload.

        A row-5 pre-image mangled by an out-of-band writer converges to a
        directory with no ``SKILL.md``. The in-lock re-check repeats the FULL
        discovery contract, so that is a not-found, not a transfer of a
        half-artifact.
        """
        _isolate_home(monkeypatch, tmp_path / "home")
        p = _write_marker(store)
        p["old"].mkdir()  # a tree, but not a skill

        with pytest.raises(ArtifactNotFoundError) as excinfo:
            self._transfer(project)

        assert str(store) not in excinfo.value.message
        assert excinfo.value.message == (
            "skills/skill is no longer a complete artifact "
            "(it disappeared, or an interrupted transaction left it incomplete)."
        )


@pytest.fixture
def web_project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    (tmp_path / ".memtomem" / "skills").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def web_store(web_project: Path) -> Path:
    return web_project / ".memtomem" / "skills"


@pytest.fixture
async def client(web_project: Path):
    """The real ASGI app — the route's dependencies resolve the scope root.

    Calling the handler function directly would hand FastAPI's ``Query``
    default objects straight through to the engine, so the test would exercise
    a code path no request can produce.
    """
    app = create_app(lifespan=None, mode="dev")
    app.state.project_root = web_project
    app.state.storage = AsyncMock()
    for attr in (
        "config",
        "search_pipeline",
        "index_engine",
        "embedder",
        "dedup_scanner",
        "last_reload_error",
    ):
        setattr(app.state, attr, None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestUserTierConsentPrecedesRecovery:
    """Recovery is a host write, so it may not run before the user has confirmed.

    The specific hole (PR review): in a row-2/row-5 state the canonical is
    ABSENT, so a delete's presence-derived disclosure is empty, the host-write
    gate stays open — its documented no-op behavior — and a prelude placed
    first would restore the tree under ``~/.memtomem`` and clear the
    transients before the locked gate could return ``needs_confirmation``. The
    request that was never confirmed would already have changed host state.
    """

    async def test_unconfirmed_user_delete_of_a_mid_swap_skill_changes_nothing(
        self, client: AsyncClient, web_project: Path
    ) -> None:
        user_store = web_project / ".memtomem" / "skills"  # HOME == web_project
        p = _row_2(user_store)
        before = _residue(user_store)

        resp = await client.delete("/api/context/skills/skill?target_scope=user")

        body = resp.json()
        assert body.get("status") == "needs_confirmation", body
        assert body["confirm"] == "allow_host_writes"
        # The disclosure names the canonical even though it does not exist yet —
        # that is what makes consent cover what recovery would materialize.
        assert any(str(p["dst"]) in t for t in body["host_targets"]), body["host_targets"]
        # And nothing moved: marker and both transients exactly as found.
        assert _residue(user_store) == before
        assert not p["dst"].exists()

    async def test_confirmed_user_delete_recovers_then_deletes(
        self, client: AsyncClient, web_project: Path
    ) -> None:
        user_store = web_project / ".memtomem" / "skills"
        p = _row_2(user_store)

        resp = await client.delete(
            "/api/context/skills/skill?target_scope=user&allow_host_writes=true"
        )

        assert resp.status_code == 200, resp.text
        assert resp.json()["deleted"], resp.text
        assert not p["dst"].exists()
        assert _residue(user_store) == []


class TestWebSkillRoutes:
    """The three CRUD routes: row 2/5 converge, row 4 maps to 409 where reachable."""

    async def test_create_converges_a_row_2(self, client: AsyncClient, web_store: Path) -> None:
        p = _row_2(web_store)

        resp = await client.post(
            "/api/context/skills", json={"name": "skill", "content": "# skill\n"}
        )

        # Recovery restored the artifact under the lock, so the create refuses
        # as a conflict — but against the RECOVERED tree, with no residue left.
        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "already_exists"
        _assert_converged(web_store, p["dst"], "replacement")

    async def test_row_4_is_refused_before_the_lock_on_create(
        self, client: AsyncClient, web_store: Path
    ) -> None:
        """The narrowed contract, encoded: create cannot reach the prelude in row 4.

        ``dst`` is a real directory, so the unlocked ``already_exists`` check
        returns first. That refusal is true and writes nothing — but it means a
        row-4 wedge surfaces here under a neighbouring code, which is why the
        §10 obligation is stated over the paths that actually acquire C0.
        """
        p = _row_4(web_store)
        before = _wedge_state(p)

        resp = await client.post(
            "/api/context/skills", json={"name": "skill", "content": "# skill\n"}
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "already_exists"
        assert _wedge_state(p) == before, "a pre-lock refusal must reap nothing"

    async def test_create_broken_marker_maps_to_409_swap_recovery_pending(
        self, client: AsyncClient, web_store: Path
    ) -> None:
        """Create's REACHABLE direction (D5, Codex review): with ``dst`` absent
        the ``already_exists`` pre-check passes, and only the in-lock prelude
        stands between the wedge and a fresh canonical landing on top of it —
        delete create's ``SwapRecoveryError`` arm and this is a 500."""
        p = _broken_marker(web_store)
        before = _wedge_state(p)

        resp = await client.post(
            "/api/context/skills", json={"name": "skill", "content": "# skill\n"}
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["reason_code"] == "swap_recovery_pending"
        _assert_no_demotion(resp.text)
        assert str(web_store) not in resp.text, "wire reasons are redacted"
        assert not p["dst"].exists(), "a refusal must create nothing"
        assert _wedge_state(p) == before

    async def test_delete_maps_row_4_to_409_swap_recovery_pending(
        self, client: AsyncClient, web_store: Path
    ) -> None:
        """Delete DOES reach the lock, so it gets the typed refusal."""
        p = _row_4(web_store)
        before = _wedge_state(p)

        resp = await client.delete("/api/context/skills/skill")

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "swap_recovery_pending"
        _assert_no_demotion(resp.text)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).is_file(), "a refusal must delete nothing"

    async def test_delete_converges_a_row_2_then_deletes(
        self, client: AsyncClient, web_store: Path
    ) -> None:
        p = _row_2(web_store)

        resp = await client.delete("/api/context/skills/skill")

        assert resp.status_code == 200
        assert resp.json()["deleted"], "the recovered canonical should have been deleted"
        assert not p["dst"].exists()
        assert _residue(web_store) == []

    async def test_update_maps_row_4_to_409_swap_recovery_pending(
        self, client: AsyncClient, web_store: Path
    ) -> None:
        p = _row_4(web_store)
        mtime_ns = str((p["dst"] / SKILL_MANIFEST).stat().st_mtime_ns)
        before = _wedge_state(p)

        resp = await client.put(
            "/api/context/skills/skill",
            json={"content": "# edited\n", "mtime_ns": mtime_ns},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "swap_recovery_pending"
        _assert_no_demotion(resp.text)
        assert _wedge_state(p) == before


class TestWikiInstall:
    """``install.py`` — all three kind-polymorphic sites, each through its own entry point.

    One test per site, deliberately: they are three separate ``with`` bodies
    with three separate in-lock re-checks, so a suite that only ever calls
    ``install_skill`` stays green with either of the other two preludes deleted
    (Codex code gate).
    """

    # ``_wiki_commit`` / ``_wiki_with`` hoisted to module level for the D5
    # matrix below (mechanical move — same bodies).

    def test_row_2_is_recovered_before_the_dest_re_check(
        self, wiki_root: Path, tmp_path: Path
    ) -> None:
        """Recovery precedes the in-lock ``dest.exists()`` — which RETURNS on what it sees.

        Without the ordering, the install would classify the destination absent
        (row 2 has no ``dst``), extract over the recovering transaction, and
        leave the next recovery reading a row whose action deletes the
        pre-image.
        """
        _wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        store = project / ".memtomem" / "skills"
        store.mkdir(parents=True)
        p = _row_2(store, name="foo")

        with pytest.raises(AlreadyInstalledError):
            install_skill(project, "foo")

        # The refusal is correct BECAUSE recovery restored the artifact first.
        _assert_converged(store, p["dst"], "replacement")

    def test_row_4_refuses_and_deletes_nothing(self, wiki_root: Path, tmp_path: Path) -> None:
        """Row 4 leaves a real ``dst``, so the unlocked pre-check refuses first.

        Pinned rather than asserted away: this is the narrowed §10 contract —
        the wedge surfaces as ``already_installed`` here, and nothing is
        reaped, which is what makes the narrowing safe.
        """
        _wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        store = project / ".memtomem" / "skills"
        store.mkdir(parents=True)
        _row_4(store, name="foo")
        before = _residue(store)

        with pytest.raises(AlreadyInstalledError):
            install_skill(project, "foo")

        assert _residue(store) == before

    def test_update_recovers_before_the_dirty_classify(
        self, wiki_root: Path, tmp_path: Path
    ) -> None:
        """``_apply_update``'s own site: the dirty re-classification reads the tree.

        A row-2 destination has no ``dst`` at all, so a pre-recovery classify
        would see "missing dest" and re-extract over the transaction. After
        recovery the tree is the replacement — which differs from the recorded
        install — so the update correctly refuses as dirty instead.
        """
        _wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")  # records the lock.json entry update needs
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        store = project / ".memtomem" / "skills"
        shutil.rmtree(store / "foo")
        p = _row_2(store, name="foo")

        with pytest.raises(StaleInstallError):
            update_skill(project, "foo")

        _assert_converged(store, p["dst"], "replacement")

    def test_row_4_refuses_the_update_and_deletes_nothing(
        self, wiki_root: Path, tmp_path: Path
    ) -> None:
        """Row 4 under ``--force``, the one way an update reaches the lock in that state.

        Without force the pre-lock dirty classify refuses first — row 4 leaves a
        ``dst`` whose bytes differ from the recorded install, so it reads as a
        local edit. That is the narrowed §10 contract again, and the second
        assertion below pins it. With force the pre-lock gate is deliberately
        skipped, the lock is taken, and the prelude is what refuses: an
        interrupted transaction is not something ``--force`` may overwrite.
        """
        _wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        store = project / ".memtomem" / "skills"
        shutil.rmtree(store / "foo")
        p = _row_4(store, name="foo")
        before = _residue(store)

        with pytest.raises(SwapRecoveryError):
            update_skill(project, "foo", force=True)

        assert _residue(store) == before
        assert (p["old"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-b"
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"

        # …and without --force it never gets that far: a pre-lock refusal that
        # still reaps nothing.
        with pytest.raises(StaleInstallError):
            update_skill(project, "foo")
        assert _residue(store) == before

    def test_the_cli_prints_a_sentence_not_an_oserror_repr(
        self, wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`mm context install skill` — the primary single-asset command.

        Listing ``SwapRecoveryError`` in ``_translate_to_click`` alone routes it
        through that helper's default ``str(exc)`` branch, which is exactly the
        ``[Errno 16] …: '<path>'`` form (PR review). The translator special-cases
        it, and this pins the user-visible result rather than the plumbing.
        """
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")  # the lock.json entry update needs
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        store = project / ".memtomem" / "skills"
        shutil.rmtree(store / "foo")
        p = _row_4(store, name="foo")
        before = _wedge_state(p)
        monkeypatch.chdir(project)

        # --force so the pre-lock dirty gate does not refuse before the lock.
        result = CliRunner().invoke(
            context, ["update", "skill", "foo", "--force"], catch_exceptions=False
        )

        assert result.exit_code == 1
        _assert_no_demotion(result.output)
        assert "interrupted directory swap" in result.output
        assert str(p["old"]) in result.output, "the operator needs the path to inspect"
        assert _wedge_state(p) == before

    def test_pinned_install_recovers_before_its_own_re_check(
        self, wiki_root: Path, tmp_path: Path
    ) -> None:
        """``_apply_pinned_install`` — the third site, reached via ``install --all``.

        Its own ``with`` body and its own dirty re-classify, so the other two
        preludes prove nothing about it.
        """
        _wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        store = project / ".memtomem" / "skills"
        shutil.rmtree(store / "foo")
        p = _row_2(store, name="foo")

        classifications = _classify_for_install_all(project, wiki=WikiStore.at_default())
        row = next(c for c in classifications if c.name == "foo")
        with pytest.raises((StaleInstallError, AlreadyInstalledError)):
            _apply_pinned_install(project, row, wiki=WikiStore.at_default(), force=False)

        _assert_converged(store, p["dst"], "replacement")


class TestMcpTransfer:
    """MCP parity: the refusal is prefix-coded, not flattened into a collision."""

    async def test_row_4_source_returns_swap_recovery_pending(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_artifact_transfer

        home = _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        p = _row_4(store)
        before = _wedge_state(p)

        out = await mem_context_artifact_transfer(
            asset_type="skills",
            name="skill",
            from_scope="project_shared",
            to_scope="user",
            mode="move",
            apply=True,
            allow_host_writes=True,  # user tier writes outside the project root
        )

        assert out.startswith("refused: swap_recovery_pending:")
        # NOT the plain collision line the base class would have produced.
        assert "destination already exists" not in out
        _assert_no_demotion(out)
        assert _wedge_state(p) == before
        assert not (home / ".memtomem" / "skills" / "skill").exists()

    async def test_row_2_resolves_the_default_tier_cross_project(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The third discovery probe: MCP's own default-tier lookup.

        Reached when ``to_project_scope_id`` is given without ``to_scope`` —
        the tool then resolves the source tier itself, through a
        ``_detect_source_scope`` handed to ``to_thread``. Without the marker
        opt-in there it answers "not found" and the engine that would have
        recovered the artifact is never called. That third call site was missed
        by a grep for the function because it is passed as a callable, not
        called (Codex code gate).
        """
        from memtomem.cli.context_cmd import ContextGatewayConfig  # noqa: F401 — patched below
        from memtomem.context.projects import KnownProjectsStore, compute_scope_id
        from memtomem.server.tools.context import mem_context_artifact_transfer

        home = tmp_path / "home"
        home.mkdir()
        _isolate_home(monkeypatch, home)
        proj_a, proj_b = tmp_path / "proj-a", tmp_path / "proj-b"
        for proj in (proj_a, proj_b):
            (proj / ".git").mkdir(parents=True)
            (proj / ".memtomem").mkdir()
        kp = tmp_path / "known_projects.json"

        class _FakeCfg:
            known_projects_path = kp
            experimental_claude_projects_scan = False
            auto_display_configured_projects = True

        monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
        monkeypatch.chdir(proj_a)
        KnownProjectsStore(kp).add(proj_b)

        store = proj_a / ".memtomem" / "skills"
        store.mkdir()
        p = _row_2(store)

        out = await mem_context_artifact_transfer(
            asset_type="skills",
            name="skill",
            mode="copy",
            to_project_scope_id=compute_scope_id(proj_b),
            # Dry run: discovery must resolve the tier, and nothing may be written.
        )

        assert "not found" not in out, out
        assert _residue(store) == sorted([p["marker"].name, p["old"].name, p["staging"].name]), (
            "a dry run must not recover anything"
        )


class TestSeeder:
    """The dev seeder stops loudly rather than seeding over a wedged Store."""

    def test_row_4_propagates_the_typed_error_to_the_library_caller(self, tmp_path: Path) -> None:
        """The engine keeps the typed error; the CLI is what turns it into an exit-1.

        Pinned at the library level deliberately — translating inside the
        seeder would take that choice away from every other caller.
        """
        from memtomem.context._validation_seed import (
            SKILL_OUT_OF_SYNC,
            seed_adr0026_validation_states,
        )

        skills = tmp_path / ".memtomem" / "skills"
        skills.mkdir(parents=True)
        _row_4(skills, name=SKILL_OUT_OF_SYNC)
        before = _residue(skills)

        with pytest.raises(SwapForeignDestination):
            seed_adr0026_validation_states(tmp_path)

        assert _residue(skills) == before

    def test_the_second_seeded_skill_has_its_own_prelude(self, tmp_path: Path) -> None:
        """Two writes, two locks, two preludes — and the first one aborts the run.

        A wedge on the FIRST skill stops the seeder before the second write, so
        a test that only wedges ``code-review`` would stay green with the
        second prelude deleted. This one wedges ``commit-helper`` instead, which
        is only reachable after the first write has already succeeded.
        """
        from memtomem.context._validation_seed import SKILL_IN_SYNC, seed_adr0026_validation_states

        skills = tmp_path / ".memtomem" / "skills"
        skills.mkdir(parents=True)
        p = _row_2(skills, name=SKILL_IN_SYNC)

        seed_adr0026_validation_states(tmp_path)

        # The second write's prelude converged the transaction, then the seeder
        # overwrote the manifest with its own body — no residue either way.
        assert _residue(skills) == []
        assert p["dst"].is_dir()
        assert SKILL_IN_SYNC in (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8")

    def test_the_cli_turns_it_into_a_one_line_error(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context
        from memtomem.context._validation_seed import SKILL_OUT_OF_SYNC

        target = tmp_path / "seed-here"
        skills = target / ".memtomem" / "skills"
        skills.mkdir(parents=True)
        p = _row_4(skills, name=SKILL_OUT_OF_SYNC)
        before = _wedge_state(p)

        result = CliRunner().invoke(context, ["seed-validation", str(target), "--force"])

        assert result.exit_code == 1
        assert "interrupted directory swap" in result.output
        _assert_no_demotion(result.output)
        assert _wedge_state(p) == before


# ─────────────────────────────────────────────────────────────────────────────
# D5: live-surface negative matrix (issue #1919 — G4a-3c follow-up, spec rev.6)
#
# The supertype-interception guard (#1917) is the structural half: every broad
# handler between a swap raise and a public surface is classified. This section
# is the behavioural half — each case wedges a REAL pending swap on disk and
# drives one public surface end-to-end, asserting (a) the pinned
# ``swap_recovery_pending`` envelope (status/reason_code/literal), (b) that the
# guard's forbidden demotions never appear, and (c) that the residue is
# untouched (recovery belongs to the operator, not the surface).
#
# Two wedge shapes cover every surface (``_dir_swap.recover_pending_swaps``):
# row 4 refuses only where no pre-lock presence check sees its real ``dst``
# first; everywhere else (extract/import/init, fresh install, non-overwrite
# Pull) the ONLY refusing wedge is a corrupt marker with ``dst`` absent —
# rows 2/5/6 would converge instead of refusing. Surfaces with a presence
# pre-check pin BOTH directions: the neighbouring refusal on the row-4
# variant, the swap envelope on the reachable one (the 3b caveat, carried).
# ─────────────────────────────────────────────────────────────────────────────

_FORBIDDEN = ("destination_exists", "target_conflict", "unreadable", "[Errno", "Traceback")


def _assert_no_demotion(text: str) -> None:
    """The generic classifications the guard exists to prevent, absent."""
    for token in _FORBIDDEN:
        assert token not in text, f"swap demoted to a generic failure ({token!r}): {text}"


def _surface_rows(output: str, needle: str) -> str:
    """The CLI batch surfaces' pinned unit is the classification ROW.

    ``CliRunner`` captures the app's diagnostic logging alongside stdout, and
    the engine's per-skip ``logger.warning`` legitimately repeats the raw
    ``[Errno 16]`` form there — that channel is not the surface contract.
    Selecting the row(s) keeps the forbidden-substring sweep honest about what
    it pins without asserting anything about log formatting.
    """
    rows = [line for line in output.splitlines() if needle in line]
    assert rows, f"no {needle!r} row in the surface output:\n{output}"
    return "\n".join(rows)


def _wedge_state(p: dict[str, Path]) -> tuple[list[str], dict[str, object]]:
    """Residue basenames PLUS content-level state of every wedge path.

    ``_residue`` alone compares names — an in-place rewrite of the marker or
    either candidate tree under the same basenames would pass it. "Untouched"
    means bytes, so each of ``dst``/``old``/``staging``/``marker`` is captured
    as (kind, content): file bytes, a dir's file map, or absence.
    """

    def capture(path: Path) -> object:
        # ``lstat``-style dispatch: a symlink is its OWN entry kind, never
        # "absent" and never followed — recovery code treats symlink type as
        # security-relevant, so an absent path swapped for a dangling link (or
        # a file for a same-content link) must change the snapshot (Codex R2).
        if path.is_symlink():
            return ("symlink", os.readlink(path))
        if not path.exists():
            return ("absent",)
        if path.is_dir():
            entries: dict[str, object] = {}
            for child in sorted(path.rglob("*")):
                rel = child.relative_to(path).as_posix()
                if child.is_symlink():
                    entries[rel] = ("symlink", os.readlink(child))
                elif child.is_dir():
                    entries[rel] = ("dir",)
                else:
                    entries[rel] = ("file", child.read_bytes())
            return ("dir", entries)
        return ("file", path.read_bytes())

    return _residue(p["dst"].parent), {key: capture(path) for key, path in p.items()}


def _broken_marker(root: Path, name: str = "skill") -> dict[str, Path]:
    """A wedge that refuses even where a presence pre-check guards row 4.

    ``dst`` is absent, so ``already_exists`` / ``canonical_exists`` gates pass
    — and the prelude, which cannot parse the marker, fails closed under the
    lock. Rows 2/5/6 cannot serve here: they converge instead of refusing.
    """
    p = _paths(root, name)
    p["marker"].write_text("not json at all", encoding="utf-8")
    return p


def _row_4_around(root: Path, name: str = "skill") -> dict[str, Path]:
    """Row 4 built AROUND an existing real ``dst`` (installed tree kept).

    For surfaces whose pre-lock classification must still read the live
    artifact (update --all's clean classify, overwrite-Pull's content diff):
    marker + ``old`` + ``staging`` are planted, the destination tree is left
    exactly as the caller created it.
    """
    p = _write_marker(root, name)
    _tree(p["old"], "candidate-b")
    _tree(p["staging"], "replacement")
    return p


def _skill(root: Path, name: str, body: str | None = None) -> Path:
    """A real canonical/runtime skill tree at ``root/name``."""
    return _tree(root / name, body if body is not None else f"---\nname: {name}\n---\nbody\n")


class TestWedgeStateHelper:
    """The snapshot helper's own contract: entry-type changes are not noise.

    ``_dir_swap`` treats symlink type as security-relevant, so the matrix's
    "untouched" proof must distinguish a path from a link to it. Both cases
    below leave ``_residue`` (basenames) identical — only the typed snapshot
    can catch them (Codex R2).
    """

    def test_absent_to_dangling_symlink_is_detected(self, store: Path) -> None:
        p = _row_2(store)  # dst absent
        before = _wedge_state(p)
        try:
            p["dst"].symlink_to(store / "nowhere")
        except OSError:  # pragma: no cover — symlink-less filesystems
            pytest.skip("symlinks unavailable on this platform")

        assert _wedge_state(p) != before

    def test_file_to_same_content_symlink_swap_is_detected(
        self, tmp_path: Path, store: Path
    ) -> None:
        """The byte-reading impostor: a link whose target holds the SAME bytes
        reads identically through ``read_bytes`` — only the entry kind tells."""
        p = _row_4(store)
        before = _wedge_state(p)
        manifest = p["old"] / SKILL_MANIFEST
        stash = tmp_path / "elsewhere.md"
        stash.write_bytes(manifest.read_bytes())
        manifest.unlink()
        try:
            manifest.symlink_to(stash)
        except OSError:  # pragma: no cover — symlink-less filesystems
            pytest.skip("symlinks unavailable on this platform")

        assert _wedge_state(p) != before


@pytest.fixture
def d5_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    return _isolate_home(monkeypatch, home)


@pytest.fixture
def d5_proj(tmp_path: Path, d5_home: Path) -> Path:
    """A project root SEPARATE from HOME — the existing ``web_project`` aliases
    the user store onto the project store, which would let a user-tier case
    silently exercise the project tier instead."""
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem" / "skills").mkdir(parents=True)
    (p / ".claude").mkdir()
    return p


@pytest.fixture
def d5_store(d5_proj: Path) -> Path:
    return d5_proj / ".memtomem" / "skills"


@pytest.fixture
def d5_kp(tmp_path: Path) -> Path:
    return tmp_path / "kp.json"


@pytest.fixture
async def d5_client(d5_proj: Path, d5_kp: Path):
    """Real ASGI app with a real gateway config (known-projects registry) —
    the transfer / sync-all / wiki routes read it; the plain ``client``
    fixture's ``config=None`` cannot serve them."""
    from memtomem.config import ContextGatewayConfig, Mem2MemConfig

    app = create_app(lifespan=None, mode="dev")
    app.state.project_root = d5_proj
    app.state.storage = AsyncMock()
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=d5_kp,
        experimental_claude_projects_scan=False,
    )
    app.state.config = config
    for attr in (
        "search_pipeline",
        "index_engine",
        "embedder",
        "dedup_scanner",
        "last_reload_error",
    ):
        setattr(app.state, attr, None)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestWebTransferRoute:
    """``POST /context/{kind}/{name}/transfer`` — 409 with the typed code.

    The engine half (``transfer_artifact``) is pinned by ``TestTransferApply``;
    this pins the ROUTE's translation ladder: ``TransferRecoveryError`` before
    ``TransferCollisionError``, so a wedged source is a ``swap_recovery_pending``
    conflict and a wedged destination stays a plain ``destination_exists``.
    """

    async def test_source_row_4_maps_to_409_swap_recovery_pending(
        self, d5_client: AsyncClient, d5_store: Path, d5_home: Path
    ) -> None:
        p = _row_4(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post(
            "/api/context/skills/skill/transfer",
            json={"mode": "move", "to_target_scope": "user", "allow_host_writes": True},
        )

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["reason_code"] == "swap_recovery_pending"
        _assert_no_demotion(resp.text)
        assert str(d5_store) not in resp.text, "wire reasons are redacted"
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert not (d5_home / ".memtomem" / "skills" / "skill").exists()

    async def test_destination_row_4_stays_destination_exists(
        self, d5_client: AsyncClient, d5_store: Path, d5_home: Path
    ) -> None:
        """The two 409s must not cross: a wedged DESTINATION has a real ``dst``
        the pre-lock collision check sees first, so it keeps the collision code
        (the engine twin is ``test_destination_row_4_is_refused_before_the_lock``)."""
        _skill(d5_store, "skill", "source")
        user_store = d5_home / ".memtomem" / "skills"
        user_store.mkdir(parents=True)
        p = _row_4(user_store)
        before = _wedge_state(p)

        resp = await d5_client.post(
            "/api/context/skills/skill/transfer",
            json={
                "mode": "move",
                # The wedged destination's real ``dst`` makes discovery see the
                # name in BOTH tiers — name the source so the ambiguity 400
                # does not shadow the collision contract under test.
                "from_scope": "project_shared",
                "to_target_scope": "user",
                "allow_host_writes": True,
            },
        )

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["reason_code"] == "destination_exists"
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before
        assert (d5_store / "skill" / SKILL_MANIFEST).read_text(encoding="utf-8") == "source"


class TestWebPullApply:
    """``POST /context/skills/{name}/pull`` — the overwrite path's live pin.

    ``_overwrite_skill_tree``'s own re-raise arm is not deterministically
    reachable from a live surface (its wedge would need the transaction's
    random suffix); the OUTER commit prelude fires first and carries the same
    contract, which is what these pin. The inner arm stays engine-tested
    (guard registry row + PR-G4b tests).
    """

    async def test_row_4_without_overwrite_is_the_canonical_exists_refusal(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        """Pre-lock direction: row 4 leaves a real canonical, so a plain Pull
        refuses result-coded (200) before any lock or prelude runs."""
        seed_multi_runtime(d5_proj, "skills", "skill", {"claude": "---\nname: skill\n---\nnew\n"})
        p = _row_4(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post(
            "/api/context/skills/skill/pull",
            params={"target_scope": "project_shared"},
            json={"confirm_project_shared": True},
        )

        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "canonical_exists", body
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before

    async def test_overwrite_pull_maps_the_wedge_to_409_swap_recovery_pending(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        """Reachable direction: ``overwrite=true`` passes the presence gate, the
        commit prelude refuses under C0 — 409, typed, nothing snapshotted."""
        seed_multi_runtime(d5_proj, "skills", "skill", {"claude": "---\nname: skill\n---\nnew\n"})
        p = _row_4(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post(
            "/api/context/skills/skill/pull",
            params={"target_scope": "project_shared"},
            json={"confirm_project_shared": True, "overwrite": True},
        )

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["reason_code"] == "swap_recovery_pending"
        assert "interrupted directory swap" in detail["message"]
        _assert_no_demotion(resp.text)
        assert str(d5_store) not in resp.text, "wire reasons are redacted"
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert not (p["dst"] / "versions").exists(), "refusal must precede the snapshot"


class TestWebSyncAll:
    """``POST /context/sync-all`` (+ ``-projects``) — the phase envelope carries
    the typed skip ROW; the phase itself stays ``ok``.

    That is the contract, not a gap: ``generate_all_skills`` converts recovery
    to per-destination skips before ``_run_phase``'s broad handlers could see
    it (the guard classifies them UNREACHABLE/handled_upstream on exactly this
    argument — if this test fails, that registry row is wrong too).
    """

    async def test_single_project_phase_reports_the_typed_skip_row(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        _skill(d5_store, "demo-skill")
        runtime_root = d5_proj / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        p = _row_4(runtime_root, "demo-skill")
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/sync-all")

        assert resp.status_code == 200, resp.text
        phase = next(ph for ph in resp.json()["phases"] if ph["type"] == "skills")
        assert phase["status"] == "ok"
        rows = [r for r in phase["skipped"] if r["reason_code"] == "swap_recovery_pending"]
        assert rows, phase["skipped"]
        assert "interrupted directory swap" in rows[0]["reason"]
        _assert_no_demotion(json.dumps(phase))
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"

    async def test_cross_project_batch_isolates_the_wedged_project(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path, tmp_path: Path
    ) -> None:
        """Wedged project A reports the typed row; clean sibling B still pushes."""
        _skill(d5_store, "demo-skill")
        runtime_root = d5_proj / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        p = _row_4(runtime_root, "demo-skill")
        before = _wedge_state(p)

        sibling = tmp_path / "sibling"
        (sibling / ".claude").mkdir(parents=True)
        (sibling / ".memtomem").mkdir()
        _skill(sibling / ".memtomem" / "skills", "demo-skill")
        reg = await d5_client.post("/api/context/known-projects", json={"root": str(sibling)})
        assert reg.status_code == 200, reg.text

        resp = await d5_client.post("/api/context/sync-all-projects")

        assert resp.status_code == 200, resp.text
        data = resp.json()
        by_root = {e["root"]: e for e in data["projects"]}
        wedged = by_root[str(d5_proj)]
        skills_phase = next(ph for ph in wedged["phases"] if ph["type"] == "skills")
        rows = [r for r in skills_phase["skipped"] if r["reason_code"] == "swap_recovery_pending"]
        assert rows, skills_phase["skipped"]
        assert "interrupted directory swap" in rows[0]["reason"]
        _assert_no_demotion(json.dumps(skills_phase))
        clean = by_root[str(sibling)]
        clean_skills = next(ph for ph in clean["phases"] if ph["type"] == "skills")
        assert clean_skills["generated"], "the clean sibling must still push"
        assert _wedge_state(p) == before


class TestWebWikiRoutes:
    """``POST /context/skills/{name}/{install,update}`` — 409, path-free envelope."""

    async def test_fresh_install_row_4_refuses_as_already_installed(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path, wiki_root: Path
    ) -> None:
        _wiki_with(wiki_root, "foo")
        p = _row_4(d5_store, name="foo")
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/foo/install")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["reason_code"] == "already_installed"
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before

    async def test_fresh_install_broken_marker_maps_to_409_swap_recovery_pending(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path, wiki_root: Path
    ) -> None:
        """``dst`` absent, so the presence pre-check passes and the in-lock
        prelude is what refuses — the reachable direction for install."""
        _wiki_with(wiki_root, "foo")
        p = _broken_marker(d5_store, name="foo")
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/foo/install")

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["reason_code"] == "swap_recovery_pending"
        _assert_no_demotion(resp.text)
        assert str(d5_store) not in resp.text, "the fixed envelope is path-free"
        assert _wedge_state(p) == before
        assert p["marker"].read_text(encoding="utf-8") == "not json at all"

    async def test_unforced_update_row_4_refuses_as_stale_install(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path, wiki_root: Path
    ) -> None:
        _wiki_with(wiki_root, "foo")
        install_skill(d5_proj, "foo")
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        shutil.rmtree(d5_store / "foo")
        p = _row_4(d5_store, name="foo")
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/foo/update")

        assert resp.status_code == 409, resp.text
        assert resp.json()["detail"]["reason_code"] == "stale_install"
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before

    async def test_forced_update_row_4_maps_to_409_swap_recovery_pending(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path, wiki_root: Path
    ) -> None:
        """``force`` skips the dirty gate — the one way an update holds C0 in
        row 4 — and the prelude refuses with the typed conflict."""
        _wiki_with(wiki_root, "foo")
        install_skill(d5_proj, "foo")
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        shutil.rmtree(d5_store / "foo")
        p = _row_4(d5_store, name="foo")
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/foo/update", json={"force": True})

        assert resp.status_code == 409, resp.text
        detail = resp.json()["detail"]
        assert detail["reason_code"] == "swap_recovery_pending"
        _assert_no_demotion(resp.text)
        assert str(d5_store) not in resp.text, "the fixed envelope is path-free"
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"


class TestWebSkillsSyncRoute:
    """``POST /context/skills/sync`` — the standalone fan-out formatter."""

    async def test_wedged_runtime_destination_is_a_typed_skip_row(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        """A real directory sits at the runtime ``dst`` — the exact state the
        ``target_conflict`` demotion would misread. The row must carry the swap
        code instead."""
        _skill(d5_store, "demo-skill")
        runtime_root = d5_proj / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        p = _row_4(runtime_root, "demo-skill")
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/sync")

        assert resp.status_code == 200, resp.text
        body = resp.json()
        rows = [r for r in body["skipped"] if r["reason_code"] == "swap_recovery_pending"]
        assert rows, body["skipped"]
        assert "interrupted directory swap" in rows[0]["reason"]
        _assert_no_demotion(resp.text)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"

    async def test_row_2_converges_and_the_push_lands(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        """Positive control: a recoverable state is converged, then pushed over."""
        _skill(d5_store, "demo-skill", "---\nname: demo-skill\n---\ncanonical\n")
        runtime_root = d5_proj / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        p = _row_2(runtime_root, "demo-skill")

        resp = await d5_client.post("/api/context/skills/sync")

        assert resp.status_code == 200, resp.text
        assert resp.json()["generated"], resp.text
        assert _residue(runtime_root) == []
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8").endswith("canonical\n")


class TestWebImportRoutes:
    """The shared ``_import_payload`` formatter, via all three endpoints."""

    async def test_bulk_import_row_4_skips_as_canonical_exists(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        _skill(d5_proj / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _row_4(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/import")

        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json()["skipped"] if r["reason_code"] == "canonical_exists"]
        assert rows, resp.text
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before

    async def test_bulk_import_broken_marker_skips_as_swap_recovery_pending(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        _skill(d5_proj / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _broken_marker(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/import")

        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json()["skipped"] if r["reason_code"] == "swap_recovery_pending"]
        assert rows, resp.text
        assert "swap marker" in rows[0]["reason"]
        _assert_no_demotion(resp.text)
        assert _wedge_state(p) == before
        assert p["marker"].read_text(encoding="utf-8") == "not json at all"

    async def test_single_import_carries_the_same_typed_row(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        _skill(d5_proj / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _broken_marker(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/skill/import")

        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json()["skipped"] if r["reason_code"] == "swap_recovery_pending"]
        assert rows, resp.text
        _assert_no_demotion(resp.text)
        assert _wedge_state(p) == before

    async def test_single_import_row_4_skips_as_canonical_exists(
        self, d5_client: AsyncClient, d5_proj: Path, d5_store: Path
    ) -> None:
        """The pre-lock direction, pinned per endpoint (Codex review): row 4's
        real canonical is what the presence pre-check refuses on."""
        _skill(d5_proj / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _row_4(d5_store)
        before = _wedge_state(p)

        resp = await d5_client.post("/api/context/skills/skill/import")

        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json()["skipped"] if r["reason_code"] == "canonical_exists"]
        assert rows, resp.text
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before

    async def test_import_to_user_reports_the_wedged_user_store(
        self, d5_client: AsyncClient, d5_proj: Path, d5_home: Path
    ) -> None:
        """The user-tier variant: the wedge sits in ``~/.memtomem/skills`` —
        which only a HOME-separate fixture can distinguish from the project."""
        _skill(d5_proj / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        user_store = d5_home / ".memtomem" / "skills"
        user_store.mkdir(parents=True)
        p = _broken_marker(user_store)
        before = _wedge_state(p)

        resp = await d5_client.post(
            "/api/context/skills/skill/import-to-user",
            json={"allow_host_writes": True},
        )

        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json()["skipped"] if r["reason_code"] == "swap_recovery_pending"]
        assert rows, resp.text
        _assert_no_demotion(resp.text)
        assert _wedge_state(p) == before

    async def test_import_to_user_row_4_skips_as_canonical_exists(
        self, d5_client: AsyncClient, d5_proj: Path, d5_home: Path
    ) -> None:
        """Same pre-lock pin for the user-tier endpoint (Codex review)."""
        _skill(d5_proj / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        user_store = d5_home / ".memtomem" / "skills"
        user_store.mkdir(parents=True)
        p = _row_4(user_store)
        before = _wedge_state(p)

        resp = await d5_client.post(
            "/api/context/skills/skill/import-to-user",
            json={"allow_host_writes": True},
        )

        assert resp.status_code == 200, resp.text
        rows = [r for r in resp.json()["skipped"] if r["reason_code"] == "canonical_exists"]
        assert rows, resp.text
        assert "swap_recovery_pending" not in resp.text
        assert _wedge_state(p) == before


class TestMcpMigrate:
    """``mem_context_artifact_migrate`` — prefix-coded, before the ClickException arm."""

    async def test_row_4_source_returns_swap_recovery_pending(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_artifact_migrate

        home = _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        p = _row_4(store)
        before = _wedge_state(p)

        out = await mem_context_artifact_migrate(
            asset_type="skills", name="skill", to_scope="user", apply=True
        )

        assert out.startswith("refused: swap_recovery_pending:"), out
        _assert_no_demotion(out)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert not (home / ".memtomem" / "skills" / "skill").exists()


class TestMcpPull:
    """``mem_context_pull`` — the swap status rides ``error:``, NOT ``refused:``.

    Pinned classification (#1917 `_PULL_ERROR_STATUSES`): recovery needs an
    operator, so it sits with the errors; asserting the prefix keeps a future
    refactor from silently re-bucketing it.
    """

    async def test_row_4_without_overwrite_refuses_as_canonical_exists(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_pull

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        seed_multi_runtime(project, "skills", "skill", {"claude": "---\nname: skill\n---\nnew\n"})
        p = _row_4(store)
        before = _wedge_state(p)

        out = await mem_context_pull(
            kind="skills",
            name="skill",
            scope="project_shared",
            apply=True,
            confirm_project_shared=True,
        )

        assert out.startswith("refused: "), out
        assert "swap_recovery_pending" not in out
        assert _wedge_state(p) == before

    async def test_overwrite_pull_surfaces_the_wedge_as_error(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_pull

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        seed_multi_runtime(project, "skills", "skill", {"claude": "---\nname: skill\n---\nnew\n"})
        p = _row_4(store)
        before = _wedge_state(p)

        out = await mem_context_pull(
            kind="skills",
            name="skill",
            scope="project_shared",
            apply=True,
            confirm_project_shared=True,
            overwrite=True,
        )

        assert out.startswith("error: "), out
        assert not out.startswith("refused:"), out
        assert "interrupted directory swap" in out
        _assert_no_demotion(out)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert not (p["dst"] / "versions").exists(), "refusal must precede the snapshot"


class TestMcpInitExtract:
    """``mem_context_init`` — the extract engine's typed skips, prefix intact."""

    async def test_broken_marker_is_a_skipped_row_not_a_blocked_one(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_init

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        _skill(project / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _broken_marker(store)
        before = _wedge_state(p)

        out = await mem_context_init(include="skills")

        assert "skipped skill:" in out, out
        assert "swap marker" in out, out
        assert "blocked skill" not in out, "a wedge is not a privacy block"
        _assert_no_demotion(out)
        assert str(store) not in out, "MCP reasons are redacted"
        assert _wedge_state(p) == before
        assert p["marker"].read_text(encoding="utf-8") == "not json at all"

    async def test_row_4_skips_as_canonical_exists(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_init

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        _skill(project / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _row_4(store)
        before = _wedge_state(p)

        out = await mem_context_init(include="skills")

        assert "skipped skill: canonical exists" in out, out
        assert "swap" not in out.lower(), out
        assert _wedge_state(p) == before


class TestMcpGenerateAndSync:
    """``mem_context_generate`` / ``mem_context_sync`` — two separate formatting
    sites of the same batch engine (the code is dropped by design; the WORDING
    is the contract these surfaces keep)."""

    def _seed(self, project: Path, store: Path) -> dict[str, Path]:
        (project / ".memtomem" / "context.md").write_text("# ctx\n", encoding="utf-8")
        _skill(store, "demo-skill")
        runtime_root = project / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        return _row_4(runtime_root, "demo-skill")

    async def test_generate_reports_the_typed_skip_wording(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_generate

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        p = self._seed(project, store)
        before = _wedge_state(p)

        out = await mem_context_generate(include="skills")

        assert "skipped demo-skill:" in out, out
        assert "interrupted directory swap" in out, out
        _assert_no_demotion(out)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"

    async def test_sync_reports_the_typed_skip_wording(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from memtomem.server.tools.context import mem_context_sync

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        p = self._seed(project, store)
        before = _wedge_state(p)

        out = await mem_context_sync(include="skills")

        assert "skipped demo-skill:" in out, out
        assert "interrupted directory swap" in out, out
        _assert_no_demotion(out)
        assert _wedge_state(p) == before


class TestCliSyncSkipRow:
    """``mm context sync --include skills`` — the fan-out skip row, paths verbatim."""

    def test_wedged_runtime_destination_is_a_yellow_skip_row(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        (project / ".memtomem" / "context.md").write_text("## Notes\n\nctx\n", encoding="utf-8")
        _skill(store, "demo-skill")
        runtime_root = project / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        p = _row_4(runtime_root, "demo-skill")
        before = _wedge_state(p)

        result = CliRunner().invoke(context, ["sync", "--include", "skills", "-y"])

        assert result.exit_code == 0, result.output
        row = _surface_rows(result.output, "skipped demo-skill:")
        assert "interrupted directory swap" in row
        _assert_no_demotion(row)
        # The CLI is the disclosure surface: the operator gets the real paths.
        assert str(p["dst"]) in row, row
        assert _wedge_state(p) == before


class TestCliInstallAll:
    """``mm context install --all`` — its own batch arm, red row + exit 1."""

    def test_broken_marker_fails_the_row_with_the_typed_wording(
        self, wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _wiki_with(wiki_root, "foo")
        proj = tmp_path / "proj"
        proj.mkdir()
        install_skill(proj, "foo")  # the lock.json pin install --all restores
        store = proj / ".memtomem" / "skills"
        shutil.rmtree(store / "foo")
        p = _broken_marker(store, name="foo")
        before = _wedge_state(p)
        monkeypatch.chdir(proj)

        result = CliRunner().invoke(context, ["install", "--all", "--yes"])

        assert result.exit_code == 1, result.output
        assert "✗ skills/foo: interrupted directory swap in the Store — " in result.output
        assert "swap marker" in result.output
        _assert_no_demotion(result.output)
        assert _wedge_state(p) == before
        assert p["marker"].read_text(encoding="utf-8") == "not json at all"


class TestCliPull:
    """``mm context pull`` — prepare-to-commit race + the overwrite variant."""

    @pytest.fixture
    def pull_proj(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> Path:
        _isolate_home(monkeypatch, tmp_path / "home")
        (project / ".git").mkdir()  # --scope=project_shared requires a project root
        monkeypatch.chdir(project)
        seed_multi_runtime(project, "skills", "skill", {"claude": "---\nname: skill\n---\nnew\n"})
        return project

    def test_row_4_without_overwrite_is_the_canonical_exists_refusal(
        self, pull_proj: Path, store: Path
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        p = _row_4(store)
        before = _wedge_state(p)

        result = CliRunner().invoke(
            context, ["pull", "skills", "skill", "--apply", "--scope", "project_shared", "--yes"]
        )

        assert result.exit_code == 1, result.output
        assert "swap" not in result.output.lower(), result.output
        assert _wedge_state(p) == before

    def test_a_wedge_landing_between_preview_and_apply_is_the_typed_one_liner(
        self, pull_proj: Path, store: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The R3-6 race: prepare sees a clean Store, the wedge lands while the
        user reads the confirmation prompt, commit's prelude refuses. The wedge
        is REAL disk state — the patched prompt only controls its timing."""
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        planted: dict[str, Path] = {}
        wedge_before: list[object] = []

        def _confirm_and_wedge(*args: object, **kwargs: object) -> bool:
            planted.update(_row_4(store))
            wedge_before.append(_wedge_state(planted))
            return True

        monkeypatch.setattr("click.confirm", _confirm_and_wedge)

        result = CliRunner().invoke(
            context, ["pull", "skills", "skill", "--apply", "--scope", "project_shared"]
        )

        assert planted, "the confirmation prompt never ran"
        assert result.exit_code == 1, result.output
        assert "interrupted directory swap" in result.output
        _assert_no_demotion(result.output)
        assert _wedge_state(planted) == wedge_before[0], "the wedge is exactly as planted"

    def test_overwrite_pull_refuses_before_the_snapshot(self, pull_proj: Path, store: Path) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _skill(store, "skill", "---\nname: skill\n---\ncanonical\n")
        p = _row_4_around(store, "skill")
        before = _wedge_state(p)

        result = CliRunner().invoke(
            context,
            [
                "pull",
                "skills",
                "skill",
                "--apply",
                "--overwrite",
                "--scope",
                "project_shared",
                "--yes",
            ],
        )

        assert result.exit_code == 1, result.output
        assert "interrupted directory swap" in result.output
        _assert_no_demotion(result.output)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8").endswith("canonical\n"), (
            "the refused overwrite must leave the canonical untouched"
        )
        assert not (p["dst"] / "versions").exists(), "refusal must precede the snapshot"


class TestCliScopeMigrate:
    """``mm context migrate --to`` — ``TransferRecoveryError`` IS a ClickException;
    Click renders the one-liner with no arm in ``_translate_to_click`` needed."""

    def test_row_4_source_is_the_one_line_recovery_error(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        home = _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        p = _row_4(store)
        before = _wedge_state(p)

        result = CliRunner().invoke(
            context, ["migrate", "skills", "skill", "--to", "user", "--apply", "-y"]
        )

        assert result.exit_code == 1, result.output
        assert "interrupted directory swap left two candidate trees" in result.output
        _assert_no_demotion(result.output)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert not (home / ".memtomem" / "skills" / "skill").exists()


class TestCliTransfer:
    """``mm context copy`` cross-project — same ClickException pass-through."""

    def test_row_4_source_is_the_one_line_recovery_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context
        from memtomem.context.projects import KnownProjectsStore

        _isolate_home(monkeypatch, tmp_path / "home")
        proj_a, proj_b = tmp_path / "proj-a", tmp_path / "proj-b"
        for proj in (proj_a, proj_b):
            (proj / ".git").mkdir(parents=True)
            (proj / ".memtomem").mkdir()
        kp = tmp_path / "known_projects.json"

        class _FakeCfg:
            known_projects_path = kp
            experimental_claude_projects_scan = False
            auto_display_configured_projects = True

        monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
        monkeypatch.chdir(proj_a)
        KnownProjectsStore(kp).add(proj_b)
        store = proj_a / ".memtomem" / "skills"
        store.mkdir()
        p = _row_4(store)
        before = _wedge_state(p)

        result = CliRunner().invoke(
            context,
            [
                "copy",
                "skills",
                "skill",
                "--to-project",
                str(proj_b),
                "--to",
                "project_shared",
                "--apply",
                "--confirm-project-shared",
                "-y",
            ],
        )

        assert result.exit_code == 1, result.output
        assert "interrupted directory swap left two candidate trees" in result.output
        _assert_no_demotion(result.output)
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
        assert not (proj_b / ".memtomem" / "skills" / "skill").exists()


class TestCliUpdateAll:
    """``mm context update <asset> --all`` — wedged project + clean sibling."""

    def test_wedged_project_fails_its_row_and_the_clean_sibling_completes(
        self, wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _wiki_with(wiki_root, "foo")
        proj_a, proj_b = tmp_path / "proj-a", tmp_path / "proj-b"
        proj_a.mkdir()
        proj_b.mkdir()
        install_skill(proj_a, "foo")
        install_skill(proj_b, "foo")
        _wiki_commit(wiki_root, "foo", b"# newer\n")
        store_a = proj_a / ".memtomem" / "skills"
        p = _row_4_around(store_a, "foo")
        before = _wedge_state(p)

        kp = tmp_path / "known.json"
        kp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": [
                        {"root": str(r), "added_at": "2026-01-01T00:00:00.000000Z", "label": None}
                        for r in (proj_a, proj_b)
                    ],
                }
            ),
            encoding="utf-8",
        )

        class _FakeCfg:
            known_projects_path = kp

        monkeypatch.setattr("memtomem.cli.context_cmd.ContextGatewayConfig", lambda: _FakeCfg())
        monkeypatch.chdir(proj_a)

        result = CliRunner().invoke(context, ["update", "skill", "foo", "--all", "--yes"])

        assert result.exit_code == 1, result.output
        assert f"✗ {proj_a}: interrupted directory swap in the Store — " in result.output
        assert f"✓ {proj_b}: updated" in result.output, "the clean row must still complete"
        _assert_no_demotion(result.output)
        assert "Summary: 1 updated, 1 failed" in result.output
        assert _wedge_state(p) == before
        # The wedged project's tree is exactly the original install.
        assert (p["dst"] / SKILL_MANIFEST).read_bytes() == b"# from wiki\n"
        assert (proj_b / ".memtomem" / "skills" / "foo" / SKILL_MANIFEST).read_bytes() == (
            b"# newer\n"
        )


class TestCliInitSkills:
    """``mm context init --include skills`` — both directions of the caveat."""

    def test_broken_marker_is_a_skip_row_with_the_verbatim_path(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        _skill(project / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _broken_marker(store)
        before = _wedge_state(p)

        result = CliRunner().invoke(context, ["init", "--include", "skills"])

        assert result.exit_code == 0, result.output
        row = _surface_rows(result.output, "skipped skill:")
        assert "swap marker" in row
        _assert_no_demotion(row)
        assert str(p["marker"]) in row, "the operator needs the path to inspect"
        assert _wedge_state(p) == before

    def test_row_4_skips_as_canonical_exists(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context

        _isolate_home(monkeypatch, tmp_path / "home")
        monkeypatch.chdir(project)
        _skill(project / ".claude" / "skills", "skill", "---\nname: skill\n---\nruntime\n")
        p = _row_4(store)
        before = _wedge_state(p)

        result = CliRunner().invoke(context, ["init", "--include", "skills"])

        assert result.exit_code == 0, result.output
        assert "skipped skill: canonical exists" in result.output
        assert "swap" not in result.output.lower(), result.output
        assert _wedge_state(p) == before


class TestCliSyncAllProjects:
    """``mm context sync --all-projects`` — the wedge is a leg-level skip row;
    the project row itself SUCCEEDS (the engine converted it before the batch's
    failure arms could see it) and the clean sibling pushes."""

    def test_wedged_project_reports_the_row_and_the_batch_stays_green(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from click.testing import CliRunner

        from memtomem.cli.context_cmd import context
        from memtomem.config import ContextGatewayConfig

        _isolate_home(monkeypatch, tmp_path / "home")
        kp = tmp_path / "kp.json"
        monkeypatch.setattr(
            "memtomem.cli.context_cmd.ContextGatewayConfig",
            lambda: ContextGatewayConfig(
                known_projects_path=kp,
                experimental_claude_projects_scan=False,
            ),
        )

        def _proj(name: str) -> Path:
            root = tmp_path / name
            (root / ".claude").mkdir(parents=True)
            (root / ".memtomem").mkdir()
            _skill(root / ".memtomem" / "skills", "demo-skill")
            return root

        proj_a, proj_b = _proj("proj-a"), _proj("proj-b")
        kp.write_text(
            json.dumps(
                {
                    "version": 1,
                    "projects": [
                        {
                            "root": str(r),
                            "added_at": "2026-01-01T00:00:00Z",
                            "label": None,
                            "enabled": True,
                        }
                        for r in (proj_a, proj_b)
                    ],
                }
            ),
            encoding="utf-8",
        )
        runtime_root = proj_a / ".claude" / "skills"
        runtime_root.mkdir(parents=True)
        p = _row_4(runtime_root, "demo-skill")
        before = _wedge_state(p)
        monkeypatch.chdir(proj_a)

        result = CliRunner().invoke(
            context, ["sync", "--all-projects", "--include", "skills", "--yes"]
        )

        assert result.exit_code == 0, result.output
        row = _surface_rows(result.output, "skipped demo-skill:")
        assert "interrupted directory swap" in row
        _assert_no_demotion(row)
        assert "Summary: 2 pushed, 0 failed" in result.output
        assert (proj_b / ".claude" / "skills" / "demo-skill" / SKILL_MANIFEST).is_file(), (
            "the clean sibling must still push"
        )
        assert _wedge_state(p) == before
        assert (p["dst"] / SKILL_MANIFEST).read_text(encoding="utf-8") == "candidate-a"
