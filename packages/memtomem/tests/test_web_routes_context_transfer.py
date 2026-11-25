"""HTTP-layer tests for ``POST /api/context/{kind}/{name}/transfer`` (A-5 #1276).

The engine matrix (cross-project move/copy, Gate A residue, rollback,
provenance carry-over) is pinned by ``test_context_transfer.py``; this file
covers what the WEB surface adds (ADR-0023 §10):

- destination resolution through project discovery (404 unknown,
  409 ``sync_paused`` incl. the implicit same-project destination,
  409 ``no_memtomem_store``);
- the disclose-then-confirm round-trips (Gate B ``confirm_project_shared``,
  user-tier ``allow_host_writes`` with ``host_targets``);
- the object error envelope (``error_kind`` + ``reason_code``) and the
  issue-pinned string 422 for ``PrivacyScanError``;
- ``dry_run`` plan responses and the provenance triple on the wire.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context.lockfile import Lockfile, utcnow_iso8601_z
from memtomem.web.app import create_app
from memtomem.web.routes import _errors as _errors_mod
from .helpers import set_home

_AGENT_BODY = "---\nname: foo\ndescription: a clean test agent\n---\n\nhello\n"
_SECRET_BODY = "---\nname: foo\ndescription: leaky\n---\n\nkey=AKIA1234567890ABCDEF\n"


# ── Fixtures ─────────────────────────────────────────────────────────────


@pytest.fixture
def cwd_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox HOME; return the cwd project root with markers + store."""
    set_home(monkeypatch, tmp_path / "home")
    (tmp_path / "home").mkdir()
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude").mkdir()
    (cwd / ".memtomem").mkdir()
    return cwd


@pytest.fixture
def known_projects_path(tmp_path: Path) -> Path:
    return tmp_path / "kp.json"


@pytest.fixture
def app(cwd_root: Path, known_projects_path: Path):
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = cwd_root
    application.state.storage = AsyncMock()
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=known_projects_path,
        experimental_claude_projects_scan=False,
    )
    application.state.config = config
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    application.state.last_reload_error = None
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _write_agent(store_root: Path, name: str, body: str = _AGENT_BODY) -> Path:
    """Dir-layout canonical agent under *store_root* (an ``agents`` dir)."""
    artifact = store_root / name
    artifact.mkdir(parents=True, exist_ok=True)
    manifest = artifact / "agent.md"
    manifest.write_text(body, encoding="utf-8")
    return manifest


def _shared_agents(root: Path) -> Path:
    return root / ".memtomem" / "agents"


def _local_agents(root: Path) -> Path:
    return root / ".memtomem" / "agents.local"


async def _register(client, root: Path, *, enabled: bool | None = None) -> str:
    """Register *root* as a known project; optionally flip enrollment."""
    resp = await client.post("/api/context/known-projects", json={"root": str(root)})
    assert resp.status_code == 200, resp.text
    scope_id = resp.json()["project_scope_id"]
    if enabled is not None:
        resp = await client.patch(
            f"/api/context/known-projects/{scope_id}", json={"enabled": enabled}
        )
        assert resp.status_code == 200, resp.text
    return scope_id


def _other_project(tmp_path: Path, name: str = "proj-b", *, store: bool = True) -> Path:
    other = tmp_path / name
    other.mkdir()
    (other / ".claude").mkdir()
    if store:
        (other / ".memtomem").mkdir()
    return other


def _collision_message(kind: str, name: str, tier: str) -> str:
    return (
        f"destination already exists: {kind} '{name}' in the {tier} tier of the "
        "destination. Resolve manually or remove the existing entry first."
    )


#: Everything in front of the paths in the engine's ``transfer_staging_busy`` text.
_BUSY_REMEDIATION = (
    "transfer staging is blocked: a name it needs is already taken and was NOT removed, "
    "because that entry can be an interrupted move's only copy of an artifact. Inspect it "
    "by hand, then retry. Occupied: "
)


_BUSY_MESSAGE = (
    "transfer staging is blocked: a name it needs is already taken and was NOT removed, "
    "because that entry can be an interrupted move's only copy of an artifact. Inspect it "
    "by hand, then retry. The server log has the details."
)
_REPLACED_MESSAGE = (
    "transfer staging was replaced out of band, or this filesystem does not report file "
    "identity: nothing was promoted and nothing was removed. Inspect the entry by hand, "
    "then retry; if the filesystem is the cause, stage on one that reports it. The server "
    "log has the details."
)


def _strings(value: object) -> list[str]:
    """Every key and string value of a decoded JSON body, recursively."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (*_strings(k), *_strings(v))]
    if isinstance(value, list):
        return [s for item in value for s in _strings(item)]
    return []


def _assert_path_free(body: object, *leaky: Path) -> None:
    """No decoded string carries a leaky path (given or resolved form) or ``$HOME``.

    Walks the decoded body rather than ``json.dumps`` of it: serialized, a
    Windows path's backslashes are doubled and the raw path never matches.
    """
    forbidden = {str(Path.home())}
    for p in leaky:
        forbidden |= {str(p), str(p.resolve())}
    for s in _strings(body):
        for f in forbidden:
            assert f not in s, (f, s)


# ── apply: cross-project move / copy ────────────────────────────────────


@pytest.mark.asyncio
async def test_cross_project_move_ok(client, cwd_root: Path, tmp_path: Path) -> None:
    """shared→shared move A→B: 200 ok + result fields the UI builds on."""
    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["transferred"] is True
    assert data["mode"] == "move"
    assert data["from_scope"] == "project_shared"
    assert data["to_scope"] == "project_shared"
    # One-click follow-up sync contract: the destination scope_id + the
    # engine's exact cd-prefixed command.
    assert data["dst_project_scope_id"] == scope_b
    assert data["needs_sync"] is True
    assert data["sync_command"].startswith("cd ")
    assert "mm context sync --scope project_shared" in data["sync_command"]
    # Provenance triple is on the wire even in the quiet case.
    assert data["provenance"] == "not_applicable"
    assert data["provenance_reason"] is None
    assert data["provenance_reason_code"] is None

    assert not src_manifest.parent.exists()
    dst_manifest = _shared_agents(other) / "foo" / "agent.md"
    assert dst_manifest.read_text(encoding="utf-8") == _AGENT_BODY


@pytest.mark.asyncio
async def test_cross_project_copy_keeps_source(client, cwd_root: Path, tmp_path: Path) -> None:
    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY
    assert (_shared_agents(other) / "foo" / "agent.md").is_file()


# ── #1310: project_local landing git-protects the DESTINATION ────────────


@pytest.mark.asyncio
async def test_cross_project_local_transfer_appends_dest_gitignore(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """A web transfer landing on project_local git-protects the DESTINATION
    (CLI/MCP parity, #1310): the dest .gitignore gets the marker, the SOURCE
    project is never git-touched, and the response reports ``appended``."""
    from memtomem.cli.context_cmd import _GITIGNORE_MARKER

    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    (other / ".git").mkdir()  # marker is only meaningful in a git working tree
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_local",
            "to_project_scope_id": scope_b,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["to_scope"] == "project_local"
    assert data["gitignore_marker"] == "appended"

    # Canonical landed on the destination's local tier.
    assert (_local_agents(other) / "foo" / "agent.md").is_file()
    # Destination .gitignore now carries the marker block.
    dst_gi = (other / ".gitignore").read_text(encoding="utf-8")
    assert _GITIGNORE_MARKER in dst_gi
    assert ".memtomem/*.local/" in dst_gi
    # The SOURCE project is never git-touched by a destination landing.
    assert not (cwd_root / ".gitignore").exists()
    assert src_manifest.read_text(encoding="utf-8") == _AGENT_BODY  # copy kept source


@pytest.mark.asyncio
async def test_project_local_transfer_without_git_surfaces_skip(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """When the project_local destination has a store but no git signal, the
    marker write is skipped (meaningful only in a git tree) and the skip is
    SURFACED in the response instead of silently dropped (#1310)."""
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)  # .memtomem + .claude, no .git / pyproject
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_local",
            "to_project_scope_id": scope_b,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["gitignore_marker"] == "no_project_signal"
    # No .gitignore conjured in a non-git directory.
    assert not (other / ".gitignore").exists()


@pytest.mark.asyncio
async def test_copy_rename_surfaces_provenance_reason_code(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """A renamed shared→shared copy of a wiki-tracked source never carries
    provenance — the response must surface the typed reason code (the A-4
    ``_skip_reasons`` contract this surface exists to expose)."""
    _write_agent(_shared_agents(cwd_root), "foo")
    Lockfile.at(cwd_root).upsert_entry(
        "agents", "foo", wiki_commit="abc123", installed_at=utcnow_iso8601_z()
    )
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "as_name": "bar",
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["dst_name"] == "bar"
    assert data["provenance"] == "not_carried"
    assert data["provenance_reason_code"] == "renamed_copy"
    assert "lock.json entries are keyed by wiki asset name" in data["provenance_reason"]
    assert (_shared_agents(other) / "bar" / "agent.md").is_file()


# ── destination refusals ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collision_409_destination_exists(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    _write_agent(_shared_agents(other), "foo", "---\nname: foo\n---\n\nresident\n")
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    # The engine names the destination by its absolute path (#3302).
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "destination_exists"
    assert detail["message"] == _collision_message("agents", "foo", "project_shared")


@pytest.mark.asyncio
async def test_collision_409_names_the_renamed_destination(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """``as_name`` is the name that collided, and the tier is the one asked for."""
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    _write_agent(_local_agents(other), "bar", "---\nname: bar\n---\n\nresident\n")
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_local",
            "to_project_scope_id": scope_b,
            "as_name": "bar",
        },
    )
    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "destination_exists"
    assert detail["message"] == _collision_message("agents", "bar", "project_local")


@pytest.mark.asyncio
@pytest.mark.requires_symlinks
async def test_collision_409_in_symlinked_store_is_path_free(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """The engine resolves the destination store, so a store linked out of the
    project names a directory under neither root — the route's own message
    must not depend on stripping it."""
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    external = tmp_path / "external-agents"
    _write_agent(external, "foo", "---\nname: foo\n---\n\nresident\n")
    _shared_agents(other).symlink_to(external, target_is_directory=True)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, external, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "destination_exists"
    assert detail["message"] == _collision_message("agents", "foo", "project_shared")


@pytest.mark.asyncio
async def test_collision_409_user_tier_is_path_free(client, cwd_root: Path, tmp_path: Path) -> None:
    """The user tier resolves under the request-time HOME, which the import-time
    ``$HOME`` collapse does not know about."""
    _write_agent(_shared_agents(cwd_root), "foo")
    _write_agent(Path.home() / ".memtomem" / "agents", "foo", "---\nname: foo\n---\n\nhost\n")

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "from_scope": "project_shared",
            "to_target_scope": "user",
            "allow_host_writes": True,
        },
    )
    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root)
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "destination_exists"
    assert detail["message"] == _collision_message("agents", "foo", "user")


def _usurp_staging_after_scan(monkeypatch, dst_store):
    """Replace the in-flight staging entry once Gate A has read ours (#2314).

    Faithful to the engine's own cells: the swap happens between the scan and
    the promote, which is the window the claim's identity exists to survive.
    Returns the list the pathname is appended to, so the cell can assert the
    replacement was left alone.
    """
    from memtomem.context import transfer as transfer_mod

    real_scan = transfer_mod.scan_artifact_tree
    replaced = []

    def scan_then_usurp(*args, **kwargs):
        result = real_scan(*args, **kwargs)
        staged = [p for p in dst_store.glob(".migrate-*") if not p.name.endswith(".aside")]
        assert len(staged) == 1, staged
        staged[0].rename(staged[0].with_name(staged[0].name + ".aside"))
        staged[0].mkdir()
        # umask-independent: a pathological umask elsewhere in this suite would
        # otherwise leave the replacement unsearchable (0o177 clears owner-x).
        staged[0].chmod(0o700)
        (staged[0] / "theirs.md").write_text("theirs", encoding="utf-8")
        replaced.append(staged[0])
        return result

    monkeypatch.setattr(transfer_mod, "scan_artifact_tree", scan_then_usurp)
    return replaced


@pytest.mark.asyncio
async def test_replaced_staging_409_transfer_staging_replaced(
    client,
    cwd_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An entry replaced out of band is a conflict with its own code (#2314).

    Twin of the MCP action's ``refused: transfer_staging_replaced``. Without
    this arm the route answers 500 for a state the caller can act on, and the
    engine's refusal — which is what kept the usurper's entry alive — reads as
    a server fault.
    """
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    dst_store = _shared_agents(other)
    dst_store.mkdir(parents=True, exist_ok=True)
    replaced = _usurp_staging_after_scan(monkeypatch, dst_store)
    # The reproduction that moved the old 200-character cut past the paths: a
    # frozen ``$HOME`` that occurs in the prose. The wire text must not care.
    monkeypatch.setattr(_errors_mod, "_HOME", "Inspect it by hand, then retry.")

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post(
            "/api/context/agents/foo/transfer",
            json={
                "mode": "copy",
                "to_target_scope": "project_shared",
                "to_project_scope_id": scope_b,
                "confirm_project_shared": True,
            },
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "transfer_staging_replaced"
    # The route's own sentence: the engine's text ends with the staging path,
    # which goes to the log line instead (#3532).
    assert detail["message"] == _REPLACED_MESSAGE
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1, records
    assert records[0].startswith(
        "transfer agents/foo refused (transfer_staging_replaced): transfer staging was "
        "replaced out of band"
    )
    assert records[0].endswith(f"Staging: {replaced[0]}"), records[0]
    assert (replaced[0] / "theirs.md").exists()


@pytest.mark.asyncio
async def test_unverifiable_staging_409_carries_the_filesystem_remedy(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A filesystem that reports no file identity gets its own remedy on the wire (#3532).

    ``_staging_identity`` answers ``None`` there (``st_ino == 0``), and the claim
    then refuses to consume its own entry; retrying on the same filesystem would
    fail the same way, so the fixed sentence has to name the other way out.
    """
    from memtomem.context import migrate as migrate_mod

    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    monkeypatch.setattr(migrate_mod, "_staging_identity", lambda _path: None)

    # A copy refuses at the promote; the move twin below refuses earlier, at
    # the source, because a move that renamed first could not roll back (#3545).
    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post(
            "/api/context/agents/foo/transfer", json={**_move_body(scope_b), "mode": "copy"}
        )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "transfer_staging_replaced"
    assert detail["message"] == _REPLACED_MESSAGE
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1, records
    assert "transfer staging cannot be verified" in records[0]


@pytest.mark.asyncio
async def test_a_move_without_file_identity_is_refused_before_it_consumes_the_source(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The move twin of the cell above answers the same 409, with nothing moved (#3545).

    Every entry under both projects reports ``st_ino == 0``, the filesystem the
    issue describes. The move used to rename the source into staging, refuse
    to promote it, and answer a partial-commit 500 that blamed a replacement
    nobody made.
    """
    source = _write_agent(_shared_agents(cwd_root), "foo")
    before = source.read_bytes()
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    roots = (cwd_root.resolve(), other.resolve())
    real_lstat = Path.lstat

    class _Zeroed:
        def __init__(self, info):
            self._info = info

        def __getattr__(self, name):
            return getattr(self._info, name)

        st_ino = 0

    def lstat(self, *args, **kwargs):
        info = real_lstat(self, *args, **kwargs)
        return _Zeroed(info) if any(self.is_relative_to(r) for r in roots) else info

    monkeypatch.setattr(Path, "lstat", lstat)

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post("/api/context/agents/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "transfer_staging_replaced"
    assert detail["message"] == _REPLACED_MESSAGE
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1, records
    assert "does not report file identity" in records[0]
    assert source.read_bytes() == before
    dst_store = _shared_agents(other)
    assert not (dst_store / "foo.md").exists() and not (dst_store / "foo").exists()
    for store in (_shared_agents(cwd_root), dst_store):
        assert not store.exists() or not list(store.glob(".migrate-*")), store


@pytest.mark.asyncio
async def test_staging_collision_409_transfer_staging_busy(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A staging leftover is a conflict, not a 500 (#2309).

    Its own reason code, never ``destination_exists``: the destination is
    free — a leftover under a staging name is in the way, and the remediation
    is to inspect that entry, not to remove anything at the destination. The
    engine refuses to clear it because it can be an interrupted move's only
    copy of an artifact, so the caller has to be told which state this is.
    """
    from memtomem.context import migrate as migrate_mod

    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    # Pin the suffix and plant a leftover under it — a real collision, built
    # the way the engine's own tests build one.
    monkeypatch.setattr(migrate_mod.os, "getpid", lambda: 424242)
    monkeypatch.setattr(migrate_mod.secrets, "token_hex", lambda _n: "deadbeef")
    dst_store = _shared_agents(other)
    dst_store.mkdir(parents=True, exist_ok=True)
    leftover = dst_store / ".migrate-foo-424242-deadbeef.tmp"
    leftover.mkdir()
    (leftover / "agent.md").write_text("only copy\n", encoding="utf-8")
    # The reproduction that moved the old 200-character cut past the paths: a
    # frozen ``$HOME`` that occurs in the prose. The wire text must not care.
    monkeypatch.setattr(_errors_mod, "_HOME", "Inspect it by hand, then retry.")

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "transfer_staging_busy"
    # The route's own sentence: the engine's text ends with the occupied
    # paths, which go to the log line instead (#3532).
    assert detail["message"] == _BUSY_MESSAGE
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    # The leftover survived the request, which is the entire point.
    assert (leftover / "agent.md").read_text(encoding="utf-8") == "only copy\n"


_SKILL_BODY = "---\nname: {name}\ndescription: a clean test skill\n---\n\nhello\n"


def _shared_skills(root: Path) -> Path:
    return root / ".memtomem" / "skills"


def _write_skill(store_root: Path, name: str) -> Path:
    artifact = store_root / name
    artifact.mkdir(parents=True, exist_ok=True)
    manifest = artifact / "SKILL.md"
    manifest.write_text(_SKILL_BODY.format(name=name), encoding="utf-8")
    return manifest


def _plant_two_swap_markers(store_root: Path, name: str) -> list[Path]:
    """Two markers claiming one artifact: a real wedged state (``_dir_swap._find_marker``).

    The prelude refuses it before reading either marker, so the bodies do not
    matter; the names must match ``.swap-<name>-<pid>-<6 hex>.json``.
    """
    store_root.mkdir(parents=True, exist_ok=True)
    markers = [store_root / f".swap-{name}-{s}.json" for s in ("111-aaaaaa", "222-bbbbbb")]
    for marker in markers:
        marker.write_text("{}", encoding="utf-8")
    return markers


def _swap_message(side: str, name: str, tier: str = "project_shared") -> str:
    return (
        f"an interrupted directory swap blocks this transfer: skills/{name} in the "
        f"{side} {tier} tier could not be recovered automatically. Inspect it "
        "by hand, then retry. The server log has the details."
    )


def _transfer_records(caplog: pytest.LogCaptureFixture) -> list:
    """The route's own log records; each must be one scanned line with no traceback."""
    records = [r for r in caplog.records if r.name == "memtomem.web.routes.context_transfer"]
    for record in records:
        assert record.exc_info is None, record.getMessage()
        assert record.exc_text is None, record.getMessage()
        assert "\n" not in record.getMessage(), record.getMessage()
    return records


def _move_body(scope_b: str) -> dict:
    return {
        "mode": "move",
        "to_target_scope": "project_shared",
        "to_project_scope_id": scope_b,
        "confirm_project_shared": True,
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("side", ["source", "destination"])
async def test_swap_recovery_409_is_path_free(
    client, cwd_root: Path, tmp_path: Path, side: str, caplog: pytest.LogCaptureFixture
) -> None:
    """A wedged swap on either side refuses without naming a host path (#3532).

    Driven through the real prelude: two markers for one name is a state
    ``_find_marker`` refuses, and the engine's own sentence for it embeds the
    store path. The wire gets which side and tier is wedged; the server log
    gets the entries, and never the engine sentence (a malformed marker's
    field values are interpolated into some of those sentences).
    """
    _write_skill(_shared_skills(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    wedged = _shared_skills(cwd_root if side == "source" else other)
    _plant_two_swap_markers(wedged, "foo")

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post("/api/context/skills/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "swap_recovery_pending"
    assert detail["message"] == _swap_message(side, "foo")

    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1, records
    assert records[0] == (
        "transfer skills/foo refused (swap_recovery_pending): "
        f"{_swap_message(side, 'foo').removesuffix(' The server log has the details.')} "
        f"Entries: {wedged / 'foo'}"
    )
    # Nothing was moved: the source is where it was.
    assert (_shared_skills(cwd_root) / "foo" / "SKILL.md").is_file()


@pytest.mark.asyncio
async def test_swap_recovery_409_user_tier_destination(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """The tier in the text is the wedged side's, here the user tier (#3532)."""
    _write_skill(_shared_skills(cwd_root), "foo")
    user_store = tmp_path / "home" / ".memtomem" / "skills"
    _plant_two_swap_markers(user_store, "foo")

    resp = await client.post(
        "/api/context/skills/foo/transfer",
        json={
            "mode": "move",
            # The markers make discovery count ``foo`` as present in the user
            # tier too; name the source so the ambiguity 400 does not shadow this.
            "from_scope": "project_shared",
            "to_target_scope": "user",
            "allow_host_writes": True,
        },
    )

    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root)
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "swap_recovery_pending"
    assert detail["message"] == _swap_message("destination", "foo", tier="user")


@pytest.mark.asyncio
async def test_swap_recovery_409_names_the_renamed_destination(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """A copy with ``as_name`` is wedged under the destination name, not the source's (#3532)."""
    _write_skill(_shared_skills(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    _plant_two_swap_markers(_shared_skills(other), "bar")

    resp = await client.post(
        "/api/context/skills/foo/transfer",
        json={**_move_body(scope_b), "mode": "copy", "as_name": "bar"},
    )

    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    assert resp.json()["detail"]["message"] == _swap_message("destination", "bar")


@pytest.mark.asyncio
async def test_swap_recovery_409_keeps_marker_values_out(
    client, cwd_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A malformed marker's field value reaches neither the wire nor the log (#3532).

    The engine's sentence for a field mismatch interpolates the value it read
    (``_dir_swap._validated_paths``); a value no secret pattern matches would
    pass the scan, so the sentence itself must stay out.
    """
    _write_skill(_shared_skills(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    store = _shared_skills(other)
    store.mkdir(parents=True)
    marker = store / ".swap-foo-111-aaaaaa.json"
    marker.write_text(json.dumps({"version": 1, "name": "OPAQUEVALUE42"}), encoding="utf-8")

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post("/api/context/skills/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "swap_recovery_pending"
    assert "OPAQUEVALUE42" not in resp.text
    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1, records
    assert "OPAQUEVALUE42" not in records[0]
    assert records[0].endswith(f"Entries: {marker}")


@pytest.mark.asyncio
async def test_swap_recovery_409_without_safe_message_falls_back(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A raise site that sets no ``safe_message`` still answers path-free (#3532)."""
    from memtomem.context.transfer import TransferRecoveryError
    from memtomem.web.routes import context_transfer as route_mod

    _write_skill(_shared_skills(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    def _wedged(*_a: object, **_k: object) -> None:
        raise TransferRecoveryError(f"swap pending at '{other}'")

    monkeypatch.setattr(route_mod, "transfer_artifact", _wedged)
    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post("/api/context/skills/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    assert resp.json()["detail"]["message"] == (
        "an interrupted directory swap blocks this transfer and could not be recovered "
        "automatically. Inspect it by hand, then retry. The server log has the details."
    )
    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1 and records[0].endswith("Entries: none recorded"), records


@pytest.mark.asyncio
async def test_swap_recovery_409_secret_shaped_name_is_scanned(
    client, cwd_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The composed text still goes through the secret scan (#3532).

    A validated artifact name can match a provider-token pattern, and the
    wire sentence and the log line both carry the name.
    """
    from memtomem.web.routes._errors import _SECRET_REDACTED_MARKER

    name = "ghp_" + "a" * 36
    _write_skill(_shared_skills(cwd_root), name)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    _plant_two_swap_markers(_shared_skills(other), name)

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post(f"/api/context/skills/{name}/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "swap_recovery_pending"
    assert detail["message"] == _SECRET_REDACTED_MARKER
    logged = [r.getMessage() for r in _transfer_records(caplog)]
    assert logged == [_SECRET_REDACTED_MARKER], logged


@pytest.mark.asyncio
async def test_staging_busy_409_logs_the_occupied_paths(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """The wire gets a fixed sentence; the server log names the occupied paths (#3532)."""
    from memtomem.context import migrate as migrate_mod

    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    monkeypatch.setattr(migrate_mod.os, "getpid", lambda: 424242)
    monkeypatch.setattr(migrate_mod.secrets, "token_hex", lambda _n: "deadbeef")
    dst_store = _shared_agents(other)
    dst_store.mkdir(parents=True, exist_ok=True)
    leftover = dst_store / ".migrate-foo-424242-deadbeef.tmp"
    leftover.mkdir()

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post("/api/context/agents/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "transfer_staging_busy"
    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert len(records) == 1, records
    assert records[0].startswith(
        f"transfer agents/foo refused (transfer_staging_busy): {_BUSY_REMEDIATION}"
    )
    assert records[0].endswith(str(leftover)), records[0]


@pytest.mark.asyncio
async def test_refusal_log_line_escapes_control_characters(caplog) -> None:
    """A line break in a path cannot split the record or forge a line after it (#3532)."""
    from memtomem.web.routes import context_transfer as route_mod

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        route_mod._log_refusal("agents", "foo", "transfer_staging_busy", "Occupied: /a\nFAKE b")

    records = [r.getMessage() for r in _transfer_records(caplog)]
    assert records == [
        "transfer agents/foo refused (transfer_staging_busy): Occupied: /a\\x0aFAKE b"
    ]


@pytest.mark.asyncio
async def test_refusal_log_line_scans_the_paths_not_only_the_name(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog
) -> None:
    """A secret shape in a staging path, with an ordinary artifact name, still redacts the line.

    The secret-name cell cannot tell a whole-line scan from a name-only one,
    because the name itself trips the scanner (#3532).
    """
    from memtomem.context import migrate as migrate_mod
    from memtomem.web.routes._errors import _SECRET_REDACTED_MARKER

    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path, name="ghp_" + "b" * 36)
    scope_b = await _register(client, other)
    monkeypatch.setattr(migrate_mod.os, "getpid", lambda: 424242)
    monkeypatch.setattr(migrate_mod.secrets, "token_hex", lambda _n: "deadbeef")
    dst_store = _shared_agents(other)
    dst_store.mkdir(parents=True, exist_ok=True)
    (dst_store / ".migrate-foo-424242-deadbeef.tmp").mkdir()

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post("/api/context/agents/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["message"] == _BUSY_MESSAGE
    assert [r.getMessage() for r in _transfer_records(caplog)] == [_SECRET_REDACTED_MARKER]


@pytest.mark.asyncio
async def test_unexpected_failure_500_is_path_free(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catch-all has no structure to render from, so it says nothing of the cause (#3532)."""
    from memtomem.web.routes import context_transfer as route_mod

    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    def _boom(*_a: object, **_k: object) -> None:
        raise RuntimeError(f"boom while reading {other / '.memtomem' / 'agents' / 'foo'}")

    monkeypatch.setattr(route_mod, "transfer_artifact", _boom)

    resp = await client.post("/api/context/agents/foo/transfer", json=_move_body(scope_b))

    assert resp.status_code == 500, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "internal"
    assert detail["message"] == "transfer failed unexpectedly; the server log has the details"


@pytest.mark.asyncio
async def test_paused_destination_409_sync_paused(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "sync_paused"
    assert detail["project_scope_id"] == scope_b
    # Nothing moved.
    assert (_shared_agents(cwd_root) / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_implicit_destination_in_paused_source_project_409(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """Codex design-gate Major-2 pin: omitting ``to_project_scope_id`` must
    not write a project tier the explicit spelling of the same destination
    refuses. Source selector names a paused project; destination implicit."""
    other = _other_project(tmp_path)
    _write_agent(_shared_agents(other), "foo")
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post(
        f"/api/context/agents/foo/transfer?project_scope_id={scope_b}",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "sync_paused"
    assert (_shared_agents(other) / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_user_tier_from_paused_source_project_not_gated_by_eligibility(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """User-tier destination is a host write, not the paused project's
    runtime — eligibility must not refuse it (allow_host_writes gates it)."""
    other = _other_project(tmp_path)
    _write_agent(_shared_agents(other), "foo")
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post(
        f"/api/context/agents/foo/transfer?project_scope_id={scope_b}",
        json={"mode": "move", "to_target_scope": "user", "allow_host_writes": True},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "ok"
    assert (Path.home() / ".memtomem" / "agents" / "foo" / "agent.md").is_file()


@pytest.mark.asyncio
async def test_unknown_destination_scope_404(client, cwd_root: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "to_project_scope_id": "p-deadbeef0000",
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "unknown project_scope_id" in detail["message"]


def _no_store_message(scope_id: str) -> str:
    return (
        f"destination project {scope_id!r} has no .memtomem/ store. Initialize it first: "
        "run `mm context init` in that project's directory."
    )


@pytest.mark.asyncio
async def test_destination_without_store_409(client, cwd_root: Path) -> None:
    """The 409 names the destination by its scope id, not its path (#2489).

    The destination sits under the sandbox ``$HOME`` with a space in its name:
    the old text put the absolute path in the prose and, unquoted, in a ``cd``
    hint that broke on the space.
    """
    _write_agent(_shared_agents(cwd_root), "foo")
    bare = _other_project(Path.home(), "bare project", store=False)
    scope_b = await _register(client, bare)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "to_project_scope_id": scope_b,
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "no_memtomem_store"
    assert detail["message"] == _no_store_message(scope_b)
    _assert_path_free(resp.json(), bare, cwd_root)


@pytest.mark.asyncio
async def test_source_not_found_404(client, cwd_root: Path) -> None:
    resp = await client.post(
        "/api/context/agents/ghost/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert detail["message"] == (
        "agents/ghost not found in any scope (user / project_shared / project_local)."
    )


@pytest.mark.asyncio
async def test_unknown_source_scope_404(client, cwd_root: Path) -> None:
    # The SOURCE selector resolves through ``_resolve_selected_scope``, which
    # raises the B-1 #1284 object envelope; ``_resolve_source`` must re-raise it
    # verbatim rather than stringify the dict into the message field.
    resp = await client.post(
        "/api/context/agents/foo/transfer?project_scope_id=p-deadbeef0000",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "unknown project_scope_id" in detail["message"]
    # Regression guard: the message is the clean string, NOT a ``str({...})``
    # dict repr (the pre-fix bug that the substring check above would miss).
    assert "error_kind" not in detail["message"]
    assert not detail["message"].startswith("{")


# ── disclose-then-confirm round-trips ────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_b_round_trip(client, cwd_root: Path) -> None:
    """project_shared destination: disclose (no write) → confirmed re-POST."""
    src_manifest = _write_agent(_local_agents(cwd_root), "foo")

    first = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "project_shared"},
    )
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "needs_confirmation"
    assert data["confirm"] == "confirm_project_shared"
    assert "host_targets" not in data
    plan = data["plan"]
    assert plan["transferred"] is False
    assert plan["dst_path"].endswith("foo")
    # Disclosure performed no write.
    assert src_manifest.is_file()
    assert not (_shared_agents(cwd_root) / "foo").exists()

    second = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_shared",
            "confirm_project_shared": True,
        },
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    assert (_shared_agents(cwd_root) / "foo" / "agent.md").is_file()
    assert not src_manifest.parent.exists()


@pytest.mark.asyncio
async def test_host_write_round_trip(client, cwd_root: Path) -> None:
    """user-tier destination: host paths disclosed → allow_host_writes re-POST."""
    src_manifest = _write_agent(_shared_agents(cwd_root), "foo")
    host_dst = Path.home() / ".memtomem" / "agents" / "foo"

    first = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "user"},
    )
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "needs_confirmation"
    assert data["confirm"] == "allow_host_writes"
    assert data["host_targets"] == [str(host_dst)]
    assert data["plan"]["dst_project_scope_id"] is None
    assert src_manifest.is_file()
    assert not host_dst.exists()

    second = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "user", "allow_host_writes": True},
    )
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    assert (host_dst / "agent.md").read_text(encoding="utf-8") == _AGENT_BODY
    assert not src_manifest.parent.exists()


@pytest.mark.asyncio
async def test_dry_run_plans_without_confirmation(client, cwd_root: Path) -> None:
    src_manifest = _write_agent(_local_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer?dry_run=true",
        json={"mode": "move", "to_target_scope": "project_shared"},
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "plan"
    assert data["transferred"] is False
    assert src_manifest.is_file()
    assert not (_shared_agents(cwd_root) / "foo").exists()


# ── Gate A / privacy envelope ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_privacy_scan_blocks_with_standard_string_envelope(client, cwd_root: Path) -> None:
    """Secret bytes landing in project_shared → the standard 422 STRING
    envelope (issue-pinned), zero residue at the destination. Copy mode
    carries the transfer-native source-anchored remediation hint (a
    same-root move keeps migrate's historical Gate A wording — engine
    contract, pinned in test_context_transfer.py)."""
    src_manifest = _write_agent(_local_agents(cwd_root), "foo", _SECRET_BODY)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "to_target_scope": "project_shared",
            "confirm_project_shared": True,
        },
    )
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "Offending file" in detail
    # The hint re-anchors onto the SOURCE manifest (the transient staging
    # path is gone after rollback).
    assert str(src_manifest) in detail
    # Zero residue at the destination; copy never touched the source.
    assert not (_shared_agents(cwd_root) / "foo").exists()
    assert src_manifest.read_text(encoding="utf-8") == _SECRET_BODY


# ── validation 400s ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_user_destination_with_project_selector_400(client, cwd_root: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "user",
            "to_project_scope_id": "p-cafecafe0000",
            "allow_host_writes": True,
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "user tier is global" in detail["message"]


@pytest.mark.asyncio
async def test_rename_with_move_400(client, cwd_root: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "move",
            "to_target_scope": "project_local",
            "as_name": "bar",
        },
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "copy mode only" in detail["message"]


@pytest.mark.asyncio
async def test_invalid_name_400(client, cwd_root: Path) -> None:
    """Name validation fires before any path math (CLI-parity). A
    slash-bearing traversal shape can't even reach the handler — the
    router 404s a path-param slash — so the reachable invalid class is
    charset violations."""
    resp = await client.post(
        "/api/context/agents/foo$/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "invalid agent name" in detail["message"]


@pytest.mark.asyncio
async def test_unsupported_kind_400(client, cwd_root: Path) -> None:
    # "mcp-servers" graduated to a supported kind in A-12 (#1282) — the
    # unsupported example is now a kind no surface accepts.
    resp = await client.post(
        "/api/context/memories/foo/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "unsupported kind for artifact transfer" in detail["message"]
    assert "'mcp-servers'" in detail["message"]  # the 400 advertises the new kind


# ── timeout envelope ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_timeout_503_busy_with_bounded_lock_budget(
    client, cwd_root: Path, monkeypatch
) -> None:
    """Engine lock-budget TimeoutError → 503 busy. The fake also records the
    call kwargs: the route MUST pass the bounded ``lock_timeout`` — an
    unbounded (None) engine wait would silently reopen the #1145
    orphan-worker shape while this test kept passing (Codex review)."""
    import memtomem.web.routes.context_transfer as route_mod

    _write_agent(_shared_agents(cwd_root), "foo")
    seen_kwargs: dict = {}

    def hung_transfer(*args, **kwargs):
        seen_kwargs.update(kwargs)
        raise TimeoutError("could not acquire .lock within 30s (held by another process)")

    monkeypatch.setattr(route_mod, "transfer_artifact", hung_transfer)
    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "move", "to_target_scope": "project_local"},
    )
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "busy"
    assert "Transfer timed out" in detail["message"]
    assert seen_kwargs["lock_timeout"] == route_mod._TRANSFER_LOCK_BUDGET_S


# ── mcp-servers copy (A-12 #1282) ────────────────────────────────────
#
# Adapter semantics are pinned by ``test_mcp_servers_copy.py``; these pin
# the route's mcp branch: the validation 400 matrix in mcp vocabulary,
# the shared gates (confirm round-trip, eligibility, store check)
# applying unchanged, the issue-pinned 422s (string privacy envelope /
# object parse envelope), and the wire shape (since #1311 a runnable
# ``sync_command`` — ``cd <dst> && mm context sync --include=mcp-servers`` —
# with ``sync_hint`` as its prose mirror).


_MCP_CLEAN = {"command": "npx", "args": ["-y", "srv"], "env": {"PG_HOST": "localhost"}}
_MCP_SECRET = {"command": "npx", "env": {"AWS_ACCESS_KEY": "AKIA1234567890ABCDEF"}}


def _write_mcp_server(root: Path, name: str = "pg", definition: dict | None = None) -> Path:
    store = root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    path = store / f"{name}.json"
    path.write_text(
        json.dumps(definition if definition is not None else _MCP_CLEAN, indent=2) + "\n",
        encoding="utf-8",
    )
    return path


_MCP_MISSING = "mcp-servers/pg not found at the source project: .memtomem/mcp-servers/pg.json"


def _mcp_copy_body(scope_b: str, **overrides) -> dict:
    body = {
        "mode": "copy",
        "to_target_scope": "project_shared",
        "to_project_scope_id": scope_b,
        "confirm_project_shared": True,
    }
    body.update(overrides)
    return body


@pytest.mark.asyncio
async def test_mcp_copy_ok(client, cwd_root: Path, tmp_path: Path) -> None:
    src = _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "ok"
    assert data["transferred"] is True
    assert data["kind"] == "mcp-servers"
    assert data["mode"] == "copy"
    assert data["layout"] == "flat"
    assert (data["from_scope"], data["to_scope"]) == ("project_shared", "project_shared")
    assert data["dst_project_scope_id"] == scope_b
    # Follow-up contract (#1311): needs_sync with a runnable cd-prefixed
    # command; the prose sync_hint mirrors it and keeps the API call.
    assert data["needs_sync"] is True
    assert data["sync_command"].startswith("cd ")
    assert "mm context sync --include=mcp-servers --scope project_shared" in data["sync_command"]
    assert f"project_scope_id={scope_b}" in data["sync_hint"]
    assert data["provenance"] == "not_applicable"

    dst = other / ".memtomem" / "mcp-servers" / "pg.json"
    assert dst.read_bytes() == src.read_bytes()
    assert src.is_file()


@pytest.mark.asyncio
async def test_mcp_copy_malformed_source_is_path_free(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    # Real parse path for the transfer-copy catch site (#1412): a malformed
    # SOURCE canonical makes the copy adapter's parser embed the resolved
    # source path in ``str(exc)``; the web boundary must render the path-free
    # ``exc.safe_message`` (basename + the JSON problem) instead. A trailing
    # comma is invalid JSON but secret-free, so Gate A passes and the parse
    # branch is the one that fires.
    store = cwd_root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True, exist_ok=True)
    src = store / "pg.json"
    src.write_text('{\n  "command": "echo",\n}\n', encoding="utf-8")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))

    assert resp.status_code == 422, resp.text
    body = resp.json()
    assert body["detail"]["error_kind"] == "parse", body
    message = body["detail"]["message"]
    assert "pg.json" in message, message  # names the artifact
    assert "invalid JSON" in message, message  # names the problem
    blob = json.dumps(body)
    assert str(src.resolve()) not in blob, body  # full source path never leaks
    assert str(src.resolve().parent) not in blob, body
    assert str(cwd_root.resolve()) not in blob, body
    assert str(Path.home()) not in blob, body  # #1385 literal: no $HOME / username


@pytest.mark.asyncio
async def test_mcp_needs_confirmation_round_trip(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    dst = other / ".memtomem" / "mcp-servers" / "pg.json"

    first = await client.post(
        "/api/context/mcp-servers/pg/transfer",
        json=_mcp_copy_body(scope_b, confirm_project_shared=False),
    )
    assert first.status_code == 200, first.text
    data = first.json()
    assert data["status"] == "needs_confirmation"
    assert data["confirm"] == "confirm_project_shared"
    assert data["plan"]["transferred"] is False
    assert data["plan"]["sync_command"].startswith("cd ")
    assert (
        "mm context sync --include=mcp-servers --scope project_shared"
        in data["plan"]["sync_command"]
    )
    assert data["plan"]["sync_hint"]
    assert not dst.exists()

    second = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert second.status_code == 200, second.text
    assert second.json()["status"] == "ok"
    assert dst.is_file()


@pytest.mark.asyncio
async def test_mcp_dry_run_plan_writes_nothing(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post(
        "/api/context/mcp-servers/pg/transfer?dry_run=true",
        json=_mcp_copy_body(scope_b, confirm_project_shared=False),
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["status"] == "plan"
    assert data["transferred"] is False
    assert not (other / ".memtomem" / "mcp-servers").exists()


@pytest.mark.asyncio
async def test_mcp_validation_400_matrix(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    cases = [
        (_mcp_copy_body(scope_b, mode="move"), "mcp-servers support copy only"),
        (_mcp_copy_body(scope_b, as_name="pg2"), "as_name is not supported for mcp-servers"),
        (
            _mcp_copy_body(scope_b, to_target_scope="project_local"),
            "single-tier (project_shared) by design",
        ),
        (
            _mcp_copy_body(scope_b, from_scope="user"),
            "single-tier (project_shared) by design",
        ),
        (
            _mcp_copy_body(scope_b, to_project_scope_id=None),
            "mcp-servers copy is cross-project only",
        ),
    ]
    for body, fragment in cases:
        resp = await client.post("/api/context/mcp-servers/pg/transfer", json=body)
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error_kind"] == "validation"
        assert fragment in detail["message"]
    assert not (other / ".memtomem" / "mcp-servers").exists()


@pytest.mark.asyncio
async def test_mcp_same_project_selector_400(client, cwd_root: Path) -> None:
    _write_mcp_server(cwd_root)
    scope_self = await _register(client, cwd_root)

    resp = await client.post(
        "/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_self)
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "resolves to the source project" in detail["message"]


@pytest.mark.asyncio
async def test_mcp_collision_409(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    existing = _write_mcp_server(other, definition={"command": "theirs"})
    before = existing.read_bytes()
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 409, resp.text
    # The mcp-servers adapter raises the same collision with its own path (#3302).
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "destination_exists"
    assert detail["message"] == _collision_message("mcp-servers", "pg", "project_shared")
    assert existing.read_bytes() == before


@pytest.mark.asyncio
async def test_mcp_privacy_422_string_envelope(client, cwd_root: Path, tmp_path: Path) -> None:
    """Issue #1282 acceptance 1 on the wire: the standard project_shared
    block envelope — a STRING 422 detail, same as every sync surface."""
    _write_mcp_server(cwd_root, definition=_MCP_SECRET)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert detail.startswith("Gate A: pg.json contains 1 privacy pattern hit(s)")
    # Zero destination residue: no canonical, no staging leftovers (the
    # store dir + lock sidecar are the locking machinery's own artifacts).
    dst_store = other / ".memtomem" / "mcp-servers"
    assert not (dst_store / "pg.json").exists()
    assert not list(dst_store.glob(".migrate-*"))


@pytest.mark.asyncio
async def test_mcp_parse_422_object_envelope(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root, definition={"type": "sse", "url": "https://example.com"})
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "parse"
    assert "Only stdio servers are supported" in detail["message"]


@pytest.mark.asyncio
async def test_mcp_source_not_found_404(client, cwd_root: Path, tmp_path: Path) -> None:
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 404, resp.text
    # The engine's text ends with the resolved source path (#3532).
    _assert_path_free(resp.json(), tmp_path, cwd_root, other)
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert detail["message"] == _MCP_MISSING


@pytest.mark.asyncio
async def test_mcp_source_not_found_404_linked_store(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """A source store linked out of the project still answers 404, path-free (#3532).

    ``canonical_mcp_server_root`` resolves the link, so the engine's path sits
    under no project root; the public text must not be derived from it.
    """
    elsewhere = tmp_path / "elsewhere" / "mcp"
    elsewhere.mkdir(parents=True)
    try:
        (cwd_root / ".memtomem" / "mcp-servers").symlink_to(elsewhere, target_is_directory=True)
    except OSError as exc:  # pragma: no cover - Windows without symlink privilege
        pytest.skip(f"cannot create a directory symlink here: {exc}")
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 404, resp.text
    _assert_path_free(resp.json(), tmp_path, cwd_root, other, elsewhere)
    assert resp.json()["detail"]["message"] == _MCP_MISSING


@pytest.mark.asyncio
async def test_mcp_paused_destination_409(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other, enabled=False)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "sync_paused"
    assert not (other / ".memtomem" / "mcp-servers").exists()


@pytest.mark.asyncio
async def test_mcp_no_store_destination_409(client, cwd_root: Path, tmp_path: Path) -> None:
    """Outside ``$HOME``, with a space and (POSIX) a control character in the name (#2489)."""
    _write_mcp_server(cwd_root)
    name = "proj b" if sys.platform == "win32" else "proj b\x1b[31m"
    other = _other_project(tmp_path, name, store=False)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "no_memtomem_store"
    assert detail["message"] == _no_store_message(scope_b)
    _assert_path_free(resp.json(), other, cwd_root)
    assert not any("\x1b" in s for s in _strings(resp.json()))


# ── 400 validation arm: engine refusals that name a path (#3543) ─────────


_REFUSED_FALLBACK = "the transfer request was refused; the server log has the details."


def _assert_validation(resp, message: str, *leaky: Path) -> None:
    assert resp.status_code == 400, resp.text
    _assert_path_free(resp.json(), *leaky)
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert detail["message"] == message


@pytest.mark.asyncio
async def test_ambiguous_source_400_is_path_free(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(Path.home() / ".memtomem" / "agents", "foo")
    _write_agent(_shared_agents(cwd_root), "foo")
    _write_agent(_local_agents(cwd_root), "foo")

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={"mode": "copy", "to_target_scope": "user", "allow_host_writes": True},
    )
    _assert_validation(
        resp,
        "agents/foo exists in multiple scopes: user, project_shared, project_local. "
        "Set from_scope to one of these scopes.",
        tmp_path,
        cwd_root,
    )


@pytest.mark.asyncio
async def test_user_to_user_400_is_path_free(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(Path.home() / ".memtomem" / "agents", "foo")

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "from_scope": "user",
            "to_target_scope": "user",
            "allow_host_writes": True,
        },
    )
    _assert_validation(
        resp,
        "agents/foo is already in the global user tier; choose a project tier as the destination.",
        tmp_path,
        cwd_root,
    )


_SAME_STORE = (
    "the source ({src}) and destination ({dst}) resolve to the same canonical store; "
    "choose a destination backed by a different store."
)


@pytest.mark.asyncio
async def test_same_store_400_is_path_free(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_agent(_shared_agents(cwd_root), "foo")

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "from_scope": "project_shared",
            "to_target_scope": "project_shared",
            "confirm_project_shared": True,
        },
    )
    _assert_validation(
        resp,
        _SAME_STORE.format(src="project_shared", dst="project_shared"),
        tmp_path,
        cwd_root,
    )


@pytest.mark.asyncio
@pytest.mark.requires_symlinks
async def test_linked_tiers_same_store_400_is_path_free(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """``canonical_artifact_dir`` resolves links, so two tiers can share one store."""
    _write_agent(_shared_agents(cwd_root), "foo")
    _local_agents(cwd_root).symlink_to(_shared_agents(cwd_root), target_is_directory=True)

    resp = await client.post(
        "/api/context/agents/foo/transfer",
        json={
            "mode": "copy",
            "from_scope": "project_local",
            "to_target_scope": "project_shared",
            "confirm_project_shared": True,
        },
    )
    _assert_validation(
        resp,
        _SAME_STORE.format(src="project_local", dst="project_shared"),
        tmp_path,
        cwd_root,
    )


@pytest.mark.asyncio
@pytest.mark.requires_symlinks
async def test_mcp_symlinked_source_400_is_path_free(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    real = _write_mcp_server(tmp_path / "real")
    store = cwd_root / ".memtomem" / "mcp-servers"
    store.mkdir(parents=True)
    (store / "pg.json").symlink_to(real)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    _assert_validation(
        resp,
        "mcp-servers/pg: the source .memtomem/mcp-servers/pg.json is a symlink; mcp-servers "
        "copy refuses symlinked canonicals — the destination must be a regular git-tracked "
        "file. Replace the symlink with a regular file holding the definition, then re-run "
        "the copy.",
        tmp_path,
        cwd_root,
        other,
        real,
    )


@pytest.mark.asyncio
async def test_mcp_unreadable_source_400_is_path_free(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_mcp_server(cwd_root).resolve()
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)
    fired: list[Path] = []
    real_read_bytes = Path.read_bytes

    def _read_bytes(self: Path) -> bytes:
        if self == src:
            fired.append(self)
            raise PermissionError(13, "Permission denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", _read_bytes)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    assert fired, "the read hook never matched the canonical source"
    _assert_validation(
        resp,
        "cannot read the source .memtomem/mcp-servers/pg.json; check that it exists and "
        "that the server can read it.",
        tmp_path,
        cwd_root,
        other,
    )


@pytest.mark.asyncio
async def test_mcp_non_utf8_source_400_is_path_free(client, cwd_root: Path, tmp_path: Path) -> None:
    _write_mcp_server(cwd_root).write_bytes(b'{"command": "\xff"}\n')
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    resp = await client.post("/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b))
    _assert_validation(
        resp,
        "the source .memtomem/mcp-servers/pg.json is not valid UTF-8; fix the source "
        "definition before copying.",
        tmp_path,
        cwd_root,
        other,
    )


@pytest.mark.asyncio
async def test_path_naming_refusal_without_safe_message_falls_back(
    client,
    cwd_root: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A raise site that leaves ``safe_message`` unset still answers path-free,
    and the engine's text goes to the one log line the fallback promises."""
    from memtomem.context.migrate import PathNamingClickException
    from memtomem.web.routes import context_transfer as route

    engine_text = f"refused: {cwd_root / '.memtomem'} is wrong"

    def _refuse(*_a: object, **_k: object) -> None:
        raise PathNamingClickException(engine_text)

    monkeypatch.setattr(route, "copy_mcp_server", _refuse)
    other = _other_project(tmp_path)
    scope_b = await _register(client, other)

    with caplog.at_level("WARNING", logger="memtomem.web.routes.context_transfer"):
        resp = await client.post(
            "/api/context/mcp-servers/pg/transfer", json=_mcp_copy_body(scope_b)
        )
    _assert_validation(resp, _REFUSED_FALLBACK, tmp_path, cwd_root, other)
    assert [r.getMessage() for r in _transfer_records(caplog)] == [
        f"transfer mcp-servers/pg refused (validation): {engine_text}"
    ]


@pytest.mark.asyncio
async def test_validation_400_passes_the_secret_scan(client, cwd_root: Path) -> None:
    """Every 400 text from the final arm is scanned, path-naming or not (#3543)."""
    name = "AKIA" + "1234567890ABCDEF"  # assembled at runtime: a synthetic AWS key ID
    _write_agent(_shared_agents(cwd_root), name)

    resp = await client.post(
        f"/api/context/agents/{name}/transfer",
        json={"mode": "move", "from_scope": "project_shared", "to_target_scope": "project_shared"},
    )
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert detail["message"] == "<redacted: secret-shape>"
