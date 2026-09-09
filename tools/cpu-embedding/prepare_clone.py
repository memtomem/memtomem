#!/usr/bin/env python3
"""Back up SQLite read-only and clone selected indexed sources for evaluation.

Does not apply a migration or edit production configuration. Explicit source
arguments avoid accidentally copying an entire private corpus. Output is private.
"""

import argparse
from contextlib import closing
import hashlib
import json
import os
import sqlite3
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source", type=Path, action="append", required=True)
    args = parser.parse_args()
    os.umask(0o077)
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    config_bytes = args.config.expanduser().read_bytes()
    (root / "rollback-config.json").write_bytes(config_bytes)
    source_root = root / "sources"
    source_root.mkdir()
    original = args.database.expanduser().resolve()
    with closing(sqlite3.connect(original.as_uri() + "?mode=ro", uri=True)) as src:
        with closing(sqlite3.connect(root / "rollback.sqlite3")) as dest:
            src.backup(dest)
    manifest = []
    with sqlite3.connect(
        (root / "rollback.sqlite3").as_uri() + "?mode=ro&immutable=1", uri=True
    ) as db:
        for source in args.source:
            source = source.expanduser().resolve()
            raw = source.read_bytes()
            identity = hashlib.sha256(str(source).encode()).hexdigest()[:16]
            destination = source_root / identity / source.name
            destination.parent.mkdir()
            destination.write_bytes(raw)
            count = db.execute(
                "SELECT count(*) FROM chunks WHERE source_file=?", (str(source),)
            ).fetchone()[0]
            manifest.append(
                {
                    "source": str(source),
                    "clone": str(destination),
                    "sha256": hashlib.sha256(raw).hexdigest(),
                    "bytes": len(raw),
                    "previous_chunks": count,
                }
            )
    config = {
        "embedding": {"provider": "onnx", "model": "multilingual-e5-small"},
        "indexing": {"memory_dirs": [str(source_root)], "extract_entities": False},
        "storage": {"sqlite_path": str(root / "e5.sqlite3")},
    }
    (root / "e5-config.json").write_text(json.dumps(config, indent=2) + "\n")
    (root / "manifest.json").write_text(
        json.dumps(
            {
                "status": "CLONE_ONLY",
                "production_database": str(original),
                "config_sha256": hashlib.sha256(config_bytes).hexdigest(),
                "scope": "Evaluation subset only; metadata-preserving production migration requires review",
                "sources": manifest,
            },
            indent=2,
        )
        + "\n"
    )
    print(f"Prepared {len(manifest)} sources; production unchanged; private output: {root}")


if __name__ == "__main__":
    main()
