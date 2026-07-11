"""Golden wire-shape fixtures for the Context Gateway POST report endpoints (#1692 PR 7).

Sibling of ``test_web_wire_fixtures.py`` (which pins the read endpoints and
owns the golden machinery — ``_shape`` / ``_assert_matches_golden`` are
imported from there). These endpoints mutate the seeded tmp project, so they
get their own file: the read-only suite deliberately avoids seeded-mutation
setups.

Pinned shape variants (one golden per variant, not per kind — the three
artifact kinds share the ``_import_payload`` builders byte-for-byte):

- ``sync_all``            — single-project five-phase report
- ``sync_all_projects``   — cross-project batch report (ADR-0025)
- ``import_agents``       — bulk import: populated ``imported`` + ``skipped``
                            and the always-present bulk ``dry_run`` key
- ``import_agent_single`` — single-item import: ``dry_run`` OMITTED
                            (``response_model_exclude_unset`` contract)
- ``import_needs_confirmation`` — unconfirmed user-tier import: the
                            ``host_write_gate`` envelope with nested ``plan``

Regenerate after an intentional wire change with::

    MEMTOMEM_UPDATE_WIRE_GOLDENS=1 uv run pytest \
        packages/memtomem/tests/test_web_wire_fixtures_reports.py

and review the golden diff like any other contract change.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.web.app import create_app

from .helpers import set_home
from .test_web_wire_fixtures import _assert_matches_golden

_AGENT_BODY = b"""---
name: reviewer
description: Code review agent
tools: [Read, Grep]
---
You are a code review agent.
"""

_RUNTIME_AGENT_BODY = b"""---
name: newagent
description: Freshly authored runtime agent
tools: [Read]
---
You are a new runtime agent.
"""


# ── Fixtures (mirror test_web_wire_fixtures.py) ───────────────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("MEMTOMEM_WIKI_PATH", raising=False)
    # Kimi's locations honor these env vars (runtime_registry) — a developer
    # machine that sets either would leak a real config into the runtime
    # probes and change the golden shapes.
    monkeypatch.delenv("KIMI_SHARE_DIR", raising=False)
    monkeypatch.delenv("KIMI_CODE_HOME", raising=False)
    return home


@pytest.fixture
def cwd_root(tmp_path: Path, fake_home: Path) -> Path:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude").mkdir()
    (cwd / ".memtomem").mkdir()
    # One canonical agent so the sync agents phase has fan-out work to report.
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
    """A second (registered) scope so the batch report pins >1 project entry."""
    other = tmp_path / "other"
    other.mkdir()
    (other / ".claude").mkdir()
    (other / ".memtomem").mkdir()
    resp = await client.post("/api/context/known-projects", json={"root": str(other)})
    assert resp.status_code == 200, resp.text


def _seed_runtime_agents(cwd_root: Path) -> None:
    """One importable runtime agent + one runtime copy of the existing
    canonical, so the import report pins BOTH arrays populated."""
    runtime_agents = cwd_root / ".claude" / "agents"
    runtime_agents.mkdir(parents=True, exist_ok=True)
    (runtime_agents / "newagent.md").write_bytes(_RUNTIME_AGENT_BODY)
    (runtime_agents / "reviewer.md").write_bytes(_AGENT_BODY)


# ── Sync All pins ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_sync_all_wire_shape(client) -> None:
    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # The agents phase fans out the seeded canonical — a report of pure
    # empty phases would pin a weaker shape than production emits.
    agents_phase = next(p for p in payload["phases"] if p["type"] == "agents")
    assert agents_phase["generated"], agents_phase
    _assert_matches_golden("sync_all", payload)


@pytest.mark.asyncio
async def test_sync_all_projects_wire_shape(client, tmp_path: Path) -> None:
    await _register_second_project(client, tmp_path)
    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["summary"]["executed"] == 2, payload["summary"]
    _assert_matches_golden("sync_all_projects", payload)


# ── Import pins ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_import_bulk_wire_shape(client, cwd_root: Path) -> None:
    _seed_runtime_agents(cwd_root)
    resp = await client.post("/api/context/agents/import")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # Bulk responses always carry dry_run; both arrays are populated so the
    # golden pins the row shapes, not just empty lists.
    assert payload["imported"] and payload["skipped"], payload
    assert payload["dry_run"] is False
    _assert_matches_golden("import_agents", payload)


@pytest.mark.asyncio
async def test_import_single_wire_shape(client, cwd_root: Path) -> None:
    _seed_runtime_agents(cwd_root)
    resp = await client.post("/api/context/agents/newagent/import")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    # The single-item response never carried dry_run — absent, not null
    # (the response_model_exclude_unset contract this golden pins).
    assert "dry_run" not in payload
    _assert_matches_golden("import_agent_single", payload)


@pytest.mark.asyncio
async def test_import_needs_confirmation_wire_shape(client, fake_home: Path) -> None:
    # A user-tier import reads the HOME runtime roots; an unconfirmed request
    # with pending writes returns the host_write_gate disclosure envelope
    # (200 + status, not an error) with the dry-run preview nested as plan.
    home_agents = fake_home / ".claude" / "agents"
    home_agents.mkdir(parents=True)
    (home_agents / "homeagent.md").write_bytes(_RUNTIME_AGENT_BODY)
    resp = await client.post("/api/context/agents/import?target_scope=user")
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "needs_confirmation", payload
    _assert_matches_golden("import_needs_confirmation", payload)
