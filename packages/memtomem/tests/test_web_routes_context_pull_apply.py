"""ADR-0030 PR-D — web route ``POST /api/context/{kind}/{name}/pull``.

The web sibling of ``mm context pull --apply`` (PR-C) over the SAME
``context.pull_apply`` engine. The engine's own status paths (§5 refusal,
capture-once, Gate A, plan_stale) are pinned in ``test_context_pull_apply.py``;
here we pin the WEB wiring: the result-coded contract (domain decisions return
200 with a ``ContextPullApplyResponse`` the picker branches on), the four
HTTP-mapped statuses (503 / 409 / 500), destination consent (project_shared
confirm + user host-write gate), the literal-true force valve, request-shape
validation, and the redaction boundary (no absolute path / secret / raw bytes).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import Mem2MemConfig
from memtomem.context.pull_apply import PullApplyResult
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.web.app import create_app

from .helpers import seed_multi_runtime, set_home

# Runtime-assembled so the scannable token never appears verbatim in committed
# source (mirrors ``test_context_pull_apply._SECRET`` — push-protection safe).
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


def _skill_body(name: str, marker: str) -> str:
    return f"---\nname: {name}\n---\n{marker}\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, str(h))
    return h


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem").mkdir()
    return p


@pytest.fixture
def app(proj: Path, home: Path):
    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = proj
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


async def _pull(client, kind: str, name: str, *, scope: str = "project_shared", **body):
    return await client.post(
        f"/api/context/{kind}/{name}/pull",
        params={"target_scope": scope},
        json=body,
    )


# ── request-shape validation (400, like the preview route) ───────────────────


@pytest.mark.asyncio
async def test_bad_kind_400(client) -> None:
    resp = await _pull(client, "bogus", "x")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_kind"] == "validation"


@pytest.mark.asyncio
async def test_project_local_rejected_400(client) -> None:
    resp = await _pull(client, "skills", "demo", scope="project_local")
    assert resp.status_code == 400
    assert "project_local" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_target_scope_required_422(client) -> None:
    # No default: an implicit project_shared would silently write the git tier.
    resp = await client.post("/api/context/skills/demo/pull", json={})
    assert resp.status_code == 422  # FastAPI missing-required-query


@pytest.mark.asyncio
async def test_bad_name_400(client) -> None:
    resp = await _pull(client, "skills", "bad!name")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_kind"] == "validation"


@pytest.mark.asyncio
async def test_export_only_source_runtime_400(client, proj: Path) -> None:
    # codex is export-only for agents — an ineligible --from is a request-shape
    # error, rejected at the boundary (parity with the CLI's up-front guard),
    # never a bare ValueError into a 500.
    resp = await _pull(client, "agents", "demo", source_runtime="codex")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_kind"] == "validation"


# ── result-coded domain decisions all return 200 ─────────────────────────────


@pytest.mark.asyncio
async def test_applied_created(client, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "demo", {"claude": _skill_body("demo", "fresh")})
    resp = await _pull(client, "skills", "demo", confirm_project_shared=True)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["write_outcome"] == "created"
    assert body["selected_runtime"] == "claude"
    assert body["reason_code"] is None
    # canonical_path is project-relative / ~-collapsed, never the abs proj path.
    assert body["canonical_path"] is not None
    assert str(proj) not in resp.text


@pytest.mark.asyncio
async def test_source_conflict_200_with_candidates(client, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "skills",
        "demo",
        {"claude": _skill_body("demo", "stale"), "codex": _skill_body("demo", "fresh")},
    )
    # A refusal happens in prepare (before any consent gate) — no confirm needed.
    resp = await _pull(client, "skills", "demo")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "source_conflict"
    assert body["reason_code"] == "source_conflict"
    assert body["distinct_landing_count"] == 2
    assert {c["runtime"] for c in body["candidates"]} >= {"claude", "codex"}
    # A refusal wrote nothing.
    assert not (canonical_artifact_dir("skills", "project_shared", proj) / "demo").exists()


@pytest.mark.asyncio
async def test_source_runtime_pick_resolves_conflict(client, proj: Path) -> None:
    seed_multi_runtime(
        proj,
        "skills",
        "demo",
        {"claude": _skill_body("demo", "stale"), "codex": _skill_body("demo", "fresh")},
    )
    resp = await _pull(
        client, "skills", "demo", source_runtime="codex", confirm_project_shared=True
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "applied"
    assert body["selected_runtime"] == "codex"


@pytest.mark.asyncio
async def test_nothing_importable(client, proj: Path) -> None:
    resp = await _pull(client, "skills", "absent")
    assert resp.status_code == 200, resp.text
    assert resp.json()["status"] == "nothing_importable"


@pytest.mark.asyncio
async def test_identical_noop(client, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "demo", {"claude": _skill_body("demo", "fresh")})
    first = await _pull(client, "skills", "demo", confirm_project_shared=True)
    assert first.json()["status"] == "applied"
    # Pull the same content again → byte-identical no-op (returned in prepare, so
    # no consent gate; still status applied).
    again = await _pull(client, "skills", "demo")
    assert again.status_code == 200
    body = again.json()
    assert body["status"] == "applied"
    assert body["write_outcome"] == "identical"


# ── destination consent gates (Blocker fix) ──────────────────────────────────


@pytest.mark.asyncio
async def test_project_shared_needs_confirmation(client, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "demo", {"claude": _skill_body("demo", "fresh")})
    resp = await _pull(client, "skills", "demo")  # no confirm_project_shared
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "needs_confirmation"
    assert body["confirm"] == "confirm_project_shared"
    # Nothing was written — consent is required first.
    assert not (canonical_artifact_dir("skills", "project_shared", proj) / "demo").exists()


@pytest.mark.asyncio
async def test_user_tier_needs_host_write_confirmation(client, proj: Path) -> None:
    seed_multi_runtime(
        proj, "skills", "demo", {"claude": _skill_body("demo", "fresh")}, scope="user"
    )
    resp = await _pull(client, "skills", "demo", scope="user")  # no allow_host_writes
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "needs_confirmation"
    assert body["confirm"] == "allow_host_writes"
    assert body["host_targets"]  # discloses the host path(s) the write lands on
    assert not (canonical_artifact_dir("skills", "user", proj) / "demo").exists()
    # Confirmed → the write lands.
    ok = await _pull(client, "skills", "demo", scope="user", allow_host_writes=True)
    ok_body = ok.json()
    assert ok_body["status"] == "applied"
    # A user-tier dst is a resolved host path — canonical_path must never carry
    # either the raw or resolved $HOME spelling (the canonical-path-leak rule).
    home = Path.home()
    assert str(home) not in ok.text
    assert str(home.resolve()) not in ok.text
    assert ok_body["canonical_path"] is not None


# ── Gate A on the wire + literal-true valve ──────────────────────────────────


@pytest.mark.asyncio
async def test_gate_blocked_user_tier_bypassable(client, proj: Path) -> None:
    seed_multi_runtime(
        proj, "skills", "demo", {"claude": _skill_body("demo", f"key={_SECRET}")}, scope="user"
    )
    blocked = await _pull(client, "skills", "demo", scope="user", allow_host_writes=True)
    assert blocked.status_code == 200, blocked.text
    body = blocked.json()
    assert body["status"] == "gate_blocked"
    assert body["reason_code"] is not None
    assert body["force_bypassable"] is True
    # The block message names neither the secret nor a path.
    assert _SECRET not in blocked.text
    # Reviewed → literal force bypass applies on the bypassable tier.
    forced = await _pull(
        client,
        "skills",
        "demo",
        scope="user",
        allow_host_writes=True,
        force_unsafe_import=True,
    )
    assert forced.json()["status"] == "applied"


@pytest.mark.asyncio
async def test_force_unsafe_import_literal_true_only(client, proj: Path) -> None:
    """A coercible ``"true"`` string must NOT enable the Gate A bypass — only a
    JSON literal ``true`` (the web force-unsafe transport contract)."""
    seed_multi_runtime(
        proj, "skills", "demo", {"claude": _skill_body("demo", f"key={_SECRET}")}, scope="user"
    )
    coerced = await _pull(
        client,
        "skills",
        "demo",
        scope="user",
        allow_host_writes=True,
        force_unsafe_import="true",  # string, not the JSON literal true
    )
    assert coerced.status_code == 200, coerced.text
    assert coerced.json()["status"] == "gate_blocked"  # string did not bypass


@pytest.mark.asyncio
async def test_gate_blocked_project_shared_hard(client, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "demo", {"claude": _skill_body("demo", f"key={_SECRET}")})
    resp = await _pull(
        client, "skills", "demo", confirm_project_shared=True, force_unsafe_import=True
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "gate_blocked"
    # project_shared has no force bypass (ADR-0011 §5), even with the literal.
    assert body["force_bypassable"] is False
    assert _SECRET not in resp.text


# ── the four HTTP-mapped statuses ────────────────────────────────────────────


def _commit_returning(status: str):
    def _commit(plan, *, lock_timeout=None):
        return PullApplyResult(
            status=status,
            kind="skills",
            name="demo",
            scope="project_shared",
            reason=f"{status} at /abs/secret/path",
            reason_code=status,
        )

    return _commit


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "code", "error_kind"),
    [
        ("lock_timeout", 503, "busy"),
        ("plan_stale", 409, "conflict"),
        # 409, not 500: nothing failed infrastructurally and nothing was
        # written — the artifact is wedged in a state only an operator can
        # adjudicate, which is a conflict about the resource (ADR-0030 §10).
        ("swap_recovery_pending", 409, "conflict"),
        ("snapshot_failed", 500, "internal"),
        ("write_failed", 500, "internal"),
    ],
)
async def test_commit_status_http_mapping(
    client, proj: Path, monkeypatch, status: str, code: int, error_kind: str
) -> None:
    seed_multi_runtime(proj, "skills", "demo", {"claude": _skill_body("demo", "fresh")})
    monkeypatch.setattr(
        "memtomem.web.routes.context_gateway.commit_pull", _commit_returning(status)
    )
    resp = await _pull(client, "skills", "demo", confirm_project_shared=True)
    assert resp.status_code == code, resp.text
    assert resp.json()["detail"]["error_kind"] == error_kind
    # An OSError-shaped reason never leaks its path on the error envelope.
    assert "/abs/secret/path" not in resp.text


# ── redaction / parity contracts ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_no_absolute_path_in_response(client, proj: Path) -> None:
    seed_multi_runtime(proj, "skills", "demo", {"claude": _skill_body("demo", "clean")})
    resp = await _pull(client, "skills", "demo", confirm_project_shared=True)
    assert resp.status_code == 200
    assert str(proj) not in resp.text
    assert str(Path.home()) not in resp.text


def test_status_literal_parity_with_engine() -> None:
    """The 200-body ``status`` Literal is exactly the engine's PullApplyStatus
    minus the five HTTP-mapped statuses (503 lock_timeout, 409 plan_stale, 409
    swap_recovery_pending, 500 snapshot_failed / write_failed). Drift in either
    direction fails loudly (the ``content_status`` parity rule applied to the
    apply enum)."""
    from typing import get_args

    from memtomem.context.pull_apply import PullApplyStatus
    from memtomem.web.schemas.context import ContextPullApplyResponse

    engine = set(get_args(PullApplyStatus))
    wire = set(get_args(ContextPullApplyResponse.model_fields["status"].annotation))
    assert wire == engine - {
        "lock_timeout",
        "plan_stale",
        "snapshot_failed",
        "write_failed",
        "swap_recovery_pending",
    }
