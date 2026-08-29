"""Cross-process-safe isolated commit orchestration for the wiki.

Both the web **Commit affordance** (ADR-0027 §3, ``web/routes/wiki_mutations.py``)
and the ``mm wiki {skill,agent,command} commit`` CLI funnel through
:func:`commit_targets` so the two surfaces share **one** commit code path: the
same wiki-root cross-process file lock (they MUST derive the *same* lock path or
they would not mutually exclude each other), the same read → ``commit_paths`` →
``.bak``-cleanup window, and the same race guards. Layer-specific concerns stay
in each caller — HTTP envelopes + the in-process ``_gateway_lock`` + a worker
thread for the web route; Click output + ``ClickException`` mapping for the CLI.

The heavy lifting (out-of-worktree temp index → ``commit-tree`` → ref
compare-and-swap) lives in :meth:`memtomem.wiki.store.WikiStore.commit_paths`;
this module wraps it with the cross-process lock, the per-target byte read with a
``stat → read → stat`` TOCTOU verify, and the race-guarded backup cleanup.
"""

from __future__ import annotations

import hashlib
import logging
import tempfile
import time
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path

from memtomem._runtime_paths import _test_runtime_dir_override, ensure_runtime_dir
from memtomem.context._atomic import _file_lock
from memtomem.wiki.store import WikiNothingToCommitError, WikiStore

logger = logging.getLogger(__name__)

_COMMIT_LOCK_TIMEOUT = 30.0
"""Cross-process lock budget (seconds).

Bounded well below the web handler's ``asyncio.timeout(60)`` so the worker
thread returns a clean :class:`TimeoutError` instead of being orphaned past the
handler deadline (#1145 precedent). The CLI is synchronous, so the same bound
just caps how long it waits on a concurrent ``mm web`` / second ``mm wiki``
commit before giving up.
"""

__all__ = [
    "CommitOutcome",
    "ResolvedTarget",
    "WikiLockUnavailableError",
    "WikiTargetChangedError",
    "commit_targets",
    "legacy_wiki_commit_lock_path",
    "wiki_commit_lock",
    "wiki_commit_lock_path",
]


class WikiLockUnavailableError(RuntimeError):
    """The commit lock could not be *established* — distinct from contention.

    Contention is :class:`TimeoutError` ("someone else holds it, retry"); this is
    "the lock's own home is unusable", which retrying will not fix: a runtime dir
    owned by another uid, carrying group/world bits, or replaced by a symlink or
    junction (:func:`~memtomem._runtime_paths.ensure_runtime_dir` refuses all
    three), or the ``mkdir``/``open`` of the lock file failing outright.

    Routing the lock through the runtime dir (#2225) made those validation
    refusals reachable from a wiki commit for the first time, and an unclassified
    ``OSError`` here would surface as a CLI traceback or a bare 500. Subclassing
    ``RuntimeError`` means the ``except RuntimeError`` arms every adapter already
    has catch it as a backstop even if a future call site forgets a dedicated arm;
    the dedicated arms exist so the *message* is right rather than "git failed".

    ``__str__`` is the underlying message, which for a validation refusal carries
    the actionable removal hint. It embeds an absolute path, so the **web** route
    must log it rather than echo it — see the path-free envelope there.
    """


class WikiTargetChangedError(RuntimeError):
    """A target's on-disk bytes changed out from under the commit.

    Carries the current ``mtime_ns`` so a caller can echo it: the web route maps
    this to its 409 ``stale_target`` envelope; the CLI prints a re-run hint. Two
    distinct triggers — a stale per-target token (the web client's Save handshake
    no longer matches disk) or a write that landed *during* the byte read (the
    TOCTOU guard). Both mean "don't commit bytes you didn't verify".
    """

    def __init__(self, rel: str, current_mtime_ns: int) -> None:
        super().__init__(rel)
        self.rel = rel
        self.current_mtime_ns = current_mtime_ns


@dataclass(frozen=True)
class ResolvedTarget:
    """One server-resolved file to commit.

    ``rel`` is the wiki-relative POSIX path; ``path`` the absolute on-disk file.
    ``expected_mtime_ns`` is the token the caller last saw (the web Save
    response); ``None`` means **no stale-since-token check** — used by the CLI,
    which reads current disk bytes directly with no prior Save handshake. The
    read-during-read TOCTOU check (bytes changing *while* we read) still applies
    in both modes.
    """

    rel: str
    path: Path
    expected_mtime_ns: int | None = None


@dataclass(frozen=True)
class CommitOutcome:
    """Result of :func:`commit_targets`.

    ``committed`` is ``False`` on the benign no-op path (the saved bytes already
    match HEAD, so no new history was written); ``wiki_head`` / ``wiki_dirty``
    are read back *after* the commit + ``.bak`` cleanup.
    """

    committed: bool
    wiki_head: str
    wiki_dirty: bool


def wiki_commit_lock_path(root: Path) -> Path:
    """Cross-process commit lock path, in the runtime dir keyed by the wiki root.

    Kept **outside** the wiki tree on purpose: ``_file_lock`` ``mkdir``s the lock
    file's parent, so a lock under ``<wiki>/.git/`` could forge a bogus ``.git/``
    if the wiki were removed (``WikiStore.exists()`` only checks ``.git`` is a
    dir). A runtime-dir path also can never show up in ``git status``.

    ``root`` is ``.resolve()``-d here so callers can pass the raw ``store.root``
    (which ``WikiStore.at_default`` leaves un-resolved): two processes deriving
    the path from the same wiki — even via a symlink — land on the **same** lock
    file, which is what makes the web↔CLI exclusion genuinely cross-process.

    The parent is the :mod:`memtomem._runtime_paths` runtime dir rather than a
    raw ``tempfile.gettempdir()`` leaf (#2225): that honours the pytest
    runtime-dir override, so a test's ``tmp_path`` wiki no longer strands a lock
    file in the developer's real temp — one per root, unbounded and never
    unlinked. Unlike the pid helpers this *creates* the directory rather than
    returning a bare path, because ``_file_lock`` would otherwise ``mkdir`` it at
    umask mode and :func:`ensure_runtime_dir` rejects a runtime dir with any
    group/world bit set — leaving a later server start to fail on a directory
    this function made.
    """
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return ensure_runtime_dir() / f"wiki-commit-{digest}.lock"


def legacy_wiki_commit_lock_path(root: Path) -> Path:
    """Pre-#2225 lock path, held transitionally so an upgrade stays exclusive.

    Moving the lock (above) means a process from the previous release and one
    from this release key the same wiki to *different* files and stop excluding
    each other. That window is real, not theoretical: ``mm web`` routinely
    outlives the client that started it (#2226), so a pre-upgrade server can
    still be committing when a freshly installed CLI runs. ``promote_asset``'s
    failure path is the sharp edge — its ``shutil.rmtree(dest_dir)`` rollback is
    only safe because the lock proves the directory is this invocation's alone.

    So :func:`wiki_commit_lock` takes **both**. Retiring this leg is *not* a
    matter of counting releases: a user can skip the transitional release
    entirely, and the very lifecycle that motivates this bridge — a ``mm web``
    that outlives its client — is what makes "surely nobody is still running it"
    unprovable from a version number. It comes out once the support window
    declares pre-#2225 versions unsupported, or once a process that old can no
    longer reach a wiki at all (e.g. an intervening schema/protocol break that
    fences it). Until one of those holds, dropping it re-opens the window.

    Under pytest this is redirected into the runtime-dir override — the whole
    point of #2225 was that the raw ``tempfile.gettempdir()`` leaf stranded one
    never-unlinked file per ``tmp_path`` wiki root in the developer's real temp,
    and re-acquiring it here would have reintroduced exactly that leak. The
    ``legacy/`` subdirectory keeps it a *distinct* file from the canonical lock:
    ``_file_lock`` contends with itself across two open file descriptions in one
    process, so collapsing the pair onto one path would self-deadlock.

    That redirect is also why this goes through :func:`ensure_runtime_dir` rather
    than the bare override path: nested under the runtime dir, ``_file_lock``'s
    ``mkdir(parents=True)`` would otherwise create the runtime dir itself at
    umask, and the next :func:`ensure_runtime_dir` would refuse the 0o755
    directory as unsafe. Ensuring in *both* derivations makes the pair
    order-independent, so a future caller cannot reintroduce that trap by
    acquiring the legacy lock first.
    """
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    if _test_runtime_dir_override() is not None:
        return ensure_runtime_dir() / "legacy" / f"wiki-commit-{digest}.lock"
    return Path(tempfile.gettempdir()) / "memtomem" / f"wiki-commit-{digest}.lock"


@contextmanager
def wiki_commit_lock(root: Path, *, timeout: float) -> Iterator[None]:
    """Hold the wiki's cross-process mutation lock for *root*.

    The single acquisition point for both ``commit_targets`` and
    ``promote_asset``. Nesting order — legacy outer, canonical inner — is fixed
    here rather than repeated at each call site precisely because two call sites
    ordering the pair differently would deadlock ABBA.

    *timeout* is the budget for the **pair**, not per lock: the canonical
    acquisition gets whatever the legacy one left of the deadline. Splitting it
    keeps the ``_COMMIT_LOCK_TIMEOUT`` guarantee that the wait stays bounded
    below the web handler's ``asyncio.timeout(60)``, which a naive nesting of two
    30s waits would have doubled straight through. Expiry raises
    :class:`TimeoutError` from whichever acquisition ran out, having acquired
    nothing that it does not release — the callers' existing "busy, retry" arms
    need no new clause.

    A failure to *establish* either lock — as opposed to losing a race for it —
    becomes :class:`WikiLockUnavailableError`, so no adapter can leak a raw
    ``OSError``. ``TimeoutError`` is itself an ``OSError`` and must keep its own
    meaning, hence the re-raise before the wrap.
    """
    with ExitStack() as stack:
        try:
            # Derivation sits inside the guard because it is
            # ``ensure_runtime_dir`` — the validation refusals — and not merely
            # the lock open that can fail here. ``_file_lock`` is a generator
            # context manager, so the ``mkdir``/``open`` runs on entry, not on
            # the call: only ``enter_context`` is actually guarded.
            canonical = wiki_commit_lock_path(root)
            legacy = legacy_wiki_commit_lock_path(root)
            deadline = time.monotonic() + timeout
            stack.enter_context(_file_lock(legacy, timeout=timeout))
            remaining = max(deadline - time.monotonic(), 0.0)
            stack.enter_context(_file_lock(canonical, timeout=remaining))
        except TimeoutError:
            raise
        except OSError as exc:
            raise WikiLockUnavailableError(str(exc)) from exc
        # Outside the try on purpose: an ``OSError`` raised by the *caller's*
        # body (a failed write under the lock) must keep its own type rather
        # than be reclassified as a lock-establishment failure.
        yield


def commit_targets(
    store: WikiStore,
    targets: list[ResolvedTarget],
    *,
    message: str,
    expected_head: str | None = None,
    force: bool = False,
) -> CommitOutcome:
    """Commit *targets* in isolation onto HEAD, cross-process-locked.

    Holds the wiki-root file lock for the whole read → commit → reconcile →
    cleanup window, so a concurrent CLI ``mm wiki`` / second ``mm web`` commit
    cannot interleave. Per target the bytes are read under a ``stat → read →
    stat`` verify so the committed blob is exactly the bytes whose ``mtime_ns``
    matched (a concurrent same-path write → :class:`WikiTargetChangedError`,
    never a stale-bytes commit). ``.bak`` cleanup is race-guarded (it only
    unlinks a backup whose mtime snapshot, taken *pre*-commit, still matches) and
    also runs on the no-op path so a save-identical-bytes-then-commit never
    leaves the wiki dirty.

    Known limitation: ``mtime_ns`` is the whole staleness token, so two
    ``os.replace`` saves landing within one mtime tick are indistinguishable —
    negligible with nanosecond mtime resolution (APFS/ext4); a coarse-mtime
    filesystem would need the token paired with a size/content hash (#1520).

    ``expected_head`` is the compare-and-swap guard threaded to
    :meth:`WikiStore.commit_paths`:

    * a concrete SHA (the **web** route — the ``wiki_head`` the browser last saw)
      commits *only* if HEAD still matches, else :class:`~memtomem.wiki.store.WikiHeadMovedError`;
    * ``None`` (the **CLI**, which has no stale browser view) reads HEAD **inside
      the lock** and commits onto that — i.e. onto the freshest HEAD. The atomic
      ``update-ref`` CAS in ``commit_paths`` still guards the tiny window against
      a truly external ``$EDITOR``+git that honours no lock.

    Raises :class:`WikiTargetChangedError` (a target moved — caller → conflict),
    :class:`~memtomem.wiki.store.WikiHeadMovedError` (HEAD advanced — propagated),
    :class:`~memtomem.wiki.store.WikiDetachedHeadError` (no branch to commit
    onto — propagated; the message is fixed and path-free),
    :class:`TimeoutError` (the cross-process lock is held past
    ``_COMMIT_LOCK_TIMEOUT`` by a concurrent committer — the web route maps it to
    a 503, the CLI to a retry hint), :class:`WikiLockUnavailableError` (the
    lock's runtime dir is unusable — **not** contention, so a caller must not
    offer "retry"; it subclasses ``RuntimeError``, so an arm for it MUST precede
    the one below or it is misreported as a git failure), or
    :class:`RuntimeError` (git failure — the caller MUST surface a fixed message;
    the raw stderr embeds the absolute wiki path).
    """
    with wiki_commit_lock(store.root, timeout=_COMMIT_LOCK_TIMEOUT):
        # Read HEAD inside the lock when the caller supplied no CAS token, so the
        # CLI commits onto the freshest HEAD rather than a value snapshotted
        # before acquiring the lock. commit_paths re-validates the shape.
        head = expected_head if expected_head is not None else store.current_commit()

        files: dict[str, bytes] = {}
        # (target_path, committed_mtime_ns, bak_path, bak_mtime_snapshot_or_None)
        cleanup: list[tuple[Path, int, Path, int | None]] = []
        for target in targets:
            rel, path = target.rel, target.path
            if not path.is_file():
                # A target the caller resolved is gone — unrecoverable, even with force.
                raise WikiTargetChangedError(rel, 0)
            before = path.stat().st_mtime_ns
            if target.expected_mtime_ns is not None and before != target.expected_mtime_ns:
                # Stale since the caller's Save token (web only; CLI passes None).
                if not force:
                    raise WikiTargetChangedError(rel, before)
                logger.warning(
                    "force-commit bypassed stale mtime on %s (expected=%s actual=%s)",
                    rel,
                    target.expected_mtime_ns,
                    before,
                )
            data = path.read_bytes()
            after = path.stat().st_mtime_ns
            if after != before:
                # The file changed during the read (concurrent writer).
                if not force:
                    raise WikiTargetChangedError(rel, after)
                data = path.read_bytes()
                after = path.stat().st_mtime_ns
            files[rel] = data
            # Snapshot the target's own .bak (the one this asset's last Save left)
            # so cleanup can unlink ONLY that exact backup. A Save writes the .bak
            # *before* replacing the target and does NOT take this commit lock, so a
            # concurrent cross-process Save can drop a *fresh* .bak while the target
            # mtime still matches; matching the snapshot avoids deleting it.
            bak = path.with_suffix(path.suffix + ".bak")
            bak_mtime = bak.stat().st_mtime_ns if bak.is_file() else None
            cleanup.append((path, after, bak, bak_mtime))

        try:
            store.commit_paths(files, message=message, expected_head=head)
            committed = True
        except WikiNothingToCommitError:
            committed = False

        # Race-guarded .bak cleanup: remove a committed target's own backup only
        # when (a) one existed at commit time, (b) the target is still the bytes we
        # committed, and (c) the .bak is byte-for-byte the same file (mtime
        # unchanged) — never a fresh backup a concurrent Save just wrote. Runs
        # outside the try/except so the no-op path cleans up too.
        for path, expect_mtime, bak, bak_snapshot in cleanup:
            if bak_snapshot is None:
                continue  # no backup at commit time → never delete one now
            try:
                target_unchanged = path.is_file() and path.stat().st_mtime_ns == expect_mtime
                bak_unchanged = bak.is_file() and bak.stat().st_mtime_ns == bak_snapshot
                if target_unchanged and bak_unchanged:
                    bak.unlink(missing_ok=True)
            except OSError:
                logger.warning("wiki commit: .bak cleanup failed for %s", path.name)

        return CommitOutcome(
            committed=committed,
            wiki_head=store.current_commit(),
            wiki_dirty=store.is_dirty(),
        )
