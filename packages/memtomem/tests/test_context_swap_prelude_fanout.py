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
import shutil
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context._dir_swap import SwapForeignDestination, SwapRecoveryError, has_pending_swap
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

from .helpers import set_home

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


def _assert_converged(root: Path, dst: Path, content: str) -> None:
    assert dst.is_dir(), f"{dst} did not converge to a directory"
    assert (dst / SKILL_MANIFEST).read_text(encoding="utf-8") == content
    assert _residue(root) == [], "recovery left a marker or transient behind"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / ".memtomem" / "skills").mkdir(parents=True)
    return tmp_path


@pytest.fixture
def store(project: Path) -> Path:
    return project / ".memtomem" / "skills"


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
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        p = _row_2(store)

        self._transfer(project)

        assert not p["dst"].exists(), "the move should have consumed the recovered tree"
        assert _residue(store) == [], "recovery left transients behind"

    def test_source_row_5_is_recovered_and_moved(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        p = _row_5(store)

        self._transfer(project)

        assert not p["dst"].exists()
        assert _residue(store) == []

    def test_source_row_4_refuses_as_swap_recovery_pending(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Reachable here — the source tree is what discovery found, not a collision."""
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
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
        monkeypatch.setenv("HOME", str(home))
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
        monkeypatch.setenv("HOME", str(home))
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

    def test_a_recovered_tree_without_its_manifest_is_not_transferred(
        self, project: Path, store: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """Recovery authenticates the marker↔path relation, never the payload.

        A row-5 pre-image mangled by an out-of-band writer converges to a
        directory with no ``SKILL.md``. The in-lock re-check repeats the FULL
        discovery contract, so that is a not-found, not a transfer of a
        half-artifact.
        """
        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        p = _write_marker(store)
        p["old"].mkdir()  # a tree, but not a skill

        with pytest.raises(ArtifactNotFoundError):
            self._transfer(project)


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
        _row_4(web_store)
        before = _residue(web_store)

        resp = await client.post(
            "/api/context/skills", json={"name": "skill", "content": "# skill\n"}
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "already_exists"
        assert _residue(web_store) == before, "a pre-lock refusal must reap nothing"

    async def test_delete_maps_row_4_to_409_swap_recovery_pending(
        self, client: AsyncClient, web_store: Path
    ) -> None:
        """Delete DOES reach the lock, so it gets the typed refusal."""
        p = _row_4(web_store)
        before = _residue(web_store)

        resp = await client.delete("/api/context/skills/skill")

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "swap_recovery_pending"
        assert _residue(web_store) == before
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
        before = _residue(web_store)

        resp = await client.put(
            "/api/context/skills/skill",
            json={"content": "# edited\n", "mtime_ns": mtime_ns},
        )

        assert resp.status_code == 409
        assert resp.json()["detail"]["reason_code"] == "swap_recovery_pending"
        assert _residue(web_store) == before


class TestWikiInstall:
    """``install.py`` — all three kind-polymorphic sites, each through its own entry point.

    One test per site, deliberately: they are three separate ``with`` bodies
    with three separate in-lock re-checks, so a suite that only ever calls
    ``install_skill`` stays green with either of the other two preludes deleted
    (Codex code gate).
    """

    @staticmethod
    def _wiki_commit(wiki_root_path: Path, name: str, body: bytes) -> None:
        """Commit new bytes for ``name`` so an update has work to do.

        Without a second commit the update returns a no-op BEFORE taking the
        lock (HEAD already matches the recorded pin), and the test would prove
        nothing about the prelude.
        """
        (wiki_root_path / "skills" / name / SKILL_MANIFEST).write_bytes(body)
        subprocess.run(
            ["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(wiki_root_path), "commit", "-m", f"update {name}"],
            check=True,
            capture_output=True,
        )

    @staticmethod
    def _wiki_with(wiki_root_path: Path, name: str) -> None:
        store = WikiStore.at_default()
        store.init()
        skill_dir = wiki_root_path / "skills" / name
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / SKILL_MANIFEST).write_bytes(b"# from wiki\n")
        subprocess.run(
            ["git", "-C", str(wiki_root_path), "add", "."], check=True, capture_output=True
        )
        subprocess.run(
            ["git", "-C", str(wiki_root_path), "commit", "-m", f"add {name}"],
            check=True,
            capture_output=True,
        )

    def test_row_2_is_recovered_before_the_dest_re_check(
        self, wiki_root: Path, tmp_path: Path
    ) -> None:
        """Recovery precedes the in-lock ``dest.exists()`` — which RETURNS on what it sees.

        Without the ordering, the install would classify the destination absent
        (row 2 has no ``dst``), extract over the recovering transaction, and
        leave the next recovery reading a row whose action deletes the
        pre-image.
        """
        self._wiki_with(wiki_root, "foo")
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
        self._wiki_with(wiki_root, "foo")
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
        self._wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")  # records the lock.json entry update needs
        self._wiki_commit(wiki_root, "foo", b"# newer\n")
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
        self._wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")
        self._wiki_commit(wiki_root, "foo", b"# newer\n")
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

    def test_pinned_install_recovers_before_its_own_re_check(
        self, wiki_root: Path, tmp_path: Path
    ) -> None:
        """``_apply_pinned_install`` — the third site, reached via ``install --all``.

        Its own ``with`` body and its own dirty re-classify, so the other two
        preludes prove nothing about it.
        """
        self._wiki_with(wiki_root, "foo")
        project = tmp_path / "proj"
        project.mkdir()
        install_skill(project, "foo")
        self._wiki_commit(wiki_root, "foo", b"# newer\n")
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

        monkeypatch.setenv("HOME", str(tmp_path / "home"))
        monkeypatch.chdir(project)
        _row_4(store)

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
        monkeypatch.setenv("HOME", str(home))
        monkeypatch.setenv("USERPROFILE", str(home))
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
        _row_4(skills, name=SKILL_OUT_OF_SYNC)

        result = CliRunner().invoke(context, ["seed-validation", str(target), "--force"])

        assert result.exit_code == 1
        assert "interrupted directory swap" in result.output
        assert "Traceback" not in result.output
