"""HTTP-layer tests for ``POST /api/context/sync-all`` (A-8 #1278, ADR-0024).

The per-type engines are pinned by their own suites; this file covers what
the aggregating route adds:

- effect parity with the front-end orchestrator (the five per-type POSTs)
  for the same project/tier — identical runtime trees AND identical
  phase-native fragments;
- per-phase report semantics: one phase fails, later phases still run and
  report (the ADR-0024 decision — NOT the front-end's stop-at-first-failure);
- the benign-skip contract (#1262): skip rows keep their raw
  ``{runtime, reason, reason_code}`` shape so classification stays
  client-side;
- the route-level gates: eligibility 409 (shared resolver shape), tier
  400s and the outer-timeout 503 on the ADR-0023 §10 envelope;
- exactly ONE ``_gateway_lock`` acquisition for the whole run.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.web.app import create_app
from memtomem.web.routes._sync_phase import SyncPhaseError
from memtomem.web.routes.context_sync_all import (
    _phase_error_envelope,
    _settings_severity,
    _SYNC_ALL_PHASES,
)

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


# ── Fixtures (mirror test_web_routes_context_transfer.py) ────────────────


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
    """Map of root-relative POSIX path → bytes for every runtime output.

    Excludes ``.memtomem/`` (canonical store + bookkeeping like version
    snapshots, whose ``surface`` attribution legitimately differs between
    the per-type routes and sync-all).
    """
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


def _other_project(tmp_path: Path, name: str) -> Path:
    other = tmp_path / name
    other.mkdir()
    (other / ".claude").mkdir()
    (other / ".memtomem").mkdir()
    return other


def _phase(data: dict, phase_type: str) -> dict:
    matches = [p for p in data["phases"] if p["type"] == phase_type]
    assert len(matches) == 1, data["phases"]
    return matches[0]


# ── Report shape ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_empty_project_phase_order_and_benign_skip_shape(client) -> None:
    """Phase order is pinned to the front-end's, and skip rows keep the raw
    ``{runtime, reason, reason_code}`` shape (#1262 — classification is the
    JS consumer's job, so ``no_canonical_root`` arrives verbatim)."""
    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["summary"]["changed"] is False
    assert data["summary"]["outcome"] == "noop"

    assert [p["type"] for p in data["phases"]] == list(_SYNC_ALL_PHASES)
    for phase_type in ("skills", "commands", "agents", "mcp-servers"):
        phase = _phase(data, phase_type)
        assert phase["status"] == "ok"
        assert phase["generated"] == []
        assert {s["reason_code"] for s in phase["skipped"]} == {"no_canonical_root"}
        for skip in phase["skipped"]:
            assert set(skip) == {"runtime", "reason", "reason_code"}
        assert phase["canonical_root"]

    settings = _phase(data, "settings")
    assert settings["status"] == "ok"
    # No canonical settings.json → every available generator reports an
    # in-band "skipped" row; the roll-up keeps the phase ok.
    assert settings["results"], settings
    assert {r["status"] for r in settings["results"]} == {"skipped"}
    assert "duplicate_tier_warnings" in settings

    assert data["summary"] == {
        "status": "ok",
        "changed": False,
        "outcome": "noop",
        "ok": 5,
        "failed": 0,
        "needs_confirmation": 0,
        "generated_total": 0,
        "skipped_total": sum(len(_phase(data, t)["skipped"]) for t in _SYNC_ALL_PHASES[:4]),
    }


@pytest.mark.asyncio
async def test_effect_parity_with_per_type_orchestration(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    """Acceptance criterion #1: for the same canonical set and tier, one
    sync-all call produces byte-identical runtime trees AND the identical
    phase-native fragments the five per-type POSTs return."""
    project_a = _other_project(tmp_path, "proj-a")
    project_b = _other_project(tmp_path, "proj-b")
    _seed_artifacts(project_a)
    _seed_artifacts(project_b)
    scope_a = await _register(client, project_a)
    scope_b = await _register(client, project_b)

    per_type: dict[str, dict] = {}
    for phase_type in ("skills", "commands", "agents", "mcp-servers"):
        resp = await client.post(
            f"/api/context/{phase_type}/sync", params={"project_scope_id": scope_a}
        )
        assert resp.status_code == 200, resp.text
        per_type[phase_type] = resp.json()
    resp = await client.post("/api/context/settings/sync", params={"project_scope_id": scope_a})
    assert resp.status_code == 200, resp.text
    per_type["settings"] = resp.json()

    resp = await client.post("/api/context/sync-all", params={"project_scope_id": scope_b})
    assert resp.status_code == 200, resp.text
    data = resp.json()

    # Same writes on disk (paths are root-relative on both sides).
    tree_a = _runtime_tree(project_a)
    tree_b = _runtime_tree(project_b)
    assert tree_a and tree_a == tree_b
    assert any(rel.startswith(".claude/skills/") for rel in tree_b)
    assert ".mcp.json" in tree_b
    assert ".claude/settings.json" in tree_b

    # Phase entries embed the native per-type bodies verbatim. The settings
    # rows carry the ABSOLUTE target path, which legitimately differs per
    # project — normalize both roots to a placeholder before comparing.
    # Walk the parsed structure rather than substring-replacing a
    # ``json.dumps`` rendering: on Windows the serialized text escapes the
    # path's backslashes (``C:\\Users``) while ``str(root)`` keeps single
    # ones, so a serialized-text replace silently no-ops and the targets
    # never normalize.
    def _rootless(value, root: Path):
        if isinstance(value, str):
            return value.replace(str(root), "<root>")
        if isinstance(value, list):
            return [_rootless(v, root) for v in value]
        if isinstance(value, dict):
            return {k: _rootless(v, root) for k, v in value.items()}
        return value

    for phase_type, native in per_type.items():
        phase = _phase(data, phase_type)
        assert phase["status"] == "ok", phase
        for key, value in native.items():
            assert _rootless(phase[key], project_b) == _rootless(value, project_a), (
                phase_type,
                key,
            )

    summary = data["summary"]
    assert summary["status"] == "ok"
    assert summary["ok"] == 5
    assert summary["generated_total"] == sum(
        len(per_type[t]["generated"]) for t in ("skills", "commands", "agents", "mcp-servers")
    )
    assert summary["generated_total"] > 0
    assert summary["changed"] is True
    assert summary["outcome"] == "changed"


@pytest.mark.asyncio
async def test_second_run_reports_in_sync_skips(client, cwd_root: Path) -> None:
    """Re-running over an unchanged tree surfaces the benign ``in_sync``
    code raw — the server never folds it into a verdict (#1262)."""
    _seed_artifacts(cwd_root)
    first = await client.post("/api/context/sync-all")
    assert first.status_code == 200, first.text
    assert first.json()["summary"]["status"] == "ok"

    second = await client.post("/api/context/sync-all")
    assert second.status_code == 200, second.text
    data = second.json()
    assert data["summary"]["status"] == "ok"
    # Only the mcp-servers engine emits in_sync skips (skills/commands/
    # agents regenerate idempotently); the code must arrive raw.
    mcp_skips = _phase(data, "mcp-servers")["skipped"]
    assert any(s["reason_code"] == "in_sync" for s in mcp_skips), mcp_skips


# ── Per-phase report on failure (the ADR-0024 decision) ─────────────────


@pytest.mark.asyncio
async def test_mixed_result_failed_phase_does_not_stop_later_phases(client, cwd_root: Path) -> None:
    """Acceptance criterion #3: a Gate A privacy block fails the agents
    phase with the error envelope; every other phase still runs, reports,
    and writes (unlike the front-end's stop-at-first-failure)."""
    _seed_artifacts(cwd_root)
    (cwd_root / ".memtomem" / "agents" / "leaky.md").write_bytes(_SECRET_AGENT_BODY)

    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    agents = _phase(data, "agents")
    assert agents["status"] == "failed"
    error = agents["error"]
    assert error["error_kind"] == "validation"
    assert error["reason_code"] == "privacy_blocked"
    assert error["http_status"] == 422
    assert "privacy" in error["message"].lower()
    # The blocked secret itself must never round-trip into the report.
    assert "AKIA1234567890ABCDEF" not in resp.text

    for phase_type in ("skills", "commands", "mcp-servers", "settings"):
        assert _phase(data, phase_type)["status"] == "ok", phase_type
    # Later phases really wrote — mcp-servers and settings run AFTER agents.
    assert (cwd_root / ".mcp.json").is_file()
    assert (cwd_root / ".claude" / "settings.json").is_file()

    assert data["summary"]["status"] == "partial"
    assert data["summary"]["failed"] == 1
    assert data["summary"]["ok"] == 4


@pytest.mark.asyncio
async def test_privacy_block_422_message_is_path_free(client, cwd_root: Path) -> None:
    """#1385 finding 1: the privacy-block 422 envelope must not echo the
    absolute canonical path. The engine's ``PrivacyScanError.message`` ends
    with ``… remove the secret from {blocked.path} …`` — a ``.resolve()``'d
    host path under ``$HOME`` (leaking the OS username over loopback). The
    sync cores must raise a fixed, path-free detail and keep the full message
    only on the chained exception (server-side traceback)."""
    _seed_artifacts(cwd_root)
    (cwd_root / ".memtomem" / "agents" / "leaky.md").write_bytes(_SECRET_AGENT_BODY)

    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    error = _phase(resp.json(), "agents")["error"]

    assert error["reason_code"] == "privacy_blocked"
    assert error["http_status"] == 422
    assert "privacy" in error["message"].lower()
    # The fix: the privacy-block detail carries no absolute canonical path
    # (in either symlink form). (A settings phase's write ``target`` is a
    # separate, intentional path surface and is out of scope here.)
    assert str(cwd_root) not in error["message"]
    assert str(cwd_root.resolve()) not in error["message"]
    assert "leaky.md" not in error["message"]


@pytest.mark.asyncio
async def test_settings_error_rolls_up_to_failed_phase(client, cwd_root: Path) -> None:
    """Settings failures are in-band result rows, not exceptions: the phase
    status rolls up to ``failed`` with the rows embedded (no ``error`` key),
    and the run-level status reads ``partial``."""
    (cwd_root / ".memtomem" / "settings.json").write_text("{not json", encoding="utf-8")

    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    data = resp.json()

    settings = _phase(data, "settings")
    assert settings["status"] == "failed"
    assert "error" not in settings
    assert any(r["status"] == "error" for r in settings["results"])
    assert data["summary"]["status"] == "partial"
    assert data["summary"]["failed"] == 1


# ── Route-level gates ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_paused_project_refused_409_before_any_phase(
    client, cwd_root: Path, tmp_path: Path
) -> None:
    other = _other_project(tmp_path, "paused")
    _seed_artifacts(other)
    scope_id = await _register(client, other, enabled=False)

    resp = await client.post("/api/context/sync-all", params={"project_scope_id": scope_id})
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["reason_code"] == "sync_paused"
    # Nothing ran: no runtime fan-out landed in the paused project.
    assert _runtime_tree(other) == {}


@pytest.mark.asyncio
async def test_project_local_tier_rejected_400(client) -> None:
    resp = await client.post("/api/context/sync-all", params={"target_scope": "project_local"})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "project_shared" in detail["message"]


@pytest.mark.asyncio
async def test_user_tier_rejected_400(client) -> None:
    resp = await client.post("/api/context/sync-all", params={"target_scope": "user"})
    assert resp.status_code == 400, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "project-tier" in detail["message"]


# ── Lock + timeout model ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_whole_run_holds_one_gateway_lock_acquisition(
    client, cwd_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #1278's core constraint: the five phases run under ONE
    ``_gateway_lock`` acquisition (the lock is non-reentrant — a per-phase
    re-acquire would deadlock, per-phase independent acquires would let a
    concurrent mutator interleave between phases)."""
    from memtomem.web.routes import _locks

    _seed_artifacts(cwd_root)
    acquisitions = 0
    real_aenter = _locks._LoopLocalLock.__aenter__

    async def counting_aenter(self):
        nonlocal acquisitions
        if self is _locks._gateway_lock:
            acquisitions += 1
        return await real_aenter(self)

    monkeypatch.setattr(_locks._LoopLocalLock, "__aenter__", counting_aenter)

    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 200, resp.text
    assert len(resp.json()["phases"]) == len(_SYNC_ALL_PHASES)
    assert acquisitions == 1


@pytest.mark.asyncio
async def test_outer_timeout_returns_503_busy_envelope(
    client, cwd_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.web.routes import context_skills, context_sync_all

    _seed_artifacts(cwd_root)
    monkeypatch.setattr(context_sync_all, "_SYNC_ALL_TIMEOUT_S", 0.05)

    def slow_generate(*args, **kwargs):
        time.sleep(0.5)
        raise AssertionError("unreachable — the outer timeout fires first")

    monkeypatch.setattr(context_skills, "generate_all_skills", slow_generate)

    resp = await client.post("/api/context/sync-all")
    assert resp.status_code == 503, resp.text
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "busy"
    assert "timed out" in detail["message"]


@pytest.mark.asyncio
async def test_concurrent_sync_all_serialised_by_lock(client, cwd_root: Path) -> None:
    """Two concurrent sync-all runs must not interleave phases: the second
    waits for the first's whole window (both succeed — serially)."""
    _seed_artifacts(cwd_root)
    r1, r2 = await asyncio.gather(
        client.post("/api/context/sync-all"),
        client.post("/api/context/sync-all"),
    )
    assert r1.status_code == 200, r1.text
    assert r2.status_code == 200, r2.text
    # One of the two ran second over an already-synced tree: its
    # mcp-servers phase reports in_sync (the engine that detects
    # byte-equality) rather than re-writing.
    skips = [
        {s["reason_code"] for s in _phase(r.json(), "mcp-servers")["skipped"]} for r in (r1, r2)
    ]
    assert any("in_sync" in codes for codes in skips), skips


# ── Unit pins for the shaping helpers ────────────────────────────────────


def test_settings_severity_ladder() -> None:
    """error/aborted outrank needs_confirmation, which outranks ok/skipped —
    the front-end ladder, including the (currently project_shared-unreachable,
    deliberately defensive) needs_confirmation branch."""
    assert _settings_severity([]) == "ok"
    assert _settings_severity([{"status": "ok"}, {"status": "skipped"}]) == "ok"
    assert (
        _settings_severity([{"status": "ok"}, {"status": "needs_confirmation"}])
        == "needs_confirmation"
    )
    assert _settings_severity([{"status": "needs_confirmation"}, {"status": "error"}]) == "failed"
    assert _settings_severity([{"status": "aborted"}]) == "failed"


def test_phase_error_envelope_string_and_dict_details() -> None:
    """String details become ``message``; dict details keep their extra keys
    (strict-drop's partial ``generated``) and their own ``reason_code`` wins
    over the exception attribute (``setdefault``)."""
    string_err = SyncPhaseError(
        422, "Gate A: blocked", error_kind="validation", reason_code="privacy_blocked"
    )
    assert _phase_error_envelope(string_err) == {
        "error_kind": "validation",
        "http_status": 422,
        "message": "Gate A: blocked",
        "reason_code": "privacy_blocked",
    }

    dict_err = SyncPhaseError(
        422,
        detail={
            "reason_code": "strict_drop",
            "message": "2 field(s) dropped",
            "generated": [{"runtime": "claude", "path": ".claude/agents/a.md"}],
        },
        error_kind="validation",
        reason_code="strict_drop",
    )
    envelope = _phase_error_envelope(dict_err)
    assert envelope["error_kind"] == "validation"
    assert envelope["http_status"] == 422
    assert envelope["message"] == "2 field(s) dropped"
    assert envelope["reason_code"] == "strict_drop"
    assert envelope["generated"] == [{"runtime": "claude", "path": ".claude/agents/a.md"}]
