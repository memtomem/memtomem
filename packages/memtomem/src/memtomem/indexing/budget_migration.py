"""Reviewed, resumable chunk replacement without changing source files or settings.

Plan/apply: python -m memtomem.indexing.budget_migration --help.
Reports contain metadata and hashes only. Per-source receipts commit with chunks.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memtomem.chunking.bounded import TokenBudget
from memtomem.config_signature import build_fresh_config
from memtomem.indexing.budget_audit import audit
from memtomem.indexing.engine import IndexEngine
from memtomem.indexing.privacy_projection import POLICY_VERSION
from memtomem.models import Chunk


def digest(value: Any) -> str:
    def canonical(item: Any) -> Any:
        if isinstance(item, dict):
            return {str(key): canonical(child) for key, child in item.items()}
        if isinstance(item, (set, frozenset)):
            return sorted(
                (canonical(child) for child in item),
                key=lambda child: json.dumps(child, sort_keys=True, default=str),
            )
        if isinstance(item, (tuple, list)):
            return [canonical(child) for child in item]
        if hasattr(item, "get_secret_value"):
            return item.get_secret_value()
        return item

    return hashlib.sha256(
        json.dumps(canonical(value), sort_keys=True, default=str).encode()
    ).hexdigest()


def source_hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def state_hash(db: sqlite3.Connection, source: Path) -> str:
    wanted = [
        "id",
        "content_hash",
        "heading_hierarchy",
        "start_line",
        "end_line",
        "tags",
        "namespace",
        "scope",
        "project_root",
        "origin",
        "valid_from_unix",
        "valid_to_unix",
        "retrieval_context",
        "redaction_count",
    ]
    defaults = {"retrieval_context": "", "redaction_count": 0}
    cursor = db.execute("SELECT * FROM chunks WHERE source_file=? ORDER BY id", (str(source),))
    columns = [item[0] for item in cursor.description]
    rows = []
    for row in cursor:
        values = dict(zip(columns, row, strict=True))
        rows.append([values.get(name, defaults.get(name)) for name in wanted])
    return digest(rows)


def private_json(path: Path, value: Any) -> None:
    """Atomic owner-only report replacement; no source text in reports."""
    import tempfile

    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def preserve_metadata(chunks: list[Chunk], previous: list[Chunk]) -> list[Chunk]:
    for chunk in chunks:
        matches = [
            old
            for old in previous
            if old.metadata.end_line == 0
            or (
                old.metadata.start_line <= chunk.metadata.end_line
                and old.metadata.end_line >= chunk.metadata.start_line
            )
        ] or previous
        stamps = {
            (
                c.metadata.namespace,
                c.metadata.scope,
                c.metadata.project_root,
                c.metadata.origin,
                c.metadata.valid_from_unix,
                c.metadata.valid_to_unix,
            )
            for c in matches
        }
        if len(stamps) != 1:
            raise ValueError("ambiguous existing metadata; source retained")
        namespace, scope, project_root, origin, valid_from, valid_to = stamps.pop()
        chunk.metadata = replace(
            chunk.metadata,
            namespace=namespace,
            scope=scope,
            project_root=project_root,
            origin=origin,
            valid_from_unix=valid_from,
            valid_to_unix=valid_to,
            tags=tuple(
                sorted(
                    set(chunk.metadata.tags).union(
                        tag for old in matches for tag in old.metadata.tags
                    )
                )
            ),
        )
    return chunks


def make_plan(config: Any, omitted: set[str]) -> dict[str, Any]:
    path = config.storage.sqlite_path.expanduser().resolve()
    plan = audit(path, config.indexing, omitted)
    plan.update(
        version=1,
        policy=POLICY_VERSION,
        db_path=str(path),
        config_hash=digest(config.model_dump(mode="python")),
    )
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        for entry in plan["reindex"]:
            entry["state_hash"] = state_hash(db, Path(entry["source"]))
    plan["manifest_id"] = digest(plan)
    return plan


class ReviewedEngine(IndexEngine):
    def __init__(self, *args: Any, manifest: dict[str, Any], **kwargs: Any):
        super().__init__(*args, **kwargs)
        self.manifest = manifest
        self.entries = {entry["source"]: entry for entry in manifest["reindex"]}
        self.previous: list[Chunk] = []

    def receipt_key(self, path: Path) -> str:
        return "budget_migration:" + self.manifest["manifest_id"] + ":" + digest(str(path))

    async def _validate_source_commit(self, file_path: Path) -> None:
        entry = self.entries[str(file_path)]
        if source_hash(file_path) != entry["source_sha256"]:
            raise ValueError("source changed since preview")
        if (
            digest(build_fresh_config(migrate=False).model_dump(mode="python"))
            != self.manifest["config_hash"]
        ):
            raise ValueError("configuration changed since preview")
        if state_hash(self._storage._get_db(), file_path) != entry["state_hash"]:
            raise ValueError("stored source changed since preview")

    async def _record_source_commit(self, file_path: Path) -> None:
        if source_hash(file_path) != self.entries[str(file_path)]["source_sha256"]:
            raise ValueError("source changed before commit")
        self._storage._set_meta(
            self.receipt_key(file_path), state_hash(self._storage._get_db(), file_path)
        )

    async def _index_file(self, file_path: Path, *args: Any, **kwargs: Any) -> Any:
        # Called under the engine's cross-process source lock.
        await self._validate_source_commit(file_path)
        self.previous = await self._storage.list_chunks_by_source(file_path, limit=None)
        if not self.previous:
            raise ValueError("reviewed source no longer indexed")
        scope, project_root = self._resolve_scope(file_path)
        if {(c.metadata.scope, c.metadata.project_root) for c in self.previous} != {
            (scope, project_root)
        }:
            raise ValueError("current scope differs from stored scope")
        if len({c.metadata.namespace for c in self.previous}) != 1:
            raise ValueError("mixed source namespaces require separate review")
        return await super()._index_file(file_path, *args, **kwargs)

    def _chunk_projected_content(self, path: Path, content: str) -> list[Chunk]:
        chunks = super()._chunk_projected_content(path, content)
        return preserve_metadata(chunks, self.previous)


async def apply_plan(config: Any, plan: dict[str, Any], report: Path) -> dict[str, Any]:
    from memtomem.runtime.components import close_components, create_components

    manifest_id = plan["manifest_id"]
    if digest({k: v for k, v in plan.items() if k != "manifest_id"}) != manifest_id:
        raise ValueError("manifest digest mismatch")
    if (
        plan["policy"] != POLICY_VERSION
        or digest(config.model_dump(mode="python")) != plan["config_hash"]
    ):
        raise ValueError("policy or configuration changed; generate a new preview")
    if (
        source_hash(Path(config.indexing.chunk_tokenizer_path).expanduser())
        != plan["tokenizer_sha256"]
    ):
        raise ValueError("tokenizer changed since preview")
    db_path = config.storage.sqlite_path.expanduser().resolve()
    if str(db_path) != plan["db_path"]:
        raise ValueError("database path changed")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup = report.with_name("before-budget-migration-" + stamp + ".db")
    fd = os.open(backup, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    with sqlite3.connect(db_path.as_uri() + "?mode=ro", uri=True) as src:
        with sqlite3.connect(backup) as dst:
            src.backup(dst)
    # Process-local controls only. Persisted configuration is never rewritten.
    runtime = config.model_copy(deep=True)
    runtime.storage.sqlite_path = db_path
    runtime.llm.enabled = False
    runtime.rerank.enabled = False
    runtime.embedding.onnx_batch_size = 1
    runtime.embedding.batch_size = 1
    components = await create_components(runtime, load_ambient_config=False, entity_backfill=False)
    result: dict[str, Any] = {"manifest_id": manifest_id, "backup": str(backup), "files": []}
    budget = TokenBudget(config.indexing)
    engine = ReviewedEngine(
        components.storage,
        components.embedder,
        config.indexing,
        namespace_config=config.namespace,
        generation=components.index_engine._generation,
        manifest=plan,
    )
    try:
        for entry in plan["reindex"]:
            path = Path(entry["source"])
            status: dict[str, Any] = {"source": str(path)}
            try:
                if "error" in entry or entry.get("privacy_decision") in {
                    "blocked",
                    "blocked_project_shared",
                }:
                    raise ValueError("preview refused this source")
                receipt = components.storage._get_meta(engine.receipt_key(path))
                current = state_hash(components.storage._get_db(), path)
                if receipt and receipt == current and source_hash(path) == entry["source_sha256"]:
                    status["status"] = "already_completed"
                else:
                    stats = await engine.index_file(path, force=True, path_scope="explicit")
                    if stats.errors:
                        raise ValueError("indexing did not complete")
                    after = await components.storage.list_chunks_by_source(path, limit=None)
                    for chunk in after:
                        budget.validate(chunk)
                    if source_hash(path) != entry["source_sha256"]:
                        raise ValueError("source changed after commit; replan required")
                    status.update(status="ok", chunks=len(after))
            except Exception as exc:
                status.update(status="error", error_type=type(exc).__name__)
            result["files"].append(status)
            private_json(report, result)
            print(
                json.dumps(
                    {
                        "done": len(result["files"]),
                        "total": len(plan["reindex"]),
                        "status": status["status"],
                    }
                ),
                flush=True,
            )
        result["completed"] = all(
            r["status"] in {"ok", "already_completed"} for r in result["files"]
        )
        private_json(report, result)
    finally:
        await close_components(components)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["plan", "apply"])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--omit-source", action="append", default=[])
    args = parser.parse_args()
    config = build_fresh_config(migrate=False)
    if args.mode == "plan":
        plan = make_plan(config, {str(Path(p).expanduser().resolve()) for p in args.omit_source})
        private_json(args.manifest, plan)
        print(json.dumps({"sources": len(plan["reindex"]), "manifest_id": plan["manifest_id"]}))
    else:
        if args.report is None:
            parser.error("--report is required for apply")
        result = asyncio.run(apply_plan(config, json.loads(args.manifest.read_text()), args.report))
        if not result["completed"]:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
