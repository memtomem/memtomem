from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import Mem2MemConfig
from memtomem.web.app import create_app


@pytest.fixture
def app(tmp_path: Path):
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = tmp_path
    application.state.storage = AsyncMock()
    application.state.config = Mem2MemConfig()
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _definition(command: str = "uvx") -> str:
    return json.dumps({"command": command, "args": ["--from", "demo", "demo-server"]}, indent=2)


@pytest.mark.anyio
async def test_create_list_read_sync_and_overview(client: AsyncClient, tmp_path: Path) -> None:
    create = await client.post(
        "/api/context/mcp-servers",
        json={"name": "demo", "content": _definition()},
    )
    assert create.status_code == 200
    assert create.json()["canonical_path"] == ".memtomem/mcp-servers/demo.json"

    listing = await client.get("/api/context/mcp-servers")
    assert listing.status_code == 200
    row = listing.json()["mcp-servers"][0]
    assert row["name"] == "demo"
    assert row["runtimes"] == [{"runtime": "project_mcp", "status": "missing target"}]

    detail = await client.get("/api/context/mcp-servers/demo")
    assert detail.status_code == 200
    assert detail.json()["fields"]["command"] == "uvx"

    (tmp_path / ".mcp.json").write_text(
        json.dumps({"project": "keep", "mcpServers": {"other": {"command": "node"}}}),
        encoding="utf-8",
    )
    sync = await client.post("/api/context/mcp-servers/sync")
    assert sync.status_code == 200
    assert sync.json()["generated"] == [{"runtime": "project_mcp", "path": ".mcp.json"}]

    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert written["project"] == "keep"
    assert written["mcpServers"]["other"] == {"command": "node"}
    assert written["mcpServers"]["demo"]["command"] == "uvx"

    overview = await client.get("/api/context/overview")
    assert overview.status_code == 200
    assert overview.json()["mcp_servers"]["total"] == 1
    assert overview.json()["mcp_servers"]["in_sync"] == 1


@pytest.mark.anyio
async def test_rejects_non_project_shared_scope(client: AsyncClient) -> None:
    r = await client.get("/api/context/mcp-servers", params={"target_scope": "user"})
    assert r.status_code == 400
    assert "project_shared" in r.json()["detail"]


@pytest.mark.anyio
async def test_create_rejects_invalid_json(client: AsyncClient) -> None:
    r = await client.post(
        "/api/context/mcp-servers",
        json={"name": "demo", "content": "{not json"},
    )
    assert r.status_code == 422
    assert "invalid JSON" in r.json()["detail"]


@pytest.mark.anyio
async def test_create_rejects_secret_shaped_content(client: AsyncClient) -> None:
    secret = "sk-" + "a" * 30
    r = await client.post(
        "/api/context/mcp-servers",
        json={
            "name": "leaky",
            "content": json.dumps({"command": "env", "env": {"OPENAI_API_KEY": secret}}),
        },
    )
    assert r.status_code == 422
    assert "privacy pattern" in r.json()["detail"]
    assert secret not in r.text
