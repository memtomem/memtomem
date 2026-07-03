"""Retry-idempotency for the MCP memory-write tools (issue #1573).

The three memory-write tools (``mem_add`` / ``mem_batch_add`` /
``mem_agent_share``) accept an optional ``idempotency_key``. A retried call
with a seen key returns the original result and performs NO write, so a
transport retry of a lost response can't duplicate data. Without a key the
tools stay at-least-once (documented). This file pins:

* the storage-layer ledger (``idempotency_get`` / ``idempotency_put``);
* replay-suppresses-write for each of the three tools;
* only *successful* writes are recorded (a failed call stays re-runnable);
* the concurrent-same-key guard for ``mem_add`` (per-file lock);
* the ``mem_batch_add`` atomic single-write refactor (issue #1573).
"""

from __future__ import annotations

import asyncio

import pytest

from memtomem.constants import AGENT_NAMESPACE_PREFIX, SHARED_NAMESPACE
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import _REPLAY_MARKER, mem_add, mem_batch_add
from memtomem.server.tools.multi_agent import mem_agent_register, mem_agent_share

from helpers import StubCtx, make_chunk

# Single-pattern secret sample (matches sk-... pattern #4), same as
# test_memory_crud_redaction.py — no other ambiguity.
_SECRET_SAMPLE = "Notes on token: sk-" + "a" * 30


def _heading_count(path) -> int:
    """Number of ``## `` entry headings in a memory file."""
    if not path.exists():
        return 0
    return sum(
        1 for line in path.read_text(encoding="utf-8").splitlines() if line.startswith("## ")
    )


def _total_headings(mem_dir) -> int:
    """Total ``## `` headings across every markdown file under mem_dir."""
    return sum(_heading_count(p) for p in mem_dir.rglob("*.md"))


def _ledger_count(comp) -> int:
    db = comp.storage._get_db()
    return db.execute("SELECT COUNT(*) FROM idempotency_ledger").fetchone()[0]


# ── Storage layer ───────────────────────────────────────────────────────────


class TestLedgerStorage:
    @pytest.mark.asyncio
    async def test_claim_complete_get_roundtrip(self, bm25_only_components):
        comp, _ = bm25_only_components
        assert await comp.storage.idempotency_get("mem_add", "k1") is None
        # First claim wins; a completed get is still a miss until complete().
        assert await comp.storage.idempotency_claim("mem_add", "k1") == ("won", None)
        assert await comp.storage.idempotency_get("mem_add", "k1") is None
        await comp.storage.idempotency_complete("mem_add", "k1", "RESULT-1")
        assert await comp.storage.idempotency_get("mem_add", "k1") == "RESULT-1"
        # PK is (tool, key): a different tool or key is a miss.
        assert await comp.storage.idempotency_get("mem_batch_add", "k1") is None
        assert await comp.storage.idempotency_get("mem_add", "k2") is None

    @pytest.mark.asyncio
    async def test_second_claim_sees_pending_then_completed(self, bm25_only_components):
        comp, _ = bm25_only_components
        assert await comp.storage.idempotency_claim("mem_add", "k") == ("won", None)
        # A second claim before completion is told it's in flight.
        assert await comp.storage.idempotency_claim("mem_add", "k") == ("pending", None)
        await comp.storage.idempotency_complete("mem_add", "k", "R")
        # After completion the second claim replays the stored result.
        assert await comp.storage.idempotency_claim("mem_add", "k") == ("completed", "R")

    @pytest.mark.asyncio
    async def test_release_lets_key_be_reclaimed(self, bm25_only_components):
        comp, _ = bm25_only_components
        assert await comp.storage.idempotency_claim("mem_add", "k") == ("won", None)
        await comp.storage.idempotency_release("mem_add", "k")
        assert _ledger_count(comp) == 0
        # The key is free again after a released (failed) claim.
        assert await comp.storage.idempotency_claim("mem_add", "k") == ("won", None)

    @pytest.mark.asyncio
    async def test_release_does_not_remove_completed_row(self, bm25_only_components):
        comp, _ = bm25_only_components
        await comp.storage.idempotency_claim("mem_add", "k")
        await comp.storage.idempotency_complete("mem_add", "k", "R")
        # Release is scoped to pending rows — a completed row survives.
        await comp.storage.idempotency_release("mem_add", "k")
        assert await comp.storage.idempotency_get("mem_add", "k") == "R"

    @pytest.mark.asyncio
    async def test_expired_completed_row_is_miss(self, bm25_only_components):
        comp, _ = bm25_only_components
        await comp.storage.idempotency_claim("mem_add", "exp", ttl_s=-1)
        await comp.storage.idempotency_complete("mem_add", "exp", "X", ttl_s=-1)
        assert await comp.storage.idempotency_get("mem_add", "exp") is None

    @pytest.mark.asyncio
    async def test_claim_purges_expired_rows(self, bm25_only_components):
        comp, _ = bm25_only_components
        await comp.storage.idempotency_claim("mem_add", "old", ttl_s=-1)
        await comp.storage.idempotency_complete("mem_add", "old", "X", ttl_s=-1)
        # A fresh claim runs the lazy purge, dropping the expired 'old' row.
        await comp.storage.idempotency_claim("mem_add", "new")
        assert _ledger_count(comp) == 1


# ── mem_add ─────────────────────────────────────────────────────────────────


class TestMemAddIdempotency:
    @pytest.mark.asyncio
    async def test_replay_returns_original_and_writes_once(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        first = await mem_add(  # type: ignore[arg-type]
            content="remember this", title="A", file="x.md", idempotency_key="k1", ctx=ctx
        )
        base = first.split(_REPLAY_MARKER)[0]
        second = await mem_add(  # type: ignore[arg-type]
            content="remember this", title="A", file="x.md", idempotency_key="k1", ctx=ctx
        )

        assert _REPLAY_MARKER in second
        assert second.startswith(base)
        assert _REPLAY_MARKER not in first
        assert _heading_count(target) == 1
        assert len(await comp.storage.list_chunks_by_source(target)) == 1

    @pytest.mark.asyncio
    async def test_no_key_stays_at_least_once(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        await mem_add(content="dup", title="A", file="x.md", ctx=ctx)  # type: ignore[arg-type]
        await mem_add(content="dup", title="A", file="x.md", ctx=ctx)  # type: ignore[arg-type]
        assert _heading_count(target) == 2

    @pytest.mark.asyncio
    async def test_distinct_keys_write_twice(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        await mem_add(  # type: ignore[arg-type]
            content="a", title="A", file="x.md", idempotency_key="k1", ctx=ctx
        )
        await mem_add(  # type: ignore[arg-type]
            content="a", title="A", file="x.md", idempotency_key="k2", ctx=ctx
        )
        assert _heading_count(target) == 2

    @pytest.mark.asyncio
    async def test_rejects_overlong_key(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        result = await mem_add(  # type: ignore[arg-type]
            content="hello", file="x.md", idempotency_key="x" * 257, ctx=ctx
        )
        assert result.startswith("Error: idempotency_key too long")
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_rejects_blank_key(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        result = await mem_add(  # type: ignore[arg-type]
            content="hello", file="x.md", idempotency_key="   ", ctx=ctx
        )
        assert result.startswith("Error: idempotency_key must be non-empty")

    @pytest.mark.asyncio
    async def test_failed_call_not_recorded(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        # Empty content fails before any write; the key must stay re-runnable.
        err = await mem_add(  # type: ignore[arg-type]
            content="   ", file="x.md", idempotency_key="k1", ctx=ctx
        )
        assert err.startswith("Error")
        assert await comp.storage.idempotency_get("mem_add", "k1") is None

        ok = await mem_add(  # type: ignore[arg-type]
            content="real content now", file="x.md", idempotency_key="k1", ctx=ctx
        )
        assert _REPLAY_MARKER not in ok
        assert _heading_count(target) == 1

    @pytest.mark.asyncio
    async def test_redaction_block_not_recorded(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        blocked = await mem_add(  # type: ignore[arg-type]
            content=_SECRET_SAMPLE, file="x.md", idempotency_key="k1", ctx=ctx
        )
        assert "Error" in blocked
        assert await comp.storage.idempotency_get("mem_add", "k1") is None
        assert not target.exists()

        # Same key with clean content performs a real write (not a replay).
        ok = await mem_add(  # type: ignore[arg-type]
            content="clean content", file="x.md", idempotency_key="k1", ctx=ctx
        )
        assert _REPLAY_MARKER not in ok
        assert _heading_count(target) == 1

    @pytest.mark.asyncio
    async def test_concurrent_same_key_single_write(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        results = await asyncio.gather(
            mem_add(  # type: ignore[arg-type]
                content="race", title="A", file="x.md", idempotency_key="k1", ctx=ctx
            ),
            mem_add(  # type: ignore[arg-type]
                content="race", title="A", file="x.md", idempotency_key="k1", ctx=ctx
            ),
        )
        # The per-file lock serializes the two calls: exactly one writes, the
        # other replays from the ledger.
        assert _heading_count(target) == 1
        assert sum(_REPLAY_MARKER in r for r in results) == 1

    @pytest.mark.asyncio
    async def test_concurrent_same_key_different_files_single_write(self, bm25_only_components):
        """Codex-blocker regression: two same-key calls to DIFFERENT files must
        not both write. The global (tool, key) claim (not the per-file lock)
        blocks the second — it gets an 'in progress' error or a replay, never a
        second physical write."""
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        a, b = mem_dir / "a.md", mem_dir / "b.md"

        results = await asyncio.gather(
            mem_add(  # type: ignore[arg-type]
                content="race", title="A", file="a.md", idempotency_key="k1", ctx=ctx
            ),
            mem_add(  # type: ignore[arg-type]
                content="race", title="B", file="b.md", idempotency_key="k1", ctx=ctx
            ),
        )
        # Exactly one physical entry lands across both targets.
        assert _heading_count(a) + _heading_count(b) == 1
        # Exactly one call performed the write; the other was suppressed as an
        # in-progress conflict or a replay (neither wrote).
        suppressed = sum(("in progress" in r or _REPLAY_MARKER in r) for r in results)
        assert suppressed == 1

    @pytest.mark.asyncio
    async def test_index_failure_after_append_no_duplicate_on_retry(
        self, bm25_only_components, monkeypatch
    ):
        """Codex re-review regression: the append lands but index_file raises.
        The durable entry is on disk, so the claim must NOT be released — a
        retry with the same key must not re-append a duplicate."""
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "x.md"

        original = comp.index_engine.index_file

        async def flaky(*a, **k):
            raise RuntimeError("index boom")

        monkeypatch.setattr(comp.index_engine, "index_file", flaky)

        err = await mem_add(  # type: ignore[arg-type]
            content="durable", title="A", file="x.md", idempotency_key="k1", ctx=ctx
        )
        assert "Error" in err
        assert _heading_count(target) == 1  # the append is durable despite index failure

        # Retry with the same key: the claim is still pending (never released),
        # so the durable append is not repeated.
        retry = await mem_add(  # type: ignore[arg-type]
            content="durable", title="A", file="x.md", idempotency_key="k1", ctx=ctx
        )
        assert "in progress" in retry
        assert _heading_count(target) == 1  # NOT 2 — no duplicate

        # Recovery: once indexing works, a fresh key writes normally.
        monkeypatch.setattr(comp.index_engine, "index_file", original)
        ok = await mem_add(  # type: ignore[arg-type]
            content="durable", title="A", file="x.md", idempotency_key="k2", ctx=ctx
        )
        assert _REPLAY_MARKER not in ok
        assert _heading_count(target) == 2


# ── mem_batch_add ────────────────────────────────────────────────────────────


class TestMemBatchAddIdempotency:
    @pytest.mark.asyncio
    async def test_replay_no_duplicate(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "b.md"
        entries = [
            {"key": "One", "value": "first"},
            {"key": "Two", "value": "second"},
            {"key": "Three", "value": "third"},
        ]

        first = await mem_batch_add(  # type: ignore[arg-type]
            entries=entries, file=str(target), idempotency_key="k1", ctx=ctx
        )
        second = await mem_batch_add(  # type: ignore[arg-type]
            entries=entries, file=str(target), idempotency_key="k1", ctx=ctx
        )

        assert _REPLAY_MARKER not in first
        assert _REPLAY_MARKER in second
        assert second.startswith(first)
        assert _heading_count(target) == 3

    @pytest.mark.asyncio
    async def test_rejected_batch_not_recorded(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "b.md"

        blocked = await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "Clean", "value": "safe content"},
                {"key": "Secret", "value": _SECRET_SAMPLE},
            ],
            file=str(target),
            idempotency_key="k1",
            ctx=ctx,
        )
        assert "Error" in blocked
        assert await comp.storage.idempotency_get("mem_batch_add", "k1") is None

        # Resubmit a clean batch with the same key — it writes.
        ok = await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": "Clean", "value": "safe content"}],
            file=str(target),
            idempotency_key="k1",
            ctx=ctx,
        )
        assert _REPLAY_MARKER not in ok
        assert _heading_count(target) == 1

    @pytest.mark.asyncio
    async def test_skipped_count_preserved(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "b.md"

        result = await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "One", "value": "first"},
                {"key": "Empty", "value": ""},
                {"key": "Two", "value": "second"},
            ],
            file=str(target),
            ctx=ctx,
        )
        assert "Skipped: 1 entries (empty content)" in result
        assert _heading_count(target) == 2

    @pytest.mark.asyncio
    async def test_atomic_single_write_call(self, bm25_only_components, monkeypatch):
        """The batch append composes all blocks and writes them in ONE call
        (issue #1573 all-or-nothing refactor)."""
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        target = mem_dir / "b.md"

        import memtomem.tools.memory_writer as mw

        calls: list[int] = []
        real = mw.append_blocks

        def counting(file_path, blocks):
            calls.append(len(blocks))
            return real(file_path, blocks)

        monkeypatch.setattr(mw, "append_blocks", counting)

        await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": f"E{i}", "value": f"v{i}"} for i in range(5)],
            file=str(target),
            ctx=ctx,
        )
        # Exactly one append_blocks call, carrying all 5 blocks.
        assert calls == [5]


# ── memory_writer byte-identity ──────────────────────────────────────────────


class TestBatchAppendByteIdentity:
    def test_append_blocks_matches_sequential_append_entry(self, tmp_path, monkeypatch):
        """The composed single-write output is byte-for-byte what sequential
        ``append_entry`` calls produced before the refactor."""
        import memtomem.tools.memory_writer as mw

        entries = [
            ("one", "A", ["t1"]),
            ("## already a heading", None, None),
            ("three", "C", None),
        ]

        seq_path = tmp_path / "seq.md"
        for content, title, tags in entries:
            mw.append_entry(seq_path, content, title=title, tags=tags)

        atomic_path = tmp_path / "atomic.md"
        blocks = [mw.format_entry_block(c, title=t, tags=g) for c, t, g in entries]
        mw.append_blocks(atomic_path, blocks)

        assert atomic_path.read_bytes() == seq_path.read_bytes()

    def test_append_blocks_empty_is_noop(self, tmp_path):
        import memtomem.tools.memory_writer as mw

        path = tmp_path / "none.md"
        mw.append_blocks(path, [])
        assert not path.exists()


# ── mem_agent_share ──────────────────────────────────────────────────────────


class TestMemAgentShareIdempotency:
    async def _seed_source(self, comp, ctx):
        await mem_agent_register(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        source = make_chunk(
            "cache strategy knowledge",
            tags=("cache",),
            namespace=f"{AGENT_NAMESPACE_PREFIX}alpha",
        )
        await comp.storage.upsert_chunks([source])
        return source

    @pytest.mark.asyncio
    async def test_replay_no_second_copy_or_link(self, bm25_only_components):
        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        source = await self._seed_source(comp, ctx)

        first = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        second = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )

        assert _REPLAY_MARKER not in first
        assert _REPLAY_MARKER in second
        assert "Shared to namespace" in second

        # Exactly one shared copy and one provenance link — the retry created
        # neither a second copy nor a second link.
        results, _ = await comp.search_pipeline.search(
            query="cache strategy", top_k=10, namespace=SHARED_NAMESPACE
        )
        assert len(results) == 1
        db = comp.storage._get_db()
        assert db.execute("SELECT COUNT(*) FROM chunk_links").fetchone()[0] == 1

    @pytest.mark.asyncio
    async def test_failed_share_not_recorded(self, bm25_only_components):
        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        source = await self._seed_source(comp, ctx)

        # A nonexistent chunk id → "not found"; nothing landed, so the key
        # stays re-runnable and the ledger stays empty.
        missing = "00000000-0000-0000-0000-000000000000"
        out = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=missing, target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        assert "not found" in out
        assert _ledger_count(comp) == 0

        # A real share with the same key then works (not suppressed as replay).
        ok = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        assert _REPLAY_MARKER not in ok

    @pytest.mark.asyncio
    async def test_key_not_forwarded_to_mem_add_ledger(self, bm25_only_components):
        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        source = await self._seed_source(comp, ctx)

        await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        # The share records under its own tool name, not "mem_add" — the inner
        # copy must not consume the key.
        assert await comp.storage.idempotency_get("mem_agent_share", "k1") is not None
        assert await comp.storage.idempotency_get("mem_add", "k1") is None

    @pytest.mark.asyncio
    async def test_precopy_raise_releases_claim(self, bm25_only_components, monkeypatch):
        """Codex 3rd-review regression: a raise in pre-copy prep (e.g. a storage
        error from get_chunk) lands nothing durable, so the claim is released and
        the key stays re-runnable — not wedged pending until TTL."""
        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        source = await self._seed_source(comp, ctx)

        real_get = comp.storage.get_chunk
        state = {"boom": True}

        async def flaky_get(uid):
            if state["boom"]:
                raise RuntimeError("storage boom")
            return await real_get(uid)

        monkeypatch.setattr(comp.storage, "get_chunk", flaky_get)

        err = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        assert "Error" in err
        assert _ledger_count(comp) == 0  # claim released — nothing landed

        # The key is free again: a real share with the same key works.
        state["boom"] = False
        ok = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        assert _REPLAY_MARKER not in ok

    @pytest.mark.asyncio
    async def test_index_failure_after_copy_no_duplicate_on_retry(
        self, bm25_only_components, monkeypatch
    ):
        """Codex re-review regression: a raise from _mem_add_core (after its copy
        append lands) must not release the share claim — a retry must not
        duplicate the copy."""
        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        source = await self._seed_source(comp, ctx)

        async def flaky(*a, **k):
            raise RuntimeError("index boom")

        monkeypatch.setattr(comp.index_engine, "index_file", flaky)

        err = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        assert "Error" in err
        landed = _total_headings(mem_dir)  # the copy append is durable
        assert landed >= 1

        retry = await mem_agent_share(  # type: ignore[arg-type]
            chunk_id=str(source.id), target=SHARED_NAMESPACE, idempotency_key="k1", ctx=ctx
        )
        assert "in progress" in retry
        assert _total_headings(mem_dir) == landed  # no duplicate copy
