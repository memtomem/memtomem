"""Names and runtime paths of a shared daemon (#3381), importable without the server.

``memtomem.server`` registers every MCP tool when it is imported, so the CLI
commands that find, report and stop daemons read the names from here instead
of from :mod:`memtomem.server.daemon`.
"""

from __future__ import annotations

import re
from pathlib import Path

NAME_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,31}")

# In the ``server-*.pid`` family the global inventory enumerates, and never
# equal to a store's ``server-<16 hex>.pid``: the ``daemon-`` infix is not hex.
LIFETIME_LOCK_PREFIX = "server-daemon-"
LIFETIME_LOCK_SUFFIX = ".pid"
SOCKET_PREFIX = "daemon-"
SOCKET_SUFFIX = ".sock"
LOG_PREFIX = "daemon-"
LOG_SUFFIX = ".log"


def socket_path(runtime_dir: Path, name: str) -> Path:
    return runtime_dir / f"{SOCKET_PREFIX}{name}{SOCKET_SUFFIX}"


def lifetime_lock_path(runtime_dir: Path, name: str) -> Path:
    return runtime_dir / f"{LIFETIME_LOCK_PREFIX}{name}{LIFETIME_LOCK_SUFFIX}"


def log_path(runtime_dir: Path, name: str) -> Path:
    """Where a daemon started by a bridge writes its log."""
    return runtime_dir / f"{LOG_PREFIX}{name}{LOG_SUFFIX}"


def _name_between(filename: str, prefix: str, suffix: str) -> str | None:
    if not (filename.startswith(prefix) and filename.endswith(suffix)):
        return None
    name = filename[len(prefix) : len(filename) - len(suffix)]
    return name if NAME_PATTERN.fullmatch(name) else None


def name_from_lifetime_lock(filename: str) -> str | None:
    """The daemon name a lifetime-lock file name carries, or ``None``."""
    return _name_between(filename, LIFETIME_LOCK_PREFIX, LIFETIME_LOCK_SUFFIX)


def name_from_socket(filename: str) -> str | None:
    """The daemon name a socket file name carries, or ``None``."""
    return _name_between(filename, SOCKET_PREFIX, SOCKET_SUFFIX)


def name_from_log(filename: str) -> str | None:
    """The daemon name a log file name carries, or ``None``."""
    return _name_between(filename, LOG_PREFIX, LOG_SUFFIX)
