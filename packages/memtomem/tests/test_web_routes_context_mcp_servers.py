from __future__ import annotations

import json
import logging
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
    # ``_safe_rel`` returns OS-native separators (matching the skills/commands/
    # agents routes), so compare component-wise rather than against a literal
    # forward-slash string — ``.memtomem\\mcp-servers\\demo.json`` on Windows.
    assert Path(create.json()["canonical_path"]) == Path(".memtomem/mcp-servers/demo.json")

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
    assert sync.json()["generated"] == [
        {"runtime": "project_mcp", "name": "demo", "path": ".mcp.json"}
    ]

    written = json.loads((tmp_path / ".mcp.json").read_text(encoding="utf-8"))
    assert written["project"] == "keep"
    assert written["mcpServers"]["other"] == {"command": "node"}
    assert written["mcpServers"]["demo"]["command"] == "uvx"

    overview = await client.get("/api/context/overview")
    assert overview.status_code == 200
    # ``total`` counts the runtime-only "other" entry too since #1247 id 31 —
    # same name-union semantics as the skills/commands/agents overview tiles.
    assert overview.json()["mcp_servers"]["total"] == 2
    assert overview.json()["mcp_servers"]["in_sync"] == 1
    assert overview.json()["mcp_servers"]["missing_canonical"] == 1


@pytest.mark.anyio
async def test_non_shared_tier_reads_empty_but_blocks_writes(
    client: AsyncClient, tmp_path: Path
) -> None:
    """Reads stay tier-tolerant; only writes reject non-shared tiers.

    A canonical server exists in project_shared, but listing/diffing on a
    ``user`` or ``project_local`` tier must report it as absent (empty / missing
    canonical), matching the overview & projects-counts path (``mcp_servers: 0``
    for non-shared). Previously the list route 400'd here, turning the UI panel
    into a load-failed state instead of the empty/disabled state the counts
    imply. Writes still reject — canonical residency is project_shared only.
    """
    server_dir = tmp_path / ".memtomem" / "mcp-servers"
    server_dir.mkdir(parents=True)
    (server_dir / "demo.json").write_text(_definition(), encoding="utf-8")

    for scope in ("user", "project_local"):
        listing = await client.get("/api/context/mcp-servers", params={"target_scope": scope})
        assert listing.status_code == 200, scope
        assert listing.json()["mcp-servers"] == [], scope

        diff = await client.get(
            "/api/context/mcp-servers/demo/diff", params={"target_scope": scope}
        )
        assert diff.status_code == 200, scope
        assert diff.json()["runtimes"][0]["status"] == "missing canonical", scope

        read = await client.get("/api/context/mcp-servers/demo", params={"target_scope": scope})
        assert read.status_code == 404, scope

    # Writes still reject on non-shared tiers.
    create = await client.post(
        "/api/context/mcp-servers",
        params={"target_scope": "user"},
        json={"name": "demo2", "content": _definition()},
    )
    assert create.status_code == 400
    assert "project_shared" in create.json()["detail"]

    sync = await client.post("/api/context/mcp-servers/sync", params={"target_scope": "user"})
    assert sync.status_code == 400
    assert "project_shared" in sync.json()["detail"]


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


def _seed_canonical(tmp_path: Path, name: str) -> Path:
    path = tmp_path / ".memtomem" / "mcp-servers" / f"{name}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_definition(), encoding="utf-8")
    return path


@pytest.mark.anyio
async def test_PUT_force_bypasses_mtime_and_logs_warning(
    client: AsyncClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """Force-save parity with skills/commands/agents (#1229): every mtime
    bypass emits a WARNING with the path plus BOTH mtime values so the
    override is reconstructable from logs alone."""
    path = _seed_canonical(tmp_path, "force-log")
    server_mtime_ns = path.stat().st_mtime_ns
    new_content = _definition("node")

    with caplog.at_level(logging.WARNING):
        r = await client.put(
            "/api/context/mcp-servers/force-log",
            json={"content": new_content, "mtime_ns": "0", "force": True},
        )
    assert r.status_code == 200, r.text
    assert path.read_text(encoding="utf-8") == new_content

    bypass_records = [rec for rec in caplog.records if "force-save bypassed" in rec.getMessage()]
    assert bypass_records, caplog.text
    msg = bypass_records[-1].getMessage()
    assert str(path) in msg
    assert "client_mtime_ns=0" in msg
    assert f"server_mtime_ns={server_mtime_ns}" in msg


@pytest.mark.anyio
async def test_PATCH_force_logs_warning_too(
    client: AsyncClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The PATCH alias routes through the same impl — same audit contract."""
    _seed_canonical(tmp_path, "force-patch")
    with caplog.at_level(logging.WARNING):
        r = await client.patch(
            "/api/context/mcp-servers/force-patch",
            json={"content": _definition("node"), "mtime_ns": "0", "force": True},
        )
    assert r.status_code == 200, r.text
    assert any("force-save bypassed" in rec.getMessage() for rec in caplog.records)


@pytest.mark.anyio
async def test_PUT_force_with_matching_mtime_stays_silent(
    client: AsyncClient, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """force=True with a matching mtime bypasses nothing — no WARNING."""
    path = _seed_canonical(tmp_path, "force-clean")
    with caplog.at_level(logging.WARNING):
        r = await client.put(
            "/api/context/mcp-servers/force-clean",
            json={
                "content": _definition("node"),
                "mtime_ns": str(path.stat().st_mtime_ns),
                "force": True,
            },
        )
    assert r.status_code == 200, r.text
    assert not any("force-save bypassed" in rec.getMessage() for rec in caplog.records)


@pytest.mark.anyio
async def test_PUT_force_default_false_still_409s(client: AsyncClient, tmp_path: Path) -> None:
    path = _seed_canonical(tmp_path, "stale")
    original = path.read_text(encoding="utf-8")
    r = await client.put(
        "/api/context/mcp-servers/stale",
        json={"content": _definition("node"), "mtime_ns": "0"},
    )
    assert r.status_code == 409
    assert r.json()["status"] == "aborted"
    assert path.read_text(encoding="utf-8") == original


@pytest.mark.anyio
async def test_diff_reasons_distinguish_canonical_vs_mcp_json(
    client: AsyncClient, tmp_path: Path
) -> None:
    """U7 (#1229): per-name diff reasons name the file that actually broke —
    canonical .json vs the project .mcp.json — and the payload carries
    canonical_path for the fix-it hint."""
    bad = tmp_path / ".memtomem" / "mcp-servers" / "bad.json"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("{not json", encoding="utf-8")
    r = await client.get("/api/context/mcp-servers/bad/diff")
    assert r.status_code == 200
    data = r.json()
    assert Path(data["canonical_path"]) == Path(".memtomem/mcp-servers/bad.json")
    rt = data["runtimes"][0]
    assert rt["status"] == "parse error"
    assert "bad.json" in rt["reason"]
    assert str(tmp_path) not in rt["reason"]

    bad.write_text(json.dumps({"command": "uvx"}), encoding="utf-8")
    (tmp_path / ".mcp.json").write_text("{broken", encoding="utf-8")
    r = await client.get("/api/context/mcp-servers/bad/diff")
    rt = r.json()["runtimes"][0]
    assert rt["status"] == "parse error"
    assert ".mcp.json" in rt["reason"]


# ── #1247 B8: id 40 / id 31 — invalid-name tolerance + runtime-only rows ────


@pytest.mark.anyio
async def test_list_tolerates_invalid_named_canonical(client: AsyncClient, tmp_path: Path) -> None:
    """One stray 'my server.json' used to 500 the whole list route (#1247
    id 40). The valid canonical keeps serving; the invalid name surfaces as a
    runtime-only row whose runtime status is 'invalid name'."""
    create = await client.post(
        "/api/context/mcp-servers",
        json={"name": "good", "content": _definition()},
    )
    assert create.status_code == 200
    stray = tmp_path / ".memtomem" / "mcp-servers" / "my server.json"
    stray.write_text(_definition(), encoding="utf-8")

    listing = await client.get("/api/context/mcp-servers")
    assert listing.status_code == 200
    rows = {row["name"]: row for row in listing.json()["mcp-servers"]}
    assert rows["good"]["canonical_path"] is not None
    assert rows["my server"]["canonical_path"] is None
    assert rows["my server"]["runtimes"][0]["status"] == "invalid name"


@pytest.mark.anyio
async def test_list_includes_runtime_only_servers(client: AsyncClient, tmp_path: Path) -> None:
    """A server present only in .mcp.json gets a canonical_path-less row with
    'missing canonical' status — mirror of the skills list route (#1247 id 31)."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"adhoc": {"command": "node"}}}),
        encoding="utf-8",
    )
    listing = await client.get("/api/context/mcp-servers")
    assert listing.status_code == 200
    rows = listing.json()["mcp-servers"]
    assert rows and rows[0]["name"] == "adhoc"
    assert rows[0]["canonical_path"] is None
    assert rows[0]["runtimes"] == [{"runtime": "project_mcp", "status": "missing canonical"}]


@pytest.mark.anyio
async def test_diff_route_returns_runtime_content_for_runtime_only(
    client: AsyncClient, tmp_path: Path
) -> None:
    """The runtime-only detail pane fetches /diff — it must carry the actual
    .mcp.json definition, not an empty 'missing canonical' shell (#1247 id 31)."""
    (tmp_path / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"adhoc": {"command": "node", "args": ["server.js"]}}}),
        encoding="utf-8",
    )
    diff = await client.get("/api/context/mcp-servers/adhoc/diff")
    assert diff.status_code == 200
    body = diff.json()
    runtime = body["runtimes"][0]
    assert runtime["status"] == "missing canonical"
    assert runtime["runtime_content"] is not None
    assert "server.js" in runtime["runtime_content"]


# ── #1247 B8: id 42 / id 43 — named generated rows + in-sync skip ────────────


@pytest.mark.anyio
async def test_sync_payload_carries_names_and_in_sync_rerun_skips(
    client: AsyncClient, tmp_path: Path
) -> None:
    create = await client.post(
        "/api/context/mcp-servers",
        json={"name": "demo", "content": _definition()},
    )
    assert create.status_code == 200

    first = await client.post("/api/context/mcp-servers/sync")
    assert first.status_code == 200
    assert first.json()["generated"] == [
        {"runtime": "project_mcp", "name": "demo", "path": ".mcp.json"}
    ]
    mtime_before = (tmp_path / ".mcp.json").stat().st_mtime_ns

    second = await client.post("/api/context/mcp-servers/sync")
    assert second.status_code == 200
    body = second.json()
    assert body["generated"] == []
    assert len(body["skipped"]) == 1
    assert body["skipped"][0]["reason_code"] == "in_sync"
    assert (tmp_path / ".mcp.json").stat().st_mtime_ns == mtime_before
