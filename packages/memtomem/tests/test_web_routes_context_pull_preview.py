"""ADR-0030 PR-B — web route ``GET /api/context/{kind}/{name}/pull-preview``.

Validation (bad kind / bad name / project_local), the happy-path envelope, and
the redaction contract (no absolute canonical path reaches the wire). The wire
SHAPE is pinned separately in ``test_web_wire_fixtures.py``; here we assert
route behavior and the two-axis field values end-to-end.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.config import Mem2MemConfig
from memtomem.web.app import create_app

from .helpers import set_home


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    h = tmp_path / "home"
    h.mkdir()
    set_home(monkeypatch, str(h))
    return h


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    p = tmp_path / "proj"
    (p / ".claude").mkdir(parents=True)
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


def _seed_runtime_skill(proj: Path, runtime_dir: str, name: str, marker: str) -> None:
    d = proj / runtime_dir / "skills" / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_bytes(f"---\nname: {name}\n---\n{marker}\n".encode())


@pytest.mark.asyncio
async def test_happy_path_two_candidates(client, proj: Path) -> None:
    _seed_runtime_skill(proj, ".claude", "demo", "stale")
    _seed_runtime_skill(proj, ".agents", "demo", "fresh")
    resp = await client.get("/api/context/skills/demo/pull-preview")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["kind"] == "skills"
    assert body["name"] == "demo"
    assert body["store_present"] is False
    runtimes = {c["runtime"]: c for c in body["candidates"]}
    assert runtimes["claude"]["content_status"] == "new"
    assert runtimes["codex"]["content_status"] == "new"  # .agents/skills = codex
    assert runtimes["claude"]["gate_status"] == "ok"  # clean content, no secret
    assert body["ambiguous"] is True  # two distinct landing groups
    assert body["auto_source"] is None


@pytest.mark.asyncio
async def test_bad_kind_400(client) -> None:
    resp = await client.get("/api/context/bogus/x/pull-preview")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_kind"] == "validation"


@pytest.mark.asyncio
async def test_bad_name_400(client) -> None:
    # A char outside validate_name's [A-Za-z0-9._-] charset (dots alone are
    # allowed; only the exact "."/".." and path separators are structural).
    resp = await client.get("/api/context/skills/bad!name/pull-preview")
    assert resp.status_code == 400
    assert resp.json()["detail"]["error_kind"] == "validation"


@pytest.mark.asyncio
async def test_project_local_rejected_400(client) -> None:
    resp = await client.get(
        "/api/context/skills/demo/pull-preview", params={"target_scope": "project_local"}
    )
    assert resp.status_code == 400
    assert "project_local" in resp.json()["detail"]["message"]


@pytest.mark.asyncio
async def test_no_absolute_path_in_response(client, proj: Path) -> None:
    """The wire never carries an absolute canonical/runtime path (redaction)."""
    _seed_runtime_skill(proj, ".claude", "demo", "clean")
    resp = await client.get("/api/context/skills/demo/pull-preview")
    assert resp.status_code == 200
    assert str(proj) not in resp.text


def test_redact_pull_reason_strips_external_path(tmp_path: Path) -> None:
    """A runtime symlinked outside project_root/HOME can embed its resolved
    absolute path in an OSError reason; the backstop strips it (Codex Major)."""
    from memtomem.web.routes.context_gateway import _redact_pull_reason

    reason = "[Errno 13] Permission denied: '/Volumes/shared/rt/skills/demo/SKILL.md'"
    out = _redact_pull_reason(reason, tmp_path)
    assert out is not None
    assert "/Volumes/shared" not in out
    assert "<path>" in out


def test_redact_pull_reason_strips_path_with_spaces(tmp_path: Path) -> None:
    """A mount name with a space must be scrubbed whole, not up to the first
    space (PR review — the [\\w.-] class left `` Drive/...`` on the wire)."""
    from memtomem.web.routes.context_gateway import _redact_pull_reason

    reason = "[Errno 13] Permission denied: '/Volumes/My Drive/rt/skills/demo/SKILL.md'"
    out = _redact_pull_reason(reason, tmp_path)
    assert out is not None
    assert "Drive" not in out
    assert "skills" not in out
    assert "<path>" in out


def test_redact_pull_reason_keeps_path_free_diagnostic(tmp_path: Path) -> None:
    """A path-free TOML parse message survives redaction intact (useful signal)."""
    from memtomem.web.routes.context_gateway import _redact_pull_reason

    reason = "Expected '=' after a key in a key/value pair (at line 1, column 9)"
    assert _redact_pull_reason(reason, tmp_path) == reason
