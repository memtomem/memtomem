"""Web API tests for the Quality Lab eval-case + replay surface (#1802, PR-5).

The router is dev-only and a thin translation layer over storage + the replay
engine. Error mapping is router-local by exception *type*:
``EvalCaseNotFoundError``→404, ``EvalCaseValidationError``→422, every other
``EvalCaseError``→409. Storage and the replay engine are mocked — the real
contracts are pinned in ``test_eval_cases.py`` / ``test_quality_replay.py``.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.errors import EvalCaseError, EvalCaseNotFoundError, EvalCaseValidationError
from memtomem.web.app import create_app

RUN_ID = "11111111-1111-4111-8111-111111111111"

CASE_ROW = {
    "case_id": "cccccccc-1111-4111-8111-111111111111",
    "name": "baseline",
    "query_text": "quality query",
    "top_k": 5,
    "source_run_id": RUN_ID,
    "version": 1,
    "status": "active",
    "created_at": "2026-07-18T00:00:00+00:00",
    "updated_at": "2026-07-18T00:00:00+00:00",
    "label_count": 3,
}

PROMOTED_CASE = {
    "case_id": CASE_ROW["case_id"],
    "name": f"run-{RUN_ID}",
    "labels": [{"content_hash": "a"}, {"content_hash": "b"}],
}

REPORT = {
    "schema_version": 1,
    "kind": "replay_report",
    "as_of_unix": 1_784_500_000,
    "deterministic": True,
    "nondeterministic_stages": [],
    "counts": {"replayed": 1, "archived_skipped": 0, "degraded": 0, "excluded_from_aggregate": 0},
    "aggregate": {"mean_hit_rate": 1.0, "mrr": 1.0, "evaluated_cases": 1},
    "cases": [{"case_id": CASE_ROW["case_id"], "metrics": {}}],
}


@pytest.fixture
def app(monkeypatch):
    application = create_app(lifespan=None, mode="dev")
    storage = AsyncMock()
    storage.list_eval_cases = AsyncMock(return_value=[CASE_ROW])
    storage.promote_search_run = AsyncMock(return_value=PROMOTED_CASE)
    application.state.storage = storage
    application.state.search_pipeline = AsyncMock()
    application.state.config = object()

    monkeypatch.setattr(
        "memtomem.web.routes.quality.current_fingerprints",
        lambda s, c: ({"profile": "p", "corpus": "c", "index": "i"}, {}),
    )
    monkeypatch.setattr(
        "memtomem.web.routes.quality.replay_cases",
        AsyncMock(return_value=REPORT),
    )
    return application


@pytest.fixture
async def client(app):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


class TestListCases:
    async def test_list_returns_summaries(self, app, client):
        resp = await client.get("/api/quality/cases")
        assert resp.status_code == 200
        data = resp.json()
        assert data["total"] == 1
        assert data["cases"][0]["case_id"] == CASE_ROW["case_id"]
        assert data["cases"][0]["label_count"] == 3
        app.state.storage.list_eval_cases.assert_awaited_once_with(status=None)

    async def test_status_filter_forwarded(self, app, client):
        resp = await client.get("/api/quality/cases?status=archived")
        assert resp.status_code == 200
        app.state.storage.list_eval_cases.assert_awaited_once_with(status="archived")

    async def test_bad_status_rejected(self, client):
        resp = await client.get("/api/quality/cases?status=bogus")
        assert resp.status_code == 422

    async def test_summary_is_an_allowlist(self, app, client):
        app.state.storage.list_eval_cases.return_value = [
            {**CASE_ROW, "filters_json": "{}", "promotion_snapshot_json": "[]"}
        ]
        resp = await client.get("/api/quality/cases")
        assert resp.status_code == 200
        entry = resp.json()["cases"][0]
        assert "filters_json" not in entry
        assert "promotion_snapshot_json" not in entry


class TestPromote:
    async def test_default_name_uses_full_run_id(self, app, client):
        resp = await client.post("/api/quality/cases", json={"run_id": RUN_ID})
        assert resp.status_code == 200
        data = resp.json()
        assert data["case_id"] == CASE_ROW["case_id"]
        assert data["label_count"] == 2
        kwargs = app.state.storage.promote_search_run.await_args.kwargs
        assert kwargs["name"] == f"run-{RUN_ID}"  # full id, never a prefix
        assert kwargs["allow_unreplayable_filters"] is False
        assert kwargs["fingerprints"] == {"profile": "p", "corpus": "c", "index": "i"}

    async def test_explicit_name_passed_through(self, app, client):
        resp = await client.post("/api/quality/cases", json={"run_id": RUN_ID, "name": "my-case"})
        assert resp.status_code == 200
        assert app.state.storage.promote_search_run.await_args.kwargs["name"] == "my-case"

    async def test_blank_name_rejected(self, client):
        resp = await client.post("/api/quality/cases", json={"run_id": RUN_ID, "name": "   "})
        assert resp.status_code == 422

    async def test_two_runs_sharing_prefix_both_promotable(self, app, client):
        # Two UUIDs whose first 8 chars match — a prefix-based default name
        # would false-collide; the full-id default must not.
        run_a = "abcdef00-1111-4111-8111-111111111111"
        run_b = "abcdef00-2222-4222-8222-222222222222"
        await client.post("/api/quality/cases", json={"run_id": run_a})
        await client.post("/api/quality/cases", json={"run_id": run_b})
        names = [c.kwargs["name"] for c in app.state.storage.promote_search_run.await_args_list]
        assert names == [f"run-{run_a}", f"run-{run_b}"]
        assert names[0] != names[1]

    async def test_not_found_maps_to_404(self, app, client):
        app.state.storage.promote_search_run.side_effect = EvalCaseNotFoundError(
            f"run_id {RUN_ID!r} not found"
        )
        resp = await client.post("/api/quality/cases", json={"run_id": RUN_ID})
        assert resp.status_code == 404

    async def test_no_feedback_maps_to_409(self, app, client):
        app.state.storage.promote_search_run.side_effect = EvalCaseError(
            f"run {RUN_ID!r} has no feedback to promote"
        )
        resp = await client.post("/api/quality/cases", json={"run_id": RUN_ID})
        assert resp.status_code == 409

    async def test_malformed_name_maps_to_422(self, app, client):
        # A validation failure (bad shape / secret-shaped) is a 422, not a 409
        # state conflict.
        app.state.storage.promote_search_run.side_effect = EvalCaseValidationError(
            "eval case name contains a secret-shaped token and was refused"
        )
        resp = await client.post("/api/quality/cases", json={"run_id": RUN_ID, "name": "some-name"})
        assert resp.status_code == 422

    async def test_collision_message_with_not_found_still_409(self, app, client):
        # Adversarial: a name-collision message that contains the substring
        # "not found" must NOT be misclassified as 404 — classification is by
        # type, never by message text.
        app.state.storage.promote_search_run.side_effect = EvalCaseError(
            "eval case name 'baseline not found' already exists"
        )
        resp = await client.post(
            "/api/quality/cases", json={"run_id": RUN_ID, "name": "baseline not found"}
        )
        assert resp.status_code == 409


class TestReplay:
    async def test_replay_returns_report(self, app, client):
        resp = await client.post("/api/quality/replay", json={})
        assert resp.status_code == 200
        assert resp.json() == REPORT

    @pytest.mark.parametrize("body", [{}, {"cases": []}])
    async def test_empty_selection_forwards_none(self, app, client, body):
        from memtomem.web.routes import quality as qmod

        resp = await client.post("/api/quality/replay", json=body)
        assert resp.status_code == 200
        assert qmod.replay_cases.await_args.kwargs["case_ids"] is None

    async def test_params_forwarded_and_stripped(self, app, client):
        from memtomem.web.routes import quality as qmod

        resp = await client.post(
            "/api/quality/replay", json={"cases": [" baseline "], "as_of_unix": 123}
        )
        assert resp.status_code == 200
        assert qmod.replay_cases.await_args.kwargs["case_ids"] == ["baseline"]
        assert qmod.replay_cases.await_args.kwargs["as_of_unix"] == 123

    async def test_blank_selector_rejected(self, client):
        resp = await client.post("/api/quality/replay", json={"cases": ["ok", "  "]})
        assert resp.status_code == 422

    async def test_negative_as_of_rejected(self, client):
        resp = await client.post("/api/quality/replay", json={"as_of_unix": -1})
        assert resp.status_code == 422

    async def test_out_of_range_as_of_rejected(self, client):
        from memtomem.quality.replay import MAX_AS_OF_UNIX

        resp = await client.post("/api/quality/replay", json={"as_of_unix": MAX_AS_OF_UNIX + 1})
        assert resp.status_code == 422

    async def test_bool_as_of_rejected(self, client):
        resp = await client.post("/api/quality/replay", json={"as_of_unix": True})
        assert resp.status_code == 422

    async def test_not_found_maps_to_404(self, app, client):
        from memtomem.web.routes import quality as qmod

        qmod.replay_cases.side_effect = EvalCaseNotFoundError("eval case 'x' not found")
        resp = await client.post("/api/quality/replay", json={"cases": ["x"]})
        assert resp.status_code == 404


class TestDevOnlyPin:
    @pytest.mark.parametrize(
        "method,path",
        [
            ("GET", "/api/quality/cases"),
            ("POST", "/api/quality/cases"),
            ("POST", "/api/quality/replay"),
        ],
    )
    async def test_prod_mode_hides_all_routes(self, method, path):
        prod_app = create_app(lifespan=None, mode="prod")
        transport = ASGITransport(app=prod_app)
        async with AsyncClient(transport=transport, base_url="http://test") as c:
            resp = await c.request(method, path, json={"run_id": RUN_ID})
        assert resp.status_code == 404
