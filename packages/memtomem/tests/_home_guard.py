"""Catch tests that leave memtomem-managed user settings changed (#1892).

The incident behind #1892 was reached indirectly: a test called an HTTP route,
and production code resolved ``Path.home()`` before writing the developer's
real ``~/.claude/settings.json``.  A source scanner cannot see that call chain.

This module deliberately protects only the small user-scope files returned by
``SETTINGS_GENERATORS`` (currently Claude, Codex, Gemini, and Kimi settings).
It fingerprints those files before and after every normally completed test.
Creation, deletion, and byte-content changes fail the test; a byte-identical
rewrite does not.

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
after SIGKILL, detect a write that is fully restored before teardown, or make
an adversarial pathname-swap guarantee.  Existing final-component symlinks and
reparse points are refused rather than followed.  The witness sees only audit
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
    this process was not observed making.  Any other non-off value is ``on``,
    which keeps the historical meaning of "the guard is armed".
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
    """One observed in-process write to a watched path."""

    event: str
    path: str
    window: str
    thread: str
    stack: tuple[str, ...] = ()

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

    def _affected(self, candidate: object) -> tuple[str, ...]:
        """Return the watched paths a change to ``candidate`` would change.

        Usually that is ``candidate`` itself.  A rename also moves whole
        directories, and moving ``~/.claude`` aside removes the settings file
        inside it just as surely as unlinking the file does, so an endpoint
        that is an ancestor of a watched path stands in for it.
        """
        key = normalise_target(os.fsdecode(candidate))  # type: ignore[arg-type]
        if key in self._targets:
            return (key,)
        prefix = key + os.sep
        return tuple(target for target in sorted(self._targets) if target.startswith(prefix))

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
                for key in self._affected(candidate):
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
                            )
                        )
        except Exception:  # pragma: no cover - defensive; see class docstring
            return


@dataclass(frozen=True)
class Violation:
    path: str
    kind: Literal["created", "deleted", "modified", "unsafe"]
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
