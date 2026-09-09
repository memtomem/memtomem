"""Cross-process completed-source receipts. Call only under the source lock."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from memtomem.storage.sqlite_helpers import norm_path


def digest(value: Any) -> str:
    def normalize(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(k): normalize(v) for k, v in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted((normalize(v) for v in item), key=str)
        if isinstance(item, (list, tuple)):
            return [normalize(v) for v in item]
        return item

    return hashlib.sha256(
        json.dumps(normalize(value), sort_keys=True, default=str).encode()
    ).hexdigest()


def state(db: Any, source: Path, dimension: int) -> tuple[str, int] | None:
    rows = db.execute(
        """SELECT c.rowid, c.id, c.content, c.content_hash, c.heading_hierarchy,
        c.retrieval_context, c.namespace, c.tags, c.valid_from_unix, c.valid_to_unix,
        c.scope, c.project_root, c.start_line, c.end_line, c.source_read_only,
        c.source_span_hash, c.redaction_count
        FROM chunks c WHERE c.source_file=? ORDER BY c.id""",
        (norm_path(source),),
    ).fetchall()
    if not rows:
        return None
    for row in rows:
        if db.execute("SELECT 1 FROM chunks_fts WHERE rowid=?", (row[0],)).fetchone() is None:
            return None
        if (
            dimension
            and db.execute("SELECT 1 FROM chunks_vec WHERE rowid=?", (row[0],)).fetchone() is None
        ):
            return None
    return digest([list(row) for row in rows]), len(rows)


def reusable(db: Any, source: Path, source_hash: str, policy: str, dimension: int) -> int | None:
    row = db.execute(
        "SELECT source_hash, policy, state_hash FROM source_index_receipts WHERE source_file=?",
        (norm_path(source),),
    ).fetchone()
    if row is None or row[0] != source_hash or row[1] != policy:
        return None
    current = state(db, source, dimension)
    return current[1] if current is not None and current[0] == row[2] else None


def record(db: Any, source: Path, source_hash: str, policy: str, dimension: int) -> None:
    current = state(db, source, dimension)
    if current is not None:
        db.execute(
            """INSERT OR REPLACE INTO source_index_receipts
            (source_file, source_hash, policy, state_hash) VALUES (?, ?, ?, ?)""",
            (norm_path(source), source_hash, policy, current[0]),
        )
