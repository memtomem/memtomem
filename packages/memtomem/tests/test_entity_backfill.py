"""One-time startup entity backfill for pre-#2145 stores (#2133).

The write path extracts entities for everything written since #2145/#2155;
these tests pin the walk that gives *already stored* chunks the same attempt —
and, just as load-bearing, the states in which it must refuse to walk at all.
"""

from __future__ import annotations

import pytest
from helpers import make_chunk

from memtomem.storage.mixins.entities import (
    _ENTITY_BACKFILL_KEY,
    ENTITY_BACKFILL_DONE,
    ENTITY_BACKFILL_STALE,
)
from memtomem.tools.entity_backfill import backfill_entities
from memtomem.tools.entity_sync import sync_entities_for_chunks

# Regex-stable extraction targets, mirroring ``test_index_time_entities.py``.
_WITH_PYTHON = "We decided to use Python for the parser.\n"
_NO_ENTITIES = "aaa bbb ccc\n"


async def _clear_state(storage) -> None:
    """Remove the marker ``create_components`` stamped at fixture setup.

    The fixture stack runs the backfill on its own empty store and lands on
    ``done`` — which is itself the integration proof, see
    ``TestAutomaticExecution`` — so tests exercising the walk reset to the
    never-ran state first.
    """
    db = storage._get_db()
    db.execute("DELETE FROM _memtomem_meta WHERE key = ?", (_ENTITY_BACKFILL_KEY,))
    db.commit()


async def _seed_bare_chunks(storage, n=3, content=_WITH_PYTHON, source="legacy.md"):
    """Store chunks the way a pre-#2145 binary did: no entity sync."""
    chunks = [make_chunk(f"{content} #{i}", source=source) for i in range(n)]
    await storage.upsert_chunks(chunks)
    return chunks


class TestBackfillWalk:
    @pytest.mark.asyncio
    async def test_populates_bare_chunks_including_gone_sources(self, bm25_only_components):
        comp, _ = bm25_only_components
        # A source path that exists nowhere on disk — the chunks a forced
        # re-index can never reach (consolidation summaries, imports). Content
        # must come from the DB, so these get covered like any others.
        chunks = await _seed_bare_chunks(comp.storage, source="gone.consolidated.md")
        await _clear_state(comp.storage)

        processed = await backfill_entities(comp.storage, enabled=True)

        assert processed == len(chunks)
        for chunk in chunks:
            values = {
                (e["entity_type"], e["entity_value"])
                for e in await comp.storage.get_entities_for_chunk(str(chunk.id))
            }
            assert ("technology", "Python") in values
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

    @pytest.mark.asyncio
    async def test_entityless_chunks_get_their_attempt_and_done_ends_it(self, bm25_only_components):
        """A chunk that extracts to nothing stays row-less — only the ``done``
        flag stops it being re-walked forever, so the flag must land."""
        comp, _ = bm25_only_components
        chunks = await _seed_bare_chunks(comp.storage, content=_NO_ENTITIES)
        await _clear_state(comp.storage)

        assert await backfill_entities(comp.storage, enabled=True) == len(chunks)
        for chunk in chunks:
            assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE
        # The re-run is the point: without the flag this would walk again.
        assert await backfill_entities(comp.storage, enabled=True) == 0

    @pytest.mark.asyncio
    async def test_existing_rows_are_never_touched(self, bm25_only_components):
        """Rows may be an LLM scan's product, strictly richer than regex —
        the walk is insert-only for bare chunks and hands-off for the rest."""
        comp, _ = bm25_only_components
        chunk = make_chunk(_WITH_PYTHON, source="scanned.md")
        await comp.storage.upsert_chunks([chunk])
        llm_rows = [{"entity_type": "concept", "entity_value": "parser design", "confidence": 0.7}]
        await comp.storage.upsert_entities(str(chunk.id), llm_rows)
        await _clear_state(comp.storage)

        await backfill_entities(comp.storage, enabled=True)

        stored = await comp.storage.get_entities_for_chunk(str(chunk.id))
        assert [(e["entity_type"], e["entity_value"]) for e in stored] == [
            ("concept", "parser design")
        ]

    @pytest.mark.asyncio
    async def test_rows_gained_between_selection_and_write_survive(self, bm25_only_components):
        """The in-transaction re-check: a scan that populates a selected chunk
        after the read-pool page was taken must win, because
        ``upsert_entities`` deletes before inserting."""
        comp, _ = bm25_only_components
        storage = comp.storage
        chunks = await _seed_bare_chunks(storage, n=2)
        await _clear_state(storage)
        target = chunks[0]
        llm_rows = [{"entity_type": "concept", "entity_value": "race window", "confidence": 0.8}]

        real_list = storage.list_chunks_missing_entities
        raced = {"done": False}

        async def _list_then_race(**kwargs):
            page = await real_list(**kwargs)
            if not raced["done"]:
                raced["done"] = True
                await storage.upsert_entities(str(target.id), llm_rows)
            return page

        storage.list_chunks_missing_entities = _list_then_race
        try:
            await backfill_entities(storage, enabled=True)
        finally:
            storage.list_chunks_missing_entities = real_list

        stored = await storage.get_entities_for_chunk(str(target.id))
        assert [(e["entity_type"], e["entity_value"]) for e in stored] == [
            ("concept", "race window")
        ]
        # The chunk that was not raced still got its regex rows.
        assert await storage.get_entities_for_chunk(str(chunks[1].id))

    @pytest.mark.asyncio
    async def test_crash_resumes_from_persisted_cursor(self, bm25_only_components):
        comp, _ = bm25_only_components
        storage = comp.storage
        chunks = await _seed_bare_chunks(storage, n=4)
        await _clear_state(storage)

        real_filter = storage.filter_unextracted_chunk_ids
        calls = {"n": 0}

        async def _fail_second_batch(chunk_ids):
            calls["n"] += 1
            if calls["n"] == 2:
                raise RuntimeError("crashed mid-walk")
            return await real_filter(chunk_ids)

        storage.filter_unextracted_chunk_ids = _fail_second_batch
        try:
            with pytest.raises(RuntimeError, match="crashed mid-walk"):
                await backfill_entities(storage, enabled=True, batch_size=2)
        finally:
            storage.filter_unextracted_chunk_ids = real_filter

        state = await storage.entity_backfill_get_state()
        assert state is not None and state.isdigit()  # batch 1's cursor survived
        (error_run,) = await storage.maintenance_run_latest(kind="entity_backfill", limit=1)
        assert error_run["status"] == "error"

        assert await backfill_entities(storage, enabled=True, batch_size=2) == 2
        assert await storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE
        for chunk in chunks:
            assert await storage.get_entities_for_chunk(str(chunk.id))

    @pytest.mark.asyncio
    async def test_startup_cap_pauses_and_resumes(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunks = await _seed_bare_chunks(comp.storage, n=5)
        await _clear_state(comp.storage)

        assert (
            await backfill_entities(
                comp.storage, enabled=True, batch_size=2, max_chunks_per_startup=4
            )
            == 4
        )
        state = await comp.storage.entity_backfill_get_state()
        assert state is not None and state.isdigit()
        (run,) = await comp.storage.maintenance_run_latest(kind="entity_backfill", limit=1)
        assert run["status"] == "ok"
        assert run["summary"]["completed"] is False

        assert await backfill_entities(comp.storage, enabled=True, batch_size=2) == 1
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE
        for chunk in chunks:
            assert await comp.storage.get_entities_for_chunk(str(chunk.id))


class TestRefusalStates:
    @pytest.mark.asyncio
    async def test_disabled_writes_nothing_and_leaves_no_flag(self, bm25_only_components):
        """``done`` under a disabled config would strand a user who enables
        extraction later — the flag means completed, not considered."""
        comp, _ = bm25_only_components
        chunks = await _seed_bare_chunks(comp.storage)
        await _clear_state(comp.storage)

        assert await backfill_entities(comp.storage, enabled=False) == 0
        assert await comp.storage.entity_backfill_get_state() is None
        for chunk in chunks:
            assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []

        # Enabling later completes normally.
        assert await backfill_entities(comp.storage, enabled=True) == len(chunks)
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

    @pytest.mark.asyncio
    async def test_stale_short_circuits_like_done(self, bm25_only_components):
        """The gap ``stale`` records is an explicit opt-out; remediation is
        ``mem_entity_scan``, never a silent re-walk."""
        comp, _ = bm25_only_components
        chunks = await _seed_bare_chunks(comp.storage)
        await comp.storage.entity_backfill_set_state(ENTITY_BACKFILL_STALE)

        assert await backfill_entities(comp.storage, enabled=True) == 0
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_STALE
        for chunk in chunks:
            assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []

    @pytest.mark.asyncio
    async def test_backend_without_the_surface_degrades_to_noop(self):
        class Bare:
            pass

        assert await backfill_entities(Bare(), enabled=True) == 0

    @pytest.mark.asyncio
    async def test_noop_startup_leaves_no_audit_row(self, bm25_only_components):
        comp, _ = bm25_only_components  # fixture startup already ran to ``done``
        assert await backfill_entities(comp.storage, enabled=True) == 0
        assert await comp.storage.maintenance_run_latest(kind="entity_backfill") == []


class TestStaleArming:
    @pytest.mark.asyncio
    async def test_disabled_content_write_downgrades_done(self, bm25_only_components):
        comp, _ = bm25_only_components
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

        chunk = make_chunk(_WITH_PYTHON, source="late.md")
        await comp.storage.upsert_chunks([chunk])
        assert await sync_entities_for_chunks(comp.storage, [chunk], enabled=False) == 0

        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_STALE
        assert await comp.storage.get_entities_for_chunk(str(chunk.id)) == []

    @pytest.mark.asyncio
    async def test_arms_inside_the_callers_transaction(self, bm25_only_components):
        """The marker and the unextracted content must commit together — the
        arming write may not end the caller's transaction (#2168)."""
        comp, _ = bm25_only_components
        chunk = make_chunk(_WITH_PYTHON, source="txn.md")
        async with comp.storage.transaction():
            await comp.storage.upsert_chunks([chunk])
            await sync_entities_for_chunks(comp.storage, [chunk], enabled=False)
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_STALE

    @pytest.mark.asyncio
    async def test_race_loser_write_does_not_arm(self, bm25_only_components):
        """A chunk that lost the #691 uniqueness race stored nothing, so a
        disabled write of only race-losers is not a coverage gap."""
        comp, _ = bm25_only_components
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

        # Never persisted — stands in for the loser whose id was dropped.
        ghost = make_chunk(_WITH_PYTHON, source="loser.md")
        await sync_entities_for_chunks(comp.storage, [ghost], enabled=False)
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

    @pytest.mark.asyncio
    async def test_no_arming_without_done_or_without_chunks(self, bm25_only_components):
        comp, _ = bm25_only_components
        chunk = make_chunk(_WITH_PYTHON, source="cursor.md")
        await comp.storage.upsert_chunks([chunk])

        # Mid-walk cursor keeps its resume position.
        await comp.storage.entity_backfill_set_state("7")
        await sync_entities_for_chunks(comp.storage, [chunk], enabled=False)
        assert await comp.storage.entity_backfill_get_state() == "7"

        # An empty write arms nothing.
        await comp.storage.entity_backfill_set_state(ENTITY_BACKFILL_DONE)
        await sync_entities_for_chunks(comp.storage, [], enabled=False)
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

        # An *enabled* write is not a gap.
        await sync_entities_for_chunks(comp.storage, [chunk], enabled=True)
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE


class TestAutomaticExecution:
    @pytest.mark.asyncio
    async def test_create_components_ran_the_backfill(self, bm25_only_components):
        """The fixture goes through ``create_components`` on a fresh store; the
        marker's presence is the proof no entry point needs a command."""
        comp, _ = bm25_only_components
        assert await comp.storage.entity_backfill_get_state() == ENTITY_BACKFILL_DONE

    @pytest.mark.asyncio
    async def test_opt_out_skips_the_walk(self, bm25_only_components, tmp_path):
        """Quality Lab's transient replay stacks must observe, not migrate."""
        from memtomem.config import Mem2MemConfig
        from memtomem.server.component_factory import close_components, create_components

        comp, _ = bm25_only_components
        chunks = await _seed_bare_chunks(comp.storage)
        await _clear_state(comp.storage)

        config = Mem2MemConfig()
        config.storage.sqlite_path = comp.config.storage.sqlite_path
        config.indexing.memory_dirs = list(comp.config.indexing.memory_dirs)
        config.embedding.dimension = 1024
        config.search.enable_dense = False
        transient = await create_components(
            config, load_ambient_config=False, entity_backfill=False
        )
        try:
            assert await transient.storage.entity_backfill_get_state() is None
            for chunk in chunks:
                assert await transient.storage.get_entities_for_chunk(str(chunk.id)) == []
        finally:
            await close_components(transient)
