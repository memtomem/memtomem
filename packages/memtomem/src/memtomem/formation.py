"""Review-first memory candidate extraction from exact session events."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from memtomem import privacy

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
        raise ValueError("content contains sensitive data")

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
