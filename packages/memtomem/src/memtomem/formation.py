"""Review-first memory candidate extraction from exact session events."""

from __future__ import annotations

import hashlib
import logging
import re
from datetime import datetime, timedelta, timezone
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from memtomem import privacy

logger = logging.getLogger(__name__)

EXTRACTOR_VERSION = "heuristic-v1"
DEFAULT_STALE_CLAIM_MINUTES = 15
_KIND_PATTERNS = (
    ("decision", 0.95, re.compile(r"(?i)\b(decision|decided|chosen)\b|결정|채택")),
    ("preference", 0.9, re.compile(r"(?i)\b(prefer|preference)\b|선호")),
    ("procedure", 0.9, re.compile(r"(?i)\b(procedure|workflow|steps?)\b|절차|워크플로")),
    ("action", 0.85, re.compile(r"(?i)\b(todo|action item|follow[- ]?up)\b|할 일|후속 조치")),
    (
        "fact",
        0.75,
        re.compile(
            r"(?i)(?:^|[.!?]\s*)fact\s*:|\b(?:runs on|depends on|uses .{1,40} for)\b|"
            r"(?:^|[.!?]\s*)사실\s*:|에서 실행된다|에 의존한다|을 사용한다|를 사용한다"
        ),
    ),
)
_SUPERSEDE_RE = re.compile(r"(?i)\b(replaced|supersedes|changed from)\b|대체|변경")


def _classify(content: str) -> tuple[str, str, str, float] | None:
    match = next(
        (
            (name, confidence)
            for name, confidence, pattern in _KIND_PATTERNS
            if pattern.search(content)
        ),
        None,
    )
    if match is None:
        return None
    kind, confidence = match
    operation = "supersede" if _SUPERSEDE_RE.search(content) else "add"
    destination = "pinned" if kind == "procedure" else "memory"
    return kind, operation, destination, confidence


async def scan_session_candidates(storage: Any, session_id: str) -> list[dict[str, Any]]:
    """Extract review candidates only from events belonging to ``session_id``."""
    events = await storage.get_session_events(session_id)
    created: list[dict[str, Any]] = []
    now = datetime.now(timezone.utc)
    for event in events:
        content = str(event["content"]).strip()
        classification = _classify(content)
        if not content or classification is None or privacy.scan(content):
            continue
        kind, operation, destination, confidence = classification
        fingerprint = hashlib.sha256(
            f"{kind}\0{operation}\0{destination}\0{content.casefold()}".encode()
        ).hexdigest()
        candidate = {
            "id": str(uuid4()),
            "session_id": session_id,
            "kind": kind,
            "operation": operation,
            "destination": destination,
            "content": content[:2000],
            "evidence": [
                {
                    "event_id": event["id"],
                    "chunk_ids": event.get("chunk_ids", []),
                    "span": [0, min(len(content), 2000)],
                }
            ],
            "matched_existing_ids": [],
            "confidence": confidence,
            "sensitivity": "normal",
            "proposed_diff": f"+ {content[:2000]}",
            "extractor_version": EXTRACTOR_VERSION,
            "fingerprint": fingerprint,
            "created_at": now.isoformat(timespec="seconds"),
            "expires_at": (now + timedelta(days=30)).isoformat(timespec="seconds"),
        }
        if await storage.add_memory_candidate(candidate):
            created.append(candidate)
    return created


async def propose_memory_candidate(
    storage: Any,
    content: str,
    *,
    source: str,
    source_ref: str,
    idempotency_key: str,
) -> tuple[dict[str, Any], bool]:
    """Queue one explicit external proposal for review without promoting it."""
    body = content.strip()
    if not body:
        raise ValueError("content cannot be empty")
    if len(body) > 2000:
        raise ValueError("content exceeds 2000 characters")
    if len(source) > 128 or len(source_ref) > 512 or len(idempotency_key) > 256:
        raise ValueError("proposal metadata exceeds size limit")
    if not source.strip() or not idempotency_key.strip():
        raise ValueError("source and idempotency_key are required")
    ref = source_ref.strip()
    if privacy.scan(body) or (ref and privacy.scan(ref)):
        raise ValueError("content or source_ref contains sensitive data")

    classification = _classify(body)
    kind, operation, destination, confidence = classification or (
        "proposed",
        "add",
        "memory",
        0.5,
    )
    now = datetime.now(timezone.utc)
    fingerprint = hashlib.sha256(
        f"external\0{source.strip()}\0{idempotency_key.strip()}".encode()
    ).hexdigest()
    external_session_id = f"external:{source.strip()}:{fingerprint[:24]}"
    await storage.create_session(
        external_session_id,
        source.strip(),
        "formation",
        metadata={"source_ref": ref, "external_proposal": True},
    )
    candidate = {
        "id": str(uuid4()),
        "session_id": external_session_id,
        "kind": kind,
        "operation": operation,
        "destination": destination,
        "content": body,
        "evidence": [{"source": source.strip(), "source_ref": ref}],
        "matched_existing_ids": [],
        "confidence": confidence,
        "sensitivity": "normal",
        "proposed_diff": f"+ {body}",
        "extractor_version": "external-proposal-v1",
        "fingerprint": fingerprint,
        "status": "pending",
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=30)).isoformat(timespec="seconds"),
    }
    created = await storage.add_memory_candidate(candidate)
    if created:
        return candidate, False

    existing = await storage.get_memory_candidate_by_fingerprint(external_session_id, fingerprint)
    if existing is None:
        raise RuntimeError("idempotent candidate insert was ignored but no row exists")
    if existing["content"] != body:
        raise ValueError("idempotency_key was already used with different content")
    return existing, True


#: Longest neighbour excerpt included in a review envelope. Enough to judge
#: what the existing memory claims; short enough that a five-neighbour
#: envelope stays readable in a terminal and in an MCP response.
NEIGHBOUR_EXCERPT_CHARS = 300


def _excerpt(text: str) -> str:
    """One-line, length-capped preview of an existing memory."""
    flat = " ".join(text.split())
    if len(flat) <= NEIGHBOUR_EXCERPT_CHARS:
        return flat
    return flat[:NEIGHBOUR_EXCERPT_CHARS] + "..."


def _currently_valid(chunk: Any, as_of_unix: int) -> bool:
    """Whether ``chunk``'s temporal-validity window covers ``as_of_unix``.

    Inclusive on both ends, ``None`` meaning unbounded — the same semantics as
    ``search.pipeline._apply_validity_filter``, deliberately reported rather
    than applied: a memory that has already been superseded is exactly the
    neighbour a reviewer most needs to see.
    """
    vfrom = chunk.metadata.valid_from_unix
    vto = chunk.metadata.valid_to_unix
    lower = vfrom if vfrom is not None else float("-inf")
    upper = vto if vto is not None else float("inf")
    return lower <= as_of_unix <= upper


async def candidate_neighbour_evidence(
    storage: Any,
    embedder: Any,
    candidate: dict[str, Any],
    *,
    top_k: int = 5,
    project_context_root: Any = None,
    display_path: Callable[[Any], str] = str,
) -> dict[str, Any]:
    """Describe what the store already says about ``candidate``.

    Decides nothing and writes no memory: the candidate keeps its status and
    its stored fields. (The caller's own ``get_memory_candidate`` lookup can
    still flip an *already-expired* pending row to ``expired`` — a pre-existing
    side effect of every candidate accessor, not of gathering evidence.) The
    returned envelope always carries a ``status`` — evidence that cannot be
    gathered must never block inspecting or deciding a candidate, so a failed
    lookup is reported in-band instead of raised.

    ``status`` is one of:

    - ``available`` — the lookup ran. ``neighbours`` may still be empty, which
      means the store genuinely has nothing close.
    - ``dense_disabled`` — the store is bm25-only, so there is no neighbour
      signal to give at all (not "no neighbours").
    - ``dense_not_indexed`` — the store holds chunks but none of them have a
      vector yet (right after an embedding reset, or mid first index). Dense
      search would answer "nothing is close" about a corpus it cannot see.
    - ``dimension_mismatch`` — the embedder's width disagrees with the
      store's; re-index or fix the embedding config before trusting a review.
    - ``unavailable`` — anything else went wrong; details are logged.

    When the store can answer, ``coverage`` reports ``{"total", "with_dense"}``
    so a reviewer can discount an empty or thin result set that reflects a
    partly-vectorised corpus rather than an empty one.

    The corpus is the indexed chunks the caller's scope can see: accepted,
    durable memories. Pending candidates are not compared against each other,
    and pinned-context blocks are not visible to dense search.

    Args:
        storage: Storage backend.
        embedder: Embedding provider.
        candidate: Candidate row, as returned by the storage accessors.
        top_k: Maximum neighbours to return, highest dense score first.
        project_context_root: Project root to pin neighbours to.
        display_path: Renders a chunk's source path for the calling surface —
            the MCP surface passes a redacting formatter, the CLI does not.

    Returns:
        A fresh envelope dict; see ``status`` above.
    """
    from memtomem.search.conflict import (
        CONFLICT_OVERLAP_MAX,
        DENSE_SCORE_THRESHOLD,
        EVIDENCE_VERSION,
        RESTATEMENT_OVERLAP_MIN,
        find_neighbours,
    )

    envelope: dict[str, Any] = {
        "candidate_id": candidate.get("id"),
        "status": "available",
        "version": EVIDENCE_VERSION,
        "corpus": "indexed_chunks",
        "thresholds": {
            "dense_score": DENSE_SCORE_THRESHOLD,
            "conflict_overlap_max": CONFLICT_OVERLAP_MAX,
            "restatement_overlap_min": RESTATEMENT_OVERLAP_MIN,
        },
        "neighbours": [],
        "summary": {"potential_conflict": 0, "restatement_candidate": 0, "related": 0},
    }

    if getattr(storage, "dense_enabled", True) is False:
        envelope["status"] = "dense_disabled"
        envelope["reason"] = "store is bm25-only; no neighbour signal is available"
        return envelope

    # A live ``chunks_vec`` table is not the same as a vectorised corpus: an
    # embedding reset recreates it empty, and a first index fills it gradually.
    # Reporting "nothing is close" from a corpus dense search cannot see yet
    # would be the one failure this envelope exists to prevent.
    coverage = None
    get_coverage = getattr(storage, "get_dense_coverage", None)
    if get_coverage is not None:
        try:
            coverage = await get_coverage()
        except Exception:
            logger.warning("Candidate evidence: dense coverage probe failed", exc_info=True)
    if coverage is not None:
        envelope["coverage"] = dict(coverage)
        if coverage.get("total", 0) > 0 and coverage.get("with_dense", 0) == 0:
            envelope["status"] = "dense_not_indexed"
            envelope["reason"] = (
                "no indexed chunk has a vector yet; re-index before reading this as "
                "'nothing is close'"
            )
            return envelope

    try:
        neighbours = await find_neighbours(
            candidate.get("content", ""),
            storage,
            embedder,
            top_k=top_k,
            project_context_root=project_context_root,
        )
    except ValueError as exc:
        # Only the store's own width check earns the dimension remediation.
        # A ValueError from anywhere else (an embedding provider rejecting its
        # input, say) would be handed a fix that does not apply, so it falls
        # through to the generic branch with no raw message echoed out.
        if "dimension mismatch" not in str(exc).lower():
            logger.warning("Candidate evidence lookup failed", exc_info=True)
            envelope["status"] = "unavailable"
            envelope["reason"] = "neighbour lookup failed; see server logs"
            return envelope
        logger.warning("Candidate evidence: dimension mismatch", exc_info=True)
        envelope["status"] = "dimension_mismatch"
        envelope["reason"] = str(exc)
        return envelope
    except Exception:
        logger.warning("Candidate evidence lookup failed", exc_info=True)
        envelope["status"] = "unavailable"
        envelope["reason"] = "neighbour lookup failed; see server logs"
        return envelope

    as_of_unix = int(datetime.now(timezone.utc).timestamp())
    for n in neighbours:
        meta = n.chunk.metadata
        envelope["neighbours"].append(
            {
                "chunk_id": str(n.chunk.id),
                "source_file": display_path(meta.source_file),
                "namespace": meta.namespace,
                "excerpt": _excerpt(n.chunk.content),
                "dense_score": round(n.dense_score, 4),
                "text_overlap": round(n.text_overlap, 4),
                "label": n.label,
                "valid_from_unix": meta.valid_from_unix,
                "valid_to_unix": meta.valid_to_unix,
                "currently_valid": _currently_valid(n.chunk, as_of_unix),
            }
        )
        envelope["summary"][n.label] += 1

    return envelope
