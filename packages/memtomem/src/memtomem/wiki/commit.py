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
from dataclasses import dataclass
from pathlib import Path

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
    "WikiTargetChangedError",
    "commit_targets",
    "wiki_commit_lock_path",
]


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
    """Cross-process commit lock path, in system-temp keyed by the wiki root.

    Kept **outside** the wiki tree on purpose: ``_file_lock`` ``mkdir``s the lock
    file's parent, so a lock under ``<wiki>/.git/`` could forge a bogus ``.git/``
    if the wiki were removed (``WikiStore.exists()`` only checks ``.git`` is a
    dir). A system-temp path also can never show up in ``git status``.

    ``root`` is ``.resolve()``-d here so callers can pass the raw ``store.root``
    (which ``WikiStore.at_default`` leaves un-resolved): two processes deriving
    the path from the same wiki — even via a symlink — land on the **same** lock
    file, which is what makes the web↔CLI exclusion genuinely cross-process.
    """
    digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()[:16]
    return Path(tempfile.gettempdir()) / "memtomem" / f"wiki-commit-{digest}.lock"


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
    a 503, the CLI to a retry hint), or :class:`RuntimeError` (git failure — the
    caller MUST surface a fixed message; the raw stderr embeds the absolute wiki
    path).
    """
    lock_path = wiki_commit_lock_path(store.root)
    with _file_lock(lock_path, timeout=_COMMIT_LOCK_TIMEOUT):
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
