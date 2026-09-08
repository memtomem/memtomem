"""Read-only token audit / reindex preview: python -m memtomem.indexing.budget_audit.

Reports contain paths and hashes, no bodies. Never loads an embedding session.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from memtomem.chunking.bounded import TokenBudget
from memtomem.config import IndexingConfig
from memtomem.indexing.engine import IndexEngine, _build_exclude_spec, _path_is_excluded

CODE_SUFFIXES = {".py", ".js", ".ts", ".jsx", ".tsx", ".mjs"}


def audit(db_path: Path, config: IndexingConfig, omitted: set[str]) -> dict[str, Any]:
    budget = TokenBudget(config)
    sources: dict[str, list] = defaultdict(list)
    oversized = []
    invalid_inputs = []
    maximum = 0
    total = 0
    with sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.execute("PRAGMA query_only=ON")
        columns = {r[1] for r in db.execute("PRAGMA table_info(chunks)")}
        context_column = "retrieval_context" if "retrieval_context" in columns else "''"
        rows = db.execute(
            "SELECT id, source_file, content, content_hash, chunk_type, namespace, scope, "
            "heading_hierarchy, "
            + context_column
            + " FROM chunks ORDER BY source_file, start_line, id"
        )
        for row in rows:
            # Stream bodies; retain only the small metadata needed by the preview.
            sources[row[1]].append((*row[:2], None, *row[3:7]))
            total += 1
            count = budget.count(row[2])
            maximum = max(maximum, count)
            if count > budget.body:
                oversized.append({"id": row[0], "source": row[1], "type": row[4], "tokens": count})
            prefix = row[8] or " > ".join(json.loads(row[7]))
            composed = prefix + "\n\n" + row[2] if prefix else row[2]
            if (
                budget.count(prefix) > budget.context
                or budget.count(composed, special=True) > budget.model
            ):
                invalid_inputs.append({"id": row[0], "source": row[1]})
    candidates = {r["source"] for r in oversized}
    candidates.update(r["source"] for r in invalid_inputs)
    candidates.update(s for s in sources if Path(s).suffix.lower() in CODE_SUFFIXES | {".json"})
    spec = _build_exclude_spec(config.exclude_patterns)
    excluded = {s for s in sources if _path_is_excluded(Path(s), config.all_index_roots(), spec)}
    # Discovery mode never touches these dependencies; see chunk_content's contract.
    engine = IndexEngine(None, None, config)  # type: ignore[arg-type]
    preview: list[dict[str, Any]] = []
    for source in sorted(candidates - excluded - omitted):
        path = Path(source)
        entry: dict[str, Any] = {
            "source": source,
            "old_chunks": len(sources[source]),
            "namespaces": sorted({r[5] for r in sources[source]}),
            "scopes": sorted({r[6] for r in sources[source]}),
        }
        try:
            raw = path.read_bytes()
            content = raw.decode("utf-8")
            decision = engine.preview_redaction_decision(path, content)
            entry["privacy_decision"] = decision
            if decision in {"blocked", "blocked_project_shared"}:
                entry["error"] = "PrivacyRejection"
                preview.append(entry)
                continue
            from memtomem.indexing.privacy_projection import prepare_index_content

            scope, _ = engine._resolve_scope(path)
            projection = prepare_index_content(content, scope=scope)
            entry["redaction_count"] = projection.redaction_count
            chunks = engine.chunk_content(path, content)
            entry.update(
                {
                    "source_sha256": hashlib.sha256(raw).hexdigest(),
                    "new_chunks": len(chunks),
                    "max_body_tokens": max((budget.count(c.content) for c in chunks), default=0),
                    "max_context_tokens": max(
                        (budget.count(c.metadata.retrieval_context) for c in chunks), default=0
                    ),
                    "max_input_tokens": max(
                        (budget.count(c.retrieval_content, special=True) for c in chunks), default=0
                    ),
                }
            )
            if path.suffix.lower() in CODE_SUFFIXES:
                entry["source_exact"] = "".join(c.content for c in chunks) == projection.content
        except Exception as exc:
            entry["error"] = type(exc).__name__
        preview.append(entry)
    return {
        "total_chunks": total,
        "invalid_inputs": invalid_inputs,
        "max_body_tokens": maximum,
        "oversized_chunks": oversized,
        "oversized_by_type": dict(Counter(r["type"] for r in oversized)),
        "excluded": [
            {"source": s, "chunk_ids": [r[0] for r in sources[s]]} for s in sorted(excluded)
        ],
        "omitted_sources": sorted(omitted),
        "reindex": preview,
        "tokenizer_sha256": hashlib.sha256(
            Path(config.chunk_tokenizer_path).read_bytes()
        ).hexdigest(),
        "budgets": {"body": budget.body, "context": budget.context, "model": budget.model},
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--body-tokens", type=int, default=4096)
    parser.add_argument("--exclude-pattern", action="append", default=[])
    parser.add_argument("--omit-source", action="append", default=[])
    args = parser.parse_args()
    config = IndexingConfig(
        hard_max_chunk_tokens=args.body_tokens,
        chunk_tokenizer_path=str(args.tokenizer.expanduser()),
        exclude_patterns=args.exclude_pattern,
        memory_dirs=[],
    )
    report = audit(args.db.expanduser(), config, set(args.omit_source))
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                "total_chunks": report["total_chunks"],
                "oversized": len(report["oversized_chunks"]),
                "reindex_sources": len(report["reindex"]),
                "excluded_sources": len(report["excluded"]),
                "errors": sum("error" in r for r in report["reindex"]),
                "new_max_body_tokens": max(
                    (r.get("max_body_tokens", 0) for r in report["reindex"]), default=0
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
