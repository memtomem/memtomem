"""Context Portal runtime-registration surface (ADR-0021 §B).

Covers the PR1 wrappers over ``runtime_registry``:
- ``GET /api/context/runtimes`` (client axis: claude/antigravity/codex/kimi),
- the additive ``overview.detected_runtimes`` enrichment (gemini->antigravity),
- ``mem_context_detect(include_runtimes=True)``,
- the invariant that ``runtimes`` never widens the shared MCP sync include set.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.web.app import create_app

from .helpers import set_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    set_home(monkeypatch, tmp_path)
    return tmp_path


def _register_antigravity(home: Path) -> None:
    """Write a memtomem registration into the Antigravity CLI config under home."""
    cfg = home / ".gemini" / "antigravity-cli" / "mcp_config.json"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps({"mcpServers": {"memtomem": {}}}), encoding="utf-8")


@pytest.fixture
def app(home: Path, tmp_path: Path):
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = cwd
    application.state.storage = AsyncMock()
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=tmp_path / "kp.json",
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


# --- GET /api/context/runtimes ------------------------------------------------


@pytest.mark.asyncio
async def test_runtimes_route_shape(client) -> None:
    resp = await client.get("/api/context/runtimes")
    assert resp.status_code == 200
    data = resp.json()
    assert "project_root" in data
    assert data["runtimes_status"] == "ok"
    assert data["warnings"] == []
    names = [r["name"] for r in data["runtimes"]]
    assert names == ["claude", "antigravity", "codex", "kimi"]
    for r in data["runtimes"]:
        assert set(r) >= {
            "name",
            "installed",
            "memtomem_registered",
            "mms_registered",
            "registered_locations",
            "config_paths",
            "error_kind",
        }


@pytest.mark.asyncio
async def test_runtimes_route_probe_failure_envelope(client, monkeypatch) -> None:
    """A probe-machinery failure must not be wire-identical to "no clients".

    #1692 PR 6: the route used to collapse the exception to ``runtimes: []``
    — indistinguishable from a healthy empty host. Patch at the definition
    site (the route imports ``probe_all_runtimes`` function-locally).
    """
    # ``_redact_message`` collapses the import-time $HOME (frozen in
    # ``_errors._HOME``), not the test's monkeypatched home — raise with the
    # frozen value so the assertion exercises the real redaction chokepoint.
    from memtomem.web.routes._errors import _HOME

    def _boom(*args, **kwargs):
        raise PermissionError(f"{_HOME}/secret denied")

    monkeypatch.setattr("memtomem.context.runtime_registry.probe_all_runtimes", _boom)
    resp = await client.get("/api/context/runtimes")
    assert resp.status_code == 200
    data = resp.json()
    assert data["runtimes"] == []
    assert data["runtimes_status"] == "unavailable"
    (warning,) = data["warnings"]
    assert warning["reason_code"] == "status_unavailable"
    assert warning["error_kind"] == "permission"
    assert warning["retryable"] is True
    # Privacy pin: the redacted message must not leak the absolute $HOME path.
    assert _HOME not in warning["message"]
    assert warning["message"] == "~/secret denied"


@pytest.mark.asyncio
async def test_runtimes_route_detects_antigravity(client, home: Path) -> None:
    _register_antigravity(home)
    resp = await client.get("/api/context/runtimes")
    agy = next(r for r in resp.json()["runtimes"] if r["name"] == "antigravity")
    assert agy["installed"] is True
    assert agy["memtomem_registered"] is True
    assert agy["registered_locations"] == ["cli"]


# --- overview.detected_runtimes additive enrichment ---------------------------


@pytest.mark.asyncio
async def test_overview_detected_runtimes_enriched(client, home: Path) -> None:
    _register_antigravity(home)
    resp = await client.get("/api/context/overview")
    assert resp.status_code == 200
    runtimes = {r["name"]: r for r in resp.json()["detected_runtimes"]}
    # Backward-compat: name + available preserved for every KNOWN_RUNTIME.
    assert {"claude", "gemini", "codex", "kimi"} <= set(runtimes)
    for r in runtimes.values():
        assert "available" in r
        assert "installed" in r
        assert "memtomem_registered" in r
    # gemini runtime entry reflects the Antigravity client (gemini->antigravity).
    assert runtimes["gemini"]["memtomem_registered"] is True
    assert runtimes["claude"]["memtomem_registered"] is False
    # Healthy path: the #1692 PR 6 availability flag reads false.
    assert resp.json()["detected_runtimes_unavailable"] is False


@pytest.mark.asyncio
async def test_overview_detected_runtimes_unavailable(client, monkeypatch) -> None:
    """A failed detection probe must not read as "no runtimes" (#1692 PR 6).

    ``compute_runtime_coverage`` is imported function-locally by the route's
    ``_compute_detected_runtimes`` helper, so patch its definition site
    (mirrors the PR 5 pattern in test_web_routes_context_projects.py).
    """

    def _boom(*args, **kwargs):
        raise RuntimeError("coverage probe exploded")

    monkeypatch.setattr("memtomem.context.runtime_coverage.compute_runtime_coverage", _boom)
    resp = await client.get("/api/context/overview")
    assert resp.status_code == 200
    data = resp.json()
    assert data["detected_runtimes"] == []
    assert data["detected_runtimes_unavailable"] is True


# --- mem_context_detect(include_runtimes=True) --------------------------------


@pytest.mark.asyncio
async def test_mem_context_detect_include_runtimes(home: Path, monkeypatch) -> None:
    from memtomem.server.tools import context as ctxtool

    monkeypatch.setattr(ctxtool, "_find_project_root", lambda: home)
    _register_antigravity(home)
    out = await ctxtool.mem_context_detect(include_runtimes=True)
    assert "Provider-client registration:" in out
    assert "antigravity: memtomem registered" in out


# --- shared sync include contract is NOT widened (Codex round-3 Major-1) -------


def test_runtimes_not_in_shared_sync_include() -> None:
    from memtomem.server.tools.context import _KNOWN_INCLUDES, _parse_include

    assert "runtimes" not in _KNOWN_INCLUDES
    with pytest.raises(ValueError):
        _parse_include("runtimes")
