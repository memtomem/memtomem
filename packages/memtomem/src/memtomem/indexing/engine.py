"""Indexing engine: orchestrates chunking, embedding, and storage."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from typing import TYPE_CHECKING, TypedDict

import pathspec

from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.chunking.structured import StructuredChunker
from memtomem.config import (
    IndexingConfig,
    NamespaceConfig,
    NamespacePolicyRule,
    categorize_memory_dir,
    memory_dir_kind,
    provider_for_category,
)
from memtomem.indexing.differ import DiffResult, compute_diff
from memtomem.models import Chunk, ChunkMetadata, IndexingStats

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.storage.base import StorageBackend

logger = logging.getLogger(__name__)

_MAX_INDEX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

# Built-in exclude patterns. Always applied in addition to user
# ``IndexingConfig.exclude_patterns``; users cannot disable these. Secret and
# noise tuples are kept separate for call-site clarity — secrets are a
# long-lived security invariant, noise evolves with upstream tool layouts.
_BUILTIN_SECRET_PATTERNS: tuple[str, ...] = (
    "**/oauth_creds.json",
    "**/credentials*",
    "**/id_rsa*",
    "**/*.pem",
    "**/*.key",
    "**/.ssh/**",
)

_BUILTIN_NOISE_PATTERNS: tuple[str, ...] = (
    "**/.claude/**/*.meta.json",
    # Same target via root-relative match for when ``~/.claude/projects`` itself
    # is the auto-discovered memory_dir root and the rel path drops ``.claude/``.
    "**/subagents/*.meta.json",
)


def _build_exclude_spec(patterns: Iterable[str]) -> pathspec.GitIgnoreSpec:
    # pathspec 1.x GitIgnoreSpec has no case-sensitivity flag; lowercase
    # patterns at build time and lowercase candidate paths at match time for
    # case-insensitive matching across filesystems.
    return pathspec.GitIgnoreSpec.from_lines(p.lower() for p in patterns)


_BUILTIN_EXCLUDE_SPEC = _build_exclude_spec((*_BUILTIN_SECRET_PATTERNS, *_BUILTIN_NOISE_PATTERNS))


def _exclude_match_keys(file_path: Path, memory_dirs: Iterable[str | Path]) -> list[str]:
    """Build the lowercase path strings to feed an exclude spec.

    Includes the absolute path and one entry per ``memory_dirs`` parent the
    file lives under (rel-to-root). Either match counts as excluded — this
    is what prevents a built-in pattern like ``**/.claude/**/*.meta.json``
    from being silently bypassed when ``~/.claude/projects`` itself is the
    indexed root, or when ``index_file`` is invoked from the file watcher
    (which doesn't go through ``_discover_files``).
    """
    resolved = file_path.resolve()
    keys: list[str] = [resolved.as_posix().lower()]
    for mem_dir in memory_dirs:
        try:
            rel = resolved.relative_to(Path(mem_dir).expanduser().resolve())
        except ValueError:
            continue
        keys.append(rel.as_posix().lower())
    return keys


def _path_is_excluded(
    file_path: Path,
    memory_dirs: Iterable[str | Path],
    user_spec: pathspec.GitIgnoreSpec,
) -> bool:
    """True if ``file_path`` matches any built-in or user exclude pattern."""
    for key in _exclude_match_keys(file_path, memory_dirs):
        if _BUILTIN_EXCLUDE_SPEC.match_file(key) or user_spec.match_file(key):
            return True
    return False


def _dir_creation_time_iso(p: Path) -> str | None:
    """OS filesystem creation time (ISO-8601 UTC) or ``None`` if dir missing.

    Prefers ``st_birthtime`` (macOS / Windows always; Linux 3.12+ on
    ext4/btrfs/xfs with statx). Falls back to ``st_ctime`` on older Linux
    setups — ``st_ctime`` there is metadata-change time, so it can shift on
    ``chmod`` / ``chown``. Acceptable for sort ordering since it's monotonic
    for newly-created dirs in normal workflows.
    """
    try:
        st = p.stat()
    except OSError:
        return None
    ts = getattr(st, "st_birthtime", None)
    if ts is None:
        ts = st.st_ctime
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


def norm_dir_prefix(d: str | Path) -> str:
    """Return the directory path normalized for ``str.startswith`` matching.

    Adds a trailing slash so a configured dir ``/foo`` does not falsely
    claim files under ``/foo-bar/...``. Always runs through
    :func:`~memtomem.storage.sqlite_helpers.norm_path` (which resolves
    symlinks and applies Unicode NFC) so the prefix shape matches the
    source-side normalisation regardless of whether the dir currently
    exists on disk — the chunks table holds resolved paths, and a
    configured-but-missing dir would otherwise compare in raw ``/tmp``
    form against resolved ``/private/tmp`` source paths on macOS.

    Used by both :func:`memory_dir_stats` (which buckets chunks per
    configured dir) and :func:`resolve_owning_memory_dir` (which goes
    the other way — given a source, find the owning dir). Keeping the
    normalisation in one place ensures the two views stay consistent
    when the prefix rules evolve.
    """
    from memtomem.storage.sqlite_helpers import norm_path

    p = Path(d).expanduser()
    base = norm_path(p)
    if not base.endswith("/"):
        base += "/"
    return base


def resolve_owning_memory_dir(
    source_path: str | Path,
    configured_dirs: Iterable[str | Path],
) -> Path | None:
    """Return the configured ``memory_dir`` that contains ``source_path``.

    Returns ``None`` for orphan sources — files indexed in the past but
    whose owning dir is no longer in the configured list (typical after
    a user removes a dir without purging its chunks). The Web UI surfaces
    these in the General view so they don't disappear.

    When configured dirs are nested (e.g. ``~/work`` and
    ``~/work/notes``), the longest-matching prefix wins so the source is
    attributed to the most specific grouping the user explicitly added.
    """
    from memtomem.storage.sqlite_helpers import norm_path

    target = norm_path(Path(source_path).expanduser())
    best: tuple[int, Path] | None = None
    for d in configured_dirs:
        prefix = norm_dir_prefix(d)
        if target.startswith(prefix):
            length = len(prefix)
            if best is None or length > best[0]:
                best = (length, Path(d).expanduser())
    return best[1] if best else None


def _count_files_on_disk(p: Path, extensions: frozenset[str]) -> int:
    """Count regular files under ``p`` whose suffix is in ``extensions``.

    Recursive ``rglob`` so the count matches what ``index_path(recursive=True)``
    would discover, modulo user exclude patterns (left out here so the
    web status fetch stays fast for the dominant case — users will hit
    Reindex anyway, and the badge is informational). Returns 0 on
    ``OSError`` (permissions, broken symlink, etc.) to keep the badge
    reading "0 files" rather than crashing the panel.
    """
    try:
        return sum(1 for fp in p.rglob("*") if fp.is_file() and fp.suffix in extensions)
    except OSError:
        return 0


async def memory_dir_stats(
    storage: "StorageBackend",
    memory_dirs: Iterable[str | Path],
    *,
    supported_extensions: frozenset[str] | None = None,
) -> list[dict[str, object]]:
    """Return per-dir index status for each configured ``memory_dir``.

    Shape: ``[{path, chunk_count, source_file_count, file_count, exists,
    category, provider, kind, created_at, last_indexed}]`` in the same
    order as ``memory_dirs``. Drives the web UI's "(N chunks)" / "(not
    indexed)" badges so users can see which dirs need a manual reindex
    (the running watcher only reacts to fs events, so files that landed
    while the server was down stay invisible until a forced re-walk;
    the opt-in :attr:`~memtomem.config.IndexingConfig.startup_backfill`
    flag covers the same gap on startup for users who explicitly enable
    it). ``category`` is provided by
    :func:`~memtomem.config.categorize_memory_dir` and ``provider`` by
    :func:`~memtomem.config.provider_for_category`, so the Web UI can
    build a vendor → product tree without maintaining its own regex or
    mapping. RFC #304 Phase 1.

    ``created_at`` is the OS filesystem creation time (ISO-8601 UTC,
    ``None`` for missing dirs); ``last_indexed`` is the max
    ``chunks.updated_at`` over source files under the dir prefix (``None``
    when the dir has no chunks). Both feed the Web UI sort dropdown that
    appears once a product leaf has ≥ 6 entries.

    When ``supported_extensions`` is provided, each existing dir is also
    walked with ``rglob`` to count files matching one of those suffixes —
    that's ``file_count`` in the response. The walk runs in worker
    threads via ``asyncio.gather`` so 28+ dirs don't serialize on disk
    I/O. Without ``supported_extensions``, ``file_count`` is 0 — keeps
    the existing test fixtures (which call this function directly without
    a config) working unchanged.

    Aggregation: one ``get_source_files_with_counts()`` call over the
    whole ``chunks`` table, bucketed in Python by normalised-path prefix
    — avoids N LIKE queries for large dir lists. ``kind`` is provided
    by :func:`~memtomem.config.memory_dir_kind` so the Web UI can split
    the Sources page into Memory and General views from the same
    response shape.
    """
    from memtomem.storage.sqlite_helpers import norm_path

    rows = await storage.get_source_files_with_counts()
    dir_list = list(memory_dirs)

    file_counts: list[int]
    if supported_extensions:
        file_counts = await asyncio.gather(
            *[
                asyncio.to_thread(
                    _count_files_on_disk,
                    Path(d).expanduser(),
                    supported_extensions,
                )
                if Path(d).expanduser().exists()
                else _resolved_zero()
                for d in dir_list
            ]
        )
    else:
        file_counts = [0] * len(dir_list)

    out: list[dict[str, object]] = []
    for d, file_count in zip(dir_list, file_counts):
        dir_path = Path(d).expanduser()
        exists = dir_path.exists()
        prefix = norm_dir_prefix(d)

        chunk_count = 0
        source_file_count = 0
        max_last_updated: str | None = None
        for row in rows:
            # row = (Path, chunk_count, last_updated, namespaces, ...)
            source_path, count, last_updated = row[0], row[1], row[2]
            if norm_path(source_path).startswith(prefix):
                chunk_count += count
                source_file_count += 1
                if last_updated is not None and (
                    max_last_updated is None or last_updated > max_last_updated
                ):
                    max_last_updated = last_updated

        category = categorize_memory_dir(d)
        out.append(
            {
                # Return the expanded path so the response shape matches
                # the other ``/api/memory-dirs/*`` endpoints (all of which
                # use ``str(Path(p).expanduser().resolve())``). Returning
                # the config-raw form (e.g. ``~/memories``) caused the web
                # UI's per-row lookup to miss tilde-prefixed entries —
                # every other dir rendered file/chunk/created badges
                # while ``~/memories`` came back blank.
                "path": str(dir_path),
                "chunk_count": chunk_count,
                "source_file_count": source_file_count,
                "file_count": file_count,
                "exists": exists,
                "category": category,
                "provider": provider_for_category(category),
                "kind": memory_dir_kind(d),
                "created_at": _dir_creation_time_iso(dir_path) if exists else None,
                "last_indexed": max_last_updated,
            }
        )
    return out


async def _resolved_zero() -> int:
    """Awaitable that resolves to 0 — used for missing dirs in the
    ``asyncio.gather`` slot so the result list stays positionally
    aligned with ``memory_dirs``."""
    return 0


class _IndexFileBase(TypedDict):
    total: int
    indexed: int
    skipped: int
    deleted: int
    errors: list[str]


class IndexFileResult(_IndexFileBase, total=False):
    new_chunk_ids: list[UUID]


class IndexEngine:
    def __init__(
        self,
        storage: StorageBackend,
        embedder: EmbeddingProvider,
        config: IndexingConfig,
        registry: ChunkerRegistry | None = None,
        namespace_config: NamespaceConfig | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._config = config
        self._ns_config = namespace_config or NamespaceConfig()
        self._ns_rule_specs: list[tuple[pathspec.GitIgnoreSpec, NamespacePolicyRule]] = [
            (_build_exclude_spec([rule.path_glob]), rule) for rule in self._ns_config.rules
        ]
        self._warned_empty_parent_rules: set[int] = set()
        self._registry = registry or ChunkerRegistry(
            [
                MarkdownChunker(),
                StructuredChunker(indexing_config=config),
                ReStructuredTextChunker(),
            ]
        )
        self._index_lock = asyncio.Lock()  # prevent concurrent indexing of same files
        # Observability counter for ``GET /api/indexing/active`` — independent
        # of ``_index_lock`` because ``index_path_stream`` runs outside the
        # lock and ``asyncio.Lock.locked()`` is racy. Incremented on entry
        # and decremented in a ``finally`` block by every public entry point
        # (``index_path``, ``index_file``, ``index_path_stream``).
        self._active_runs: int = 0

    @property
    def is_active(self) -> bool:
        """True while at least one indexing run is in flight on this engine.

        Drives the cross-tab / post-reload survival of the web UI's header
        indicator (#582 item 4.11). Counter, not boolean — concurrent stream
        + locked runs both keep it on.

        Scope is **broader** than the three web-triggered surfaces #602's
        ``STATE.indexing`` covered: any caller that enters ``index_path``,
        ``index_file``, or ``index_path_stream`` is counted, including the
        file watcher, MCP-tool ``mem_edit`` / ``mem_delete`` paths, and CLI
        ``mm index``. The result is that the web indicator may flicker
        briefly on watcher-triggered re-indexes — preferred over silently
        under-reporting server-side indexing activity to the UI.
        """
        return self._active_runs > 0

    async def index_path(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexingStats:
        self._active_runs += 1
        try:
            async with self._index_lock:
                return await self._index_path_inner(path, recursive, force, namespace)
        finally:
            self._active_runs -= 1

    async def _index_path_inner(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexingStats:
        start = time.monotonic()
        path = path.resolve()

        if not self._is_within_memory_dirs(path):
            logger.warning("Path %s resolves outside configured memory_dirs, skipping", path)
            return IndexingStats(0, 0, 0, 0, 0, 0.0)

        # File-set parity: route through ``discover_indexable_files`` so the
        # preview-namespace endpoint and the indexing run see the same set.
        files = self.discover_indexable_files(path, recursive)
        if not files:
            return IndexingStats(0, 0, 0, 0, 0, 0.0)

        sem = asyncio.Semaphore(8)

        async def _bounded(fp: Path) -> IndexFileResult:
            async with sem:
                return await self._index_file(fp, force, namespace=namespace)

        raw_results = await asyncio.gather(*[_bounded(f) for f in files], return_exceptions=True)
        file_results: list[IndexFileResult] = []
        all_errors: list[str] = []
        for i, r in enumerate(raw_results):
            if isinstance(r, dict):
                file_results.append(r)
                all_errors.extend(r.get("errors", []))
            elif isinstance(r, Exception):
                logger.error("Indexing failed for %s: %s", files[i], r)
                all_errors.append(f"{files[i].name}: {r}")

        # Aggregate new_chunk_ids across all files — preserves per-file order
        # so callers that sort/filter by source get a consistent ordering.
        all_new_chunk_ids: list[UUID] = []
        for r in file_results:
            ids = r.get("new_chunk_ids", ())
            if ids:
                all_new_chunk_ids.extend(ids)

        # Distinct namespaces resolved across the file set. Computed
        # independently of ``_index_file`` so a per-file failure (parse
        # error, embedding crash) doesn't drop the namespace echo. Pure
        # pathspec match, no I/O.
        resolved_ns = self.resolve_namespaces_for(files, namespace)

        duration = (time.monotonic() - start) * 1000
        return IndexingStats(
            total_files=len(files),
            total_chunks=sum(r["total"] for r in file_results),
            indexed_chunks=sum(r["indexed"] for r in file_results),
            skipped_chunks=sum(r["skipped"] for r in file_results),
            deleted_chunks=sum(r["deleted"] for r in file_results),
            duration_ms=duration,
            errors=tuple(all_errors),
            new_chunk_ids=tuple(all_new_chunk_ids),
            resolved_namespaces=tuple(resolved_ns),
        )

    def resolve_namespaces_for(
        self, files: list[Path], explicit_ns: str | None = None
    ) -> list[str | None]:
        """Resolve namespaces for ``files`` in stable (sort) order, distinct.

        Public companion to ``_resolve_namespace`` for callers (preview
        route, future surfaces) that need the namespace echo without
        running the indexer. ``None`` represents the
        ``default_namespace == "default"`` carve-out (untagged).
        """
        ns_set: set[str | None] = {self._resolve_namespace(f, explicit_ns) for f in files}
        return sorted(ns_set, key=lambda x: (x is None, x or ""))

    def discover_indexable_files(self, path: Path, recursive: bool = True) -> list[Path]:
        """Enumerate files ``index_path`` would visit for ``path``.

        Single source of truth for "which files would be indexed" — the
        ``trigger_index`` route, the ``preview-namespace`` route, and any
        future surface that needs to introspect the file set go through
        here. Mirrors the file-vs-dir branching at the top of
        ``_index_path_inner`` so the preview cannot drift from reality.
        """
        path = path.resolve()
        if not self._is_within_memory_dirs(path):
            return []
        if path.is_file():
            return [path]
        if path.is_dir():
            return self._discover_files(path, recursive)
        return []

    async def index_file(
        self,
        file_path: Path,
        force: bool = False,
        namespace: str | None = None,
    ) -> IndexingStats:
        """Index a single file. Convenience wrapper for external callers.

        ``force=True`` re-embeds every chunk in the file but preserves chunk
        identity (UUID) and per-chunk personalization (``access_count``,
        ``use_count``, ``last_accessed_at``, ``importance_score``) for
        chunks whose content hash matches an existing row. New chunks get
        schema defaults; chunks whose hash vanished from the file are
        deleted. See ``docs/adr/0005-force-reindex-metadata-contract.md``
        for the contract and rationale. Callers that go through
        ``mem_edit`` / ``mem_delete`` / CLI ``mm index --force`` / web
        ``POST /reindex`` all use this path.
        """
        # Defense-in-depth: the primary guard lives at the top of
        # ``_index_file`` (covers every caller — watcher, stream endpoint,
        # CLI, MCP tools). This public-entry check is kept so the call
        # returns early with zeroed stats without entering the lock.
        user_spec = _build_exclude_spec(self._config.exclude_patterns)
        if _path_is_excluded(file_path, self._config.memory_dirs, user_spec):
            logger.debug("Skipping excluded file %s", file_path)
            return IndexingStats(
                total_files=0,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=0.0,
                new_chunk_ids=(),
            )
        self._active_runs += 1
        try:
            async with self._index_lock:
                start = time.monotonic()
                result = await self._index_file(file_path.resolve(), force, namespace=namespace)
                duration = (time.monotonic() - start) * 1000
        finally:
            self._active_runs -= 1
        return IndexingStats(
            total_files=1,
            total_chunks=result["total"],
            indexed_chunks=result["indexed"],
            skipped_chunks=result["skipped"],
            deleted_chunks=result["deleted"],
            duration_ms=duration,
            new_chunk_ids=tuple(result.get("new_chunk_ids", ())),
        )

    async def is_duplicate(
        self,
        text: str,
        *,
        namespace: str | None = None,
        threshold: float = 0.92,
    ) -> bool:
        """Check if text is semantically similar to existing indexed content."""
        from memtomem.models import NamespaceFilter

        try:
            embedding = await self._embedder.embed_query(text)
            ns_filter = NamespaceFilter.parse(namespace) if namespace else None
            results = await self._storage.dense_search(
                embedding, top_k=1, namespace_filter=ns_filter
            )
            return bool(results and results[0].score >= threshold)
        except Exception:
            logger.warning("is_duplicate failed; treating as non-duplicate", exc_info=True)
            return False

    def _resolve_namespace(self, file_path: Path, explicit_ns: str | None) -> str | None:
        """Determine the namespace for a file.

        Priority: explicit parameter > policy rules (first valid match) >
        auto_ns (folder-based) > default_namespace. Returns None only if
        default_namespace is "default" and nothing else matched (preserves
        backward compat — chunks without namespace stay untagged).
        """
        if explicit_ns is not None:
            return explicit_ns

        if self._ns_rule_specs:
            candidate = file_path.as_posix().lower().lstrip("/")
            for i, (spec, rule) in enumerate(self._ns_rule_specs):
                if not spec.match_file(candidate):
                    continue
                ns = self._format_namespace(rule.namespace, file_path, rule_index=i)
                if ns is not None:
                    return ns

        if self._ns_config.enable_auto_ns:
            # Derive namespace from the immediate parent folder name,
            # but skip if the file sits at the root of a memory_dir
            # (otherwise the memory_dir folder name becomes the namespace).
            parent = file_path.parent.resolve()
            memory_roots = {Path(d).expanduser().resolve() for d in self._config.memory_dirs}
            if parent not in memory_roots:
                name = parent.name
                if name and name not in (".", ""):
                    return name

        default = self._ns_config.default_namespace
        if default and default != "default":
            return default

        return None

    def _format_namespace(self, template: str, file_path: Path, *, rule_index: int) -> str | None:
        """Substitute ``{parent}`` and ``{ancestor:N}`` in a namespace template.

        ``{parent}`` resolves to the file's immediate parent folder name;
        ``{ancestor:N}`` resolves to the folder ``N`` levels above the
        immediate parent (``N=0`` is equivalent to ``{parent}``). Returns
        ``None`` when a placeholder would expand to an empty string (root
        of filesystem) or ``N`` exceeds the available ancestors, so the
        caller can fall through to the next rule. Logs once per rule index
        to surface skips without flooding.
        """
        import string as _string

        parts: list[str] = []
        for literal, field_name, spec, _conv in _string.Formatter().parse(template):
            parts.append(literal)
            if field_name is None:
                continue
            if field_name == "parent":
                name = file_path.parent.name
                reason = "parent name empty"
                index = 0
            elif field_name == "ancestor":
                # Config validator already enforced spec is a non-negative int.
                index = int(spec) if spec else 0
                try:
                    name = file_path.parents[index].name
                except IndexError:
                    name = ""
                reason = f"ancestor:{index} out of range"
            else:
                # Unknown placeholder — validator rejects these at load time,
                # so this branch is defensive only.
                return None
            if not name:
                if rule_index not in self._warned_empty_parent_rules:
                    self._warned_empty_parent_rules.add(rule_index)
                    logger.warning(
                        "namespace rule #%d skipped for %s: %s",
                        rule_index,
                        file_path,
                        reason,
                    )
                return None
            parts.append(name)
        return "".join(parts)

    def _is_within_memory_dirs(self, path: Path) -> bool:
        """Check that *path* is within at least one configured memory_dir."""
        for d in self._config.memory_dirs:
            root = Path(d).expanduser().resolve()
            try:
                if path.is_relative_to(root):
                    return True
            except TypeError:
                try:
                    path.relative_to(root)
                    return True
                except ValueError:
                    continue
        return False

    async def _index_file(
        self,
        file_path: Path,
        force: bool,
        namespace: str | None = None,
    ) -> IndexFileResult:
        # Return shape: total/indexed/skipped/deleted (ints), errors (list[str]),
        # new_chunk_ids (list[UUID]). Early zero-result paths may omit
        # new_chunk_ids — consumers must tolerate missing keys.

        # Primary exclude guard — every caller (index_file, _index_path_inner
        # after _discover_files, index_path_stream single-file branch) funnels
        # through here, so a single check closes all entry points including
        # ones added later. ``_discover_files`` still filters upstream for
        # directory walks, but this guard ensures single-file callers like
        # ``index_path_stream(file)`` cannot smuggle credentials or noise.
        user_spec = _build_exclude_spec(self._config.exclude_patterns)
        if _path_is_excluded(file_path, self._config.memory_dirs, user_spec):
            logger.debug("Skipping excluded file %s", file_path)
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        try:
            file_size = file_path.stat().st_size
        except OSError:
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        if file_size > _MAX_INDEX_FILE_BYTES:
            logger.warning("Skipping %s: file too large (%d bytes)", file_path.name, file_size)
            return {
                "total": 0,
                "indexed": 0,
                "skipped": 0,
                "deleted": 0,
                "errors": [
                    f"{file_path.name}: file too large ({file_size // 1024 // 1024}MB,"
                    f" max {_MAX_INDEX_FILE_BYTES // 1024 // 1024}MB)"
                ],
            }

        try:
            content = file_path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            logger.warning("Non-UTF-8 content in %s, replacing invalid bytes", file_path.name)
            content = file_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        # Skip binary files (null bytes indicate non-text content)
        if "\x00" in content[:8192]:
            logger.warning("Skipping %s: appears to be a binary file", file_path.name)
            return {
                "total": 0,
                "indexed": 0,
                "skipped": 0,
                "deleted": 0,
                "errors": [f"{file_path.name}: binary file detected, skipping"],
            }

        if self._registry.get(file_path.suffix) is None:
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        new_chunks = self._registry.chunk_file(file_path, content)

        # Post-processing: merge short chunks + add overlap
        new_chunks = _merge_short_chunks(
            new_chunks,
            self._config.min_chunk_tokens,
            self._config.max_chunk_tokens,
            self._config.target_chunk_tokens,
        )
        if self._config.chunk_overlap_tokens > 0:
            new_chunks = _add_overlap(new_chunks, self._config.chunk_overlap_tokens)

        # Resolve namespace: explicit > auto_ns > default
        resolved_ns = self._resolve_namespace(file_path, namespace)
        if resolved_ns is not None:
            new_chunks = self._apply_namespace(new_chunks, resolved_ns)

        if not new_chunks:
            # File exists but is empty / unparseable — delete stale chunks
            deleted = await self._storage.delete_by_source(file_path)
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": deleted, "errors": []}

        # Always run hash-aware diff: ``compute_diff`` reuses existing chunk
        # IDs for hash-matched chunks (see ``differ.py:compute_diff``). For
        # ``force=True`` we then promote the matched ``unchanged`` chunks
        # into ``to_upsert`` so they get re-embedded — but their IDs are
        # preserved by the diff, and ``upsert_chunks`` UPDATE clause does not
        # touch ``access_count`` / ``use_count`` / ``last_accessed_at`` /
        # ``importance_score`` (sqlite_backend.py UPDATE column list). Net
        # effect: force re-indexes content but keeps per-chunk personalization
        # and chunk identity. See ``docs/adr/0005-force-reindex-metadata-contract.md``.
        existing_hashes = await self._storage.get_chunk_hashes(file_path)
        diff_result = compute_diff(existing_hashes, new_chunks)
        # ``new_chunk_ids`` in the return shape is documented as "freshly
        # created chunks" — callers like ``mem_consolidate_apply`` rely on
        # this distinction. Capture before any force-promotion so the
        # field stays accurate even when force re-embeds unchanged chunks.
        truly_new_chunk_ids = [c.id for c in diff_result.to_upsert]
        if force and diff_result.unchanged:
            diff_result = DiffResult(
                to_upsert=diff_result.to_upsert + diff_result.unchanged,
                to_delete=diff_result.to_delete,
                unchanged=[],
            )

        # Embed BEFORE any deletion — if embedding fails, DB stays untouched.
        # Skip embedding entirely when using the noop provider (BM25-only mode).
        if diff_result.to_upsert and self._embedder.dimension > 0:
            texts = [c.retrieval_content for c in diff_result.to_upsert]
            try:
                embeddings = await self._embedder.embed_texts(texts)
                for chunk, emb in zip(diff_result.to_upsert, embeddings):
                    chunk.embedding = emb
            except Exception as exc:
                logger.error(
                    "Embedding failed for %s (%d chunks): %s",
                    file_path,
                    len(diff_result.to_upsert),
                    exc,
                )
                return {
                    "total": len(new_chunks),
                    "indexed": 0,
                    "skipped": len(new_chunks),
                    "deleted": 0,
                    "errors": [f"Embedding failed: {exc}"],
                }

        # Now safe to mutate DB — embedding succeeded.
        # Wrap delete+upsert in a single transaction for atomicity.
        async with self._storage.transaction():
            if diff_result.to_delete:
                await self._storage.delete_chunks(diff_result.to_delete)

            if diff_result.to_upsert:
                await self._storage.upsert_chunks(diff_result.to_upsert)

        return {
            "total": len(new_chunks),
            "indexed": len(diff_result.to_upsert),
            "skipped": len(diff_result.unchanged),
            "deleted": len(diff_result.to_delete),
            "errors": [],
            "new_chunk_ids": truly_new_chunk_ids,
        }

    async def index_path_stream(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
    ):
        """Like index_path(), but yields progress dicts as each file is processed.

        Yields dicts with ``type`` key:
        - ``"progress"``: emitted after each file with fields
          ``file, files_done, files_total, indexed, skipped``.
        - ``"complete"``: final summary — ``total_files, total_chunks,
          indexed_chunks, skipped_chunks, deleted_chunks, duration_ms,
          errors``. ``errors`` is a list of human-readable strings in the
          same loose shape as ``IndexingStats.errors`` so non-stream UI
          handlers reuse verbatim. Empty list when the run had no errors.

        Note: this path runs **outside** ``_index_lock`` (unlike
        ``index_path`` / ``index_file``). The ``_active_runs`` counter
        is bumped here too so ``GET /api/indexing/active`` covers
        stream runs uniformly.
        """
        self._active_runs += 1
        try:
            start = time.monotonic()
            path = path.resolve()

            if path.is_file():
                files = [path]
            elif path.is_dir():
                files = self._discover_files(path, recursive)
            else:
                yield {
                    "type": "complete",
                    "total_files": 0,
                    "total_chunks": 0,
                    "indexed_chunks": 0,
                    "skipped_chunks": 0,
                    "deleted_chunks": 0,
                    "duration_ms": 0.0,
                    "errors": [],
                    "resolved_namespaces": [],
                }
                return

            total_files = len(files)
            # Pre-compute the namespace echo so the complete event surfaces
            # what was actually applied — single render across both stream
            # and non-stream paths (see ``_index_path_inner``).
            resolved_ns_for_event = self.resolve_namespaces_for(files, namespace)
            agg = {"total_chunks": 0, "indexed": 0, "skipped": 0, "deleted": 0}
            all_errors: list[str] = []

            for i, fp in enumerate(files, start=1):
                try:
                    result = await self._index_file(fp, force, namespace=namespace)
                except Exception as exc:
                    logger.error("Stream indexing failed for %s: %s", fp, exc)
                    # Path-prefix matches non-stream's ``asyncio.gather(return_exceptions=True)``
                    # branch in ``_index_path_inner`` so consumers see the same error
                    # shape regardless of whether they used the stream or non-stream
                    # endpoint.
                    result = {
                        "total": 0,
                        "indexed": 0,
                        "skipped": 0,
                        "deleted": 0,
                        "errors": [f"{fp.name}: {exc}"],
                    }
                agg["total_chunks"] += result["total"]
                agg["indexed"] += result["indexed"]
                agg["skipped"] += result["skipped"]
                agg["deleted"] += result["deleted"]
                all_errors.extend(result.get("errors", []))
                yield {
                    "type": "progress",
                    "file": str(fp),
                    "files_done": i,
                    "files_total": total_files,
                    "indexed": result["indexed"],
                    "skipped": result["skipped"],
                }

            duration = (time.monotonic() - start) * 1000
            yield {
                "type": "complete",
                "total_files": total_files,
                "total_chunks": agg["total_chunks"],
                "indexed_chunks": agg["indexed"],
                "skipped_chunks": agg["skipped"],
                "deleted_chunks": agg["deleted"],
                "duration_ms": round(duration, 1),
                "errors": all_errors,
                "resolved_namespaces": resolved_ns_for_event,
            }
        finally:
            self._active_runs -= 1

    @staticmethod
    def _apply_namespace(chunks: list[Chunk], namespace: str) -> list[Chunk]:
        """Return new Chunk instances with the given namespace applied."""
        result = []
        for c in chunks:
            new_meta = ChunkMetadata(
                source_file=c.metadata.source_file,
                heading_hierarchy=c.metadata.heading_hierarchy,
                chunk_type=c.metadata.chunk_type,
                start_line=c.metadata.start_line,
                end_line=c.metadata.end_line,
                language=c.metadata.language,
                tags=c.metadata.tags,
                namespace=namespace,
                overlap_before=c.metadata.overlap_before,
                overlap_after=c.metadata.overlap_after,
            )
            result.append(
                Chunk(
                    content=c.content,
                    metadata=new_meta,
                    id=c.id,
                    content_hash=c.content_hash,
                    embedding=c.embedding,
                    created_at=c.created_at,
                    updated_at=c.updated_at,
                )
            )
        return result

    _EXCLUDED_DIRS = frozenset(
        {
            ".venv",
            "venv",
            ".git",
            "node_modules",
            "__pycache__",
            ".pytest_cache",
            ".ruff_cache",
            ".mypy_cache",
            "dist",
            "build",
            ".tox",
            ".eggs",
            ".idea",
            ".vscode",
            # Directory-level secret stores. Never traverse even if a parent
            # is added to memory_dirs.
            ".aws",
            ".ssh",
            ".gnupg",
        }
    )

    _EXCLUDED_SUFFIXES = (".egg-info",)

    @classmethod
    def _is_excluded_part(cls, part: str) -> bool:
        """Check if a path component should be excluded."""
        if part in cls._EXCLUDED_DIRS:
            return True
        return any(part.endswith(suffix) for suffix in cls._EXCLUDED_SUFFIXES)

    def _discover_files(self, directory: Path, recursive: bool) -> list[Path]:
        supported = self._registry.supported_extensions() & self._config.supported_extensions
        user_spec = _build_exclude_spec(self._config.exclude_patterns)
        memory_dirs = self._config.memory_dirs

        def is_excluded(fp: Path, rel: Path | None) -> bool:
            # User negation cannot override built-in exclusions.
            # ``_path_is_excluded`` checks both the absolute path and the rel
            # path under each memory_dir, which keeps built-in patterns
            # (e.g. ``**/.claude/**/*.meta.json``) effective even when
            # ``directory`` is the auto-discovered ``~/.claude/projects`` root
            # and the rel path no longer contains ``.claude/``.
            return _path_is_excluded(fp, memory_dirs, user_spec)

        files: list[Path] = []
        if recursive:
            for fp in directory.rglob("*"):
                if not fp.is_file():
                    continue
                if fp.suffix not in supported:
                    continue
                rel = fp.relative_to(directory)
                if any(self._is_excluded_part(part) for part in rel.parts):
                    continue
                if is_excluded(fp, rel):
                    continue
                files.append(fp)
        else:
            for ext in supported:
                for fp in directory.glob(f"*{ext}"):
                    if is_excluded(fp, fp.relative_to(directory)):
                        continue
                    files.append(fp)
        return sorted(files)


# ---------------------------------------------------------------------------
# Post-processing: merge short chunks + add overlap
# ---------------------------------------------------------------------------


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English, ~2 for Korean."""
    korean = sum(1 for c in text if "\uac00" <= c <= "\ud7a3")
    ratio = 2 if korean > len(text) * 0.3 else 4
    return max(1, len(text) // ratio)


def _is_strict_prefix(shorter: tuple[str, ...], longer: tuple[str, ...]) -> bool:
    """True when ``shorter`` is a proper prefix of ``longer`` (ancestor→descendant)."""
    return len(shorter) < len(longer) and longer[: len(shorter)] == shorter


def _heading_level(heading: str) -> int:
    """Return the markdown heading level (``# X`` → 1, ``## X`` → 2), else 0.

    Non-markdown heading tokens (plain strings like ``"H1"``, ``"Section"``)
    return 0 so heuristics keyed on level only fire when the chunker really
    produced a markdown heading.
    """
    stripped = heading.lstrip()
    level = 0
    for ch in stripped:
        if ch == "#":
            level += 1
        else:
            break
    if level == 0 or level > 6:
        return 0
    if len(stripped) <= level or stripped[level] != " ":
        return 0
    return level


def _can_merge(current: Chunk, nxt: Chunk, *, current_is_short: bool = False) -> bool:
    """Check if two chunks can be merged.

    Guiding principle: "작을 때 관대, 클 때 엄격" — short chunks relax the
    hierarchy gate; larger chunks still need structural kinship
    (identical / headingless / sibling / same-path ancestor-descendant).

    Short-chunk leniency tiers:

    - **Identical top-level root** (``ch[0] == nh[0]``): cross-subsection
      orphans rescued while distinct top-level entries (mem_add's
      ``## Cache Decision`` vs ``## Database Decision``) stay separate.
    - **Heading inversion** (``cur_level > nxt_level``): a short chunk
      whose root is a deeper heading level than the next chunk's root is
      structurally orphaned (the chunker saw ``## X`` before the doc's
      real ``# Y`` root). Fold forward. Only markdown-style ``#`` headings
      participate — plain-string hierarchies like ``("H1",)`` keep level 0
      and so never trigger this, preserving mem_add protection.

    ``current_is_short=True`` is set by Pass 1 and Pass 3 (tail sweep); Pass 2
    (greedy packing) uses the strict kinship rules only.
    """
    if current.metadata.source_file != nxt.metadata.source_file:
        return False
    if current.metadata.heading_hierarchy == nxt.metadata.heading_hierarchy:
        return True
    # Allow headingless short chunk to merge forward into the next section
    if not current.metadata.heading_hierarchy:
        return True
    ch = current.metadata.heading_hierarchy
    nh = nxt.metadata.heading_hierarchy
    # Sibling: same direct parent, depth >= 2
    if len(ch) >= 2 and len(nh) >= 2 and ch[:-1] == nh[:-1]:
        return True
    # Same-path ancestor-descendant: parent section body next to its own
    # subsection (e.g. ``## 4`` intro body + ``## 4 > ### X``).
    if _is_strict_prefix(ch, nh) or _is_strict_prefix(nh, ch):
        return True
    if current_is_short and nh:
        # Tier 1: identical top-level root
        if ch[0] == nh[0]:
            return True
        # Tier 2: heading inversion (current deeper than next's root).
        cur_level = _heading_level(ch[0])
        nxt_level = _heading_level(nh[0])
        if cur_level and nxt_level and cur_level > nxt_level:
            return True
    return False


def _merged_hierarchy(current: Chunk, nxt: Chunk) -> tuple[str, ...]:
    """Pick the heading hierarchy for a merged chunk.

    - Identical / headingless: use the more specific one.
    - Otherwise: keep the common prefix; diverging leaves on either side are
      dropped from the hierarchy and restored inline via
      ``_build_merged_content``.

    Common-prefix unification (rather than descendant promotion) keeps chained
    merges honest: once a sibling-merge has already collapsed a hierarchy to
    its common ancestor, a later ancestor→descendant step could otherwise
    relabel the merged chunk with just one child's heading.
    """
    ch = current.metadata.heading_hierarchy
    nh = nxt.metadata.heading_hierarchy
    if ch == nh or not ch:
        return nh or ch
    common: list[str] = []
    for a, b in zip(ch, nh):
        if a == b:
            common.append(a)
        else:
            break
    return tuple(common) if common else nh


def _prepend_dropped_headings(content: str, dropped: tuple[str, ...]) -> str:
    """Prefix ``content`` with heading lines that would otherwise be lost.

    Used on sibling merges where the common-prefix resolution drops each
    chunk's diverging leaf heading(s).
    """
    if not dropped:
        return content
    header = "\n".join(dropped)
    return f"{header}\n\n{content}"


def _build_merged_content(current: Chunk, nxt: Chunk, merged_hierarchy: tuple[str, ...]) -> str:
    """Concatenate two chunks' bodies, restoring any headings dropped by
    hierarchy resolution so retrieval keeps the breadcrumb signal.
    """
    ch = current.metadata.heading_hierarchy
    nh = nxt.metadata.heading_hierarchy
    dropped_ch = ch[len(merged_hierarchy) :]
    dropped_nh = nh[len(merged_hierarchy) :]
    left = _prepend_dropped_headings(current.content, dropped_ch)
    right = _prepend_dropped_headings(nxt.content, dropped_nh)
    return f"{left}\n\n{right}"


def _merge_pair(current: Chunk, nxt: Chunk) -> Chunk:
    """Produce a single Chunk by merging ``current`` and ``nxt``."""
    hierarchy = _merged_hierarchy(current, nxt)
    content = _build_merged_content(current, nxt, hierarchy)
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=current.metadata.source_file,
            heading_hierarchy=hierarchy,
            chunk_type=current.metadata.chunk_type,
            start_line=current.metadata.start_line,
            end_line=nxt.metadata.end_line,
            language=current.metadata.language,
            tags=tuple(set(current.metadata.tags) | set(nxt.metadata.tags)),
            namespace=current.metadata.namespace,
        ),
    )


def _merge_short_chunks(
    chunks: list[Chunk],
    min_tokens: int,
    max_tokens: int = 0,
    target_tokens: int = 0,
) -> list[Chunk]:
    """Merge consecutive same-source chunks into semantically coherent groups.

    Three passes:
    - Pass 1 (min enforcement): forward-merge while cur < min_tokens, ignoring
      the hierarchy gate so orphan micro-chunks (frontmatter, stray short
      sections) always get absorbed.
    - Pass 2 (greedy packing): when ``target_tokens`` > 0, keep packing adjacent
      hierarchy-compatible siblings/descendants while cur < target AND
      combined <= max. Set ``target_tokens=0`` to disable.
    - Pass 3 (tail backward sweep): if the final chunk is still < min, try
      merging it into its predecessor once.

    ``max_tokens`` caps every merge; ``min_tokens <= 0`` skips all passes.
    """
    if min_tokens <= 0 or len(chunks) <= 1:
        return chunks

    if max_tokens <= min_tokens:
        max_tokens = max(min_tokens * 4, 512)

    # ---- Pass 1: min enforcement (hierarchy-agnostic) ----
    pass1: list[Chunk] = []
    i = 0
    while i < len(chunks):
        c = chunks[i]
        cur_tokens = _estimate_tokens(c.content)
        while (
            cur_tokens < min_tokens
            and i + 1 < len(chunks)
            and _can_merge(c, chunks[i + 1], current_is_short=True)
        ):
            nxt = chunks[i + 1]
            nxt_tokens = _estimate_tokens(nxt.content)
            merged_tokens = cur_tokens + nxt_tokens + 1
            # Honor the max_tokens ceiling, except when it was already
            # breached upstream (the chunker uses a 4 char/token ratio
            # while Korean-heavy text re-estimates at 2 char/token, so
            # already-emitted chunks can sit above max). Merging a short
            # orphan into an over-ceiling neighbour does not meaningfully
            # worsen the chunk size, and preserves the orphan's context.
            if merged_tokens > max_tokens and nxt_tokens <= max_tokens:
                break
            c = _merge_pair(c, nxt)
            cur_tokens = _estimate_tokens(c.content)
            i += 1
        pass1.append(c)
        i += 1

    # ---- Pass 2: greedy packing (hierarchy-respecting) ----
    if target_tokens > min_tokens and len(pass1) > 1:
        pass2: list[Chunk] = []
        i = 0
        while i < len(pass1):
            c = pass1[i]
            cur_tokens = _estimate_tokens(c.content)
            while cur_tokens < target_tokens and i + 1 < len(pass1) and _can_merge(c, pass1[i + 1]):
                nxt = pass1[i + 1]
                merged_tokens = cur_tokens + _estimate_tokens(nxt.content) + 1
                if merged_tokens > max_tokens:
                    break
                c = _merge_pair(c, nxt)
                cur_tokens = _estimate_tokens(c.content)
                i += 1
            pass2.append(c)
            i += 1
    else:
        pass2 = pass1

    # ---- Pass 3: tail backward sweep ----
    if len(pass2) >= 2:
        last = pass2[-1]
        last_tokens = _estimate_tokens(last.content)
        if last_tokens < min_tokens:
            prev = pass2[-2]
            prev_tokens = _estimate_tokens(prev.content)
            combined = prev_tokens + last_tokens + 1
            # Broken-ceiling rescue (same rationale as Pass 1): if prev was
            # already above max, absorbing the tail orphan is fine.
            within_ceiling = combined <= max_tokens or prev_tokens > max_tokens
            if within_ceiling and _can_merge(prev, last, current_is_short=True):
                pass2[-2] = _merge_pair(prev, last)
                pass2.pop()

    return pass2


def _add_overlap(chunks: list[Chunk], overlap_tokens: int) -> list[Chunk]:
    """Add token overlap between adjacent chunks from the same source file.

    Each chunk gets a suffix from the previous chunk (overlap_before)
    and a prefix from the next chunk (overlap_after).
    overlap_before/overlap_after in metadata record the char count of overlap
    so consumers can strip it for deduplication (e.g., document reconstruction).
    """
    if overlap_tokens <= 0 or len(chunks) <= 1:
        return chunks

    overlap_chars = min(overlap_tokens * 3, 5000)  # rough token→char, capped

    result: list[Chunk] = []
    for i, c in enumerate(chunks):
        prefix = ""
        suffix = ""
        ob = 0  # overlap_before char count
        oa = 0  # overlap_after char count

        # Borrow from previous chunk (same file)
        if i > 0 and chunks[i - 1].metadata.source_file == c.metadata.source_file:
            prev_content = chunks[i - 1].content
            prefix = (
                prev_content[-overlap_chars:] if len(prev_content) > overlap_chars else prev_content
            )
            ob = len(prefix)

        # Borrow from next chunk (same file)
        if i + 1 < len(chunks) and chunks[i + 1].metadata.source_file == c.metadata.source_file:
            next_content = chunks[i + 1].content
            suffix = (
                next_content[:overlap_chars] if len(next_content) > overlap_chars else next_content
            )
            oa = len(suffix)

        if ob == 0 and oa == 0:
            result.append(c)
            continue

        parts = []
        if prefix:
            parts.append(prefix)
        parts.append(c.content)
        if suffix:
            parts.append(suffix)

        new_content = "\n".join(parts)
        new_meta = ChunkMetadata(
            source_file=c.metadata.source_file,
            heading_hierarchy=c.metadata.heading_hierarchy,
            chunk_type=c.metadata.chunk_type,
            start_line=c.metadata.start_line,
            end_line=c.metadata.end_line,
            language=c.metadata.language,
            tags=c.metadata.tags,
            namespace=c.metadata.namespace,
            overlap_before=ob,
            overlap_after=oa,
        )
        result.append(Chunk(content=new_content, metadata=new_meta))
    return result
