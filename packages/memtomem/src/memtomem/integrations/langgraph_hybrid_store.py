"""Persistent LangGraph Store with filtered BM25, exact dense search and RRF."""

from __future__ import annotations

import asyncio
import logging
import math
import struct
import threading
import warnings
from collections.abc import Awaitable, Callable, Iterable
from concurrent.futures import Future, wait
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast

try:
    from langgraph.store.base import (
        BaseStore,
        GetOp,
        Item,
        ListNamespacesOp,
        Op,
        PutOp,
        Result,
        SearchItem,
        SearchOp,
        TTLConfig,
        _validate_namespace,
        get_text_at_path,
    )
    from langgraph.store.base.embed import ensure_embeddings
except ImportError as exc:
    raise ImportError("MemtomemHybridStore requires pip install 'memtomem[langgraph]'") from exc

from memtomem import privacy
from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides
from memtomem.integrations.langgraph import _resolve_target_scope
from memtomem.storage.hybrid_store import HybridDatabase, encode, validate_filter

logger = logging.getLogger(__name__)

# The embedding calls this context is inside. A ContextVar is what "a callback
# is in flight" actually means here: thread identity cannot express it, because
# a sync embedder runs on the loop's default executor (a foreign thread) while
# an async one runs on the loop thread itself, so either check passes in one
# direction and the reentrant call then blocks forever on the lock its own
# caller holds (#2365). The marker survives both hops that
# separate the callback from the guard: langchain_core's run_in_executor
# wraps the sync call in copy_context().run, and asyncio.to_thread copies the
# context by contract. Each entry names its store, so a callback that reaches
# a *different* store is still allowed. What no such marker can reach is an
# embedder that dispatches to an executor of its own without copying the
# context -- langchain_core skips the copy when handed an explicit executor --
# so reentry from there still deadlocks, and the guide says so.
_EMBEDDING_CALLBACKS: ContextVar[tuple[_Callback, ...]] = ContextVar(
    "memtomem_hybrid_store_embedding_callback", default=()
)


class _Callback:
    """One embedding call, and whether it is still running.

    Resetting the ContextVar cannot reach a context that was *copied* from the
    callback -- a task spawned inside it keeps the tuple it inherited -- so the
    marker carries its own liveness and the refusal ends when the call does
    rather than outliving it for whatever the callback started.
    """

    __slots__ = ("running", "store")

    def __init__(self, store: MemtomemHybridStore):
        self.store = store
        self.running = True


def _positive(value: Any, name: str) -> None:
    if (
        not isinstance(value, (int, float))
        or isinstance(value, bool)
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be finite and positive")


class MemtomemHybridStore(BaseStore):
    """An explicit-path SQLite source of truth, independent of Core memories.

    ``retrieval`` accepts mode (hybrid/bm25/dense), weights ([bm25,dense]), and
    rrf_k. Embeddings require a stable ``index_id`` identifying the model and
    preprocessing contract. Caller-owned embedding clients are not closed.
    Batch operations commit individually; import commits as a single unit.
    Accepted operations drain on close, including cancelled async callers.
    """

    supports_ttl = True

    def __init__(
        self,
        path: str | Path,
        *,
        index: dict | None = None,
        retrieval: dict | None = None,
        ttl: TTLConfig | None = None,
        index_id: str | None = None,
        scope: str = "user",
        confirm_project_shared: bool = False,
        force_unsafe: bool = False,
    ):
        self.path = Path(path).expanduser().resolve()
        self.index = dict(index or {})
        self.fields = self.index.get("fields", ["$"])
        if not isinstance(self.fields, list) or not all(
            isinstance(f, str) and f for f in self.fields
        ):
            raise ValueError("index fields must be a list of nonempty paths")
        self.fields = list(self.fields)
        self.dims = self.index.get("dims")
        if self.index.get("embed") is not None:
            if not isinstance(self.dims, int) or isinstance(self.dims, bool) or self.dims <= 0:
                raise ValueError("Embedding dims must be a positive integer")
            if not isinstance(index_id, str) or not index_id.strip():
                raise ValueError("Explicit index_id is required for persistent embeddings")
        elif self.dims is not None:
            raise ValueError("dims requires embed")
        self.retrieval = {"mode": "hybrid", "weights": [1.0, 1.0], "rrf_k": 60, **(retrieval or {})}
        if self.retrieval["mode"] not in {"bm25", "dense", "hybrid"}:
            raise ValueError("Unknown retrieval mode")
        weights = self.retrieval["weights"]
        if (
            len(weights) != 2
            or any(not math.isfinite(w) or w < 0 for w in weights)
            or not any(weights)
        ):
            raise ValueError("weights require two finite nonnegative values, at least one positive")
        self.retrieval["weights"] = list(weights)
        _positive(self.retrieval["rrf_k"], "rrf_k")
        self.ttl_config = cast(TTLConfig, dict(ttl or {}))
        for setting in ("default_ttl", "sweep_interval_minutes"):
            if self.ttl_config.get(setting) is not None:
                _positive(self.ttl_config[setting], setting)
        if scope not in {"user", "project_local", "project_shared"}:
            raise ValueError("Unknown scope")
        config = Mem2MemConfig()
        load_config_d(config)
        load_config_overrides(config, migrate=False)
        inferred = _resolve_target_scope(
            self.path, config.indexing.memory_dirs, config.indexing.project_memory_dirs
        )
        self.scope = "project_shared" if "project_shared" in (scope, inferred) else inferred
        self.force_unsafe = force_unsafe
        if self.scope == "project_shared":
            if not confirm_project_shared:
                raise ValueError("project_shared requires confirm_project_shared=True")
            privacy.emit_project_shared_confirmation(
                surface="langgraph_hybridstore_init",
                mechanism="param",
                action="init",
                audit_context={"scope_inferred_from_path": scope != inferred},
            )
        self._configuration = {"fields": self.fields, "dims": self.dims, "index_id": index_id}
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._state_lock = threading.Lock()
        self._closed = False
        self._close_done = threading.Event()
        self._pending: set[Future[Any]] = set()
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, name="memtomem-hybrid-store", daemon=True)
        self._thread.start()
        try:
            self._submit(self._initialize).result()
        except BaseException:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            raise

    def _run(self):
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_forever()
        finally:
            self._loop.run_until_complete(self._loop.shutdown_asyncgens())
            self._loop.run_until_complete(self._loop.shutdown_default_executor())
            self._loop.close()

    async def _initialize(self):
        self._embedder = (
            ensure_embeddings(self.index["embed"]) if self.index.get("embed") is not None else None
        )
        self._database = HybridDatabase(self.path, self._configuration)
        self._operations = asyncio.Lock()
        self._sweeper = None
        if self.ttl_config.get("sweep_interval_minutes") is not None:
            self._sweeper = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self):
        # A sweep that fails (a busy-timeout against another writer, say) must
        # not end periodic cleanup for the life of the store, and its error
        # must not surface later out of close(). Log it and try again on the
        # next tick; sweep_ttl() still reports the same error synchronously.
        while True:
            await asyncio.sleep(self.ttl_config["sweep_interval_minutes"] * 60)
            try:
                self._database.sweep()
            except Exception:
                logger.warning(
                    "TTL sweep failed for %s; retrying on the next interval",
                    self.path,
                    exc_info=True,
                )

    async def _embed(self, method: Callable[[Any], Awaitable[Any]], argument: Any) -> Any:
        """Await an embedder with this store marked as the in-flight callback."""
        callback = _Callback(self)
        token = _EMBEDDING_CALLBACKS.set((*_EMBEDDING_CALLBACKS.get(), callback))
        try:
            return await method(argument)
        finally:
            callback.running = False
            _EMBEDDING_CALLBACKS.reset(token)

    def _inside_own_callback(self) -> bool:
        return any(
            callback.store is self and callback.running for callback in _EMBEDDING_CALLBACKS.get()
        )

    def _on_own_loop(self, refusal: str) -> None:
        """Refuse a wait that only the loop running it could satisfy.

        The callback marker cannot stand in for this. Code the callback started
        outlives it -- a task an async embedder spawns keeps running on the
        store's loop after the marker is cleared -- and a synchronous call from
        there waits on a future only that same loop can complete. Ordinary
        async callers on the loop are fine: they suspend instead of blocking,
        so this belongs to the waits rather than to _submit. Shutdown is the
        exception among them, which is why aclose() asks too: offloading it
        leaves the worker joining the store thread while the store thread waits
        for that worker's executor.
        """
        if threading.current_thread() is self._thread:
            raise RuntimeError(refusal)

    _NO_BLOCKING = (
        "Cannot run a synchronous store operation from the store's event loop; await the async API"
    )
    # "another thread" would be wrong advice: handing close() to a worker of
    # the store's *own* loop deadlocks the same way, and no guard here sees
    # that hop. The safe caller is the code that owns the store.
    _NO_CLOSING = (
        "Cannot close the store from its own event loop; "
        "close it from outside the store's own threads"
    )

    def _blocking(self, function, *args):
        self._on_own_loop(self._NO_BLOCKING)
        return self._submit(function, *args).result()

    def _submit(self, function, *args):
        if self._inside_own_callback():
            raise RuntimeError("Store operations cannot reenter from an embedding callback")
        with self._state_lock:
            if self._closed:
                raise RuntimeError("Store is closed")
            future = asyncio.run_coroutine_threadsafe(function(*args), self._loop)
            self._pending.add(future)

        def done(completed):
            with self._state_lock:
                self._pending.discard(completed)

        future.add_done_callback(done)
        return future

    async def _await(self, function, *args):
        return await asyncio.shield(asyncio.wrap_future(self._submit(function, *args)))

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        return self._blocking(self._batch, list(ops))

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        return await self._await(self._batch, list(ops))

    def _vectors(self, vectors, count):
        if len(vectors) != count:
            raise ValueError("Embedding count mismatch")
        normalized = []
        for vector in vectors:
            if (
                len(vector) != self.dims
                or not all(math.isfinite(x) for x in vector)
                or not any(vector)
            ):
                raise ValueError(
                    "Embedding must have configured dimensions and finite nonzero norm"
                )
            # Normalize before float32 storage to avoid overflow/underflow in cosine.
            magnitude = math.hypot(*vector)
            if not math.isfinite(magnitude):
                raise ValueError("Embedding norm must be finite")
            unit = [x / magnitude for x in vector]
            normalized.append(
                list(struct.unpack(f"{len(unit)}f", struct.pack(f"{len(unit)}f", *unit)))
            )
        return normalized

    async def _prepare(self, op: PutOp) -> dict:
        _validate_namespace(op.namespace)
        if not isinstance(op.key, str) or not op.key:
            raise ValueError("key must be a nonempty string")
        if op.ttl is not None:
            _positive(op.ttl, "ttl")
        if (
            op.index is not None
            and op.index is not False
            and (
                not isinstance(op.index, list)
                or not all(isinstance(f, str) and f for f in op.index)
            )
        ):
            raise ValueError("index must be None, False, or a list of paths")
        record = {
            "namespace": op.namespace,
            "key": op.key,
            "value": op.value,
            "ttl": op.ttl,
            "index": op.index,
            "text": "",
            "vectors": [],
        }
        if op.value is None:
            return record
        if not isinstance(op.value, dict):
            raise ValueError("Store value must be a JSON object")
        serialized = encode(op.value)
        guard = privacy.enforce_write_guard(
            serialized,
            surface="langgraph_hybridstore_put",
            scope=self.scope,
            force_unsafe=self.force_unsafe,
        )
        if guard.decision != "pass" and not (
            guard.decision == "bypassed" and self.scope != "project_shared"
        ):
            raise ValueError("Store write blocked by privacy guard")
        # Detach caller-owned values before awaiting external embedding work.
        import json

        record["value"] = json.loads(serialized)
        if op.index is not False:
            texts = []
            for field in self.fields if op.index is None else op.index:
                texts.extend(get_text_at_path(record["value"], field))
            record["text"] = "\n".join(texts)
            if self._embedder is not None and texts:
                record["vectors"] = self._vectors(
                    await self._embed(self._embedder.aembed_documents, texts), len(texts)
                )
        return record

    @staticmethod
    def _item(row: dict, search: bool = False):
        import json

        values = dict(
            namespace=tuple(json.loads(row["namespace"])),
            key=row["key"],
            value=json.loads(row["value_json"]),
            created_at=datetime.fromtimestamp(row["created_at"], timezone.utc),
            updated_at=datetime.fromtimestamp(row["updated_at"], timezone.utc),
        )
        return SearchItem(**values, score=row.get("score")) if search else Item(**values)

    async def _search(self, op: SearchOp):
        validate_filter(op.filter)
        mode = self.retrieval["mode"]
        vector, fallback = None, None
        if op.query is not None and mode in {"dense", "hybrid"}:
            if self._embedder is None:
                if mode == "dense":
                    raise ValueError("dense mode requires embeddings")
                mode = "bm25"
                fallback = "embeddings_not_configured"
            elif mode == "dense" or self.retrieval["weights"][1] > 0:
                try:
                    vector = self._vectors(
                        [await self._embed(self._embedder.aembed_query, op.query)], 1
                    )[0]
                except Exception:
                    if mode == "dense":
                        raise
                    warnings.warn(
                        "Query embedding failed; using BM25", RuntimeWarning, stacklevel=2
                    )
                    mode, fallback = "bm25", "query_embedding_failed"
        rows, diagnostics = self._database.search(
            op.namespace_prefix,
            op.filter,
            op.query,
            vector,
            mode,
            op.limit,
            op.offset,
            self.retrieval["weights"],
            self.retrieval["rrf_k"],
            op.refresh_ttl,
        )
        diagnostics["fallback_reason"] = fallback
        return {"items": [self._item(row, True) for row in rows], "diagnostics": diagnostics}

    async def _batch(self, ops):
        results = []
        async with self._operations:
            for op in ops:
                if isinstance(op, PutOp):
                    self._database.write([await self._prepare(op)])
                    results.append(None)
                elif isinstance(op, GetOp):
                    row = self._database.get(op.namespace, op.key, op.refresh_ttl)
                    results.append(self._item(row) if row else None)
                elif isinstance(op, SearchOp):
                    results.append((await self._search(op))["items"])
                elif isinstance(op, ListNamespacesOp):
                    if (
                        op.limit < 0
                        or op.offset < 0
                        or (op.max_depth is not None and op.max_depth < 0)
                    ):
                        raise ValueError("namespace limits must be nonnegative")
                    namespaces = self._database.namespaces()
                    for condition in op.match_conditions or ():
                        if condition.match_type not in {"prefix", "suffix"}:
                            raise ValueError("Unknown namespace match type")
                        path = condition.path
                        namespaces = {
                            ns
                            for ns in namespaces
                            if len(ns) >= len(path)
                            and all(
                                p == "*" or p == s
                                for p, s in zip(
                                    path,
                                    ns[: len(path)]
                                    if condition.match_type == "prefix"
                                    else ns[len(ns) - len(path) :],
                                    strict=True,
                                )
                            )
                        }
                    results.append(
                        sorted({ns[: op.max_depth] for ns in namespaces})[
                            op.offset : op.offset + op.limit
                        ]
                    )
                else:
                    raise TypeError(f"Unsupported Store operation: {type(op).__name__}")
        return results

    def search_with_diagnostics(
        self, namespace_prefix, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None
    ):
        return self._blocking(
            self._diagnostics,
            SearchOp(
                namespace_prefix,
                filter,
                limit,
                offset,
                query,
                self.ttl_config.get("refresh_on_read", True)
                if refresh_ttl is None
                else refresh_ttl,
            ),
        )

    async def asearch_with_diagnostics(
        self, namespace_prefix, *, query=None, filter=None, limit=10, offset=0, refresh_ttl=None
    ):
        return await self._await(
            self._diagnostics,
            SearchOp(
                namespace_prefix,
                filter,
                limit,
                offset,
                query,
                self.ttl_config.get("refresh_on_read", True)
                if refresh_ttl is None
                else refresh_ttl,
            ),
        )

    async def _diagnostics(self, op):
        async with self._operations:
            return await self._search(op)

    async def _export(self):
        import json

        async with self._operations:
            records = []
            for row in self._database.export():
                records.append(
                    {
                        "namespace": json.loads(row["namespace"]),
                        "key": row["key"],
                        "value": json.loads(row["value_json"]),
                        "created_at": row["created_at"],
                        "updated_at": row["updated_at"],
                        "revision": row["revision"],
                        "ttl": row["ttl"],
                        "expires_at": row["expires_at"],
                        "index": json.loads(row["index_json"]),
                    }
                )
            return {"format": "memtomem-hybrid-json-v1", "records": records}

    def export_json(self) -> dict:
        """Return a JSON-serializable envelope of live records; never write a file."""
        return self._blocking(self._export)

    async def aexport_json(self) -> dict:
        return await self._await(self._export)

    async def _import(self, envelope):
        if envelope.get("format") != "memtomem-hybrid-json-v1":
            raise ValueError("Unsupported export format")
        async with self._operations:
            records, identities = [], set()
            for raw in envelope["records"]:
                identity = (tuple(raw["namespace"]), raw["key"])
                if identity in identities:
                    raise ValueError("Duplicate import identity")
                identities.add(identity)
                if raw["value"] is None:
                    raise ValueError("Import requires JSON object values")
                record = await self._prepare(
                    PutOp(*identity, raw["value"], raw["index"], raw["ttl"])
                )
                for field in ("created_at", "updated_at", "expires_at"):
                    value = raw[field]
                    if value is None and field == "expires_at":
                        continue
                    if not isinstance(value, (float, int)) or not math.isfinite(value):
                        raise ValueError(f"Invalid import {field}")
                if not isinstance(raw["revision"], int) or raw["revision"] < 1:
                    raise ValueError("Invalid revision")
                if (raw["ttl"] is None) != (raw["expires_at"] is None):
                    raise ValueError("Import TTL and expiry must agree")
                record.update(
                    {f: raw[f] for f in ("created_at", "updated_at", "expires_at", "revision")}
                )
                records.append(record)
            self._database.write(records, importing=True)
            return len(records)

    def import_json(self, envelope: dict) -> int:
        return self._blocking(self._import, envelope)

    async def aimport_json(self, envelope: dict) -> int:
        return await self._await(self._import, envelope)

    async def _sweep(self):
        async with self._operations:
            return self._database.sweep()

    def sweep_ttl(self) -> int:
        return self._blocking(self._sweep)

    async def _shutdown(self):
        try:
            if self._sweeper is not None:
                self._sweeper.cancel()
                # return_exceptions keeps a sweeper that died for any reason
                # from re-raising its stale error out of close().
                await asyncio.gather(self._sweeper, return_exceptions=True)
        finally:
            self._database.close()

    def close(self) -> None:
        if self._inside_own_callback():
            raise RuntimeError("Cannot close the store from its embedding callback")
        self._on_own_loop(self._NO_CLOSING)
        with self._state_lock:
            already_closing = self._closed
            self._closed = True
            pending = list(self._pending)
        if already_closing:
            self._close_done.wait()
            return
        try:
            # Drain accepted operations. Each outcome already reached its own
            # caller; close only waits for completion, so nothing is re-raised.
            wait(pending)
            asyncio.run_coroutine_threadsafe(self._shutdown(), self._loop).result()
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join()
            self._close_done.set()

    async def aclose(self) -> None:
        # Both refusals have to happen before the offload. close() would catch
        # the callback itself, since to_thread carries the marker to the worker,
        # but answering here keeps that refusal in the frame that caused it. The
        # loop check has no second chance at all: on the worker it is false, and
        # that worker then joins the store thread while the store thread waits
        # for the executor the worker belongs to.
        if self._inside_own_callback():
            raise RuntimeError("Cannot close the store from its embedding callback")
        self._on_own_loop(self._NO_CLOSING)
        await asyncio.to_thread(self.close)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()
