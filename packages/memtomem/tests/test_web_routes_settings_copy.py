"""HTTP-layer tests for ``POST /api/context/settings/hooks/copy`` (#1281, A-11).

Engine semantics (dual-write durability, conflicts, locking, Gate A
internals) are pinned by ``test_settings_copy.py``; this file covers what
the WEB surface adds:

- destination resolution through project discovery (404 unknown, 409
  ``sync_paused`` for every tier — the canonical leg is a project write,
  409 ``no_memtomem_store``);
- the sequential disclose-then-confirm round-trips
  (``confirm_project_shared`` then user-tier ``allow_host_writes`` with
  ``host_targets``), keyed on PENDING writes so no-op re-POSTs never
  prompt;
- Gate A before consent — the issue-pinned 422 STRING detail, for
  private destination tiers too;
- the object error envelope and the CLI-parity status vocabulary
  (``plan`` / ``needs_confirmation`` / ``noop`` / ``ok`` / ``conflicts``).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context.settings import CANONICAL_SETTINGS_FILE
from memtomem.web.app import create_app

from .helpers import set_home

SECRET = "api_key=AKIA1234567890ABCDEF"


def _inner(command: str = "mm session start") -> dict:
    return {"type": "command", "command": command, "timeout": 5000}


def _seed_canonical(root: Path, command: str = "mm session start") -> None:
    path = root / CANONICAL_SETTINGS_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [_inner(command)]}]}},
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


# ── Fixtures (the test_web_routes_context_transfer harness shape) ────


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def cwd_root(tmp_path: Path, home: Path) -> Path:
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    (cwd / ".claude").mkdir()
    (cwd / ".memtomem").mkdir()
    _seed_canonical(cwd)
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


def _other_project(tmp_path: Path, name: str = "proj-b", *, store: bool = True) -> Path:
    other = tmp_path / name
    other.mkdir()
    (other / ".claude").mkdir()
    if store:
        (other / ".memtomem").mkdir()
    return other


def _body(scope_id: str, **overrides) -> dict:
    body = {
        "event": "PostToolUse",
        "matcher": "Edit|Write",
        "to_project_scope_id": scope_id,
        "to_target_scope": "project_local",
    }
    body.update(overrides)
    return body


COPY_URL = "/api/context/settings/hooks/copy"


# ── destination resolution ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_unknown_destination_404_envelope(client) -> None:
    resp = await client.post(COPY_URL, json=_body("p-000000000000"))
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "unknown project_scope_id" in detail["message"]


@pytest.mark.asyncio
async def test_paused_destination_409_for_private_tier_too(client, tmp_path) -> None:
    """The canonical leg writes the destination project for EVERY tier —
    a paused destination refuses even when the tier is project_local."""
    other = _other_project(tmp_path)
    scope_id = await _register(client, other, enabled=False)
    resp = await client.post(COPY_URL, json=_body(scope_id))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "conflict"
    assert detail["reason_code"] == "sync_paused"
    assert not (other / CANONICAL_SETTINGS_FILE).exists()


@pytest.mark.asyncio
async def test_destination_without_store_409(client, tmp_path) -> None:
    other = _other_project(tmp_path, store=False)
    scope_id = await _register(client, other)
    resp = await client.post(COPY_URL, json=_body(scope_id))
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["reason_code"] == "no_memtomem_store"
    assert "mm context init" in detail["message"]


# ── selector errors ──────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_unmatched_selector_404_lists_labels(client, tmp_path) -> None:
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)
    resp = await client.post(COPY_URL, json=_body(scope_id, event="SessionStart", matcher=""))
    assert resp.status_code == 404
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "missing"
    assert "available: PostToolUse:Edit|Write" in detail["message"]


@pytest.mark.asyncio
async def test_ambiguous_selector_400(client, tmp_path, cwd_root) -> None:
    (cwd_root / CANONICAL_SETTINGS_FILE).write_text(
        json.dumps(
            {
                "hooks": {
                    "PostToolUse": [
                        {
                            "matcher": "Edit|Write",
                            "hooks": [_inner("mm session start"), _inner("mm idx")],
                        }
                    ]
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)
    resp = await client.post(COPY_URL, json=_body(scope_id))
    assert resp.status_code == 400
    detail = resp.json()["detail"]
    assert detail["error_kind"] == "validation"
    assert "--hook-command" in detail["message"] or "hook_command" in detail["message"]


@pytest.mark.asyncio
async def test_same_project_destination_400(client, cwd_root) -> None:
    scope_id = await _register(client, cwd_root)
    resp = await client.post(COPY_URL, json=_body(scope_id))
    assert resp.status_code == 400
    assert "settings-migrate" in resp.json()["detail"]["message"]


# ── dry-run / confirm round-trips / apply ────────────────────────────


@pytest.mark.asyncio
async def test_dry_run_returns_plan_writes_nothing(client, tmp_path) -> None:
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)
    resp = await client.post(f"{COPY_URL}?dry_run=true", json=_body(scope_id))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "plan"
    assert payload["command_preview"] == "mm session start"
    assert payload["canonical"] == {"state": "missing", "reason": ""}
    assert payload["target"] == {"state": "missing", "reason": ""}
    assert payload["dst_project_scope_id"] == scope_id
    assert not (other / CANONICAL_SETTINGS_FILE).exists()


@pytest.mark.asyncio
async def test_confirm_round_trip_then_apply(client, tmp_path) -> None:
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)

    first = await client.post(COPY_URL, json=_body(scope_id))
    assert first.status_code == 200, first.text
    envelope = first.json()
    assert envelope["status"] == "needs_confirmation"
    assert envelope["confirm"] == "confirm_project_shared"
    assert envelope["plan"]["dst_scope"] == "project_local"
    assert not (other / CANONICAL_SETTINGS_FILE).exists()

    second = await client.post(COPY_URL, json=_body(scope_id, confirm_project_shared=True))
    assert second.status_code == 200, second.text
    payload = second.json()
    assert payload["status"] == "ok"
    assert payload["canonical"]["written"] is True
    assert payload["target"]["written"] is True
    assert payload["sync_command"].endswith("--scope project_local")

    canonical = json.loads((other / CANONICAL_SETTINGS_FILE).read_text(encoding="utf-8"))
    assert canonical["hooks"]["PostToolUse"][0]["hooks"][0]["command"] == "mm session start"
    tier = json.loads((other / ".claude" / "settings.local.json").read_text(encoding="utf-8"))
    assert tier["hooks"]["PostToolUse"][0]["hooks"][0]["statusMessage"].startswith("memtomem · ")


@pytest.mark.asyncio
async def test_user_tier_stacks_both_gates(client, tmp_path, home) -> None:
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)
    body = _body(scope_id, to_target_scope="user")

    first = await client.post(COPY_URL, json=body)
    assert first.json()["confirm"] == "confirm_project_shared"

    second = await client.post(COPY_URL, json={**body, "confirm_project_shared": True})
    envelope = second.json()
    assert envelope["status"] == "needs_confirmation"
    assert envelope["confirm"] == "allow_host_writes"
    assert envelope["host_targets"] == [str(home / ".claude" / "settings.json")]
    assert not (home / ".claude" / "settings.json").exists()

    third = await client.post(
        COPY_URL,
        json={**body, "confirm_project_shared": True, "allow_host_writes": True},
    )
    assert third.status_code == 200, third.text
    assert third.json()["status"] == "ok"
    assert (home / ".claude" / "settings.json").is_file()
    # The canonical leg still lands at the destination PROJECT.
    assert (other / CANONICAL_SETTINGS_FILE).is_file()


@pytest.mark.asyncio
async def test_noop_repost_never_prompts(client, tmp_path) -> None:
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)
    applied = await client.post(COPY_URL, json=_body(scope_id, confirm_project_shared=True))
    assert applied.json()["status"] == "ok"

    # Re-POST without ANY confirm flag: nothing pending → no envelope.
    rerun = await client.post(COPY_URL, json=_body(scope_id))
    assert rerun.status_code == 200, rerun.text
    payload = rerun.json()
    assert payload["status"] == "noop"
    assert payload["canonical"]["already"] is True
    assert payload["target"]["already"] is True


@pytest.mark.asyncio
async def test_canonical_conflict_reports_conflicts_no_prompt(client, tmp_path) -> None:
    other = _other_project(tmp_path)
    canonical = other / CANONICAL_SETTINGS_FILE
    canonical.parent.mkdir(parents=True, exist_ok=True)
    canonical.write_text(
        json.dumps(
            {"hooks": {"PostToolUse": [{"matcher": "Edit|Write", "hooks": [_inner("rival")]}]}}
        )
        + "\n",
        encoding="utf-8",
    )
    before = canonical.read_bytes()
    scope_id = await _register(client, other)

    resp = await client.post(COPY_URL, json=_body(scope_id))
    assert resp.status_code == 200, resp.text
    payload = resp.json()
    assert payload["status"] == "conflicts"
    assert any("'rival'" in w for w in payload["warnings"])
    assert canonical.read_bytes() == before


# ── Gate A ───────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_gate_a_422_string_before_consent(client, tmp_path, cwd_root) -> None:
    """Scan-first ordering: a doomed copy 422s instead of completing a
    needs_confirmation round-trip — and the detail is the issue-pinned
    STRING, even for a private destination tier."""
    _seed_canonical(cwd_root, command=f"echo {SECRET}")
    other = _other_project(tmp_path)
    scope_id = await _register(client, other)

    resp = await client.post(COPY_URL, json=_body(scope_id))  # no confirm flags
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert isinstance(detail, str)
    assert "git history is forever" in detail
    assert SECRET not in detail
    # #1385 finding-1 sibling (Codex rec A): the 422 detail must not echo the
    # absolute canonical settings path. Pre-fix it ended with "… remove the
    # secret from {blocked.path} …" under cwd_root (the source canonical).
    assert str(cwd_root) not in detail
    assert str(cwd_root.resolve()) not in detail
    assert str(other) not in detail
    assert not (other / CANONICAL_SETTINGS_FILE).exists()
