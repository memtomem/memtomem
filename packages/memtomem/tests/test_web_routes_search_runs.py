"""Web API tests for the Quality Lab search-run inspection surface (#1801).

The router is dev-only and a thin translation layer over storage: the
app-level handlers map ``KeyError``→404 and ``ValueError``→400, and only
``FeedbackConflictError`` gets a bespoke 409 here. Storage is mocked —
the real validation contract is pinned in ``test_search_feedback.py``.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import DEFAULT, AsyncMock

from memtomem.errors import FeedbackConflictError
from memtomem.web.app import create_app

RUN_ID = "11111111-1111-4111-8111-111111111111"

RUN_SUMMARY = {
    "run_id": RUN_ID,
    "query_text": "quality query",
    "created_at": "2026-07-17T00:00:00+00:00",
    "result_count": 2,
    "origin": "web",
    "feedback_count": 1,
}

RUN_DETAIL = {
    "run_id": RUN_ID,
    "query_text": "quality query",
    "created_at": "2026-07-17T00:00:00+00:00",
    "observation": {"origin": "web", "top_k": 5, "cache_hit": False},
    "result_snapshot": [
        {
            "chunk_id": "c1",
            "rank": 1,
            "score": 0.9,
            "source_name": "note.md",
            "content_hash": "abc",
            "heading_hierarchy": ["Overview"],
            "namespace": "default",
            "language": "en",
        },
        {"chunk_id": "c2", "rank": 2, "score": 0.5, "source_name": "note.md"},
    ],
}

FEEDBACK_ROW = {
    "run_id": RUN_ID,
    "chunk_id": "c1",
    "judgment": "relevant",
    "created_at": "2026-07-17T00:00:00.000001+00:00",
    "updated_at": "2026-07-17T00:00:00.000001+00:00",
    "created": True,
    "replaced": False,
}


@pytest.fixture
def app():
    application = create_app(lifespan=None, mode="dev")
    # Every handler settles in-flight observation writes before reading, so
    # a run this process just answered is never missing (#2183). The order is
    # recorded here because a flush *after* the read would look identical in
    # a mock-based test while 404-ing in production.
    call_order: list[str] = []

    def _record(name: str):
        def side_effect(*args, **kwargs):
            call_order.append(name)
            return DEFAULT

        return side_effect

    storage = AsyncMock()
    storage.get_search_runs = AsyncMock(return_value=[RUN_SUMMARY], side_effect=_record("storage"))
    storage.get_search_run = AsyncMock(return_value=RUN_DETAIL, side_effect=_record("storage"))
    storage.get_search_feedback = AsyncMock(
        return_value=[
            {
                "chunk_id": "c1",
                "judgment": "relevant",
                "created_at": "2026-07-17T00:00:00.000001+00:00",
                "updated_at": "2026-07-17T00:00:00.000002+00:00",
            }
        ],
        side_effect=_record("storage"),
    )
    storage.save_search_feedback = AsyncMock(
        return_value=FEEDBACK_ROW, side_effect=_record("storage")
    )
    pipeline = AsyncMock()
    pipeline.flush_observation = AsyncMock(side_effect=_record("flush"))
    application.state.storage = storage
    application.state.search_pipeline = pipeline
    application.state.config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[]))
    application.state.project_root = Path("/project-a")
    application.state.call_order = call_order
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestListRuns:
    async def test_list_returns_summaries(self, app, client):
        resp = await client.get("/api/search/runs")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["runs"][0]["run_id"] == RUN_ID
        assert data["runs"][0]["feedback_count"] == 1
        app.state.storage.get_search_runs.assert_awaited_once_with(
            limit=50, since=None, project_context_root=None
        )

    async def test_boundary_tracks_live_project_registration(
        self, app, client, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        project_root = tmp_path / "project"
        project_root.mkdir()
        monkeypatch.chdir(project_root)
        app.state.config.indexing.project_memory_dirs = [project_root / ".memtomem" / "memories"]

        first = await client.get("/api/search/runs")
        assert first.status_code == 200
        assert (
            app.state.storage.get_search_runs.await_args.kwargs["project_context_root"]
            == project_root.resolve()
        )

        app.state.config.indexing.project_memory_dirs = []
        second = await client.get("/api/search/runs")
        assert second.status_code == 200
        assert app.state.storage.get_search_runs.await_args.kwargs["project_context_root"] is None

    @pytest.mark.parametrize("bad_limit", [0, 201, -5])
    async def test_limit_bounds_rejected(self, client, bad_limit):
        resp = await client.get(f"/api/search/runs?limit={bad_limit}")
        assert resp.status_code == 422

    async def test_bad_since_maps_to_400(self, app, client):
        app.state.storage.get_search_runs.side_effect = ValueError(
            "since must be an ISO-8601 timestamp, got 'yesterday'"
        )
        resp = await client.get("/api/search/runs?since=yesterday")
        assert resp.status_code == 400


class TestRunDetail:
    async def test_detail_merges_judgments(self, client):
        resp = await client.get(f"/api/search/runs/{RUN_ID}")
        assert resp.status_code == 200
        data = resp.json()
        assert data["query_text"] == "quality query"
        assert data["observation"]["top_k"] == 5
        judged, unjudged = data["results"]
        assert judged["chunk_id"] == "c1" and judged["judgment"] == "relevant"
        assert judged["feedback_updated_at"] == "2026-07-17T00:00:00.000002+00:00"
        assert unjudged["chunk_id"] == "c2" and unjudged["judgment"] is None

    async def test_snapshot_out_is_an_allowlist(self, app, client):
        # SnapshotEntryOut is a deliberate allowlist, not a passthrough
        # (#1812): a key the snapshot writer grows later — including a writer
        # regression that leaked raw content or an absolute path — must be
        # dropped, never auto-surfaced. This response is the privacy boundary.
        app.state.storage.get_search_run.return_value = {
            **RUN_DETAIL,
            "result_snapshot": [
                {
                    **RUN_DETAIL["result_snapshot"][0],
                    "novelty_score": 0.42,  # benign future field
                    "content": "raw secret text",  # writer-regression leak
                    "source_path": "/Users/someone/private/note.md",  # absolute path
                }
            ],
        }
        resp = await client.get(f"/api/search/runs/{RUN_ID}")
        assert resp.status_code == 200
        entry = resp.json()["results"][0]
        assert "novelty_score" not in entry
        assert "content" not in entry
        assert "source_path" not in entry
        # The declared safe fields still render.
        assert entry["source_name"] == "note.md" and entry["content_hash"] == "abc"

    async def test_unknown_run_maps_to_404(self, app, client):
        app.state.storage.get_search_run.side_effect = KeyError("run_id 'x' not found")
        resp = await client.get("/api/search/runs/x")
        assert resp.status_code == 404


class TestPostFeedback:
    async def test_created(self, app, client):
        resp = await client.post(
            f"/api/search/runs/{RUN_ID}/feedback",
            json={"chunk_id": "c1", "judgment": "relevant"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["created"] is True and data["replaced"] is False
        app.state.storage.save_search_feedback.assert_awaited_once_with(
            RUN_ID,
            "c1",
            "relevant",
            replace=False,
            project_context_root=None,
        )

    async def test_idempotent_resubmit(self, app, client):
        app.state.storage.save_search_feedback.return_value = {
            **FEEDBACK_ROW,
            "created": False,
        }
        resp = await client.post(
            f"/api/search/runs/{RUN_ID}/feedback",
            json={"chunk_id": "c1", "judgment": "relevant"},
        )
        assert resp.status_code == 200
        assert resp.json()["created"] is False

    async def test_conflict_maps_to_409(self, app, client):
        app.state.storage.save_search_feedback.side_effect = FeedbackConflictError(
            "feedback for run 'r' chunk 'c1' is already 'relevant'; pass replace=true to overwrite"
        )
        resp = await client.post(
            f"/api/search/runs/{RUN_ID}/feedback",
            json={"chunk_id": "c1", "judgment": "not_relevant"},
        )
        assert resp.status_code == 409
        assert "replace=true" in resp.json()["detail"]

    async def test_unknown_run_maps_to_404(self, app, client):
        app.state.storage.save_search_feedback.side_effect = KeyError("run_id 'x' not found")
        resp = await client.post(
            "/api/search/runs/x/feedback",
            json={"chunk_id": "c1", "judgment": "relevant"},
        )
        assert resp.status_code == 404

    async def test_bad_judgment_maps_to_400(self, app, client):
        app.state.storage.save_search_feedback.side_effect = ValueError(
            "judgment must be one of ['not_relevant', 'relevant'], got 'maybe'"
        )
        resp = await client.post(
            f"/api/search/runs/{RUN_ID}/feedback",
            json={"chunk_id": "c1", "judgment": "maybe"},
        )
        assert resp.status_code == 400


class TestDevOnlyPin:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/search/runs"),
            ("GET", f"/api/search/runs/{RUN_ID}"),
            ("POST", f"/api/search/runs/{RUN_ID}/feedback"),
        ],
    )
    async def test_prod_mode_hides_all_routes(self, method, path):
        prod_app = create_app(lifespan=None, mode="prod")
        transport = ASGITransport(app=prod_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.request(method, path, json={"chunk_id": "c1", "judgment": "relevant"})
        assert resp.status_code == 404


class TestObservationFlush:
    """#2183: the search path returns its run ID before the row commits.

    Each handler must settle the pending write *before* it reads, or a run
    the same process just answered 404s from its own detail endpoint.
    """

    async def test_list_flushes_all_pending_before_reading(self, app, client):
        resp = await client.get("/api/search/runs")
        assert resp.status_code == 200
        app.state.search_pipeline.flush_observation.assert_awaited_once_with()
        assert app.state.call_order[0] == "flush"

    async def test_detail_flushes_its_run_before_reading(self, app, client):
        resp = await client.get(f"/api/search/runs/{RUN_ID}")
        assert resp.status_code == 200
        app.state.search_pipeline.flush_observation.assert_awaited_once_with(RUN_ID)
        assert app.state.call_order[0] == "flush"

    async def test_feedback_flushes_its_run_before_writing(self, app, client):
        resp = await client.post(
            f"/api/search/runs/{RUN_ID}/feedback",
            json={"chunk_id": "c1", "judgment": "relevant"},
        )
        assert resp.status_code == 200
        app.state.search_pipeline.flush_observation.assert_awaited_once_with(RUN_ID)
        assert app.state.call_order == ["flush", "storage"]
