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


def content_hash(text: str) -> str:
    """Hash source text directly, without ``digest``'s JSON normalization pass.

    ``digest`` exists for structured policy payloads; handing it a whole source
    file means a full ``json.dumps`` escape of that file before any hashing.
    """
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def state(db: Any, source: Path, dimension: int) -> tuple[str, int] | None:
    # One statement, not two point lookups per chunk row: a large source has
    # hundreds of chunks, and this runs on both the reuse check and the record.
    # ``incomplete`` is non-zero when a row is missing its FTS shadow, or its
    # vector while embeddings are configured — either way the stored index is
    # not whole and no receipt may be honoured.
    #
    # The vector term is spliced into the SQL *text* rather than parameterized
    # off: with ``dimension == 0`` the table was never created (see
    # ``sqlite_schema``, issue #298), and SQLite resolves every table name at
    # prepare time, so a disabled-but-present ``chunks_vec`` sub-select would
    # raise "no such table" on BM25-only databases.
    vector_term = (
        " OR NOT EXISTS (SELECT 1 FROM chunks_vec v WHERE v.rowid=c.rowid)" if dimension else ""
    )
    rows = db.execute(
        f"""SELECT c.rowid, c.id, c.content, c.content_hash, c.heading_hierarchy,
        c.retrieval_context, c.namespace, c.tags, c.valid_from_unix, c.valid_to_unix,
        c.scope, c.project_root, c.start_line, c.end_line, c.source_read_only,
        c.source_span_hash, c.redaction_count,
        (NOT EXISTS (SELECT 1 FROM chunks_fts f WHERE f.rowid=c.rowid){vector_term})
        AS incomplete
        FROM chunks c WHERE c.source_file=? ORDER BY c.id""",  # nosec B608 - see above
        (norm_path(source),),
    ).fetchall()
    if not rows:
        return None
    if any(row[-1] for row in rows):
        return None
    # Drop the completeness column before hashing: it is a validity check, not
    # part of the stored state the receipt pins.
    return digest([list(row[:-1]) for row in rows]), len(rows)


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
