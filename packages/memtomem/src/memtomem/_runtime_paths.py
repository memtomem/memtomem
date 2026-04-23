"""Runtime-state path resolution.

Runtime files (pid files, locks, sockets) belong on ``$XDG_RUNTIME_DIR``
when the platform provides one — the kernel auto-cleans them at logout,
no stale artifacts survive a reboot, and they never mingle with the
user's persistent data under ``~/.memtomem/``.

Resolution order:

1. ``$XDG_RUNTIME_DIR/memtomem`` — Linux + systemd (and any other OS that
   exports the var). Per-user, ``tmpfs``-backed, kernel-managed lifecycle.
   Accepted only if the base is a real directory (not a symlink), owned
   by the effective uid, and has mode ``0o700`` (no group/world bits).
2. ``{tempfile.gettempdir()}/memtomem-{uid}`` — macOS (where
   ``gettempdir()`` resolves to a per-user ``/var/folders/.../T/`` already)
   and Linux without systemd. ``uid`` suffix disambiguates a shared
   ``/tmp``; ``mode=0o700`` at creation keeps it private to the user.

Security posture for the runtime directory itself:

- Never follow a symlink — an attacker on a shared ``/tmp`` could
  pre-create ``memtomem-{uid}`` as a link into the user's home, and a
  naive ``mkdir`` would silently no-op through it. ``os.stat`` with
  ``follow_symlinks=False`` catches this.
- Refuse any pre-existing directory not owned by the effective uid or
  not at mode ``0o700``. This trades convenience for a predictable
  contract: a ``root``-owned leftover from ``sudo mm …`` or a
  mode-degraded dir raises :class:`PermissionError` with a remediation
  hint instead of silently writing the pid file into a world-readable
  directory. The only fix is ``rm -rf`` the runtime dir and retry.
"""

from __future__ import annotations

import os
import stat
import tempfile
from pathlib import Path


def _is_safe_dir(path: Path) -> bool:
    """Return True iff ``path`` is a regular directory, owner-matches the
    effective uid, and has no group/world permission bits.

    Used by both the ``$XDG_RUNTIME_DIR`` gate (where we silently fall
    through to the tempdir form on failure) and the ``ensure`` path
    (where failure becomes a :class:`PermissionError`). ``lstat``-style
    semantics — we reject a symlink outright, never its target.
    """
    try:
        st = os.stat(path, follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISDIR(st.st_mode):
        return False
    if stat.S_IMODE(st.st_mode) & 0o077:
        return False
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        return False
    return True


def runtime_dir() -> Path:
    """Return the memtomem runtime directory path *without* creating it.

    See module docstring for resolution order. Use :func:`ensure_runtime_dir`
    when the directory needs to exist (e.g. opening a pid file for write);
    the plain form is safe to call during read-only introspection such as
    the uninstall inventory walk, which must not leave behind an empty dir.

    The ``$XDG_RUNTIME_DIR`` branch is gated on :func:`_is_safe_dir` —
    a misconfigured export (``XDG_RUNTIME_DIR=/tmp``, or a user-created
    symlink) silently falls through to the tempdir form so we never
    place the pid file in a world-readable location just because the
    environment was wrong.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg and _is_safe_dir(Path(xdg)):
        return Path(xdg) / "memtomem"

    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    return Path(tempfile.gettempdir()) / f"memtomem-{uid}"


def ensure_runtime_dir() -> Path:
    """Return the runtime directory, creating it with ``mode=0o700`` if missing.

    A pre-existing directory is validated: symlink, wrong owner, or any
    group/world bit set raises :class:`PermissionError` with a
    ``rm -rf <path>`` hint. We never ``chmod`` an existing directory
    (silent permission changes would hide the underlying misconfiguration
    — and bypass any audit a sysadmin might run against the parent).

    On creation we ``chmod`` explicitly to ``0o700`` as a belt-and-suspenders
    fix for exotic ``umask`` values (e.g. ``umask 0o177`` would clear the
    owner-execute bit supplied to ``mkdir``, leaving an unusable directory
    on silent success).
    """
    target = runtime_dir()
    try:
        st = os.stat(target, follow_symlinks=False)
    except FileNotFoundError:
        st = None
    except OSError as exc:
        raise PermissionError(
            f"runtime dir {target}: cannot stat ({exc}). Remove it and retry."
        ) from exc

    if st is not None:
        if stat.S_ISLNK(st.st_mode):
            raise PermissionError(
                f"runtime dir {target} is a symlink; refusing to follow. Remove it: rm -f {target}"
            )
        if not stat.S_ISDIR(st.st_mode):
            raise PermissionError(
                f"runtime dir {target} exists but is not a directory. Remove it: rm -f {target}"
            )
        if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
            raise PermissionError(
                f"runtime dir {target} is owned by uid {st.st_uid} "
                f"(expected {os.geteuid()}). Remove it and retry: rm -rf {target}"
            )
        unsafe = stat.S_IMODE(st.st_mode) & 0o077
        if unsafe:
            raise PermissionError(
                f"runtime dir {target} has unsafe permissions "
                f"0o{stat.S_IMODE(st.st_mode):o} (expected 0o700, "
                f"group/world bits: 0o{unsafe:o}). "
                f"Remove it and retry: rm -rf {target}"
            )
        return target

    # Missing — create with 0o700, then chmod explicitly to neutralize any
    # umask that would have masked the mode bits. Both calls are racy if
    # another process interposes, but the worst case is a
    # FileExistsError → re-validate via recursion.
    try:
        target.mkdir(mode=0o700, exist_ok=False)
    except FileExistsError:
        return ensure_runtime_dir()
    try:
        os.chmod(target, 0o700)
    except OSError:
        pass
    return target


def server_pid_path() -> Path:
    """Return the path to ``memtomem-server``'s pid / flock file.

    Does not create the parent directory — callers that intend to open the
    path for write should go through :func:`ensure_runtime_dir` first.
    """
    return runtime_dir() / "server.pid"


_LEGACY_PID_NAME = ".server.pid"


def legacy_server_pid_path() -> Path:
    """Return the pre-relocation pid file path (``~/.memtomem/.server.pid``).

    Kept as a target for backward-compat probes during the transition
    period. Both :mod:`memtomem.server` (startup mutual-exclusion against
    a pre-#412 server still running at the legacy location) and
    :mod:`memtomem.cli.uninstall_cmd` (mixed-version refusal) consult
    this path.

    ``Path.home()`` is evaluated every call so tests that monkeypatch
    ``HOME`` get the isolated path — import-time binding would capture
    the developer's real home and leak across the fixture.
    """
    return Path.home() / ".memtomem" / _LEGACY_PID_NAME
