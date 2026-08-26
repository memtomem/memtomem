"""Indexing engine: orchestrates chunking, embedding, and storage."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import inspect
import logging
import os
import stat as stat_module
import time
from collections.abc import Awaitable, Callable, Iterable, Iterator, Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID
from typing import TYPE_CHECKING, Any, Literal, TypedDict, cast

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
from memtomem.errors import EmbeddingError, NamespaceResolutionError, RetryableError
from memtomem.generation import ComponentGeneration
from memtomem.indexing.differ import DiffResult, compute_diff
from memtomem.indexing.redaction_exemption import declared_exemption
from memtomem.models import Chunk, IndexingStats
from memtomem.tools.entity_sync import sync_entities_for_chunks

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider
    from memtomem.storage.base import StorageBackend
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)

PathScope = Literal["configured", "explicit"]

_MAX_INDEX_FILE_BYTES = 10 * 1024 * 1024  # 10 MB

# Max files whose ``_index_file`` pipelines run concurrently in a bulk
# ``index_path`` run. Also the default and upper bound for the embedding
# concurrency below.
_FILE_CONCURRENCY = 8


def _resolve_embed_limit(embedder: object) -> int:
    """Resolve a provider's optional ``preferred_concurrency`` hint (#1783).

    Accepts only a real ``int`` (``bool`` excluded), clamped to
    ``[1, _FILE_CONCURRENCY]``; anything else — attribute absent, a Mock
    auto-attribute from ``AsyncMock`` test embedders, a malformed
    third-party value — falls back to ``_FILE_CONCURRENCY`` (the pre-#1783
    behavior). See the contract comment on ``EmbeddingProvider`` in
    ``embedding/base.py``.
    """
    hint = getattr(embedder, "preferred_concurrency", None)
    if isinstance(hint, int) and not isinstance(hint, bool):
        return max(1, min(_FILE_CONCURRENCY, hint))
    return _FILE_CONCURRENCY


def _supports_input_context(embedder: object) -> bool:
    """Return whether the concrete embed method accepts context kwargs."""
    if getattr(embedder, "supports_input_context", None) is not True:
        return False
    try:
        parameters = inspect.signature(embedder.embed_texts).parameters
    except (TypeError, ValueError, AttributeError):
        return False
    if any(param.kind is inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
        return True
    return {"source_path", "chunk_indices"}.issubset(parameters)


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


def _distinct_sorted(values: Iterable[str | None]) -> list[str | None]:
    """Distinct namespaces in stable sort order, ``None`` (untagged) last."""
    return sorted(set(values), key=lambda x: (x is None, x or ""))


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

    retryable = False

    def __init__(self, *, path: Path, hit_count: int, scope: str, decision: str) -> None:
        self.path = path
        self.hit_count = hit_count
        self.scope = scope
        self.decision = decision
        super().__init__(f"redaction_blocked: {path.name} (hits={hit_count}, decision={decision})")


class NamespaceMixedUnderForceError(Exception):
    """A forced re-index would collapse a multi-namespace file into one namespace.

    ``force=True`` promotes every unchanged chunk into ``to_upsert``, and the
    whole upsert carries one namespace — so a file whose rows are split across
    several namespaces would have all of them rewritten to the rule-resolved
    one, moving agent-scoped content into a searchable namespace with no way
    back (the day-file name encoding is one-way, so nothing on disk can
    restore it). Permanent, not retryable: re-running changes nothing. The
    caller picks an out — an explicit namespace, ``reassign_namespaces=True``,
    or splitting the file.

    The message carries ``file_path.name`` only. Bulk error strings are echoed
    verbatim through the web complete event and API responses, which redact
    host paths.
    """

    retryable = False


#: Why a file's chunks got the namespace they got (#2061). ``preserved`` and
#: ``preserved_against_rules`` both mean "the stored namespace won"; the
#: latter additionally means current rules would have chosen differently, and
#: is what the CLI advisory counts so a user whose rule edit did not take
#: effect is told which command applies it.
#: ``bound_new_source`` is the session-inheritance carve-out (#2104): a caller
#: that binds writes to a context namespace (an MCP agent session, a
#: ``mem_ns_set`` current namespace) passes it as ``new_source_namespace``, and
#: it applies only to sources with no stored rows. An already-indexed file
#: keeps its namespace, so a bulk re-index under a session cannot move content
#: the session did not write.
NamespaceDecisionReason = Literal[
    "explicit",
    "preserved",
    "preserved_against_rules",
    "resolved",
    "reassigned",
    "mixed_force_refused",
    "bound_new_source",
]


@dataclasses.dataclass(frozen=True)
class NamespaceDecision:
    """The namespace a file's chunks get, the rows it had, and why.

    One structured answer instead of a bare ``str | None`` so the reporting
    surfaces (advisory counters, move summary, the system-namespace warning)
    read the *authoritative in-lock* resolution rather than re-deriving it
    from a pre-write preview that a concurrent writer may have invalidated.
    """

    target: str | None
    stored: tuple[str, ...] = ()
    reason: NamespaceDecisionReason = "resolved"


def _reject_reassign_with_explicit_ns(
    namespace: str | None, reassign: bool, new_source_namespace: str | None = None
) -> None:
    """Refuse the flag pairs that cannot mean anything coherent.

    An explicit namespace short-circuits rule resolution, so pairing it with
    ``reassign_namespaces=True`` asks for two different targets at once. The
    CLI rejects the combination too, but the check belongs here as well: the
    engine entrypoints are public API, and silently letting the explicit
    namespace win would drop the reassignment — and with it the stored lookup
    that reporting depends on — without a word.

    ``new_source_namespace`` is refused alongside ``reassign`` for the same
    reason at the one place the two overlap: a source with no stored rows.
    Reassignment says "the rules decide", session binding says "the session
    decides", and a run cannot honor both. Files that *do* have rows are
    unaffected by the binding, so the conflict is real but narrow — narrow
    enough that letting one win silently would be indistinguishable from the
    other having been applied (#2104).
    """
    if reassign and namespace is not None:
        raise ValueError(
            "reassign_namespaces=True cannot be combined with an explicit namespace: "
            "an explicit namespace short-circuits the rules that reassignment exists "
            "to apply. Pass one or the other."
        )
    if reassign and new_source_namespace is not None:
        raise ValueError(
            "reassign_namespaces=True cannot be combined with new_source_namespace: "
            "reassignment resolves every file through the path rules, including the "
            "row-less ones the binding would claim. Pass one or the other."
        )


@dataclasses.dataclass
class _NamespaceTally:
    """Run-level roll-up of per-file :class:`NamespaceDecision` records.

    Shared by both bulk paths so the stream and non-stream surfaces cannot
    report different numbers for the same run.
    """

    preserved_against_rules: int = 0
    reassigned: int = 0
    moves: dict[tuple[str, str], int] = dataclasses.field(default_factory=dict)

    def record(
        self,
        decision: NamespaceDecision,
        *,
        written: bool,
        canonical: Callable[[str | None], str],
    ) -> None:
        if decision.reason == "preserved_against_rules":
            # Counted whether or not a write followed: an unchanged file that
            # kept a namespace the current rules disagree with is exactly the
            # case the advisory exists to name.
            self.preserved_against_rules += 1
            return
        if decision.reason != "reassigned" or not written:
            # A move is only real once its namespace-bearing upsert committed.
            return
        self.reassigned += 1
        target = canonical(decision.target)
        for old in decision.stored:
            old_canonical = canonical(old)
            if old_canonical == target:
                continue
            self.moves[(old_canonical, target)] = self.moves.get((old_canonical, target), 0) + 1

    def summary(self) -> tuple[dict[str, object], ...]:
        """Moves as structured records, not rendered lines.

        Rendering here would force every consumer to parse the string back
        apart. The CLI's system-scope warning needs both endpoints (a move
        from one system namespace to another exposes nothing) and the file
        count (one line can stand for many files), so the numbers travel as
        numbers.
        """
        return tuple(
            {"from": old, "to": new, "files": count}
            for (old, new), count in sorted(self.moves.items())
        )


class _IndexFileBase(TypedDict):
    total: int
    indexed: int
    skipped: int
    deleted: int
    errors: list[str]


class IndexFileResult(_IndexFileBase, total=False):
    new_chunk_ids: list[UUID]
    # Ordered same-string subset of ``errors`` whose underlying exception is
    # typed retryable. Optional so every historical zero/permanent return shape
    # remains valid.
    retryable_errors: list[str]
    # Present only when at least one chunk was successfully upserted. The
    # value comes from the authoritative namespace resolution inside the
    # per-file critical section; ``None`` is the valid untagged carve-out.
    resolved_namespace: str | None
    # The authoritative in-lock namespace resolution and its reason (#2061).
    # Present once resolution ran, whether or not a write followed — the
    # "rules would have chosen differently" advisory is true for an unchanged
    # file too. ``namespace_written`` says whether a namespace-bearing upsert
    # actually committed, which is what the move counters require.
    namespace_decision: NamespaceDecision
    namespace_written: bool
    # Set to 1 by the stream path when a file is skipped by the ADR-0006
    # redaction gate; aggregated into ``IndexingStats.blocked_files``. The
    # non-stream path tracks blocks via the raised ``PrivacyRejection`` instead.
    blocked: int
    # 1 when the blocked file was ``project_shared`` (hard-refused, not
    # bypassable with force_unsafe) — aggregated into
    # ``IndexingStats.blocked_project_shared_files``.
    blocked_project_shared: int
    # 1 when the file carried a frontmatter ``redaction: documents-patterns``
    # declaration the guard honoured (#2076). Set only on the success return,
    # after the chunk transaction commits: an exempted file whose embedding or
    # storage write then fails was not indexed under an exemption, and
    # counting it would overstate how often the valve is actually used. A file
    # whose chunks were all unchanged *does* count — it was adjudicated and
    # admitted under the declaration this run, and a standing bypass that went
    # unreported on steady-state re-indexes would be invisible exactly when
    # someone should notice it is still in force.
    exempted: int
    # Ids of the chunks this file left untouched because their content hash
    # already matched — the ``DiffResult.unchanged`` set, and the only chunks
    # a run can leave without a vector while reporting success (#2115).
    # Deliberately NOT derivable from ``skipped``: an embedding failure also
    # reports every chunk of the file as skipped, and those chunks were never
    # written at all. Collected only when the embedder is supposed to produce
    # vectors, so a BM25-only store carries no id lists.
    unchanged_chunk_ids: list[str]
    # True iff this file committed a durable, search-visible chunk write:
    # a delete, an upsert, a line-range refresh, or a metadata-only refresh
    # (tags #2124 / validity window #2140). Deliberately NOT derivable from
    # the counters: metadata-only rows are reported as ``skipped`` and the
    # line-range refresh is reported nowhere, so ``indexed + deleted > 0``
    # misses exactly the writes search filters on. Consumers use it to decide
    # whether to drop the search result cache (#2141). Absent on every path
    # that returns before the transaction commits — read it with
    # ``.get("mutated", False)``.
    mutated: bool


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
        generation: ComponentGeneration | None = None,
    ) -> None:
        self._storage = storage
        self._embedder = embedder
        # The embedder/pipeline/engine generation this engine belongs to
        # (#2180); see the matching field on ``SearchPipeline``. Held for the
        # span of every entry point that can reach ``self._embedder``, so
        # ``revert_to_stored`` cannot close the embedder under a running
        # index. An engine built on its own gets a private, never-retired
        # handle.
        self._generation = generation or ComponentGeneration()
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
        # Level L3 of the memory-file lock order (see ``context._atomic``
        # module docstring): the per-file sidecar (L2) is acquired ABOVE this
        # lock, never below, so no path ever waits on a sidecar while holding
        # ``_index_lock`` (#1587).
        #
        # Engine-wide, not per file. ``index_file`` and ``index_path_stream``
        # hold it per file; the bulk ``index_path`` path does NOT (#2105) —
        # holding it across a run would mean acquiring L2 under L3, the
        # reverse-order cycle #1587 removed, and holding it per file would
        # collapse the run's ``_FILE_CONCURRENCY``-way pipeline to one file at
        # a time. Bulk relies on L2 instead, whose layer-1 in-process
        # ``asyncio.Lock`` gives same-process same-file exclusion; it falls
        # back to L3 only where the sidecar is skipped (``lock_held`` or the
        # #1566 parent-gone case). The resource ceilings a run-wide L3 used to
        # imply now live in the engine-scoped semaphores below.
        self._index_lock = asyncio.Lock()
        # Engine-wide file / embedding concurrency ceilings (#2105). Both were
        # per-run semaphores while a run-wide ``_index_lock`` made runs
        # mutually exclusive; once runs (and runs vs. ``index_file`` / stream)
        # can overlap, per-run objects would multiply the ceiling by the number
        # of in-flight runs — including the #1783 ONNX activation-memory cap.
        # Keyed by event loop for the same reason as
        # ``context._atomic._intra_async_lock_for``: a single instance-level
        # semaphore binds to the first loop that awaits it and then raises
        # "bound to a different event loop" when reused, and pytest gives each
        # async test its own loop.
        self._file_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
        self._embed_sems: dict[asyncio.AbstractEventLoop, asyncio.Semaphore] = {}
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

    @contextlib.contextmanager
    def _active_run(self) -> Iterator[None]:
        """Count one in-flight run and pin its component generation (#2180).

        The generation hold spans the same window as the ``_active_runs``
        counter — entry to exit of a public entry point — because that is
        exactly the window in which the run may await ``self._embedder``, and
        ``revert_to_stored`` retires that embedder out from under it.
        """
        self._active_runs += 1
        try:
            with self._generation.hold():
                yield
        finally:
            self._active_runs -= 1

    @staticmethod
    def _loop_local_semaphore(
        registry: dict[asyncio.AbstractEventLoop, asyncio.Semaphore], limit: int
    ) -> asyncio.Semaphore:
        """Return *registry*'s semaphore for the running loop, creating it once.

        Closed loops are pruned on the new-loop path — a contended semaphore
        strongly references its bound loop, so a ``WeakKeyDictionary`` could
        not reclaim it. Mirrors ``context._atomic._intra_async_lock_for``.
        """
        loop = asyncio.get_running_loop()
        sem = registry.get(loop)
        if sem is None:
            for dead in [lp for lp in registry if lp.is_closed()]:
                del registry[dead]
            sem = asyncio.Semaphore(limit)
            registry[loop] = sem
        return sem

    def _file_semaphore(self) -> asyncio.Semaphore:
        """Engine-wide cap on concurrently running ``_index_file`` pipelines."""
        return self._loop_local_semaphore(self._file_sems, _FILE_CONCURRENCY)

    def _embed_semaphore(self) -> asyncio.Semaphore:
        """Engine-wide cap on concurrent ``embed_texts`` calls (#1783)."""
        return self._loop_local_semaphore(self._embed_sems, _resolve_embed_limit(self._embedder))

    async def index_path(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
        *,
        force_unsafe: bool = False,
        path_scope: PathScope = "configured",
        reassign_namespaces: bool = False,
        new_source_namespace: str | None = None,
    ) -> IndexingStats:
        _reject_reassign_with_explicit_ns(namespace, reassign_namespaces, new_source_namespace)
        force = force or reassign_namespaces
        with self._active_run():
            # No run-wide ``_index_lock`` (#2105): each file takes its own L2
            # sidecar inside ``_bounded`` instead, which is what serializes
            # this run against another process touching the same file. Holding
            # L3 here would put every one of those L2 acquires under L3 — the
            # reverse-order cycle #1587 removed. Run-vs-run and run-vs-CRUD
            # exclusion is therefore per file, not per run; the concurrency
            # ceilings a run-wide lock used to imply live in the engine-scoped
            # semaphores.
            return await self._index_path_inner(
                path,
                recursive,
                force,
                namespace,
                force_unsafe=force_unsafe,
                path_scope=path_scope,
                reassign_namespaces=reassign_namespaces,
                new_source_namespace=new_source_namespace,
            )

    async def _count_missing_vectors(self, chunk_ids: Sequence[str]) -> int:
        """Count how many hash-matched chunks this run left without a vector.

        Takes the ids the files reported rather than deriving a scope from
        counters: ``skipped`` also covers files whose embedding failed, whose
        chunks were never written and so cannot be missing anything. One
        batched query per run, only on runs that actually skipped something
        under a vector-producing embedder — a clean run pays nothing.

        Failure is not a gap: if the count cannot be taken, report 0 rather
        than telling the operator to re-embed on a guess.
        """
        if not chunk_ids:
            return 0
        try:
            return await self._storage.count_chunks_missing_vectors(chunk_ids)
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("missing-vector count unavailable: %s", exc)
            return 0

    @staticmethod
    def _unchanged_ids(file_results: Iterable[IndexFileResult]) -> list[str]:
        """Flatten the per-file hash-matched chunk ids of a run."""
        ids: list[str] = []
        for result in file_results:
            ids.extend(result.get("unchanged_chunk_ids", ()))
        return ids

    async def _index_path_inner(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
        *,
        force_unsafe: bool = False,
        path_scope: PathScope = "configured",
        reassign_namespaces: bool = False,
        new_source_namespace: str | None = None,
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

        # Both concurrency caps are engine-scoped and applied by
        # ``_index_file_locked`` (#2105) — see the accessors on
        # ``IndexEngine``. Nothing to set up per run.

        # Per-file namespaces, resolved BEFORE the writes — the same ordering
        # the stream variant uses. Namespace preservation reads the store
        # (issue #2005), so a store that cannot answer must fail the run
        # here, before any durable write, as the typed retryable error
        # (issue #2018). The per-file write still resolves inside its own
        # critical section — a concurrent writer may have moved a file since
        # this prepass — and a lookup failure there fails that file closed,
        # keeping its type in ``retryable_errors`` below.
        prepass_namespaces = await self._resolve_namespaces_per_file(
            files,
            namespace,
            force=force,
            reassign=reassign_namespaces,
            new_source_namespace=new_source_namespace,
        )

        async def _bounded(fp: Path) -> IndexFileResult:
            # ``engine_serialized=False``: take this file's L2 sidecar but not
            # L3, so the run stays ``_FILE_CONCURRENCY``-way parallel while
            # still serializing per file against other processes and other
            # in-process indexers (#2105). The file-concurrency slot is taken
            # inside ``_index_file_locked``, above L2, together with every
            # other entry point's.
            result, _ = await self._index_file_locked(
                fp,
                force,
                namespace=namespace,
                force_unsafe=force_unsafe,
                path_scope=path_scope,
                reassign_namespaces=reassign_namespaces,
                new_source_namespace=new_source_namespace,
                engine_serialized=False,
            )
            return result

        raw_results = await asyncio.gather(*[_bounded(f) for f in files], return_exceptions=True)
        file_results: list[IndexFileResult] = []
        all_errors: list[str] = []
        retryable_errors: list[str] = []
        blocked_paths: list[str] = []
        blocked_project_shared = 0
        exempted_paths: list[str] = []
        applied_namespaces: list[str | None] = []
        # Keep the prepass vector positionally aligned with ``files``. A
        # successful upsert replaces its entry below with the authoritative
        # in-lock answer; no-write and failed outcomes retain this preview.
        echo_namespaces = list(prepass_namespaces)
        tally = _NamespaceTally()
        for i, r in enumerate(raw_results):
            if isinstance(r, dict):
                file_results.append(r)
                if r.get("exempted"):
                    exempted_paths.append(str(files[i]))
                all_errors.extend(r.get("errors", []))
                retryable_errors.extend(r.get("retryable_errors", []))
                decision = r.get("namespace_decision")
                if decision is not None:
                    tally.record(
                        decision,
                        written=r.get("namespace_written", False),
                        canonical=self._canonical_namespace,
                    )
                if "resolved_namespace" in r:
                    # ``None`` is a real applied value, so key presence — not
                    # truthiness — decides whether to replace the prepass.
                    echo_namespaces[i] = r["resolved_namespace"]
                    applied_namespaces.append(r["resolved_namespace"])
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
                msg = f"{files[i].name}: {r}"
                all_errors.append(msg)
                # ``gather(return_exceptions=True)`` erases types; re-read
                # this one so a transient store outage stays tellable apart
                # from a permanently broken file (issue #2018). A sidecar
                # acquire that ran out its budget is the same kind of
                # transient — the watcher already re-queues on it and
                # ``mm index`` drives its hook retry off
                # ``retryable_errors`` — but ``TimeoutError`` is not a
                # ``RetryableError`` subclass, so name it (#2105).
                if isinstance(r, RetryableError | TimeoutError):
                    retryable_errors.append(msg)

        # Aggregate new_chunk_ids across all files — preserves per-file order
        # so callers that sort/filter by source get a consistent ordering.
        all_new_chunk_ids: list[UUID] = []
        for r in file_results:
            ids = r.get("new_chunk_ids", ())
            if ids:
                all_new_chunk_ids.extend(ids)

        duration = (time.monotonic() - start) * 1000
        missing_vectors = await self._count_missing_vectors(self._unchanged_ids(file_results))
        return IndexingStats(
            total_files=len(files),
            total_chunks=sum(r["total"] for r in file_results),
            indexed_chunks=sum(r["indexed"] for r in file_results),
            skipped_chunks=sum(r["skipped"] for r in file_results),
            deleted_chunks=sum(r["deleted"] for r in file_results),
            duration_ms=duration,
            errors=tuple(dict.fromkeys(all_errors)),
            retryable_errors=tuple(dict.fromkeys(retryable_errors)),
            new_chunk_ids=tuple(all_new_chunk_ids),
            resolved_namespaces=tuple(_distinct_sorted(echo_namespaces)),
            applied_namespaces=tuple(_distinct_sorted(applied_namespaces)),
            blocked_files=len(blocked_paths),
            blocked_paths=tuple(blocked_paths),
            blocked_project_shared_files=blocked_project_shared,
            exempted_files=len(exempted_paths),
            exempted_paths=tuple(exempted_paths),
            namespaces_preserved_against_rules=tally.preserved_against_rules,
            namespaces_reassigned=tally.reassigned,
            namespace_moves=tally.summary(),
            chunks_missing_vectors=missing_vectors,
            mutated=any(r.get("mutated", False) for r in file_results),
        )

    async def resolve_namespaces_for(
        self,
        files: list[Path],
        explicit_ns: str | None = None,
        *,
        force: bool = False,
        reassign: bool = False,
        new_source_namespace: str | None = None,
    ) -> list[str | None]:
        """Resolve namespaces for ``files`` in stable (sort) order, distinct.

        Public companion to ``effective_namespace_for`` for callers (preview
        route, future surfaces) that need the namespace echo without running
        the indexer. ``None`` represents the
        ``default_namespace == "default"`` carve-out (untagged).

        Async because namespace preservation reads the store: a preview that
        answered from the rules alone would promise a namespace the write
        would not use for any file that already has chunks (issue #2005).
        Pass the same ``force`` the write will use, or the preview answers
        for a different operation than the one being previewed.
        """
        _reject_reassign_with_explicit_ns(explicit_ns, reassign, new_source_namespace)
        return _distinct_sorted(
            await self._resolve_namespaces_per_file(
                files,
                explicit_ns,
                force=force,
                reassign=reassign,
                new_source_namespace=new_source_namespace,
            )
        )

    async def _resolve_namespaces_per_file(
        self,
        files: list[Path],
        explicit_ns: str | None = None,
        *,
        force: bool = False,
        reassign: bool = False,
        new_source_namespace: str | None = None,
    ) -> list[str | None]:
        """Each file's effective namespace, positionally aligned with ``files``.

        The bulk paths' pre-write prepass (issue #2018): a store that cannot
        answer fails the whole run here, before any durable write, as the
        typed retryable error. The write path re-resolves inside each file's
        critical section — that answer stays authoritative for successful
        upserts. These entries are the fallback echo for files that perform
        no namespace-bearing write or fail before returning a result.

        Only lookup failures abort the run from here. A file whose stored
        namespaces are mixed under ``force`` is refused by the *write*, not by
        this prepass (#2061) — raising here would abort the whole run before
        per-file error handling, so one unsplittable legacy file would block
        indexing every other file in the tree.
        """
        return [
            await self.effective_namespace_for(
                f,
                explicit_ns,
                force=force,
                reassign=reassign,
                new_source_namespace=new_source_namespace,
            )
            for f in files
        ]

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

    def chunk_content(self, file_path: Path, content: str) -> list[Chunk]:
        """Chunk ``content`` exactly as indexing would, post-processing included.

        Single source of truth for "which chunks would this file produce" — the
        companion to :meth:`discover_indexable_files`. ``_index_file`` calls it,
        and so does ``mm memory doctor``'s ``stale_index`` check, which needs
        the *same* chunk boundaries and content hashes the indexer would write
        in order to tell a real content change from a bare ``touch`` (#2078).

        Pure: no storage, no embedder, no privacy scan, no namespace/scope
        resolution — those stay in ``_index_file``, and none of them affect
        ``content_hash`` or ``heading_hierarchy``. Callers that only hold a
        storage-less engine (the doctor's discovery engine) can use it safely.
        Returns ``[]`` for a suffix no chunker is registered for.
        """
        chunks = self._registry.chunk_file(file_path, content)
        # Post-processing: merge short chunks + add overlap
        chunks = _merge_short_chunks(
            chunks,
            self._config.min_chunk_tokens,
            self._config.max_chunk_tokens,
            self._config.target_chunk_tokens,
        )
        if self._config.chunk_overlap_tokens > 0:
            chunks = _add_overlap(chunks, self._config.chunk_overlap_tokens)
        return chunks

    async def _index_file_locked(
        self,
        path: Path,
        force: bool,
        *,
        namespace: str | None = None,
        on_chunk_progress: Callable[[int, int], None] | None = None,
        force_unsafe: bool = False,
        already_scanned: bool = False,
        lock_held: bool = False,
        path_scope: PathScope = "configured",
        reassign_namespaces: bool = False,
        new_source_namespace: str | None = None,
        engine_serialized: bool = True,
    ) -> tuple[IndexFileResult, float]:
        """Run ``_index_file`` under the L2 sidecar → L3 ``_index_lock`` pair.

        Single home for the per-file lock policy so ``index_file``,
        ``index_path_stream`` and the bulk ``index_path`` fan-out cannot drift
        (#1574 item 6, #2105). Returns the raw per-file result plus the
        duration (ms) of the indexing work itself — measured inside the locks,
        so lock-wait time is excluded.

        ``engine_serialized=False`` takes the sidecar (L2) but NOT
        ``_index_lock`` (L3) — the bulk path, whose files run
        ``_FILE_CONCURRENCY``-way concurrently and would otherwise serialize
        on the one engine-wide L3. L2 still supplies both halves of what that
        path needs: its layer-1 in-process ``asyncio.Lock`` excludes another
        indexer in this process on the same file, its flock excludes another
        process. The ``skip_sidecar`` branch below ignores the flag and takes
        L3 regardless, so a file with no sidecar to hold is never unlocked.

        The lock is keyed on ``path.resolve()`` while ``_index_file`` receives
        ``path`` **as given**. Every caller must contend on one sidecar per
        physical file, but the work path is separately load-bearing: namespace
        rules pattern-match it (``_resolve_namespace_directed``), so resolving
        a symlinked leaf discovered by
        the bulk walk would match the target's rules instead of the alias's,
        including under ``--reassign-namespaces``. Storage identity is not at
        stake either way — ``storage.sqlite_helpers.norm_path`` resolves
        ``source_file`` on write.

        Known limit, unchanged in kind by #2105: a symlink retargeted between
        the ``resolve()`` here and the read inside ``_index_file`` leaves us
        holding the old target's sidecar while reading the new one. The sidecar
        never bound external mutation of the data file (see
        ``context._atomic``); before #2105 this path held no lock at all.

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

        async def _run() -> tuple[IndexFileResult, float]:
            start = time.monotonic()
            result = await self._index_file(
                path,
                force,
                namespace=namespace,
                on_chunk_progress=on_chunk_progress,
                force_unsafe=force_unsafe,
                already_scanned=already_scanned,
                path_scope=path_scope,
                # Engine-scoped, so the #1783 embedding cap holds across every
                # entry point now that a bulk run can overlap an ``index_file``
                # or stream call (#2105).
                embed_semaphore=self._embed_semaphore(),
                reassign_namespaces=reassign_namespaces,
                new_source_namespace=new_source_namespace,
            )
            return result, (time.monotonic() - start) * 1000

        # Engine-wide file-pipeline slot, ALWAYS acquired above L2 (#2105).
        # Ordering matters: a task waiting for a slot must hold no sidecar, or
        # slot holders blocked on that sidecar could never release. That is
        # exactly why ``lock_held`` callers are exempt — they reach here
        # already holding L2 for their whole read→rewrite→reindex span, so
        # queueing them for a slot would be the one shape that closes the
        # cycle. The ceiling is therefore ``_FILE_CONCURRENCY`` slot-managed
        # pipelines (bulk fan-out, watcher, stream, single-file, importers)
        # plus however many interactive CRUD spans are mid-reindex — a
        # bounded overcommit, and the embedding cap below stays strict for
        # all of them.
        if lock_held:
            # No sidecar to key: the caller holds it, and the missing-parent
            # test short-circuits — so this branch resolves nothing.
            return await self._locked_index(
                None, lock_held=True, engine_serialized=engine_serialized, run=_run
            )
        async with self._file_semaphore():
            # Resolving happens in ``_locked_index``, inside this slot: it is a
            # synchronous stat chain, and a bulk gather would otherwise run one
            # per discovered file on the event loop before anything bounds
            # them — on a network-backed tree, the whole walk at once.
            return await self._locked_index(
                path,
                lock_held=False,
                engine_serialized=engine_serialized,
                run=_run,
            )

    async def _locked_index(
        self,
        path: Path | None,
        *,
        lock_held: bool,
        engine_serialized: bool,
        run: Callable[[], Awaitable[tuple[IndexFileResult, float]]],
    ) -> tuple[IndexFileResult, float]:
        """L2 → L3 half of :meth:`_index_file_locked`, slot already held.

        ``path`` is the file to key the sidecar on, or ``None`` when
        ``lock_held`` — the caller owns that sidecar already. The key is built
        here rather than by the caller so the builder and the acquire stay in
        one function: ``test_context_c0_prelude_guard`` derives lock paths by
        intra-function taint, and a key handed in as a parameter makes this
        acquire invisible to it.
        """
        # In-body import on purpose: tests monkeypatch the budget by dotted
        # path (``memtomem.context._atomic._MEMORY_SIDECAR_LOCK_BUDGET_S``);
        # a module-top ``from`` import would freeze the value.
        from memtomem.context._atomic import (
            _MEMORY_SIDECAR_LOCK_BUDGET_S,
            async_file_lock,
            memory_lock_path,
        )

        # One sidecar per physical file — ``memory_lock_path`` resolves, which
        # is what makes an alias and its target contend on one lockfile
        # (#2130). The work path stays as the caller discovered it (see
        # ``_index_file_locked``); only the key is canonical. The sidecar's
        # parent IS the file's resolved parent, so it answers #1566 too.
        lock_path = None if path is None else memory_lock_path(path)

        # Skip the sidecar when the caller already holds it (lock_held) or
        # when the parent dir is gone (#1566: a delete-by-source pass for a
        # vanished file — taking the sidecar would ``mkdir`` the deleted
        # parent back into existence just to lock a delete, resurrecting the
        # directory the user removed). ``_index_lock`` still serializes
        # in-process — including for an ``engine_serialized=False`` caller,
        # which would otherwise hold no lock at all; a migrate sidecar for a
        # missing-parent path lives in that same missing parent, so no live
        # pair-op can be mid-flight.
        if lock_path is None or not lock_path.parent.is_dir():
            async with self._index_lock:
                return await run()
        async with async_file_lock(
            lock_path,
            timeout=_MEMORY_SIDECAR_LOCK_BUDGET_S,
        ):
            if not engine_serialized:
                return await run()
            async with self._index_lock:
                return await run()

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
        reassign_namespaces: bool = False,
        new_source_namespace: str | None = None,
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
        ``POST /reindex`` all use this path. It re-embeds only: a file's
        stored namespace is preserved, never re-resolved through the rules
        (#2061).

        ``new_source_namespace`` is the session-inheritance slot (#2104): a
        caller whose writes are bound to a context namespace passes it here
        instead of as ``namespace``, and it applies only to a source with no
        stored rows. An already-indexed file keeps its namespace, so a bulk
        re-index under an agent session cannot move content the session never
        wrote. Cannot be combined with ``reassign_namespaces=True``.

        ``reassign_namespaces=True`` is the opt-in that *does* re-resolve,
        overwriting stored namespaces with what the current rules say. It
        implies ``force`` — applying rules only to files that happen to have
        changed would be a silently partial migration — and cannot be
        combined with an explicit ``namespace`` (the preview helpers refuse
        the same pair, so a preview cannot describe an operation the write
        would reject).

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
        _reject_reassign_with_explicit_ns(namespace, reassign_namespaces, new_source_namespace)
        force = force or reassign_namespaces
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
        with self._active_run():
            result, duration = await self._index_file_locked(
                file_path.resolve(),
                force,
                namespace=namespace,
                force_unsafe=force_unsafe,
                already_scanned=already_scanned,
                lock_held=lock_held,
                path_scope=path_scope,
                reassign_namespaces=reassign_namespaces,
                new_source_namespace=new_source_namespace,
            )
        tally = _NamespaceTally()
        decision = result.get("namespace_decision")
        if decision is not None:
            tally.record(
                decision,
                written=result.get("namespace_written", False),
                canonical=self._canonical_namespace,
            )
        return IndexingStats(
            total_files=1,
            total_chunks=result["total"],
            indexed_chunks=result["indexed"],
            skipped_chunks=result["skipped"],
            deleted_chunks=result["deleted"],
            duration_ms=duration,
            errors=tuple(result.get("errors", ())),
            retryable_errors=tuple(result.get("retryable_errors", ())),
            new_chunk_ids=tuple(result.get("new_chunk_ids", ())),
            resolved_namespaces=(
                (result["resolved_namespace"],) if "resolved_namespace" in result else ()
            ),
            applied_namespaces=(
                (result["resolved_namespace"],) if "resolved_namespace" in result else ()
            ),
            exempted_files=1 if result.get("exempted") else 0,
            exempted_paths=(str(file_path),) if result.get("exempted") else (),
            namespaces_preserved_against_rules=tally.preserved_against_rules,
            namespaces_reassigned=tally.reassigned,
            namespace_moves=tally.summary(),
            chunks_missing_vectors=await self._count_missing_vectors(self._unchanged_ids([result])),
            mutated=result.get("mutated", False),
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
            # Held, not counted: this is a probe, not an indexing run, so it
            # stays out of ``is_active`` — but it awaits the embedder, so it
            # pins the generation like every other embedder user (#2180).
            with self._generation.hold():
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

    def _canonical_namespace(self, namespace: str | None) -> str:
        """The spelling a namespace has once stored.

        ``None`` is the untagged carve-out, which the chunk model persists as
        the configured default. Comparing a resolver answer against a stored
        value without this would read a file that never moved as a move
        (``None`` vs ``"default"`` are the same state by two names).
        """
        return namespace if namespace is not None else self._ns_config.default_namespace

    async def _namespace_decision(
        self,
        file_path: Path,
        explicit_ns: str | None = None,
        *,
        force: bool = False,
        reassign: bool = False,
        new_source_namespace: str | None = None,
    ) -> NamespaceDecision:
        """Resolve ``file_path``'s namespace and record why (issue #2061).

        | caller | stored rows | outcome |
        |---|---|---|
        | explicit namespace | any | that namespace |
        | plain or ``force`` | one, unanimous | preserved |
        | plain or ``force`` | none | ``new_source_namespace`` if given, else rules → auto_ns → default |
        | plain (no force) | several | rules — only the *changed* chunks move, which is the pre-existing ADR-0032 ambiguity |
        | ``force`` | several | refused (:class:`NamespaceMixedUnderForceError`) — force rewrites *every* row, so one namespace would swallow the rest |
        | ``reassign`` | any | rules, deliberately overwriting what is stored |

        ``new_source_namespace`` is the session-inheritance slot (#2104). A
        caller whose writes are bound to a context namespace — an MCP agent
        session, a ``mem_ns_set`` current namespace — passes it *here* rather
        than as ``explicit_ns``, and it reaches only sources with no stored
        rows. That keeps the #2004 contract for new content while stopping a
        bulk re-index from restamping files the session never wrote: an
        explicit namespace is a caller naming a target for *this* call, a
        bound one is ambient context, and only the first may move rows.

        The stored lookup runs on every path, ``reassign`` included: the old
        namespace is what the move summary and the system-namespace warning
        report, so reassigning without reading it first would move rows and
        be unable to say from where.

        Raises:
            NamespaceResolutionError: the stored lookup could not answer.
                Failing is deliberate: falling back to rule resolution on a
                transient read error performs exactly the silent namespace
                move preservation exists to prevent. Retryable — the watcher
                re-queues rather than dropping.
        """
        if explicit_ns is not None:
            return NamespaceDecision(target=explicit_ns, reason="explicit")
        try:
            stored = tuple(await self._storage.namespaces_for_source(file_path))
        except Exception as exc:
            raise NamespaceResolutionError(
                f"could not read the stored namespace for {file_path}: {exc}"
            ) from exc
        ruled, rule_directed = self._resolve_namespace_directed(file_path, None)

        if reassign:
            moved = bool(stored) and any(
                self._canonical_namespace(s) != self._canonical_namespace(ruled) for s in stored
            )
            return NamespaceDecision(
                target=ruled, stored=stored, reason="reassigned" if moved else "resolved"
            )

        if len(stored) == 1:
            # A re-index carrying no caller intent must not move a file out
            # of the namespace it is already stored under — ``force`` no
            # longer exempts itself from that (#2061): it means "re-embed
            # everything", never "re-namespace everything". Only a
            # *unanimous* stored namespace is preserved; a file holding
            # several is the ambiguity #2005 is about, and there is no
            # per-line provenance to split it by.
            #
            # Normalise the untagged carve-out back to ``None``: with the
            # configured default at "default", ``_resolve_namespace`` answers
            # ``None`` and the chunk model's own default stores the literal
            # "default" — the same state by two names. Returning the stored
            # spelling would make this resolver answer ``None`` before a file
            # is written and "default" after, so the preview and the
            # ``/api/index`` echo would disagree about a file that never moved.
            target = (
                None
                if (stored[0] == "default" and self._ns_config.default_namespace == "default")
                else stored[0]
            )
            # "Against the rules" requires a rule to have spoken. Without
            # ``rule_directed`` a stock config — no rules, no auto_ns — would
            # report every non-default file as one the rules disagree with,
            # and hand its owner a command that reassigns it (#2061).
            overruled = rule_directed and self._canonical_namespace(
                target
            ) != self._canonical_namespace(ruled)
            reason: NamespaceDecisionReason = (
                "preserved_against_rules" if overruled else "preserved"
            )
            return NamespaceDecision(target=target, stored=stored, reason=reason)

        if len(stored) > 1 and force:
            return NamespaceDecision(target=ruled, stored=stored, reason="mixed_force_refused")

        if not stored and new_source_namespace is not None:
            # The only place a bound namespace applies: a source the store has
            # never seen. Deliberately *below* the preserved and mixed-force
            # branches, so binding can neither move an indexed file nor slip
            # past the refusal a mixed file earns (#2104).
            return NamespaceDecision(target=new_source_namespace, reason="bound_new_source")

        return NamespaceDecision(target=ruled, stored=stored, reason="resolved")

    async def namespace_decision_for(
        self,
        file_path: Path,
        explicit_ns: str | None = None,
        *,
        force: bool = False,
        reassign: bool = False,
        new_source_namespace: str | None = None,
    ) -> NamespaceDecision:
        """Public form of :meth:`_namespace_decision`.

        For callers that pin the namespace explicitly on a forced re-index
        (the web chunk-delete path). Pinning is what makes the pin *bypass*
        the multi-namespace refusal — an explicit namespace is caller intent
        and always wins — so a caller that pins has to ask for the reason, not
        just the value, or it silently reintroduces the collapse the refusal
        exists to prevent (#2061).
        """
        _reject_reassign_with_explicit_ns(explicit_ns, reassign, new_source_namespace)
        return await self._namespace_decision(
            file_path,
            explicit_ns,
            force=force,
            reassign=reassign,
            new_source_namespace=new_source_namespace,
        )

    async def effective_namespace_for(
        self,
        file_path: Path,
        explicit_ns: str | None = None,
        *,
        force: bool = False,
        reassign: bool = False,
        new_source_namespace: str | None = None,
    ) -> str | None:
        """The namespace ``index_file`` will stamp on ``file_path``'s chunks.

        The single answer to that question (issue #2005). Three callers need
        it and they must not disagree: ``_index_file`` itself, the add
        surfaces' mixed-namespace guard — which would otherwise refuse
        writes the indexer would have handled safely — and the
        ``resolved_namespaces`` echo, which would otherwise report a
        namespace the write did not use.

        ``None`` is the untagged carve-out (see ``_resolve_namespace``), not
        "unknown". See :meth:`_namespace_decision` for the full table and for
        the structured form the reporting surfaces read. This method answers
        the value only — including for the ``force`` + multi-namespace file
        the write refuses, so previews stay non-raising.

        Raises:
            NamespaceResolutionError: the stored lookup could not answer
                (retryable).
        """
        _reject_reassign_with_explicit_ns(explicit_ns, reassign, new_source_namespace)
        decision = await self._namespace_decision(
            file_path,
            explicit_ns,
            force=force,
            reassign=reassign,
            new_source_namespace=new_source_namespace,
        )
        return decision.target

    def _resolve_namespace(self, file_path: Path, explicit_ns: str | None) -> str | None:
        """Determine the namespace for a file.

        Priority: explicit parameter > policy rules (first valid match) >
        auto_ns (folder-based) > default_namespace. Returns None only if
        default_namespace is "default" and nothing else matched (preserves
        backward compat — chunks without namespace stay untagged).
        """
        return self._resolve_namespace_directed(file_path, explicit_ns)[0]

    def _resolve_namespace_directed(
        self, file_path: Path, explicit_ns: str | None
    ) -> tuple[str | None, bool]:
        """:meth:`_resolve_namespace`, plus whether anything *chose* the answer.

        The flag is false when resolution fell through to the configured
        default because no rule matched and no folder derivation applied.
        That distinction is what keeps the "#2061" preservation advisory
        honest: a file sitting in a namespace the config never mentions has
        not had a rule overruled by preservation, so telling its owner that
        "current rules would assign differently" would be false — and, for an
        ``agent-runtime:`` file under stock config, it would be advice to run
        the very reassignment that #2061 was filed about.
        """
        if explicit_ns is not None:
            return explicit_ns, True

        if self._ns_rule_specs:
            candidate = file_path.as_posix().lower().lstrip("/")
            for i, (spec, rule) in enumerate(self._ns_rule_specs):
                if not spec.match_file(candidate):
                    continue
                ns = self._format_namespace(rule.namespace, file_path, rule_index=i)
                if ns is not None:
                    return ns, True

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
                    return name, True

        default = self._ns_config.default_namespace
        if default and default != "default":
            return default, False

        return None, False

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
        return {
            "total": 0,
            "indexed": 0,
            "skipped": 0,
            "deleted": deleted,
            "errors": [],
            "mutated": deleted > 0,
        }

    async def _extract_entities_for(self, chunks: Sequence[Chunk]) -> int:
        """Rewrite ``chunk_entities`` for chunks whose content was just written.

        Thin delegate to :func:`~memtomem.tools.entity_sync.sync_entities_for_chunks`,
        which carries the contract and the reasoning. Kept as a method because the
        engine is where the config knob lives and because ``_index_file`` calls it
        from inside the chunk-write transaction, so entities and their chunk commit
        together or not at all.

        Only ``diff_result.to_upsert`` belongs here. The ``unchanged`` and
        ``metadata_only`` buckets keep their stored content, so their entities are
        still accurate — and, since ``mutated`` is already true whenever
        ``to_upsert`` is non-empty, confining extraction to that bucket means the
        callers' existing cache-invalidation contract still covers every entity
        write.
        """
        return await sync_entities_for_chunks(
            self._storage, chunks, enabled=self._config.extract_entities
        )

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
        embed_semaphore: asyncio.Semaphore | None = None,
        reassign_namespaces: bool = False,
        new_source_namespace: str | None = None,
    ) -> IndexFileResult:
        # Return shape: total/indexed/skipped/deleted (ints), errors (list[str]),
        # new_chunk_ids (list[UUID]), and resolved_namespace (str | None) when
        # at least one chunk was successfully upserted. Early/no-write paths
        # may omit the optional keys — consumers must tolerate their absence.

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

        mismatch = getattr(self._storage, "embedding_mismatch", None)
        if isinstance(mismatch, dict):
            return {
                "total": 0,
                "indexed": 0,
                "skipped": 0,
                "deleted": 0,
                "errors": [
                    "Embedding configuration differs from stored vectors; indexing is "
                    "disabled. Run 'mm embedding-reset --mode apply-current', then "
                    "'mm index --force <path>' (or mem_index(force=true))."
                ],
            }

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

        # Adjudicate on the *canonical* path (#2076). ``classify_scope``
        # pattern-matches the path string and the two bulk paths disagree on
        # what they hand us — non-stream passes the discovered leaf as found
        # (only the root was resolved), stream passes ``fp.resolve()``. That
        # divergence was inert while every bypass needed an explicit
        # ``force_unsafe``; a file-declared exemption makes the scope decision
        # load-bearing on its own, so a user-dir symlink pointing at a
        # ``project_shared`` note must not be adjudicated as ``user``, and an
        # ``.md`` alias to a non-Markdown target must not claim a Markdown
        # frontmatter declaration. Storage identity is deliberately NOT
        # changed: chunks keep keying on ``file_path`` as before.
        try:
            decision_path = file_path.resolve()
        except OSError:  # pragma: no cover - defensive: unreadable parent
            decision_path = file_path

        if self._registry.get(decision_path.suffix) is None:
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
        #
        # A file may also declare its own exemption in frontmatter (#2076,
        # ADR-0006 Axis E.5). Unlike ``force_unsafe`` — which no surface can
        # pass to the watcher, the debounce drain, or ``mem_index`` — the
        # declaration travels with the content the gate already reads, so it
        # reaches every un-adjudicated surface at once. The guard still
        # decides: it honours the declaration only for label-shaped hits, and
        # hard-refuses it for ``project_shared`` exactly as it does the valve.
        scope_val, project_root = self._resolve_scope(decision_path)
        exempted = False
        if not already_scanned:
            declared = declared_exemption(decision_path, content)
            guard = privacy.enforce_write_guard(
                content,
                surface="index",
                force_unsafe=force_unsafe,
                scope=scope_val,
                declared_exemption=declared,
                audit_context={"path": str(file_path)},
            )
            exempted = guard.decision == "exempted"
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
            if guard.decision not in ("pass", "bypassed", "exempted"):
                raise RuntimeError(f"unexpected enforce_write_guard decision: {guard.decision!r}")

        new_chunks = self.chunk_content(file_path, content)

        # Resolve namespace: explicit > preserved > bound-new-source > rules >
        # auto_ns > default.
        # ``force`` no longer skips preservation (#2061): it re-embeds, and
        # only ``reassign_namespaces`` re-resolves through the rules. This
        # lookup runs inside the per-file critical section on purpose: a bulk
        # run's pre-write prepass (issue #2018) answers for the run's *start*,
        # and a concurrent writer may have moved the file since — deciding the
        # stamp from a pre-lock answer would silently undo that write. A
        # failure here fails this file closed; the bulk flatten branches
        # keep the retryable type in ``stats.retryable_errors``.
        ns_decision = await self._namespace_decision(
            file_path,
            namespace,
            force=force,
            reassign=reassign_namespaces,
            new_source_namespace=new_source_namespace,
        )
        resolved_ns = ns_decision.target
        if resolved_ns is not None and ns_decision.reason != "mixed_force_refused":
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
            # File exists but is empty / unparseable — delete stale chunks.
            # Ahead of the mixed-namespace refusal below on purpose: an
            # emptied source writes no namespace, so there is nothing to
            # collapse, and refusing here would strand its rows as
            # permanently searchable content for a file with no content.
            deleted = await self._storage.delete_by_source(file_path)
            return {
                "total": 0,
                "indexed": 0,
                "skipped": 0,
                "deleted": deleted,
                "errors": [],
                "mutated": deleted > 0,
            }

        if ns_decision.reason == "mixed_force_refused":
            # Reached only when a namespace-bearing upsert would follow. The
            # absolute path goes to the log; the raised message carries the
            # bare name because bulk error strings are echoed verbatim through
            # the web complete event, which redacts host paths.
            logger.error(
                "Refusing forced re-index of %s: chunks span namespaces %s",
                file_path,
                ", ".join(sorted(ns_decision.stored)),
            )
            # No filename here: both bulk paths prefix the basename when they
            # flatten a per-file exception, and prefixing in two layers renders
            # ``mixed.md: mixed.md: …``. The single-file callers surface the
            # exception type itself, which names the file in the log above.
            raise NamespaceMixedUnderForceError(
                f"chunks span several namespaces "
                f"({', '.join(sorted(ns_decision.stored))}) and a forced re-index "
                "rewrites every one of them, so all rows would collapse into one "
                "namespace. Re-run without --force, pass an explicit namespace to "
                "choose the target, use --reassign-namespaces to apply the current "
                "rules on purpose, or split the file per namespace."
            )

        # Always run hash-aware diff: ``compute_diff`` reuses existing chunk
        # IDs for hash-matched chunks (see ``differ.py:compute_diff``). For
        # ``force=True`` we then promote the matched ``unchanged`` chunks
        # into ``to_upsert`` so they get re-embedded — but their IDs are
        # preserved by the diff, and ``upsert_chunks`` UPDATE clause does not
        # touch ``access_count`` / ``use_count`` / ``last_accessed_at`` /
        # ``importance_score`` (sqlite_backend.py UPDATE column list). Net
        # effect: force re-indexes content but keeps per-chunk personalization
        # and chunk identity. See ``docs/adr/0005-force-reindex-metadata-contract.md``.
        existing_state = await self._storage.get_chunk_index_state(file_path)
        diff_result = compute_diff(existing_state, new_chunks)
        chunk_positions = {id(chunk): index + 1 for index, chunk in enumerate(new_chunks)}
        # ``new_chunk_ids`` in the return shape is documented as "freshly
        # created chunks" — callers like ``mem_consolidate_apply`` rely on
        # this distinction. Capture before any force-promotion so the
        # field stays accurate even when force re-embeds unchanged chunks.
        truly_new_chunk_ids = [c.id for c in diff_result.to_upsert]

        # Ids of the chunks already in the store that this file matched by
        # content hash (#2115). Captured before force promotion empties the
        # list, and before the early returns below, because both paths can
        # leave those chunks vectorless:
        #
        # * a plain run skips them, and after an embedding reset they have no
        #   vector to skip back to;
        # * a *forced* run promotes them into the upsert set, so a repair whose
        #   embedding then fails leaves them exactly as broken as it found
        #   them — with no ``unchanged`` list left to read at the failure.
        #
        # Over-capturing is safe: the count is taken from the store after the
        # run, so a force that succeeds reports zero because the vectors are
        # back. Ids of chunks whose embedding failed are absent by
        # construction — they were never written, and counting them would
        # demand a re-embed that cannot help.
        #
        # Blocked files never reach here: the redaction gate raises above,
        # before the diff, so a refused file is not handed ``--force`` advice.
        # ``metadata_only`` chunks are skipped by the embedder exactly like
        # ``unchanged`` ones — they keep the vector they already had — so they
        # belong in this capture too.
        unchanged_ids = (
            [str(c.id) for c in diff_result.unchanged + diff_result.metadata_only]
            if self._embedder.dimension > 0
            else []
        )

        if force and (diff_result.unchanged or diff_result.metadata_only):
            # A forced run re-embeds every matched chunk, and ``upsert_chunks``
            # writes the fresh metadata along with the content, so the
            # metadata-only bucket has nothing left to do on this path.
            diff_result = DiffResult(
                to_upsert=(
                    diff_result.to_upsert + diff_result.unchanged + diff_result.metadata_only
                ),
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
                    "namespace_decision": ns_decision,
                    "namespace_written": False,
                    "unchanged_chunk_ids": unchanged_ids,
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
                # Only the embed call queues on the embedding semaphore;
                # everything around it stays governed by the file-level
                # semaphore alone. ``_index_file_locked`` passes the
                # engine-scoped one for every entry point (#2105); ``None``
                # is left for direct calls in tests.
                async with (
                    embed_semaphore if embed_semaphore is not None else contextlib.nullcontext()
                ):
                    progress_cb = on_chunk_progress if emit_progress else None
                    if _supports_input_context(self._embedder):
                        # Optional duck-typed capability: only providers that
                        # explicitly opt in receive path/index metadata. Strict
                        # ``is True`` avoids Mock auto-attributes and keeps old
                        # third-party embedder signatures untouched.
                        contextual_embed = cast(Any, self._embedder.embed_texts)
                        embeddings = await contextual_embed(
                            texts,
                            on_progress=progress_cb,
                            source_path=str(file_path),
                            chunk_indices=[
                                chunk_positions[id(chunk)] for chunk in diff_result.to_upsert
                            ],
                        )
                    else:
                        embeddings = await self._embedder.embed_texts(
                            texts,
                            on_progress=progress_cb,
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
                message = f"Embedding failed: {exc}"
                return {
                    "total": len(new_chunks),
                    "indexed": 0,
                    "skipped": len(new_chunks),
                    "deleted": 0,
                    "errors": [message],
                    "retryable_errors": [message] if isinstance(exc, RetryableError) else [],
                    # Resolution already ran, so the decision holds even though
                    # nothing was written. ``namespace_written`` stays false —
                    # no move happened — but "this file kept a namespace the
                    # rules disagree with" is still true, and dropping it here
                    # would under-report the advisory on a partial run.
                    "namespace_decision": ns_decision,
                    "namespace_written": False,
                    "unchanged_chunk_ids": unchanged_ids,
                }

        # Now safe to mutate DB — embedding succeeded.
        # Wrap delete+upsert in a single transaction for atomicity.
        async with self._storage.transaction():
            if diff_result.to_delete:
                await self._storage.delete_chunks(diff_result.to_delete)

            # Both buckets keep their stored vector, and a sibling edit can have
            # shifted either one's lines, so both need the cheap range refresh.
            hash_matched = diff_result.unchanged + diff_result.metadata_only
            ranges_changed = 0
            if hash_matched:
                ranges_changed = await self._storage.update_chunk_line_ranges(hash_matched)

            metadata_changed = 0
            if diff_result.metadata_only:
                # Content identical, retrieval metadata moved: rewrite the
                # metadata columns alone — tags (#2124) and the validity window
                # (#2140) — rather than re-embedding the chunk.
                metadata_changed = await self._storage.update_chunk_metadata(
                    diff_result.metadata_only
                )

            if diff_result.to_upsert:
                await self._storage.upsert_chunks(diff_result.to_upsert)
                # Entities are rewritten with the chunk, inside the same
                # transaction (#2145). Before this, ``chunk_entities`` was
                # written only by ``mem_entity_scan``, so it was empty on a
                # default install and decayed after a scan: an in-place chunk
                # UPDATE leaves the old rows describing content that is gone,
                # and a delete cascades them away with nothing to restore them.
                # Extraction here is the regex path only — stdlib ``re``, no
                # model, no I/O — so it is free next to the embedding call this
                # same write already paid for.
                await self._extract_entities_for(diff_result.to_upsert)

        # Both metadata mutators return the count of rows they actually
        # changed, so a run whose diff bucketed rows as metadata-only but
        # found every column already current still reports ``mutated=False``
        # (#2141). ``to_delete``/``to_upsert`` are non-empty only when there
        # is real work, so their length is signal enough.
        mutated = bool(
            diff_result.to_delete or diff_result.to_upsert or ranges_changed or metadata_changed
        )

        # The namespace write is committed as of here. Capture the decision
        # now, before the auxiliary work below, so a failure in something that
        # is not the write cannot erase a move that actually happened.
        result: IndexFileResult = {
            "total": len(new_chunks),
            "indexed": len(diff_result.to_upsert),
            # Metadata-only rows are reported as skipped: nothing was embedded,
            # and the counters are a documented parse target (``mm index``'s
            # summary line, the ``mem_index`` block, ``IndexResponse``), so the
            # cheap metadata write stays as silent as its sibling
            # ``update_chunk_line_ranges``.
            "skipped": len(diff_result.unchanged) + len(diff_result.metadata_only),
            "deleted": len(diff_result.to_delete),
            "errors": [],
            "mutated": mutated,
            "new_chunk_ids": truly_new_chunk_ids,
            "namespace_decision": ns_decision,
            "namespace_written": bool(diff_result.to_upsert),
        }
        if unchanged_ids:
            result["unchanged_chunk_ids"] = unchanged_ids
        if diff_result.to_upsert:
            result["resolved_namespace"] = resolved_ns
        if exempted:
            result["exempted"] = 1

        # Per-source AI summary refresh — runs *after* the transaction so a
        # slow LLM call never holds the chunk write lock. The signature
        # check inside ``maybe_update_ai_summary`` skips files whose chunk
        # set didn't change, so steady-state reindex pays nothing.
        # ``new_chunks`` is the full current chunk set for the file (not
        # just ``diff_result.to_upsert``); the signature must hash all
        # current chunks to remain stable when only some changed.
        #
        # Fail-soft: the chunks are already durable, so letting a summary
        # failure propagate would report a committed write — including a
        # committed namespace move — as a failed file. Record it as a
        # per-file error instead and keep the write's outcome.
        from memtomem.indexing.summarizer import maybe_update_ai_summary

        try:
            await maybe_update_ai_summary(
                cast("SqliteBackend", self._storage), self._llm, file_path, new_chunks, self._config
            )
        except Exception as exc:
            logger.error("AI summary refresh failed for %s: %s", file_path, exc)
            message = f"AI summary refresh failed: {exc}"
            result["errors"] = [message]
            if isinstance(exc, RetryableError):
                result["retryable_errors"] = [message]
        return result

    async def index_path_stream(
        self,
        path: Path,
        recursive: bool = True,
        force: bool = False,
        namespace: str | None = None,
        *,
        force_unsafe: bool = False,
        path_scope: PathScope = "configured",
        reassign_namespaces: bool = False,
        new_source_namespace: str | None = None,
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
          errors, retryable_errors, resolved_namespaces, applied_namespaces,
          exempted_files, exempted_paths``.
          ``errors`` is a list of human-readable strings in the same loose
          shape as ``IndexingStats.errors`` so non-stream UI handlers reuse
          verbatim. ``retryable_errors`` is its same-string retryable subset.
          ``applied_namespaces`` is the authoritative subset of the hybrid
          ``resolved_namespaces`` echo. Error lists are empty when the run had
          no errors. Also carries the namespace advisory counters
          ``namespaces_preserved_against_rules``, ``namespaces_reassigned``,
          and ``namespace_moves`` (#2061) — same values the non-stream
          ``IndexingStats`` reports, so the two surfaces cannot diverge.

        Locking: each file is indexed under the same L2 sidecar →
        L3 ``_index_lock`` pair as ``index_file`` (via
        ``_index_file_locked``), taken **per file** so a stream run
        serializes against watcher/CLI/CRUD reindexes of the same file
        without holding a lock across the whole tree walk (#1574 item 6).
        The ``_active_runs`` counter is still bumped once per stream run
        so ``GET /api/indexing/active`` covers discovery and the gaps
        between files, where no lock is held.
        """
        _reject_reassign_with_explicit_ns(namespace, reassign_namespaces, new_source_namespace)
        force = force or reassign_namespaces
        with self._active_run():
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
                    # Nothing was walked, so nothing was written. Stated
                    # rather than omitted: the key is part of the event shape
                    # now, and a consumer should not have to know which
                    # completion path it is reading (#2141).
                    "mutated": False,
                    "errors": [f"path is outside configured memory directories: {path}"],
                    "retryable_errors": [],
                    "resolved_namespaces": [],
                    "applied_namespaces": [],
                    "blocked_files": 0,
                    "blocked_paths": [],
                    "blocked_project_shared_files": 0,
                    "exempted_files": 0,
                    "exempted_paths": [],
                    "namespaces_preserved_against_rules": 0,
                    "namespaces_reassigned": 0,
                    "namespace_moves": [],
                    "chunks_missing_vectors": 0,
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
                    # Nothing was walked, so nothing was written. Stated
                    # rather than omitted: the key is part of the event shape
                    # now, and a consumer should not have to know which
                    # completion path it is reading (#2141).
                    "mutated": False,
                    "errors": [f"index path does not exist: {path}"],
                    "retryable_errors": [],
                    "resolved_namespaces": [],
                    "applied_namespaces": [],
                    "blocked_files": 0,
                    "blocked_paths": [],
                    "blocked_project_shared_files": 0,
                    "exempted_files": 0,
                    "exempted_paths": [],
                    "namespaces_preserved_against_rules": 0,
                    "namespaces_reassigned": 0,
                    "namespace_moves": [],
                    "chunks_missing_vectors": 0,
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
            # Pre-compute per-file namespaces so the complete event surfaces
            # what was applied — single render across both stream and
            # non-stream paths (see ``_index_path_inner``). Resolved on the
            # same ``fp.resolve()`` form the write below uses, so the
            # preservation lookup and the write answer for the same path.
            # A store that cannot answer fails the run here, before any
            # write (issue #2018); the per-file runner still resolves inside
            # its own lock, and a failure there fails that file closed with
            # its type kept in ``retryable_errors``.
            resolved_files = [fp.resolve() for fp in files]
            prepass_namespaces = await self._resolve_namespaces_per_file(
                resolved_files,
                namespace,
                force=force,
                reassign=reassign_namespaces,
                new_source_namespace=new_source_namespace,
            )
            echo_namespaces = list(prepass_namespaces)
            tally = _NamespaceTally()
            # Ids of chunks left untouched by a hash match, for the one
            # post-run missing-vector count (#2115). Carried as ids rather
            # than a counter because ``skipped`` also covers files whose
            # embedding failed, which wrote nothing at all.
            unchanged_ids: list[str] = []
            agg = {
                "total_chunks": 0,
                "indexed": 0,
                "skipped": 0,
                "deleted": 0,
                "blocked": 0,
                "exempted": 0,
                "blocked_project_shared": 0,
            }
            any_mutated = False
            all_errors: list[str] = []
            retryable_errors: list[str] = []
            blocked_paths: list[str] = []
            exempted_paths: list[str] = []
            applied_namespaces: list[str | None] = []

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
                            reassign_namespaces=reassign_namespaces,
                            new_source_namespace=new_source_namespace,
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
                        # error shape regardless of stream vs non-stream —
                        # including the retryable marker (issue #2018).
                        result = {
                            "total": 0,
                            "indexed": 0,
                            "skipped": 0,
                            "deleted": 0,
                            "errors": [f"{fp.name}: {exc}"],
                        }
                        # ``TimeoutError`` included for the same reason as the
                        # non-stream branch: a sidecar budget overrun is
                        # transient, and the two branches promise one error
                        # shape (#2105).
                        if isinstance(exc, RetryableError | TimeoutError):
                            retryable_errors.append(f"{fp.name}: {exc}")
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
                any_mutated = any_mutated or result.get("mutated", False)
                if result.get("exempted"):
                    agg["exempted"] += 1
                    exempted_paths.append(str(fp))
                unchanged_ids.extend(result.get("unchanged_chunk_ids", ()))
                all_errors.extend(result.get("errors", []))
                retryable_errors.extend(result.get("retryable_errors", []))
                stream_decision = result.get("namespace_decision")
                if stream_decision is not None:
                    tally.record(
                        stream_decision,
                        written=result.get("namespace_written", False),
                        canonical=self._canonical_namespace,
                    )
                if "resolved_namespace" in result:
                    # ``None`` is a real applied value, so key presence — not
                    # truthiness — decides whether to replace the prepass.
                    echo_namespaces[i - 1] = result["resolved_namespace"]
                    applied_namespaces.append(result["resolved_namespace"])
                yield {
                    "type": "progress",
                    "file": str(fp),
                    "files_done": i,
                    "files_total": total_files,
                    "indexed": result["indexed"],
                    "skipped": result["skipped"],
                    # Per-file, not just on ``complete``: a client that
                    # disconnects mid-stream still needs to learn that files
                    # already committed (#2141).
                    "mutated": result.get("mutated", False),
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
                "errors": list(dict.fromkeys(all_errors)),
                "retryable_errors": list(dict.fromkeys(retryable_errors)),
                "resolved_namespaces": _distinct_sorted(echo_namespaces),
                "applied_namespaces": _distinct_sorted(applied_namespaces),
                "blocked_files": agg["blocked"],
                "blocked_paths": blocked_paths,
                "exempted_files": agg["exempted"],
                "exempted_paths": exempted_paths,
                "blocked_project_shared_files": agg["blocked_project_shared"],
                "mutated": any_mutated,
                "namespaces_preserved_against_rules": tally.preserved_against_rules,
                "namespaces_reassigned": tally.reassigned,
                "namespace_moves": list(tally.summary()),
                "chunks_missing_vectors": await self._count_missing_vectors(unchanged_ids),
            }

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

    def preview_redaction_decision(self, file_path: Path, content: str) -> str:
        """What would the redaction gate decide for ``content`` at ``file_path``?

        The read-only twin of the gate in ``_index_file``: same canonical-path
        rule, same scope resolution, same frontmatter-exemption lookup, same
        :func:`privacy.enforce_write_guard`. Returns one of ``"pass"`` /
        ``"blocked"`` / ``"blocked_project_shared"`` / ``"exempted"``.

        ``mm memory doctor`` needs this to route a stale file to the finding
        whose remediation can actually work, and asked the question itself
        with a bare ``privacy.scan`` before #2076 — which is a *second*
        implementation of "is this blocked" that silently disagrees the moment
        the guard grows a branch (an exempt file read as blocked, sent to a
        remedy it does not need). Consuming the engine's own judgement is what
        keeps the two from drifting.

        Records nothing: ``record_outcome=False`` keeps a diagnostic run out of
        the audit counters and emits no bypass/exemption log line. A read-only
        command must not look like a write in the audit trail.
        """
        try:
            decision_path = file_path.resolve()
        except OSError:  # pragma: no cover - defensive: unreadable parent
            decision_path = file_path
        scope_val, _ = self._resolve_scope(decision_path)
        return privacy.enforce_write_guard(
            content,
            surface="memory_doctor",
            scope=scope_val,
            declared_exemption=declared_exemption(decision_path, content),
            record_outcome=False,
        ).decision

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
