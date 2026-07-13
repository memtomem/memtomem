"""Review-first candidate extraction and temporal assertion storage."""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

import pytest

from memtomem.formation import scan_session_candidates


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
