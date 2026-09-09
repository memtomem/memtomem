"""Read-only full-corpus E5 preflight over a frozen private snapshot.

Never imports vectors or initializes storage. Reports each refusal and reference
mapping, rather than implying that a fresh index is a lossless migration.
"""

import argparse
from collections import Counter, defaultdict
from contextlib import closing
from dataclasses import fields
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import UUID

from memtomem.chunking.bounded import TokenBudget
from memtomem.config import Mem2MemConfig
from memtomem.indexing.budget_migration import preserve_metadata
from memtomem.indexing.engine import IndexEngine, _build_exclude_spec, _path_is_excluded
from memtomem.indexing.redaction_exemption import declared_exemption, indexer_text
from memtomem.models import Chunk, ChunkMetadata, ChunkType


def candidate_config(raw):
    data = json.loads(json.dumps(raw))
    data["embedding"].update(
        provider="onnx",
        model="multilingual-e5-small",
        dimension=384,
        max_sequence_tokens=512,
        threads=2,
        onnx_batch_size=4,
        onnx_cpu_mem_arena=False,
        onnx_variant="fp32",
        onnx_artifact_path="",
    )
    for key in (
        "hard_max_chunk_tokens",
        "chunk_context_tokens",
        "chunk_model_tokens",
        "chunk_tokenizer_path",
        "max_chunk_tokens",
        "target_chunk_tokens",
        "min_chunk_tokens",
        "chunk_overlap_tokens",
        "chunk_input_prefix",
    ):
        data.setdefault("indexing", {}).pop(key, None)
    return Mem2MemConfig(**data)


def old_chunk(row):
    metadata = {
        field.name: row[field.name] for field in fields(ChunkMetadata) if field.name in row.keys()
    }
    for key in ("tags", "heading_hierarchy"):
        metadata[key] = tuple(json.loads(metadata.get(key, "[]") or "[]"))
    metadata["source_file"] = Path(metadata["source_file"])
    if metadata.get("project_root"):
        metadata["project_root"] = Path(metadata["project_root"])
    metadata["chunk_type"] = ChunkType(row["chunk_type"])
    return Chunk(
        id=UUID(row["id"]),
        content=row["content"],
        metadata=ChunkMetadata(**metadata),
    )


def main(root):
    os.umask(0o077)
    manifest = json.loads((root / "manifest.json").read_text())
    config = candidate_config(json.loads((root / "original-config.json").read_text()))
    budget = TokenBudget(config.indexing)
    engine = IndexEngine(None, None, config.indexing, namespace_config=config.namespace)
    entries, docs = [], []
    mappings = {}
    with closing(sqlite3.connect((root / "original.db").as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        for item in manifest["sources"]:
            entry = {"id": item["id"], "source": item["source"], "old_chunks": item["old_chunks"]}
            try:
                if item["status"] != "copied":
                    raise ValueError("capture failed")
                source = Path(item["source"])
                raw = (root / item["copy"]).read_bytes()
                if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                    raise ValueError("snapshot checksum mismatch")
                content = indexer_text(raw)
                decision = engine.preview_redaction_decision(source, content)
                entry["privacy"] = decision
                if decision.startswith("blocked"):
                    raise ValueError("privacy refusal")
                if _path_is_excluded(
                    source,
                    config.indexing.all_index_roots(),
                    _build_exclude_spec(config.indexing.exclude_patterns),
                ):
                    raise ValueError("excluded source")
                old = [
                    old_chunk(row)
                    for row in db.execute(
                        "SELECT * FROM chunks WHERE source_file=?", (str(source),)
                    )
                ]
                chunks = preserve_metadata(
                    engine.chunk_content(
                        source, content, exempt=bool(declared_exemption(source, content))
                    ),
                    old,
                )
                if not chunks:
                    raise ValueError("empty replacement")
                for chunk in chunks:
                    budget.validate(chunk)
                hashes = defaultdict(list)
                for i, chunk in enumerate(chunks):
                    key = f"{item['id']}:{i}"
                    hashes[chunk.content_hash].append(key)
                    docs.append(
                        {
                            "id": key,
                            "text": chunk.retrieval_content,
                            "source_id": item["id"],
                            "start_line": chunk.metadata.start_line,
                            "end_line": chunk.metadata.end_line,
                        }
                    )
                for chunk in old:
                    mappings[str(chunk.id)] = hashes.get(chunk.content_hash, [])
                entry.update(
                    status="ready",
                    new_chunks=len(chunks),
                    max_input_tokens=max(
                        budget.count(c.retrieval_content, special=True) for c in chunks
                    ),
                )
            except Exception as exc:
                entry.update(status="refused", error=str(exc), error_type=type(exc).__name__)
            entries.append(entry)
            if len(entries) % 200 == 0:
                print(
                    json.dumps(
                        {
                            "done": len(entries),
                            "total": len(manifest["sources"]),
                            "refused": sum(e["status"] == "refused" for e in entries),
                        }
                    ),
                    flush=True,
                )
        links = []
        for row in db.execute("SELECT source_id,target_id,link_type FROM chunk_links"):
            record = dict(row)
            record["source_candidates"] = mappings.get(row["source_id"], [])
            record["target_candidates"] = mappings.get(row["target_id"], [])
            record["unique_exact_mapping"] = (
                len(record["source_candidates"]) == 1 and len(record["target_candidates"]) == 1
            )
            links.append(record)
    summary = {
        "sources": len(entries),
        "ready": sum(e["status"] == "ready" for e in entries),
        "refused": dict(Counter(e["error"] for e in entries if e["status"] == "refused")),
        "new_chunks": len(docs),
        "links": len(links),
        "links_without_unique_exact_mapping": sum(not e["unique_exact_mapping"] for e in links),
    }
    report = {
        "status": "HOLD"
        if summary["refused"] or summary["links_without_unique_exact_mapping"]
        else "PREFLIGHT_ONLY",
        "summary": summary,
        "sources": entries,
        "links": links,
        "old_to_new_exact": mappings,
    }
    (root / "preflight.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    (root / "e5-documents.json").write_text(json.dumps(docs, ensure_ascii=False) + "\n")
    print(json.dumps({"status": report["status"], **summary}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    main(parser.parse_args().snapshot.resolve())
