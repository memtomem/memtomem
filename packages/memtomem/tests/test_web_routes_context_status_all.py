"""Web tests for ``GET /api/context/status-all`` (A-10 #1280).

The shared ``collect_project_status`` derivation is unit-pinned in
``test_context_status.py``; this file covers the route's own contracts:

- per-project entry shapes (``ok``/``drift``/``skipped``/``error``) and the
  counts-only batch summary;
- skip entries via the shared ``sync_skip_reason`` codes (full-literal pin);
- the project_shared tier gate (400 validation envelope);
- per-project crash isolation with pass-through kwarg pins (Codex fold);
- corrupt lockfile ⇒ entry ``error`` with the partial aggregate retained
  (Codex design-gate fold) and the sanitized ``lockfile_error`` string;
- wiki-absent degradation (the #1280 acceptance criterion);
- CLI ↔ web summary parity on one fixture tree (the shared-vocabulary
  acceptance criterion pinned end to end).
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem.cli.context_cmd import context as context_group
from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context._atomic import installed_at_from_dest
from memtomem.context.lockfile import Lockfile
from memtomem.context.status import collect_project_status
from memtomem.web.app import create_app
from memtomem.wiki.store import WikiStore

from .helpers import set_home

_AGENT_BODY = b"""---
name: reviewer
description: Code review agent
tools: [Read, Grep]
---
You are a code review agent.
"""

_SKIP_CODES = {"missing_root", "sync_paused", "sync_not_enrolled", "stale_project"}


# ── Fixtures (mirror test_web_routes_context_sync_all_projects.py) ───────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    monkeypatch.delenv("MEMTOMEM_WIKI_PATH", raising=False)
    return home


@pytest.fixture
def cwd_root(tmp_path: Path, fake_home: Path) -> Path:
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


# ── Helpers ──────────────────────────────────────────────────────────────


def _seed_agent(root: Path) -> None:
    agents = root / ".memtomem" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "reviewer.md").write_bytes(_AGENT_BODY)


def _seed_missing_install(root: Path) -> None:
    """Lockfile entry whose dest is gone — a wiki-axis ``missing`` row that
    needs no wiki repo at all."""
    Lockfile.at(root).upsert_entry(
        "agents", "ghost", wiki_commit="0" * 40, installed_at="2026-01-01T00:00:00Z"
    )


async def _register(client, root: Path, *, enabled: bool | None = None) -> str:
    resp = await client.post("/api/context/known-projects", json={"root": str(root)})
    assert resp.status_code == 200, resp.text
    scope_id = resp.json()["project_scope_id"]
    if enabled is not None:
        resp = await client.patch(
            f"/api/context/known-projects/{scope_id}", json={"enabled": enabled}
        )
        assert resp.status_code == 200, resp.text
    return scope_id


def _other_project(tmp_path: Path, name: str) -> Path:
    other = tmp_path / name
    other.mkdir()
    (other / ".claude").mkdir()
    (other / ".memtomem").mkdir()
    return other


def _entry(data: dict, root: Path) -> dict:
    matches = [p for p in data["projects"] if p["root"] == str(root)]
    assert len(matches) == 1, data["projects"]
    return matches[0]


# ── Entry shapes + summary ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_aggregate_two_projects_shape(client, cwd_root: Path, tmp_path: Path) -> None:
    """Drifted + clean entries carry the full aggregate shape; the summary
    is counts-only (deliberately no roll-up status string — fleet health is
    ``drifted + errors == 0``, derivable)."""
    _seed_agent(cwd_root)  # never synced → runtime drift
    _seed_missing_install(cwd_root)  # wiki-axis missing row
    clean = _other_project(tmp_path, "proj-clean")
    await _register(client, clean)

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert data["target_scope"] == "project_shared"
    drifted = _entry(data, cwd_root)
    assert drifted["status"] == "drift"
    assert drifted["project_scope_id"].startswith("p-")
    assert drifted["lockfile_error"] is None
    assert set(drifted["state_counts"]) == {
        "ok",
        "behind",
        "dirty",
        "missing",
        "stale-pin",
        "local-draft",
        "untracked",
    }
    assert drifted["state_counts"]["missing"] == 1
    assert drifted["state_counts"]["untracked"] == 1
    assert set(drifted["diff_counts"]) == {
        "skills",
        "commands",
        "agents",
        "mcp_servers",
        "settings",
    }
    assert drifted["diff_counts"]["agents"].get("missing_target", 0) >= 1
    ghost_rows = [r for r in drifted["rows"] if r["name"] == "ghost"]
    assert ghost_rows == [
        {
            "asset_type": "agents",
            "name": "ghost",
            "pin_commit": "0" * 40,
            "installed_at": "2026-01-01T00:00:00Z",
            "state": "missing",
            "dirty_file_count": 0,
            "reason": "dest missing",
            "tier": "project_shared",
        }
    ]

    clean_entry = _entry(data, clean)
    assert clean_entry["status"] == "ok"
    assert clean_entry["rows"] == []

    assert data["summary"] == {
        "projects_total": 2,
        "executed": 2,
        "drifted": 1,
        "clean": 1,
        "errors": 0,
        "skipped": 0,
    }


@pytest.mark.asyncio
async def test_skip_entries_shared_reason_codes(client, cwd_root: Path, tmp_path: Path) -> None:
    """Ineligible scopes become skip entries — reason codes are the shared
    ``sync_skip_reason`` literals, full-set pinned; no crash on a missing
    or stale root (the #1280 acceptance criterion)."""
    paused = _other_project(tmp_path, "paused")
    await _register(client, paused, enabled=False)
    gone = _other_project(tmp_path, "gone")
    await _register(client, gone)
    shutil.rmtree(gone)
    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / ".claude").mkdir()  # markers but no .memtomem store
    await _register(client, stale)

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    by_code = {e["reason_code"]: e for e in data["projects"] if e["status"] == "skipped"}
    assert set(by_code) == {"sync_paused", "missing_root", "stale_project"}
    assert set(by_code) < _SKIP_CODES
    for entry in by_code.values():
        assert entry["message"]
        assert "rows" not in entry
    assert data["summary"]["skipped"] == 3
    assert data["summary"]["executed"] == 1  # the cwd scope


@pytest.mark.asyncio
async def test_tier_gate_400_validation_envelope(client) -> None:
    for tier in ("user", "project_local"):
        resp = await client.get("/api/context/status-all", params={"target_scope": tier})
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error_kind"] == "validation"
        assert "project_shared tier only" in detail["message"]


# ── Error isolation ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_collector_crash_error_entry_sibling_intact(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One project's crash becomes an ``error`` entry (A-9 failed-entry
    envelope shape) and the loop proceeds. The monkeypatched collector pins
    the pass-through kwargs (wiki instance + the batch tier) — a wrapper
    that dropped them would false-pass."""
    sibling = _other_project(tmp_path, "proj-b")
    await _register(client, sibling)
    seen_kwargs: list[dict] = []

    def _exploding(root, **kwargs):
        seen_kwargs.append(kwargs)
        if root == cwd_root:
            raise PermissionError("boom: unreadable tree")
        return collect_project_status(root, **kwargs)

    monkeypatch.setattr("memtomem.web.routes.context_gateway.collect_project_status", _exploding)

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    failed = _entry(data, cwd_root)
    assert failed["status"] == "error"
    assert failed["error"]["error_kind"] == "permission"
    assert "boom" in failed["error"]["message"]
    assert failed["error"]["http_status"] == 500
    assert _entry(data, sibling)["status"] == "ok"
    assert data["summary"]["errors"] == 1
    assert data["summary"]["clean"] == 1
    assert len(seen_kwargs) == 2
    for kwargs in seen_kwargs:
        assert kwargs["target_scope"] == "project_shared"
        assert isinstance(kwargs["wiki"], WikiStore)


@pytest.mark.asyncio
async def test_settings_diff_error_uses_status_shape_envelope(
    client, cwd_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex impl-review fold: a raising ``diff_settings`` must serialize
    with the OVERVIEW's settings envelope (status-based: ``status: error``
    + ``error_kind``/``error_message``), not the artifact kinds'
    count-based ``{"total": 0, "error": true}`` shape — one client must
    not meet two incompatible settings error envelopes across the two
    routes. The contained error still reads as drift."""

    def _boom(project_root, *, scope):
        raise RuntimeError("settings diff exploded")

    monkeypatch.setattr("memtomem.context.settings.diff_settings", _boom)

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), cwd_root)

    settings_envelope = entry["diff_counts"]["settings"]
    assert settings_envelope["status"] == "error"
    assert settings_envelope["error_kind"] == "internal"
    assert "settings diff exploded" in settings_envelope["error_message"]
    assert "total" not in settings_envelope  # the count-shape marker must NOT leak in
    assert entry["status"] == "drift"  # contained per-kind error == drift, not entry error


@pytest.mark.asyncio
async def test_lockfile_error_entry_keeps_partial_aggregate(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """Codex design-gate fold: a corrupt lockfile makes the entry ``error``
    while the partial aggregate (state_counts / diff_counts / rows) is
    retained, the summary counts it, and the sanitized error string never
    leaks the absolute project root."""
    (cwd_root / ".memtomem" / "lock.json").write_text("{not json", encoding="utf-8")
    skills = cwd_root / ".memtomem" / "skills" / "draft-skill"
    skills.mkdir(parents=True)
    (skills / "SKILL.md").write_text("# s\n", encoding="utf-8")

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    entry = _entry(data, cwd_root)
    assert entry["status"] == "error"
    assert entry["lockfile_error"]
    assert str(cwd_root) not in entry["lockfile_error"]
    assert entry["state_counts"]["untracked"] == 1  # partial aggregate retained
    assert [r["state"] for r in entry["rows"]] == ["untracked"]
    assert data["summary"]["errors"] == 1
    assert data["summary"]["executed"] == 1


@pytest.mark.asyncio
async def test_wiki_absent_degrades_to_stale_pin_rows(client, cwd_root: Path) -> None:
    """#1280 acceptance: with no wiki repo the clean install renders as a
    stale-pin row (reason pinned), wiki_head is null, and the project reads
    as drifted — exactly the single-project degradation."""
    dest = cwd_root / ".memtomem" / "skills" / "pinned"
    dest.mkdir(parents=True)
    (dest / "SKILL.md").write_bytes(b"# s\n")
    Lockfile.at(cwd_root).upsert_entry(
        "skills", "pinned", wiki_commit="0" * 40, installed_at=installed_at_from_dest(dest)
    )

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    entry = _entry(resp.json(), cwd_root)

    assert entry["status"] == "drift"
    assert entry["wiki_head"] is None
    assert entry["state_counts"]["stale-pin"] == 1
    row = [r for r in entry["rows"] if r["name"] == "pinned"][0]
    assert row["state"] == "stale-pin"
    assert row["reason"] == "wiki not present"


# ── CLI ↔ web parity ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cli_web_summary_parity(
    client,
    cwd_root: Path,
    tmp_path: Path,
    known_projects_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The shared-vocabulary acceptance criterion, end to end: one fixture
    tree drives both surfaces and the per-project classification counts
    must agree exactly (web summary == CLI summary line)."""
    _seed_agent(cwd_root)  # drift
    clean = _other_project(tmp_path, "proj-clean")
    await _register(client, clean)
    paused = _other_project(tmp_path, "paused")
    await _register(client, paused, enabled=False)

    resp = await client.get("/api/context/status-all")
    assert resp.status_code == 200, resp.text
    summary = resp.json()["summary"]
    assert summary == {
        "projects_total": 3,
        "executed": 2,
        "drifted": 1,
        "clean": 1,
        "errors": 0,
        "skipped": 1,
    }

    monkeypatch.setattr(
        "memtomem.cli.context_cmd.ContextGatewayConfig",
        lambda: ContextGatewayConfig(
            known_projects_path=known_projects_path,
            experimental_claude_projects_scan=False,
        ),
    )
    monkeypatch.chdir(cwd_root)
    result = CliRunner().invoke(context_group, ["status", "--all-projects"])

    assert result.exit_code == 0, result.output
    assert (
        f"Summary: {summary['drifted']} with drift, {summary['clean']} clean, "
        f"{summary['errors']} error(s), {summary['skipped']} skipped." in result.output
    )
