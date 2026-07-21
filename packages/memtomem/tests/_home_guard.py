"""Catch tests that leave memtomem-managed user settings changed (#1892).

The incident behind #1892 was reached indirectly: a test called an HTTP route,
and production code resolved ``Path.home()`` before writing the developer's
real ``~/.claude/settings.json``.  A source scanner cannot see that call chain.

This module deliberately protects only the small user-scope files returned by
``SETTINGS_GENERATORS`` (currently Claude, Codex, Gemini, and Kimi settings).
It fingerprints those files before and after every normally completed test.
Creation, deletion, and byte-content changes fail the test; a byte-identical
rewrite does not.

This is a regression tripwire on a trusted local filesystem, not a general
filesystem-integrity library.  It does not walk home-directory trees, recover
after SIGKILL, detect a write that is fully restored before teardown, or make
an adversarial pathname-swap guarantee.  Existing final-component symlinks and
reparse points are refused rather than followed.  Keeping that contract narrow
is what makes the guard portable across the mandatory Windows, macOS, and Linux
test jobs.
"""

from __future__ import annotations

import hashlib
import os
import stat as stat_mod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Literal

#: Per-invocation escape hatch.  There is intentionally no pytest marker or
#: allowlist: tests do not legitimately leave these real user files changed.
DISABLE_ENV = "MEMTOMEM_TEST_HOME_GUARD"

#: Settings files are small.  Bound every read so an unexpectedly huge path
#: cannot turn a per-test tripwire into unbounded I/O.
MAX_CONFIG_BYTES = 8 * 1024 * 1024


class HomeGuardError(RuntimeError):
    """The guard could not establish a trustworthy baseline."""


def guard_enabled(env: dict[str, str] | None = None) -> bool:
    """Return whether the guard is enabled for this pytest invocation."""
    raw = (env if env is not None else os.environ).get(DISABLE_ENV, "")
    return raw.strip().lower() not in {"off", "0", "false", "no"}


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


def format_violations(nodeid: str, violations: list[Violation]) -> str:
    """Format one actionable pytest failure without including file contents."""
    rendered = "\n".join(f"  {violation}" for violation in violations)
    return (
        f"{nodeid} changed real user settings outside its test sandbox:\n"
        f"{rendered}\n"
        "Use tests.helpers.set_home(monkeypatch, tmp_path) before calling the "
        "production path that writes these files."
    )
