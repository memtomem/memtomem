"""sqlite-vec's KNN ``k`` cap and the adaptive over-fetch that has to respect it.

``dense_search`` escalates its inner KNN ``LIMIT`` when a namespace / scope
filter rejects the nearest candidates. The final escalation used to be the full
``chunks_vec`` row count, which sqlite-vec 0.1.9 refuses above
``VEC_MAX_KNN_K``; the resulting ``OperationalError`` propagated all the way to
the search pipeline, which treats any exception as "dense search unavailable"
and drops the whole leg — including candidates an earlier, smaller attempt had
already returned (#2119).
"""

from __future__ import annotations

import sqlite3

import pytest

from helpers import make_chunk as _make_chunk
from memtomem.errors import StorageError
from memtomem.storage import sqlite_backend as _backend
from memtomem.storage.base import NamespaceFilter

NEAR = [0.9] * 1024
FAR = [-0.9] * 1024


# Literal, not the module constant: these injections must keep working against
# a build that has no constant at all, so the pins below fail on BEHAVIOR rather
# than on a missing symbol.
CAP_ERROR = "k value in knn query too large, provided {k} and the limit is 4096"
UNRELATED_ERROR = "database disk image is malformed"


class _RecordingDB:
    """Delegating wrapper that records every KNN ``k`` and can inject a failure."""

    def __init__(
        self,
        inner,
        ks: list[int],
        fail_on_call: int | None = None,
        message: str = CAP_ERROR,
    ):
        self._inner = inner
        self._ks = ks
        self._fail_on_call = fail_on_call
        self._message = message

    def execute(self, sql, params=None):
        if params is not None and "embedding MATCH" in sql:
            self._ks.append(params[1])
            if self._fail_on_call is not None and len(self._ks) == self._fail_on_call:
                raise sqlite3.OperationalError(self._message.format(k=params[1]))
        return self._inner.execute(sql) if params is None else self._inner.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._inner, name)


def _record(
    storage,
    ks: list[int],
    fail_on_call: int | None = None,
    message: str = CAP_ERROR,
) -> None:
    inner = storage._get_read_db()
    storage._get_read_db = lambda: _RecordingDB(  # type: ignore[method-assign]
        inner, ks, fail_on_call, message
    )


async def _seed_escalating_store(storage, *, matching_near: int) -> None:
    """100 near decoys in ``other`` + a few ``wanted`` rows the filter must reach.

    The first attempt (k = max(top_k*5, 100)) sees only the decoys, so the
    post-filter result is short of ``top_k`` and the escalation fires.
    """
    chunks = [
        _make_chunk(f"decoy {i}", source=f"d{i}.md", namespace="other", embedding=NEAR)
        for i in range(200)
    ]
    chunks += [
        _make_chunk(f"near wanted {i}", source=f"nw{i}.md", namespace="wanted", embedding=NEAR)
        for i in range(matching_near)
    ]
    chunks += [
        _make_chunk(f"far wanted {i}", source=f"fw{i}.md", namespace="wanted", embedding=FAR)
        for i in range(6)
    ]
    await storage.upsert_chunks(chunks)


class TestKnnCapClamp:
    @pytest.mark.asyncio
    async def test_every_attempt_stays_within_the_engine_cap(self, storage, monkeypatch):
        """No inner K may exceed ``VEC_MAX_KNN_K``, even when the table is larger.

        Mutation that bites: restoring the unclamped ``total_vec_rows`` final
        attempt — with the cap lowered here, that value (206) sails past it.
        """
        monkeypatch.setattr(_backend, "VEC_MAX_KNN_K", 120)
        await _seed_escalating_store(storage, matching_near=0)

        ks: list[int] = []
        _record(storage, ks)
        results = await storage.dense_search(
            NEAR, top_k=5, namespace_filter=NamespaceFilter(namespaces=("wanted",))
        )

        assert ks, "dense_search must have issued at least one KNN query"
        assert max(ks) <= 120, f"inner K exceeded the engine cap: {ks}"
        # The clamp must not turn into a wasted round-trip storm either.
        assert len(ks) == len(set(ks)), f"duplicate inner K values retried: {ks}"
        # Honest about the trade: 200 nearer decoys outrank every ``wanted`` row,
        # and the cap forbids looking past them, so the filtered result really is
        # empty here. What matters is that it is an empty RESULT, not an
        # exception — the caller keeps its BM25 leg and its other dense hits
        # instead of losing dense retrieval wholesale.
        assert results == []

    @pytest.mark.asyncio
    async def test_escalation_reaches_rows_the_first_attempt_filtered_away(self, storage):
        """The over-fetch still works — the clamp must not cost recall."""
        await _seed_escalating_store(storage, matching_near=0)

        results = await storage.dense_search(
            NEAR, top_k=5, namespace_filter=NamespaceFilter(namespaces=("wanted",))
        )

        assert len(results) == 5
        assert all(r.chunk.metadata.namespace == "wanted" for r in results)


class TestEscalationFailureDegrades:
    @pytest.mark.asyncio
    async def test_failed_escalation_keeps_the_earlier_candidates(self, storage):
        """A later attempt blowing up must not discard what the first one found.

        Before #2119 the exception propagated and the pipeline logged "Dense
        search unavailable", so a query that HAD two valid dense hits returned
        zero of them.
        """
        await _seed_escalating_store(storage, matching_near=2)

        ks: list[int] = []
        _record(storage, ks, fail_on_call=2)
        results = await storage.dense_search(
            NEAR, top_k=5, namespace_filter=NamespaceFilter(namespaces=("wanted",))
        )

        assert len(ks) >= 2, f"expected an escalation attempt, got {ks}"
        assert len(results) == 2, "must degrade to the first attempt's candidates"
        assert all(r.chunk.metadata.namespace == "wanted" for r in results)

    @pytest.mark.asyncio
    async def test_unrelated_sqlite_failure_is_not_absorbed(self, storage):
        """Only the k-cap error may be answered with stale candidates.

        A corrupt page or a failed read is not a recall problem. Degrading on it
        would hand back a partial result set and let a broken store look healthy
        for as long as the first attempt keeps succeeding.
        """
        await _seed_escalating_store(storage, matching_near=2)

        ks: list[int] = []
        _record(storage, ks, fail_on_call=2, message=UNRELATED_ERROR)
        with pytest.raises(sqlite3.OperationalError, match="malformed"):
            await storage.dense_search(
                NEAR, top_k=5, namespace_filter=NamespaceFilter(namespaces=("wanted",))
            )

    @pytest.mark.asyncio
    async def test_first_attempt_failure_still_raises(self, storage):
        """With nothing in hand there is nothing to degrade to — surface it."""
        await _seed_escalating_store(storage, matching_near=0)

        ks: list[int] = []
        _record(storage, ks, fail_on_call=1)
        with pytest.raises(sqlite3.OperationalError):
            await storage.dense_search(
                NEAR, top_k=5, namespace_filter=NamespaceFilter(namespaces=("wanted",))
            )


class TestExhaustiveAboveCap:
    @pytest.mark.asyncio
    async def test_refuses_instead_of_pretending(self, storage, monkeypatch):
        """Exhaustive determinism is unavailable above the cap — say so.

        A clamped "exhaustive" pass would be an ordinary truncated KNN wearing
        the determinism label, and a replay diff would silently compare against
        a partial scan.
        """
        monkeypatch.setattr(_backend, "VEC_MAX_KNN_K", 50)
        await _seed_escalating_store(storage, matching_near=0)

        with pytest.raises(StorageError) as excinfo:
            await storage.dense_search(NEAR, top_k=5, exhaustive=True)

        message = str(excinfo.value)
        assert "50" in message, f"must name the cap it hit: {message!r}"
        assert "206" in message, f"must name the store size: {message!r}"
        # Remediation, not just a diagnosis.
        assert "non-exhaustive" in message, f"must point at what still works: {message!r}"

    @pytest.mark.asyncio
    async def test_still_exhaustive_below_the_cap(self, storage, monkeypatch):
        """The #1802 guarantee is untouched for stores the engine can scan."""
        monkeypatch.setattr(_backend, "VEC_MAX_KNN_K", 4096)
        chunks = [
            _make_chunk(f"body {i}", source=f"e{i}.md", embedding=[0.2] * 1024) for i in range(120)
        ]
        await storage.upsert_chunks(chunks)

        ks: list[int] = []
        _record(storage, ks)
        results = await storage.dense_search([0.2] * 1024, top_k=5, exhaustive=True)

        assert ks == [120], f"exhaustive must scan every embedding once, got {ks}"
        assert [r.chunk.id for r in results] == sorted((c.id for c in chunks), key=str)[:5]


class TestEmptyVectorTable:
    @pytest.mark.asyncio
    async def test_no_query_is_issued_when_there_is_nothing_to_search(self, storage):
        """An empty ``chunks_vec`` must cost zero KNN round-trips, not three."""
        ks: list[int] = []
        _record(storage, ks)
        results = await storage.dense_search(NEAR, top_k=5)

        assert results == []
        assert ks == [], f"empty table must not be probed at all, got {ks}"

    @pytest.mark.asyncio
    async def test_wrong_width_still_raises_on_an_empty_table(self, storage):
        """The early return must not swallow a dimension mismatch.

        ``chunks_vec`` is empty-but-live after ``reset_embedding_meta`` while
        the chunk rows wait for a re-index. Answering a wrong-width embedder
        with ``[]`` there reads as "no similar chunks" to dedup scanning and the
        similar-chunks endpoint, and leaves an ordinary search with no
        ``dense_error`` to report.
        """
        ks: list[int] = []
        _record(storage, ks)
        with pytest.raises(ValueError, match="dimension mismatch"):
            await storage.dense_search([0.1] * 512, top_k=5)

        assert ks == [], "the mismatch must be caught before any KNN query"


# ---- data_version memo for the chunks_vec row count -------------------------
#
# ``count(*)`` on a vec0 vtab is a full scan (no stats), so ``dense_search``
# memoizes it per read connection behind ``PRAGMA data_version``. The memo
# must never go stale-low: ``knn_ceiling = min(total_vec_rows, cap)`` clips
# the KNN K, so a stale-low count silently drops matching results.


class _CountingDB:
    """Delegates to a real connection, counting chunks_vec COUNT queries."""

    def __init__(self, inner):
        self._inner = inner
        self.count_queries = 0

    def execute(self, sql, *args):
        if "count(*) FROM chunks_vec" in sql:
            self.count_queries += 1
        return self._inner.execute(sql, *args)

    def __getattr__(self, name):
        return getattr(self._inner, name)


async def test_vec_row_count_memoized_until_another_connection_commits(storage):
    chunk = _make_chunk(content="memo seed", embedding=NEAR)
    await storage.upsert_chunks([chunk])

    read_db = storage._get_read_db()
    counting = _CountingDB(read_db)

    assert storage._cached_vec_row_count(counting) == 1
    assert storage._cached_vec_row_count(counting) == 1
    assert counting.count_queries == 1  # second call served from the memo

    # A commit on the *writer* connection bumps the reader's data_version.
    await storage.upsert_chunks([_make_chunk(content="second", source="b.md", embedding=FAR)])
    assert storage._cached_vec_row_count(counting) == 2
    assert counting.count_queries == 2


async def test_dense_search_sees_rows_committed_after_the_memo_warmed(storage):
    """The correctness pin: a permanently cached count would clamp
    ``knn_ceiling`` at the primed value and silently drop newer rows."""
    await storage.upsert_chunks([_make_chunk(content="first", source="s0.md", embedding=NEAR)])
    # Prime the memo on every pool connection — dense_search round-robins
    # over the read pool, so a single search would leave the others cold and
    # a later search could dodge a stale memo by landing on a cold one.
    for _ in range(max(1, len(storage._read_pool))):
        primed = await storage.dense_search(NEAR, top_k=5)
        assert len(primed) == 1

    await storage.upsert_chunks(
        [_make_chunk(content=f"later {i}", source=f"s{i}.md", embedding=NEAR) for i in range(1, 5)]
    )
    results = await storage.dense_search(NEAR, top_k=5)
    assert len(results) == 5


async def test_vec_row_count_not_memoized_on_the_writer_connection(storage):
    """The writer's own commits do not bump its own data_version, so the
    memo must be bypassed on the writer-connection fallback path."""
    await storage.upsert_chunks([_make_chunk(content="only", embedding=NEAR)])
    writer = storage._get_db()
    assert storage._cached_vec_row_count(writer) == 1
    await storage.upsert_chunks([_make_chunk(content="more", source="b.md", embedding=FAR)])
    assert storage._cached_vec_row_count(writer) == 2
