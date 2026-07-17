"""Web API tests for the Quality Lab search-run inspection surface (#1801).

The router is dev-only and a thin translation layer over storage: the
app-level handlers map ``KeyError``→404 and ``ValueError``→400, and only
``FeedbackConflictError`` gets a bespoke 409 here. Storage is mocked —
the real validation contract is pinned in ``test_search_feedback.py``.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient
from unittest.mock import AsyncMock

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
    storage = AsyncMock()
    storage.get_search_runs = AsyncMock(return_value=[RUN_SUMMARY])
    storage.get_search_run = AsyncMock(return_value=RUN_DETAIL)
    storage.get_search_feedback = AsyncMock(
        return_value=[
            {
                "chunk_id": "c1",
                "judgment": "relevant",
                "created_at": "2026-07-17T00:00:00.000001+00:00",
                "updated_at": "2026-07-17T00:00:00.000002+00:00",
            }
        ]
    )
    storage.save_search_feedback = AsyncMock(return_value=FEEDBACK_ROW)
    application.state.storage = storage
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
        app.state.storage.get_search_runs.assert_awaited_once_with(limit=50, since=None)

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
            RUN_ID, "c1", "relevant", replace=False
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
