"""HTTP-layer tests for ``POST /api/context/sync-all-projects`` (A-9 #1279, ADR-0025).

The single-project sync-all route is pinned by
``test_web_routes_context_sync_all.py``; this file covers what the
cross-project batch adds:

- per-project entries embedding the single-project report verbatim, and
  batch ≡ single effect parity per executed project;
- skip semantics: paused / missing / stale scopes become reported
  ``skipped`` rows with reason codes (batch reports, never refuses);
- the all-skipped batch summary edge (``ok`` with ``executed: 0`` — a
  ``failed == executed``-first roll-up would mark 0/0 failed);
- per-project failure isolation (privacy block, per-project timeout) and
  the per-project — NOT batch-wide — ``_gateway_lock`` window;
- tier gates and the host-isolation acceptance criterion (user-tier
  fan-out must never fire);
- ``surface`` pass-through to the cores (kwarg pin — a monkeypatched
  engine test that ignores kwargs would false-pass the attribution).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.web.app import create_app
from memtomem.web.routes.context_sync_all import _SYNC_ALL_PHASES, _summarize_projects

from .helpers import set_home

_SKILL_BODY = b"# Demo skill\n\nDo the demo thing.\n"

_CMD_BODY = b"""---
description: Review code
allowed-tools: [Read, Grep]
---
Review the provided file.
"""

_AGENT_BODY = b"""---
name: reviewer
description: Code review agent
tools: [Read, Grep]
---
You are a code review agent.
"""

_SECRET_AGENT_BODY = b"""---
name: leaky
description: leaks a credential
---
key=AKIA1234567890ABCDEF
"""

_HOOK_RULE = {
    "matcher": "",
    "hooks": [{"type": "command", "command": "echo ok"}],
}


# ── Fixtures (mirror test_web_routes_context_sync_all.py) ────────────────


@pytest.fixture
def fake_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def cwd_root(tmp_path: Path, fake_home: Path) -> Path:
    """Return the cwd project root with markers + store (HOME sandboxed)."""
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


def _seed_artifacts(root: Path) -> None:
    """One canonical of every type (LF bytes — sync-style on every platform)."""
    skill = root / ".memtomem" / "skills" / "demo-skill"
    skill.mkdir(parents=True, exist_ok=True)
    (skill / "SKILL.md").write_bytes(_SKILL_BODY)

    commands = root / ".memtomem" / "commands"
    commands.mkdir(parents=True, exist_ok=True)
    (commands / "demo-cmd.md").write_bytes(_CMD_BODY)

    agents = root / ".memtomem" / "agents"
    agents.mkdir(parents=True, exist_ok=True)
    (agents / "reviewer.md").write_bytes(_AGENT_BODY)

    mcp = root / ".memtomem" / "mcp-servers"
    mcp.mkdir(parents=True, exist_ok=True)
    (mcp / "demo-srv.json").write_text(
        json.dumps({"command": "uvx", "args": ["demo-server"]}, indent=2),
        encoding="utf-8",
    )

    (root / ".memtomem" / "settings.json").write_text(
        json.dumps({"hooks": {"PostToolUse": [_HOOK_RULE]}}),
        encoding="utf-8",
    )


def _runtime_tree(root: Path) -> dict[str, bytes]:
    """Map of root-relative POSIX path → bytes for every runtime output."""
    tree: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if rel.startswith(".memtomem/"):
            continue
        tree[rel] = path.read_bytes()
    return tree


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


def _home_snapshot(home: Path) -> dict[str, bytes]:
    return {p.relative_to(home).as_posix(): p.read_bytes() for p in home.rglob("*") if p.is_file()}


# ── Report shape + effect parity ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_multi_project_report_and_parity_with_single_route(
    client, cwd_root: Path, fake_home: Path, tmp_path: Path
) -> None:
    """Executed entries embed the single-project report verbatim (phase
    order pinned), identically-seeded projects produce identical runtime
    trees, and the batch's writes match the single sync-all route's on a
    third identically-seeded project. The fake HOME stays byte-identical
    — the acceptance criterion that user-tier fan-out never fires."""
    project_a = _other_project(tmp_path, "proj-a")
    _seed_artifacts(cwd_root)
    _seed_artifacts(project_a)
    await _register(client, project_a)
    home_before = _home_snapshot(fake_home)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert [e["root"] for e in data["projects"]] == [str(cwd_root), str(project_a)]
    for root in (cwd_root, project_a):
        entry = _entry(data, root)
        assert entry["status"] == "ok"
        assert entry["project_scope_id"].startswith("p-")
        assert [p["type"] for p in entry["phases"]] == list(_SYNC_ALL_PHASES)
        assert all(p["status"] == "ok" for p in entry["phases"])
        assert entry["summary"]["status"] == "ok"
        assert entry["summary"]["ok"] == len(_SYNC_ALL_PHASES)

    tree_cwd = _runtime_tree(cwd_root)
    tree_a = _runtime_tree(project_a)
    assert tree_cwd and tree_cwd == tree_a
    assert any(rel.startswith(".claude/skills/") for rel in tree_a)
    assert ".mcp.json" in tree_a
    assert ".claude/settings.json" in tree_a

    summary = data["summary"]
    assert summary["status"] == "ok"
    assert summary["projects_total"] == 2
    assert summary["executed"] == 2
    assert summary["ok"] == 2
    assert summary["skipped"] == 0
    assert summary["generated_total"] > 0

    # Effect parity with the single-project route: a third identical
    # project synced via /context/sync-all lands the same runtime tree.
    project_b = _other_project(tmp_path, "proj-b")
    _seed_artifacts(project_b)
    scope_b = await _register(client, project_b)
    single = await client.post("/api/context/sync-all", params={"project_scope_id": scope_b})
    assert single.status_code == 200, single.text
    assert _runtime_tree(project_b) == tree_a

    # User-tier fan-out never fired: HOME is byte-identical.
    assert _home_snapshot(fake_home) == home_before


# ── Skip semantics ───────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paused_project_skipped_with_reason_sibling_executes(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """A paused project is a reported skip row — the single route's
    eligibility 409 has no batch analogue — and does not stop siblings."""
    paused = _other_project(tmp_path, "paused")
    _seed_artifacts(paused)
    await _register(client, paused, enabled=False)
    _seed_artifacts(cwd_root)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    entry = _entry(data, paused)
    assert entry["status"] == "skipped"
    assert entry["reason_code"] == "sync_paused"
    assert "paused" in entry["message"]
    assert "phases" not in entry
    assert _runtime_tree(paused) == {}

    assert _entry(data, cwd_root)["status"] == "ok"
    assert data["summary"]["status"] == "ok"
    assert data["summary"]["executed"] == 1
    assert data["summary"]["skipped"] == 1


@pytest.mark.asyncio
async def test_missing_and_stale_projects_skipped(client, cwd_root: Path, tmp_path: Path) -> None:
    import shutil

    gone = _other_project(tmp_path, "gone")
    await _register(client, gone)
    shutil.rmtree(gone)

    stale = tmp_path / "stale"
    stale.mkdir()
    (stale / ".claude").mkdir()  # runtime marker but no .memtomem store
    await _register(client, stale)

    _seed_artifacts(cwd_root)
    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert _entry(data, gone)["reason_code"] == "missing_root"
    stale_entry = _entry(data, stale)
    assert stale_entry["reason_code"] == "stale_project"
    assert "mm context init" in stale_entry["message"]
    # The batch-only stale gate really skipped: nothing was written there.
    assert _runtime_tree(stale) == {}
    assert data["summary"]["executed"] == 1
    assert data["summary"]["skipped"] == 2


@pytest.mark.asyncio
async def test_all_skipped_batch_is_ok_with_executed_zero(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """Codex design-gate fold: an all-skipped batch reads ``ok`` with
    ``executed: 0`` visible — skipping paused projects is the designed
    outcome, and a ``failed == executed``-first roll-up would mark 0/0
    as failed."""
    (cwd_root / ".memtomem").rmdir()  # cwd becomes stale (empty store dir)
    paused = _other_project(tmp_path, "paused")
    _seed_artifacts(paused)
    await _register(client, paused, enabled=False)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    assert {e["status"] for e in data["projects"]} == {"skipped"}
    assert _entry(data, cwd_root)["reason_code"] == "stale_project"
    assert _entry(data, paused)["reason_code"] == "sync_paused"
    summary = data["summary"]
    assert summary["status"] == "ok"
    assert summary["executed"] == 0
    assert summary["skipped"] == 2
    assert summary["projects_total"] == 2
    assert summary["generated_total"] == 0


# ── Failure isolation ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_privacy_block_fails_one_project_sibling_still_runs(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """Gate A in project A fails A's agents phase (entry ``partial``);
    the sibling still executes fully and the batch reads ``partial``.
    The blocked secret never round-trips into the batch report."""
    leaky = _other_project(tmp_path, "leaky")
    _seed_artifacts(leaky)
    (leaky / ".memtomem" / "agents" / "leaky.md").write_bytes(_SECRET_AGENT_BODY)
    await _register(client, leaky)
    _seed_artifacts(cwd_root)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    entry = _entry(data, leaky)
    assert entry["status"] == "partial"
    agents_phase = [p for p in entry["phases"] if p["type"] == "agents"][0]
    assert agents_phase["status"] == "failed"
    assert agents_phase["error"]["reason_code"] == "privacy_blocked"
    assert "AKIA1234567890ABCDEF" not in resp.text
    # Later phases of the SAME project still ran (ADR-0024 §1, per phase).
    assert (leaky / ".mcp.json").is_file()

    assert _entry(data, cwd_root)["status"] == "ok"
    assert data["summary"]["status"] == "partial"
    assert data["summary"]["partial"] == 1
    assert data["summary"]["ok"] == 1


@pytest.mark.asyncio
async def test_project_timeout_fails_row_and_batch_proceeds(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-project timeout converts to a failed entry (busy envelope,
    completed phases kept — here none, skills hangs first) and the NEXT
    project still executes: there is no batch-level timeout."""
    from memtomem.context import skills as skills_module
    from memtomem.web.routes import context_skills, context_sync_all

    slow = _other_project(tmp_path, "slow")
    _seed_artifacts(slow)
    await _register(client, slow)
    _seed_artifacts(cwd_root)

    monkeypatch.setattr(context_sync_all, "_SYNC_ALL_TIMEOUT_S", 0.2)
    real_generate = skills_module.generate_all_skills

    def conditional_slow(project_root, *args, **kwargs):
        if project_root == cwd_root:
            time.sleep(0.6)
        return real_generate(project_root, *args, **kwargs)

    monkeypatch.setattr(context_skills, "generate_all_skills", conditional_slow)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    timed_out = _entry(data, cwd_root)
    assert timed_out["status"] == "failed"
    assert timed_out["error"]["error_kind"] == "busy"
    assert timed_out["error"]["http_status"] == 503
    assert timed_out["phases"] == []  # skills hung — nothing completed
    assert "summary" not in timed_out  # summary present iff the loop completed

    survivor = _entry(data, slow)
    assert survivor["status"] == "ok"
    assert _runtime_tree(slow) != {}

    assert data["summary"]["status"] == "partial"
    assert data["summary"]["failed"] == 1
    assert data["summary"]["ok"] == 1


# ── Lock model + gates ───────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_lock_window_is_per_project(
    client, cwd_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ADR-0025 §3: one ``_gateway_lock`` acquisition PER EXECUTED project
    (released between projects), not one batch-wide window and not one per
    phase."""
    from memtomem.web.routes import _locks

    project_a = _other_project(tmp_path, "proj-a")
    _seed_artifacts(project_a)
    await _register(client, project_a)
    _seed_artifacts(cwd_root)

    acquisitions = 0
    real_aenter = _locks._LoopLocalLock.__aenter__

    async def counting_aenter(self):
        nonlocal acquisitions
        if self is _locks._gateway_lock:
            acquisitions += 1
        return await real_aenter(self)

    monkeypatch.setattr(_locks._LoopLocalLock, "__aenter__", counting_aenter)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    assert resp.json()["summary"]["executed"] == 2
    assert acquisitions == 2


@pytest.mark.asyncio
async def test_tier_gates_match_single_route(client) -> None:
    for tier, fragment in (("project_local", "project_shared"), ("user", "project-tier")):
        resp = await client.post("/api/context/sync-all-projects", params={"target_scope": tier})
        assert resp.status_code == 400, resp.text
        detail = resp.json()["detail"]
        assert detail["error_kind"] == "validation"
        assert fragment in detail["message"]


@pytest.mark.asyncio
async def test_surface_kwarg_reaches_cores(
    client, cwd_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The batch passes its own audit surface to the artifact cores; the
    single route keeps the A-8 default (kwarg pin — an engine monkeypatch
    that drops kwargs would false-pass the attribution)."""
    from memtomem.web.routes import context_sync_all

    _seed_artifacts(cwd_root)
    surfaces: list[str | None] = []

    async def capture_skills_core(project_root, target_scope, *, surface=None):
        surfaces.append(surface)
        return {"generated": [], "dropped": [], "skipped": [], "canonical_root": ""}

    monkeypatch.setattr(context_sync_all, "_sync_skills_core", capture_skills_core)

    resp = await client.post("/api/context/sync-all-projects")
    assert resp.status_code == 200, resp.text
    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    assert surfaces == ["web_context_sync_all_projects", "web_context_sync_all"]


# ── Unit pins for the batch roll-up ──────────────────────────────────────


def test_summarize_projects_ladder() -> None:
    ok = {"status": "ok", "phases": [{"generated": ["a"], "skipped": []}]}
    partial = {"status": "partial", "phases": [{"generated": [], "skipped": [{"r": 1}]}]}
    failed = {"status": "failed", "phases": []}
    skipped = {"status": "skipped"}

    all_skipped = _summarize_projects([skipped, skipped])
    assert all_skipped["status"] == "ok"
    assert all_skipped["executed"] == 0
    assert all_skipped["skipped"] == 2

    assert _summarize_projects([ok, skipped])["status"] == "ok"
    assert _summarize_projects([ok, partial])["status"] == "partial"
    assert _summarize_projects([ok, failed])["status"] == "partial"
    assert _summarize_projects([failed, failed, skipped])["status"] == "failed"

    totals = _summarize_projects([ok, partial, failed, skipped])
    assert totals["projects_total"] == 4
    assert totals["executed"] == 3
    assert totals["generated_total"] == 1
    assert totals["skipped_rows_total"] == 1
