"""Review-first memory candidate extraction from exact session events."""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from memtomem import privacy

EXTRACTOR_VERSION = "heuristic-v1"
_KIND_PATTERNS = (
    ("decision", re.compile(r"(?i)\b(decision|decided|chosen)\b|결정|채택")),
    ("preference", re.compile(r"(?i)\b(prefer|preference)\b|선호")),
    ("procedure", re.compile(r"(?i)\b(procedure|workflow|steps?)\b|절차|워크플로")),
    ("action", re.compile(r"(?i)\b(todo|action item|follow[- ]?up)\b|할 일|후속 조치")),
    ("fact", re.compile(r"(?i)\b(fact|uses?|is|are|runs?)\b|사실|사용|이다|입니다")),
)
_SUPERSEDE_RE = re.compile(r"(?i)\b(replaced|supersedes|changed from)\b|대체|변경")


def _classify(content: str) -> tuple[str, str, str] | None:
    kind = next((name for name, pattern in _KIND_PATTERNS if pattern.search(content)), None)
    if kind is None:
        return None
    operation = "supersede" if _SUPERSEDE_RE.search(content) else "add"
    destination = "pinned" if kind == "procedure" else "memory"
    return kind, operation, destination


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
        kind, operation, destination = classification
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
            "confidence": 0.8,
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
