"""Cross-process completed-source receipts. Call only under the source lock."""

from __future__ import annotations

import hashlib
import json
import os
from importlib import metadata
from pathlib import Path
from typing import Any

import memtomem
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


def _raise(error: OSError) -> None:
    raise error


def _tree_fingerprint(root: Path) -> str:
    """Hash every ``*.py`` file under *root*, path included, in a fixed order.

    Files are ordered by their POSIX relative path as a string, so the digest
    does not depend on the platform's path separator or ``Path`` ordering.
    Paths are hashed as file-system bytes, so a name that is not valid UTF-8
    still hashes. Raises ``OSError`` when any directory cannot be listed or no
    file is found: a partial or empty walk would be a stable digest of the
    wrong tree.
    """
    files = sorted(
        ((Path(dirpath) / name).relative_to(root).as_posix(), Path(dirpath) / name)
        for dirpath, _dirs, names in os.walk(root, onerror=_raise)
        for name in names
        if name.endswith(".py")
    )
    if not files:
        raise FileNotFoundError(f"no Python source under {root}")
    h = hashlib.sha256()
    for rel, path in files:
        h.update(os.fsencode(rel))
        h.update(b"\0")
        h.update(path.read_bytes())
        h.update(b"\0")
    return h.hexdigest()


def code_fingerprint() -> str:
    """Identity of the installed memtomem source, for the receipt policy.

    The release version alone cannot tell two builds of one version apart: a
    checkout of unreleased ``main`` and the PyPI release both report it, and
    a chunker change between them would otherwise reuse the other build's
    receipts (#2773). The whole package is hashed, not only ``chunking/``,
    because chunk output also depends on modules outside it.

    Returns ``"unavailable"`` when the source cannot be read (a zipped or
    frozen install), which leaves the version as the only code identity. It
    runs while the indexing engine is imported, so no failure may escape.
    """
    location = getattr(memtomem, "__file__", None)
    if not location:
        return "unavailable"
    try:
        return _tree_fingerprint(Path(location).parent)
    except (OSError, ValueError):
        return "unavailable"


# Third-party packages whose presence or version changes chunk output, as
# top-level module name -> distribution name. Each one is imported lazily
# under ``chunking/`` and the chunker falls back when it is absent, so an
# install, removal or upgrade changes chunks without touching memtomem's own
# source (#2774). ``tests/test_receipt_chunker_versions.py`` keeps the keys
# equal to the third-party modules imported directly under ``chunking/``.
CHUNKER_DISTRIBUTIONS: dict[str, str] = {
    "tokenizers": "tokenizers",
    "tree_sitter": "tree-sitter",
    "tree_sitter_javascript": "tree-sitter-javascript",
    "tree_sitter_python": "tree-sitter-python",
    "tree_sitter_typescript": "tree-sitter-typescript",
    "yaml": "PyYAML",
}


def chunker_versions() -> dict[str, str | None]:
    """Installed version of each third-party chunker dependency, for the receipt policy.

    Keyed by distribution name; ``None`` when the distribution is not
    installed. Read from distribution metadata rather than by importing: the
    chunkers import these lazily on purpose, and this runs while the indexing
    engine is imported. A distribution whose metadata is present but whose
    import fails is therefore recorded by its version, not by the fallback the
    chunker will take.

    No failure may escape. A metadata error other than "not installed"
    records ``"unavailable"`` for that entry, which still differs from both a
    version and ``None``.
    """
    versions: dict[str, str | None] = {}
    for dist in sorted(set(CHUNKER_DISTRIBUTIONS.values())):
        try:
            versions[dist] = metadata.version(dist)
        except metadata.PackageNotFoundError:
            versions[dist] = None
        except Exception:
            versions[dist] = "unavailable"
    return versions


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
