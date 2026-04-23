"""Runtime-state path resolution.

Runtime files (pid files, locks, sockets) belong on ``$XDG_RUNTIME_DIR``
when the platform provides one — the kernel auto-cleans them at logout,
no stale artifacts survive a reboot, and they never mingle with the
user's persistent data under ``~/.memtomem/``.

Resolution order:

1. ``$XDG_RUNTIME_DIR/memtomem`` — Linux + systemd (and any other OS that
   exports the var). Per-user, ``tmpfs``-backed, kernel-managed lifecycle.
2. ``{tempfile.gettempdir()}/memtomem-{uid}`` — macOS (where
   ``gettempdir()`` resolves to a per-user ``/var/folders/.../T/`` already)
   and Linux without systemd. ``uid`` suffix disambiguates a shared
   ``/tmp``; ``mode=0o700`` at creation keeps it private to the user.

The directory is created on first access. We never try to ``chmod`` an
existing directory — if a user ran memtomem as root once and left behind
a ``root``-owned runtime dir, fixing it silently would be worse than
letting the open fail loudly.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path


def runtime_dir() -> Path:
    """Return the memtomem runtime directory path *without* creating it.

    See module docstring for resolution order. Use :func:`ensure_runtime_dir`
    when the directory needs to exist (e.g. opening a pid file for write);
    the plain form is safe to call during read-only introspection such as
    the uninstall inventory walk, which must not leave behind an empty dir.
    """
    xdg = os.environ.get("XDG_RUNTIME_DIR", "")
    if xdg:
        base = Path(xdg)
        if base.is_dir():
            return base / "memtomem"

    uid = os.geteuid() if hasattr(os, "geteuid") else 0
    return Path(tempfile.gettempdir()) / f"memtomem-{uid}"


def ensure_runtime_dir() -> Path:
    """Return the runtime directory, creating it with ``mode=0o700`` if missing.

    Never touches permissions on an existing directory; a ``root``-owned
    leftover from a prior ``sudo`` invocation is surfaced by the caller's
    ``open()`` rather than silently adjusted here.
    """
    target = runtime_dir()
    target.mkdir(mode=0o700, exist_ok=True)
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

    Kept as a target for backward-compat probes in the uninstall CLI
    during the transition period. A mixed-version upgrade (old server
    running, new uninstall) still needs to see the old location to
    refuse correctly.

    ``Path.home()`` is evaluated every call so tests that monkeypatch
    ``HOME`` get the isolated path — import-time binding would capture
    the developer's real home and leak across the fixture.
    """
    return Path.home() / ".memtomem" / _LEGACY_PID_NAME
