"""Verify private evaluation output without treating it as a migration."""

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

import sqlite_vec
from memtomem.chunking.bounded import TokenBudget
from memtomem.config import Mem2MemConfig


def verify(root, live_config):
    manifest = json.loads((root / "manifest.json").read_text())
    preflight = json.loads((root / "preflight.json").read_text())
    config = Mem2MemConfig(**json.loads((root / "full-C/runtime-config.json").read_text()))
    budget = TokenBudget(config.indexing)
    with closing(sqlite3.connect((root / "full-C/index.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.enable_load_extension(True)
        sqlite_vec.load(db)
        db.enable_load_extension(False)
        db.execute("PRAGMA query_only=ON")
        maximum = 0
        violations = 0
        for body, context, hierarchy in db.execute(
            "SELECT content,retrieval_context,heading_hierarchy FROM chunks"
        ):
            context = context or " > ".join(json.loads(hierarchy))
            text = context + "\n\n" + body if context else body
            count = budget.count(text, special=True)
            maximum = max(maximum, count)
            violations += count > 512
        counts = {
            table: db.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in [
                "chunks",
                "chunks_vec",
                "chunks_fts",
                "source_index_receipts",
                "chunk_links",
                "chunk_entities",
                "namespace_metadata",
                "sessions",
                "session_events",
            ]
        }
        missing_vectors = db.execute(
            "SELECT count(*) FROM chunks c LEFT JOIN chunks_vec v ON v.rowid=c.rowid WHERE v.rowid IS NULL"
        ).fetchone()[0]
        missing_fts = db.execute(
            "SELECT count(*) FROM chunks c LEFT JOIN chunks_fts f ON f.rowid=c.rowid WHERE f.rowid IS NULL"
        ).fetchone()[0]
        check = db.execute("PRAGMA quick_check").fetchall()
        foreign_keys = db.execute("PRAGMA foreign_key_check").fetchall()
    drift = []
    for item in manifest["sources"]:
        try:
            current = hashlib.sha256(Path(item["source"]).read_bytes()).hexdigest()
        except OSError:
            current = None
        if current != item.get("sha256"):
            drift.append(item["id"])
    original_config = live_config.expanduser().resolve()
    config_unchanged = (
        hashlib.sha256(original_config.read_bytes()).hexdigest() == manifest["config_sha256"]
    )
    report = {
        "status": "EVALUATION_ONLY_MIGRATION_HOLD",
        "counts": counts,
        "max_final_input_tokens": maximum,
        "input_violations": violations,
        "missing_vectors": missing_vectors,
        "missing_fts": missing_fts,
        "quick_check": check,
        "foreign_key_errors": foreign_keys,
        "live_config_unchanged": config_unchanged,
        "source_drift_count": len(drift),
        "source_drift_ids": drift,
        "migration_blockers": preflight["summary"],
        "metadata_note": "Fresh evaluation DB intentionally has no migrated links/session history; never replace production with it",
    }
    (root / "integrity.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: v for k, v in report.items() if k != "source_drift_ids"}))
    if violations or missing_vectors or missing_fts or check != [("ok",)] or foreign_keys:
        raise SystemExit("Evaluation integrity check failed")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--live-config", type=Path, required=True)
    args = parser.parse_args()
    verify(args.snapshot.resolve(), args.live_config)
