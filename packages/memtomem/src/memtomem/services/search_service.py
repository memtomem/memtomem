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
from typing import TYPE_CHECKING, Any, get_args

from memtomem.chunking.markdown import _parse_validity_bound
from memtomem.config import TargetScope
from memtomem.models import InvalidScopeFilterError, ScopeFilter

if TYPE_CHECKING:
    from pathlib import Path

    from memtomem.constants import SearchOrigin
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


#: The scope tier vocabulary, read off the ADR-0010 ``TargetScope`` literal
#: so a tier added there is accepted here without a second edit.
_SCOPE_TIERS = frozenset(get_args(TargetScope))


def validate_scope_vocabulary(scope: str | None) -> str | None:
    """Check a ``scope`` argument against the closed ADR-0011 tier vocabulary.

    ``ScopeFilter.parse`` deliberately accepts any exact string: it is a
    predicate parser, mirroring the *open* namespace alphabet, and several
    callers depend on an unrecognized tier reaching no rows rather than
    raising (portable eval cases, the CLI's empty-state diagnostics). The
    public vocabulary is a different question, and this is where it is
    answered — before a surface opens anything — so ``scope=User`` is the
    same answer over HTTP, MCP and the CLI instead of a 422 on one and a
    successful empty search on the others. The three search surfaces call
    this; recall deliberately does not (see :meth:`ScopeFilter.parse`).

    A glob is checked too, against the same vocabulary: the alphabet is
    finite, so "does this pattern select any tier at all" is answerable, and
    ``projet_*`` is the same typo as ``projet_local`` wearing a star. What a
    glob is *not* checked for is naming a tier exactly — ``project_*`` is a
    perfectly good pattern and no tier's name.

    Returns:
        The value with surrounding whitespace stripped, or ``None`` when
        there is nothing to filter by (``None`` or blank). Surfaces must
        forward what comes back, not the original: validating a stripped
        copy while passing the padded value on would let ``" user "``
        through the check and into ``scope IN (' user ')``.

    Raises:
        InvalidScopeFilterError: the value mixes a comma list with a glob
            (raised by the parse this delegates to), an exact value or a
            member of a comma list is not a scope tier, or a glob selects
            no tier at all.
    """
    if scope is None:
        return None
    scope = scope.strip()
    if not scope:
        return None
    # Parse first: a comma/glob mix is a syntax error, and it has an
    # actionable message of its own. Checking the vocabulary ahead of the
    # parse would answer ``project_*,user`` with "matches no scope tier",
    # which is true of the string and useless to the person who typed it.
    parsed = ScopeFilter.parse(scope)
    if parsed is not None and parsed.pattern:
        if not any(parsed.matches(tier) for tier in _SCOPE_TIERS):
            raise InvalidScopeFilterError(
                f"scope {scope!r} matches no scope tier. "
                f"Patterns are matched against {', '.join(sorted(_SCOPE_TIERS))}."
            )
        return scope
    unknown = sorted(set(parsed.scopes if parsed else ()) - _SCOPE_TIERS)
    if unknown:
        raise InvalidScopeFilterError(
            f"scope {', '.join(repr(value) for value in unknown)} is not a scope tier. "
            f"Use one or more of {', '.join(sorted(_SCOPE_TIERS))}, "
            'or a glob ("project_*").'
        )
    return scope


class InvalidRrfWeightError(ValueError):
    """A caller-supplied RRF weight is outside the range fusion can honor."""


def rrf_weights_from(bm25_weight: float | None, dense_weight: float | None) -> list[float] | None:
    """Build the RRF weight pair, or ``None`` to follow server config.

    Each side defaults on ``is None``, not on falsiness. ``0.0`` is a value a
    caller can mean: it disables that leg outright — the pipeline skips the
    retrieval and fusion inserts none of its candidates (#2092). Both zero is
    refused: a search with no weighted leg is meaningless.

    Negative and non-finite weights are refused. A negative weight does not
    gently de-emphasise a leg — it inverts it, because ``w / (k + rank)``
    rises toward zero as rank grows, so rank 50 outscores rank 1 and the
    worst matches are promoted. This guards the request boundary;
    ``search.rrf_weights`` from config is validated at its own mutation
    surfaces, and the pipeline falls back to ``[1.0, 1.0]`` (with a
    warning) on a pair that bypassed them (#2094).

    Raises:
        InvalidRrfWeightError: a supplied weight is negative or non-finite,
            or both weights are zero.
    """
    if bm25_weight is None and dense_weight is None:
        return None
    weights = [
        1.0 if bm25_weight is None else bm25_weight,
        1.0 if dense_weight is None else dense_weight,
    ]
    for name, weight in zip(("bm25_weight", "dense_weight"), weights):
        # ``mem_do`` raw params are not type-checked, so a boolean or an
        # arbitrary-precision int (where ``math.isfinite`` overflows) can
        # arrive here — both are refusals, never crashes (#2094 review).
        if isinstance(weight, bool) or not isinstance(weight, (int, float)):
            raise InvalidRrfWeightError(f"{name} must be a finite number, got {weight!r}.")
        try:
            finite = math.isfinite(weight)
        except OverflowError:
            finite = False
        if not finite:
            raise InvalidRrfWeightError(f"{name} must be a finite number, got {weight}.")
        if weight < 0:
            raise InvalidRrfWeightError(f"{name} must be >= 0, got {weight}.")
    if not any(weights):
        raise InvalidRrfWeightError(
            "bm25_weight and dense_weight cannot both be zero — at least one leg must carry weight."
        )
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


def _embedding_side(info: object) -> str:
    """Render one side of an embedding mismatch as ``provider/model (Nd)``.

    The ``none`` provider carries an empty model name (``EmbeddingConfig.model``
    defaults to ``""``), so a naive join reads ``none/ (0d)``. Drop the slash
    when there is no model to name.
    """
    if not isinstance(info, dict):
        return "unknown"
    provider = str(info.get("provider") or "unknown")
    model = str(info.get("model") or "")
    dimension = info.get("dimension")
    name = f"{provider}/{model}" if model else provider
    return f"{name} ({dimension}d)" if dimension is not None else name


def dense_degraded_hint(mismatch: dict[str, Any] | None) -> str:
    """Describe a query whose dense leg was suppressed by an embedding mismatch.

    Emitted on *every* affected search, unlike the server's one-shot
    announcement: the degradation persists until the operator resets, and a
    caller who only ever sees one search has no other in-band signal (#2063).

    The wording deliberately avoids claiming the results are keyword-only —
    BM25 can be disabled or zero-weighted independently, in which case the
    query had no retrieval leg at all. It points at bare ``mm embedding-reset``
    (status mode, non-destructive), which prints both the destructive
    ``apply-current`` path and the ``revert-to-stored`` alternative, rather
    than naming the vector-deleting command directly.
    """
    detail = ""
    if mismatch is not None:
        detail = (
            f": DB stored {_embedding_side(mismatch.get('stored'))}, "
            f"config uses {_embedding_side(mismatch.get('configured'))}"
        )
    return (
        f"dense retrieval did not contribute to this query — the stored embeddings "
        f"do not match the configured embedding policy{detail}. Fix: run "
        f'`mm embedding-reset` (CLI) or mem_embedding_reset(mode="status") (MCP) '
        f"for the reset options (docs/guides/configuration.md#reset-flow)."
    )


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
    record: bool = True,
    project_context_root: Path | None = None,
    origin: SearchOrigin,
) -> tuple[list[SearchResult], RetrievalStats, list[str]]:
    """Run a hybrid search and assemble the result-derived hints.

    Args:
        pipeline: Search pipeline to query.
        query: Search query, already validated by the caller.
        namespace: Explicit namespace, or ``None`` to fall back to
            ``current_namespace``.
        current_namespace: The surface's ambient namespace.
        as_of: ``YYYY-MM-DD`` / ``YYYY-QN`` temporal bound, or ``None``.
        record: ``False`` runs the query as a replay — see
            ``SearchPipeline.search`` for what that suppresses and widens.
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
        InvalidRrfWeightError: a weight is negative or non-finite, or both
            weights are zero.
        InvalidFilterSyntaxError: ``namespace`` or ``scope`` mixes a comma
            list with a glob (raised from the pipeline's parse), or ``scope``
            names something outside the tier vocabulary — an unknown exact
            value or a glob selecting no tier — raised here by
            :func:`validate_scope_vocabulary`. The three search surfaces
            validate up front, so they see all of these before reaching this
            function; the check here is the backstop for in-process callers.
    """
    as_of_unix = parse_as_of_bound(as_of)
    # Backstop for in-process callers. Every user-facing surface validates
    # ahead of initialization, so it can fail before opening anything and
    # word the message in its own idiom; this catches a caller that reaches
    # the core directly and would otherwise hand the pipeline a scope no
    # surface would have accepted.
    scope = validate_scope_vocabulary(scope)

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
        record=record,
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
    # Degradation before discovery: a caller whose semantic leg silently
    # dropped out needs that before a note about rows they could reach.
    if stats.dense_suppressed_mismatch:
        hints.append(dense_degraded_hint(stats.mismatch_detail))
    if effective_ns is None and stats.hidden_system_ns > 0:
        hints.append(hidden_namespace_hint(stats.hidden_system_ns, stats.hidden_by_prefix))

    return results, stats, hints
