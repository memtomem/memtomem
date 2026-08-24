"""Review-first candidate extraction and temporal assertion storage."""

from __future__ import annotations

import json
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from memtomem.errors import QueryEmbeddingDimensionError
from memtomem.formation import propose_memory_candidate, scan_session_candidates
from memtomem.server.tools import formation as formation_tools


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
async def test_external_candidate_reproposal_returns_actual_decided_status(storage):
    first, _ = await propose_memory_candidate(
        storage,
        "Preference: concise responses",
        source="memtomem-stm",
        source_ref="trace",
        idempotency_key="decided-key",
    )
    assert await storage.decide_memory_candidate(first["id"], "rejected", "reviewer")
    existing, duplicate = await propose_memory_candidate(
        storage,
        "Preference: concise responses",
        source="memtomem-stm",
        source_ref="trace",
        idempotency_key="decided-key",
    )
    assert duplicate is True
    assert existing["id"] == first["id"]
    assert existing["status"] == "rejected"


@pytest.mark.asyncio
async def test_external_candidate_reproposal_returns_expired_status(storage):
    first, _ = await propose_memory_candidate(
        storage,
        "Fact: service uses SQLite",
        source="memtomem-stm",
        source_ref="trace",
        idempotency_key="expired-key",
    )
    storage._get_db().execute(
        "UPDATE memory_candidates SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (first["id"],),
    )
    storage._get_db().commit()
    existing, duplicate = await propose_memory_candidate(
        storage,
        "Fact: service uses SQLite",
        source="memtomem-stm",
        source_ref="trace",
        idempotency_key="expired-key",
    )
    assert duplicate is True
    assert existing["status"] == "expired"


@pytest.mark.asyncio
async def test_external_candidate_rejects_idempotency_key_content_mismatch(storage):
    await propose_memory_candidate(
        storage,
        "Decision: use blue-green",
        source="memtomem-stm",
        source_ref="trace",
        idempotency_key="reused-key",
    )
    with pytest.raises(ValueError, match="different content"):
        await propose_memory_candidate(
            storage,
            "Decision: use canary",
            source="memtomem-stm",
            source_ref="trace",
            idempotency_key="reused-key",
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "source", "source_ref", "key", "match"),
    [
        (" ", "memtomem-stm", "", "key", "empty"),
        ("x" * 2001, "memtomem-stm", "", "key", "2000"),
        ("valid", "s" * 129, "", "key", "size limit"),
        ("valid", "memtomem-stm", "r" * 513, "key", "size limit"),
        ("valid", "memtomem-stm", "", "k" * 257, "size limit"),
        ("valid", " ", "", "key", "required"),
        ("valid", "memtomem-stm", "", " ", "required"),
        ("valid", "memtomem-stm", "api_key=sk-secret-value", "key", "sensitive"),
    ],
)
async def test_external_candidate_validation(
    storage, content: str, source: str, source_ref: str, key: str, match: str
):
    with pytest.raises(ValueError, match=match):
        await propose_memory_candidate(
            storage,
            content,
            source=source,
            source_ref=source_ref,
            idempotency_key=key,
        )


@pytest.mark.asyncio
async def test_candidate_propose_tool_returns_actual_response_shape(storage, monkeypatch):
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(return_value=SimpleNamespace(storage=storage)),
    )
    result = json.loads(
        await formation_tools.mem_candidate_propose(
            "Unclassified external note",
            "memtomem-stm",
            "trace",
            "tool-key",
        )
    )
    assert set(result) == {"ok", "candidate_id", "status", "created_at", "duplicate"}
    assert result["status"] == "pending"
    stored = await storage.get_memory_candidate(result["candidate_id"])
    assert stored is not None
    assert stored["confidence"] == 0.5


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

    assert await storage.resolve_uncertain_memory_candidate(
        candidate["id"],
        reviewer="operator-alice",
        reason="confirmed the durable entry already exists",
    )
    assert not await storage.resolve_uncertain_memory_candidate(
        candidate["id"], reviewer="operator-bob", reason="second attempt"
    )
    resolved = await storage.get_memory_candidate(candidate["id"])
    assert resolved["status"] == "rejected"
    transitions = await storage.list_memory_candidate_transitions(candidate["id"])
    assert transitions[-1]["from_status"] == "write_uncertain"
    assert transitions[-1]["to_status"] == "rejected"
    assert transitions[-1]["actor"] == "operator-alice"


@pytest.mark.asyncio
async def test_uncertain_resolution_requires_reviewer_and_reason(storage):
    with pytest.raises(ValueError, match="reviewer"):
        await storage.resolve_uncertain_memory_candidate(
            "candidate", reviewer="", reason="inspected"
        )
    with pytest.raises(ValueError, match="reason"):
        await storage.resolve_uncertain_memory_candidate("candidate", reviewer="alice", reason="")


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
async def test_mcp_resolves_write_uncertain_without_another_write():
    from memtomem.server.tools.formation import mem_candidate_review

    storage = SimpleNamespace(
        get_memory_candidate=AsyncMock(
            return_value={"id": "candidate-1", "status": "write_uncertain"}
        ),
        resolve_uncertain_memory_candidate=AsyncMock(return_value=True),
    )
    app = SimpleNamespace(storage=storage, ensure_initialized=AsyncMock())
    ctx = SimpleNamespace(request_context=SimpleNamespace(lifespan_context=app))

    missing_reason = json.loads(
        await mem_candidate_review("candidate-1", "reject", reviewer="alice", reason="", ctx=ctx)
    )
    assert missing_reason["ok"] is False
    assert "requires a reason" in missing_reason["reason"]
    resolved = json.loads(
        await mem_candidate_review(
            "candidate-1",
            "reject",
            reviewer="alice",
            reason="confirmed durable write",
            ctx=ctx,
        )
    )
    assert resolved == {
        "ok": True,
        "status": "rejected",
        "resolved_from": "write_uncertain",
    }
    storage.resolve_uncertain_memory_candidate.assert_awaited_once()


@pytest.mark.asyncio
async def test_cli_uncertain_resolution_requires_reason_and_reports_success(monkeypatch, capsys):
    import click

    from memtomem.cli.review_cmd import _decide

    storage = SimpleNamespace(
        get_memory_candidate=AsyncMock(
            return_value={"id": "candidate-1", "status": "write_uncertain"}
        ),
        resolve_uncertain_memory_candidate=AsyncMock(return_value=True),
    )

    @asynccontextmanager
    async def fake_components():
        yield SimpleNamespace(storage=storage)

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    with pytest.raises(click.ClickException, match="requires --reason"):
        await _decide("candidate-1", "rejected", "alice", "")
    await _decide("candidate-1", "rejected", "alice", "confirmed durable write")
    assert json.loads(capsys.readouterr().out)["resolved_from"] == "write_uncertain"
    storage.resolve_uncertain_memory_candidate.assert_awaited_once()


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


#: Deliberately under /private/tmp: on macOS the MCP formatter rewrites that
#: prefix and the CLI does not, so the two surfaces' path handling is
#: distinguishable. Expectations are computed from the formatters, never
#: written as literals — the rewrite is platform-specific.
_NEIGHBOUR_SOURCE = Path("/private/tmp/notes.md")


def _neighbour_result(content, score, *, valid_to_unix=None):
    from memtomem.models import Chunk, ChunkMetadata
    from memtomem.storage.base import SearchResult

    chunk = Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=_NEIGHBOUR_SOURCE, valid_to_unix=valid_to_unix),
        embedding=[0.5, 0.5],
    )
    return SearchResult(chunk=chunk, score=score, rank=1, source="dense")


class _EvidenceStorage:
    """Real candidate lookup is not needed here — only the dense half."""

    dense_enabled = True

    def __init__(self, results=None, error=None, coverage=None):
        self.results = results or []
        self.error = error
        self.coverage = coverage if coverage is not None else {"total": 4, "with_dense": 4}

    async def get_dense_coverage(self):
        return self.coverage

    async def dense_search(self, embedding, top_k=20, **kwargs):
        if self.error is not None:
            raise self.error
        return self.results


class _Embedder:
    async def embed_query(self, text):
        return [0.5, 0.5]


def _evidence_config():
    """Minimal config for the project-scope resolver the tool threads through."""
    return SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[]))


async def _seed_candidate(storage, text="Decision: use blue-green deployment"):
    from memtomem.formation import scan_session_candidates

    session_id = f"evidence-{uuid4()}"
    await storage.create_session(session_id, "agent", "default")
    await storage.add_session_event(session_id, "note", text)
    created = await scan_session_candidates(storage, session_id)
    return created[0]


@pytest.mark.asyncio
async def test_candidate_evidence_reports_neighbours_and_summary_counts(storage, monkeypatch):
    candidate = await _seed_candidate(storage)
    dense = _EvidenceStorage(
        [
            _neighbour_result("Rollout happens through canary traffic shifting", 0.9),
            _neighbour_result("Decision: use blue-green deployment", 0.85),
        ]
    )
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))

    assert result["ok"] is True
    assert result["status"] == "available"
    assert result["corpus"] == "indexed_chunks"
    assert result["summary"] == {
        "potential_conflict": 1,
        "restatement_candidate": 1,
        "related": 0,
    }
    labels = [n["label"] for n in result["neighbours"]]
    assert labels == ["potential_conflict", "restatement_candidate"]
    # Excerpts only — an embedding must never ride out on a review surface.
    assert all("embedding" not in n for n in result["neighbours"])
    # Paths go through the same display formatter mem_search results use.
    # Computed, not literal: _display_path rewrites /private/tmp only on macOS.
    from memtomem.server.formatters import _display_path

    assert result["neighbours"][0]["source_file"] == _display_path(_NEIGHBOUR_SOURCE)


@pytest.mark.asyncio
async def test_candidate_evidence_shows_superseded_neighbours_flagged_not_hidden(
    storage, monkeypatch
):
    candidate = await _seed_candidate(storage)
    expired = int(datetime.now(timezone.utc).timestamp()) - 3600
    dense = _EvidenceStorage(
        [_neighbour_result("Rollout happens through canary shifts", 0.9, valid_to_unix=expired)]
    )
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))

    assert len(result["neighbours"]) == 1
    assert result["neighbours"][0]["currently_valid"] is False


@pytest.mark.asyncio
async def test_candidate_evidence_never_mutates_the_candidate_row(storage, monkeypatch):
    candidate = await _seed_candidate(storage)
    before = await storage.get_memory_candidate(candidate["id"])
    dense = _EvidenceStorage([_neighbour_result("anything at all", 0.9)])
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))
    assert result["status"] == "available"
    assert await storage.get_memory_candidate(candidate["id"]) == before


@pytest.mark.asyncio
async def test_candidate_evidence_rejects_unknown_candidate_and_bad_top_k(storage, monkeypatch):
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    missing = json.loads(await formation_tools.mem_candidate_evidence("no-such-candidate"))
    assert missing == {"ok": False, "reason": "candidate not found"}

    bad = json.loads(await formation_tools.mem_candidate_evidence("no-such-candidate", top_k=21))
    assert bad == {"ok": False, "reason": "top_k must be between 1 and 20"}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "expected_status"),
    [
        (
            QueryEmbeddingDimensionError("Embedding dimension mismatch: query has 2d"),
            "dimension_mismatch",
        ),
        (RuntimeError("vector index is corrupt"), "unavailable"),
    ],
)
async def test_candidate_evidence_reports_failure_in_band(
    storage, monkeypatch, caplog, error, expected_status
):
    candidate = await _seed_candidate(storage)
    dense = _EvidenceStorage(error=error)
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    with caplog.at_level("WARNING", logger="memtomem.formation"):
        result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))

    # In band: the reviewer still gets an envelope, and can still decide.
    assert result["ok"] is True
    assert result["status"] == expected_status
    assert result["reason"]
    # The raised message stays in the log; the response carries fixed guidance.
    assert "2d" not in result["reason"]
    assert result["neighbours"] == []
    assert caplog.records


@pytest.mark.asyncio
async def test_candidate_evidence_distinguishes_bm25_only_from_no_neighbours(storage, monkeypatch):
    candidate = await _seed_candidate(storage)

    async def _never_called(*args, **kwargs):  # pragma: no cover - must not run
        raise AssertionError("dense_search called on a bm25-only store")

    monkeypatch.setattr(storage, "dense_search", _never_called, raising=False)
    monkeypatch.setattr(type(storage), "dense_enabled", property(lambda self: False))
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))
    assert result["status"] == "dense_disabled"
    assert result["neighbours"] == []


@pytest.mark.asyncio
async def test_cli_evidence_prints_envelope_and_reports_missing_candidate(monkeypatch, capsys):
    import click

    from memtomem.cli.review_cmd import _evidence

    dense = _EvidenceStorage([_neighbour_result("Canary rollout is the standard", 0.9)])
    storage = SimpleNamespace(
        get_memory_candidate=AsyncMock(
            side_effect=lambda cid: (
                {"id": cid, "content": "Decision: use blue-green deployment"}
                if cid == "candidate-1"
                else None
            )
        ),
        dense_enabled=True,
        dense_search=dense.dense_search,
    )

    @asynccontextmanager
    async def fake_components():
        yield SimpleNamespace(storage=storage, embedder=_Embedder(), config=_evidence_config())

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    await _evidence("candidate-1", 5)
    envelope = json.loads(capsys.readouterr().out)
    assert envelope["status"] == "available"
    assert envelope["neighbours"][0]["label"] == "potential_conflict"
    # CLI keeps paths verbatim; it runs on the machine that owns them.
    assert envelope["neighbours"][0]["source_file"] == str(_NEIGHBOUR_SOURCE)
    from memtomem.server.formatters import _display_path

    if _display_path(_NEIGHBOUR_SOURCE) != str(_NEIGHBOUR_SOURCE):
        # Where the two formatters disagree, this pins that the CLI does not
        # borrow the MCP one.
        assert envelope["neighbours"][0]["source_file"] != _display_path(_NEIGHBOUR_SOURCE)

    with pytest.raises(click.ClickException, match="Candidate not found"):
        await _evidence("nope", 5)


def test_candidate_evidence_is_reachable_through_mem_do():
    from memtomem.server.tool_registry import ACTIONS

    assert ACTIONS["candidate_evidence"].category == "formation"


@pytest.mark.asyncio
async def test_candidate_evidence_flags_a_corpus_with_no_vectors_yet(storage, monkeypatch):
    """An unvectorised corpus must not read as "nothing is close"."""
    candidate = await _seed_candidate(storage)
    dense = _EvidenceStorage([], coverage={"total": 12, "with_dense": 0})
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(storage, "get_dense_coverage", dense.get_dense_coverage, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))
    assert result["status"] == "dense_not_indexed"
    assert result["coverage"] == {"total": 12, "with_dense": 0}


@pytest.mark.asyncio
async def test_candidate_evidence_reports_partial_coverage_alongside_results(storage, monkeypatch):
    candidate = await _seed_candidate(storage)
    dense = _EvidenceStorage(
        [_neighbour_result("Canary rollout is the standard", 0.9)],
        coverage={"total": 10, "with_dense": 3},
    )
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(storage, "get_dense_coverage", dense.get_dense_coverage, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))
    assert result["status"] == "available"
    assert result["coverage"] == {"total": 10, "with_dense": 3}
    assert len(result["neighbours"]) == 1


@pytest.mark.asyncio
async def test_candidate_evidence_classifies_by_type_not_by_error_message(storage, monkeypatch):
    candidate = await _seed_candidate(storage)
    dense = _EvidenceStorage(
        error=ValueError("dimension mismatch in provider payload for token /secret")
    )
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(storage, "get_dense_coverage", dense.get_dense_coverage, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    result = json.loads(await formation_tools.mem_candidate_evidence(candidate["id"]))
    assert result["status"] == "unavailable"
    # The unrecognised error's own text stays in the log, not in the response.
    assert "/secret" not in json.dumps(result)


@pytest.mark.asyncio
async def test_cli_evidence_pins_the_same_project_scope_as_the_mcp_tool(monkeypatch, tmp_path):
    """CLI parity: inside a registered project both surfaces sample the same rows."""
    from memtomem.cli.review_cmd import _evidence

    project_root = (tmp_path / "proj").resolve()
    memories = project_root / ".memtomem" / "memories"
    memories.mkdir(parents=True)
    monkeypatch.chdir(project_root)

    seen = {}

    class _ScopeStorage(_EvidenceStorage):
        async def dense_search(self, embedding, top_k=20, **kwargs):
            seen.update(kwargs)
            return []

    dense = _ScopeStorage()
    storage = SimpleNamespace(
        get_memory_candidate=AsyncMock(return_value={"id": "c1", "content": "Decision: X"}),
        dense_enabled=True,
        dense_search=dense.dense_search,
        get_dense_coverage=dense.get_dense_coverage,
    )
    config = SimpleNamespace(indexing=SimpleNamespace(project_memory_dirs=[str(memories)]))

    @asynccontextmanager
    async def fake_components():
        yield SimpleNamespace(storage=storage, embedder=_Embedder(), config=config)

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake_components)
    await _evidence("c1", 5)
    assert seen["project_context_root"] == project_root


@pytest.mark.asyncio
async def test_dense_coverage_counts_only_the_scoped_project(storage, tmp_path):
    """Another project's vectors must not vouch for this one's corpus."""
    from memtomem.models import Chunk, ChunkMetadata

    here = (tmp_path / "here").resolve()
    there = (tmp_path / "there").resolve()
    for root in (here, there):
        (root / ".memtomem" / "memories").mkdir(parents=True)

    def _chunk(root, name):
        return Chunk(
            content=f"note in {name}",
            metadata=ChunkMetadata(
                source_file=root / ".memtomem" / "memories" / f"{name}.md",
                scope="project",
                project_root=root,
            ),
            embedding=[],
        )

    await storage.upsert_chunks([_chunk(here, "a"), _chunk(there, "b"), _chunk(there, "c")])

    store_wide = await storage.get_dense_coverage()
    scoped = await storage.get_dense_coverage(here)
    assert store_wide["total"] == 3
    assert scoped["total"] == 1


@pytest.mark.asyncio
async def test_candidate_evidence_inherits_the_accessor_lazy_expiry(storage, monkeypatch):
    """The one state change the action can cause, pinned so the docs stay true.

    Gathering evidence writes nothing of its own, but its candidate lookup is
    the shared accessor, which flips an already-expired pending row. The tool
    docstring says so; this keeps that claim honest.
    """
    candidate = await _seed_candidate(storage)
    storage._get_db().execute(
        "UPDATE memory_candidates SET expires_at='2000-01-01T00:00:00+00:00' WHERE id=?",
        (candidate["id"],),
    )
    storage._get_db().commit()

    dense = _EvidenceStorage([_neighbour_result("anything", 0.9)])
    monkeypatch.setattr(storage, "dense_search", dense.dense_search, raising=False)
    monkeypatch.setattr(
        formation_tools,
        "_get_app_initialized",
        AsyncMock(
            return_value=SimpleNamespace(
                storage=storage, embedder=_Embedder(), config=_evidence_config()
            )
        ),
    )
    await formation_tools.mem_candidate_evidence(candidate["id"])
    after = await storage.get_memory_candidate(candidate["id"])
    assert after["status"] == "expired"
