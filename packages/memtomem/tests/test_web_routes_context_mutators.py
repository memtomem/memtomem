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
import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.skills import SKILL_MANIFEST
from memtomem.web.app import create_app


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def gateway_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sandbox ``HOME`` and return the project root."""
    monkeypatch.setenv("HOME", str(tmp_path))
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
    assert adapter["manifest"](gateway_root, "my-artifact.v2").is_file()


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
    """If ``os.replace`` fails during POST create, the target file does not
    exist and no ``.<name>.*.tmp`` sibling is left behind. Symmetric to the
    PUT atomicity test above — proves create goes through atomic_write_text
    on all three route types.
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

    target = adapter["manifest"](gateway_root, "fresh-one")
    assert not target.exists(), f"{adapter['type']}: partial file was created"
    if target.parent.exists():
        leftover = list(target.parent.glob(f".{target.name}.*.tmp"))
        assert leftover == [], f"{adapter['type']}: tempfile leaked: {leftover}"


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
