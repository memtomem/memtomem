"""Golden wire-shape fixtures for the Context Gateway read endpoints (#1692).

#1692 adds additive fields to these responses across several PRs while
promising that existing field names, types, and positions stay stable through
the next minor release. These tests pin the *serialized shape* — key sets,
key order, and value types — of each read endpoint against a checked-in
golden, so any wire change (additive or not) shows up as an explicit golden
diff in review instead of riding along silently.

Values are deliberately NOT pinned: every leaf is reduced to a type tag
(``"str"``, ``"int"``, ``"bool"``, ``"null"``, ...) before comparison, so
machine-specific roots, timestamps, and git hashes cannot make the goldens
flaky. The comparison is on the *rendered JSON text*, which is what makes key
order part of the pin (dict key order survives ``json.dumps``).

Regenerate after an intentional wire change with::

    MEMTOMEM_UPDATE_WIRE_GOLDENS=1 uv run pytest \
        packages/memtomem/tests/test_web_wire_fixtures.py

and review the golden diff like any other contract change.

The POST report endpoints (Sync All, Import) are pinned in the sibling
``test_web_wire_fixtures_reports.py`` — their goldens need seeded-mutation
setups this read-only file deliberately avoids.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.web.app import create_app

from .helpers import set_home

_GOLDEN_DIR = Path(__file__).parent / "data" / "wire"

_AGENT_BODY = b"""---
name: reviewer
description: Code review agent
tools: [Read, Grep]
---
You are a code review agent.
"""


# ── Fixtures (mirror test_web_routes_context_status_all.py) ───────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("MEMTOMEM_WIKI_PATH", raising=False)
    # Kimi's locations honor these env vars (runtime_registry) — a developer
    # machine that sets either would leak a real config into the runtime
    # probes and change the golden shapes (mirrors test_runtime_registry.py's
    # autouse isolation).
    monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    return home


@pytest.fixture
def cwd_root(tmp_path: Path, fake_home: Path) -> Path:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude").mkdir()
    (cwd / ".memtomem").mkdir()
    # One canonical agent so counts/diffs describe a non-empty store.
    agents = cwd / ".memtomem" / "agents"
    agents.mkdir(parents=True)
    (agents / "reviewer.md").write_bytes(_AGENT_BODY)
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


async def _register_second_project(client, tmp_path: Path) -> None:
    """A second (registered) scope so list-shaped responses pin >1 entry."""
    other = tmp_path / "other"
    other.mkdir()
    (other / ".claude").mkdir()
    (other / ".memtomem").mkdir()
    resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    assert resp.status_code == 200, resp.text


# ── Golden machinery ──────────────────────────────────────────────────────


def _shape(value: object) -> object:
    """Reduce a decoded JSON payload to its shape: containers keep structure
    (and key order), every scalar leaf becomes a type tag."""
    if isinstance(value, dict):
        return {key: _shape(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_shape(item) for item in value]
    if value is None:
        return "null"
    # bool before int: bool is an int subclass.
    if isinstance(value, bool):
        return "bool"
    if isinstance(value, int):
        return "int"
    if isinstance(value, float):
        return "float"
    if isinstance(value, str):
        return "str"
    return type(value).__name__  # pragma: no cover — JSON has no other leaf


def _assert_matches_golden(name: str, payload: dict) -> None:
    rendered = json.dumps(_shape(payload), indent=2) + "\n"
    path = _GOLDEN_DIR / f"{name}.json"
    if os.environ.get("MEMTOMEM_UPDATE_WIRE_GOLDENS") == "1":
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered, encoding="utf-8")
    golden = path.read_text(encoding="utf-8")
    assert rendered == golden, (
        f"wire shape of {name!r} changed (key set, key order, or value types).\n"
        f"If intentional, regenerate with MEMTOMEM_UPDATE_WIRE_GOLDENS=1 and "
        f"review the golden diff.\n--- golden ---\n{golden}\n--- actual ---\n{rendered}"
    )


# ── Read-endpoint pins ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_overview_wire_shape(client) -> None:
    resp = await client.get("/api/context/overview")
    assert resp.status_code == 200
    _assert_matches_golden("overview", resp.json())


@pytest.mark.asyncio
async def test_projects_wire_shape_bare(client, tmp_path: Path) -> None:
    await _register_second_project(client, tmp_path)
    resp = await client.get("/api/context/projects")
    assert resp.status_code == 200
    _assert_matches_golden("projects_bare", resp.json())


@pytest.mark.asyncio
async def test_projects_wire_shape_with_includes(client, tmp_path: Path) -> None:
    await _register_second_project(client, tmp_path)
    resp = await client.get("/api/context/projects?include=counts,runtime_coverage")
    assert resp.status_code == 200
    _assert_matches_golden("projects_include", resp.json())


@pytest.mark.asyncio
async def test_runtimes_wire_shape(client) -> None:
    resp = await client.get("/api/context/runtimes")
    assert resp.status_code == 200
    _assert_matches_golden("runtimes", resp.json())


@pytest.mark.asyncio
async def test_status_all_wire_shape(client, tmp_path: Path) -> None:
    await _register_second_project(client, tmp_path)
    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200
    _assert_matches_golden("status_all", resp.json())


@pytest.mark.asyncio
async def test_pull_preview_wire_shape(client, cwd_root: Path) -> None:
    """ADR-0030 PR-B — a divergent two-candidate skill pins the pull-preview
    wire (both axes, the §5 signal, and the candidate row shape)."""
    for runtime_dir, marker in ((".claude", "stale"), (".agents", "fresh")):
        d = cwd_root / runtime_dir / "skills" / "demo"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_bytes(f"---\nname: demo\n---\n{marker}\n".encode())
    resp = await client.get("/api/context/skills/demo/pull-preview")
    assert resp.status_code == 200, resp.text
    _assert_matches_golden("pull_preview", resp.json())


@pytest.mark.asyncio
async def test_pull_apply_wire_shape(client, cwd_root: Path) -> None:
    """ADR-0030 PR-D — a divergent two-candidate skill (no source_runtime)
    refuses with ``source_conflict``, pinning the richest apply-response shape:
    the candidate rows plus every refusal field, all on a 200 (the result-coded
    contract)."""
    for runtime_dir, marker in ((".claude", "stale"), (".agents", "fresh")):
        d = cwd_root / runtime_dir / "skills" / "demo"
        d.mkdir(parents=True, exist_ok=True)
        (d / "SKILL.md").write_bytes(f"---\nname: demo\n---\n{marker}\n".encode())
    resp = await client.post(
        "/api/context/skills/demo/pull", params={"target_scope": "project_shared"}, json={}
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "source_conflict"
    _assert_matches_golden("pull_apply", resp.json())


@pytest.mark.asyncio
async def test_status_global_wire_shape(client, fake_home: Path) -> None:
    """ADR-0030 PR-F — a divergent user-tier skill pins the global-status wire:
    store counts, the runtime-coverage list, and a POPULATED pull-drift row
    (nesting + field order the ``verdict`` Literal parity test cannot cover)."""
    store = fake_home / ".memtomem" / "skills" / "demo"
    store.mkdir(parents=True, exist_ok=True)
    (store / "SKILL.md").write_bytes(b"---\nname: demo\n---\nstore v1\n")
    runtime = fake_home / ".claude" / "skills" / "demo"
    runtime.mkdir(parents=True, exist_ok=True)
    (runtime / "SKILL.md").write_bytes(b"---\nname: demo\n---\nruntime v2\n")
    resp = await client.get("/api/context/status-global")
    assert resp.status_code == 200, resp.text
    assert resp.json()["pull_drift"]["rows"], "golden must pin a populated drift row"
    _assert_matches_golden("status_global", resp.json())
