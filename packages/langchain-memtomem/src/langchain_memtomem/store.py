"""A LangGraph ``BaseStore`` whose ``search(query=...)`` runs the memtomem pipeline.

ADR-0042. Each item is a JSON record (``_records.py``, the source of truth),
an indexed item also has one Markdown projection, and the projection is
indexed by a memtomem component stack the store builds for itself under
``root``. The developer's own memtomem index is never touched.

The order of every mutation, what repair does with an interrupted one, and
how a query widens its window: ``docs/protocol.md`` in this package.
"""

from __future__ import annotations

import asyncio
import copy
import logging
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from langgraph.store.base import (
    BaseStore,
    GetOp,
    ListNamespacesOp,
    Op,
    PutOp,
    Result,
    SearchItem,
    SearchOp,
)
from memtomem import privacy
from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides
from memtomem.indexing.privacy_gate import PrivacyRejection
from memtomem.integrations.langgraph import resolve_target_scope
from memtomem.integrations.langgraph_store import matches_filter, render_projection
from memtomem.models import IndexingStats, SearchResult
from memtomem.runtime import Components, close_components, create_components
from memtomem.runtime.files import (
    CRUD_SIDECAR_LOCK_BUDGET_S,
    async_file_lock,
    atomic_write_text,
    memory_lock_path,
)
from memtomem.storage.sqlite_helpers import norm_path

from langchain_memtomem._protocol import (
    Action,
    ChunkStamp,
    IndexedMarker,
    ItemPaths,
    ItemState,
    classify,
    item_from_path,
    item_paths,
    read_indexed_marker,
    read_marker,
    render_indexed_marker,
    render_projection_file,
    text_digest,
    to_memtomem_namespace,
)
from langchain_memtomem._records import RecordError, RecordStore

logger = logging.getLogger(__name__)

#: Default recall bound of one query search, in pipeline chunks (ADR-0042 B4).
DEFAULT_MAX_CHUNKS_PER_SEARCH = 200
#: The first window a query asks the pipeline for is at least this many chunks.
MIN_INITIAL_WINDOW = 20
#: How long a query search waits for a disagreeing item's lock before skipping
#: the item. Short on purpose: a writer holding the lock is mid-mutation.
SEARCH_REPAIR_LOCK_S = 0.25

#: Surfaces this store reports to the redaction audit log.
_SURFACE_PUT = "langchain_memtomem_put"
_SURFACE_REPAIR = "langchain_memtomem_repair"


class StoreIndexError(RuntimeError):
    """The index step of a ``put`` did not take; the record was written."""


class Steps:
    """Named step boundaries of a mutation. Tests replace ``at`` to interrupt."""

    async def at(self, name: str) -> None:
        return None


def _registered_project_dirs() -> list[Path]:
    """The developer's project registry, read as ``MemtomemBaseStore`` reads it.

    This is the one ambient input the store reads on purpose (ADR-0042 B7):
    ``config.json``, ``config.d`` and ``MEMTOMEM_*`` together name the project
    tiers, and the tier gate has to agree with memtomem's own.
    """
    registry = Mem2MemConfig()
    load_config_d(registry)
    load_config_overrides(registry, migrate=False)
    return [Path(p) for p in registry.indexing.project_memory_dirs]


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return


def _write_marker(path: Path, generation: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, f"{generation}\n")


def _expected_projection(record: dict[str, Any]) -> str | None:
    """The projection text a record renders to; ``None`` when it has nothing to rank."""
    index = record.get("index")
    if index is False:
        return None
    body = render_projection(record["value"], index)
    if not body:
        return None
    return render_projection_file(body)


def _judge(stats: IndexingStats, path: Path) -> None:
    """Raise unless the index step took. Zero indexed chunks alone is not failure."""
    if stats.total_files == 0:
        raise StoreIndexError(f"{path} was not indexed: the engine excluded or skipped it")
    if stats.errors:
        raise StoreIndexError(f"indexing {path} failed: {'; '.join(stats.errors)}")
    if stats.blocked_files:
        raise StoreIndexError(f"indexing {path} was refused by the redaction gate")


def _parse_datetime(value: str) -> datetime:
    return datetime.fromisoformat(value)


class MemtomemPipelineStore(BaseStore):
    """LangGraph ``BaseStore`` over JSON records, ranked by the memtomem pipeline.

    ``await MemtomemPipelineStore.open(root)`` builds the store's own memtomem
    components (SQLite index, embedder, pipeline) under ``root``, repairs any
    item an interrupted mutation left behind, and returns a store ready for
    the five ``BaseStore`` operations. ``config`` holds the sections of
    ``Mem2MemConfig`` the caller sets (``embedding``, ``search``, ``rerank``,
    ``chunking``, …); ``storage`` and ``indexing`` are the store's own and are
    refused. Nothing from the developer's ``config.json``, ``config.d`` or
    ``MEMTOMEM_*`` variables reaches these components. The project registry is
    read from them, once, to find which tier ``root`` is in.

    TTLs are not supported: ``supports_ttl`` is ``False``, so LangGraph
    refuses an explicit ``ttl=`` with ``NotImplementedError``.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        config: dict[str, Any] | None = None,
        max_chunks_per_search: int = DEFAULT_MAX_CHUNKS_PER_SEARCH,
        confirm_project_shared: bool = False,
        force_unsafe: bool = False,
    ) -> None:
        if max_chunks_per_search < 1:
            raise ValueError("max_chunks_per_search must be at least 1")
        # ``norm_path`` is how memtomem keys a source path: resolved, and on a
        # filesystem that folds Unicode forms (macOS) folded to NFC. Hits and
        # the orphan sweep compare paths against this root, so it has to be
        # the same spelling the index stores.
        self.root = Path(norm_path(Path(root).expanduser()))
        self.records_dir = self.root / "records"
        self.projections_dir = self.root / "projections"
        self.max_chunks_per_search = max_chunks_per_search
        self.force_unsafe = force_unsafe
        self.scope: str = "user"
        self._config = copy.deepcopy(config or {})
        self._confirm_project_shared = confirm_project_shared
        self._comp: Components | None = None
        self._records: RecordStore | None = None
        self._steps = Steps()
        self._opened = False

    # -- lifecycle ---------------------------------------------------------

    @classmethod
    async def open(cls, root: str | Path, **kwargs: Any) -> MemtomemPipelineStore:
        store = cls(root, **kwargs)
        await store._open()
        return store

    async def __aenter__(self) -> MemtomemPipelineStore:
        if not self._opened:
            await self._open()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _open(self) -> None:
        registry = await asyncio.to_thread(_registered_project_dirs)
        tier = resolve_target_scope(self.root, [], registry)
        if tier == "project_shared" and not self._confirm_project_shared:
            raise ValueError(
                f"{self.root} is inside a registered project_shared tier; writes there are "
                "git-tracked, so opening it requires confirm_project_shared=True"
            )
        self.scope = tier
        for directory in (self.records_dir, self.projections_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if tier == "project_shared":
            # ADR-0011 §5 Gate B consent, once per store as ``MemtomemBaseStore``
            # records it; every put still runs Gate A for the tier.
            privacy.emit_project_shared_confirmation(
                surface="langchain_memtomem_open",
                mechanism="param",
                action="init",
            )
        config = Mem2MemConfig.without_environment(**self._config_sections(registry))
        comp = await create_components(config, load_ambient_config=False)
        try:
            self._records = RecordStore(
                self.records_dir, scope=tier, force_unsafe=self.force_unsafe
            )
            self._comp = comp
            await self._repair_all()
        except BaseException:
            self._comp = None
            self._records = None
            await close_components(comp)
            raise
        self._opened = True

    def _config_sections(self, registry: list[Path]) -> dict[str, Any]:
        overrides = copy.deepcopy(self._config)
        for owned in ("storage", "indexing"):
            if owned in overrides:
                raise ValueError(f"config[{owned!r}] belongs to the store; choose root= instead")
        search = dict(overrides.pop("search", None) or {})
        search["system_namespace_prefixes"] = []
        return {
            **overrides,
            "search": search,
            "storage": {"sqlite_path": self.root / "index.db"},
            "indexing": {
                "memory_dirs": [self.projections_dir],
                "project_memory_dirs": list(registry),
            },
        }

    def _require_open(self) -> tuple[Components, RecordStore]:
        if self._comp is None or self._records is None:
            raise RuntimeError(
                "the store is not open: use `await MemtomemPipelineStore.open(root)`"
            )
        return self._comp, self._records

    async def aclose(self) -> None:
        comp, self._comp = self._comp, None
        self._records = None
        self._opened = False
        if comp is not None:
            await close_components(comp)

    def close(self) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(self.aclose())
            return
        raise RuntimeError("Use await store.aclose() inside an active event loop")

    # -- BaseStore ---------------------------------------------------------

    def batch(self, ops: Iterable[Op]) -> list[Result]:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.abatch(ops))
        raise RuntimeError("Use await store.abatch()/aget()/aput() inside an active event loop")

    async def abatch(self, ops: Iterable[Op]) -> list[Result]:
        _, records = self._require_open()
        results: list[Result] = []
        for op in list(ops):
            if isinstance(op, PutOp):
                if op.value is None:
                    await self._delete(op.namespace, op.key)
                else:
                    await self._put(op.namespace, op.key, op.value, op.index)
                results.append(None)
            elif isinstance(op, GetOp):
                record = records.read(tuple(op.namespace), str(op.key))
                results.append(records.item(record) if record is not None else None)
            elif isinstance(op, SearchOp):
                results.append(await self._search(op))
            elif isinstance(op, ListNamespacesOp):
                results.append(
                    records.list_namespaces(op.match_conditions, op.max_depth, op.limit, op.offset)
                )
            else:  # pragma: no cover - a future LangGraph op must fail closed
                raise TypeError(f"unsupported LangGraph store operation: {type(op).__name__}")
        return results

    # -- mutation ----------------------------------------------------------

    def _paths(self, namespace: tuple[str, ...], key: str) -> ItemPaths:
        return item_paths(self.records_dir, self.projections_dir, namespace, key)

    @asynccontextmanager
    async def _item_lock(self, paths: ItemPaths, timeout: float) -> AsyncIterator[None]:
        """The item's cross-process sidecar lock, keyed on its projection path.

        One lock per item, whether or not the projection exists: the lock file
        sits beside where the projection would be, so its directory is made
        first. ``index_file(lock_held=True)`` relies on this being the very
        lock memtomem would take for the file.

        The store's own file work under this lock (record, projection, the
        markers) runs synchronously on the event loop, never in a worker
        thread: cancelling a task that awaits ``to_thread`` releases this lock
        while the thread keeps working, and a late unlink could then remove a
        record another instance wrote in between. Each such step is a few
        small file operations. memtomem's own awaited calls (``index_file``,
        ``delete_by_source``) guard their own work against cancellation.
        """
        paths.projection.parent.mkdir(parents=True, exist_ok=True)
        async with async_file_lock(memory_lock_path(paths.projection), timeout=timeout):
            yield

    def _guard_projection(self, text: str, namespace_enc: str, surface: str) -> bool:
        """Scan the projection text; return whether ``force_unsafe`` applies to it."""
        guard = privacy.enforce_write_guard(
            text,
            surface=surface,
            scope=self.scope,
            force_unsafe=self.force_unsafe,
            audit_context={"namespace": namespace_enc},
        )
        if guard.decision == "pass":
            return False
        if guard.decision == "bypassed" and self.scope != "project_shared":
            return True
        raise ValueError(f"projection write blocked by {len(guard.hits)} privacy pattern(s)")

    async def _stored_stamps(self, paths: ItemPaths) -> frozenset[ChunkStamp]:
        comp, _ = self._require_open()
        rows = await comp.storage.list_chunks_by_source(paths.projection, limit=None)
        return frozenset((str(row.id), text_digest(row.retrieval_content)) for row in rows)

    async def _index(
        self, paths: ItemPaths, namespace_enc: str, text: str, bypass: bool
    ) -> frozenset[ChunkStamp]:
        """Index the projection; return the stamps of the rows the index holds afterwards.

        The ``.indexed`` marker goes first, so an interruption anywhere before
        the new one is written leaves no marker to vouch for rows it did not
        describe.
        """
        comp, _ = self._require_open()
        _unlink(paths.indexed)
        try:
            stats = await comp.index_engine.index_file(
                paths.projection,
                namespace=namespace_enc,
                lock_held=True,
                ingress_guarded=True,
                valve_text=text,
                force_unsafe=bypass,
            )
        except PrivacyRejection as exc:
            raise ValueError(
                f"projection {paths.projection.name} was refused by the redaction gate"
            ) from exc
        _judge(stats, paths.projection)
        return await self._stored_stamps(paths)

    @staticmethod
    def _write_indexed(
        paths: ItemPaths, generation: int, text: str, chunks: frozenset[ChunkStamp]
    ) -> None:
        paths.indexed.parent.mkdir(parents=True, exist_ok=True)
        atomic_write_text(
            paths.indexed,
            render_indexed_marker(IndexedMarker(generation, text_digest(text), chunks)),
        )

    async def _drop_projection(self, paths: ItemPaths, *, boundary: str | None = None) -> None:
        comp, _ = self._require_open()
        _unlink(paths.projection)
        if boundary is not None:
            await self._steps.at(boundary)
        _unlink(paths.indexed)
        await comp.storage.delete_by_source(paths.projection)

    async def _put(
        self,
        namespace: tuple[str, ...],
        key: str,
        value: dict[str, Any],
        index: bool | list[str] | None,
    ) -> None:
        comp, records = self._require_open()
        namespace = tuple(namespace)
        key = str(key)
        namespace_enc = to_memtomem_namespace(namespace)
        paths = self._paths(namespace, key)
        prepared = records.prepare(namespace, key, value, index)
        text = _expected_projection({"value": prepared.value, "index": prepared.index})
        async with self._item_lock(paths, CRUD_SIDECAR_LOCK_BUDGET_S):
            existing = records.read(namespace, key)
            generation = (
                max(
                    int(existing.get("generation", 0)) if existing else 0,
                    read_marker(paths.last) or 0,
                    read_marker(paths.deleting) or 0,
                )
                + 1
            )
            # Both guards run before the first write: the record's in
            # ``prepare`` above, the projection's here.
            bypass = False
            if text is not None:
                bypass = self._guard_projection(text, namespace_enc, _SURFACE_PUT)
            await self._steps.at("put.before_record")
            records.write(prepared, generation=generation, existing=existing)
            await self._steps.at("put.after_record")
            # The record above outranks any deletion marker (its generation is
            # newer), so the marker is superseded; repair would drop it too.
            _unlink(paths.deleting)
            if text is None:
                await self._drop_projection(paths, boundary="put.between_unlinks")
                await self._steps.at("put.after_projection")
            else:
                atomic_write_text(paths.projection, text)
                await self._steps.at("put.after_projection")
                chunks = await self._index(paths, namespace_enc, text, bypass)
                await self._steps.at("put.after_index")
                self._write_indexed(paths, generation, text, chunks)
                await self._steps.at("put.after_marker")
        comp.search_pipeline.invalidate_cache()

    async def _delete(self, namespace: tuple[str, ...], key: str) -> None:
        comp, records = self._require_open()
        namespace = tuple(namespace)
        key = str(key)
        paths = self._paths(namespace, key)
        async with self._item_lock(paths, CRUD_SIDECAR_LOCK_BUDGET_S):
            try:
                existing = records.read(namespace, key)
            except RecordError:
                # A torn record is still this item's: deleting it is the remedy.
                existing = None
            derived = (
                paths.record.exists()
                or paths.projection.exists()
                or paths.indexed.exists()
                or paths.deleting.exists()
            )
            if existing is None and not derived:
                return
            generation = max(
                int(existing.get("generation", 0)) if existing else 0,
                read_marker(paths.last) or 0,
                read_marker(paths.deleting) or 0,
            )
            _write_marker(paths.deleting, generation)
            _write_marker(paths.last, generation)
            await self._steps.at("delete.after_marker")
            _unlink(paths.projection)
            _unlink(paths.indexed)
            await self._steps.at("delete.after_projection")
            await comp.storage.delete_by_source(paths.projection)
            await self._steps.at("delete.after_rows")
            _unlink(paths.record)
            await self._steps.at("delete.after_record")
            _unlink(paths.deleting)
        comp.search_pipeline.invalidate_cache()

    # -- repair ------------------------------------------------------------

    async def _read_state(
        self,
        namespace: tuple[str, ...],
        key: str,
        paths: ItemPaths,
        *,
        check_rows: bool = False,
    ) -> tuple[ItemState, dict[str, Any] | None]:
        """The item's state. ``check_rows`` also compares the stored rows with the marker.

        Repair asks for it; the search path does not, because every hit is
        compared with the marker stamp by stamp anyway.
        """
        comp, records = self._require_open()
        try:
            record = records.read(namespace, key)
        except RecordError:
            logger.warning("Unreadable record %s; its projection is treated as stray", paths.record)
            record = None
        expected = _expected_projection(record) if record is not None else None
        projection_present = paths.projection.is_file()
        projection_current = False
        if projection_present and expected is not None:
            try:
                projection_current = paths.projection.read_bytes() == expected.encode("utf-8")
            except OSError:
                projection_current = False
        marker = read_indexed_marker(paths.indexed)
        record_gen = int(record.get("generation", 0)) if record is not None else None
        indexed_current = (
            marker is not None
            and expected is not None
            and marker.generation == record_gen
            and marker.digest == text_digest(expected)
        )
        counts = await comp.storage.count_chunks_by_sources([paths.projection])
        rows_match = True
        if check_rows and indexed_current and marker is not None:
            rows_match = await self._stored_stamps(paths) == marker.chunks
        state = ItemState(
            record_gen=record_gen,
            index_off=record is not None and expected is None,
            projection_present=projection_present,
            projection_current=projection_current,
            marker_present=paths.indexed.exists(),
            indexed_current=indexed_current,
            indexed_chunks=marker.chunks if marker is not None and indexed_current else frozenset(),
            deleting_gen=read_marker(paths.deleting),
            rows=sum(counts.values()),
            rows_match=rows_match,
        )
        return state, record

    async def _repair_item_locked(self, namespace: tuple[str, ...], key: str) -> ItemState:
        """One repair pass over one item, under its lock. Returns the state after it."""
        comp, _ = self._require_open()
        paths = self._paths(namespace, key)
        state, record = await self._read_state(namespace, key, paths, check_rows=True)
        action = classify(state)
        if action is Action.FINISH_DELETE:
            reached = max(
                state.deleting_gen or 0, state.record_gen or 0, read_marker(paths.last) or 0
            )
            _write_marker(paths.last, reached)
            await self._drop_projection(paths)
            _unlink(paths.record)
            _unlink(paths.deleting)
            comp.search_pipeline.invalidate_cache()
            return (await self._read_state(namespace, key, paths))[0]
        if state.deleting_gen is not None:
            _unlink(paths.deleting)
        if action is not Action.NOTHING:
            # Rows are about to change; a result this instance cached before
            # the repair would otherwise outlive it for ``search.cache_ttl``.
            comp.search_pipeline.invalidate_cache()
        if action is Action.PURGE_PROJECTION:
            await self._drop_projection(paths)
        elif action in (Action.RENDER_AND_INDEX, Action.INDEX):
            assert record is not None and state.record_gen is not None
            namespace_enc = to_memtomem_namespace(namespace)
            text = _expected_projection(record)
            assert text is not None  # index_off was classified above
            try:
                bypass = self._guard_projection(text, namespace_enc, _SURFACE_REPAIR)
                if action is Action.RENDER_AND_INDEX:
                    atomic_write_text(paths.projection, text)
                chunks = await self._index(paths, namespace_enc, text, bypass)
                await self._steps.at("repair.after_index")
                self._write_indexed(paths, state.record_gen, text, chunks)
            except (ValueError, StoreIndexError) as exc:
                # The item stays unserved (no agreeing marker); the next open
                # or the next put retries. Nothing is lost: the record is whole.
                logger.warning("Repair of %s left it unindexed: %s", paths.record, exc)
        if action is not Action.NOTHING:
            comp.search_pipeline.invalidate_cache()
        return (await self._read_state(namespace, key, paths))[0]

    async def _repair_all(self) -> None:
        comp, _ = self._require_open()
        items: set[tuple[tuple[str, ...], str]] = set()
        strays: list[Path] = []

        def discover() -> None:
            for path in self.records_dir.rglob("*.json"):
                try:
                    items.add(item_from_path(self.records_dir, path, ".json"))
                except ValueError:
                    logger.warning("Ignoring a file this store did not write: %s", path)
            for path in self.records_dir.rglob("*.json.deleting"):
                try:
                    items.add(item_from_path(self.records_dir, path.with_suffix(""), ".json"))
                except ValueError:
                    logger.warning("Ignoring a file this store did not write: %s", path)
            for path in self.projections_dir.rglob("*.md"):
                try:
                    items.add(item_from_path(self.projections_dir, path, ".md"))
                except ValueError:
                    strays.append(path)
            for path in self.projections_dir.rglob("*.md.indexed"):
                try:
                    items.add(item_from_path(self.projections_dir, path.with_suffix(""), ".md"))
                except ValueError:
                    strays.append(path)

        await asyncio.to_thread(discover)
        for namespace, key in sorted(items):
            paths = self._paths(namespace, key)
            async with self._item_lock(paths, CRUD_SIDECAR_LOCK_BUDGET_S):
                await self._repair_item_locked(namespace, key)
        for path in strays:
            logger.warning("Removing a projection this store did not write: %s", path)
            _unlink(path)
            await comp.storage.delete_by_source(path)
        # Rows whose projection file is gone: the index is derived, so they go.
        # The snapshot of files is taken without locks, so a put on another
        # instance can land between it and the row listing; each candidate is
        # therefore re-judged under its item's lock before anything is deleted.
        present = {Path(norm_path(p)) for p in self.projections_dir.rglob("*.md")}
        await self._steps.at("repair.after_snapshot")
        for source in await comp.storage.get_all_source_files():
            if source in present:
                continue
            try:
                namespace, key = item_from_path(self.projections_dir, source, ".md")
            except ValueError:
                await comp.storage.delete_by_source(source, as_stored=True)
                continue
            paths = self._paths(namespace, key)
            async with self._item_lock(paths, CRUD_SIDECAR_LOCK_BUDGET_S):
                if paths.projection.is_file():
                    await self._repair_item_locked(namespace, key)
                else:
                    await comp.storage.delete_by_source(source, as_stored=True)
        comp.search_pipeline.invalidate_cache()

    # -- search ------------------------------------------------------------

    async def _search(self, op: SearchOp) -> list[SearchItem]:
        _, records = self._require_open()
        if not op.query:
            return records.search(tuple(op.namespace_prefix), op.filter, op.limit, op.offset)
        if op.limit <= 0:
            return []
        need = op.offset + op.limit
        prefix = tuple(op.namespace_prefix)
        pattern = to_memtomem_namespace(prefix) + "*" if prefix else None
        budget = self.max_chunks_per_search
        top_k = min(budget, max(2 * need, MIN_INITIAL_WINDOW))
        while True:
            served = await self._window(op.query, top_k, pattern, prefix, op.filter, need)
            if len(served) >= need or top_k >= budget:
                break
            top_k = min(budget, 2 * top_k)
        return [
            SearchItem(
                namespace=namespace,
                key=key,
                value=record["value"],
                created_at=_parse_datetime(record["created_at"]),
                updated_at=_parse_datetime(record["updated_at"]),
                score=score,
            )
            for (namespace, key), record, score in served[op.offset : need]
        ]

    async def _window(
        self,
        query: str,
        top_k: int,
        pattern: str | None,
        prefix: tuple[str, ...],
        filters: dict[str, Any] | None,
        need: int,
    ) -> list[tuple[tuple[tuple[str, ...], str], dict[str, Any], float]]:
        """One pipeline window, judged item by item. Each window stands alone.

        A hit whose stamp (chunk id and retrieval-text digest) is not among
        those last indexed for the record's current text, or whose files
        disagree, is not served from the hit in
        hand: the item is repaired under its lock if the lock is free, and the
        window is then retrieved again from the fresh rows, bypassing the
        result cache.
        Items still disagreeing on the second pass are skipped.
        """
        comp, _ = self._require_open()
        results, _ = await comp.search_pipeline.search(
            query, top_k=top_k, namespace=pattern, scope=self.scope
        )
        served, repaired = await self._judge_hits(results, prefix, filters, need, repair=True)
        if repaired:
            results, _ = await comp.search_pipeline.search(
                query, top_k=top_k, namespace=pattern, scope=self.scope, record=False
            )
            served, _ = await self._judge_hits(results, prefix, filters, need, repair=False)
        return served

    def _group(
        self, results: list[SearchResult], prefix: tuple[str, ...]
    ) -> list[tuple[tuple[tuple[str, ...], str], frozenset[ChunkStamp], float]]:
        """Hits grouped into items, in order of first appearance.

        The item is named by the hit's source path and must agree with the
        row's own namespace; the stamps of the item's hits and its best score
        go with it.
        """
        order: list[tuple[tuple[str, ...], str]] = []
        best: dict[tuple[tuple[str, ...], str], tuple[set[ChunkStamp], float]] = {}
        for result in results:
            metadata = result.chunk.metadata
            try:
                namespace, key = item_from_path(
                    self.projections_dir, Path(metadata.source_file), ".md"
                )
            except ValueError:
                continue
            if metadata.namespace != to_memtomem_namespace(namespace):
                continue  # not a row this store wrote for that path
            if namespace[: len(prefix)] != prefix:
                continue
            ident = (namespace, key)
            stamp = (str(result.chunk.id), text_digest(result.chunk.retrieval_content))
            if ident not in best:
                order.append(ident)
                best[ident] = ({stamp}, result.score)
            else:
                stamps, seen = best[ident]
                stamps.add(stamp)
                best[ident] = (stamps, max(seen, result.score))
        return [(ident, frozenset(best[ident][0]), best[ident][1]) for ident in order]

    async def _judge_hits(
        self,
        results: list[SearchResult],
        prefix: tuple[str, ...],
        filters: dict[str, Any] | None,
        need: int,
        *,
        repair: bool,
    ) -> tuple[list[tuple[tuple[tuple[str, ...], str], dict[str, Any], float]], bool]:
        served: list[tuple[tuple[tuple[str, ...], str], dict[str, Any], float]] = []
        repaired = False
        for (namespace, key), hit_chunks, score in self._group(results, prefix):
            paths = self._paths(namespace, key)
            state, record = await self._read_state(namespace, key, paths)
            if record is not None and state.agrees and hit_chunks <= state.indexed_chunks:
                if matches_filter(record["value"], filters):
                    served.append(((namespace, key), record, score))
                    if len(served) >= need:
                        break
                continue
            if not repair:
                continue
            try:
                async with self._item_lock(paths, SEARCH_REPAIR_LOCK_S):
                    await self._repair_item_locked(namespace, key)
            except TimeoutError:
                continue  # a writer holds the item; this call does without it
            repaired = True
        return served, repaired
