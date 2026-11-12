"""Catch tests that leave memtomem-managed user settings changed (#1892).

The incident behind #1892 was reached indirectly: a test called an HTTP route,
and production code resolved ``Path.home()`` before writing the developer's
real ``~/.claude/settings.json``.  A source scanner cannot see that call chain.

This module deliberately protects only the small user-scope files returned by
``SETTINGS_GENERATORS`` (currently Claude, Codex, Gemini, and Kimi settings).
It fingerprints those files before and after every normally completed test.
Creation, deletion, and byte-content changes fail the test.  A byte-identical
rewrite, or a write the test restores before teardown, does not, except in
``strict`` mode, which also fails a test seen attempting to open one of these
files for writing, or to rename, remove or truncate it, whatever the bytes end
up as (#2302).  The audit event fires before the operation, so an attempt that
then fails counts too.  That check counts only operations that name the file
itself: one
that renames an ancestor directory is used to attribute a change, not as a
verdict, because it cannot tell an empty directory from one holding the file.
So a test that moves ``~/.claude`` aside, writes or restores the settings file
under the temporary name, and moves the directory back passes ``strict`` when
the bytes end up unchanged; a change to them still fails it.
An ``open`` audit event carries no ``dir_fd``, so a relative name is read
against the cwd: a test whose cwd is the home directory and that opens
``.claude/settings.json`` relative to a descriptor pointing elsewhere fails
``strict`` although only its own file changed.  Exempting relative opens
instead would let every cwd-relative write pass silently; this false positive
is loud, and an absolute path avoids it.

A byte difference alone does not say *this test wrote the file* (#2355).  On a
developer machine these paths have another owner — the surrounding editor
session writes ``~/.claude/settings.json`` routinely — so the digest comparison
alone blamed whichever test happened to be in flight.  ``WriteWitness`` records
the writes this pytest process makes to the watched paths, so a net change can
be *attributed* before a test is accused.  A change with a matching in-process
write still fails that test; a change with none is reported as an observation
about the file rather than as a verdict about the test, and only fails in
``strict`` mode (which CI sets, having no other owner for those files).

This is a regression tripwire on a trusted local filesystem, not a general
filesystem-integrity library.  It does not walk home-directory trees, recover
after SIGKILL, detect a write that is fully restored before teardown outside
``strict`` mode (or, in it, one made through a shape the witness cannot see),
or make an adversarial pathname-swap guarantee.  Existing final-component symlinks and
reparse points are never fingerprinted through: ``strict`` refuses to run when
a settings target is one at session start, and ``on`` leaves that target out of
the content comparison and checks it by write observation only (#3019), so a
dotfiles symlink does not cost the guard on every other target.  For such a
target the witness's blind spots below are detection gaps, not attribution
gaps: with no fingerprint, an unseen write is not reported at all.  A
symlink's destination is watched under the one spelling ``realpath`` gave it
when the session armed, so a write to it under another spelling, or to a new
destination after the link is retargeted, is not seen either.  A link replaced
unseen stays degraded for the rest of the session, because degradation is
decided once, when the session arms.  The witness sees only audit
events raised in *this* process: a subprocess's writes, ``mmap`` stores, writes
through a descriptor opened before the hook armed, and writes aimed at a
relative pathname under a ``dir_fd`` all stay invisible to it, so
"unattributed" means "this process was not observed writing it", never
"nothing in this process wrote it".  Pathnames are compared with
``os.path.normcase``, which is identity on POSIX: a write through a
differently-cased spelling of the same file is attributed on Windows and not on
a case-insensitive macOS volume.  Keeping that contract narrow is what makes
the guard portable across the mandatory Windows, macOS, and Linux test jobs.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
import sys
import threading
import traceback
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

#: Per-invocation mode switch.  There is intentionally no pytest marker or
#: allowlist for *attributed* changes: tests do not legitimately leave these
#: real user files changed.  Unattributed changes are a statement about the
#: file, not about the test, and ``strict`` is how a run with no third-party
#: owner (CI) asks for them to fail anyway.
DISABLE_ENV = "MEMTOMEM_TEST_HOME_GUARD"

#: Settings files are small.  Bound every read so an unexpectedly huge path
#: cannot turn a per-test tripwire into unbounded I/O.
MAX_CONFIG_BYTES = 8 * 1024 * 1024


class HomeGuardError(RuntimeError):
    """The guard could not establish a trustworthy baseline."""


GuardMode = Literal["off", "on", "strict"]


def guard_mode(env: dict[str, str] | None = None) -> GuardMode:
    """Return the guard mode for this pytest invocation.

    ``strict`` additionally fails a test whose window contains a net change
    this process was not observed making, and a test observed attempting to
    open a watched file for writing, or to rename, remove or truncate it, even
    with no net change (#2302).  Any other non-off value is ``on``, which keeps
    the historical meaning of "the guard is armed".
    """
    raw = (env if env is not None else os.environ).get(DISABLE_ENV, "").strip().lower()
    if raw in {"off", "0", "false", "no"}:
        return "off"
    if raw == "strict":
        return "strict"
    return "on"


def guard_enabled(env: dict[str, str] | None = None) -> bool:
    """Return whether the guard is enabled for this pytest invocation."""
    return guard_mode(env) != "off"


@contextmanager
def as_home(home: Path) -> Iterator[None]:
    """Temporarily make ``Path.home()`` resolve to ``home`` on every OS."""
    previous = {key: os.environ.get(key) for key in ("HOME", "USERPROFILE")}
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _lexical_absolute(path: Path) -> Path:
    """Normalize ``.``/``..`` without resolving symlinks or junctions."""
    return Path(os.path.abspath(os.fspath(path)))


def derive_targets(home: Path) -> tuple[Path, ...]:
    """Ask production for every user-scope settings write target.

    Paths remain lexical.  Resolving them here would erase the identity of a
    final symlink before the arm-time policy can reject it.  A future settings
    generator is included automatically, but a generator that escapes the
    supplied home fails closed.
    """
    from memtomem.context.settings import SETTINGS_GENERATORS

    lexical_home = _lexical_absolute(home)
    sentinel_project = lexical_home / "__home_guard_no_such_project__"
    targets: set[Path] = set()

    with as_home(lexical_home):
        for generator in SETTINGS_GENERATORS.values():
            target = generator.target_file(sentinel_project, "user")
            if target is None:
                continue
            candidate = _lexical_absolute(Path(target))
            if not candidate.is_relative_to(lexical_home):
                raise HomeGuardError(
                    f"home guard target escapes the real home: {candidate}. Refusing to arm."
                )
            targets.add(candidate)

    if not targets:
        raise HomeGuardError(
            "home guard derivation produced no settings targets. Refusing to arm "
            "because an empty watched set looks identical to a clean test run."
        )
    return tuple(sorted(targets))


FingerprintState = Literal["missing", "regular", "unsafe"]


@dataclass(frozen=True)
class FileFingerprint:
    """Content identity or an explicit state that cannot be safely watched."""

    state: FingerprintState
    digest: str = ""
    detail: str = ""


def _is_reparse_point(st: os.stat_result) -> bool:
    attributes = getattr(st, "st_file_attributes", 0)
    marker = getattr(stat_mod, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return bool(marker and attributes & marker)


def fingerprint(path: Path, *, max_bytes: int = MAX_CONFIG_BYTES) -> FileFingerprint:
    """Return a bounded content fingerprint without intentionally following links.

    The pathname is inspected with ``lstat`` and, where the platform exposes
    them, reparse attributes.  The descriptor is then classified again with
    ``fstat`` and read up to ``max_bytes + 1``.  ``O_NOFOLLOW``/``O_NONBLOCK``
    are used when available, but the contract is intentionally not an atomic
    hostile-filesystem guarantee on platforms that do not provide those flags.
    """
    try:
        lst = path.lstat()
    except FileNotFoundError:
        return FileFingerprint("missing")
    except (OSError, ValueError) as exc:
        return FileFingerprint("unsafe", detail=f"cannot inspect final entry ({exc})")

    if stat_mod.S_ISLNK(lst.st_mode) or _is_reparse_point(lst):
        return FileFingerprint("unsafe", detail="final entry is a symlink or reparse point")
    if not stat_mod.S_ISREG(lst.st_mode):
        return FileFingerprint("unsafe", detail="final entry is not a regular file")
    if lst.st_size > max_bytes:
        return FileFingerprint(
            "unsafe", detail=f"file is {lst.st_size} bytes; limit is {max_bytes}"
        )

    flags = os.O_RDONLY
    flags |= getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        fd = os.open(path, flags)
    except FileNotFoundError:
        return FileFingerprint("missing")
    except OSError as exc:
        return FileFingerprint("unsafe", detail=f"cannot open regular file ({exc})")

    try:
        try:
            opened = os.fstat(fd)
        except OSError as exc:
            return FileFingerprint("unsafe", detail=f"cannot inspect open file ({exc})")
        if not stat_mod.S_ISREG(opened.st_mode):
            return FileFingerprint("unsafe", detail="opened entry is not a regular file")
        if opened.st_size > max_bytes:
            return FileFingerprint(
                "unsafe", detail=f"file is {opened.st_size} bytes; limit is {max_bytes}"
            )

        digest = hashlib.sha256()
        total = 0
        while True:
            try:
                block = os.read(fd, min(1024 * 1024, max_bytes + 1 - total))
            except OSError as exc:
                return FileFingerprint("unsafe", detail=f"cannot read regular file ({exc})")
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                return FileFingerprint(
                    "unsafe", detail=f"file grew beyond the {max_bytes}-byte limit while read"
                )
            digest.update(block)
        return FileFingerprint("regular", digest=digest.hexdigest())
    finally:
        os.close(fd)


def snapshot_files(paths: tuple[Path, ...]) -> dict[str, FileFingerprint]:
    """Fingerprint all watched files on every call; there is no metadata fast path."""
    return {str(path): fingerprint(path) for path in paths}


@dataclass(frozen=True)
class DegradedTarget:
    """A settings target that was unsafe to fingerprint when the session armed.

    ``aliases`` are the witness keys whose writes count as writes to it: the
    target's own pathname and, for a symlink, the file it named at that moment.
    The second matters because a settings writer that keeps the link intact
    replaces the link's destination, not the link, and the witness compares
    pathnames rather than files.  Naming the destination reads nothing through
    the link; its content is still never compared.
    """

    path: str
    reason: str
    aliases: tuple[str, ...]


def _link_destination(path: Path) -> str | None:
    """Return the normalised destination of a symlink at ``path``, if it has one."""
    try:
        if not os.path.islink(path):
            return None
        return normalise_target(os.path.realpath(path))
    except (OSError, ValueError):
        return None


def partition_baseline(
    targets: tuple[Path, ...], snapshot: dict[str, FileFingerprint], mode: GuardMode
) -> tuple[tuple[Path, ...], tuple[DegradedTarget, ...]]:
    """Split the session's targets into fingerprinted ones and degraded ones.

    ``strict`` keeps failing closed: any unsafe target refuses the session.
    Otherwise an unsafe target is degraded instead of refusing the session —
    the usual cause is a settings file kept in a dotfiles repository and
    symlinked into place, and refusing would leave ``off`` as the only way to
    run, which unguards every other target too.  The decision is made once,
    here; a target that turns unsafe later is a change, not a degradation.
    """
    if mode == "strict":
        require_armable(snapshot)
        return targets, ()
    watched: list[Path] = []
    degraded: list[DegradedTarget] = []
    for target in targets:
        value = snapshot[str(target)]
        if value.state != "unsafe":
            watched.append(target)
            continue
        aliases = [normalise_target(target)]
        destination = _link_destination(target)
        if destination is not None and destination not in aliases:
            aliases.append(destination)
        degraded.append(DegradedTarget(str(target), value.detail, tuple(aliases)))
    return tuple(watched), tuple(degraded)


def require_armable(snapshot: dict[str, FileFingerprint]) -> None:
    """Reject a baseline containing anything other than missing/regular files."""
    unsafe = [(path, value.detail) for path, value in snapshot.items() if value.state == "unsafe"]
    if not unsafe:
        return
    details = "\n".join(f"  {path} — {reason}" for path, reason in unsafe)
    raise HomeGuardError(
        "home guard cannot safely watch the current settings target(s):\n"
        f"{details}\n"
        f"Fix the path or set {DISABLE_ENV}=off for this invocation."
    )


# -- in-process write witness ----------------------------------------------

#: Audit events that can change one of the watched pathnames.  ``os.replace``
#: raises ``os.rename``; ``os.unlink`` raises ``os.remove``.  The production
#: settings writer is ``tempfile.mkstemp`` + ``os.replace``, so the watched
#: path is reached by *rename* and never by an ``open`` of its own name.
_WATCHED_EVENTS = frozenset({"open", "os.rename", "os.remove", "os.truncate"})

#: ``open`` fires for reads too — including this module's own fingerprint
#: reads.  Only the write-capable flag combinations are of interest.
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | getattr(os, "O_APPEND", 0)

#: Label used for a write observed outside any test's window (collection,
#: session fixtures, or the gap between two tests).
BETWEEN_TESTS = "<between tests>"

#: Bound on the captured stack.  A witness hit is rare; the bound only keeps a
#: pathological recursion from turning one observation into megabytes.
MAX_STACK_FRAMES = 12


def normalise_target(path: Path | str) -> str:
    """Return the comparison key for a watched pathname.

    Lexical, then ``normcase`` so a case-insensitive filesystem cannot dodge
    the witness through a differently-cased spelling of the same file.
    """
    return os.path.normcase(os.fspath(_lexical_absolute(Path(os.fspath(path)))))


@dataclass(frozen=True)
class WriteObservation:
    """One observed in-process write-capable operation on a watched path.

    An audit event is raised *before* the operation runs, so this records an
    attempt: an ``os.open`` with write flags that is closed unused, or an
    ``os.remove`` that then raises, is recorded too.  ``direct`` is False when
    the operation named an ancestor directory (a rename of ``~/.claude``) and
    the watched path was projected from it, whether or not a file existed
    there.
    """

    event: str
    path: str
    window: str
    thread: str
    stack: tuple[str, ...] = ()
    direct: bool = True

    def __str__(self) -> str:
        return f"{self.path} — {self.event} on thread {self.thread}"


def _resolvable(path: object, dir_fd: object) -> bool:
    """Return whether ``path`` alone names the file the call will act on.

    A ``dir_fd`` is ignored by the OS for an absolute path, so only a relative
    pathname passed with a directory descriptor is unresolvable from here.
    """
    if dir_fd is None or dir_fd == -1:
        return True
    try:
        return os.path.isabs(os.fsdecode(path))  # type: ignore[arg-type]
    except TypeError:
        return False


def _candidate_paths(event: str, args: tuple[object, ...]) -> tuple[object, ...]:
    """Return every pathname whose content or existence this event changes."""
    if event == "open":
        # (path, mode, flags).  ``open()`` passes a string mode and ``os.open``
        # passes None, so the flags integer is the only portable signal.
        if len(args) < 3 or not isinstance(args[2], int) or not args[2] & _WRITE_FLAGS:
            return ()
        return (args[0],)
    if event == "os.rename":
        # (src, dst, src_dir_fd, dst_dir_fd).  Both endpoints change: the
        # destination gains the source's content, and the source *pathname*
        # stops existing — renaming a watched file out of the way is a write
        # to it just as much as overwriting it is.
        if len(args) < 2:
            return ()
        src_dir_fd = args[2] if len(args) >= 3 else None
        dst_dir_fd = args[3] if len(args) >= 4 else None
        candidates = []
        if _resolvable(args[0], src_dir_fd):
            candidates.append(args[0])
        if _resolvable(args[1], dst_dir_fd):
            candidates.append(args[1])
        return tuple(candidates)
    # os.remove: (path, dir_fd).  os.truncate: (path, length) — a length is
    # not a descriptor, so it is never consulted here.
    if not args:
        return ()
    if event == "os.remove" and not _resolvable(args[0], args[1] if len(args) >= 2 else None):
        return ()
    return (args[0],)


class WriteWitness:
    """Record this process's writes to the watched settings paths.

    The audit hook runs on *every* audit event in the process, so it returns on
    a set-membership test before doing anything else, and returns again while
    no path is watched: normalising a candidate pathname is the expensive part
    and only a watched-event, non-empty-target call reaches it.  Any exception
    inside an audit hook propagates into the caller's ``open``/``rename``,
    which would turn a diagnostic into a test failure of its own, so the body
    is wrapped.

    An observation's window label is taken under the same lock that appends it,
    so a write racing a window boundary is labelled with the window that
    actually collects it — a late worker's write cannot be filed under the test
    that spawned it after that test's window has closed (#2211).
    """

    def __init__(self, targets: tuple[Path, ...]) -> None:
        self._lock = threading.Lock()
        self._targets: frozenset[str] = frozenset()
        self._window = BETWEEN_TESTS
        self._observations: list[WriteObservation] = []
        self._installed = False
        self.watch(targets)

    @property
    def targets(self) -> frozenset[str]:
        return self._targets

    @property
    def window(self) -> str:
        return self._window

    def watch(self, targets: tuple[Path, ...]) -> None:
        """Replace the watched set.

        The session guard sets this once, at construction.  It is a method
        rather than a constructor-only value because an audit hook cannot be
        uninstalled: this module's own pins need many different watched sets
        within one process and must not leak a hook per test to get them.
        """
        # Computed before the lock is taken: normalisation touches the path
        # machinery, and the hook takes this same non-reentrant lock.
        watched = frozenset(normalise_target(target) for target in targets)
        with self._lock:
            self._targets = watched

    def install(self) -> None:
        """Arm the audit hook.  Idempotent; audit hooks cannot be removed."""
        if self._installed:
            return
        self._installed = True
        sys.addaudithook(self._hook)

    def begin_window(self, nodeid: str) -> None:
        """Label subsequent observations and drop anything left over."""
        with self._lock:
            self._window = nodeid
            self._observations = []

    def drain(self) -> list[WriteObservation]:
        """Return and clear the observations recorded since ``begin_window``."""
        with self._lock:
            observed, self._observations = self._observations, []
            self._window = BETWEEN_TESTS
        return observed

    def _affected(self, candidate: object) -> tuple[tuple[str, bool], ...]:
        """Return ``(watched path, direct)`` for each path a change to ``candidate`` changes.

        Usually that is ``candidate`` itself (``direct``).  A rename also moves
        whole directories, and moving ``~/.claude`` aside removes the settings
        file inside it just as surely as unlinking the file does, so an
        endpoint that is an ancestor of a watched path stands in for it
        (not ``direct``).
        """
        key = normalise_target(os.fsdecode(candidate))  # type: ignore[arg-type]
        if key in self._targets:
            return ((key, True),)
        prefix = key + os.sep
        return tuple(
            (target, False) for target in sorted(self._targets) if target.startswith(prefix)
        )

    def _hook(self, event: str, args: tuple[object, ...]) -> None:
        if event not in _WATCHED_EVENTS:
            return
        try:
            targets = self._targets
            if not targets:
                return
            for candidate in _candidate_paths(event, args):
                if isinstance(candidate, int):
                    continue
                for key, direct in self._affected(candidate):
                    stack = tuple(
                        traceback.format_list(traceback.extract_stack(limit=MAX_STACK_FRAMES))
                    )
                    thread = threading.current_thread().name
                    # The window is read inside the lock that appends, so the
                    # label cannot name a window that has already been drained.
                    with self._lock:
                        self._observations.append(
                            WriteObservation(
                                event=event,
                                path=key,
                                window=self._window,
                                thread=thread,
                                stack=stack,
                                direct=direct,
                            )
                        )
        except Exception:  # pragma: no cover - defensive; see class docstring
            return


@dataclass(frozen=True)
class Violation:
    path: str
    kind: Literal["created", "deleted", "modified", "unsafe", "written"]
    detail: str

    def __str__(self) -> str:
        return f"{self.path} — {self.kind}: {self.detail}"


def diff_files(
    before: dict[str, FileFingerprint], after: dict[str, FileFingerprint]
) -> list[Violation]:
    """Report net changes without emitting file bytes or digest values."""
    violations: list[Violation] = []
    for path in sorted(set(before) | set(after)):
        old = before.get(path, FileFingerprint("missing"))
        new = after.get(path, FileFingerprint("missing"))
        if new.state == "unsafe":
            violations.append(Violation(path, "unsafe", new.detail))
        elif old.state == "unsafe":
            violations.append(Violation(path, "unsafe", old.detail))
        elif old.state == "missing" and new.state == "regular":
            violations.append(Violation(path, "created", "a settings file appeared"))
        elif old.state == "regular" and new.state == "missing":
            violations.append(Violation(path, "deleted", "the settings file disappeared"))
        elif old.state == "regular" and new.state == "regular" and old.digest != new.digest:
            violations.append(Violation(path, "modified", "byte content changed"))
    return violations


def classify(
    violations: list[Violation], observations: list[WriteObservation]
) -> tuple[list[Violation], list[Violation]]:
    """Split net changes by whether this process was seen writing that path.

    Returns ``(attributed, unattributed)``.  An observation attributes a
    violation only when it names the same path: a write to one watched file
    says nothing about a change to another.
    """
    written = {observation.path for observation in observations}
    attributed: list[Violation] = []
    unattributed: list[Violation] = []
    for violation in violations:
        target = attributed if normalise_target(violation.path) in written else unattributed
        target.append(violation)
    return attributed, unattributed


def degraded_writes(
    degraded: tuple[DegradedTarget, ...], observations: list[WriteObservation]
) -> list[Violation]:
    """Report each observed write to a degraded target or its link destination.

    A degraded target has no before/after fingerprint, so an observed write is
    the whole verdict: there is no net change to compare it against.  The
    violation names the pathname that was written, so the failure message can
    show the write site the witness recorded for it.
    """
    written = {observation.path for observation in observations}
    violations: list[Violation] = []
    for target in degraded:
        own = normalise_target(target.path)
        for alias in target.aliases:
            if alias not in written:
                continue
            where = (
                "a write was observed on this target"
                if alias == own
                else f"a write was observed on the file the symlink {target.path} points to"
            )
            violations.append(
                Violation(
                    alias,
                    "written",
                    f"{where}; its content is not compared because {target.reason}",
                )
            )
    return violations


def access_without_change(
    violations: list[Violation],
    observations: list[WriteObservation],
    degraded: tuple[DegradedTarget, ...],
) -> list[WriteObservation]:
    """Direct write-capable accesses to a watched path that left no net change (#2302).

    The digest comparison fails a test only for a net change, so a write the
    test restores before teardown, or a byte-identical rewrite, passes it.
    ``strict`` fails these too.  Only *direct* observations count: one
    projected from a rename of an ancestor directory cannot tell an empty
    directory from one holding the file, so it stays an attribution aid (see
    :func:`classify`) and is not a verdict on its own.  Paths that already
    have a violation, and degraded targets (handled by :func:`degraded_writes`),
    are left out so nothing is reported twice.
    """
    changed = {normalise_target(violation.path) for violation in violations}
    excluded = changed | {alias for target in degraded for alias in target.aliases}
    return [
        observation
        for observation in observations
        if observation.direct and observation.path not in excluded
    ]


def format_access_without_change(nodeid: str, accesses: list[WriteObservation]) -> str:
    """Describe an observed write-capable access attempt that left the bytes unchanged."""
    lines: list[str] = []
    for observation in accesses:
        lines.append(f"  {observation}, from:")
        lines.extend(
            f"    {frame}" for entry in observation.stack for frame in entry.rstrip().splitlines()
        )
    rendered = "\n".join(lines)
    return (
        f"{nodeid} was observed attempting write-capable access to real user "
        "settings outside its test sandbox (opening them for writing, or renaming, "
        "removing or truncating them; the audit event fires before the operation, "
        "so it may not have succeeded). Their bytes are unchanged at teardown, "
        "which this run is in strict mode to catch:\n"
        f"{rendered}\n"
        "Use tests.helpers.set_home(monkeypatch, tmp_path) before calling the "
        "production path that touches these files."
    )


def _witness_lines(violations: list[Violation], observations: list[WriteObservation]) -> list[str]:
    """Render the observed write sites for the given violations."""
    wanted = {normalise_target(violation.path) for violation in violations}
    lines: list[str] = []
    for observation in observations:
        if observation.path not in wanted:
            continue
        lines.append(f"  observed {observation.event} on thread {observation.thread}, from:")
        lines.extend(
            f"    {frame}" for entry in observation.stack for frame in entry.rstrip().splitlines()
        )
    return lines


def format_violations(
    nodeid: str, violations: list[Violation], observations: list[WriteObservation] | None = None
) -> str:
    """Format one actionable pytest failure without including file contents."""
    rendered = "\n".join(f"  {violation}" for violation in violations)
    witness = _witness_lines(violations, observations or [])
    trailer = ("\n" + "\n".join(witness)) if witness else ""
    return (
        f"{nodeid} changed real user settings outside its test sandbox:\n"
        f"{rendered}{trailer}\n"
        "Use tests.helpers.set_home(monkeypatch, tmp_path) before calling the "
        "production path that writes these files."
    )


def format_unattributed(nodeid: str, violations: list[Violation], mode: GuardMode) -> str:
    """Describe a change this process was never observed making.

    Deliberately says nothing about what the test should do differently: no
    in-process write to the path was seen, so naming the test as the writer —
    which is what the old message did — is the misattribution being fixed.
    """
    rendered = "\n".join(f"  {violation}" for violation in violations)
    tail = (
        "This run is in strict mode, where any change to these files fails."
        if mode == "strict"
        else f"Set {DISABLE_ENV}=strict to make this fail the test instead."
    )
    return (
        f"real user settings changed during {nodeid}, but no in-process write "
        "to them was observed:\n"
        f"{rendered}\n"
        "That is consistent with a concurrent writer that owns the file (an "
        "editor session, for instance) and also with a write this process made "
        "through a shape the witness cannot see: a subprocess, mmap, a "
        "descriptor opened before the hook armed, or a dir_fd-relative path.\n"
        f"{tail}"
    )
