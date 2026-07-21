"""Crash-safe replacement of a whole directory tree (ADR-0030 §10).

A non-empty directory cannot be replaced atomically on POSIX: ``os.rename`` /
``os.replace`` onto a non-empty destination is ``ENOTEMPTY``, and Linux's
``renameat2(RENAME_EXCHANGE)`` has no macOS equivalent. So replacing a Store
skill's tree is inherently **two renames** — ``dst → old``, then
``staging → dst`` — and a crash between them leaves the canonical position
empty with the only copy of the original tree under an ``.old-*`` name.

That is the whole reason this module exists. The two renames are bracketed by
a **durable intent marker** written before the first one and unlinked after
the second, so a later run can tell "mid-swap" from "nothing happened" and
converge (:func:`recover_pending_swaps`). Without the marker, the leftovers
are indistinguishable from ordinary crash debris and a reaper would delete the
user's only copy.

**Precondition for every writer, stated here because getting it wrong is how
the only copy dies.** Under C0, :func:`recover_pending_swaps` runs FIRST, and
only then may anything reap crash leftovers — and a reap must skip any
transient :func:`marker_owns_transient` still claims. A refusal from recovery
aborts; it must never fall through to a reap. The reason is concrete: today
``skills`` deletes crash leftovers from **two** marker-blind sites under the
same C0 lock — ``_recover_and_reap_internal_dirs``, the pre-write prelude that
sweeps both ``.staging-*`` and ``.old-*``, and ``_reap_move_aside``, the
post-promote collector that sweeps ``.old-*``. Both are G4a-3's fan-out; a
list that names only the prelude leaves the other one wired the old way.

Presence alone already saves the common case: an ``.old-*`` is kept while
``dst`` is absent (ADR-0030 §10), which is exactly the mid-swap shape. It is
not a substitute for the marker, because presence cannot see ownership. Row 4
is the counterexample — a foreign ``dst`` present alongside our original under
``.old-*`` reads as "present, therefore collectable", and a marker-blind reap
deletes it. The transient side is unguarded for the same reason from the other
direction: a reap that removes ``staging`` between the renames turns row 2 into
row 5, so recovery rolls back a swap whose replacement tree was complete. This
module supplies the predicate and states the contract so the sequence is not
left implicit.

Four names, all direct children of the canonical root, sharing one artifact
``<name>`` and one transaction suffix ``S = "<pid>-<6 hex>"``::

    dst      = <root>/<name>
    staging  = <root>/.staging-<name>-<S>.tmp
    old      = <root>/.old-<name>-<S>.tmp
    marker   = <root>/.swap-<name>-<S>.json

Both renames use :func:`~memtomem.context._atomic.rename_no_replace`. That is
not belt-and-braces: with ``os.replace`` a pre-existing ``old`` would be
silently *adopted* after the marker is already durable, and rename 2 would
clobber a ``dst`` an editor or shell recreated during the window. Exclusivity
guarantees whatever ends up at ``old`` was put there by *this* transaction.

Everything here assumes the caller already holds the canonical name lock (C0)
for ``<name>`` and never re-acquires it — ``_atomic._file_lock`` is
non-reentrant, so re-acquiring would self-deadlock. C0 is also the real
boundary this module defends: the threat model is **static wrong-type content,
accidental misconfiguration and crash debris**, plus *clobber* protection via
the exclusive renames. It is NOT a defense against a local actor actively
racing the transaction (ADR-0030 §6 already draws that line — "non-first-party
writers (editors, shells) remain outside the guarantee").

**Durability degrades, it never fails the operation.**
:func:`~memtomem.context._atomic.fsync_dir` returns a bool and never raises, so
on filesystems that reject directory fsync (Windows, some network/tmpfs
mounts) the guarantee drops to process-crash consistency — which is exactly
what the marker and the recovery machine cover anyway, since they are
ordering-independent given a crash and only power-cut-vulnerable.

**Windows asymmetry, researched rather than assumed.** The marker is read with
``O_NOFOLLOW | O_NONBLOCK`` and its regular-file-ness proven on the descriptor,
because a correctly-named FIFO would otherwise block ``read()`` forever *while
C0 is held*, wedging every writer for that artifact. Neither flag exists on
Windows, and ``fstat`` + ``S_ISREG`` is weaker there (MSVCRT reports
``_S_IFREG`` for any non-directory disk handle). We accept that and
deliberately do NOT add a ``ctypes``/``CreateFileW`` backend: NTFS has no FIFO
in the file namespace — named pipes live only under ``\\\\.\\pipe\\`` — so no
ordinary path can block an open indefinitely, and the residual (reparse-point
following) is shared by every other path read in this package on that
platform. The flags are therefore ``getattr(os, "O_NOFOLLOW", 0)``-style
module constants that degrade to 0.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import re
import secrets
import shutil
import stat
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Final

from memtomem.context._atomic import atomic_write_bytes, fsync_dir, rename_no_replace
from memtomem.context._names import validate_name

logger = logging.getLogger(__name__)

__all__ = [
    "SwapForeignDestination",
    "SwapRecoveryError",
    "marker_owns_transient",
    "new_swap_suffix",
    "recover_pending_swaps",
    "staging_path_for",
    "swap_dir_tree",
]


#: Marker payload schema. Fail-closed on anything else: a marker written by a
#: newer memtomem describes a protocol this build cannot unwind, and guessing
#: is how the only copy of a tree gets deleted. Wedging the artifact until the
#: operator looks IS the correct outcome here — the remediation for every
#: fail-closed state in this module is the same "inspect these paths by hand".
_MARKER_VERSION: Final = 1

#: Hard cap on marker size. The payload is seven short JSON fields; anything
#: larger is not ours and must not be parsed (or read into memory) at all.
_MARKER_MAX_BYTES: Final = 4096

#: ``O_NOFOLLOW``/``O_NONBLOCK`` degrade to 0 where the platform lacks them —
#: see the module docstring for why that is sound on Windows specifically.
_NOFOLLOW: Final[int] = getattr(os, "O_NOFOLLOW", 0)
_NONBLOCK: Final[int] = getattr(os, "O_NONBLOCK", 0)

#: Every pattern in this module ends in ``\Z``, never ``$``, and that is a
#: containment rule rather than a style choice: Python's ``$`` ALSO matches
#: immediately before a trailing newline, and a newline is a legal POSIX
#: filename character. With ``$``, ``.swap-<name>-<S>.json\n`` would satisfy
#: the "anchored, exact name" check the rest of the module leans on, and the
#: paths derived from it would name a DIFFERENT (newline-free) file — so
#: recovery would act on the real transients, unlink a marker that was never
#: there, and report the transaction resolved while the true marker stayed
#: live.
#:
#: Transaction suffix: the same ``<pid>-<6 hex>`` discipline
#: ``skills._stage_skill`` already uses, so the transients this module creates
#: are recognized by ``_names.is_internal_artifact_dir`` and reaped like any
#: other crash debris once they are no longer marker-owned.
_SUFFIX_RE: Final = re.compile(r"^\d+-[0-9a-f]{6}\Z")

#: Splits ``.staging-<name>-<pid>-<rand>.tmp`` / ``.old-<name>-<pid>-<rand>.tmp``
#: into the ``<name>-<pid>-<rand>`` BODY, which is transplanted verbatim into
#: ``.swap-<body>.json``. Deriving the marker from the body rather than from a
#: parsed ``(name, suffix)`` pair sidesteps the split ambiguity entirely:
#: ``.staging-foo-1-abcdef-2-bcdefa.tmp`` has two readings, and both produce
#: the SAME body, hence the same marker.
_TRANSIENT_BODY_RE: Final = re.compile(r"^\.(?:staging|old)-(?P<body>.+-\d+-[0-9a-f]{6})\.tmp\Z")


class SwapRecoveryError(OSError):
    """A swap could not be brought to a resolved state; the caller must stop.

    Two shapes, and the difference matters to whoever reads the message:

    * **Refused before acting** — a tampered or ambiguous marker, or a
      wrong-type entry where a directory must be. Disk is EXACTLY as found.
    * **Refused after acting** — the rename-2 unwind's restore failed, or
      recovery completed a row's rename but could not then remove the marker.
      Disk has advanced to a *safe, still-marked* state, so a later run
      reclassifies and continues rather than starting over.

    In neither shape is the caller expected to clean up: the artifact stays
    visibly unresolved until it converges or an operator looks at it.

    Subclasses ``OSError`` so a caller that already funnels ``OSError`` into a
    typed skip degrades safely if a translation is ever missed — but every
    boundary translates it explicitly (ADR-0030 §10 / the G4 design's §2.1.2).

    ``errno`` is deliberately ``EBUSY`` and must never be ``EEXIST`` /
    ``ENOTEMPTY``: ``skills._promote_race_conflict`` demotes exactly those two
    (with no ``__cause__``) to an ordinary ``target_conflict`` skip, and this
    state is not a target conflict — it is a wedged artifact that needs an
    operator.
    """

    def __init__(
        self,
        *args: object,
        original: BaseException | None = None,
        retained: Sequence[Path] = (),
    ) -> None:
        super().__init__(*args)
        #: The failure that STARTED the unwind, when this error was raised
        #: while handling another one. ``__cause__`` carries the failure that
        #: made the unwind itself fail — the two are different errors and
        #: collapsing them loses the one an operator actually needs.
        self.original = original
        #: Paths deliberately left on disk. **Informational only** — for the
        #: log line and the operator-facing message. It is NOT the signal for
        #: "may I delete the staging tree": that question is answered by
        #: :func:`marker_owns_transient` reading the disk, because a SIGKILL
        #: between the marker write and the unwind produces the same retained
        #: state with no exception to carry an attribute at all.
        self.retained = tuple(retained)


class SwapForeignDestination(SwapRecoveryError):
    """The destination was (re)created by someone outside the gateway.

    Two shapes reach here, and they are NOT the same as an ordinary target
    conflict:

    * recovery row 4 (``dst`` + ``old`` + ``staging`` all present), where the
      provenance of ``dst`` is genuinely ambiguous — see
      :func:`recover_pending_swaps`;
    * an ``EEXIST`` from a recovery rename, i.e. ``dst`` came back between the
      classification and the restore.

    Distinct from the base class so a surface can say "someone else wrote
    here" rather than "recovery failed", while both still map to the single
    ``swap_recovery_pending`` reason code.
    """


@dataclass(frozen=True)
class _SwapPaths:
    """The four §4 names, derived once from one ``(root, name, suffix)``.

    Single derivation point on purpose: the marker's relational validation
    compares the JSON's path fields against *these* names, so if any caller
    could build one of the four independently the comparison would degrade
    from "equals what we derived" to "equals a string we were handed".
    """

    root: Path
    name: str
    suffix: str
    dst: Path
    old: Path
    staging: Path
    marker: Path


def _swap_paths(root: Path, name: str, suffix: str) -> _SwapPaths:
    """Derive the four names. *name* must already be a validated identifier.

    That precondition is what makes ``root / name`` a direct CHILD, and every
    containment argument in this module rests on it: ``""`` and ``"."`` would
    collapse ``dst`` onto *root* itself, and ``".."`` would aim it at the
    parent — after which a recovery row would happily rename or remove a tree
    outside the canonical root. The public entry points call
    :func:`~memtomem.context._names.validate_name` before they get here, which
    rejects those three along with separators and control characters.
    """
    return _SwapPaths(
        root=root,
        name=name,
        suffix=suffix,
        dst=root / name,
        old=root / f".old-{name}-{suffix}.tmp",
        staging=root / f".staging-{name}-{suffix}.tmp",
        marker=root / f".swap-{name}-{suffix}.json",
    )


def _marker_re_for(name: str) -> re.Pattern[str]:
    """Anchored, exact-``name`` marker matcher for *name*.

    ``re.escape`` + full anchoring rather than a generic ``.+`` name group:
    with a prefix match, ``.swap-foo-*`` also matches
    ``.swap-foo-bar-<pid>-<rand>.json``, which belongs to the perfectly valid
    artifact ``foo-bar`` — the cross-destination bug #1871 fixed for the
    ``.staging``/``.old`` reaper, in the one other place the same shape occurs.
    """
    return re.compile(rf"^\.swap-{re.escape(name)}-(?P<pid>\d+)-(?P<rand>[0-9a-f]{{6}})\.json\Z")


def _staging_re_for(name: str) -> re.Pattern[str]:
    """Anchored, exact-``name`` staging matcher for *name* (see :func:`_marker_re_for`)."""
    return re.compile(rf"^\.staging-{re.escape(name)}-(?P<pid>\d+)-(?P<rand>[0-9a-f]{{6}})\.tmp\Z")


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def new_swap_suffix() -> str:
    """Allocate one transaction suffix ``<pid>-<6 hex>``.

    The caller allocates it because it needs the staging PATH before the tree
    it will hold exists; :func:`swap_dir_tree` reads the suffix back out of
    that path's basename rather than allocating a second one.
    """
    return f"{os.getpid()}-{secrets.token_hex(3)}"


def staging_path_for(dst: Path, suffix: str) -> Path:
    """Where a swap's staging tree for *dst* must be built.

    The naming grammar lives here, with the parser that reads it back, so a
    caller cannot hand :func:`swap_dir_tree` a basename it is unable to bind a
    marker to.
    """
    if not _SUFFIX_RE.match(suffix):
        raise ValueError(f"swap suffix {suffix!r} is not '<pid>-<6 hex>'")
    return dst.parent / f".staging-{dst.name}-{suffix}.tmp"


def marker_owns_transient(transient: Path) -> bool:
    """True when a live swap marker claims *transient*, so it is not ours to delete.

    The §4.1 invariant: **a transient claimed by a live marker is removed only
    by the successful forward path or by :func:`recover_pending_swaps` — never
    by a caller's cleanup, and never by the crash-leftover reaper.** Delete one
    out from under a surviving marker and the next recovery run classifies a
    state that marker no longer describes: dropping a claimed ``.staging-*``
    turns the fail-closed "all three present" row into the "``dst`` + ``old``"
    row, whose action then deletes ``old``.

    Answers for BOTH transients, not just staging. The ``.old-*`` half is the
    dangerous one — it holds the **pre-image**, and after a crash between the
    renames it is the only copy of the artifact in existence, while a staging
    tree is at worst a rebuildable replacement.

    This is a DISK probe rather than a return value or an exception attribute
    on purpose. The invariant is a property of what is on disk, not of how
    control left the swap: a ``SIGKILL`` between the marker write and the
    unwind leaves marker-owned transients behind with no exception in flight at
    all, and a control-flow signal would miss exactly that case.

    Never raises; a path that is not a conforming transient basename, or whose
    marker is absent or not a regular file, is simply not claimed. Meaningful
    only while holding C0 for the artifact.
    """
    match = _TRANSIENT_BODY_RE.match(transient.name)
    if match is None:
        return False
    marker = transient.parent / f".swap-{match.group('body')}.json"
    try:
        return stat.S_ISREG(os.lstat(marker).st_mode)
    except OSError:
        return False


# ── Marker I/O ────────────────────────────────────────────────────────


def _marker_bytes(paths: _SwapPaths) -> bytes:
    """The marker payload — **basenames only**, never absolute paths.

    A marker that is copied or moved therefore cannot name a tree outside its
    own directory, and every path field is a value the reader can re-derive
    from the basename it parsed rather than a location it must trust.
    """
    payload = {
        "version": _MARKER_VERSION,
        "name": paths.name,
        "suffix": paths.suffix,
        "dst": paths.dst.name,
        "old": paths.old.name,
        "staging": paths.staging.name,
        "created_at": _now_iso(),
    }
    return json.dumps(payload, sort_keys=True).encode("utf-8")


def _write_marker(paths: _SwapPaths) -> None:
    """Make the intent durable. After this returns, ``staging`` is marker-owned.

    ``full_fsync=True`` plus the parent flush is the ordering the whole
    protocol rests on: the marker must reach stable storage BEFORE the first
    rename, or a power cut can leave the renames visible with no record that
    they were ever intended.
    """
    atomic_write_bytes(paths.marker, _marker_bytes(paths), full_fsync=True)
    fsync_dir(paths.root)


def _read_marker_bytes(marker: Path) -> bytes:
    """Read a marker without ever blocking and without following a symlink.

    ``O_NONBLOCK`` makes the open itself non-blocking even for a FIFO;
    ``fstat`` on the DESCRIPTOR (not an ``lstat`` before the open) closes the
    window where the entry is swapped for a FIFO between the check and the
    open; and the read loops to EOF or ``CAP + 1`` because a single
    ``os.read`` may return short — which would neither prove the size bound
    nor read a valid marker.

    A pre-open ``lstat`` + ``S_ISREG`` check is NOT a substitute for any of
    that, and ``O_NOFOLLOW`` alone rejects only symlinks.
    """
    try:
        fd = os.open(marker, os.O_RDONLY | _NOFOLLOW | _NONBLOCK)
    except OSError as exc:
        raise SwapRecoveryError(
            errno.EBUSY, f"swap marker is unreadable ({exc})", str(marker)
        ) from exc
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise SwapRecoveryError(errno.EBUSY, "swap marker is not a regular file", str(marker))
        chunks: list[bytes] = []
        size = 0
        while size <= _MARKER_MAX_BYTES:
            block = os.read(fd, _MARKER_MAX_BYTES + 1 - size)
            if not block:
                return b"".join(chunks)
            chunks.append(block)
            size += len(block)
        raise SwapRecoveryError(
            errno.EBUSY,
            f"swap marker exceeds {_MARKER_MAX_BYTES} bytes",
            str(marker),
        )
    finally:
        os.close(fd)


def _find_marker(root: Path, name: str) -> Path | None:
    """The one live marker for *name* under *root*, or ``None``.

    **Two markers for the same name is fail-closed**, not "pick one": they
    describe two transactions over the same artifact, so acting on either
    could rename or delete a tree the other one owns.

    A ``.swap-<name>-…`` entry that does NOT match the anchored shape is not
    a marker of ours and is left alone — the same reasoning that stops the
    ``.staging``/``.old`` reaper from deleting a user directory that merely
    starts with the right prefix (#1229).
    """
    pattern = _marker_re_for(name)
    found: list[Path] = []
    try:
        with os.scandir(root) as entries:
            for entry in entries:
                if pattern.match(entry.name):
                    found.append(root / entry.name)
    except FileNotFoundError:
        return None
    if not found:
        return None
    if len(found) > 1:
        raise SwapRecoveryError(
            errno.EBUSY,
            "two swap markers claim the same artifact; refusing to guess which "
            f"transaction owns the transients ({', '.join(sorted(p.name for p in found))})",
            str(root / name),
        )
    return found[0]


def _load_marker(marker: Path, root: Path, name: str) -> _SwapPaths:
    """Parse and RELATIONALLY validate *marker*, or fail closed.

    Shape validation alone is not enough. Every field is checked against the
    values derived from the marker's own basename, so a tampered or transplanted
    marker cannot direct a later ``rmtree`` at an unrelated tree:

    * the basename matches ``.swap-<name>-<pid>-<6 hex>.json`` for THIS name;
    * the JSON's ``name`` / ``suffix`` equal the ones parsed from the basename;
    * ``dst`` / ``old`` / ``staging`` equal the three derived basenames exactly.

    **That last equality IS the containment proof, given a validated name**,
    and it is worth being precise about why, because it is tempting to add a
    second, weaker one on top. The returned paths are never built from the
    marker's strings: they are :func:`_swap_paths`'s ``root / f"<literal>"``
    over a *name the public entry point already validated*, so they are direct
    children of *root* by construction, and the payload is only ever compared
    against them. A field carrying ``../escape`` or an absolute path therefore
    fails the comparison rather than being sanitized — there is no separator
    check to write here, because no marker string ever reaches a path join.
    (Re-checking ``expected.*`` for separators or a resolved parent can only
    re-assert what the constructor guarantees; the check that actually has
    something to reject is ``validate_name`` at the boundary.)

    The remaining refusals are elsewhere by design: the marker file's own type
    is proven on its descriptor in :func:`_read_marker_bytes`, and each
    transient's type — including "not a symlink" — in :func:`_present_dir`,
    immediately before it could be renamed or removed.

    Any mismatch, an unsupported ``version``, or unparseable JSON raises and
    **deletes nothing**.
    """
    basename = _marker_re_for(name).match(marker.name)
    if basename is None:  # pragma: no cover - _find_marker only yields matches
        raise SwapRecoveryError(errno.EBUSY, "swap marker name is malformed", str(marker))
    suffix = f"{basename.group('pid')}-{basename.group('rand')}"
    expected = _swap_paths(root, name, suffix)

    raw = _read_marker_bytes(marker)
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SwapRecoveryError(
            errno.EBUSY, f"swap marker is not valid JSON ({exc})", str(marker)
        ) from exc
    if not isinstance(payload, dict):
        raise SwapRecoveryError(errno.EBUSY, "swap marker is not a JSON object", str(marker))
    if payload.get("version") != _MARKER_VERSION:
        raise SwapRecoveryError(
            errno.EBUSY,
            f"swap marker declares unsupported version {payload.get('version')!r}",
            str(marker),
        )
    for field, want in (
        ("name", name),
        ("suffix", suffix),
        ("dst", expected.dst.name),
        ("old", expected.old.name),
        ("staging", expected.staging.name),
    ):
        got = payload.get(field)
        if got != want:
            raise SwapRecoveryError(
                errno.EBUSY,
                f"swap marker field {field!r} is {got!r}, expected {want!r}",
                str(marker),
            )

    return expected


# ── Durability barriers ───────────────────────────────────────────────


def _barrier_unlink_marker(paths: _SwapPaths) -> bool:
    """Flush, unlink the marker, flush again — in that order, in one place.

    The leading flush is load-bearing and not decoration (design gate R5):
    with a single trailing flush a power cut can persist the marker's deletion
    while losing the rename it was describing, leaving an UNMARKED state the
    recovery machine can no longer see. **The marker must outlive the state it
    describes.** Every mutating path routes through here so the order cannot
    drift, and so the ordering test has one call to spy on.

    Returns whether the marker is gone. **Every** caller that goes on to delete
    a transient must honour a ``False`` — the forward unwinds and the recovery
    rows alike: while the marker survives, the transients are still
    marker-owned, and removing one silently reclassifies the state for the next
    recovery run. A retained ``.old-*`` plus a deleted staging tree turns the
    fail-closed "all three present" row into the "``dst`` + ``old``" row, whose
    action then deletes ``old``.
    """
    fsync_dir(paths.root)
    try:
        paths.marker.unlink(missing_ok=True)
    except OSError as exc:
        logger.error(
            "swap: could not unlink marker %s (%s); leaving the transients it claims in place",
            paths.marker,
            exc,
        )
        return False
    fsync_dir(paths.root)
    return True


def _clear_marker_or_refuse(paths: _SwapPaths) -> None:
    """Recovery's form of the barrier: an un-removable marker is a REFUSAL.

    The forward paths can tolerate a failed unlink because they leave a
    self-healing state behind. Recovery cannot: it is the prelude every
    canonical writer runs, and returning normally tells that writer the
    artifact is resolved. If the marker survives, it is not — the next write
    materializes ``dst``, and the run after that reads the stale marker against
    a state it does not describe and classifies into the wrong row. So the
    transaction stays visibly unresolved and the caller stops.
    """
    if not _barrier_unlink_marker(paths):
        raise SwapRecoveryError(
            errno.EBUSY,
            f"recovered the swap for {paths.name!r} but could not remove its marker "
            f"('{paths.marker}'), so the transaction is not resolved; nothing further may "
            "touch this artifact until the marker is removed by hand",
            str(paths.dst),
            retained=(paths.marker,),
        )


# ── Forward protocol (§4) ─────────────────────────────────────────────


def _unwind_rename1(paths: _SwapPaths) -> None:
    """Undo a swap that never moved anything. ``dst`` was not touched."""
    if _barrier_unlink_marker(paths):
        _rmtree_quietly(paths.staging, "staging tree")


def _unwind_rename2(paths: _SwapPaths, promote_exc: OSError) -> None:
    """Put ``old`` back at ``dst`` after the promotion failed, or fail closed.

    The restore is exclusive, so if a non-gateway writer recreated ``dst``
    during the window it fails ``EEXIST`` rather than clobbering them. In that
    case **all three** of marker, ``old`` and ``staging`` are left in place:
    that residue is the only breadcrumb pointing at ``old``, and deleting the
    staging tree would collapse the fail-closed "all three present" recovery
    row into the "``dst`` + ``old``" row — whose recovery deletes ``old``, the
    original.

    One raise, with both failures attached: the restore failure as
    ``__cause__`` (which ``skills._promote_race_conflict`` reads to refuse
    demoting this state to an ordinary skip) and the original promotion
    failure in ``original``.
    """
    try:
        rename_no_replace(paths.old, paths.dst)
    except OSError as restore_exc:
        logger.error(
            "swap: could not restore %s after a failed promotion (%s). The ORIGINAL tree "
            "survives at %s and the marker is retained so recovery can see it; %s is also "
            "retained and must NOT be deleted by hand.",
            paths.dst,
            restore_exc,
            paths.old,
            paths.staging,
        )
        raise SwapRecoveryError(
            errno.EBUSY,
            f"directory swap could not be unwound; the original tree is at '{paths.old}'",
            str(paths.dst),
            original=promote_exc,
            retained=(paths.marker, paths.old, paths.staging),
        ) from restore_exc
    if _barrier_unlink_marker(paths):
        _rmtree_quietly(paths.staging, "staging tree")


def _post_commit_cleanup(paths: _SwapPaths) -> None:
    """Everything after rename 2. **Never raises.**

    The commit point is rename 2: the bytes are on disk and the canonical IS
    the new tree. Reporting a cleanup failure as a write failure would tell
    the caller the operation failed for a write that actually landed — and
    suppress the deferred privacy-gate counter for it. The leftovers are
    self-healing: a surviving marker with ``dst`` + ``old`` present is a
    recovery row, and a surviving ``old`` with ``dst`` present is ordinary
    reapable debris.
    """
    if _barrier_unlink_marker(paths):
        _rmtree_quietly(paths.old, "move-aside tree")


def _rmtree_quietly(path: Path, what: str) -> None:
    """Remove a transient we own; log rather than raise if it will not go.

    Every call site here is post-barrier cleanup, and the residue it leaves is
    UNMARKED — ordinary crash debris the reaper handles on the next run. Loud
    rather than ``ignore_errors=True``: silence here is how a leaked tree goes
    unnoticed until it collides with a later transaction.
    """
    try:
        shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        logger.error("swap: could not remove %s %s (%s)", what, path, exc)


def _require_dir(path: Path, what: str) -> None:
    """Refuse anything that is not a real, non-symlink directory.

    ``ValueError`` rather than :class:`SwapRecoveryError`: this runs before the
    marker exists, so there is no transaction to be pending — it is a caller
    handing the primitive the wrong kind of thing.
    """
    try:
        mode = os.lstat(path).st_mode
    except OSError as exc:
        raise ValueError(f"{what} '{path}' is unusable ({exc})") from exc
    if not stat.S_ISDIR(mode):
        raise ValueError(
            f"{what} '{path}' is not a directory "
            "(symlinks and special files are refused, as they are during recovery)"
        )


def swap_dir_tree(staging: Path, dst: Path) -> None:
    """Replace the directory *dst* with the already-built tree *staging*.

    *staging* must be a complete, durable tree that is a sibling of *dst*:
    this function neither builds nor validates its contents, and the marker it
    writes is precisely the assertion "staging is COMPLETE" that recovery
    later relies on to choose forward over rollback. Building it durably
    (``write_tree_payload(..., durable=True)`` or equivalent) is the caller's
    half of the contract.

    Requires the canonical name lock (C0) for ``dst.name``, held by the caller
    and never re-acquired here.

    A failure at a KNOWN point is unwound immediately rather than left for
    :func:`recover_pending_swaps` to guess at, and the original exception
    propagates. The one exception is a rename-2 unwind whose restore itself
    fails: that raises :class:`SwapRecoveryError` (see :func:`_unwind_rename2`)
    and deliberately leaves marker, ``old`` and ``staging`` on disk.

    **Only an ``OSError`` unwinds.** Every unwind assumes the rename it is
    undoing did not happen, and only a failed rename proves that. A signal —
    ``KeyboardInterrupt``, or anything else raised asynchronously — can be
    delivered AFTER the kernel completed the rename and before the ``try``
    block exits, so treating it as "the rename failed" would unwind a move that
    actually happened: the marker would be deleted, and the original tree would
    survive only as unmarked ``.old-*`` debris that the reaper is free to
    delete. Cancellation therefore propagates untouched, leaving the durable
    marker for :func:`recover_pending_swaps` to resolve from disk state.

    Failures AFTER rename 2 are logged and swallowed — see
    :func:`_post_commit_cleanup`.

    *dst* must EXIST as a non-symlink directory — this is a replacement, not a
    create. ``rename_no_replace`` into an absent destination is the ordinary
    promote and belongs in the caller.

    :raises InvalidNameError: *dst*'s name is not a valid identifier. A
        ``ValueError``, deliberately NOT an :class:`OSError` — see
        :func:`recover_pending_swaps` for why the distinction matters to a
        caller sweeping many artifacts.
    :raises ValueError: *staging* is not a conforming sibling staging path for
        *dst*, or either end is not a real directory.
    :raises SwapRecoveryError: a swap for *dst* is already pending and was not
        recovered first, or the unwind itself failed and state is retained.
    :raises OSError: the swap failed at a known point and was unwound.
    """
    # Validated FIRST: every path this function derives is ``dst.parent / f"…
    # {dst.name}…"``, so a name of "", "." or ".." would aim the transaction at
    # the root itself or at its parent. See :func:`_swap_paths`.
    validate_name(dst.name, kind="artifact name")
    if staging.parent != dst.parent:
        raise ValueError(f"staging '{staging}' must be a sibling of '{dst}'")
    match = _staging_re_for(dst.name).match(staging.name)
    if match is None:
        raise ValueError(
            f"staging basename {staging.name!r} is not '.staging-{dst.name}-<pid>-<rand>.tmp'"
        )
    # An existing marker means an unresolved transaction owns these names, and
    # the write below would REPLACE it — losing the record of a pending swap
    # and letting this one unwind over the other's transients. Recovery is a
    # precondition, not something to race: the caller runs it (under the same
    # C0) before it gets here.
    #
    # Probed BEFORE the destination type gate on purpose. Rows 2, 5 and 6 of
    # an interrupted swap leave ``dst`` ABSENT, so a caller who skipped
    # recovery would otherwise be told the destination is unusable (ENOENT)
    # when the accurate diagnosis — and the actionable one — is that a swap
    # is pending. The misleading message is exactly the one a caller who got
    # the order wrong would have hit.
    pending = _find_marker(dst.parent, dst.name)
    if pending is not None:
        # SwapRecoveryError rather than ValueError: this is not a caller
        # mistake to fix in code, it is the recovery-pending condition every
        # surface already knows how to report — and it is the same type
        # ``_find_marker`` raises when it finds two.
        raise SwapRecoveryError(
            errno.EBUSY,
            f"a swap for {dst.name!r} is already pending ({pending.name}); "
            "recover it before starting another",
            str(dst),
            retained=(pending,),
        )

    # BOTH ends, not just staging. A regular file, symlink or device node at
    # ``dst`` would otherwise be moved aside and replaced, and the function
    # would report success for an operation its own contract calls a directory
    # replacement — with a symlink, it would move the LINK and leave the tree
    # it pointed at silently untouched. The recovery machine already refuses
    # every wrong type (:func:`_present_dir`); the forward path must agree, or
    # the two disagree about what this transaction is even operating on.
    _require_dir(staging, "staging")
    _require_dir(dst, "destination")

    suffix = f"{match.group('pid')}-{match.group('rand')}"
    paths = _swap_paths(dst.parent, dst.name, suffix)

    try:
        _write_marker(paths)
    except OSError:
        # Nothing claims the staging tree — the write is atomic, so a failure
        # means no marker was published — and leaving an unclaimed tree behind
        # would collide with the suffix a retry allocates.
        _rmtree_quietly(paths.staging, "unclaimed staging tree")
        raise
    try:
        rename_no_replace(paths.dst, paths.old)  # rename 1 — EXCLUSIVE
    except OSError:
        _unwind_rename1(paths)
        raise
    fsync_dir(paths.root)
    try:
        rename_no_replace(paths.staging, paths.dst)  # rename 2 — THE COMMIT POINT
    except OSError as promote_exc:
        _unwind_rename2(paths, promote_exc)
        raise
    _post_commit_cleanup(paths)


# ── Recovery state machine (§5) ───────────────────────────────────────


def _present_dir(path: Path) -> bool:
    """Whether *path* exists as a non-symlink directory; wrong types fail closed.

    Classifying by existence alone would let the forwarding rows rename a
    regular file, a symlink or a device node into the canonical position —
    exactly the posture the marker validation establishes everywhere else. So
    anything present that is not a plain directory raises, and because every
    probe runs before any mutation, that refusal costs nothing on disk.
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise SwapRecoveryError(
            errno.EBUSY, f"swap transient is unstattable ({exc})", str(path)
        ) from exc
    if stat.S_ISDIR(st.st_mode):
        return True
    raise SwapRecoveryError(
        errno.EBUSY,
        "swap transient exists but is not a directory (symlinks and special files are refused)",
        str(path),
    )


def _rename_recovery(src: Path, dst: Path) -> None:
    """Exclusive rename on a recovery path; a recreated destination fails closed."""
    try:
        rename_no_replace(src, dst)
    except OSError as exc:
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY):
            raise SwapForeignDestination(
                errno.EBUSY,
                f"'{dst}' was recreated by a non-gateway writer while recovering the swap; "
                f"refusing to clobber it. The tree this recovery would have restored is at '{src}'",
                str(dst),
                retained=(src, dst),
            ) from exc
        raise SwapRecoveryError(
            errno.EBUSY, f"swap recovery rename failed ({exc})", str(dst)
        ) from exc


def recover_pending_swaps(root: Path, name: str) -> bool:
    """Resolve a leftover swap transaction for *name* under *root*.

    Returns ``True`` when a marker was found and resolved (or when there was
    nothing left to do but drop a stale marker), ``False`` when there was no
    marker at all. Requires C0 for *name*, held by the caller and never
    re-acquired here — every call site is already inside the lock.

    A normal return therefore MEANS "resolved": the marker is gone. If it
    could not be removed the function raises rather than returning, because
    this runs as the prelude every canonical writer trusts, and a writer that
    proceeds against a surviving marker leaves the next recovery run
    classifying a state that marker no longer describes.

    Classification uses only EXISTENCE (``D`` = ``dst``, ``O`` = ``old``,
    ``T`` = ``staging``), never content: a content check would reintroduce a
    time-of-check/time-of-use window inside the very gap this machine exists to
    close. All three probes run before any mutation.

    ====  =  =  =  ==============================================  ================================
    row   D  O  T  interpretation                                   action
    ====  =  =  =  ==============================================  ================================
    1     ✓  –  ✓  crashed before rename 1; ``dst`` is ORIGINAL      drop staging
    2     –  ✓  ✓  crashed between the renames                       forward: staging → dst
    3     ✓  ✓  –  rename 2 done, cleanup pending                    drop old
    4     ✓  ✓  ✓  AMBIGUOUS — see below                             fail closed, touch nothing
    5     –  ✓  –  dst and staging both gone                         roll back: old → dst
    6     –  –  ✓  old externally removed after rename 1             forward: staging → dst
    7     ✓  –  –  complete; only the marker unlink was lost         drop the marker
    8     –  –  –  nothing to recover                                drop the marker
    ====  =  =  =  ==============================================  ================================

    **Row 2 forwards rather than rolling back.** The marker is written only
    after ``staging`` is complete, so marker-present implies staging-complete,
    and the pre-image is already preserved as a version snapshot by the
    caller's transaction — completing forward loses nothing a rollback would
    save.

    **Row 5 rolls back.** Both histories that produce it (``staging`` vanished
    before rename 2, or ``dst`` was externally removed after it) are repaired
    by restoring ``old``: it is the known pre-image, and the promoted tree's
    history entries were hardlinks to the very inodes that moved into ``old``,
    so nothing is lost. Stranding the canonical to preserve a distinction the
    filesystem cannot report is the worse trade.

    **Row 6 forwards.** ``staging`` is complete, nothing is at ``dst``, and
    there is nothing left to roll back to. (The tempting second history —
    "rename 2 succeeded, then both ``dst`` and ``old`` were removed" — is
    impossible: a successful rename 2 consumes ``staging``, which lands on row
    8.)

    **Row 4's provenance is genuinely ambiguous and the message says so.** The
    exclusive rename 1 rules out *adopting* a foreign ``old``, but not the
    sequence "a foreign ``old`` already existed → rename 1 raised → the
    process died before unwinding", which produces the identical shape with
    ``dst`` as the original rather than ``old``. The two readings disagree
    about which tree to restore and nothing on disk distinguishes them, so
    both paths are named and neither is claimed to be authoritative — asserting
    one would talk an operator into deleting the good tree.

    :raises InvalidNameError: *name* is not a valid artifact identifier. This
        is a ``ValueError``, **not** an ``OSError``, so it does not travel the
        path a caller uses to funnel this module's failures into a typed
        per-item skip — and that is deliberate. *name* is expected to come from
        an artifact the caller already resolved, not from a raw directory
        listing, so an invalid one is a programming error rather than a
        per-item outcome. A future caller that does iterate on-disk entries
        must decide explicitly whether to skip such an entry; it must not
        inherit that decision by accident.
    :raises SwapRecoveryError: a tampered/ambiguous marker or a wrong-type
        transient — nothing is mutated; or a failed recovery rename or marker
        removal — the row's own action may have completed, leaving a safe,
        still-marked state a later run resolves.
    :raises SwapForeignDestination: row 4, or a destination recreated mid-recovery.
    """
    validate_name(name, kind="artifact name")  # see :func:`_swap_paths`
    if not root.is_dir():
        return False
    marker = _find_marker(root, name)
    if marker is None:
        return False
    paths = _load_marker(marker, root, name)

    # Every probe BEFORE every mutation, so a wrong-type entry can never be
    # discovered after an earlier row action already deleted something.
    state = (_present_dir(paths.dst), _present_dir(paths.old), _present_dir(paths.staging))

    match state:
        case (True, False, True):  # row 1 — crashed before rename 1
            logger.warning("swap recovery %s: discarding staging tree %s", name, paths.staging)
            # Deliberately the reverse of rows 2/3, which clear the marker
            # first: here the marker must outlive the deletion, because a crash
            # in between leaves ``dst`` alone with a live marker — row 7, which
            # simply drops it. Clearing first would leave an UNMARKED staging
            # tree instead. Nothing irreplaceable is at stake either way (``dst``
            # is the original), but the ordering is chosen, not accidental.
            _rmtree_quietly(paths.staging, "staging tree")
            _clear_marker_or_refuse(paths)
        case (False, True, True):  # row 2 — crashed between the renames
            logger.warning("swap recovery %s: completing the promotion of %s", name, paths.staging)
            _rename_recovery(paths.staging, paths.dst)
            _clear_marker_or_refuse(paths)
            _rmtree_quietly(paths.old, "move-aside tree")
        case (True, True, False):  # row 3 — rename 2 done, cleanup pending
            logger.warning("swap recovery %s: finishing cleanup of %s", name, paths.old)
            _clear_marker_or_refuse(paths)
            _rmtree_quietly(paths.old, "move-aside tree")
        case (True, True, True):  # row 4 — fail closed, provenance ambiguous
            raise SwapForeignDestination(
                errno.EBUSY,
                # Written to SURVIVE the wire, which took two changes (PR
                # review). Every interpolated path is QUOTED, because the
                # web/MCP redactors replace a path run with ``<path>`` and
                # their segment class includes spaces — an unquoted path
                # mid-sentence swallows everything after it. And the
                # instruction comes BEFORE the paths, because those redactors
                # also truncate at 200 characters: the earlier wording spent
                # its budget explaining the two readings and lost "inspect
                # both by hand" off the end, on every surface, which is the
                # entire point of the refusal. The explanation is what gets
                # cut now, and it is the part a reader can do without.
                f"an interrupted directory swap left two candidate trees for {name!r} and which "
                f"is the original is AMBIGUOUS — inspect both by hand; nothing was changed. "
                f"Present: '{paths.dst}'; moved aside: '{paths.old}'",
                str(paths.dst),
                retained=(paths.marker, paths.dst, paths.old, paths.staging),
            )
        case (False, True, False):  # row 5 — roll back
            logger.warning("swap recovery %s: rolling back to %s", name, paths.old)
            _rename_recovery(paths.old, paths.dst)
            _clear_marker_or_refuse(paths)
        case (False, False, True):  # row 6 — old externally removed; forward
            logger.warning(
                "swap recovery %s: promoting %s (no pre-image left)", name, paths.staging
            )
            _rename_recovery(paths.staging, paths.dst)
            _clear_marker_or_refuse(paths)
        case (_, False, False):  # rows 7 and 8 — nothing to move; the marker outlived it
            logger.debug("swap recovery %s: dropping a stale marker", name)
            _clear_marker_or_refuse(paths)
    return True
