"""Search service: the surface-independent half of a memory search.

Holds the retrieval logic every surface repeats — temporal-bound parsing,
namespace fallback, RRF weight assembly, the pipeline call, and the
trust-UX hints that belong to the *result*, not to the transport. The MCP
tool, the CLI, and the in-process adapters can all call this and then
render however they render.

Dependencies arrive explicitly (``pipeline``, ``current_namespace``,
``project_context_root``) rather than through a server ``app`` object, so
this module imports nothing from MCP/Web/CLI — see ``services/__init__``.

``origin`` has no default: it labels the call in the persisted query-run
observation, and a default here would silently mislabel whichever surface
forgot to pass one.

Errors are raised, not returned as strings. Each surface translates them
into its own idiom (the MCP tool returns ``"Error: ..."`` text, web routes
map to HTTP status codes).
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from memtomem.chunking.markdown import _parse_validity_bound

if TYPE_CHECKING:
    from pathlib import Path

    from memtomem.models import SearchResult
    from memtomem.search.pipeline import RetrievalStats, SearchPipeline


class InvalidTemporalBoundError(ValueError):
    """``as_of`` was not a recognized temporal bound.

    A dedicated type so surfaces can translate *this* failure without
    also catching a ``ValueError`` raised from inside the pipeline.
    """


def parse_as_of_bound(as_of: str | None) -> int | None:
    """Parse an ``as_of`` temporal bound into a unix timestamp.

    Exposed separately so surfaces can reject a malformed bound before
    they do any setup work — ``mem_search`` validates its arguments
    before initializing the app, and that ordering is part of its
    contract.

    Raises:
        InvalidTemporalBoundError: ``as_of`` is not a recognized bound.
    """
    if as_of is None:
        return None
    as_of_unix = _parse_validity_bound(as_of, upper=False)
    if as_of_unix is None:
        raise InvalidTemporalBoundError(
            f"invalid as_of value '{as_of}'. "
            "Accepted formats: 'YYYY-MM-DD' (date) or 'YYYY-QN' (quarter, N in 1-4)."
        )
    return as_of_unix


# Characters this renderer cannot put through ``prefix + "*"`` safely.
#
# ``%`` is the sharp one: the glob-to-SQL step escapes ``_`` and maps ``*`` to
# ``%``, but leaves an existing ``%`` alone (``storage/sqlite_helpers.py``), so
# rendering it raw would *count* the prefix literally and *query* it as a
# wildcard — two different sets. A literal ``%`` is expressible by hand (the
# clause runs under ``ESCAPE '\\'``, so ``\\%`` matches one), and so is a
# literal backslash; this renderer just doesn't emit those escapes, and adding
# them would mean owning their edge cases for a shape no default configuration
# produces. Literal ``*`` is genuinely unrepresentable — every ``*`` becomes a
# wildcard. ``"`` is not a SQL problem at all: it breaks the quoted query as
# printed.
#
# So this is a rendering limit, not a parser limit: skip the suggestion rather
# than print one that selects a different set than the count reported.
_UNQUOTABLE_IN_GLOB = frozenset('%*\\"')


class InvalidRrfWeightError(ValueError):
    """A caller-supplied RRF weight is outside the range fusion can honor."""


def rrf_weights_from(bm25_weight: float | None, dense_weight: float | None) -> list[float] | None:
    """Build the RRF weight pair, or ``None`` to follow server config.

    Each side defaults on ``is None``, not on falsiness. ``0.0`` is a value a
    caller can mean: fusion adds ``weight / (k + rank)`` per leg, so a zero
    weight drops that retriever's votes out of the score. (Its candidates can
    still occupy slots at score 0 when the other leg returns fewer than
    ``top_k`` — zero silences a leg's ranking influence, it does not remove
    its rows; see #2092.)

    Negative and non-finite weights are refused. A negative weight does not
    gently de-emphasise a leg — it inverts it, because ``w / (k + rank)``
    rises toward zero as rank grows, so rank 50 outscores rank 1 and the
    worst matches are promoted. This guards the request boundary only —
    ``search.rrf_weights`` in config takes the same values with no validation
    of its own today (#2094), so a configured weight still reaches fusion
    unchecked.

    Raises:
        InvalidRrfWeightError: a supplied weight is negative or non-finite.
    """
    if bm25_weight is None and dense_weight is None:
        return None
    weights = [
        1.0 if bm25_weight is None else bm25_weight,
        1.0 if dense_weight is None else dense_weight,
    ]
    for name, weight in zip(("bm25_weight", "dense_weight"), weights):
        if not math.isfinite(weight):
            raise InvalidRrfWeightError(f"{name} must be a finite number, got {weight}.")
        if weight < 0:
            raise InvalidRrfWeightError(f"{name} must be >= 0, got {weight}.")
    return weights


def hidden_namespace_hint(total: int, by_prefix: dict[str, int], *, noun: str = "result(s)") -> str:
    """Describe hidden rows, and hand back a query that actually finds them.

    Two things the previous wording got wrong. ``system_namespace_prefixes``
    holds more than ``archive:`` — the default set also hides
    ``agent-runtime:`` — so naming a fixed prefix pointed at a namespace that
    may hold none of the rows just counted. And ``namespace="archive:..."``
    was never a working query: ``NamespaceFilter.parse`` treats a value as a
    glob only when it contains ``*`` and otherwise matches exactly, so the
    ellipsis asked for a namespace literally named ``archive:...``.

    So: name the prefixes that matched, and quote each as its own glob. One
    query per group, because a comma list cannot carry a glob — ``parse``
    checks for ``*`` first and would read the whole string as a single
    pattern.

    A prefix this renderer cannot quote as a glob meaning exactly itself is
    counted but not quoted (see ``_UNQUOTABLE_IN_GLOB``): suggesting a query
    that selects a different set than the one just reported is worse than
    suggesting none. When that leaves nothing quotable, fall back to
    unqualified advice.
    """
    if not by_prefix:
        return (
            f"{total} {noun} hidden in system namespaces "
            "(pass an explicit namespace to include them)."
        )
    prefixes = sorted(by_prefix)
    breakdown = ", ".join(f"{by_prefix[prefix]} in {prefix}*" for prefix in prefixes)
    quotable = [p for p in prefixes if not (_UNQUOTABLE_IN_GLOB & set(p))]
    if not quotable:
        return (
            f"{total} {noun} hidden in system namespaces: {breakdown} "
            "(pass an explicit namespace to include them)."
        )
    queries = " or ".join(f'namespace="{prefix}*"' for prefix in quotable)
    if len(quotable) < len(prefixes):
        suffix = "to include the groups it names"
    elif len(quotable) == 1:
        suffix = "to include them"
    else:
        suffix = "to include each group"
    return f"{total} {noun} hidden in system namespaces: {breakdown} (pass {queries} {suffix})."


async def run_search(
    pipeline: SearchPipeline,
    *,
    query: str,
    top_k: int = 10,
    source_filter: str | None = None,
    tag_filter: str | None = None,
    namespace: str | None = None,
    current_namespace: str | None = None,
    as_of: str | None = None,
    bm25_weight: float | None = None,
    dense_weight: float | None = None,
    context_window: int = 0,
    scope: str | None = None,
    rerank: bool | None = None,
    project_context_root: Path | None = None,
    origin: str,
) -> tuple[list[SearchResult], RetrievalStats, list[str]]:
    """Run a hybrid search and assemble the result-derived hints.

    Args:
        pipeline: Search pipeline to query.
        query: Search query, already validated by the caller.
        namespace: Explicit namespace, or ``None`` to fall back to
            ``current_namespace``.
        current_namespace: The surface's ambient namespace.
        as_of: ``YYYY-MM-DD`` / ``YYYY-QN`` temporal bound, or ``None``.
        project_context_root: ADR-0011 scope anchor; resolve it on the
            caller's side (``runtime.project_context``).
        origin: Call-origin label recorded with the query run.

    Returns:
        ``(results, stats, hints)``. ``hints`` are the notices derivable
        from the query and its stats, in emission order; surface-bound
        notices (e.g. the embedding-dimension announcement, which is
        per-process state) are the caller's to append.

    Raises:
        InvalidTemporalBoundError: ``as_of`` is not a recognized bound.
        InvalidRrfWeightError: a weight is negative or non-finite.
    """
    as_of_unix = parse_as_of_bound(as_of)

    effective_ns = namespace or current_namespace

    rrf_weights = rrf_weights_from(bm25_weight, dense_weight)

    results, stats = await pipeline.search(
        query=query,
        top_k=top_k,
        source_filter=source_filter,
        tag_filter=tag_filter,
        namespace=effective_ns,
        rrf_weights=rrf_weights,
        context_window=context_window if context_window > 0 else None,
        as_of_unix=as_of_unix,
        scope=scope,
        project_context_root=project_context_root,
        rerank=rerank,
        origin=origin,
    )

    # Trust-UX hints shared across formats. The archive notice is emitted
    # only for callers who did NOT pin a namespace — otherwise the archive
    # filter never engaged.
    hints: list[str] = []
    # stats.rerank_applied is the per-call effective decision, not the live
    # config — accurate even when a hot reload flips rerank.enabled while
    # this call is in flight.
    if rerank is True and not stats.rerank_applied:
        hints.append(
            "rerank=true requested but server reranking is disabled "
            "(rerank.enabled=false); results are un-reranked."
        )
    if effective_ns is None and stats.hidden_system_ns > 0:
        hints.append(hidden_namespace_hint(stats.hidden_system_ns, stats.hidden_by_prefix))

    return results, stats, hints
