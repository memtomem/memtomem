"""BM25, dense vector search, recall, and source-browsing queries for SqliteBackend."""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import AsyncIterator, Sequence
from uuid import UUID

from memtomem.errors import (
    ExhaustiveDenseSearchLimitError,
    QueryEmbeddingDimensionError,
)
from memtomem.models import (
    Chunk,
    NamespaceFilter,
    ScopeFilter,
    SearchResult,
)
from memtomem.storage import fts_tokenizer as _fts
from memtomem.storage.base import (
    ChunkAuditRow,
    ContextWindowRows,
    SearchMetadataFilter,
    parse_tag_filter,
)
from memtomem.storage.sqlite_helpers import (
    escape_like,
    match_source_filter_value,
    namespace_sql,
    placeholders,
    serialize_f32,
    utc_bound,
)
from memtomem.storage.sqlite_scope import scope_context_sql, scope_sort_priority_case
from memtomem.storage.sqlite_visibility import (
    NOT_WITHHELD_CHUNKS,
    VISIBLE_SOURCE_C,
    VISIBLE_SOURCE_CHUNKS,
    VISIBLE_SOURCE_PATH,
    neighbor_visibility_sql,
    visible_path_sql,
)

logger = logging.getLogger(__name__)

# Hard ceiling on the ``k`` of a sqlite-vec KNN query (``embedding MATCH ?``
# with an inner ``LIMIT``). sqlite-vec 0.1.9 rejects anything larger with
# ``k value in knn query too large, provided <k> and the limit is 4096``; it is
# a compile-time constant inside vec0, not a tunable. Every inner LIMIT handed
# to a MATCH must be clamped to it — an unclamped "scan the whole table"
# attempt raises, and the raise costs the ENTIRE dense leg, including the
# candidates a smaller earlier attempt already retrieved (#2119).
VEC_MAX_KNN_K = 4096

# Substring identifying sqlite-vec's own refusal of an oversized KNN ``k``
# (full text: ``k value in knn query too large, provided <k> and the limit is
# <cap>``). The clamp above should make it unreachable; it stays as the ONLY
# error we are willing to absorb by degrading to a smaller attempt's rows.
# Everything else out of SQLite — corruption, I/O, interrupt, schema — must
# propagate, or a failing store silently serves partial results forever.
_VEC_K_TOO_LARGE = "k value in knn query too large"

# BM25 can exclude held sources before LIMIT. Dense KNN must apply this
# separately after the bounded vector candidate query (see dense_search).
_VISIBLE_SOURCE_C = f"AND {VISIBLE_SOURCE_C}"
_VISIBLE_SOURCE = VISIBLE_SOURCE_CHUNKS

#: The two ``chunks_fts`` columns, as fts5 column-filter names (#2224).
#: ``content`` is what a search is *for*; ``source_file`` is a fallback so
#: typing a filename still finds that file, never a competitor to prose.
_FTS_CONTENT_COLUMN = "content"
_FTS_SOURCE_COLUMN = "source_file"


def _chunk_ids_sql(column_alias: str = "") -> str:
    """SQL fragment restricting rows to an explicit chunk-id set.

    Uses ``json_each`` over a single bound JSON array rather than an
    ``IN (?,?,…)`` list, for three reasons: ``placeholders(0)`` raises, so
    an IN-list cannot express the empty set at all; a large provenance
    set would otherwise blow past SQLite's bound-variable limit; and one
    parameter is cheaper to bind than thousands. Mirrors the ``json_each``
    idiom already used for the tag filter.
    """
    return f"{column_alias}id IN (SELECT value FROM json_each(?))"


def _chunk_ids_param(chunk_ids: Sequence[UUID]) -> str:
    return json.dumps([str(cid) for cid in chunk_ids])


def _tags_any_sql(column_alias: str = "") -> str:
    """SQL fragment matching rows carrying ANY tag from a bound JSON array.

    Two guards, both load-bearing (#2191):

    * ``CASE WHEN json_valid(...)`` rather than ``json_valid(...) AND ...``.
      SQLite does not promise left-to-right short-circuit evaluation of
      ``AND`` terms once the planner has rewritten the query, and
      ``json_each`` on malformed input raises rather than yielding no rows —
      a single legacy corrupt ``tags`` value would turn every search into an
      ``OperationalError``. ``CASE`` makes the laziness explicit.
    * ``json_type(...) = 'array'``. ``json_valid`` accepts a bare string or
      object, and ``json_each`` would then iterate its characters or its
      values — matching tags that ``_row_to_chunk`` does not see. Both sides
      degrade a non-array shape to "no tags" instead.

    The requested tags ride a single bound JSON array (as ``_chunk_ids_sql``
    does) so a large tag set cannot exceed SQLite's bound-variable limit.
    """
    return (
        f"CASE WHEN json_valid({column_alias}tags) "
        f"THEN json_type({column_alias}tags) = 'array' AND EXISTS ("
        f"SELECT 1 FROM json_each({column_alias}tags) AS ct "
        f"WHERE ct.value IN (SELECT wt.value FROM json_each(?) AS wt)) "
        f"ELSE 0 END"
    )


def _tags_any_param(tags: Sequence[str]) -> str:
    return json.dumps(list(tags))


def _metadata_filter_sql(
    metadata_filter: SearchMetadataFilter | None,
    *,
    column_alias: str = "",
) -> tuple[str, list[object]]:
    if metadata_filter is None:
        return "", []
    conditions: list[str] = []
    params: list[object] = []
    if metadata_filter.tags_any:
        # Callers that cannot afford this in SQL strip the field first —
        # ``dense_search`` does, because a sparse tag would escalate its
        # adaptive KNN over-fetch into a full vector scan (#2184/#2191).
        conditions.append(f"({_tags_any_sql(column_alias)})")
        params.append(_tags_any_param(metadata_filter.tags_any))
    if metadata_filter.source_exact:
        # ``json_each`` over one bound JSON array, not ``IN (?,?,…)`` — the
        # rescue leg can push a whole boost-source set through here (#2184)
        # and an IN-list would blow past SQLite's bound-variable limit on a
        # large set. Mirrors ``_chunk_ids_sql`` above.
        from memtomem.storage import sqlite_backend as _sb

        values = [_sb.norm_path(Path(value)) for value in metadata_filter.source_exact]
        conditions.append(f"{column_alias}source_file IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(values))
    if metadata_filter.chunk_types:
        conditions.append(
            f"{column_alias}chunk_type IN ({placeholders(len(metadata_filter.chunk_types))})"
        )
        params.extend(metadata_filter.chunk_types)
    # ``chunks.created_at`` is stored as a UTC ISO-8601 string and compared
    # lexically, so the bound is normalized here rather than trusted from the
    # caller — the web route did normalize, every other caller did not.
    if metadata_filter.created_from is not None:
        conditions.append(f"{column_alias}created_at >= ?")
        params.append(utc_bound(metadata_filter.created_from))
    if metadata_filter.created_before is not None:
        conditions.append(f"{column_alias}created_at < ?")
        params.append(utc_bound(metadata_filter.created_before))
    return " AND ".join(conditions), params


def _restrict_to_column(column: str, body: str) -> str:
    """Wrap an already-tokenized expression in one ``chunks_fts`` column filter.

    fts5 spells this ``{col} : (expr)``. The braces are what make it safe to
    build by concatenation: a *bare* ``col : expr`` prefix would be re-parsed
    if ``expr`` itself began with something column-shaped, while a braced
    column list is closed before the expression starts. ``column`` is never
    user input — it is one of the two module constants above — so nothing
    interpolated here comes from the caller.

    ``body`` is whatever ``tokenize_for_fts`` produced, unchanged: quoted
    phrases, prefix wildcards and the ``OR`` fallback all survive being
    parenthesised, which is exactly why the escaping stays that function's job
    and is not re-implemented here.
    """
    if column not in (_FTS_CONTENT_COLUMN, _FTS_SOURCE_COLUMN):  # pragma: no cover - guard
        raise ValueError(f"not a chunks_fts column: {column!r}")
    return "{" + column + "} : (" + body + ")"


def _column_match(column: str, query: str, *, use_or: bool) -> str:
    """Tokenize *query* and restrict it to one column — one call, one analysis.

    The search path builds the bodies itself (it needs each form once, not once
    per column); this is the convenience spelling the tests and any future
    single-shot caller use.
    """
    return _restrict_to_column(column, _fts.tokenize_for_fts(query, for_query=True, use_or=use_or))


class SearchOpsMixin:
    """Search, recall, context-window, and audit read operations for SqliteBackend."""

    async def bm25_search(
        self,
        query: str,
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        source_filter: str | None = None,
    ) -> list[SearchResult]:
        async with self._lease_read_db() as db:
            ns_clause = ""
            ns_params: list = []
            if namespace_filter:
                frag, ns_params = namespace_sql(namespace_filter)
                if frag:
                    ns_clause = f"AND c.{frag}"

            from memtomem.storage import sqlite_backend as _sb

            # ADR-0011 §6: scope-context filter is ALWAYS appended even
            # when the caller does not pass an explicit scope_filter,
            # so cross-project leak is impossible by construction.
            scope_frag, scope_params = scope_context_sql(
                scope_filter, project_context_root, column_alias="c."
            )
            scope_clause = f"AND ({scope_frag})"
            tie_break = scope_sort_priority_case("c.")
            metadata_frag, metadata_params = _sb._metadata_filter_sql(
                metadata_filter, column_alias="c."
            )
            metadata_clause = f"AND ({metadata_frag})" if metadata_frag else ""
            source_clause = (
                "AND memtomem_source_match(?, c.source_file) = 1" if source_filter else ""
            )
            source_params = [source_filter] if source_filter else []

            # Content first, path only as a fallback (#2224). ``chunks_fts`` is
            # ``fts5(content, source_file)`` and an unqualified MATCH searches
            # both, so a term appearing only in a chunk's *path* used to
            # retrieve it and — every column weighted 1.0 in the default
            # ``rank`` — score as a relevance signal. ``source_file`` is stored
            # raw, so unicode61 splits it into components and every directory
            # and filename word became a searchable term: on a real
            # 6,995-chunk store, 71 of the top 100 hits for ``plans`` were
            # chunks about something else, surfaced because they live under
            # ``.claude/plans/``.
            #
            # Column-qualifying the MATCH is what makes path a non-signal, and
            # zero-weighting it is not: RRF fuses on a result's *ordinal rank*
            # (``search/fusion.py`` — ``w / (k + rank)``), so a path-only row
            # scored 0.0 still occupies a BM25 slot and still contributes
            # positive fused relevance downstream. It has to not be retrieved.
            #
            # The fallback keeps filename lookup working — typing a path word
            # still finds that file's chunks — but only once nothing in any
            # chunk's *content* matches, so a path can never outrank prose.
            # No weights are needed either way: under a column-qualified MATCH
            # ``bm25(chunks_fts, 1.0, 0.0)`` and the bare ``rank`` score
            # identically, because the excluded column contributes nothing to
            # a query that never touched it.
            # AND first (FTS5's default), then OR, within each column in turn.
            # Written as one flat loop rather than a helper because
            # ``test_mixin_commit_guard`` resolves the SQL keyword of every
            # ``db.execute`` from the literal its first argument is bound to,
            # and a closure hides that binding — which is the exact shape #2207
            # added the "unknowable statement" rule for.
            #
            # The OR pass is skipped when it would repeat the AND one, decided
            # by comparing the two *tokenized* bodies rather than by looking for
            # a space in the raw query. Whitespace is the wrong test under a
            # morphological tokenizer: kiwipiepy expands one space-free Korean
            # word into several terms, so a query like ``했습니다`` has an OR
            # form that differs from its AND form and was losing the fallback
            # entirely.
            #
            # The bodies are tokenized once each and then wrapped per column —
            # two analyses per search at most, whatever the ladder does. Asking
            # for the four ``(column, form)`` expressions directly would analyse
            # the query once per expression, which for a morphological backend
            # is the expensive half of the call.
            and_body = _fts.tokenize_for_fts(query, for_query=True)
            or_body = _fts.tokenize_for_fts(query, for_query=True, use_or=True)
            bodies = [and_body] if or_body == and_body else [and_body, or_body]

            def _query(conn: sqlite3.Connection) -> list:
                # ADR-0011 §6 + PR-D review #2: filter must run *inside* the
                # FTS candidate selection, not after a post-LIMIT join. With
                # the previous shape (``LIMIT k`` on chunks_fts MATCH, then
                # filter), the global top-k could come entirely from another
                # project's chunks; the current project's matches sat just
                # below the cutoff and were dropped. Joining chunks first
                # and applying the namespace/scope predicates inside the
                # same query lets SQLite/FTS5 lazy-iterate matches and stop
                # once ``top_k`` filter-passing rows accumulate.
                #
                # ``c.*`` carries the full chunks-row layout into
                # ``_row_to_chunk`` so all defensive guards (overlap,
                # importance, validity) activate. Score sits at the
                # trailing position after the chunk columns — see
                # ``_chunks_table_column_count`` consumer below.
                # Trailing ``c.id`` is the unique final tiebreak (#516): equal
                # ``fts.rank`` rows within the same scope priority would otherwise
                # take arbitrary SQLite order, so repeated queries / reopened
                # connections could reorder them and destabilize replay diffs.
                sql = f"""SELECT c.*, fts.rank
                       FROM chunks_fts fts
                       JOIN chunks c ON c.rowid = fts.rowid
                       WHERE chunks_fts MATCH ? {ns_clause} {scope_clause} {metadata_clause} {source_clause} {_VISIBLE_SOURCE_C}
                       ORDER BY fts.rank, {tie_break}, c.id
                       LIMIT ?"""
                rows: list = []
                for column in (_FTS_CONTENT_COLUMN, _FTS_SOURCE_COLUMN):
                    for body in bodies:
                        rows = conn.execute(
                            sql,
                            [_restrict_to_column(column, body)]
                            + ns_params
                            + scope_params
                            + metadata_params
                            + source_params
                            + [top_k],
                        ).fetchall()
                        if rows:
                            break
                    if rows:
                        break
                return rows

            rows = await self._run_read_in_thread(db, _query)

        return [
            SearchResult(
                chunk=self._row_to_chunk(row[:-1]),
                score=abs(row[-1]),
                rank=rank_idx + 1,
                source="bm25",
            )
            for rank_idx, row in enumerate(rows)
        ]

    @property
    def dense_enabled(self) -> bool:
        """Whether this store can answer a dense search at all.

        False in bm25-only mode (dimension 0), where ``dense_search``
        legitimately returns ``[]``. Callers that report a lookup's outcome to
        a human need this to tell "configured without vectors" apart from
        "searched and found nothing" — the two are the same empty list.
        """
        return self._has_vec_table

    async def dense_search(
        self,
        embedding: list[float],
        top_k: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        source_filter: str | None = None,
        *,
        exhaustive: bool = False,
    ) -> list[SearchResult]:
        # bm25-only mode (dimension=0) — no chunks_vec table to query. Return
        # early instead of raising OperationalError that the search pipeline
        # would log as a misleading "Dense search unavailable" warning.
        if not self._has_vec_table:
            return []

        async with self._lease_read_db() as db:
            # Validate the query width HERE, not by sniffing sqlite-vec's error on
            # the first MATCH. ``chunks_vec`` is legitimately empty while chunks
            # survive — ``reset_embedding_meta`` drops and recreates the vector
            # table pending a re-index — and in that window an early ``return []``
            # would turn a wrong-width embedder into "no similar chunks" for
            # ``mem_dedup_scan`` and the similar-chunks endpoint, and into a search
            # with no ``dense_error`` at all (Codex review, #2120). Kept below the
            # BM25-only guard above: dimension-0 stores accept any width by design.
            if len(embedding) != self._dimension:
                raise QueryEmbeddingDimensionError.vetted(
                    f"Embedding dimension mismatch: query has {len(embedding)}d "
                    f"but DB expects {self._dimension}d. "
                    f"Check MEMTOMEM_EMBEDDING__MODEL / "
                    f"MEMTOMEM_EMBEDDING__DIMENSION."
                )

            ns_clause = ""
            ns_params: list = []
            if namespace_filter:
                frag, ns_params = namespace_sql(namespace_filter)
                if frag:
                    ns_clause = f"AND c.{frag}"

            # ADR-0011 §6: always-on scope-context fragment.
            scope_frag, scope_params = scope_context_sql(
                scope_filter, project_context_root, column_alias="c."
            )
            scope_clause = f"AND ({scope_frag})"
            tie_break = scope_sort_priority_case("c.")
            # ``tags_any`` and the public ``source_filter`` are honored, but never
            # in SQL here (#2191/#2221). Either selective predicate in the outer
            # join below would leave the result short of ``top_k``, so the adaptive
            # loop would keep retrying with a larger inner K and eventually scan
            # the full vector table. Filter the bounded KNN result in Python
            # instead: the contract still holds (callers get only matching rows),
            # at the cost of not seeing a match outside the KNN pool.
            required_tags = set(metadata_filter.tags_any) if metadata_filter is not None else set()
            required_source_filter = source_filter
            # The hold gate belongs in the outer SQL result. It preserves the
            # adaptive KNN retry when hidden rows fill the inner pool, while only
            # visible rows are decoded into Python Chunk objects.
            sql_metadata_filter = (
                dataclass_replace(metadata_filter, tags_any=())
                if metadata_filter is not None and metadata_filter.tags_any
                else metadata_filter
            )
            from memtomem.storage import sqlite_backend as _sb

            metadata_frag, metadata_params = _sb._metadata_filter_sql(
                sql_metadata_filter, column_alias="c."
            )
            metadata_clause = f"AND ({metadata_frag})" if metadata_frag else ""

            vec_max_knn_k = _sb.VEC_MAX_KNN_K

            def _query(conn: sqlite3.Connection) -> list:
                # ADR-0011 §6 + PR-D review (round 2): sqlite-vec's
                # ``embedding MATCH ?`` uses the inner ``LIMIT`` as the KNN
                # ``K`` — the namespace / scope filter must run outside that
                # subquery, so the inner K decides how many candidates the
                # outer filter is allowed to see. A *fixed* over-fetch (e.g.
                # ``top_k * 5``) silently drops valid scoped matches when
                # cross-project / cross-namespace skew exceeds that factor.
                #
                # Adaptive over-fetch: try a small K first (fast for the
                # common case where filter passes nearly everything), and if
                # the post-filter result is short of ``top_k`` AND the inner
                # K did not exhaust ``chunks_vec`` (i.e. there could still be
                # filter-passing matches beyond the cutoff), retry with a
                # larger K. Cap retries at the table size so the worst case
                # is "scan every embedding once," which matches the semantics
                # the caller would expect from "find me the nearest scoped
                # row."
                sql = f"""SELECT c.*, sub.distance
                       FROM (
                           SELECT rowid, distance
                           FROM chunks_vec
                           WHERE embedding MATCH ?
                           ORDER BY distance
                           LIMIT ?
                       ) sub
                       JOIN chunks c ON c.rowid = sub.rowid
                       WHERE {VISIBLE_SOURCE_C} {ns_clause} {scope_clause} {metadata_clause}
                       ORDER BY sub.distance, {tie_break}, c.id
                       LIMIT ?"""
                # Trailing ``c.id`` gives the outer ordering a unique final tiebreak
                # (#516). Note this only stabilizes rows the inner KNN actually
                # returned; sqlite-vec 0.1.9 prunes to the inner ``LIMIT`` with an
                # unstable distance-only sort, so equal-distance rows straddling that
                # inner cutoff are a separate concern handled by the exhaustive
                # evaluation path (see ``dense_search`` replay mode).

                # Total embedding rows — the upper bound for a meaningful KNN K.
                # ``chunks_vec`` is a vec0 *virtual* table, so ``COUNT(*)`` is a
                # full vtab scan on every call, not a stats lookup — memoized per
                # connection behind ``PRAGMA data_version`` instead.
                total_vec_rows = self._cached_vec_row_count(conn)

                # Nothing to search. Returning early also keeps the schedule below
                # monotonic — with a zero row count the clamp would otherwise produce
                # ``[100, 1000, 1]`` and spend three round-trips proving the table is
                # empty (Codex review, #2120).
                if not total_vec_rows:
                    return []

                # The largest inner K this engine will actually accept: never more
                # embeddings than exist, and never more than sqlite-vec's cap (#2119).
                knn_ceiling = min(total_vec_rows, vec_max_knn_k)

                # ``exhaustive`` (deterministic evaluation mode, #1802): sqlite-vec
                # 0.1.9 prunes to the inner ``LIMIT`` with an unstable distance-only
                # sort, so equal-distance rows straddling an adaptive cutoff are
                # selected nondeterministically — a replay diff cannot tolerate that.
                # Scanning every embedding once removes the inner cutoff entirely, so
                # the outer stable ``ORDER BY sub.distance, ..., c.id`` fully
                # determines selection. Costs one full-table KNN pass; acceptable
                # because replay runs off the interactive hot path.
                #
                # That guarantee is simply not purchasable above ``VEC_MAX_KNN_K``:
                # the cutoff cannot be removed, so a clamped "exhaustive" pass would
                # be an ordinary truncated KNN wearing the determinism label, and a
                # replay diff would compare against a silently partial scan. Refuse
                # instead — fail-closed beats a result that lies about its own shape.
                if exhaustive and total_vec_rows > vec_max_knn_k:
                    raise ExhaustiveDenseSearchLimitError.vetted(
                        f"Exhaustive dense search needs to scan all {total_vec_rows} embeddings, "
                        f"but sqlite-vec caps a KNN query at {vec_max_knn_k}. Deterministic "
                        f"replay/evaluation is unavailable on a store this large — run it "
                        f"against a smaller evaluation store, or use ordinary "
                        f"(non-exhaustive) search, whose adaptive over-fetch is clamped and "
                        f"therefore still returns results here."
                    )

                # Schedule: start at the previous fixed factor for the
                # common case, then jump to "essentially unbounded" before
                # giving up. Stop early when an attempt either returned
                # ``top_k`` rows OR already reached the ceiling (no larger K is
                # available, so retrying cannot find more). Clamped and de-duplicated
                # so a large ``top_k`` cannot spend three identical round-trips.
                attempts: list[int] = []
                for raw_k in (
                    [total_vec_rows or 1]
                    if exhaustive
                    else [
                        max(top_k * 5, 100),
                        max(top_k * 50, 1000),
                        total_vec_rows,
                    ]
                ):
                    clamped = max(1, min(raw_k, knn_ceiling))
                    if clamped not in attempts:
                        attempts.append(clamped)

                rows: list = []
                for inner_k in attempts:
                    try:
                        # With a Python-side tag/source filter the outer ``LIMIT``
                        # must not be ``top_k``: truncating here would drop a matching
                        # row sitting at rank ``top_k + 1`` of the unfiltered order.
                        # Take the whole bounded candidate set and truncate after
                        # filtering. ``inner_k`` still bounds it, and the escalation
                        # check below still counts *unfiltered* rows, so a selective
                        # filter cannot drive another KNN attempt.
                        outer_limit = inner_k if required_tags or required_source_filter else top_k
                        rows = conn.execute(
                            sql,
                            [serialize_f32(embedding), inner_k]
                            + ns_params
                            + scope_params
                            + metadata_params
                            + [outer_limit],
                        ).fetchall()
                    except sqlite3.OperationalError as exc:
                        if "Dimension mismatch" in str(exc):
                            raise QueryEmbeddingDimensionError.vetted(
                                f"Embedding dimension mismatch: query has {len(embedding)}d "
                                f"but DB expects {self._dimension}d. "
                                f"Check MEMTOMEM_EMBEDDING__MODEL / "
                                f"MEMTOMEM_EMBEDDING__DIMENSION."
                            ) from exc
                        # An escalation attempt that hits the engine's k cap must not
                        # throw away what a smaller attempt already returned: the caller
                        # treats ANY exception here as "dense search unavailable" and
                        # drops the whole leg (#2119). Degrade to the candidates in hand
                        # — but ONLY for that one recognized error. A corrupt page or a
                        # failed read is not a recall problem, and answering it with
                        # stale partial rows would hide a broken store (Codex review).
                        if rows and _VEC_K_TOO_LARGE in str(exc):
                            logger.warning(
                                "Dense KNN escalation to k=%d failed (%s); keeping the %d "
                                "candidate(s) from the previous attempt",
                                inner_k,
                                exc,
                                len(rows),
                            )
                            break
                        raise
                    # Done if we hit the requested top_k OR if this attempt already
                    # used the largest K the engine will accept (cannot do better by
                    # retrying).
                    # Held rows are already excluded by SQL. Tag and source filters
                    # remain bounded by the unfiltered candidate count.
                    if len(rows) >= top_k or inner_k >= knn_ceiling:
                        break
                return rows

            rows = await self._run_read_in_thread(db, _query)

        # Selective tag/source filtering happens here rather than in the SQL
        # above: the escalation loop must judge attempts on the unfiltered row
        # count, or a rare match reintroduces the full-scan escalation this
        # bounded pass avoids. ``top_k`` is applied after the filters and ranks
        # are assigned last so the returned list stays 1..n contiguous.
        chunks = [(self._row_to_chunk(row[:-1]), row[-1]) for row in rows]
        if required_tags:
            chunks = [pair for pair in chunks if required_tags & set(pair[0].metadata.tags)]
        if required_source_filter:
            chunks = [
                pair
                for pair in chunks
                if match_source_filter_value(
                    required_source_filter, str(pair[0].metadata.source_file)
                )
            ]
        if required_tags or required_source_filter:
            chunks = chunks[:top_k]
        return [
            SearchResult(
                chunk=chunk,
                score=1.0 / (1.0 + distance),
                rank=rank_idx + 1,
                source="dense",
            )
            for rank_idx, (chunk, distance) in enumerate(chunks)
        ]

    async def get_context_windows(
        self,
        source_file: Path,
        anchor_ids: Sequence[UUID],
        window: int,
        *,
        ns_filter: NamespaceFilter | None,
        system_prefixes: Sequence[str],
        scope_filter: ScopeFilter | None,
        project_context_root: Path | None,
        as_of_unix: int | None,
    ) -> dict[UUID, ContextWindowRows | None]:
        """Physical neighbours plus visible ordinal/total, without listing the file.

        Two shapes of query, and the split is deliberate. The counts come from
        one pass per *file* — a running ``SUM`` over the visibility predicate,
        read off at each anchor's row — so several hits in one file do not
        rescan it, and nothing is deserialised to count it. The neighbours are
        a bounded seek per anchor on ``(start_line, rowid)``: at most
        ``window`` rows each side, whatever the file's length. Neither grows
        with the file, which is the whole point of #2237.

        The counts exclude the anchor row itself (see
        :class:`ContextWindowRows`), so the caller's unconditional ``+ 1``
        carries the #2236 carve-out without either side second-guessing the
        other about whether the anchor is visible.

        A row withheld by a redaction hold (#2777) is neither counted nor
        returned as a neighbour, and each statement judges that in its own
        snapshot, so no neighbour is returned that was withheld when read.
        """
        if not anchor_ids:
            return {}

        from memtomem.storage import sqlite_backend as _sb

        norm_source = _sb.norm_path(source_file)
        vis_sql, vis_params = neighbor_visibility_sql(
            ns_filter=ns_filter,
            system_prefixes=system_prefixes,
            scope_filter=scope_filter,
            project_context_root=project_context_root,
            as_of_unix=as_of_unix,
        )
        # One pass per file. ``ROWS BETWEEN UNBOUNDED PRECEDING AND 1
        # PRECEDING`` is the "strictly before" the ordinal is defined on, and
        # ``SUM(vis) OVER ()`` the file's visible total; subtracting the
        # anchor's own ``vis`` is what leaves the count anchor-free. The
        # ordering matches ``list_chunks_by_source`` exactly, ``rowid``
        # tie-break included — chunks of a virtual source share a
        # ``start_line``, and an ordinal that disagreed with the listing would
        # be a different kind of wrong answer.
        # Nested subqueries rather than a CTE: ``test_mixin_commit_guard``
        # reads a leading ``WITH`` as a possible write, on the grounds that a
        # CTE can end in an INSERT and it should not have to parse one to find
        # out. This spelling is the same plan and is visibly a read.
        wanted = [str(a) for a in anchor_ids]
        async with self._lease_read_db() as db:
            rows = db.execute(
                f"""
                SELECT id, start_line, rid, vis, COALESCE(visible_before, 0), visible_all
                FROM (
                    SELECT id, start_line, rid, vis,
                           SUM(vis) OVER (
                               ORDER BY start_line, rid
                               ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                           ) AS visible_before,
                           SUM(vis) OVER () AS visible_all
                    FROM (
                        SELECT id, start_line, rowid AS rid,
                               CASE WHEN ({vis_sql}) AND {NOT_WITHHELD_CHUNKS}
                                    THEN 1 ELSE 0 END AS vis
                        FROM chunks WHERE source_file=?
                    )
                )
                WHERE id IN ({placeholders(len(wanted))})
                """,
                (*vis_params, norm_source, *wanted),
            ).fetchall()

            located = {row[0]: row for row in rows}
            result: dict[UUID, ContextWindowRows | None] = {}
            for anchor_id in anchor_ids:
                row = located.get(str(anchor_id))
                if row is None:
                    # Not a row of this file: deleted, or re-indexed onto another
                    # source between retrieval and here. Callers render it as "no
                    # context", which is now the only way that answer happens.
                    result[anchor_id] = None
                    continue
                _, start_line, rid, vis, visible_before, visible_all = row
                key = (start_line, rid, str(anchor_id))
                result[anchor_id] = ContextWindowRows(
                    before=self._neighbor_rows(db, norm_source, key, window, before=True),
                    after=self._neighbor_rows(db, norm_source, key, window, before=False),
                    visible_before=int(visible_before),
                    visible_total_excluding_anchor=int(visible_all or 0) - int(vis),
                )
            return result

    def _neighbor_rows(
        self,
        db: sqlite3.Connection,
        norm_source: str,
        anchor: tuple[int, int, str],
        window: int,
        *,
        before: bool,
    ) -> list[Chunk]:
        """The ``window`` rows physically adjacent to the anchor.

        Row-value comparison against the composite key so the seek uses
        ``idx_chunks_source_start`` instead of ordering the file. The
        predecessors come back descending — nearest first, which is what the
        ``LIMIT`` has to bound — and are reversed into reading order here.

        The anchor is excluded by id as well as by position. Position alone
        would not do it: this seek reads a later snapshot than the one that
        placed the anchor, and ``upsert_chunks`` updates a row's
        ``start_line`` in place, keeping its ``rowid``. A re-index landing in
        between would move the anchor past its own key and hand it back as one
        of its neighbours — the match rendered as its own context.

        The physical window is chosen first and withheld rows are dropped
        from it after, in the same statement: filtering before the ``LIMIT``
        would reach past a withheld row for a more distant one and present it
        as adjacent (#2777).
        """
        if window <= 0:
            return []
        start_line, rid, anchor_id = anchor
        op, direction = ("<", "DESC") if before else (">", "ASC")
        rows = db.execute(
            "SELECT * FROM chunks WHERE rowid IN ("
            f"SELECT rowid FROM chunks WHERE source_file=? AND (start_line, rowid) {op} (?, ?) "
            f"AND id <> ? ORDER BY start_line {direction}, rowid {direction} LIMIT ?"
            f") AND {NOT_WITHHELD_CHUNKS} "  # nosec B608
            f"ORDER BY start_line {direction}, rowid {direction}",
            (norm_source, start_line, rid, anchor_id, window),
        ).fetchall()
        chunks = [self._row_to_chunk(row) for row in rows]
        return list(reversed(chunks)) if before else chunks

    async def recall_chunks(
        self,
        since=None,
        until=None,
        source_filter: str | None = None,
        limit: int = 20,
        namespace_filter: NamespaceFilter | None = None,
        tag_filter: str | None = None,
        scope_filter: ScopeFilter | None = None,
        project_context_root: Path | None = None,
        metadata_filter: SearchMetadataFilter | None = None,
        chunk_ids: Sequence[UUID] | None = None,
    ) -> list[Chunk]:
        conditions: list[str] = [_VISIBLE_SOURCE]
        params: list[object] = []

        if chunk_ids is not None:
            conditions.append(_chunk_ids_sql())
            params.append(_chunk_ids_param(chunk_ids))
        if since is not None:
            conditions.append("created_at >= ?")
            params.append(utc_bound(since))
        if until is not None:
            conditions.append("created_at < ?")
            params.append(utc_bound(until))
        if source_filter is not None:
            conditions.append("source_file LIKE ? ESCAPE '\\'")
            params.append(f"%{escape_like(source_filter)}%")
        if namespace_filter is not None:
            frag, ns_params = namespace_sql(namespace_filter)
            if frag:
                conditions.append(frag)
                params.extend(ns_params)
        if tag_filter is not None:
            # Comma-separated tags = OR matching, the same semantics
            # ``SearchPipeline.search`` now pushes into BM25 (#750, #2191) so
            # the tag-only path ranks the set the keyword path retrieves.
            # Emitted as its own conjunct rather than folded into
            # ``metadata_filter``: a caller may pass both, and the two
            # constraints AND together like every other pair of filters.
            tags = parse_tag_filter(tag_filter)
            if tags:
                conditions.append(f"({_tags_any_sql('chunks.')})")
                params.append(_tags_any_param(tags))

        from memtomem.storage import sqlite_backend as _sb

        metadata_frag, metadata_params = _sb._metadata_filter_sql(metadata_filter)
        if metadata_frag:
            conditions.append(metadata_frag)
            params.extend(metadata_params)

        # ADR-0011 §6: always-on scope-context fragment.
        scope_frag, scope_params = scope_context_sql(scope_filter, project_context_root)
        conditions.append(scope_frag)
        params.extend(scope_params)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        # ``id DESC`` is the final, total tie-break. ``created_at`` is stored at
        # microsecond precision, so distinct writes almost never tie on the
        # first key; rows migrated from the old second-precision format (or
        # written in the same microsecond) still need the page boundary
        # decided, not left to whatever order SQLite chose (#516). Mixed
        # precisions compare correctly: both are UTC ISO-8601 strings and the
        # fractional part only extends the prefix.
        async with self._lease_read_db() as db:
            rows = db.execute(
                f"SELECT * FROM chunks {where} "
                f"ORDER BY created_at DESC, {scope_sort_priority_case()}, id DESC LIMIT ?",
                params,
            ).fetchall()
        return [self._row_to_chunk(row) for row in rows]

    async def sum_chunk_content_chars(
        self,
        chunk_ids: Sequence[UUID],
        project_context_root: Path | None = None,
    ) -> tuple[int, int]:
        """Return ``(count, total_content_chars)`` without materializing rows.

        Applies the same id filter and always-on ADR-0011 scope fragment
        ``recall_chunks`` would, so the count agrees with what a recall of
        the same ids under the same project context would return.

        The character total is not a bound on the prompt a caller will
        eventually build: headers, source paths, and separators push the
        formatted length higher, while stripping leading/trailing whitespace
        can pull it lower by an arbitrary amount. The unit is characters
        (SQLite ``LENGTH()`` counts characters on TEXT, not UTF-8 bytes), but
        callers must apply any hard size limit to the assembled body.
        """
        conditions = [_chunk_ids_sql(), _VISIBLE_SOURCE]
        params: list[object] = [_chunk_ids_param(chunk_ids)]
        scope_frag, scope_params = scope_context_sql(None, project_context_root)
        conditions.append(scope_frag)
        params.extend(scope_params)
        async with self._lease_read_db() as db:
            row = db.execute(
                f"SELECT COUNT(*), COALESCE(SUM(LENGTH(content)), 0) FROM chunks "
                f"WHERE {' AND '.join(conditions)}",
                params,
            ).fetchone()
        return int(row[0]), int(row[1])

    async def search_source_files_by_content(self, query: str, limit: int = 10000) -> list[Path]:
        term = query.strip()
        if not term:
            return []
        escaped_term = f"%{escape_like(term)}%"
        escaped_json_term = f"%{escape_like(json.dumps(term, ensure_ascii=True)[1:-1])}%"
        async with self._lease_read_db() as db:
            rows = db.execute(
                "SELECT source_file FROM chunks "
                f"WHERE {_VISIBLE_SOURCE} AND (content LIKE ? ESCAPE '\\' "
                "   OR heading_hierarchy LIKE ? ESCAPE '\\' "
                "   OR heading_hierarchy LIKE ? ESCAPE '\\') "
                "GROUP BY source_file "
                "ORDER BY MAX(updated_at) DESC, source_file "
                "LIMIT ?",
                (escaped_term, escaped_term, escaped_json_term, limit),
            ).fetchall()
        return [Path(row[0]) for row in rows]

    async def get_source_files_page(
        self,
        *,
        after: str | None,
        limit: int,
        source_filter: str | None = None,
        namespace: str | None = None,
        system_prefixes: Sequence[str] = (),
    ) -> tuple[list[tuple[Path, int, str | None, str | None, int, int, int]], str | None]:
        """One page of :meth:`get_source_files_with_counts`, filtered before ``LIMIT``.

        Rows come in ``source_file`` order, starting strictly after ``after``;
        held, pending and policy-derived hidden files, files ``source_filter``
        rejects and files :func:`source_namespaces_admit` drops never take a slot.
        Returns ``(rows, next_cursor)``. ``next_cursor`` is the stored
        ``source_file`` of the last row when more follow, else ``None``; it is the
        raw key, because the ``Path`` in a row can spell it differently (a POSIX
        path read on Windows) and would resume at the wrong place.

        One statement, so the page and its aggregates come from one snapshot. A
        traversal of a changing store never repeats a file, but misses files
        added before its cursor and reports each page as of its own read.
        """
        where = ["chunks.source_file > ?"]
        params: list[object] = [after or ""]
        if namespace:
            # A superset of token membership (a token is a substring of the
            # value it came from); ``memtomem_list_admit`` stays the authority.
            where.append("instr(chunks.namespace, ?) > 0")
            params.append(namespace)
        having: list[str] = []
        if source_filter:
            having.append("memtomem_source_match(?, chunks.source_file)")
            params.append(source_filter)
        having.append("memtomem_list_admit(GROUP_CONCAT(DISTINCT chunks.namespace), ?, ?)")
        params += [namespace, json.dumps(list(system_prefixes))]
        having.append(VISIBLE_SOURCE_PATH)
        params.append(limit + 1)
        async with self._lease_read_db() as db:
            rows = db.execute(
                "SELECT c.source_file, COUNT(*), MAX(c.updated_at), GROUP_CONCAT(DISTINCT c.namespace),"
                " CAST(AVG(LENGTH(c.content)/3) AS INTEGER),"
                " MIN(LENGTH(c.content)/3),"
                " MAX(LENGTH(c.content)/3)"
                " FROM (SELECT chunks.source_file FROM chunks"  # nosec B608 — fixed fragments, values bound
                f" WHERE {' AND '.join(where)}"
                f" GROUP BY chunks.source_file HAVING {' AND '.join(having)}"
                " ORDER BY chunks.source_file LIMIT ?) AS page"
                " JOIN chunks c ON c.source_file = page.source_file"
                " GROUP BY c.source_file ORDER BY c.source_file",
                params,
            ).fetchall()
        next_cursor = rows[limit - 1][0] if len(rows) > limit else None
        return [
            (Path(row[0]), row[1], row[2], row[3], row[4] or 0, row[5] or 0, row[6] or 0)
            for row in rows[:limit]
        ], next_cursor

    async def get_source_summaries(self) -> dict[str, tuple[list[str], str]]:
        """Return ``{source_file_path_str: (heading_hierarchy, first_chunk_content)}``.

        The "first chunk" is the section with the smallest ``start_line`` per
        source. Powers the Source tab's heuristic preview (first heading +
        first paragraph) — drives the fallback shown when no AI summary has
        been generated yet, or when the LLM is disabled. Pure read-side
        aggregation; no LLM, no extra storage column. Hidden sources are
        left out in this statement, so a hold committed after the caller
        read its visibility set still yields no excerpt.
        """
        async with self._lease_read_db() as db:
            # The screen is path-level, so it runs on the one row kept per file.
            rows = db.execute(
                "SELECT f.source_file, f.content, f.heading_hierarchy FROM ("
                "  SELECT source_file, content, heading_hierarchy,"
                "         ROW_NUMBER() OVER ("
                "           PARTITION BY source_file ORDER BY start_line, rowid"
                "         ) AS rn"
                "  FROM chunks"
                f") AS f WHERE f.rn = 1 AND {visible_path_sql('f.source_file')}"  # nosec B608
            ).fetchall()
        result: dict[str, tuple[list[str], str]] = {}
        for source, content, hh_json in rows:
            try:
                hh = list(json.loads(hh_json)) if hh_json else []
            except (json.JSONDecodeError, TypeError):
                hh = []
            result[source] = (hh, content or "")
        return result

    async def iter_chunks_for_audit(
        self,
        *,
        scope: str,
        source_exact: Path | None = None,
        source_prefix: Path | None = None,
        project_root: Path | None = None,
        batch_size: int = 500,
    ) -> AsyncIterator[ChunkAuditRow]:
        """Stream chunks in ``scope`` for a privacy audit walk.

        Independent of search / recall: no embedding lookup, no tag
        decode, no UI-side ordering. Uses ``ORDER BY id`` (PK) with a
        keyset cursor so pagination stays stable even if rows mutate
        between batches (the audit is read-only by contract, but cursor
        stability is cheaper to guarantee than to debug).

        ``source_exact`` and ``source_prefix`` are mutually exclusive
        contracts owned by the caller (CLI ``--source`` resolver). Both
        are normalised via :func:`norm_path` before the query, mirroring
        the storage layer's existing source-path equality contract used
        by :meth:`list_chunks_by_source` and friends.

        ``project_root`` is the ADR-0011 / ADR-0016 / issue #934 cross-
        project isolation gate. When ``scope`` is a project tier
        (``project_shared`` / ``project_local``) and multiple project
        roots share the same SQLite DB, the caller passes the current
        project root so the audit only walks rows owned by that root.
        ``None`` (the default) means "no project filter" — the
        ``--scope=user`` path always passes ``None`` because the user
        tier is global by design. Mixing ``scope='user'`` with a
        non-None ``project_root`` is a caller bug and is rejected up
        front; user-tier rows have ``project_root IS NULL`` in the
        chunks table so the filter would silently elide every user row.
        """
        if source_exact is not None and source_prefix is not None:
            raise ValueError(
                "iter_chunks_for_audit: source_exact and source_prefix are mutually exclusive"
            )
        if scope == "user" and project_root is not None:
            raise ValueError(
                "iter_chunks_for_audit: project_root must be None when scope='user' "
                "(user-tier rows have project_root IS NULL by contract)"
            )

        from memtomem.storage import sqlite_backend as _sb

        where_parts = ["COALESCE(scope, 'user') = ?"]
        params: list[object] = [scope]
        if project_root is not None:
            # ADR-0011 / issue #934 cross-project isolation. ``project_root``
            # in the chunks table is the string returned by ``norm_path``
            # at write time, so we apply the same normalisation here to
            # keep the equality contract byte-exact across platforms (the
            # source-path normalisation pattern above).
            where_parts.append("project_root = ?")
            params.append(_sb.norm_path(project_root))
        if source_exact is not None:
            where_parts.append("source_file = ?")
            params.append(_sb.norm_path(source_exact))
        elif source_prefix is not None:
            prefix = _sb.norm_path(source_prefix)
            # Component-aware prefix: anchor on ``<prefix><sep>`` so a request
            # for ``docs`` does not match ``docsuite``. ``norm_path`` already
            # resolves symlinks and folds the Unicode form (macOS) so the prefix and stored
            # paths share the same canonical form.
            #
            # ``substr(...) = ?`` instead of ``LIKE``: SQLite's built-in LIKE
            # is case-insensitive for ASCII by default, and COLLATE BINARY
            # does not override LIKE — so ``LIKE 'docs/%'`` would also match
            # ``DOCS/foo.md`` on a case-sensitive filesystem and turn an
            # audit ``--source docs`` into a false-positive over an unrelated
            # tree (Codex review on #905 P2-a). ``substr`` equality is a
            # binary string compare, case-sensitive, and avoids the LIKE /
            # GLOB metacharacter escape contract entirely.
            #
            # Separator is platform-native: stored paths come from
            # ``norm_path`` → ``Path.resolve()`` which emits ``\`` on Windows
            # and ``/`` on POSIX. Hardcoding ``/`` would build a prefix like
            # ``C:\repo\docs/`` on Windows and silently match no rows under
            # ``C:\repo\docs\...`` (Codex P2-b). Strip both separator forms
            # from the input so a caller passing a POSIX-style filter on
            # Windows (e.g. ``--source docs/sub``) still anchors correctly.
            anchored = prefix.rstrip("/\\") + os.sep
            where_parts.append("substr(source_file, 1, ?) = ?")
            params.append(len(anchored))
            params.append(anchored)

        where_sql = " AND ".join(where_parts)

        last_id: str | None = None
        while True:
            if last_id is None:
                query = (
                    "SELECT id, source_file, content, COALESCE(scope, 'user'), "
                    "project_root FROM chunks "
                    f"WHERE {where_sql} ORDER BY id LIMIT ?"
                )
                batch_params = (*params, batch_size)
            else:
                query = (
                    "SELECT id, source_file, content, COALESCE(scope, 'user'), "
                    "project_root FROM chunks "
                    f"WHERE {where_sql} AND id > ? ORDER BY id LIMIT ?"
                )
                batch_params = (*params, last_id, batch_size)

            async with self._lease_read_db() as db:
                rows = db.execute(query, batch_params).fetchall()
            if not rows:
                return

            for row in rows:
                chunk_id, source_file, content, row_scope, row_project_root = row
                yield ChunkAuditRow(
                    chunk_id=str(chunk_id),
                    source=Path(source_file),
                    content=str(content),
                    scope=str(row_scope),
                    project_root=Path(row_project_root) if row_project_root else None,
                )
            last_id = str(rows[-1][0])
            if len(rows) < batch_size:
                return
