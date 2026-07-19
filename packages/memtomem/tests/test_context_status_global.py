"""ADR-0030 PR-F — user-tier global portal status + pull-drift probe.

Two surfaces are pinned here:

* the engine :func:`memtomem.context.pull_preview.probe_pull_drift` — a
  read-only, whole-Store pull-direction drift sweep that reuses the preview's
  ``_collect`` pass with ``scan_gate=False`` (the badge needs only
  ``content_status``, never the expensive per-file Gate A scan);
* the web route ``GET /api/context/status-global`` — a SEPARATE, parameterless
  sibling of the ``project_shared``-only ``/context/status-all`` fleet endpoint
  (ADR-0030 §9), with the drift ``reason`` redacted at the wire boundary.
"""

from __future__ import annotations

import typing
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock

from memtomem.config import Mem2MemConfig
from memtomem.context import pull_preview
from memtomem.context.pull_preview import PullDriftRow, PullDriftSummary, probe_pull_drift
from memtomem.context.scope_resolver import canonical_artifact_dir
from memtomem.web.app import create_app
from memtomem.web.routes import context_gateway
from memtomem.web.schemas.context import ContextPullDriftRow

from .helpers import seed_multi_runtime, set_home


def _skill_body(name: str, marker: str) -> str:
    return f"---\nname: {name}\n---\n{marker}\n"


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, str(h))
    return h


def _seed_store_skill(name: str, marker: str) -> Path:
    """Write a user-tier canonical Store skill (``~/.memtomem/skills/<name>/SKILL.md``)."""
    d = canonical_artifact_dir("skills", "user", None) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(_skill_body(name, marker), encoding="utf-8")
    return d


def _row(summary: PullDriftSummary, name: str) -> PullDriftRow:
    return next(r for r in summary.rows if r.name == name)


# ── engine: probe_pull_drift ─────────────────────────────────────────────


def test_probe_empty_store_no_drift(home: Path) -> None:
    summary = probe_pull_drift(scope="user", project_root=None)
    assert summary.total == 0
    assert summary.differs == 0
    assert summary.has_pull_drift is False
    assert summary.rows == ()


def test_probe_differs_when_runtime_copy_diverges(home: Path) -> None:
    """Stale runtime copy vs a different Store copy — the founding failure shape."""
    _seed_store_skill("s", "store v1")
    seed_multi_runtime(
        home, "skills", "s", {"claude": _skill_body("s", "runtime v2")}, scope="user"
    )

    summary = probe_pull_drift(scope="user", project_root=None)

    assert summary.total == 1
    assert summary.differs == 1
    assert summary.has_pull_drift is True
    row = _row(summary, "s")
    assert row.kind == "skills"
    assert row.verdict == "differs"
    assert "claude" in row.runtimes
    assert row.reason is None


def test_probe_identical_is_not_drift(home: Path) -> None:
    body = _skill_body("s", "same bytes")
    _seed_store_skill("s", "same bytes")
    seed_multi_runtime(home, "skills", "s", {"claude": body}, scope="user")

    summary = probe_pull_drift(scope="user", project_root=None)

    assert summary.total == 1
    assert summary.identical == 1
    assert summary.has_pull_drift is False
    assert _row(summary, "s").verdict == "identical"


def test_probe_runs_no_gate_scan(home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The whole point of the probe: content_status only, NEVER the Gate A
    privacy scan. ``scan_gate=False`` must keep ``classify_gate_status`` off the
    hot path over the whole Store."""
    _seed_store_skill("s", "store v1")
    seed_multi_runtime(
        home, "skills", "s", {"claude": _skill_body("s", "runtime v2")}, scope="user"
    )

    def _boom(*_a: object, **_k: object) -> object:
        raise AssertionError("Gate A scan ran during a drift probe (scan_gate must be False)")

    monkeypatch.setattr(pull_preview, "classify_gate_status", _boom)

    summary = probe_pull_drift(scope="user", project_root=None)
    assert _row(summary, "s").verdict == "differs"  # ran to completion, no gate scan


def test_probe_single_bad_artifact_becomes_error_row(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One unreadable artifact must not blank the whole portal — it is caught
    and reported as an ``error`` row, not raised."""
    _seed_store_skill("s", "v1")

    def _raise(*_a: object, **_k: object) -> object:
        raise OSError("boom while collecting")

    monkeypatch.setattr(pull_preview, "_collect", _raise)

    summary = probe_pull_drift(scope="user", project_root=None)
    assert summary.errors == 1
    assert summary.has_pull_drift is False  # an error is indeterminate, not drift
    assert _row(summary, "s").verdict == "error"


def test_probe_unreadable_store_no_runtime_is_error_not_identical(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable Store with NO runtime copy present carries the error on
    ``_Collected`` alone (no candidate row) — it must be ``error``, never fall
    through to ``identical`` (Codex F1)."""
    _seed_store_skill("s", "v1")  # listed, but its read will be forced to fail

    def _store_err(*_a: object, **_k: object) -> tuple[bool, None, OSError]:
        return True, None, OSError("store unreadable")

    monkeypatch.setattr(pull_preview, "_read_store", _store_err)

    summary = probe_pull_drift(scope="user", project_root=None)
    assert summary.errors == 1
    row = _row(summary, "s")
    assert row.verdict == "error"
    assert row.reason == "store unreadable"


def test_probe_landing_error_when_runtime_copy_unreadable(
    home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A present runtime copy whose would-land bytes can't be computed
    (``landing_error``) is an ``error`` row — the runtime-side sibling of the
    store-side error path, pinned directly."""
    _seed_store_skill("s", "store v1")
    seed_multi_runtime(home, "skills", "s", {"claude": _skill_body("s", "v2")}, scope="user")

    def _boom(*_a: object, **_k: object) -> object:
        raise OSError("unreadable runtime copy")

    monkeypatch.setattr(pull_preview, "_read_landing", _boom)

    summary = probe_pull_drift(scope="user", project_root=None)
    assert summary.errors == 1
    row = _row(summary, "s")
    assert row.verdict == "error"
    assert "unreadable runtime copy" in (row.reason or "")


def test_probe_spans_all_pull_kinds(home: Path) -> None:
    _seed_store_skill("sk", "v1")
    seed_multi_runtime(home, "skills", "sk", {"claude": _skill_body("sk", "v2")}, scope="user")
    # An agent present only in the Store (no runtime copy) is not drift.
    agents_dir = canonical_artifact_dir("agents", "user", None)
    agents_dir.mkdir(parents=True, exist_ok=True)
    (agents_dir / "ag.md").write_text("agent body\n", encoding="utf-8")

    summary = probe_pull_drift(scope="user", project_root=None)
    kinds = {r.kind for r in summary.rows}
    assert kinds == {"skills", "agents"}
    assert summary.differs == 1  # only the skill diverged


# ── route: GET /api/context/status-global ────────────────────────────────


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """A project root DISTINCT from HOME so a stray project-tier read is visible
    (aliasing project_root to HOME would let a project_shared lookup pass)."""
    p = tmp_path / "proj"
    (p / ".git").mkdir(parents=True)
    (p / ".memtomem").mkdir()
    return p


@pytest.fixture
def client(home: Path, proj: Path):
    app = create_app(lifespan=None, mode="dev")
    app.state.project_root = proj  # the user-tier endpoint must ignore this
    app.state.storage = AsyncMock()
    app.state.config = Mem2MemConfig()
    app.state.search_pipeline = None
    app.state.index_engine = None
    app.state.embedder = None
    app.state.dedup_scanner = None
    transport = ASGITransport(app=app)
    return AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.asyncio
async def test_route_reports_user_scope_and_drift(home: Path, client) -> None:
    _seed_store_skill("s", "store v1")
    seed_multi_runtime(
        home, "skills", "s", {"claude": _skill_body("s", "runtime v2")}, scope="user"
    )

    async with client as c:
        resp = await c.get("/api/context/status-global")

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "user"
    assert body["store"]["skills"] == 1
    assert isinstance(body["runtime_coverage"], list)
    drift = body["pull_drift"]
    assert drift["has_pull_drift"] is True
    assert drift["differs"] == 1
    row = next(r for r in drift["rows"] if r["name"] == "s")
    assert row["verdict"] == "differs"
    assert "claude" in row["runtimes"]


@pytest.mark.asyncio
async def test_route_is_user_tier_only(home: Path, proj: Path, client) -> None:
    """User-only by construction. A ``project_shared`` skill seeded under a
    DISTINCT project root (``app.state.project_root``) must never surface — the
    endpoint takes no ``target_scope`` query and probes ``scope="user",
    project_root=None``, so the git tier is unreachable here."""
    # git-tier artifact under the distinct project root...
    proj_skill = canonical_artifact_dir("skills", "project_shared", proj) / "gitonly"
    proj_skill.mkdir(parents=True, exist_ok=True)
    (proj_skill / "SKILL.md").write_text(_skill_body("gitonly", "v1"), encoding="utf-8")
    # ...and one genuine user-tier artifact.
    _seed_store_skill("useronly", "v1")

    async with client as c:
        resp = await c.get("/api/context/status-global", params={"target_scope": "project_shared"})

    assert resp.status_code == 200
    body = resp.json()
    assert body["scope"] == "user"
    assert body["store"]["skills"] == 1  # only the user artifact
    names = {r["name"] for r in body["pull_drift"]["rows"]}
    assert names == {"useronly"}
    assert "gitonly" not in names


@pytest.mark.asyncio
async def test_route_redacts_error_reason(
    home: Path, client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ``error`` row's raw reason may embed an absolute path; the wire must
    not leak it (``_redact_pull_reason`` backstop)."""
    leaky = f"{home}/.memtomem/skills/secret/SKILL.md: permission denied"
    fake = PullDriftSummary(
        scope="user",
        rows=(
            PullDriftRow(kind="skills", name="secret", verdict="error", runtimes=(), reason=leaky),
        ),
        differs=0,
        errors=1,
        identical=0,
        total=1,
    )
    monkeypatch.setattr(context_gateway, "probe_pull_drift", lambda **_k: fake)

    async with client as c:
        resp = await c.get("/api/context/status-global")

    assert resp.status_code == 200
    row = resp.json()["pull_drift"]["rows"][0]
    assert str(home) not in (row["reason"] or "")
    assert ".memtomem/skills/secret" not in (row["reason"] or "")


# ── wire parity: engine enum ⇄ schema Literal ────────────────────────────


def test_verdict_literal_matches_engine() -> None:
    """The schema ``verdict`` Literal must equal the engine's ``PullDriftVerdict``
    (same discipline as ``ContextPullPreviewCandidate.content_status``)."""
    schema_tokens = set(typing.get_args(ContextPullDriftRow.model_fields["verdict"].annotation))
    engine_tokens = set(typing.get_args(pull_preview.PullDriftVerdict))
    assert schema_tokens == engine_tokens == {"differs", "identical", "error"}
