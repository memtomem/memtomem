"""Quality Lab search-observation contract tests."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from helpers import StubCtx
from memtomem.errors import TransactionOwnedError
from memtomem.server.context import AppContext
from memtomem.server.tools.reflection import mem_reflect
from memtomem.server.tools.search import mem_search
from memtomem.search.pipeline import SearchPipeline


async def _index_quality_note(components, memory_dir):
    note = memory_dir / "quality.md"
    note.write_text(
        "# Retrieval Quality\n\nDurable observation telemetry for local search.\n",
        encoding="utf-8",
    )
    await components.index_engine.index_file(note)
    return note


async def test_ranked_search_persists_durable_secret_free_observation(
    bm25_only_components,
):
    components, memory_dir = bm25_only_components
    note = await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "관측 telemetry", top_k=5, origin="web"
    )

    assert results
    assert stats.query_run_id is not None
    UUID(stats.query_run_id)
    assert stats.cache_hit is False
    assert stats.latency_ms is not None and stats.latency_ms >= 0

    await components.search_pipeline.flush_observation(stats.query_run_id)
    history = await components.storage.get_query_history(limit=10)
    assert len(history) == 1
    row = history[0]
    assert row["run_id"] == stats.query_run_id
    assert row["query_text"] == "관측 telemetry"
    assert row["observation"]["origin"] == "web"
    assert row["observation"]["query_language"] == "ko"
    assert len(row["observation"]["profile_id"]) == 64
    assert row["observation"]["final_total"] == len(results)
    assert row["result_snapshot"][0]["source_name"] == note.name
    snapshot_json = json.dumps(row["result_snapshot"], ensure_ascii=False)
    assert str(memory_dir) not in snapshot_json
    assert "content" not in row["result_snapshot"][0]


async def test_unknown_origin_is_recorded_as_internal(bm25_only_components):
    """Runtime backstop for untyped surfaces (#2089).

    ``mem_do`` raw params and persisted rows bypass the ``SearchOrigin``
    type, so an out-of-set value must degrade to ``"internal"`` rather
    than being recorded verbatim.
    """

    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "telemetry",
        top_k=5,
        origin="not-a-surface",  # type: ignore[arg-type]
    )

    assert results
    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["observation"]["origin"] == "internal"


async def test_snapshot_source_name_is_a_basename_never_a_path(bm25_only_components):
    """Value-level pin for the snapshot writer's projection (#1815).

    ``SnapshotEntryOut`` deliberately does not re-sanitize field contents
    (#1813) — the guarantee that ``source_name`` is a bare basename lives
    at the writer. A writer change that records an absolute or relative
    path must fail here, not surface through ``GET /api/search/runs``.
    """
    components, memory_dir = bm25_only_components
    nested = memory_dir / "projects" / "alpha"
    nested.mkdir(parents=True)
    note = nested / "pinned.md"
    note.write_text("# Pin\n\nBasename invariant probe content.\n", encoding="utf-8")
    await components.index_engine.index_file(note)

    results, stats = await components.search_pipeline.search(
        "basename invariant probe", top_k=5, origin="web"
    )

    assert results
    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["run_id"] == stats.query_run_id
    assert row["result_snapshot"]
    for entry in row["result_snapshot"]:
        source_name = entry["source_name"]
        assert source_name == note.name
        # Both separators: catches a path leak regardless of the OS the
        # writer ran on (POSIX "/" and Windows "\\").
        assert "/" not in source_name
        assert "\\" not in source_name


async def test_zero_result_ranked_search_still_gets_run_id(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "term-that-does-not-exist-xyz", origin="mcp"
    )

    assert results == []
    assert stats.query_run_id is not None
    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["run_id"] == stats.query_run_id
    assert row["observation"]["origin"] == "mcp"
    assert row["observation"]["final_total"] == 0
    assert row["result_snapshot"] == []


async def test_cache_hit_records_distinct_durable_run(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    _, first = await components.search_pipeline.search("telemetry", origin="cli")
    _, second = await components.search_pipeline.search("telemetry", origin="cli")

    assert first.query_run_id is not None
    assert second.query_run_id is not None
    assert first.query_run_id != second.query_run_id
    assert first.cache_hit is False
    assert second.cache_hit is True
    await components.search_pipeline.flush_observation()
    rows = await components.storage.get_query_history(limit=10)
    assert {row["run_id"] for row in rows} == {first.query_run_id, second.query_run_id}
    assert {row["observation"]["cache_hit"] for row in rows} == {False, True}


async def test_observation_failure_never_fails_search(bm25_only_components, monkeypatch, caplog):
    """A failed observation write is logged and strands its ID (#2183).

    Before the write moved off the response path the pipeline knew the
    outcome and returned ``None``. Now the ID is advertised first, so the
    failure surfaces one step later: the run stays unresolvable, which is
    what makes feedback on it fail rather than attach to nothing.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    failing_save = AsyncMock(side_effect=RuntimeError("observation DB unavailable"))
    monkeypatch.setattr(components.storage, "save_search_observation", failing_save)

    with caplog.at_level(logging.WARNING, logger="memtomem.search.pipeline"):
        results, stats = await components.search_pipeline.search("telemetry", origin="web")
        assert results
        assert stats.query_run_id is not None
        await components.search_pipeline.flush_observation(stats.query_run_id)

    failing_save.assert_awaited_once()
    assert any(
        "search observation persistence failed" in record.message for record in caplog.records
    )
    assert await components.storage.get_query_history(limit=10) == []
    with pytest.raises(KeyError):
        await components.storage.get_search_run(stats.query_run_id)


async def test_filter_only_search_is_not_observed(bm25_only_components):
    components, memory_dir = bm25_only_components
    note = await _index_quality_note(components, memory_dir)

    results, stats = await components.search_pipeline.search(
        "", source_filter=note.name, origin="web"
    )

    assert results
    assert stats.query_run_id is None
    assert await components.storage.get_query_history(limit=10) == []


async def test_mcp_structured_search_exposes_eventually_persisted_run_id(
    bm25_only_components,
):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    output = await mem_search(  # type: ignore[arg-type]
        query="telemetry", output_format="structured", ctx=ctx
    )

    payload = json.loads(output)
    UUID(payload["query_run_id"])
    await components.search_pipeline.flush_observation(payload["query_run_id"])
    row = (await components.storage.get_query_history(limit=1))[0]
    assert row["run_id"] == payload["query_run_id"]
    assert row["observation"]["origin"] == "mcp"


async def test_legacy_backend_keeps_fire_and_forget_and_skips_cache_hit_history(
    bm25_only_components,
):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    class LegacyStorageProxy:
        def __init__(self, delegate):
            self._delegate = delegate
            self.save_query_history = AsyncMock()

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    legacy_storage = LegacyStorageProxy(components.storage)
    pipeline = SearchPipeline(
        storage=legacy_storage,  # type: ignore[arg-type]
        embedder=components.embedder,
        config=components.config.search,
    )

    results, first = await pipeline.search("telemetry", origin="internal")
    await asyncio.sleep(0)
    cached_results, second = await pipeline.search("telemetry", origin="internal")
    await asyncio.sleep(0)

    assert results and cached_results
    assert first.query_run_id is None
    assert second.query_run_id is None
    assert second.cache_hit is True
    legacy_storage.save_query_history.assert_awaited_once()


async def test_explicit_instance_observation_capability_is_used(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    class InstanceCapabilityProxy:
        def __init__(self, delegate):
            self._delegate = delegate
            self.save_search_observation = AsyncMock(
                side_effect=lambda *args, **kwargs: kwargs["run_id"]
            )

        def __getattr__(self, name):
            return getattr(self._delegate, name)

    proxy = InstanceCapabilityProxy(components.storage)
    pipeline = SearchPipeline(
        storage=proxy,  # type: ignore[arg-type]
        embedder=components.embedder,
        config=components.config.search,
    )

    results, stats = await pipeline.search("telemetry", origin="internal")

    assert results
    assert stats.query_run_id is not None
    await pipeline.flush_observation(stats.query_run_id)
    proxy.save_search_observation.assert_awaited_once()


# ---- #2183: the observation write runs off the search response path --------


async def test_cache_hit_returns_before_the_observation_write_touches_storage(
    bm25_only_components, monkeypatch
):
    """The acceptance criterion of #2183, pinned at its narrowest point.

    A cache hit does no retrieval work, so the writer round trip was the
    only thing left on its critical path. The saver is gated on an event
    the test only sets after ``search()`` has already returned: if the
    write were awaited inline, this would deadlock rather than fail.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    await components.search_pipeline.search("telemetry", origin="cli")
    await components.search_pipeline.flush_observation()

    gate = asyncio.Event()
    real_save = components.storage.save_search_observation

    async def gated_save(*args, **kwargs):
        await gate.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(components.storage, "save_search_observation", gated_save)

    results, stats = await components.search_pipeline.search("telemetry", origin="cli")

    assert stats.cache_hit is True
    assert results
    assert stats.query_run_id is not None
    # Returned with the write still parked inside the gate.
    assert not gate.is_set()

    gate.set()
    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = await components.storage.get_search_run(stats.query_run_id)
    assert row["observation"]["cache_hit"] is True


async def test_advertised_run_id_matches_the_row_written_later(bm25_only_components):
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    _, stats = await components.search_pipeline.search("telemetry", origin="web")

    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = await components.storage.get_search_run(stats.query_run_id)
    assert row["run_id"] == stats.query_run_id
    assert row["query_text"] == "telemetry"


async def test_prune_never_runs_on_the_response_path(bm25_only_components, monkeypatch):
    """#2183: every 100th observation also pruned, inline, before responding."""
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    storage = components.storage
    monkeypatch.setattr(
        storage, "_history_save_count", storage._HISTORY_PRUNE_INTERVAL - 1, raising=False
    )
    pruned: list[bool] = []
    monkeypatch.setattr(storage, "_prune_old_history", lambda: pruned.append(True))

    _, stats = await components.search_pipeline.search("telemetry", origin="web")

    assert stats.query_run_id is not None
    assert pruned == []
    await components.search_pipeline.flush_observation(stats.query_run_id)
    assert pruned == [True]


async def test_flush_is_a_no_op_with_nothing_pending(bm25_only_components):
    """``asyncio.wait([])`` raises, and every history read flushes first."""
    components, _ = bm25_only_components

    await components.search_pipeline.flush_observation()
    await components.search_pipeline.flush_observation("no-such-run-id")


async def test_cancelling_a_flush_leaves_the_write_running(bm25_only_components, monkeypatch):
    """A cancelled reader must not strand the ID its search advertised.

    ``gather`` would cancel the persistence task along with the waiter;
    ``asyncio.wait`` leaves it alone.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    gate = asyncio.Event()
    real_save = components.storage.save_search_observation

    async def gated_save(*args, **kwargs):
        await gate.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(components.storage, "save_search_observation", gated_save)

    _, stats = await components.search_pipeline.search("telemetry", origin="web")
    waiter = asyncio.ensure_future(components.search_pipeline.flush_observation(stats.query_run_id))
    await asyncio.sleep(0)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter

    gate.set()
    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = await components.storage.get_search_run(stats.query_run_id)
    assert row["run_id"] == stats.query_run_id


async def test_observation_write_waits_out_a_foreign_transaction(bm25_only_components, monkeypatch):
    """The writer connection is single-owner; a background write must yield.

    ``_get_db`` rejects a task that does not own an open transaction, so
    off the response path the write retries rather than dropping the row —
    and it never lands inside the other task's transaction.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    storage = components.storage

    holding = asyncio.Event()
    search_scheduled = asyncio.Event()
    rejected: list[bool] = []
    real_get_db = storage._get_db

    def counting_get_db():
        try:
            return real_get_db()
        except TransactionOwnedError:
            rejected.append(True)
            raise

    monkeypatch.setattr(storage, "_get_db", counting_get_db)

    async def hold_transaction():
        async with storage.transaction():
            holding.set()
            # Held across the search, then released well inside the write's
            # retry budget — the point is that the write waits, not that it
            # waits forever.
            await search_scheduled.wait()

    owner = asyncio.ensure_future(hold_transaction())
    await holding.wait()

    _, stats = await components.search_pipeline.search("telemetry", origin="web")
    assert stats.query_run_id is not None
    search_scheduled.set()
    await owner

    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = await storage.get_search_run(stats.query_run_id)
    assert row["run_id"] == stats.query_run_id
    # The write really did bounce off the foreign owner before succeeding.
    assert rejected


async def test_observation_reflects_the_search_not_later_caller_mutations(
    bm25_only_components, monkeypatch
):
    """The write runs after ``search()`` returns, so its inputs are copies."""
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    gate = asyncio.Event()
    real_save = components.storage.save_search_observation

    async def gated_save(*args, **kwargs):
        await gate.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(components.storage, "save_search_observation", gated_save)

    namespaces = ["default"]
    results, stats = await components.search_pipeline.search(
        "telemetry", origin="web", namespace=namespaces
    )

    namespaces.append("mutated-after-the-search")
    results.clear()

    gate.set()
    await components.search_pipeline.flush_observation(stats.query_run_id)
    row = await components.storage.get_search_run(stats.query_run_id)
    assert row["observation"]["filters"]["namespace"] == ["default"]
    assert row["result_snapshot"]


async def test_close_waits_for_a_pending_observation(bm25_only_components, monkeypatch):
    """Shutdown must not lose a write that is still in flight.

    The saver is gated, so an unblocked ``close()`` would return with the
    row unwritten — the assertion that ``close()`` stays pending is what
    makes this a drain test rather than a race the fast path usually wins.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    pipeline = SearchPipeline(
        storage=components.storage,
        embedder=components.embedder,
        config=components.config.search,
    )

    gate = asyncio.Event()
    real_save = components.storage.save_search_observation

    async def gated_save(*args, **kwargs):
        await gate.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(components.storage, "save_search_observation", gated_save)

    _, stats = await pipeline.search("telemetry", origin="internal")
    closing = asyncio.ensure_future(pipeline.close())
    for _ in range(5):
        await asyncio.sleep(0)
    assert not closing.done()

    gate.set()
    await closing

    assert pipeline._pending_observations == {}
    row = await components.storage.get_search_run(stats.query_run_id)
    assert row["run_id"] == stats.query_run_id


async def test_a_settled_write_removes_its_own_pending_entry(bm25_only_components, monkeypatch):
    """The map is drained by each write, not only by ``close()``.

    Without this the entries would accumulate for the process lifetime and
    every flush-all would wait on tasks that finished long ago.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    pipeline = components.search_pipeline

    _, ok = await pipeline.search("telemetry", origin="web")
    await pipeline.flush_observation(ok.query_run_id)
    assert pipeline._pending_observations == {}

    monkeypatch.setattr(
        components.storage,
        "save_search_observation",
        AsyncMock(side_effect=RuntimeError("observation DB unavailable")),
    )
    _, failed = await pipeline.search("telemetry failing write", origin="web")
    await pipeline.flush_observation(failed.query_run_id)
    assert pipeline._pending_observations == {}


async def test_reflection_sees_the_zero_result_search_it_just_answered(bm25_only_components):
    """``mem_reflect``'s knowledge-gap section counts zero-result searches.

    Those rows are exactly the ones the background write (#2183) may not
    have committed yet, so a reflection run straight after a failed search
    would otherwise report no gap at all.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    _, stats = await components.search_pipeline.search("term-that-does-not-exist-xyz", origin="mcp")
    assert stats.query_run_id is not None

    report = await mem_reflect(ctx=ctx)  # type: ignore[arg-type]

    assert "term-that-does-not-exist-xyz" in report


async def test_created_at_records_the_search_not_the_write(bm25_only_components, monkeypatch):
    """History is ordered and ``since``-filtered on ``created_at`` (#2183).

    The write is backlogged and may retry, so stamping it inside the writer
    would order runs by when the queue drained rather than when they ran.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)

    # A controlled clock, not a time window: a drain-time stamp usually lands
    # in the same second as the search, so a window assertion would pass even
    # if the pipeline stopped stamping. Here the two times cannot collide.
    search_time = datetime(2031, 3, 4, 5, 6, 7, tzinfo=UTC)

    class FrozenAtSearch:
        @staticmethod
        def now(tz=None):
            return search_time

    monkeypatch.setattr("memtomem.search.pipeline.datetime", FrozenAtSearch)

    gate = asyncio.Event()
    real_save = components.storage.save_search_observation

    async def gated_save(*args, **kwargs):
        await gate.wait()
        return await real_save(*args, **kwargs)

    monkeypatch.setattr(components.storage, "save_search_observation", gated_save)

    _, stats = await components.search_pipeline.search("telemetry", origin="web")

    gate.set()
    await components.search_pipeline.flush_observation(stats.query_run_id)

    row = await components.storage.get_search_run(stats.query_run_id)
    assert row["created_at"] == search_time.isoformat(timespec="seconds")


async def test_backend_on_the_old_saver_signature_still_works(bm25_only_components):
    """``save_search_observation`` is duck-typed, not an ABC method (#2183).

    A backend written against the pre-``created_at`` signature must keep
    working — passing it an unknown keyword would raise inside the background
    task and strand the run ID it had already handed out.
    """
    components, memory_dir = bm25_only_components
    await _index_quality_note(components, memory_dir)
    delegate = components.storage
    received: list[dict] = []

    class OldSignatureBackend:
        def __init__(self, inner):
            self._inner = inner

        async def save_search_observation(
            self,
            query_text,
            query_embedding,
            result_chunk_ids,
            result_scores,
            *,
            run_id,
            observation,
            result_snapshot,
        ):
            received.append({"run_id": run_id})
            return await self._inner.save_search_observation(
                query_text,
                query_embedding,
                result_chunk_ids,
                result_scores,
                run_id=run_id,
                observation=observation,
                result_snapshot=result_snapshot,
            )

        def __getattr__(self, name):
            return getattr(self._inner, name)

    pipeline = SearchPipeline(
        storage=OldSignatureBackend(delegate),  # type: ignore[arg-type]
        embedder=components.embedder,
        config=components.config.search,
    )

    _, stats = await pipeline.search("telemetry", origin="internal")
    await pipeline.flush_observation(stats.query_run_id)

    assert received == [{"run_id": stats.query_run_id}]
    row = await delegate.get_search_run(stats.query_run_id)
    assert row["run_id"] == stats.query_run_id
