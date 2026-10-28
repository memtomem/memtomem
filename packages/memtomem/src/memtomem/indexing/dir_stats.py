"""Directory ownership, worktree detection, and per-root index status stats."""

from __future__ import annotations

import asyncio
import os
import stat as stat_module
from collections.abc import Callable, Collection, Iterable, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memtomem.config import (
    categorize_memory_dir,
    memory_dir_kind,
    provider_for_category,
)
from memtomem.storage.sqlite_helpers import norm_dir_prefix as _norm_dir_prefix

if TYPE_CHECKING:
    from memtomem.storage.base import StorageBackend

# Re-exported (not redefined) so the four call sites that import it from this
# module keep working while ``config`` can validate read-only roots with the
# same predicate without importing the engine. See the helper's own docstring.
norm_dir_prefix = _norm_dir_prefix

_GITDIR_PREFIX = "gitdir:"

_GIT_MARKER_LIMIT = 4096


def _read_git_marker(path: Path) -> str | None:
    """The text of a one-line git metadata file, or ``None`` for anything else.

    One guarded descriptor answers every question this module has about such a
    file — is it there, may we read it, is it really a file, how big is it —
    because asking them separately is what kept going wrong. Three review
    rounds each found a different way for a two-probe version (``Path.is_file``
    followed by ``open``) to raise or hang, and each was patched with one more
    ``except`` clause:

    - an embedded NUL raised ``ValueError`` out of ``os.stat``;
    - ``Path.is_file`` ignores only ``ENOENT``, ``ENOTDIR``, ``EBADF`` and
      ``ELOOP``, so an unreadable parent directory raised ``PermissionError``
      (measured — the comment that once called this guard unreachable was
      wrong);
    - a backlink replaced by a FIFO blocked the read forever, with no writer.

    ``O_NONBLOCK`` answers the third, ``fstat`` + ``S_ISREG`` rejects fifos,
    directories and device files by kind rather than by symptom, and the two
    questions are asked of the *same descriptor*, so there is no window for the
    entry to change between them. ``O_BINARY`` matters on Windows, where
    ``os.open`` would otherwise translate line endings; both flags are fetched
    with ``getattr`` because neither exists on every platform.

    The read is bounded at :data:`_GIT_MARKER_LIMIT` and anything longer is
    rejected rather than truncated: a truncated target is a different path, and
    a 40 MB file at a plausible name cost 40 MB of resident memory on every
    discovery walk, status count and audit (measured).
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        return None
    try:
        if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
            return None
        raw = os.read(descriptor, _GIT_MARKER_LIMIT + 1)
    except OSError:
        return None
    finally:
        os.close(descriptor)
    if len(raw) > _GIT_MARKER_LIMIT:
        return None
    return raw.decode("utf-8", errors="replace")


def _gitdir_target(directory: Path) -> Path | None:
    """The git directory a ``.git`` *file* in ``directory`` points at.

    ``None`` when there is no ``.git`` file (an ordinary checkout has a
    ``.git`` directory), when it cannot be read, or when it does not carry the
    ``gitdir:`` line. A relative target resolves against ``directory``, which
    is what ``git worktree add --relative-paths`` and submodules both write.

    The target is handed to ``Path`` verbatim. Rewriting ``\\`` to ``/`` first
    looks like Windows tolerance but is wrong on POSIX, where a backslash is an
    ordinary filename character: a repository in a directory named ``back\\slash``
    had its worktree go undetected (measured). ``Path`` on Windows is
    ``WindowsPath``, which already accepts either separator.
    """
    # No separate "is it a file?" probe: ``_read_git_marker`` answers that with
    # the same descriptor it reads from, so an ordinary checkout — whose
    # ``.git`` is a *directory* — comes back as ``None`` here, and nothing can
    # change the entry between the two questions.
    head = _read_git_marker(directory / ".git")
    if head is None:
        return None
    line = next((ln for ln in head.splitlines() if ln.strip()), "")
    if not line.strip().lower().startswith(_GITDIR_PREFIX):
        return None
    target = line.strip()[len(_GITDIR_PREFIX) :].strip()
    if not target:
        return None
    admin = Path(target)
    return admin if admin.is_absolute() else directory / admin


def _is_linked_worktree(directory: Path) -> bool:
    """True when ``directory`` is a linked git worktree, not a submodule.

    Decided by what git puts in the administration directory rather than by
    matching directory names, because the name is not stable. Measured with
    git 2.54, the ``gitdir:`` targets are::

        plain checkout        <repo>/.git/worktrees/<name>
        worktree of worktree  <repo>/.git/worktrees/<name>   (the common dir)
        bare repository       <repo>.git/worktrees/<name>    (no ``.git`` segment)
        --relative-paths      ../../.git/worktrees/<name>
        submodule             ../../.git/modules/<path>

    A ``.git``-then-``worktrees`` segment test misses the bare-repository
    layout, and a bare repository need not end in ``.git`` at all. What does
    hold across all three worktree layouts is that the administration
    directory carries a ``gitdir`` backlink naming the checkout it belongs to,
    and that a submodule's carries none. Requiring the backlink to resolve to
    *this* ``.git`` file is the whole test: it identifies worktrees across the
    layouts above, excludes submodules, and stops an unrelated directory from
    borrowing another worktree's administration directory to suppress itself.

    A ``commondir`` check was tried alongside this and removed: every
    administration directory that has the backlink also has ``commondir``, so
    it discriminated nothing — dropping it left the submodule case passing for
    the same reason it passes now.

    Fails closed on anything unexpected — unreadable, malformed, missing —
    which means "not a worktree", i.e. today's behaviour of indexing it.
    """
    admin = _gitdir_target(directory)
    if admin is None:
        return False
    backlink = _read_git_marker(admin / "gitdir")
    if backlink is None:
        return False
    link = next((ln.strip() for ln in backlink.splitlines() if ln.strip()), "")
    if not link:
        return False
    pointed = Path(link)
    if not pointed.is_absolute():
        pointed = admin / pointed
    try:
        return pointed.resolve() == (directory / ".git").resolve()
    except (OSError, ValueError, RuntimeError):
        # ``RuntimeError``: a symlink loop on the way to the target (3.12).
        # ``ValueError``: an embedded NUL the filesystem never sees.
        return False


def _dangling_worktree_shape(directory: Path) -> bool:
    """True when ``directory``'s ``gitdir:`` target is absent and shaped like a worktree's.

    A worktree created in another filesystem namespace — a container or remote
    sandbox that mounted the repository elsewhere, or a checkout moved without
    ``git worktree repair`` — points at an administration directory that does
    not exist here, so :func:`_is_linked_worktree` has no backlink to read
    (#2487). Its contents are still a copy of a checkout.

    "Absent" is only ``FileNotFoundError`` / ``NotADirectoryError`` from
    ``lstat``. A permission error, an embedded NUL, a symlink loop or any other
    failure is not evidence of absence, so it keeps the fail-closed answer. The
    shape is ``…/.git/worktrees/<name>``: the grandparent must be ``.git``, which
    keeps a submodule whose path starts with ``worktrees`` —
    ``.git/modules/worktrees/<x>`` — indexed. A bare repository's worktree whose
    administration directory is gone is not recognised; that fails closed too.
    """
    admin = _gitdir_target(directory)
    if admin is None:
        return False
    try:
        os.lstat(admin)
    except (FileNotFoundError, NotADirectoryError):
        return admin.parent.name == "worktrees" and admin.parent.parent.name == ".git"
    except (OSError, ValueError):
        return False
    return False


def _is_worktree_checkout(directory: Path) -> bool:
    """The one worktree test every bound uses: a verified backlink or a dangling worktree shape."""
    return _is_linked_worktree(directory) or _dangling_worktree_shape(directory)


def _is_main_checkout(directory: Path) -> bool:
    """True when ``directory/.git`` is a directory — a repository's own checkout.

    A linked worktree's ``.git`` is a file, so it never answers True here.
    Follows symlinks, as git does. Any failure answers False.
    """
    try:
        return stat_module.S_ISDIR(os.stat(directory / ".git").st_mode)
    except (OSError, ValueError):
        return False


#: Memo for one walk, purge or audit. Keys are ``(fact, directory)`` and hold
#: only facts about a directory that do not depend on which bound asked, so one
#: memo is safe across every bound :func:`_under_nested_worktree` uses.
WorktreeMemo = dict[tuple[str, Path], bool]


def _memo_fact(
    cache: WorktreeMemo | None, fact: str, directory: Path, probe: Callable[[Path], bool]
) -> bool:
    if cache is None:
        return probe(directory)
    key = (fact, directory)
    known = cache.get(key)
    if known is None:
        known = probe(directory)
        cache[key] = known
    return known


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


def _under_nested_worktree(
    file_path: Path,
    memory_dirs: Iterable[str | Path],
    cache: WorktreeMemo | None = None,
    *,
    walk_root: Path | None = None,
) -> bool:
    """True when a git worktree sits between ``file_path`` and its bound.

    A git worktree nested inside something indexed is a near-copy of a checkout,
    so every source file would be stored twice (#2474). The bound is, in order:

    1. **The owning configured root** — ``resolve_owning_memory_dir``, longest
       prefix. Registering a worktree as its own memory dir makes it the owning
       root, and a bound is never tested itself, so it is indexed again. This is
       the durable override.
    2. **The walk root** — the directory an explicit walk (``mm index <dir>``,
       ``mem_index``, the web index routes) started from, when the file is under
       it and no configured root owns it. Walking an unowned worktree itself
       indexes it for that run.
    3. **The enclosing repository** — otherwise: a single file, the debounce
       drain a hook feeds, budget migration, purge, the CRUD guard. The bound is
       the nearest ancestor whose ``.git`` is a directory; with none, nothing is
       excluded (#2486).

    Every bound uses the same worktree test, :func:`_is_worktree_checkout`, and
    none checks which repository a worktree belongs to — proving membership
    either guessed from names or left a dangling worktree indexed through a
    hook edit and unclaimed by purge. So a worktree of an unrelated repository
    placed inside a checkout is skipped too, as it always was under a root.

    Paths are resolved before any ancestry is computed, so a symlinked ancestor
    gives the same answer whichever caller asks. A file or bound that cannot be
    resolved excludes nothing from this layer; the ownership lookup itself
    raises on a path it cannot resolve, as the other exclusion layers in
    :func:`_path_is_excluded` do, so that failure never reads as "not excluded".

    ``cache`` memoises directory facts for one walk, purge or audit (see
    :data:`WorktreeMemo`). It is deliberately not process-wide: a long-running
    server would otherwise never notice a worktree added after startup, and
    would keep skipping one that was removed.
    """
    owning = resolve_owning_memory_dir(file_path, memory_dirs)
    try:
        target = Path(file_path).expanduser().resolve()
        if owning is not None:
            bound: Path | None = Path(owning).expanduser().resolve()
        elif walk_root is not None:
            bound = Path(walk_root).expanduser().resolve()
            if not target.is_relative_to(bound):
                bound = None
        else:
            bound = None
        if bound is not None:
            # ``ValueError`` when resolving symlinks moved the file out from
            # under the root it was attributed to lexically.
            rel = target.relative_to(bound)
    except (OSError, ValueError, RuntimeError):
        return False

    if bound is not None:
        between = [bound.joinpath(*rel.parts[:depth]) for depth in range(1, len(rel.parts))]
    else:
        between = []
        for parent in target.parents:
            if _memo_fact(cache, "checkout", parent, _is_main_checkout):
                break
            between.append(parent)
        else:
            return False
        between.reverse()
    return any(_memo_fact(cache, "worktree", d, _is_worktree_checkout) for d in between)


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


def _longest_owning_prefix(target: str, prefixes: Iterable[str]) -> str | None:
    """Return the longest of ``prefixes`` that ``target`` starts with.

    ``target`` is a :func:`norm_path` string, or a stored key in its
    :func:`fold_path_form` for the callers that judge indexed sources (#2878).
    ``prefixes`` are :func:`norm_dir_prefix` strings. Both are computed by the
    caller so a loop over many sources normalises each path once. This is the longest-prefix rule of
    :func:`resolve_owning_memory_dir`, which the Sources tree uses, applied to
    strings that are already normalised. The status counts and the web remove
    sweep use it so that each source belongs to exactly one root. (#2524)
    """
    best: str | None = None
    for prefix in prefixes:
        if target.startswith(prefix) and (best is None or len(prefix) > len(best)):
            best = prefix
    return best


def swept_on_remove(target: str, removed_prefix: str, surviving_prefixes: Iterable[str]) -> bool:
    """True when removing the root at ``removed_prefix`` should delete ``target``.

    A source is swept when it lies under the removed root and no index root
    that survives the removal still contains it. A surviving root that
    contains the source (a nested root, an ancestor, or the same path listed
    in another tier) would index it again on its next walk or watcher event,
    so deleting it would only make it disappear for a while (#2534). ``target``
    is a stored key in its :func:`fold_path_form`, which the sweep deletes as
    stored (#2878); the prefixes are :func:`norm_dir_prefix` forms, as in
    :func:`_longest_owning_prefix`.
    """
    return (
        target.startswith(removed_prefix)
        and _longest_owning_prefix(target, surviving_prefixes) is None
    )


def remove_sweeps_nothing(removed_prefix: str, surviving_prefixes: Iterable[str]) -> bool:
    """True when a surviving root contains the whole removed root.

    That root is an ancestor of the removed one or the same path, so
    :func:`swept_on_remove` is false for every source under it. Otherwise the
    sweep deletes exactly the sources the removed root owns under the
    longest-prefix rule, because a surviving root inside it already owns its
    own sources. So the status count of what a remove would delete is either
    0 or the root's exclusive ``chunk_count`` (#2534).
    """
    return any(removed_prefix.startswith(p) for p in surviving_prefixes)


def _count_files_on_disk(
    p: Path,
    extensions: frozenset[str],
    memory_dirs: Iterable[str | Path] = (),
    *,
    root_prefixes: Sequence[str] | None = None,
) -> int:
    """Count regular files under ``p`` whose suffix is in ``extensions``.

    Recursive ``rglob`` so the count matches what ``index_path(recursive=True)``
    would discover, modulo user exclude patterns (left out here so the
    web status fetch stays fast for the dominant case — users will hit
    Reindex anyway, and the badge is informational). Returns 0 on
    ``OSError`` (permissions, broken symlink, etc.) to keep the badge
    reading "0 files" rather than crashing the panel.

    Nested worktree checkouts *are* excluded, unlike user patterns: the web
    Sources panel renders a non-zero count with no chunks as
    "{N} files · not indexed", so a root whose only remaining files are a
    worktree copy would read as work the indexer refuses to do (#2474).

    ``memory_dirs`` must be *every* configured root, not just ``p``. The skip
    only applies to a worktree nested under the root that owns the file, so a
    worktree the user explicitly registered is owned by itself and is indexed.
    A caller that passes none falls back to ``[p]``. Worktrees nested under
    ``p`` are still skipped, but files owned by any other nested root are
    counted, because that root cannot be seen (see below).

    The count is exclusive: a file counts only when ``p`` is its longest-prefix
    owner among ``memory_dirs``, the same rule :func:`memory_dir_stats` applies
    to indexed sources. A file under a more specific configured root counts for
    that root, and so does a registered worktree nested in ``p``. Ownership is
    decided on the file's :func:`norm_path`, which is the key that storage uses.
    So a symlink in ``p`` that points into another root is not counted for
    ``p``. The target file is counted under the root that owns it, which is the
    root its source row is attributed to (#2524). This is a count of directory
    entries, not of distinct files: a file and a link to it inside the same
    root count twice.

    ``root_prefixes`` is ``norm_dir_prefix`` of every entry in ``memory_dirs``.
    :func:`memory_dir_stats` passes it so a status request normalises each
    root once, not once per root. When it is omitted, the prefixes are
    computed here.
    """
    from memtomem.storage.sqlite_helpers import norm_path

    worktree_cache: WorktreeMemo = {}
    roots = list(memory_dirs) or [p]
    own_prefix = norm_dir_prefix(p)
    prefixes = (
        list(root_prefixes) if root_prefixes is not None else [norm_dir_prefix(r) for r in roots]
    )
    if own_prefix not in prefixes:
        prefixes.append(own_prefix)
    try:
        return sum(
            1
            for fp in p.rglob("*")
            if fp.is_file()
            and fp.suffix in extensions
            and _longest_owning_prefix(norm_path(fp), prefixes) == own_prefix
            and not _under_nested_worktree(fp, roots, worktree_cache)
        )
    except OSError:
        return 0


async def _resolved_zero() -> int:
    """Awaitable that resolves to 0 — used for missing dirs in the
    ``asyncio.gather`` slot so the result list stays positionally
    aligned with ``memory_dirs``."""
    return 0


def _distinct_sorted(values: Iterable[str | None]) -> list[str | None]:
    """Distinct namespaces in stable sort order, ``None`` (untagged) last."""
    return sorted(set(values), key=lambda x: (x is None, x or ""))


async def memory_dir_stats(
    storage: "StorageBackend",
    memory_dirs: Iterable[str | Path],
    *,
    supported_extensions: frozenset[str] | None = None,
    hidden_reasons: Callable[[Path], Collection[str]] | None = None,
) -> list[dict[str, object]]:
    """Return per-dir index status for each configured ``memory_dir``.

    Shape: ``[{path, chunk_count, source_file_count, hidden_chunk_count,
    hidden_source_file_count, held_chunk_count, held_source_file_count,
    project_local_chunk_count, project_local_source_file_count, file_count,
    exists, category, provider, kind, created_at, last_indexed}]`` in the same
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
    ``chunks.updated_at`` over the source files the dir owns, leaving out those
    a more specific configured root owns (``None`` when it owns no chunks). Both feed the Web UI sort dropdown that
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
    — avoids N LIKE queries for large dir lists. Counts are exclusive:
    each source goes to its longest-prefix owner only, the rule the
    Sources tree groups by, so a parent root does not count what a
    nested root owns. Per-root numbers of distinct roots can be summed;
    one root listed twice reports the same numbers in both entries
    (#2524). ``kind`` is provided
    by :func:`~memtomem.config.memory_dir_kind` so the Web UI can split
    the Sources page into Memory and General views from the same
    response shape.

    ``hidden_reasons`` maps a source path to why the Sources tree does not
    list it, as :meth:`memtomem.web.source_visibility.SourceVisibility.check`
    answers for ``GET /api/sources``: ``"held"`` (held or pending, #2561),
    ``"scope"`` (the ``project_local`` tier, #2567), both, or neither. A hidden
    source still counts toward ``chunk_count`` and ``source_file_count``,
    which stay totals of what the root owns in the index. The reasons overlap
    (a held draft has both), so each is counted whole and the union once:

    - ``hidden_*``: every source with any reason. Totals minus hidden are
      what the tree lists for the root.
    - ``held_*``: every held or pending source, drafts included.
    - ``project_local_*``: every ``project_local`` source, held or not.

    Without ``hidden_reasons`` all three are 0.
    """
    from memtomem.storage.sqlite_helpers import fold_path_form

    rows = await storage.get_source_files_with_counts()
    dir_list = list(memory_dirs)
    prefixes = [norm_dir_prefix(d) for d in dir_list]

    file_counts: list[int]
    if supported_extensions:
        file_counts = await asyncio.gather(
            *[
                asyncio.to_thread(
                    _count_files_on_disk,
                    Path(d).expanduser(),
                    supported_extensions,
                    dir_list,
                    root_prefixes=prefixes,
                )
                if Path(d).expanduser().exists()
                else _resolved_zero()
                for d in dir_list
            ]
        )
    else:
        file_counts = [0] * len(dir_list)

    # Each source is credited to its longest-prefix owner only, so a parent
    # root's numbers leave out what a nested root owns. The Sources tree groups
    # files by the same rule (#2524). Roots that share a prefix, like one path
    # listed in two tiers, read the same bucket.
    # Bucket: [chunks, sources, last_updated, hidden chunks, hidden sources,
    # held chunks, held sources, project_local chunks, project_local sources].
    buckets: dict[str, list[Any]] = {prefix: [0, 0, None, 0, 0, 0, 0, 0, 0] for prefix in prefixes}
    for row in rows:
        # row = (Path, chunk_count, last_updated, namespaces, ...)
        source_path, count, last_updated = row[0], row[1], row[2]
        # The stored key, not where it resolves now: the remove sweep deletes
        # by the same key, so the dialog's count matches it (#2878).
        owner = _longest_owning_prefix(fold_path_form(str(source_path)), prefixes)
        if owner is None:
            continue
        bucket = buckets[owner]
        bucket[0] += count
        bucket[1] += 1
        if last_updated is not None and (bucket[2] is None or last_updated > bucket[2]):
            bucket[2] = last_updated
        reasons = hidden_reasons(source_path) if hidden_reasons is not None else ()
        if reasons:
            bucket[3] += count
            bucket[4] += 1
        if "held" in reasons:
            bucket[5] += count
            bucket[6] += 1
        if "scope" in reasons:
            bucket[7] += count
            bucket[8] += 1

    out: list[dict[str, object]] = []
    for d, prefix, file_count in zip(dir_list, prefixes, file_counts):
        dir_path = Path(d).expanduser().resolve()
        exists = dir_path.exists()
        (
            chunk_count,
            source_file_count,
            max_last_updated,
            hidden_chunks,
            hidden_sources,
            held_chunks,
            held_sources,
            local_chunks,
            local_sources,
        ) = buckets[prefix]

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
                "hidden_chunk_count": hidden_chunks,
                "hidden_source_file_count": hidden_sources,
                "held_chunk_count": held_chunks,
                "held_source_file_count": held_sources,
                "project_local_chunk_count": local_chunks,
                "project_local_source_file_count": local_sources,
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
