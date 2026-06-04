"""Regression tests for context-gateway web-route mutator hardening.

Covers the web-layer gaps closed alongside PR #283-#286:

* POST / PUT validate names via the same ``memtomem.context._names.validate_name``
  used by the CLI / MCP paths (#276 bypass).
* POST / PUT write through ``memtomem.context._atomic.atomic_write_text``
  (#275 bypass).
* PUT / DELETE hold ``_gateway_lock`` (#277 bypass on non-POST verbs).
* PUT mtime guard uses nanosecond precision (float-second guard missed
  sub-millisecond writes).
* DELETE is idempotent on missing resources.

The matrix runs each test across agents / commands / skills via
``TYPE_MATRIX`` so regressions on any single type fail loudly.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app
from .helpers import set_home


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox ``HOME`` and return the project root."""
    set_home(monkeypatch, tmp_path)
    (tmp_path / ".claude").mkdir(parents=True, exist_ok=True)
    return tmp_path


@pytest.fixture
def app(gateway_root: Path):
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = gateway_root
    application.state.storage = AsyncMock()
    application.state.config = None
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


# ---------------------------------------------------------------------------
# Per-type adapters
# ---------------------------------------------------------------------------


def _agent_content(name: str) -> str:
    return f"---\nname: {name}\ndescription: d\n---\nBody\n"


def _command_content() -> str:
    return "---\ndescription: d\n---\nBody\n"


def _skill_content() -> str:
    return "# Skill\n"


def _make_canonical_agent(root: Path, name: str) -> Path:
    path = root / ".memtomem" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_agent_content(name), encoding="utf-8")
    return path


def _make_canonical_command(root: Path, name: str) -> Path:
    path = root / ".memtomem" / "commands" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_command_content(), encoding="utf-8")
    return path


def _make_canonical_skill(root: Path, name: str) -> Path:
    skill_dir = root / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    manifest = skill_dir / SKILL_MANIFEST
    manifest.write_text(_skill_content(), encoding="utf-8")
    return manifest


TYPE_MATRIX = [
    pytest.param(
        {
            "type": "agents",
            "list_url": "/api/context/agents",
            "detail": lambda n: f"/api/context/agents/{n}",
            "create_body": lambda n: {"name": n, "content": _agent_content(n)},
            "manifest": lambda root, n: root / ".memtomem" / "agents" / f"{n}.md",
            "make_canonical": _make_canonical_agent,
            # Where a route-CREATED artifact's working file lands. ``manifest``
            # above is the legacy *flat* location used by make_canonical-seeded
            # tests; ADR-0022 create now writes the versioned *dir* layout.
            "created_working": lambda root, n: root / ".memtomem" / "agents" / n / "agent.md",
            "versioned": True,
        },
        id="agents",
    ),
    pytest.param(
        {
            "type": "commands",
            "list_url": "/api/context/commands",
            "detail": lambda n: f"/api/context/commands/{n}",
            "create_body": lambda n: {"name": n, "content": _command_content()},
            "manifest": lambda root, n: root / ".memtomem" / "commands" / f"{n}.md",
            "make_canonical": _make_canonical_command,
            "created_working": lambda root, n: root / ".memtomem" / "commands" / n / "command.md",
            "versioned": True,
        },
        id="commands",
    ),
    pytest.param(
        {
            "type": "skills",
            "list_url": "/api/context/skills",
            "detail": lambda n: f"/api/context/skills/{n}",
            "create_body": lambda n: {"name": n, "content": _skill_content()},
            "manifest": lambda root, n: root / ".memtomem" / "skills" / n / SKILL_MANIFEST,
            "make_canonical": _make_canonical_skill,
            # Skills are already dir-based but are NOT ADR-0022 versioned
            # (a skill's "version" is a whole-tree snapshot, not a single .md).
            "created_working": lambda root, n: root / ".memtomem" / "skills" / n / SKILL_MANIFEST,
            "versioned": False,
        },
        id="skills",
    ),
]


# ---------------------------------------------------------------------------
# Name validation — POST body
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hostile_name",
    ["..", ".", "../escape", "foo/bar", "foo\\bar", "foo\x00bar", "foo\nbar"],
)
@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_rejects_hostile_name(
    adapter: dict,
    hostile_name: str,
    client: AsyncClient,
):
    body = adapter["create_body"](hostile_name)
    r = await client.post(adapter["list_url"], json=body)
    assert r.status_code == 400, (adapter["type"], hostile_name, r.text)


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_rejects_long_name(adapter: dict, client: AsyncClient):
    body = adapter["create_body"]("a" * 65)
    r = await client.post(adapter["list_url"], json=body)
    assert r.status_code == 400


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_rejects_leading_dash(adapter: dict, client: AsyncClient):
    body = adapter["create_body"]("-foo")
    r = await client.post(adapter["list_url"], json=body)
    assert r.status_code == 400


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_accepts_valid_name(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    r = await client.post(adapter["list_url"], json=adapter["create_body"]("my-artifact.v2"))
    assert r.status_code == 200
    working = adapter["created_working"](gateway_root, "my-artifact.v2")
    assert working.is_file()
    if adapter["versioned"]:
        # ADR-0022: agents/commands are created in versioned directory layout
        # (working file + versions/v1.md + manifest), not a flat file — so the
        # detail panel can version them immediately (see
        # test_POST_create_is_immediately_versionable) instead of dead-ending on
        # a ``mm context migrate`` hint that then skips the file as a manual flat.
        adir = working.parent
        assert (adir / "versions" / "v1.md").is_file()
        manifest = json.loads((adir / "versions.json").read_text(encoding="utf-8"))
        assert set(manifest["versions"]) == {"v1"}
        assert manifest.get("labels", {}) == {}


VERSIONED_TYPE_MATRIX = [p for p in TYPE_MATRIX if p.values[0]["versioned"]]


@pytest.mark.parametrize("adapter", VERSIONED_TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_create_is_immediately_versionable(adapter: dict, client: AsyncClient):
    """ADR-0022 split-brain regression (Codex BLOCKER).

    A web-created agent/command is born in versioned dir layout, so snapshotting
    the next version via ``POST .../versions`` returns ``v2`` instead of the old
    flat-file dead-end (409 "flat layout — run ``mm context migrate``", which the
    migrate then skips as an unowned manual flat).
    """
    r = await client.post(adapter["list_url"], json=adapter["create_body"]("ver-me"))
    assert r.status_code == 200
    rv = await client.post(adapter["detail"]("ver-me") + "/versions", json={"note": "second"})
    assert rv.status_code == 200, rv.text
    assert rv.json()["version"]["tag"] == "v2"


@pytest.mark.parametrize("adapter", VERSIONED_TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_invalid_utf8_content_leaves_no_orphan_dir(
    adapter: dict, client: AsyncClient, gateway_root: Path
):
    """A lone-surrogate body can't be UTF-8 encoded.

    The encode happens before any ``mkdir``, so create rejects it with 400 and
    leaves no artifact directory behind — a later valid create for the same name
    must not be wedged on the orphan-dir 409 guard. (Regression: an earlier
    revision encoded after mkdir, leaking an orphan dir.)

    The lone surrogate is sent as already-escaped JSON (``\\ud800`` is plain
    ASCII on the wire) so the failure happens server-side at our encode, not in
    the client's request serializer.
    """
    raw_body = '{"name": "surrogate", "content": "\\ud800"}'
    r = await client.post(
        adapter["list_url"],
        content=raw_body,
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 400, r.text
    working = adapter["created_working"](gateway_root, "surrogate")
    assert not working.parent.exists(), f"{adapter['type']}: orphan artifact dir left behind"
    # Retry with valid content must succeed (not 409-wedge on a stale dir).
    ok = await client.post(adapter["list_url"], json=adapter["create_body"]("surrogate"))
    assert ok.status_code == 200, ok.text


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_path_param_validated(
    adapter: dict,
    client: AsyncClient,
):
    """Names in the URL path (PUT ``/..../{name}``) go through the same validator."""
    r = await client.put(
        adapter["detail"]("a" * 65),
        json={"content": "x", "mtime_ns": "0"},
    )
    assert r.status_code == 400


# ---------------------------------------------------------------------------
# Atomic write (#275 regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_atomic_on_replace_failure_preserves_original(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``os.replace`` fails mid-write, the original file is untouched and no
    tempfile leaks. Core correctness of ``atomic_write_text`` is tested in
    ``test_context_atomic.py``; this test proves the web route routes through it
    — not ``Path.write_text``, which would leave a truncated file on failure.
    """
    target = adapter["make_canonical"](gateway_root, "preserve-me")
    original_payload = target.read_text(encoding="utf-8")

    read = await client.get(adapter["detail"]("preserve-me"))
    mtime_ns = read.json()["mtime_ns"]

    import memtomem.context._atomic as _atomic

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_atomic.os, "replace", failing_replace)

    # ASGITransport's default ``raise_app_exceptions=True`` surfaces the OSError
    # directly to the test. A route using ``Path.write_text`` would not raise —
    # it would truncate the file to the new (partial) content. So: if this
    # raises, the route is funnelling writes through ``atomic_write_text``.
    with pytest.raises(OSError, match="simulated replace failure"):
        await client.put(
            adapter["detail"]("preserve-me"),
            json={"content": "DESTRUCTIVE", "mtime_ns": mtime_ns},
        )

    # Original file untouched.
    assert target.read_text(encoding="utf-8") == original_payload
    # No leaked tempfile sibling.
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_POST_atomic_on_replace_failure_leaves_no_canonical_file(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """If ``os.replace`` fails during POST create, the working file does not
    exist and no ``.<name>.*.tmp`` sibling is left behind. Symmetric to the
    PUT atomicity test above — proves create goes through atomic_write_text
    on all three route types.

    For the versioned (dir-layout) types it additionally pins the ADR-0022
    rollback: a failed create must not leave an orphan artifact directory that
    would wedge every retry on the orphan-dir 409 guard — a retry once the
    transient failure clears must succeed.
    """
    import memtomem.context._atomic as _atomic

    def failing_replace(src, dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr(_atomic.os, "replace", failing_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        await client.post(
            adapter["list_url"],
            json=adapter["create_body"]("fresh-one"),
        )

    working = adapter["created_working"](gateway_root, "fresh-one")
    assert not working.exists(), f"{adapter['type']}: partial file was created"
    if working.parent.exists():
        leftover = list(working.parent.glob(f".{working.name}.*.tmp"))
        assert leftover == [], f"{adapter['type']}: tempfile leaked: {leftover}"

    if adapter["versioned"]:
        # The whole artifact dir must be rolled back (not just the working file),
        # else the orphan-dir 409 guard turns every retry into a permanent wedge.
        assert not working.parent.exists(), (
            f"{adapter['type']}: orphan artifact dir left behind — retry would 409"
        )
        monkeypatch.undo()
        retry = await client.post(adapter["list_url"], json=adapter["create_body"]("fresh-one"))
        assert retry.status_code == 200, f"{adapter['type']}: retry after rollback failed"


# ---------------------------------------------------------------------------
# Lock on non-POST verbs (#277 regression guard)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_holds_gateway_lock(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The lock is held at the moment ``atomic_write_text`` runs."""
    adapter["make_canonical"](gateway_root, "lock-probe")
    read = await client.get(adapter["detail"]("lock-probe"))
    mtime_ns = read.json()["mtime_ns"]

    from memtomem.web.routes._locks import _gateway_lock
    import memtomem.web.routes.context_agents as _ca
    import memtomem.web.routes.context_commands as _cc
    import memtomem.web.routes.context_skills as _cs

    observed = {"locked": False}
    real = _ca.atomic_write_text  # same symbol, all three route modules

    def probe(path, text, **kw):
        observed["locked"] = _gateway_lock.locked()
        return real(path, text, **kw)

    for mod in (_ca, _cc, _cs):
        monkeypatch.setattr(mod, "atomic_write_text", probe)

    r = await client.put(
        adapter["detail"]("lock-probe"),
        json={"content": "NEW", "mtime_ns": mtime_ns},
    )
    assert r.status_code == 200
    assert observed["locked"], adapter["type"]


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_DELETE_holds_gateway_lock(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    """The lock is held at the moment the file-system mutation runs."""
    adapter["make_canonical"](gateway_root, "del-probe")

    from memtomem.web.routes._locks import _gateway_lock
    import memtomem.web.routes.context_skills as _cs

    observed = {"locked_during_mutation": False}

    # Agents & commands use Path.unlink; skills uses shutil.rmtree.
    real_unlink = Path.unlink
    real_rmtree = shutil.rmtree

    def probe_unlink(self, *a, **kw):
        observed["locked_during_mutation"] = _gateway_lock.locked()
        return real_unlink(self, *a, **kw)

    def probe_rmtree(path, *a, **kw):
        observed["locked_during_mutation"] = _gateway_lock.locked()
        return real_rmtree(path, *a, **kw)

    monkeypatch.setattr(Path, "unlink", probe_unlink)
    monkeypatch.setattr(_cs.shutil, "rmtree", probe_rmtree)

    r = await client.delete(adapter["detail"]("del-probe"))
    assert r.status_code == 200
    assert observed["locked_during_mutation"], adapter["type"]


# ---------------------------------------------------------------------------
# PUT mtime_ns CAS semantics
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_stale_mtime_ns_rejected(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    adapter["make_canonical"](gateway_root, "cas")
    r = await client.put(
        adapter["detail"]("cas"),
        json={"content": "X", "mtime_ns": "0"},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "aborted"
    # Server surfaces the current mtime_ns as a string.
    assert isinstance(body["mtime_ns"], str)
    assert int(body["mtime_ns"]) > 0


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_malformed_mtime_ns_rejected(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    adapter["make_canonical"](gateway_root, "bad-mtime")
    r = await client.put(
        adapter["detail"]("bad-mtime"),
        json={"content": "X", "mtime_ns": "not-a-number"},
    )
    assert r.status_code == 422


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_concurrent_one_succeeds_other_409s(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    """Two concurrent PUTs with the same stale mtime_ns serialise under the
    lock: the first wins, the second reads the post-first-write mtime_ns and
    returns 409 (no lost-update silent clobber)."""
    adapter["make_canonical"](gateway_root, "race")
    read = await client.get(adapter["detail"]("race"))
    mtime_ns = read.json()["mtime_ns"]

    async def do_put(tag: str) -> int:
        resp = await client.put(
            adapter["detail"]("race"),
            json={"content": f"{tag}\n", "mtime_ns": mtime_ns},
        )
        return resp.status_code

    s1, s2 = await asyncio.gather(do_put("A"), do_put("B"))
    assert sorted([s1, s2]) == [200, 409], (adapter["type"], s1, s2)


# ---------------------------------------------------------------------------
# PUT force-save (issue #763) — bypass mtime guard after explicit user choice
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_force_bypasses_mtime_check(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    """``force: true`` accepts the write even when the server's mtime_ns
    differs from the client's — the audit-trail comes from the WARNING log
    (covered separately), not from rejecting the write."""
    manifest = adapter["make_canonical"](gateway_root, "force-ok")
    r = await client.put(
        adapter["detail"]("force-ok"),
        json={"content": "FORCED\n", "mtime_ns": "0", "force": True},
    )
    assert r.status_code == 200, r.text
    assert manifest.read_text(encoding="utf-8") == "FORCED\n"
    body = r.json()
    assert body["name"] == "force-ok"
    assert int(body["mtime_ns"]) > 0


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_force_logs_warning_with_both_mtimes(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
    caplog: pytest.LogCaptureFixture,
):
    """The audit-trail for force-save is the server log: every bypass emits
    a WARNING containing the path plus *both* mtime values so the override
    is reconstructable from logs alone."""
    manifest = adapter["make_canonical"](gateway_root, "force-log")
    server_mtime_ns = manifest.stat().st_mtime_ns

    with caplog.at_level(logging.WARNING):
        r = await client.put(
            adapter["detail"]("force-log"),
            json={"content": "X\n", "mtime_ns": "0", "force": True},
        )
    assert r.status_code == 200, r.text

    bypass_records = [rec for rec in caplog.records if "force-save bypassed" in rec.getMessage()]
    assert bypass_records, caplog.text
    msg = bypass_records[-1].getMessage()
    assert str(manifest) in msg
    assert "client_mtime_ns=0" in msg
    assert f"server_mtime_ns={server_mtime_ns}" in msg


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_PUT_force_default_false_still_409s(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    """Pin: the ``force`` field defaults to ``False``. A body that explicitly
    sets ``force: false`` (or omits the field — covered by
    ``test_PUT_stale_mtime_ns_rejected``) must still 409 on stale mtime, so
    the override remains opt-in. Symmetric pair to
    ``test_PUT_force_bypasses_mtime_check``."""
    adapter["make_canonical"](gateway_root, "force-default-off")
    r = await client.put(
        adapter["detail"]("force-default-off"),
        json={"content": "X", "mtime_ns": "0", "force": False},
    )
    assert r.status_code == 409
    body = r.json()
    assert body["status"] == "aborted"


# ---------------------------------------------------------------------------
# DELETE idempotence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_DELETE_missing_is_idempotent(
    adapter: dict,
    client: AsyncClient,
):
    r = await client.delete(adapter["detail"]("does-not-exist"))
    assert r.status_code == 200
    data = r.json()
    assert data["deleted"] == []
    assert data.get("skipped", []) == []


@pytest.mark.parametrize("adapter", TYPE_MATRIX)
@pytest.mark.anyio
async def test_DELETE_existing_removes_canonical(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    adapter["make_canonical"](gateway_root, "bye")
    r = await client.delete(adapter["detail"]("bye"))
    assert r.status_code == 200
    data = r.json()
    assert len(data["deleted"]) == 1
    assert not adapter["manifest"](gateway_root, "bye").exists()


# ---------------------------------------------------------------------------
# ADR-0008 directory-layout agents / commands (issue #899)
# ---------------------------------------------------------------------------


def _make_canonical_agent_dir(root: Path, name: str, content: str | None = None) -> Path:
    path = root / ".memtomem" / "agents" / name / "agent.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else _agent_content(name), encoding="utf-8")
    return path


def _make_canonical_command_dir(root: Path, name: str, content: str | None = None) -> Path:
    path = root / ".memtomem" / "commands" / name / "command.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content if content is not None else _command_content(), encoding="utf-8")
    return path


DIR_LAYOUT_MATRIX = [
    pytest.param(
        {
            "type": "agents",
            "list_url": "/api/context/agents",
            "detail": lambda n: f"/api/context/agents/{n}",
            "rendered": lambda n: f"/api/context/agents/{n}/rendered",
            "diff": lambda n: f"/api/context/agents/{n}/diff",
            "create_body": lambda n: {"name": n, "content": _agent_content(n)},
            "flat_manifest": lambda root, n: root / ".memtomem" / "agents" / f"{n}.md",
            "make_flat": _make_canonical_agent,
            "make_dir": _make_canonical_agent_dir,
            "dir_body": lambda n: _agent_content(n).replace("Body", "DIR BODY"),
            "flat_body": lambda n: _agent_content(n).replace("Body", "FLAT BODY"),
            "warn_fragment": "agents/both: reverse-sync updates dir layout",
        },
        id="agents-dir-layout",
    ),
    pytest.param(
        {
            "type": "commands",
            "list_url": "/api/context/commands",
            "detail": lambda n: f"/api/context/commands/{n}",
            "rendered": lambda n: f"/api/context/commands/{n}/rendered",
            "diff": lambda n: f"/api/context/commands/{n}/diff",
            "create_body": lambda n: {"name": n, "content": _command_content()},
            "flat_manifest": lambda root, n: root / ".memtomem" / "commands" / f"{n}.md",
            "make_flat": _make_canonical_command,
            "make_dir": _make_canonical_command_dir,
            "dir_body": lambda n: _command_content().replace("Body", "DIR BODY"),
            "flat_body": lambda n: _command_content().replace("Body", "FLAT BODY"),
            "warn_fragment": "commands/both: reverse-sync updates dir layout",
        },
        id="commands-dir-layout",
    ),
]


@pytest.mark.parametrize("adapter", DIR_LAYOUT_MATRIX)
@pytest.mark.anyio
async def test_dir_layout_detail_and_rendered_routes_return_200(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    adapter["make_dir"](gateway_root, "dir-item")

    detail = await client.get(adapter["detail"]("dir-item"))
    assert detail.status_code == 200, (adapter["type"], detail.text)
    assert "Body" in detail.json()["content"]

    rendered = await client.get(adapter["rendered"]("dir-item"))
    assert rendered.status_code == 200, (adapter["type"], rendered.text)


@pytest.mark.parametrize("adapter", DIR_LAYOUT_MATRIX)
@pytest.mark.anyio
async def test_dir_layout_update_uses_dir_file_and_preserves_mtime_409(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    target = adapter["make_dir"](gateway_root, "dir-cas")
    read = await client.get(adapter["detail"]("dir-cas"))
    mtime_ns = read.json()["mtime_ns"]

    update = await client.put(
        adapter["detail"]("dir-cas"),
        json={"content": "UPDATED\n", "mtime_ns": mtime_ns},
    )
    assert update.status_code == 200, (adapter["type"], update.text)
    assert target.read_text(encoding="utf-8") == "UPDATED\n"

    stale = await client.put(
        adapter["detail"]("dir-cas"),
        json={"content": "STALE\n", "mtime_ns": mtime_ns},
    )
    assert stale.status_code == 409
    assert stale.json()["status"] == "aborted"
    assert target.read_text(encoding="utf-8") == "UPDATED\n"


@pytest.mark.parametrize("adapter", DIR_LAYOUT_MATRIX)
@pytest.mark.anyio
async def test_dir_layout_delete_removes_dir_file_only(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    target = adapter["make_dir"](gateway_root, "dir-delete")

    r = await client.delete(adapter["detail"]("dir-delete"))
    assert r.status_code == 200, (adapter["type"], r.text)
    assert not target.exists()
    assert target.parent.is_dir()


@pytest.mark.parametrize("adapter", DIR_LAYOUT_MATRIX)
@pytest.mark.anyio
async def test_dir_layout_diff_returns_200(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    adapter["make_dir"](gateway_root, "dir-diff")

    r = await client.get(adapter["diff"]("dir-diff"))
    assert r.status_code == 200, (adapter["type"], r.text)
    assert r.json()["canonical_content"] is not None


@pytest.mark.parametrize("adapter", DIR_LAYOUT_MATRIX)
@pytest.mark.anyio
async def test_POST_rejects_existing_flat_or_dir_layout_without_shadowing(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
):
    adapter["make_dir"](gateway_root, "dir-exists")
    dir_resp = await client.post(adapter["list_url"], json=adapter["create_body"]("dir-exists"))
    assert dir_resp.status_code == 409, (adapter["type"], dir_resp.text)
    assert not adapter["flat_manifest"](gateway_root, "dir-exists").exists()

    adapter["make_flat"](gateway_root, "flat-exists")
    flat_resp = await client.post(adapter["list_url"], json=adapter["create_body"]("flat-exists"))
    assert flat_resp.status_code == 409, (adapter["type"], flat_resp.text)


@pytest.mark.parametrize("adapter", DIR_LAYOUT_MATRIX)
@pytest.mark.anyio
async def test_both_layouts_detail_prefers_dir_and_warns(
    adapter: dict,
    client: AsyncClient,
    gateway_root: Path,
    caplog: pytest.LogCaptureFixture,
):
    adapter["make_flat"](gateway_root, "both")
    adapter["flat_manifest"](gateway_root, "both").write_text(
        adapter["flat_body"]("both"), encoding="utf-8"
    )
    adapter["make_dir"](gateway_root, "both", adapter["dir_body"]("both"))

    with caplog.at_level(logging.WARNING):
        r = await client.get(adapter["detail"]("both"))

    assert r.status_code == 200, (adapter["type"], r.text)
    assert "DIR BODY" in r.json()["content"]
    assert "FLAT BODY" not in r.json()["content"]
    assert adapter["warn_fragment"] in caplog.text
