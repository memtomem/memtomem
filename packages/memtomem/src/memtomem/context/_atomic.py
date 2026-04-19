"""Atomic write primitives for context-gateway fan-out targets.

A crash, SIGKILL, or OOM between the truncate and the flush of a plain
``Path.write_text`` leaves the target file empty or half-written — which, for
``~/.claude/settings.json`` or ``.claude/agents/<name>.md``, reloads on the
next runtime start as "no hooks / no agents configured". Every gateway write
site funnels through the helpers in this module so the worst a crash can do
is leave a ``.<name>.*.tmp`` sibling that the next successful write will
overwrite.

The pattern is ``tempfile.mkstemp`` in the same directory + ``os.replace``,
which is an atomic rename on POSIX and Windows.

Threat model is **accidental** (crash / kill), not adversarial — the
``context/`` package is the boundary where that hardening lives.

See also: :func:`memtomem.config._atomic_write_json`, which predates this
module and covers ``~/.memtomem/config.json`` specifically.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

__all__ = ["atomic_write_bytes", "atomic_write_text"]


def atomic_write_bytes(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Atomically write *data* to *path* with an explicit file mode.

    ``mode`` is applied via ``os.fchmod`` on the tempfile before the rename,
    so the result is independent of the process umask.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        os.fchmod(tmp_fd, mode)
        with os.fdopen(tmp_fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def atomic_write_text(
    path: Path,
    text: str,
    mode: int = 0o600,
    encoding: str = "utf-8",
) -> None:
    """Atomically write *text* to *path* with an explicit file mode."""
    atomic_write_bytes(path, text.encode(encoding), mode=mode)
