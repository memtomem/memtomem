"""Indexing engine: orchestrates chunking, embedding, and storage."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import logging
import os
import stat as stat_module
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from typing import TYPE_CHECKING, Literal, TypedDict, cast

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
    classify_scope,
    index_excluded_filenames,
    memory_dir_kind,
    provider_for_category,
)
from memtomem import privacy
from memtomem.errors import EmbeddingError
from memtomem.indexing.differ import DiffResult, compute_diff
from memtomem.models import Chunk, IndexingStats

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider
    from memtomem.storage.base import StorageBackend
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)

PathScope = Literal["configured", "explicit"]

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
    """True if ``file_path`` matches any exclude rule.

    Three layers, any of which excludes: (1) the provider index-file
    convention for the ``memory_dir`` root that *owns* the file — e.g. a
    ``claude-memory`` root's ``MEMORY.md``/``README.md`` is an index/meta
    file, never content; (2) the built-in secret/noise denylist; (3) the
    user's ``indexing.exclude_patterns``. Layer (1) is the single
    enforcement point shared by ``_discover_files`` (dir walk),
    ``_index_file`` (per-file funnel for watcher/CLI/MCP), and
    ``mm purge`` — so the convention can't be honored on one path and
    bypassed on another (the bug where the general walk indexed
    ``MEMORY.md`` while ``mm ingest`` skipped it).

    Ownership uses :func:`resolve_owning_memory_dir` (most-specific,
    longest-prefix root), so a nested configured root overrides its
    parent's convention — a plain ``project-docs/`` root configured under
    ``~/.codex/memories`` keeps its own ``README.md`` rather than
    inheriting Codex's exclude.
    """
    owning = resolve_owning_memory_dir(file_path, memory_dirs)
    if owning is not None and Path(file_path).name in index_excluded_filenames(
        categorize_memory_dir(owning)
    ):
        return True
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

    Adds a trailing ``os.sep`` (platform-native separator) so a configured
    dir does not falsely claim files under a sibling sharing the same
    prefix (e.g. ``/foo`` should not match ``/foo-bar/...``). Always runs
    through :func:`~memtomem.storage.sqlite_helpers.norm_path` (which
    resolves symlinks and applies Unicode NFC) so the prefix shape matches
    the source-side normalisation regardless of whether the dir currently
    exists on disk — the chunks table holds resolved paths, and a
    configured-but-missing dir would otherwise compare in raw ``/tmp``
    form against resolved ``/private/tmp`` source paths on macOS.

    The trailing-separator step uses ``os.sep`` rather than a hardcoded
    ``"/"`` so the prefix is consistent with ``norm_path``'s output on
    Windows, where ``Path.resolve()`` returns backslash-separated strings
    (``C:\\Users\\foo``) — a hardcoded ``"/"`` would yield a mixed-form
    prefix that never matches a native source path under
    ``startswith`` (#647). On POSIX, ``os.sep == "/"`` so behaviour is
    unchanged.

    Used by both :func:`memory_dir_stats` (which buckets chunks per
    configured dir) and :func:`resolve_owning_memory_dir` (which goes
    the other way — given a source, find the owning dir). Keeping the
    normalisation in one place ensures the two views stay consistent
    when the prefix rules evolve.
    """
    from memtomem.storage.sqlite_helpers import norm_path

    p = Path(d).expanduser()
    base = norm_path(p)
    if not base.endswith(os.sep):
        base += os.sep
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
        dir_path = Path(d).expanduser().resolve()
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
                # Return the resolved form so per-row keys match the
                # sibling ``/api/memory-dirs/*`` and ``/api/config``
                # endpoints (all use ``str(Path(p).expanduser().resolve())``).
                # Reverting to expanduser-only makes Web UI badge lookup
                # miss tilde- or symlink-prefixed entries (#666).
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


class PrivacyRejection(Exception):
    """Raised by :meth:`IndexEngine._index_file` when a file's content trips the
    secret-redaction guard during **un-adjudicated** indexing (ADR-0006 PR-A).

    Bulk entrypoints (``index_path`` / ``index_path_stream``) catch this per file
    and aggregate it into :attr:`IndexingStats.blocked_files`; single-file
    ``index_file`` callers let it propagate so their own rollback / error
    surfacing runs. Callers that already ran ``privacy.enforce_write_guard`` at
    their ingress layer pass ``already_scanned=True`` and never trigger this.

    Carries only the hit **count** and file path — never the matched bytes
    (secret-in-log hygiene).
    """

    def __init__(self, *, path: Path, hit_count: int, scope: str, decision: str) -> None:
        self.path = path
        self.hit_count = hit_count
        self.scope = scope
        self.decision = decision
        super().__init__(f"redaction_blocked: {path.name} (hits={hit_count}, decision={decision})")


class _IndexFileBase(TypedDict):
    total: int
    indexed: int
    skipped: int
    deleted: int
    errors: list[str]


class IndexFileResult(_IndexFileBase, total=False):
    new_chunk_ids: list[UUID]
    # Set to 1 by the stream path when a file is skipped by the ADR-0006
    # redaction gate; aggregated into ``IndexingStats.blocked_files``. The
    # non-stream path tracks blocks via the raised ``PrivacyRejection`` instead.
    blocked: int
    # 1 when the blocked file was ``project_shared`` (hard-refused, not
    # bypassable with force_unsafe) — aggregated into
    # ``IndexingStats.blocked_project_shared_files``.
    blocked_project_shared: int


class IndexEngine:
    def __init__(
        self,
        storage: StorageBackend,
        embedder: EmbeddingProvider,
        config: IndexingConfig,
        registry: ChunkerRegistry | None = None,
        namespace_config: NamespaceConfig | None = None,
        progress_threshold: int = 32,
        llm: "LLMProvider | None" = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        self._config = config
        # Optional LLM provider used by the per-source AI summary pipeline.
        # ``None`` is the default — the summary path is fully gated behind
        # ``IndexingConfig.auto_summarize``, and even when that flag is
        # True the absence of a provider silently disables generation.
        self._llm = llm
        # ``chunk_progress`` SSE events are only emitted when a single file
        # produces more than this many chunks (or always when set to 0).
        # Sourced from ``EmbeddingConfig.progress_threshold`` by callers
        # (``component_factory``, ``status_config`` reset path); defaults
        # to 32 here so test-only direct constructors stay quiet.
        self._progress_threshold = progress_threshold
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
        # Prevent concurrent indexing of the same files. This is level L3 of the
        # memory-file lock order (see ``context._atomic`` module docstring): the
        # per-file sidecar (L2) is acquired ABOVE this lock, never below, so no
        # path ever waits on a sidecar while holding ``_index_lock`` (#1587).
        self._index_lock = asyncio.Lock()
        # Observability counter for ``GET /api/indexing/active`` — independent
        # of ``_index_lock`` because runs also span discovery, gaps between
        # files, and lock-wait periods where ``asyncio.Lock.locked()`` would
        # misreport. Incremented on entry and decremented in a ``finally``
        # block by every public entry point (``index_path``, ``index_file``,
        # ``index_path_stream``).
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
        *,
        force_unsafe: bool = False,
        path_scope: PathScope = "configured",
    ) -> IndexingStats:
        self._active_runs += 1
        try:
            async with self._index_lock:
                return await self._index_path_inner(
                    path,
                    recursive,
                    force,
                    namespace,
                    force_unsafe=force_unsafe,
                    path_scope=path_scope,
                )
        finally:
            self._active_runs -= 1

    async def _index_path_inner(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
        *,
        force_unsafe: bool = False,
        path_scope: PathScope = "configured",
    ) -> IndexingStats:
        start = time.monotonic()
        path = path.resolve()

        if path_scope == "configured" and not self._is_within_memory_dirs(path):
            message = f"path is outside configured memory directories: {path}"
            logger.warning(message)
            return IndexingStats(0, 0, 0, 0, 0, 0.0, errors=(message,))

        if not path.exists():
            message = f"index path does not exist: {path}"
            return IndexingStats(0, 0, 0, 0, 0, 0.0, errors=(message,))

        # File-set parity: route through ``discover_indexable_files`` so the
        # preview-namespace endpoint and the indexing run see the same set.
        files = self.discover_indexable_files(path, recursive, path_scope=path_scope)
        if not files:
            return IndexingStats(0, 0, 0, 0, 0, 0.0)

        sem = asyncio.Semaphore(8)

        async def _bounded(fp: Path) -> IndexFileResult:
            async with sem:
                return await self._index_file(
                    fp,
                    force,
                    namespace=namespace,
                    force_unsafe=force_unsafe,
                    path_scope=path_scope,
                )

        raw_results = await asyncio.gather(*[_bounded(f) for f in files], return_exceptions=True)
        file_results: list[IndexFileResult] = []
        all_errors: list[str] = []
        blocked_paths: list[str] = []
        blocked_project_shared = 0
        for i, r in enumerate(raw_results):
            if isinstance(r, dict):
                file_results.append(r)
                all_errors.extend(r.get("errors", []))
            elif isinstance(r, PrivacyRejection):
                # ADR-0006 PR-A: un-adjudicated bulk index hit a secret-bearing
                # file. Skip it, record it as blocked, and continue the run so a
                # single flagged file doesn't abort indexing the whole tree.
                # Preserve scope/decision so callers give correct guidance:
                # project_shared is hard-refused even with force_unsafe.
                logger.warning("Indexing blocked by redaction guard for %s: %s", files[i], r)
                blocked_paths.append(str(files[i]))
                if r.scope == "project_shared":
                    blocked_project_shared += 1
                all_errors.append(
                    f"{files[i].name}: redaction_blocked "
                    f"(hits={r.hit_count}, scope={r.scope}, decision={r.decision})"
                )
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
            blocked_files=len(blocked_paths),
            blocked_paths=tuple(blocked_paths),
            blocked_project_shared_files=blocked_project_shared,
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

    def discover_indexable_files(
        self,
        path: Path,
        recursive: bool = True,
        *,
        path_scope: PathScope = "configured",
    ) -> list[Path]:
        """Enumerate files ``index_path`` would visit for ``path``.

        Single source of truth for "which files would be indexed" — the
        ``trigger_index`` route, the ``preview-namespace`` route, and any
        future surface that needs to introspect the file set go through
        here. Mirrors the file-vs-dir branching at the top of
        ``_index_path_inner`` so the preview cannot drift from reality.
        """
        path = path.resolve()
        if path_scope == "configured" and not self._is_within_memory_dirs(path):
            return []
        if path.is_file():
            return [path]
        if path.is_dir():
            return self._discover_files(path, recursive)
        return []

    async def _index_file_locked(
        self,
        resolved_path: Path,
        force: bool,
        *,
        namespace: str | None = None,
        on_chunk_progress: Callable[[int, int], None] | None = None,
        force_unsafe: bool = False,
        already_scanned: bool = False,
        lock_held: bool = False,
        path_scope: PathScope = "configured",
    ) -> tuple[IndexFileResult, float]:
        """Run ``_index_file`` under the L2 sidecar → L3 ``_index_lock`` pair.

        Single home for the per-file lock policy so ``index_file`` and
        ``index_path_stream`` cannot drift (#1574 item 6). Returns the raw
        per-file result plus the duration (ms) of the indexing work itself —
        measured inside the locks, so lock-wait time is excluded.

        ADR-0011 PR-D round 11 (B2): the cross-process sidecar means the
        sibling lock taken by ``mm context memory-migrate`` is honored here
        too. Without it, a watcher firing ``index_file(target)`` mid-migrate
        races with migrate's ``shutil.move`` + DB UPDATE pair (migrate's lock
        alone is one-sided). #1587 hoists this sidecar acquire ABOVE
        ``_index_lock`` (L2 before L3) and makes it async + bounded, so it can
        never freeze the loop while a suspended holder needs it — and lets
        CRUD callers hold the sidecar across their whole read→rewrite→reindex
        span and reach here with ``lock_held=True`` instead of
        self-deadlocking.
        """
        # In-body import on purpose: tests monkeypatch the budget by dotted
        # path (``memtomem.context._atomic._MEMORY_SIDECAR_LOCK_BUDGET_S``);
        # a module-top ``from`` import would freeze the value.
        from memtomem.context._atomic import (
            _MEMORY_SIDECAR_LOCK_BUDGET_S,
            _lock_path_for,
            async_file_lock,
        )

        # Skip the sidecar when the caller already holds it (lock_held) or
        # when the parent dir is gone (#1566: a delete-by-source pass for a
        # vanished file — taking the sidecar would ``mkdir`` the deleted
        # parent back into existence just to lock a delete, resurrecting the
        # directory the user removed). ``_index_lock`` still serializes
        # in-process; a migrate sidecar for a missing-parent path lives in
        # that same missing parent, so no live pair-op can be mid-flight.
        skip_sidecar = lock_held or not resolved_path.parent.is_dir()
        if skip_sidecar:
            async with self._index_lock:
                start = time.monotonic()
                result = await self._index_file(
                    resolved_path,
                    force,
                    namespace=namespace,
                    on_chunk_progress=on_chunk_progress,
                    force_unsafe=force_unsafe,
                    already_scanned=already_scanned,
                    path_scope=path_scope,
                )
                return result, (time.monotonic() - start) * 1000
        async with async_file_lock(
            _lock_path_for(resolved_path),
            timeout=_MEMORY_SIDECAR_LOCK_BUDGET_S,
        ):
            async with self._index_lock:
                start = time.monotonic()
                result = await self._index_file(
                    resolved_path,
                    force,
                    namespace=namespace,
                    on_chunk_progress=on_chunk_progress,
                    force_unsafe=force_unsafe,
                    already_scanned=already_scanned,
                    path_scope=path_scope,
                )
                return result, (time.monotonic() - start) * 1000

    async def index_file(
        self,
        file_path: Path,
        force: bool = False,
        namespace: str | None = None,
        *,
        force_unsafe: bool = False,
        already_scanned: bool = False,
        lock_held: bool = False,
        path_scope: PathScope = "configured",
    ) -> IndexingStats:
        """Index a single file. Convenience wrapper for external callers.

        ``lock_held=True`` tells this method the caller already holds this
        file's cross-process sidecar lock (L2) for the whole read→rewrite→
        reindex span — the memory-CRUD tools, web chunk edit/delete, web/CLI
        add, all of which mutate the file and then reindex under one
        ``async_file_lock`` (issue #1587). Re-acquiring the sidecar here would
        self-deadlock (portalocker contends between fds within one process), so
        this path skips straight to ``_index_lock`` (L3). It also stands in for
        the #1566 "parent dir gone" case, whose outermost acquirer likewise
        skips the sidecar (see below). Un-serialized callers (watcher, backfill,
        ``mm index <file>``, importers) leave it ``False`` and this method takes
        the sidecar itself, off the event loop, bounded.

        ``already_scanned=True`` skips the ADR-0006 redaction gate for callers
        that already ran ``privacy.enforce_write_guard`` on the new content at
        their own ingress layer (``mem_add`` / ``mem_edit`` / upload / chunk
        edit, …); the whole-file reindex must not re-litigate or double-count
        that content. Un-adjudicated single-file callers (``mem_fetch``, file
        import, ``mm index <file>``) leave it ``False`` and must catch the
        resulting :class:`PrivacyRejection`.

        ``force=True`` re-embeds every chunk in the file but preserves chunk
        identity (UUID) and per-chunk personalization (``access_count``,
        ``use_count``, ``last_accessed_at``, ``importance_score``) for
        chunks whose content hash matches an existing row. New chunks get
        schema defaults; chunks whose hash vanished from the file are
        deleted. See ``docs/adr/0005-force-reindex-metadata-contract.md``
        for the contract and rationale. Callers that go through
        ``mem_edit`` / ``mem_delete`` / CLI ``mm index --force`` / web
        ``POST /reindex`` all use this path.

        If ``file_path`` no longer exists on disk (deleted, renamed away, or
        replaced by a directory), this removes that source's stale chunks via
        ``delete_by_source``, regardless of exclude patterns (cleanup is never
        blocked by exclude). The delete is skipped when the whole containing
        index root has vanished, so a single missing file is purged but a
        wholesale root/volume loss is left to the periodic mass-orphan brake
        (#1565) instead of being mass-deleted per-event (#1566).
        """
        # Defense-in-depth: the primary guard lives at the top of
        # ``_index_file`` (covers every caller — watcher, stream endpoint,
        # CLI, MCP tools). This public-entry check is kept so an excluded,
        # still-present *file* returns early with zeroed stats without entering
        # the lock. A path that is no longer a regular file — missing, or
        # replaced by a directory — falls through even when excluded, so its
        # stale chunks are purged: exclude blocks indexing, not cleanup (see the
        # missing-source branch in ``_index_file``). ``is_file`` (not
        # ``exists``) is the right predicate — a same-named directory ``exists``
        # but is not indexable, and its old chunks must still be cleaned. (#1566)
        user_spec = _build_exclude_spec(self._config.exclude_patterns)
        if _path_is_excluded(file_path, self._config.all_index_roots(), user_spec) and (
            file_path.is_file()
        ):
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
            result, duration = await self._index_file_locked(
                file_path.resolve(),
                force,
                namespace=namespace,
                force_unsafe=force_unsafe,
                already_scanned=already_scanned,
                lock_held=lock_held,
                path_scope=path_scope,
            )
        finally:
            self._active_runs -= 1
        return IndexingStats(
            total_files=1,
            total_chunks=result["total"],
            indexed_chunks=result["indexed"],
            skipped_chunks=result["skipped"],
            deleted_chunks=result["deleted"],
            duration_ms=duration,
            errors=tuple(result.get("errors", ())),
            new_chunk_ids=tuple(result.get("new_chunk_ids", ())),
        )

    async def is_duplicate(
        self,
        text: str,
        *,
        namespace: str | None = None,
        threshold: float = 0.92,
        project_context_root: Path | None = None,
    ) -> bool:
        """Check if text is semantically similar to existing indexed content.

        ``project_context_root`` is threaded onto the always-on
        storage scope filter (ADR-0011 PR-D round 11). No in-tree
        callers today; the kwarg defaults to ``None`` (user-only by
        the always-on filter) and is positioned for forward-compat
        with project-aware dedup checks.
        """
        from memtomem.models import NamespaceFilter

        try:
            embedding = await self._embedder.embed_query(text)
            ns_filter = NamespaceFilter.parse(namespace) if namespace else None
            results = await self._storage.dense_search(
                embedding,
                top_k=1,
                namespace_filter=ns_filter,
                project_context_root=project_context_root,
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
            # but skip if the file sits at the root of any index root
            # (otherwise the root folder name becomes the namespace).
            # ADR-0011: include project_memory_dirs so a file at the root
            # of a registered project_shared dir does not pick up the
            # ``memories`` literal as its namespace.
            parent = file_path.parent.resolve()
            memory_roots = {Path(d).expanduser().resolve() for d in self._config.all_index_roots()}
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

    def _containing_index_root(self, path: Path) -> Path | None:
        """Return the *most-specific* resolved index root containing *path*.

        Covers user-tier ``memory_dirs`` and project-tier
        ``project_memory_dirs`` (ADR-0011). When roots are nested
        (``~/mem`` and ``~/mem/project``), the longest-prefix match wins —
        so the unmount brake in :meth:`_delete_missing_source` checks the
        nested root that actually vanished rather than a surviving parent
        that would mask it. Returns ``None`` when *path* is outside every
        root.
        """
        best: Path | None = None
        for d in self._config.all_index_roots():
            root = Path(d).expanduser().resolve()
            try:
                within = path.is_relative_to(root)
            except TypeError:
                try:
                    path.relative_to(root)
                    within = True
                except ValueError:
                    within = False
            if within and (best is None or len(root.parts) > len(best.parts)):
                best = root
        return best

    def _is_within_memory_dirs(self, path: Path) -> bool:
        """Check that *path* is within at least one configured index root.

        Method name kept for backward compatibility with callers; the
        semantic is "any registered index root".
        """
        return self._containing_index_root(path) is not None

    async def _delete_missing_source(
        self, file_path: Path, *, path_scope: PathScope = "configured"
    ) -> IndexFileResult:
        """Remove stale chunks for a source file that is gone from disk.

        Reached when ``stat``/``read_text`` raise ``FileNotFoundError`` /
        ``NotADirectoryError`` / ``IsADirectoryError`` (the file was deleted,
        renamed away, or replaced by a directory).

        Deletion is skipped when the most-specific containing index root has
        itself disappeared: when a whole watched root/volume is unmounted or
        removed, every path under it reports missing at once, and a per-event
        purge of the entire tree is exactly the mass-delete we must not do.
        Root gone → no-op; the two-pass mass-orphan brake (#1565, run by the
        scheduler / health watchdog / ``mem_cleanup_orphans``) owns that bulk
        case with a ratio check the per-event path can't replicate. This brake
        catches whole-root loss; a mountpoint that survives *empty* still
        passes ``is_dir()`` here, so that bulk case is deliberately left to the
        periodic mass-orphan scan rather than guessed at per-event. Reuses the
        same ``delete_by_source`` primitive as those backstops. (#1566)
        """
        root = self._containing_index_root(file_path)
        if path_scope == "configured" and (root is None or not root.is_dir()):
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}
        deleted = await self._storage.delete_by_source(file_path)
        if deleted:
            # Path + count only — never log file content on the delete path.
            logger.info(
                "Source file gone; removed %d stale chunk(s) from index: %s",
                deleted,
                file_path,
            )
        return {"total": 0, "indexed": 0, "skipped": 0, "deleted": deleted, "errors": []}

    async def _index_file(
        self,
        file_path: Path,
        force: bool,
        namespace: str | None = None,
        *,
        on_chunk_progress: Callable[[int, int], None] | None = None,
        force_unsafe: bool = False,
        already_scanned: bool = False,
        path_scope: PathScope = "configured",
    ) -> IndexFileResult:
        # Return shape: total/indexed/skipped/deleted (ints), errors (list[str]),
        # new_chunk_ids (list[UUID]). Early zero-result paths may omit
        # new_chunk_ids — consumers must tolerate missing keys.

        # Existence check FIRST — before the exclude guard. A file that is gone
        # from disk is a delete-by-source, and cleanup must never be blocked by
        # an exclude pattern: the orphan sweep (#1565) already purges excluded
        # orphans unconditionally, so the live path must match, else a deleted
        # + newly-excluded file's chunks stay searchable forever. ``stat`` reads
        # only metadata (no content), so statting an excluded file first is
        # safe — its content is still never read below. (#1566)
        try:
            stat_result = file_path.stat()
        except (FileNotFoundError, NotADirectoryError):
            # File deleted/renamed away (NotADirectoryError: a parent component
            # was replaced by a file, so the path cannot exist) — purge its
            # stale chunks instead of a silent no-op.
            return await self._delete_missing_source(file_path, path_scope=path_scope)
        except OSError:
            # Transient I/O (EACCES/EIO/ESTALE) — never delete on a blip.
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        # The path exists but is no longer a regular file (replaced by a
        # directory or special file) — gone *as an indexable source*, so purge
        # its stale chunks. Checked from the stat result (no extra syscall, no
        # content read) and BEFORE the exclude guard, so an excluded file that
        # was swapped for a same-named directory is still cleaned up. (#1566)
        if not stat_module.S_ISREG(stat_result.st_mode):
            return await self._delete_missing_source(file_path, path_scope=path_scope)

        # Primary exclude guard — every caller (index_file, _index_path_inner
        # after _discover_files, index_path_stream single-file branch) funnels
        # through here, so a single check closes all entry points including
        # ones added later. ``_discover_files`` still filters upstream for
        # directory walks, but this guard ensures single-file callers like
        # ``index_path_stream(file)`` cannot smuggle credentials or noise. Only
        # *indexing* (adding content) is gated here; the missing-file delete
        # above runs regardless.
        user_spec = _build_exclude_spec(self._config.exclude_patterns)
        if _path_is_excluded(file_path, self._config.all_index_roots(), user_spec):
            logger.debug("Skipping excluded file %s", file_path)
            return {"total": 0, "indexed": 0, "skipped": 0, "deleted": 0, "errors": []}

        file_size = stat_result.st_size
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
        except (FileNotFoundError, NotADirectoryError, IsADirectoryError):
            # File is gone as a *file*: unlinked between stat and read (TOCTOU),
            # or the leaf path was replaced by a directory (``IsADirectoryError``
            # — ``stat`` succeeds on the dir, the read fails). Either way the old
            # source no longer exists, so purge its stale chunks. (#1566)
            return await self._delete_missing_source(file_path, path_scope=path_scope)
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

        # ADR-0006 Axes A.3/B.2 — secret-redaction trust boundary for
        # un-adjudicated indexing. Resolve scope first (needed both here and by
        # ``_apply_scope`` below) so a ``force_unsafe`` bulk index of a
        # ``project_shared`` file is hard-refused, then scan the raw content
        # before any chunk/embed/store work. Callers that already ran
        # ``privacy.enforce_write_guard`` at their own ingress layer pass
        # ``already_scanned=True`` to skip this — the boundary is enforced there,
        # and re-scanning the whole file would double-count and re-litigate
        # already-stored content (e.g. a prior ``force_unsafe`` write elsewhere
        # in the same file). See ADR-0006 "Implementation outline (PR-A)".
        scope_val, project_root = self._resolve_scope(file_path)
        if not already_scanned:
            guard = privacy.enforce_write_guard(
                content,
                surface="index",
                force_unsafe=force_unsafe,
                scope=scope_val,
                audit_context={"path": str(file_path)},
            )
            if guard.decision in ("blocked", "blocked_project_shared"):
                # Never log the matched bytes — only the hit count.
                logger.warning(
                    "redaction_blocked: %s (hits=%d, scope=%s)",
                    file_path.name,
                    len(guard.hits),
                    scope_val,
                )
                raise PrivacyRejection(
                    path=file_path,
                    hit_count=len(guard.hits),
                    scope=scope_val,
                    decision=guard.decision,
                )
            if guard.decision not in ("pass", "bypassed"):
                raise RuntimeError(f"unexpected enforce_write_guard decision: {guard.decision!r}")

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

        # ADR-0011: tag every chunk with its resolved scope. Default
        # ``("user", None)`` for files outside any registered project
        # tier; scope-aware behavior lands in PR-C / PR-D once the read
        # / write surfaces are spec'd.
        #
        # PR-D round 10 (M1) note: hash-diff means unchanged chunks
        # aren't re-UPSERTed on a regular reindex, so a previously
        # project_shared file whose project tier is later deregistered
        # keeps its stale ``scope='project_shared'`` / ``project_root``
        # rows in storage. The in-project default merge then surfaces
        # them whenever the user is back in the deregistered cwd.
        # ``mm reindex --force`` is the documented escape hatch:
        # ``force=True`` promotes every unchanged chunk into
        # ``to_upsert`` (line 789 below) and the subsequent UPSERT
        # overwrites the persisted scope with the freshly-resolved
        # value (defaults match ``ChunkMetadata.scope='user',
        # project_root=None``, so the ("user", None) skip below is
        # safe — the new chunks already carry the correct defaults).
        # The CHANGELOG ADR-0011 PR-B entry documents the
        # post-deregistration reindex requirement.
        # ``scope_val`` / ``project_root`` were resolved above (just before the
        # redaction gate); reuse them here rather than re-resolving.
        if scope_val != "user" or project_root is not None:
            new_chunks = self._apply_scope(new_chunks, scope_val, project_root)

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
        # Refuse to silently produce BM25-only chunks when the configured
        # embedder reports dimension=0. NoopEmbedder ("none" provider) is
        # the explicit BM25-only opt-in and bypasses this guard;
        # anything else with dim=0 is a misconfigured embedder (init
        # failed, fastembed download timed out, etc.) and was previously
        # papered over by the silent skip — chunks landed in ``chunks`` +
        # ``chunks_fts`` while ``chunks_vec`` stayed empty, leaving
        # semantic search returning nothing with no audit trail.
        if diff_result.to_upsert and self._embedder.dimension == 0:
            model = getattr(self._embedder, "model_name", "?")
            if model != "none":
                msg = (
                    f"Embedder reports dimension=0 but model={model!r} — "
                    "configured provider failed to initialize. Refusing "
                    "to index BM25-only chunks; fix the embedder config "
                    'or set embedding.provider="none" for intentional '
                    "BM25-only mode."
                )
                logger.error("%s file=%s chunks=%d", msg, file_path, len(diff_result.to_upsert))
                return {
                    "total": len(new_chunks),
                    "indexed": 0,
                    "skipped": len(new_chunks),
                    "deleted": 0,
                    "errors": [msg],
                }
        if diff_result.to_upsert and self._embedder.dimension > 0:
            texts = [c.retrieval_content for c in diff_result.to_upsert]
            # Threshold gate lives here, not inside the embedder, so callers
            # without a callback (CLI ``index_path``, direct test invocations)
            # never even compute the gating predicate. ``threshold == 0`` is
            # the explicit "always emit" debug semantic — see
            # ``EmbeddingConfig.progress_threshold`` docstring.
            emit_progress = on_chunk_progress is not None and (
                self._progress_threshold == 0 or len(texts) > self._progress_threshold
            )
            try:
                embeddings = await self._embedder.embed_texts(
                    texts,
                    on_progress=on_chunk_progress if emit_progress else None,
                )
                if len(embeddings) != len(texts):
                    # Defense in depth against a short embedding array (issue
                    # #1563). The HTTP providers now assert per-batch, but a
                    # bare ``zip`` here would silently drop the trailing chunks'
                    # vectors while still committing their content_hash — the
                    # diff logic would then classify them ``unchanged`` forever
                    # and never re-embed, a permanent semantic-search hole with
                    # no audit trail. Fail loud; the ``except`` below turns this
                    # into a zero-write early return so the file stays un-hashed
                    # and re-indexes cleanly on the next trigger.
                    raise EmbeddingError(
                        f"Embedder returned {len(embeddings)} vectors for "
                        f"{len(texts)} chunks in {file_path}; refusing to index "
                        "a truncated result."
                    )
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

        # Per-source AI summary refresh — runs *after* the transaction so a
        # slow LLM call never holds the chunk write lock. The signature
        # check inside ``maybe_update_ai_summary`` skips files whose chunk
        # set didn't change, so steady-state reindex pays nothing.
        # ``new_chunks`` is the full current chunk set for the file (not
        # just ``diff_result.to_upsert``); the signature must hash all
        # current chunks to remain stable when only some changed.
        from memtomem.indexing.summarizer import maybe_update_ai_summary

        await maybe_update_ai_summary(
            cast("SqliteBackend", self._storage), self._llm, file_path, new_chunks, self._config
        )

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
        *,
        force_unsafe: bool = False,
        path_scope: PathScope = "configured",
    ):
        """Like index_path(), but yields progress dicts as each file is processed.

        Yields dicts with ``type`` key:
        - ``"discovery"``: emitted exactly once after the file walk has
          determined ``files_total`` and before any per-file work begins.
          Fields: ``files_total``. Lets CLI progress bars set their length
          without re-walking the tree (the helper would otherwise have to
          pre-compute ``expected_total`` via its own ``rglob``, duplicating
          I/O and undercounting non-``.md`` corpora — see issue #743).
          Skipped only when the path doesn't resolve to a file or
          directory (in which case the next event is ``complete`` with
          ``total_files=0``).
        - ``"chunk_progress"``: emitted *during* a single file's embedding
          when the file produces more chunks than
          ``EmbeddingConfig.progress_threshold``. Fields: ``file,
          chunks_done, chunks_total, files_done, files_total``. ``chunks_done``
          is a monotonically non-decreasing **count** of texts whose embeddings
          have completed — NOT a positional index, since concurrent batches
          (OpenAI/Ollama) finish in arbitrary order.
        - ``"progress"``: emitted after each file with fields
          ``file, files_done, files_total, indexed, skipped``.
        - ``"complete"``: final summary — ``total_files, total_chunks,
          indexed_chunks, skipped_chunks, deleted_chunks, duration_ms,
          errors``. ``errors`` is a list of human-readable strings in the
          same loose shape as ``IndexingStats.errors`` so non-stream UI
          handlers reuse verbatim. Empty list when the run had no errors.

        Locking: each file is indexed under the same L2 sidecar →
        L3 ``_index_lock`` pair as ``index_file`` (via
        ``_index_file_locked``), taken **per file** so a stream run
        serializes against watcher/CLI/CRUD reindexes of the same file
        without holding a lock across the whole tree walk (#1574 item 6).
        The ``_active_runs`` counter is still bumped once per stream run
        so ``GET /api/indexing/active`` covers discovery and the gaps
        between files, where no lock is held.
        """
        self._active_runs += 1
        try:
            start = time.monotonic()
            path = path.resolve()

            if path_scope == "configured" and not self._is_within_memory_dirs(path):
                yield {
                    "type": "complete",
                    "total_files": 0,
                    "total_chunks": 0,
                    "indexed_chunks": 0,
                    "skipped_chunks": 0,
                    "deleted_chunks": 0,
                    "duration_ms": 0.0,
                    "errors": [f"path is outside configured memory directories: {path}"],
                    "resolved_namespaces": [],
                    "blocked_files": 0,
                    "blocked_paths": [],
                    "blocked_project_shared_files": 0,
                }
                return

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
                    "errors": [f"index path does not exist: {path}"],
                    "resolved_namespaces": [],
                    "blocked_files": 0,
                    "blocked_paths": [],
                    "blocked_project_shared_files": 0,
                }
                return

            total_files = len(files)
            # Discovery event lets CLI progress bars set their length from the
            # actual indexable file count instead of pre-computing via a
            # duplicate ``rglob`` walk (issue #743). Emitted unconditionally
            # so the helper's lazy-bar branch fires for empty discovers too
            # (length=0 bar is still a valid render and avoids a special case
            # downstream).
            yield {"type": "discovery", "files_total": total_files}
            # Pre-compute the namespace echo so the complete event surfaces
            # what was actually applied — single render across both stream
            # and non-stream paths (see ``_index_path_inner``).
            resolved_ns_for_event = self.resolve_namespaces_for(files, namespace)
            agg = {
                "total_chunks": 0,
                "indexed": 0,
                "skipped": 0,
                "deleted": 0,
                "blocked": 0,
                "blocked_project_shared": 0,
            }
            all_errors: list[str] = []
            blocked_paths: list[str] = []

            for i, fp in enumerate(files, start=1):
                # Per-file queue forwards ``chunk_progress`` ticks from the
                # embedder (running inside the ``runner`` task) back to this
                # generator in real time. Without the queue+task split the
                # ``await self._index_file`` would block until the file is
                # fully embedded, defeating the purpose of mid-file progress.
                queue: asyncio.Queue = asyncio.Queue()
                DONE = object()

                # ``fp=fp, idx=i`` default-bind at definition so any future
                # refactor lifting ``runner`` out of the loop or fanning out
                # tasks won't silently regress to late-bound closure capture.
                async def runner(
                    fp: Path = fp,
                    idx: int = i,
                ) -> IndexFileResult:
                    def cb(done: int, total: int) -> None:
                        queue.put_nowait(
                            {
                                "type": "chunk_progress",
                                "file": str(fp),
                                "chunks_done": done,
                                "chunks_total": total,
                                "files_done": idx - 1,
                                "files_total": total_files,
                            }
                        )

                    try:
                        # Same L2 sidecar → L3 ``_index_lock`` policy as
                        # ``index_file``, taken per file so streaming progress
                        # survives (#1574 item 6). A sidecar timeout raises
                        # ``TimeoutError``, which the ``except Exception``
                        # below folds into this file's ``errors`` — the
                        # stream continues with the next file.
                        result, _ = await self._index_file_locked(
                            fp.resolve(),
                            force,
                            namespace=namespace,
                            on_chunk_progress=cb,
                            force_unsafe=force_unsafe,
                            path_scope=path_scope,
                        )
                        return result
                    finally:
                        queue.put_nowait(DONE)

                task = asyncio.create_task(runner())
                try:
                    while True:
                        event = await queue.get()
                        if event is DONE:
                            break
                        yield event
                    try:
                        result = await task
                    except PrivacyRejection as exc:
                        # ADR-0006 PR-A: un-adjudicated bulk index hit a
                        # secret-bearing file. Skip it, record it blocked, and
                        # continue the stream (mirrors the non-stream branch in
                        # ``_index_path_inner``).
                        logger.warning(
                            "Stream indexing blocked by redaction guard for %s: %s", fp, exc
                        )
                        blocked_paths.append(str(fp))
                        result = {
                            "total": 0,
                            "indexed": 0,
                            "skipped": 0,
                            "deleted": 0,
                            "errors": [
                                f"{fp.name}: redaction_blocked "
                                f"(hits={exc.hit_count}, scope={exc.scope}, decision={exc.decision})"
                            ],
                            "blocked": 1,
                            "blocked_project_shared": 1 if exc.scope == "project_shared" else 0,
                        }
                    except Exception as exc:
                        logger.error("Stream indexing failed for %s: %s", fp, exc)
                        # Same shape as non-stream's
                        # ``asyncio.gather(return_exceptions=True)`` branch
                        # in ``_index_path_inner`` so consumers see the same
                        # error shape regardless of stream vs non-stream.
                        result = {
                            "total": 0,
                            "indexed": 0,
                            "skipped": 0,
                            "deleted": 0,
                            "errors": [f"{fp.name}: {exc}"],
                        }
                except BaseException:
                    # Generator was closed (HTTPException, client disconnect,
                    # consumer ``aclose()``). Cancel the in-flight embedding
                    # task so we don't leak an OpenAI request / ONNX inference
                    # past the lifetime of the SSE response. The outer
                    # ``finally`` below still decrements ``_active_runs``.
                    task.cancel()
                    with contextlib.suppress(BaseException):
                        await task
                    raise

                agg["total_chunks"] += result["total"]
                agg["indexed"] += result["indexed"]
                agg["skipped"] += result["skipped"]
                agg["deleted"] += result["deleted"]
                agg["blocked"] += result.get("blocked", 0)
                agg["blocked_project_shared"] += result.get("blocked_project_shared", 0)
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
                "blocked_files": agg["blocked"],
                "blocked_paths": blocked_paths,
                "blocked_project_shared_files": agg["blocked_project_shared"],
            }
        finally:
            self._active_runs -= 1

    @staticmethod
    def _apply_namespace(chunks: list[Chunk], namespace: str) -> list[Chunk]:
        """Return new Chunk instances with the given namespace applied.

        Uses ``dataclasses.replace`` so any new ``ChunkMetadata`` fields
        (e.g. the ADR-0011 ``scope`` / ``project_root`` columns) are
        carried through automatically. The earlier explicit-constructor
        shape silently dropped fields the writer hadn't been updated to
        copy, which is the kind of bug a future field add would
        otherwise reintroduce.
        """
        result = []
        for c in chunks:
            new_meta = dataclasses.replace(c.metadata, namespace=namespace)
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

    @staticmethod
    def _apply_scope(chunks: list[Chunk], scope: str, project_root: Path | None) -> list[Chunk]:
        """Return new Chunk instances with the given scope + project_root.

        ADR-0011 §2 plumbing: indexing tags every chunk with its resolved
        scope so search can scope-filter without re-classifying paths at
        query time. Mirrors :meth:`_apply_namespace`'s shape; uses
        ``dataclasses.replace`` for the same field-evolution reason.
        """
        result = []
        for c in chunks:
            new_meta = dataclasses.replace(c.metadata, scope=scope, project_root=project_root)
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

    def _resolve_scope(self, file_path: Path) -> tuple[str, Path | None]:
        """Classify ``file_path`` into ``(scope, project_root)`` (ADR-0011 §2).

        Path-based — the same ``classify_scope`` helper that the config
        module uses. Wrapped on the engine so callers stay decoupled
        from the config-module helper's signature; future enhancements
        (e.g. memoization, additional registry sources) land here
        without touching call sites.
        """
        return classify_scope(file_path, self._config.project_memory_dirs)

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
        memory_dirs = self._config.all_index_roots()

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
    """Produce a single Chunk by merging ``current`` and ``nxt``.

    Uses ``dataclasses.replace`` so any ``ChunkMetadata`` field added
    after this code was written carries through the merge automatically
    — explicit constructor arguments would silently drop new fields.
    Today this matters for ``scope`` / ``project_root`` (ADR-0011) and
    ``valid_from_unix`` / ``valid_to_unix`` (temporal-validity RFC),
    all of which need to survive merge so search still respects scope
    boundaries and validity windows on merged output. Mirrors
    :meth:`_apply_namespace` / :meth:`_apply_scope`.
    """
    hierarchy = _merged_hierarchy(current, nxt)
    content = _build_merged_content(current, nxt, hierarchy)
    new_meta = dataclasses.replace(
        current.metadata,
        heading_hierarchy=hierarchy,
        end_line=nxt.metadata.end_line,
        tags=tuple(set(current.metadata.tags) | set(nxt.metadata.tags)),
    )
    return Chunk(content=content, metadata=new_meta)


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
        # ``dataclasses.replace`` so future ``ChunkMetadata`` fields
        # (scope / project_root / valid_from_unix / valid_to_unix /
        # next-RFC additions) carry through automatically. Explicit
        # constructor args would silently drop fields the merger
        # doesn't know about — same rationale as :meth:`_merge_pair`.
        new_meta = dataclasses.replace(c.metadata, overlap_before=ob, overlap_after=oa)
        result.append(Chunk(content=new_content, metadata=new_meta))
    return result
