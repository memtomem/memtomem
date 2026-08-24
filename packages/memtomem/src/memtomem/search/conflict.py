"""Conflict detection — find contradictions between new and existing memories."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from pathlib import Path

    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.models import Chunk
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)

#: Minimum dense score for a neighbour to count as "about the same thing".
DENSE_SCORE_THRESHOLD = 0.75
#: Token overlap below which a same-topic neighbour reads as a rewrite of the
#: same claim rather than a repetition of it.
CONFLICT_OVERLAP_MAX = 0.3
#: Token overlap at or above which a neighbour reads as a restatement.
RESTATEMENT_OVERLAP_MIN = 0.6
#: Bumped whenever the labelling rule changes, so a stored or logged verdict
#: can be traced to the rule that produced it.
EVIDENCE_VERSION = "neighbour-v1"

#: Advisory labels. They describe the *shape* of the similarity, not a
#: semantic judgement: high dense score with low token overlap is equally
#: consistent with a contradiction and with a paraphrase, so the reviewer —
#: not this module — decides what the neighbour means.
NeighbourLabel = Literal["restatement_candidate", "potential_conflict", "related"]


@dataclass(frozen=True)
class ConflictCandidate:
    """A potential conflict between new content and an existing chunk."""

    existing_chunk: Chunk
    similarity: float  # storage dense score (see NeighbourEvidence.dense_score)
    text_overlap: float  # Jaccard token overlap (low = conflict)
    conflict_score: float  # similarity - text_overlap


@dataclass(frozen=True)
class NeighbourEvidence:
    """An existing chunk near some content, with the signal used to judge it."""

    chunk: Chunk
    #: Score as returned by ``StorageBackend.dense_search``. For the SQLite
    #: backend that is ``1 / (1 + distance)`` — monotonic in closeness but
    #: **not** a cosine similarity; don't read it as a percentage of meaning.
    dense_score: float
    text_overlap: float  # Jaccard token overlap over whitespace tokens
    label: NeighbourLabel


def _jaccard_tokens(a: str, b: str) -> float:
    """Compute Jaccard similarity between token sets."""
    tokens_a = set(a.lower().split())
    tokens_b = set(b.lower().split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _label(dense_score: float, overlap: float) -> NeighbourLabel:
    """Classify a neighbour from its two scores. Advisory, never a decision."""
    if overlap >= RESTATEMENT_OVERLAP_MIN:
        return "restatement_candidate"
    if dense_score >= DENSE_SCORE_THRESHOLD and overlap < CONFLICT_OVERLAP_MAX:
        return "potential_conflict"
    return "related"


async def find_neighbours(
    content: str,
    storage: StorageBackend,
    embedder: EmbeddingProvider,
    *,
    top_k: int = 10,
    project_context_root: Path | None = None,
) -> list[NeighbourEvidence]:
    """Return the existing chunks nearest ``content``, scored and labelled.

    Unlike :func:`detect_conflicts` this applies **no** score or overlap
    filter and **no** exception handling: every neighbour the store returns
    comes back, and failures propagate. Callers that surface this to a human
    need to tell "the store has nothing to say" apart from "the lookup
    broke" — a dimension mismatch raises ``ValueError`` out of
    ``dense_search`` (``sqlite_backend.py``), and collapsing that into an
    empty list would present a broken probe as a clean bill of health.

    ``project_context_root`` is threaded onto the always-on storage scope
    filter (ADR-0011 PR-D round 11) so neighbours respect the same project
    boundary as the caller's surrounding operation.

    Args:
        content: Text to find neighbours for.
        storage: Storage backend.
        embedder: Embedding provider.
        top_k: Maximum neighbours to return.
        project_context_root: Project root to pin neighbours to.

    Returns:
        Neighbours sorted by ``dense_score`` descending.
    """
    embedding = await embedder.embed_query(content)
    results = await storage.dense_search(
        embedding,
        top_k=top_k,
        project_context_root=project_context_root,
    )

    neighbours: list[NeighbourEvidence] = []
    for r in results:
        overlap = _jaccard_tokens(content, r.chunk.content)
        neighbours.append(
            NeighbourEvidence(
                chunk=r.chunk,
                dense_score=r.score,
                text_overlap=overlap,
                label=_label(r.score, overlap),
            )
        )
    neighbours.sort(key=lambda n: n.dense_score, reverse=True)
    return neighbours[:top_k]


async def detect_conflicts(
    content: str,
    storage: StorageBackend,
    embedder: EmbeddingProvider,
    threshold: float = DENSE_SCORE_THRESHOLD,
    max_candidates: int = 5,
    *,
    project_context_root: Path | None = None,
) -> list[ConflictCandidate]:
    """Find existing chunks that semantically match but textually differ.

    A conflict is: high embedding similarity + low text overlap.
    This suggests the same topic is discussed but with different content.

    ``project_context_root`` is threaded onto the always-on storage
    scope filter (ADR-0011 PR-D round 11) so conflict candidates
    respect the same project boundary as the caller's surrounding
    operation. Without it, conflict detection would silently sample
    only user-tier candidates even when the caller is operating
    inside a registered project.

    Args:
        content: New content to check.
        storage: Storage backend.
        embedder: Embedding provider.
        threshold: Minimum similarity to consider.
        max_candidates: Maximum conflicts to return.
        project_context_root: Project root to pin candidates to.

    Returns:
        List of conflict candidates sorted by conflict_score descending.
    """
    try:
        neighbours = await find_neighbours(
            content,
            storage,
            embedder,
            top_k=10,
            project_context_root=project_context_root,
        )
    except Exception:
        logger.warning("Conflict detection failed", exc_info=True)
        return []

    candidates = [
        ConflictCandidate(
            existing_chunk=n.chunk,
            similarity=n.dense_score,
            text_overlap=n.text_overlap,
            conflict_score=n.dense_score - n.text_overlap,
        )
        for n in neighbours
        # High similarity + low text overlap = likely conflict
        if n.dense_score >= threshold and n.text_overlap < CONFLICT_OVERLAP_MAX
    ]

    candidates.sort(key=lambda c: c.conflict_score, reverse=True)
    return candidates[:max_candidates]
