"""Entity-match scoring — boost chunks whose extracted entities match the query.

Stage 7b of the keyword search path. Entities are extracted from the *query*
with the regex extractor (:mod:`memtomem.tools.entity_extraction`, never the
LLM path — search latency) and matched against ``chunk_entities`` rows written
by ``mem_entity_scan``.

The boost is **presence-only**: stored ``confidence`` mixes two uncalibrated
scales (hardcoded per-pattern regex values vs model-supplied LLM values), so it
gates candidate rows via ``min_confidence`` but never shapes the factor.
Coverage is sparse by construction — rows exist only for chunks a scan has
visited — so a chunk with no matching entities keeps its score exactly
(factor 1.0). This stage only ever promotes; it never penalizes.
"""

from __future__ import annotations

import re
from collections.abc import Sequence

from memtomem.models import SearchResult

# Deterministic cap on the query-side entity set. Bounds both the SQL OR-clause
# and the coverage denominator; not a config knob because it exists to keep the
# query bounded, not to tune ranking.
_MAX_QUERY_ENTITIES = 8


def entity_boost_factor(matched: int, total: int, max_boost: float = 1.5) -> float:
    """Coverage-based boost factor.

    matched=0            → 1.0        (no boost)
    matched=total        → max_boost  (every query entity present)
    matched=total/2      → midpoint

    ``total <= 0`` (no entities in the query) yields 1.0 — the caller should
    skip the stage entirely in that case.
    """
    if total <= 0 or matched <= 0:
        return 1.0
    coverage = min(matched, total) / total
    return 1.0 + (max_boost - 1.0) * coverage


def extract_query_entities(
    query: str,
    entity_types: Sequence[str],
    max_entities: int = _MAX_QUERY_ENTITIES,
) -> list[tuple[str, str]]:
    """Extract match keys ``(entity_type, lowercased value)`` from a query.

    Wraps the regex extractor. Two query-side adjustments over raw extraction:

    - ``technology`` hits are re-checked with a word-boundary match. The
      extractor's known-tech scan uses plain substring ``find`` (fine for prose
      where a stray hit is one entity among many, misleading for a 3-word query
      where "git" inside "digit" would be a third of the coverage denominator).
    - The result is capped deterministically by ``(position, type, value)`` so
      the same query always yields the same key set — the boost has to be
      reproducible for cached and replayed searches.
    """
    if not query or not entity_types:
        return []

    from memtomem.tools.entity_extraction import extract_entities

    extracted = extract_entities(query, list(entity_types))
    if not extracted:
        return []

    ordered = sorted(extracted, key=lambda e: (e.position, e.entity_type, e.entity_value.lower()))

    keys: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for e in ordered:
        value = e.entity_value.lower()
        if e.entity_type == "technology" and not _is_word_boundary_match(query, value):
            continue
        key = (e.entity_type, value)
        if key in seen:
            continue
        seen.add(key)
        keys.append(key)
        if len(keys) >= max_entities:
            break
    return keys


def _is_word_boundary_match(query: str, value: str) -> bool:
    """Whether ``value`` appears in ``query`` as a whole word."""
    return re.search(rf"(?<!\w){re.escape(value)}(?!\w)", query, re.IGNORECASE) is not None


def apply_entity_boost(
    results: list[SearchResult],
    matches: dict[str, set[tuple[str, str]]],
    total_query_entities: int,
    max_boost: float = 1.5,
) -> list[SearchResult]:
    """Apply entity-match boost to search results and re-sort.

    Args:
        results: Search results to boost.
        matches: Mapping of chunk_id (str) → set of matched ``(type, value)``
            keys. Chunks absent from the mapping are left at factor 1.0.
        total_query_entities: Size of the query entity set (the coverage
            denominator).
        max_boost: Maximum multiplier for a chunk matching every query entity.

    Returns:
        Re-sorted results with boosted scores.
    """
    if not results or total_query_entities <= 0:
        return results

    boosted = []
    for r in results:
        matched = len(matches.get(str(r.chunk.id), ()))
        factor = entity_boost_factor(matched, total_query_entities, max_boost)
        boosted.append(
            SearchResult(
                chunk=r.chunk,
                score=r.score * factor,
                rank=r.rank,
                source=r.source,
            )
        )

    boosted.sort(key=lambda r: r.score, reverse=True)

    return [
        SearchResult(chunk=r.chunk, score=r.score, rank=i + 1, source=r.source)
        for i, r in enumerate(boosted)
    ]
