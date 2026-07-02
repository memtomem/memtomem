"""HTTP-layer tests for the dev-tier wiki install/update routes (ADR-0008 PR-E E-3).

Covers ``POST /api/context/{type}/{name}/{install,update}`` — the web parity of
``mm context install`` / ``mm context update``. Unlike the wiki override-seed
(``wiki_mutations.py``, host-global), these write into a *project's*
``.memtomem/`` tree, so the tests wire up both a real wiki (``wiki_root`` →
``MEMTOMEM_WIKI_PATH``) and a project root (``app.state.project_root``).

The load-bearing cases: the install/update lifecycle (fresh / already-installed
/ no-op / behind / dirty-refuse / force+``.bak`` / never-installed), the
fixed-message envelopes that must NOT leak the absolute wiki/dest path (Codex
review), the Gate-A privacy block, the sync-paused 409 **plus the
``target_scope=user`` bypass regression** (the pinned resolver must ignore the
inherited tier query), and the dev-only mount (absent in prod).

``wiki_root`` / ``git_identity`` come from ``_wiki_fixtures`` via conftest.
"""

from __future__ import annotations

import asyncio
import subprocess
import threading
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context.lockfile import Lockfile
from memtomem.web.app import create_app
from memtomem.wiki.store import WikiStore

_SKILL_BODY = "# Alpha\n\nBody.\n"
_AGENT_BODY = "---\nname: beta\ndescription: a test agent\n---\n\nBody.\n"
_CMD_BODY = "---\ndescription: a test command\n---\n\nBody.\n"
# AKIA + 16 [0-9A-Z] → matches privacy.DEFAULT_PATTERNS (AWS access key id).
_SECRET = "AKIAIOSFODNN7EXAMPLE"


# ── git / wiki seeding ─────────────────────────────────────────────────────


def _commit(root: Path, message: str) -> None:
    subprocess.run(["git", "-C", str(root), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(root), "commit", "-m", message], check=True, capture_output=True
    )


def _seed_wiki(root: Path) -> None:
    """Init a wiki with a canonical skill / agent / command (no overrides)."""
    WikiStore.at_default().init()
    (root / "skills" / "alpha").mkdir(parents=True)
    (root / "skills" / "alpha" / "SKILL.md").write_text(_SKILL_BODY, encoding="utf-8")
    (root / "agents" / "beta").mkdir(parents=True)
    (root / "agents" / "beta" / "agent.md").write_text(_AGENT_BODY, encoding="utf-8")
    (root / "commands" / "gamma").mkdir(parents=True)
    (root / "commands" / "gamma" / "command.md").write_text(_CMD_BODY, encoding="utf-8")
    _commit(root, "seed")


def _advance_wiki(root: Path) -> None:
    """Edit the canonical skill and commit so wiki HEAD moves past the pin."""
    (root / "skills" / "alpha" / "SKILL.md").write_text(
        _SKILL_BODY + "\nUpstream change.\n", encoding="utf-8"
    )
    _commit(root, "advance alpha")


# ── fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def seeded_wiki(wiki_root: Path) -> Path:  # noqa: F811 — wiki_root from conftest
    _seed_wiki(wiki_root)
    return wiki_root


@pytest.fixture
def project_root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    (root / ".memtomem").mkdir(parents=True)
    (root / ".claude").mkdir()
    return root


@pytest.fixture
def known_projects_path(tmp_path: Path) -> Path:
    return tmp_path / "kp.json"


def _make_app(project_root: Path, known_projects_path: Path, *, mode: str = "dev"):
    application = create_app(lifespan=None, mode=mode)
    application.state.project_root = project_root
    application.state.storage = AsyncMock()
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=known_projects_path,
        experimental_claude_projects_scan=False,
    )
    application.state.config = config
    return application


@pytest.fixture
async def client(project_root: Path, known_projects_path: Path):
    app = _make_app(project_root, known_projects_path)
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
async def prod_client(project_root: Path, known_projects_path: Path):
    app = _make_app(project_root, known_projects_path, mode="prod")
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _lock_entry(project_root: Path, asset_type: str, name: str) -> dict | None:
    return Lockfile.at(project_root).read_entry(asset_type, name)


# ── install lifecycle ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_fresh(client, seeded_wiki, project_root: Path) -> None:
    resp = await client.post("/api/context/skills/alpha/install")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["installed"] is True
    assert data["asset_type"] == "skills"
    assert data["name"] == "alpha"
    assert data["dest"] == ".memtomem/skills/alpha"  # POSIX, project-relative
    assert data["files_written"] >= 1
    # The lockfile records the install.
    assert _lock_entry(project_root, "skills", "alpha") is not None
    # Bytes landed.
    landed = project_root / ".memtomem" / "skills" / "alpha" / "SKILL.md"
    assert landed.read_text(encoding="utf-8") == _SKILL_BODY


@pytest.mark.asyncio
async def test_install_already_installed_is_409(client, seeded_wiki, project_root: Path) -> None:
    assert (await client.post("/api/context/skills/alpha/install")).status_code == 200
    resp = await client.post("/api/context/skills/alpha/install")
    assert resp.status_code == 409
    body = resp.json()
    assert body["detail"]["reason_code"] == "already_installed"
    # No absolute project/wiki path leaks into the envelope (Codex review).
    assert str(project_root) not in resp.text


@pytest.mark.asyncio
async def test_install_agent_and_command(client, seeded_wiki, project_root: Path) -> None:
    assert (await client.post("/api/context/agents/beta/install")).status_code == 200
    assert (await client.post("/api/context/commands/gamma/install")).status_code == 200
    assert _lock_entry(project_root, "agents", "beta") is not None
    assert _lock_entry(project_root, "commands", "gamma") is not None


@pytest.mark.asyncio
async def test_install_project_root_deleted_is_404(
    client, seeded_wiki, project_root: Path, monkeypatch
) -> None:
    # #1385 finding 4: the project root is deleted in the TOCTOU window between
    # dependency resolution and the to_thread engine call, so the engine's
    # ``is_dir()`` guard raises a BARE FileNotFoundError. The handler must map
    # it to the fixed 404 envelope (no FastAPI default 500), with no absolute
    # project path in the body.
    import shutil

    import memtomem.web.routes.context_mutations as cm

    original = cm._INSTALLERS["skills"]

    def _delete_then_install(*args, **kwargs):
        shutil.rmtree(project_root, ignore_errors=True)
        return original(*args, **kwargs)

    monkeypatch.setitem(cm._INSTALLERS, "skills", _delete_then_install)
    resp = await client.post("/api/context/skills/alpha/install")

    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert detail["reason_code"] == "project_root_missing"
    assert detail["message"] == "skills/alpha: destination project no longer exists"
    # Fixed message — neither the absolute project path nor the raw engine
    # ``str(exc)`` ("project root does not exist: ...") reaches the envelope.
    assert str(project_root) not in resp.text
    assert "project root does not exist" not in resp.text


@pytest.mark.asyncio
async def test_update_project_root_deleted_is_404(
    client, seeded_wiki, project_root: Path, monkeypatch
) -> None:
    # Same TOCTOU on the update path: the ``is_dir()`` guard fires before the
    # not-installed check, so no prior install is needed to reach it.
    import shutil

    import memtomem.web.routes.context_mutations as cm

    original = cm._UPDATERS["skills"]

    def _delete_then_update(*args, **kwargs):
        shutil.rmtree(project_root, ignore_errors=True)
        return original(*args, **kwargs)

    monkeypatch.setitem(cm._UPDATERS, "skills", _delete_then_update)
    resp = await client.post("/api/context/skills/alpha/update")

    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert detail["reason_code"] == "project_root_missing"
    assert detail["message"] == "skills/alpha: destination project no longer exists"
    assert str(project_root) not in resp.text
    assert "project root does not exist" not in resp.text


@pytest.mark.asyncio
async def test_non_guard_filenotfound_is_not_project_root_missing(
    client, seeded_wiki, project_root: Path, monkeypatch
) -> None:
    # #1385 finding 4 (Codex gate): ONLY the engine's project-root is_dir guard
    # (ProjectRootMissingError) maps to 404 project_root_missing. A BARE
    # FileNotFoundError from a later source-walk / copy race (iter_installed_files,
    # copy_tree_atomic, installed_at_from_dest) must NOT be mislabeled — it falls
    # through to the generic 500, not a clean "destination project no longer
    # exists" 404. Pins the fix away from the original broad ``except
    # FileNotFoundError``, which WOULD have caught this and mislabeled it.
    import memtomem.web.routes.context_mutations as cm

    def _raise_plain_fnf(*args, **kwargs):
        raise FileNotFoundError("wiki source file vanished mid-copy")

    monkeypatch.setitem(cm._INSTALLERS, "skills", _raise_plain_fnf)
    # The plain FNF is not ProjectRootMissingError, so the route does NOT catch
    # it — it propagates (a generic 500 in production) rather than being
    # mislabeled as a clean 404 project_root_missing. The original broad
    # ``except FileNotFoundError`` WOULD have caught and mislabeled it, so this
    # ``raises`` (the FNF reaches the test transport) would fail pre-fix.
    with pytest.raises(FileNotFoundError, match="vanished mid-copy"):
        await client.post("/api/context/skills/alpha/install")


# ── update lifecycle ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_update_no_op(client, seeded_wiki) -> None:
    assert (await client.post("/api/context/skills/alpha/install")).status_code == 200
    resp = await client.post("/api/context/skills/alpha/update")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["updated"] is True
    assert data["was_no_op"] is True
    assert data["files_written"] == 0


@pytest.mark.asyncio
async def test_update_behind_advances_pin(client, seeded_wiki: Path, project_root: Path) -> None:
    assert (await client.post("/api/context/skills/alpha/install")).status_code == 200
    old_pin = _lock_entry(project_root, "skills", "alpha")["wiki_commit"]
    _advance_wiki(seeded_wiki)
    resp = await client.post("/api/context/skills/alpha/update")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["was_no_op"] is False
    assert data["new_wiki_commit"] != old_pin
    assert data["files_written"] >= 1
    # The new bytes landed.
    landed = project_root / ".memtomem" / "skills" / "alpha" / "SKILL.md"
    assert "Upstream change." in landed.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_update_dirty_without_force_is_409(
    client, seeded_wiki: Path, project_root: Path
) -> None:
    assert (await client.post("/api/context/skills/alpha/install")).status_code == 200
    _advance_wiki(seeded_wiki)
    # Local edit makes the dest dirty (byte mismatch vs recorded digest).
    landed = project_root / ".memtomem" / "skills" / "alpha" / "SKILL.md"
    landed.write_text(_SKILL_BODY + "\nLocal edit.\n", encoding="utf-8")
    resp = await client.post("/api/context/skills/alpha/update")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason_code"] == "stale_install"


@pytest.mark.asyncio
async def test_update_dirty_with_force_writes_bak(
    client, seeded_wiki: Path, project_root: Path
) -> None:
    assert (await client.post("/api/context/skills/alpha/install")).status_code == 200
    _advance_wiki(seeded_wiki)
    landed = project_root / ".memtomem" / "skills" / "alpha" / "SKILL.md"
    landed.write_text(_SKILL_BODY + "\nLocal edit.\n", encoding="utf-8")
    resp = await client.post("/api/context/skills/alpha/update", json={"force": True})
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["was_no_op"] is False
    assert data["bak_file_count"] >= 1
    # Upstream bytes won; the edit is preserved in a .bak sibling.
    assert "Upstream change." in landed.read_text(encoding="utf-8")
    bak = landed.with_suffix(".md.bak")
    assert "Local edit." in bak.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_update_never_installed_is_404(client, seeded_wiki) -> None:
    resp = await client.post("/api/context/skills/alpha/update")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "not_installed"


# ── lock budget / orphaned-worker guard (Codex impl review) ────────────────


@pytest.mark.asyncio
async def test_install_lock_timeout_is_503_no_orphan_write(
    client, seeded_wiki, project_root: Path, monkeypatch
) -> None:
    """A contended lockfile lock makes the worker self-abort INSIDE the request
    window: the route returns 503 and — crucially — no orphaned thread writes
    the lockfile after the held lock is later released."""
    import memtomem.web.routes.context_mutations as cm
    from memtomem.context._atomic import _file_lock, _lock_path_for

    monkeypatch.setattr(cm, "_INSTALL_LOCK_BUDGET_S", 0.2)
    lock_path = _lock_path_for(project_root / ".memtomem" / "lock.json")

    held = threading.Event()
    release = threading.Event()

    def _hold() -> None:
        with _file_lock(lock_path):
            held.set()
            release.wait(timeout=10)

    holder = threading.Thread(target=_hold)
    holder.start()
    try:
        assert held.wait(timeout=5)
        resp = await client.post("/api/context/skills/alpha/install")
    finally:
        release.set()
        holder.join(timeout=5)

    assert resp.status_code == 503
    # Releasing the lock the (now-dead) worker was blocked on must NOT resurrect
    # a late lockfile write — the bounded budget already aborted it.
    await asyncio.sleep(0.3)
    assert _lock_entry(project_root, "skills", "alpha") is None


# ── error envelopes (no path leak) ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_install_missing_asset_is_404(client, seeded_wiki: Path) -> None:
    resp = await client.post("/api/context/skills/nope/install")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "asset_absent"
    # The wiki root must not leak into the envelope.
    assert str(seeded_wiki) not in resp.text


@pytest.mark.asyncio
async def test_install_wiki_absent_is_404(client, wiki_root: Path) -> None:  # noqa: F811
    # wiki_root sets MEMTOMEM_WIKI_PATH but we never init → no wiki on disk.
    resp = await client.post("/api/context/skills/alpha/install")
    assert resp.status_code == 404
    assert resp.json()["detail"]["reason_code"] == "wiki_absent"
    assert str(wiki_root) not in resp.text


@pytest.mark.asyncio
async def test_install_unborn_wiki_is_409(client, unborn_wiki: Path) -> None:
    # Clone of an empty remote: the asset dir exists in the working tree but
    # there is no HEAD to pin — used to escape as a 500 RuntimeError.
    resp = await client.post("/api/context/skills/alpha/install")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason_code"] == "wiki_unborn"
    assert str(unborn_wiki) not in resp.text


@pytest.mark.asyncio
async def test_update_unborn_wiki_is_409(client, seeded_wiki: Path, project_root: Path) -> None:
    # Install from a healthy wiki first, then delete the branch ref so HEAD
    # becomes unborn again (the force-push-to-empty shape).
    resp = await client.post("/api/context/skills/alpha/install")
    assert resp.status_code == 200, resp.text
    subprocess.run(
        ["git", "-C", str(seeded_wiki), "update-ref", "-d", "refs/heads/main"],
        check=True,
        capture_output=True,
    )
    resp = await client.post("/api/context/skills/alpha/update")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason_code"] == "wiki_unborn"
    assert str(seeded_wiki) not in resp.text


@pytest.mark.asyncio
async def test_install_privacy_block_is_422_no_path_leak(
    client,
    wiki_root: Path,
    project_root: Path,  # noqa: F811
) -> None:
    # Seed a wiki asset whose canonical carries a secret-shaped string.
    WikiStore.at_default().init()
    secret_dir = wiki_root / "skills" / "leaky"
    secret_dir.mkdir(parents=True)
    (secret_dir / "SKILL.md").write_text(f"# Leaky\n\naccess key {_SECRET}\n", encoding="utf-8")
    _commit(wiki_root, "seed leaky")
    resp = await client.post("/api/context/skills/leaky/install")
    assert resp.status_code == 422
    assert resp.json()["detail"]["reason_code"] == "privacy_blocked"
    # Neither the wiki path nor the secret value may appear in the envelope.
    assert str(wiki_root) not in resp.text
    assert _SECRET not in resp.text
    # Refusal left no residue in the project tree.
    assert not (project_root / ".memtomem" / "skills" / "leaky").exists()


@pytest.mark.asyncio
async def test_invalid_name_is_400(client, seeded_wiki) -> None:
    resp = await client.post("/api/context/skills/-bad/install")
    assert resp.status_code == 400
    assert resp.json()["detail"]["reason_code"] == "invalid_name"


@pytest.mark.asyncio
async def test_unknown_asset_type_is_422(client, seeded_wiki) -> None:
    resp = await client.post("/api/context/widgets/alpha/install")
    assert resp.status_code == 422


# ── sync-eligibility gate + target_scope bypass regression (Codex Blocker) ──


async def _register_paused(client, root: Path) -> str:
    reg = await client.post("/api/context/known-projects", json={"root": str(root)})
    assert reg.status_code in (200, 201), reg.text
    scope_id = reg.json()["project_scope_id"]
    patched = await client.patch(f"/api/context/known-projects/{scope_id}", json={"enabled": False})
    assert patched.status_code == 200, patched.text
    return scope_id


@pytest.mark.asyncio
async def test_paused_project_install_is_409(client, seeded_wiki, tmp_path: Path) -> None:
    paused = tmp_path / "paused"
    (paused / ".memtomem").mkdir(parents=True)
    scope_id = await _register_paused(client, paused)
    resp = await client.post(f"/api/context/skills/alpha/install?scope_id={scope_id}")
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason_code"] == "sync_paused"


@pytest.mark.asyncio
async def test_paused_project_target_scope_user_still_409(
    client, seeded_wiki, tmp_path: Path
) -> None:
    # The pinned resolver omits target_scope from its signature, so appending
    # ?target_scope=user must NOT reach the gate (which exempts the user tier).
    # Without the fix this would 200 and write project_shared bytes into a
    # paused project (Codex review Blocker).
    paused = tmp_path / "paused"
    (paused / ".memtomem").mkdir(parents=True)
    scope_id = await _register_paused(client, paused)
    resp = await client.post(
        f"/api/context/skills/alpha/install?scope_id={scope_id}&target_scope=user"
    )
    assert resp.status_code == 409
    assert resp.json()["detail"]["reason_code"] == "sync_paused"
    # And nothing was installed into the paused project.
    assert _lock_entry(paused, "skills", "alpha") is None


# ── dev-only mount ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_routes_absent_in_prod(prod_client, seeded_wiki) -> None:
    install = await prod_client.post("/api/context/skills/alpha/install")
    update = await prod_client.post("/api/context/skills/alpha/update")
    assert install.status_code == 404
    assert update.status_code == 404
