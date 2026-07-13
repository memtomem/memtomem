"""Review-first candidate extraction and temporal assertion storage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memtomem.formation import propose_memory_candidate, scan_session_candidates


@pytest.mark.asyncio
async def test_scan_uses_exact_session_events_and_is_idempotent(storage):
    await storage.create_session("target", "agent", "default")
    await storage.create_session("other", "agent", "default")
    await storage.add_session_event(
        "target", "note", "Decision: use blue-green deployment", [str(uuid4())]
    )
    await storage.add_session_event("other", "note", "Decision: unrelated database choice")
    first = await scan_session_candidates(storage, "target")
    second = await scan_session_candidates(storage, "target")
    assert len(first) == 1
    assert second == []
    assert first[0]["content"] == "Decision: use blue-green deployment"
    assert first[0]["evidence"][0]["event_id"] > 0


@pytest.mark.asyncio
async def test_scan_skips_secret_and_routes_procedure_to_pinned(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Procedure: deploy then verify health")
    await storage.add_session_event("session", "note", "Decision: api_key=sk-secret-value")
    candidates = await scan_session_candidates(storage, "session")
    assert len(candidates) == 1
    assert candidates[0]["kind"] == "procedure"
    assert candidates[0]["destination"] == "pinned"


@pytest.mark.asyncio
async def test_scan_does_not_promote_generic_declarative_sentences(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "The service is available")
    await storage.add_session_event("session", "note", "서비스입니다")
    assert await scan_session_candidates(storage, "session") == []


@pytest.mark.asyncio
async def test_external_candidate_proposal_is_pending_and_idempotent(storage):
    first, first_duplicate = await propose_memory_candidate(
        storage,
        "Decision: use blue-green deployment",
        source="memtomem-stm",
        source_ref="docs/read_file/trace-1",
        idempotency_key="stable-key",
    )
    second, second_duplicate = await propose_memory_candidate(
        storage,
        "Decision: use blue-green deployment",
        source="memtomem-stm",
        source_ref="docs/read_file/trace-1",
        idempotency_key="stable-key",
    )
    assert first_duplicate is False
    assert second_duplicate is True
    assert second["id"] == first["id"]
    assert (await storage.get_memory_candidate(first["id"]))["status"] == "pending"


@pytest.mark.asyncio
async def test_external_candidate_proposal_rejects_sensitive_content(storage):
    with pytest.raises(ValueError, match="sensitive"):
        await propose_memory_candidate(
            storage,
            "Decision: api_key=sk-secret-value",
            source="memtomem-stm",
            source_ref="trace",
            idempotency_key="key",
        )


@pytest.mark.asyncio
async def test_candidate_state_machine(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Preference: concise responses")
    candidate = (await scan_session_candidates(storage, "session"))[0]
    assert len(await storage.list_memory_candidates()) == 1
    assert await storage.decide_memory_candidate(candidate["id"], "approved", "alice")
    assert not await storage.decide_memory_candidate(candidate["id"], "rejected", "bob")
    approved = await storage.get_memory_candidate(candidate["id"])
    assert approved["status"] == "approved"
    assert approved["reviewer"] == "alice"


@pytest.mark.asyncio
async def test_candidate_claim_is_atomic_and_releasable(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Decision: retain one copy")
    candidate = (await scan_session_candidates(storage, "session"))[0]

    claimed = await storage.claim_memory_candidate(candidate["id"], "alice")
    assert claimed is not None
    assert claimed["status"] == "writing"
    assert await storage.claim_memory_candidate(candidate["id"], "bob") is None
    assert await storage.release_memory_candidate(candidate["id"])
    assert await storage.claim_memory_candidate(candidate["id"], "bob") is not None
    assert await storage.finalize_memory_candidate(candidate["id"])
    assert (await storage.get_memory_candidate(candidate["id"]))["status"] == "approved"


@pytest.mark.asyncio
async def test_stale_claim_recovery_skips_fresh_claim_and_records_transition(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Decision: stale candidate")
    await storage.add_session_event("session", "note", "Preference: fresh candidate")
    stale, fresh = await scan_session_candidates(storage, "session")
    assert await storage.claim_memory_candidate(stale["id"], "alice") is not None
    assert await storage.claim_memory_candidate(fresh["id"], "bob") is not None

    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    cutoff = (datetime.now(timezone.utc) - timedelta(minutes=15)).isoformat(timespec="seconds")
    storage._get_db().execute(
        "UPDATE memory_candidates SET claim_started_at=? WHERE id=?",
        (old, stale["id"]),
    )
    storage._get_db().commit()

    recovered = await storage.recover_stale_memory_candidates(
        stale_before=cutoff, actor="operator-alice"
    )
    assert recovered == [stale["id"]]
    assert (await storage.get_memory_candidate(stale["id"]))["status"] == "pending"
    assert (await storage.get_memory_candidate(fresh["id"]))["status"] == "writing"
    transitions = await storage.list_memory_candidate_transitions(stale["id"])
    assert transitions[-1]["from_status"] == "writing"
    assert transitions[-1]["to_status"] == "pending"
    assert transitions[-1]["actor"] == "operator-alice"
    assert "stale approval claim recovered" in transitions[-1]["reason"]


@pytest.mark.asyncio
async def test_recovery_and_finalize_are_mutually_exclusive(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Decision: recover or finalize")
    candidate = (await scan_session_candidates(storage, "session"))[0]
    assert await storage.claim_memory_candidate(candidate["id"], "alice") is not None
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    storage._get_db().execute(
        "UPDATE memory_candidates SET claim_started_at=? WHERE id=?",
        (old, candidate["id"]),
    )
    storage._get_db().commit()

    recovered = await storage.recover_stale_memory_candidates(
        stale_before=datetime.now(timezone.utc).isoformat(timespec="seconds")
    )
    assert recovered == [candidate["id"]]
    assert not await storage.finalize_memory_candidate(candidate["id"])


@pytest.mark.asyncio
async def test_recovered_completed_write_is_quarantined_from_reapproval(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Decision: write completed")
    candidate = (await scan_session_candidates(storage, "session"))[0]
    assert await storage.claim_memory_candidate(candidate["id"], "alice") is not None
    old = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat(timespec="seconds")
    storage._get_db().execute(
        "UPDATE memory_candidates SET claim_started_at=? WHERE id=?",
        (old, candidate["id"]),
    )
    storage._get_db().commit()
    assert await storage.recover_stale_memory_candidates(
        stale_before=datetime.now(timezone.utc).isoformat(timespec="seconds")
    ) == [candidate["id"]]

    # Simulate the original writer returning after its durable write landed.
    assert await storage.mark_memory_candidate_write_uncertain(
        candidate["id"], actor="test-finalizer", reason="write already persisted"
    )
    assert await storage.claim_memory_candidate(candidate["id"], "bob") is None
    row = await storage.get_memory_candidate(candidate["id"])
    assert row["status"] == "write_uncertain"
    assert "already persisted" in row["decision_reason"]


@pytest.mark.asyncio
async def test_recovery_limit_returns_oldest_claims_first(storage):
    await storage.create_session("session", "agent", "default")
    for content in (
        "Decision: oldest claim",
        "Decision: middle claim",
        "Decision: newest claim",
    ):
        await storage.add_session_event("session", "note", content)
    candidates = await scan_session_candidates(storage, "session")
    for candidate in candidates:
        assert await storage.claim_memory_candidate(candidate["id"], "alice") is not None
    base = datetime.now(timezone.utc) - timedelta(minutes=40)
    for offset, candidate in enumerate(candidates):
        claimed_at = (base + timedelta(minutes=offset * 5)).isoformat(timespec="seconds")
        storage._get_db().execute(
            "UPDATE memory_candidates SET claim_started_at=? WHERE id=?",
            (claimed_at, candidate["id"]),
        )
    storage._get_db().commit()
    recovered = await storage.recover_stale_memory_candidates(
        stale_before=datetime.now(timezone.utc).isoformat(timespec="seconds"), limit=2
    )
    assert recovered == [candidates[0]["id"], candidates[1]["id"]]
    assert (await storage.get_memory_candidate(candidates[2]["id"]))["status"] == "writing"


@pytest.mark.asyncio
async def test_recovery_requires_timezone_and_operator_identity(storage):
    with pytest.raises(ValueError, match="timezone"):
        await storage.recover_stale_memory_candidates(
            stale_before="2026-07-13T00:00:00", actor="alice"
        )
    with pytest.raises(ValueError, match="actor"):
        await storage.recover_stale_memory_candidates(
            stale_before="2026-07-13T00:00:00+00:00", actor=""
        )


def test_review_recovery_cli_and_mcp_action_are_public():
    from click.testing import CliRunner

    from memtomem.cli.review_cmd import review
    from memtomem.server.tool_registry import ACTIONS

    result = CliRunner().invoke(review, ["recover", "--help"])
    assert result.exit_code == 0
    assert "--stale-after-minutes" in result.output
    assert "candidate_recover" in ACTIONS


@pytest.mark.asyncio
async def test_mcp_recovery_validation_returns_structured_errors():
    from memtomem.server.tools.formation import mem_candidate_recover

    invalid_age = json.loads(await mem_candidate_recover(stale_after_minutes=0))
    invalid_limit = json.loads(await mem_candidate_recover(limit=0))
    invalid_actor = json.loads(await mem_candidate_recover(actor=""))
    assert invalid_age == {
        "ok": False,
        "reason": "stale_after_minutes must be between 1 and 1440",
    }
    assert invalid_limit == {"ok": False, "reason": "limit must be between 1 and 1000"}
    assert invalid_actor == {"ok": False, "reason": "actor cannot be empty"}


@pytest.mark.asyncio
async def test_mcp_review_reports_persisted_write_after_concurrent_recovery(monkeypatch):
    from memtomem.server.tools.formation import mem_candidate_review

    storage = SimpleNamespace(
        get_memory_candidate=AsyncMock(
            return_value={
                "id": "candidate-1",
                "status": "pending",
                "destination": "memory",
                "content": "Decision: persisted once",
                "kind": "decision",
            }
        ),
        claim_memory_candidate=AsyncMock(return_value={"status": "writing"}),
        release_memory_candidate=AsyncMock(),
        finalize_memory_candidate=AsyncMock(return_value=False),
        mark_memory_candidate_write_uncertain=AsyncMock(return_value=True),
    )
    app = SimpleNamespace(storage=storage, ensure_initialized=AsyncMock())
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))
    monkeypatch.setattr(
        "memtomem.server.tools.memory_crud._mem_add_core",
        AsyncMock(return_value=("saved", SimpleNamespace(new_chunk_ids=[]))),
    )

    result = json.loads(
        await mem_candidate_review("candidate-1", "approve", reviewer="alice", ctx=ctx)
    )
    assert result["ok"] is False
    assert result["status"] == "write_uncertain"
    assert result["durable_write_persisted"] is True
    assert "do not re-approve" in result["reason"]
    storage.mark_memory_candidate_write_uncertain.assert_awaited_once()


@pytest.mark.asyncio
async def test_get_candidate_is_not_limited_by_queue_size(storage):
    await storage.create_session("session", "agent", "default")
    await storage.add_session_event("session", "note", "Decision: direct lookup")
    candidate = (await scan_session_candidates(storage, "session"))[0]
    found = await storage.get_memory_candidate(candidate["id"])
    assert found is not None
    assert found["id"] == candidate["id"]


@pytest.mark.asyncio
async def test_temporal_assertion_current_as_of_and_supersede(storage):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entity_id = str(uuid4())
    old_id = str(uuid4())
    new_id = str(uuid4())
    await storage.add_assertion(
        assertion_id=old_id,
        entity_id=entity_id,
        canonical_name="deployment strategy",
        entity_type="concept",
        predicate="uses",
        object_value="rolling",
        source_chunk_id=None,
        recorded_at="2026-01-01T00:00:00+00:00",
        valid_from="2026-01-01T00:00:00+00:00",
        valid_to="2026-06-01T00:00:00+00:00",
    )
    await storage.add_assertion(
        assertion_id=new_id,
        entity_id=entity_id,
        canonical_name="deployment strategy",
        entity_type="concept",
        predicate="uses",
        object_value="blue-green",
        source_chunk_id=None,
        recorded_at=now,
        valid_from="2026-06-01T00:00:00+00:00",
    )
    await storage.link_assertions(new_id, old_id, "supersedes")
    current = await storage.query_assertions("deployment strategy", "uses")
    assert [row["object"] for row in current] == ["blue-green"]
    historical = await storage.query_assertions(
        "deployment strategy", "uses", as_of="2026-03-01T00:00:00+00:00"
    )
    # Superseded assertions stay hidden from current-oriented queries even
    # when their historical validity overlaps; a dedicated history API can
    # expose them later without leaking them into current answers.
    assert historical == []


@pytest.mark.asyncio
async def test_assertion_edges_are_directional_multi_type(storage):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    entity = str(uuid4())
    first, second = str(uuid4()), str(uuid4())
    for assertion_id, value in ((first, "a"), (second, "b")):
        await storage.add_assertion(
            assertion_id=assertion_id,
            entity_id=entity,
            canonical_name="subject",
            entity_type="concept",
            predicate="state",
            object_value=value,
            source_chunk_id=None,
            recorded_at=now,
        )
    await storage.link_assertions(first, second, "contradicts")
    await storage.link_assertions(first, second, "supports")
    db = storage._get_db()
    rows = db.execute(
        "SELECT edge_type FROM assertion_edges WHERE source_assertion_id=? AND target_assertion_id=?",
        (first, second),
    ).fetchall()
    assert {row[0] for row in rows} == {"contradicts", "supports"}


@pytest.mark.asyncio
async def test_assertion_reuses_existing_entity_id(storage):
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    first_entity, ignored_entity = str(uuid4()), str(uuid4())
    first_assertion, second_assertion = str(uuid4()), str(uuid4())
    for assertion_id, entity_id, value in (
        (first_assertion, first_entity, "one"),
        (second_assertion, ignored_entity, "two"),
    ):
        await storage.add_assertion(
            assertion_id=assertion_id,
            entity_id=entity_id,
            canonical_name="same entity",
            entity_type="concept",
            predicate="value",
            object_value=value,
            source_chunk_id=None,
            recorded_at=now,
        )
    rows = await storage.query_assertions("same entity", "value")
    assert {row["object"] for row in rows} == {"one", "two"}
