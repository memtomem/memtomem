"""LangGraph ``BaseStore`` adapter with file-backed JSON as source of truth."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import re
import threading
from collections import OrderedDict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, cast

try:
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        MatchCondition,
        Op,
        PutOp,
        Result,
        SearchItem,
        SearchOp,
        get_text_at_path,
    )
except ImportError as exc:  # pragma: no cover - exercised by minimal-install smoke
    raise ImportError(
        "MemtomemBaseStore requires the langgraph extra: pip install 'memtomem[langgraph]'"
    ) from exc

from memtomem import privacy
from memtomem.config import EmbeddingConfig, Mem2MemConfig, TargetScope
from memtomem.context._atomic import atomic_write_text
from memtomem.embedding.base import EmbeddingProvider
from memtomem.embedding.factory import create_embedder
from memtomem.memory_scope import resolve_memory_scope_dir

_TOKEN_RE = re.compile(r"[\w.-]+", re.UNICODE)


def _encode_segment(value: str) -> str:
    if not value or value in {".", ".."} or "\x00" in value:
        raise ValueError("LangGraph namespace segments and keys must be non-empty and safe")
    return base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _read_path(value: Any, path: str) -> list[Any]:
    if path == "$":
        return [value]
    try:
        found = get_text_at_path(value, path)
    except (KeyError, IndexError, TypeError, ValueError):
        return []
    if found is None:
        return []
    if found is True:
        return [value]
    if found is False:
        return []
    return list(cast(list[str], found))


def _projection(value: dict[str, Any], index: bool | list[str] | None) -> str:
    if index is False:
        return ""
    fields = ["$"] if index is None or index is True else index
    parts: list[str] = []
    for field in fields:
        for found in _read_path(value, field):
            if isinstance(found, str):
                parts.append(found)
            else:
                parts.append(json.dumps(found, ensure_ascii=False, sort_keys=True))
    return "\n".join(parts)


def _lookup(value: dict[str, Any], dotted: str) -> Any:
    current: Any = value
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _compare(actual: Any, expected: Any) -> bool:
    if not isinstance(expected, dict) or not any(str(key).startswith("$") for key in expected):
        return actual == expected
    for operator, operand in expected.items():
        if operator == "$eq" and actual != operand:
            return False
        if operator == "$ne" and actual == operand:
            return False
        if operator == "$gt" and not (actual is not None and actual > operand):
            return False
        if operator == "$gte" and not (actual is not None and actual >= operand):
            return False
        if operator == "$lt" and not (actual is not None and actual < operand):
            return False
        if operator == "$lte" and not (actual is not None and actual <= operand):
            return False
        if operator == "$in" and actual not in operand:
            return False
        if operator == "$nin" and actual in operand:
            return False
    return True


def _matches_filter(value: dict[str, Any], filters: dict[str, Any] | None) -> bool:
    return not filters or all(
        _compare(_lookup(value, key), expected) for key, expected in filters.items()
    )


def _namespace_matches(namespace: tuple[str, ...], condition: MatchCondition) -> bool:
    path = condition.path
    if len(path) > len(namespace):
        return False
    candidate = (
        namespace[: len(path)] if condition.match_type == "prefix" else namespace[-len(path) :]
    )
    return all(expected == "*" or expected == actual for expected, actual in zip(path, candidate))


class MemtomemBaseStore(BaseStore):
    """A LangGraph store whose canonical records are inspectable JSON files.

    Exact operations require no embedding provider. Semantic search uses the
    configured memtomem embedder when available and degrades to lexical
    overlap for a BM25-only/minimal installation.
    """

    supports_ttl = False

    def __init__(
        self,
        *,
        root: str | Path | None = None,
        scope: TargetScope = "user",
        project_root: str | Path | None = None,
        confirm_project_shared: bool = False,
        embedding: EmbeddingConfig | None = None,
        force_unsafe: bool = False,
    ) -> None:
        if scope == "project_shared" and not confirm_project_shared:
            raise ValueError(
                "scope='project_shared' requires confirm_project_shared=True because writes are git-tracked"
            )
        config = Mem2MemConfig()
        if root is None:
            from memtomem.config import load_config_d, load_config_overrides

            load_config_d(config)
            load_config_overrides(config)
            user_base = Path(config.indexing.memory_dirs[0]).expanduser()
            base = resolve_memory_scope_dir(
                scope,
                Path(project_root).expanduser().resolve() if project_root else None,
                user_base,
            )
            root = base / "langgraph-store"
        self.root = Path(root).expanduser().resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.scope = scope
        self.force_unsafe = force_unsafe
        self._embedding_config = embedding or config.embedding
        self._embedder: EmbeddingProvider | None = None
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embedding_cache_limit = 1024
        self._lock = threading.RLock()

    def _path(self, namespace: tuple[str, ...], key: str) -> Path:
        parts = [_encode_segment(segment) for segment in namespace]
        return self.root.joinpath(*parts, f"{_encode_segment(str(key))}.json")

    def _read_record(self, path: Path) -> dict[str, Any] | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("value"), dict):
            return None
        return payload

    def _records(self) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        with self._lock:
            for path in sorted(self.root.rglob("*.json")):
                record = self._read_record(path)
                if record is not None:
                    records.append(record)
        return records

    @staticmethod
    def _item(record: dict[str, Any]) -> Item:
        return Item(
            namespace=tuple(record["namespace"]),
            key=record["key"],
            value=record["value"],
            created_at=_parse_datetime(record["created_at"]),
            updated_at=_parse_datetime(record["updated_at"]),
        )

    def _put(self, op: PutOp) -> None:
        path = self._path(op.namespace, op.key)
        with self._lock:
            if op.value is None:
                path.unlink(missing_ok=True)
                current = path.parent
                while current != self.root:
                    try:
                        current.rmdir()
                    except OSError:
                        break
                    current = current.parent
                return
            serialized = json.dumps(op.value, ensure_ascii=False, sort_keys=True)
            guard = privacy.enforce_write_guard(
                serialized,
                surface="langgraph_basestore_put",
                scope=self.scope,
                force_unsafe=self.force_unsafe,
            )
            if guard.decision != "pass" and not (
                guard.decision == "bypassed" and self.scope != "project_shared"
            ):
                raise ValueError(
                    f"LangGraph store write blocked by {len(guard.hits)} privacy pattern(s)"
                )
            existing = self._read_record(path)
            now = datetime.now(timezone.utc).isoformat(timespec="microseconds")
            record = {
                "schema_version": 1,
                "namespace": list(op.namespace),
                "key": op.key,
                "value": op.value,
                "index": op.index,
                "projection": _projection(op.value, op.index),
                "created_at": existing["created_at"] if existing else now,
                "updated_at": now,
            }
            atomic_write_text(path, json.dumps(record, ensure_ascii=False, indent=2) + "\n")

    def _get(self, op: GetOp) -> Item | None:
        with self._lock:
            record = self._read_record(self._path(op.namespace, op.key))
        return self._item(record) if record else None

    def _list_namespaces(self, op: ListNamespacesOp) -> list[tuple[str, ...]]:
        namespaces = {tuple(record["namespace"]) for record in self._records()}
        if op.match_conditions:
            namespaces = {
                namespace
                for namespace in namespaces
                if all(
                    _namespace_matches(namespace, condition) for condition in op.match_conditions
                )
            }
        if op.max_depth is not None:
            namespaces = {namespace[: op.max_depth] for namespace in namespaces}
        return sorted(namespaces)[op.offset : op.offset + op.limit]

    async def _ensure_embedder(self) -> EmbeddingProvider:
        if self._embedder is None:
            self._embedder = create_embedder(self._embedding_config)
        return self._embedder

    async def _dense_scores(self, query: str, records: list[dict[str, Any]]) -> list[float]:
        embedder = await self._ensure_embedder()
        if embedder.dimension == 0:
            return [0.0] * len(records)
        query_vector = await embedder.embed_query(query)
        projections = [record.get("projection", "") for record in records]
        missing = list(
            dict.fromkeys(
                text for text in projections if text and text not in self._embedding_cache
            )
        )
        if missing:
            vectors = await embedder.embed_texts(missing)
            for text, vector in zip(missing, vectors):
                self._embedding_cache[text] = vector
                self._embedding_cache.move_to_end(text)
                if len(self._embedding_cache) > self._embedding_cache_limit:
                    self._embedding_cache.popitem(last=False)
        scores: list[float] = []
        qnorm = math.sqrt(sum(value * value for value in query_vector)) or 1.0
        for text in projections:
            vector = self._embedding_cache.get(text, [])
            if not vector:
                scores.append(0.0)
                continue
            self._embedding_cache.move_to_end(text)
            vnorm = math.sqrt(sum(value * value for value in vector)) or 1.0
            scores.append(sum(a * b for a, b in zip(query_vector, vector)) / (qnorm * vnorm))
        return scores

    async def _search(self, op: SearchOp) -> list[SearchItem]:
        records = [
            record
            for record in self._records()
            if tuple(record["namespace"][: len(op.namespace_prefix)]) == op.namespace_prefix
            and _matches_filter(record["value"], op.filter)
            and (not op.query or bool(record.get("projection")))
        ]
        scores: list[float | None]
        if op.query:
            query_tokens = {token.lower() for token in _TOKEN_RE.findall(op.query)}
            lexical = []
            for record in records:
                tokens = {
                    token.lower() for token in _TOKEN_RE.findall(record.get("projection", ""))
                }
                lexical.append(len(query_tokens & tokens) / max(1, len(query_tokens)))
            dense = await self._dense_scores(op.query, records)
            scores = [(left + right) / 2 for left, right in zip(lexical, dense)]
            paired = sorted(zip(records, scores), key=lambda pair: pair[1] or 0.0, reverse=True)
        else:
            scores = [None] * len(records)
            paired = sorted(
                zip(records, scores), key=lambda pair: pair[0]["updated_at"], reverse=True
            )
        return [
            SearchItem(
                namespace=tuple(record["namespace"]),
                key=record["key"],
                value=record["value"],
                created_at=_parse_datetime(record["created_at"]),
                updated_at=_parse_datetime(record["updated_at"]),
                score=score,
            )
            for record, score in paired[op.offset : op.offset + op.limit]
        ]

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abatch(ops))
        raise RuntimeError("Use await store.abatch()/aget()/aput() inside an active event loop")

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        results: list[Result] = []
        for op in list(ops):
            if isinstance(op, PutOp):
                await asyncio.to_thread(self._put, op)
                results.append(None)
            elif isinstance(op, GetOp):
                results.append(await asyncio.to_thread(self._get, op))
            elif isinstance(op, SearchOp):
                results.append(await self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(await asyncio.to_thread(self._list_namespaces, op))
            else:  # pragma: no cover - future LangGraph op must fail closed
                raise TypeError(f"unsupported LangGraph store operation: {type(op).__name__}")
        return results

    async def aclose(self) -> None:
        if self._embedder is not None:
            await self._embedder.close()
            self._embedder = None
            self._embedding_cache.clear()

    def close(self) -> None:
        if self._embedder is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError("Use await store.aclose() inside an active event loop")
