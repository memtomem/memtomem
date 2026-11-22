"""Which store a server process would open, importable without the server.

The pid-file name, the shared daemon's frozen store and the stdio bridge's
admission check (#3381) all need this answer before any MCP machinery exists.
Importing any module of :mod:`memtomem.server` registers every tool, so the
resolution lives here and the server re-exports it.

Nothing here loads ``.env``: callers do that first, through their own seam
(``memtomem.server.lifespan._load_dotenv`` in the server, which the test suite
replaces), so a dotenv-supplied ``MEMTOMEM_STORAGE__SQLITE_PATH`` is seen.
"""

from __future__ import annotations

import os
from pathlib import Path


class StoreNotShareable(Exception):
    """The configured store cannot be shared by a daemon; the message says why.

    ``summary`` says it without the configured value, for a reason that is
    logged and shown by ``mem_status``.
    """

    def __init__(
        self, message: str, summary: str = "the configured store cannot be shared"
    ) -> None:
        super().__init__(message)
        self.summary = summary


def configured_store_path() -> Path | None:
    """Best-effort store resolution for pid-file naming (#1990).

    Mirrors the effective config the lazy context will later open: the same
    env → config.d → overrides layering as the component factory — but with
    ``migrate=False``: pid naming is a read-only concern and must not rewrite
    ``config.json``. If a pending migration would change
    ``storage.sqlite_path``, the digest is stale for exactly one server run
    (equivalent to today's store-blind name), after which the persisted
    config converges. The same bound applies when ``config.json`` /
    ``config.d`` are edited after startup: the lazy context re-reads them
    at first tool call, so the pid name can lag the opened store for this
    run only — destructive CLI paths stay protected by the DB-lock probe
    and the instance registry regardless. Any failure returns ``None``
    and the caller falls back to the transitional bare ``server.pid``.
    """
    try:
        from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

        cfg = Mem2MemConfig()
        load_config_d(cfg, quiet=True)
        load_config_overrides(cfg, migrate=False)
        return Path(cfg.storage.sqlite_path)
    except Exception:
        return None


def resolve_store_path(configured: str | Path) -> Path:
    """The store a shared daemon names, or raise when it cannot be named.

    Raises ``OSError``, ``RuntimeError`` or ``ValueError``. ``Path.resolve``
    raises on a symlink loop on Python 3.12 but returns the unresolved path on
    3.13 and later, so the result is also ``stat``-ed: a loop or an unreadable
    ancestor raises on every supported version, while a store file that does
    not exist yet is a new store, not a failure.
    """
    resolved = Path(configured).expanduser().resolve()
    try:
        os.stat(resolved)
    except FileNotFoundError:
        pass
    return resolved


def resolve_shared_store(db_path: Path | None) -> tuple[Path, Path]:
    """``(pid-naming path, frozen identity)`` of a store a daemon can share.

    ``db_path`` is :func:`configured_store_path`'s answer, taken by the
    caller after loading ``.env``. Raises :class:`StoreNotShareable` for a
    store that cannot be named, cannot be resolved, or resolves two ways, and
    for a relative configured path when the process refuses those
    (``memtomem.config.refuse_relative_config_paths``).
    """
    from memtomem._runtime_paths import store_pid_digest
    from memtomem.config import RelativeConfigPathError
    from memtomem.config_signature import build_fresh_config

    try:
        config = build_fresh_config(migrate=False, strict_overrides=False)
    except RelativeConfigPathError as exc:
        raise StoreNotShareable(str(exc)) from exc
    configured = config.storage.sqlite_path
    if db_path is None or store_pid_digest(configured) is None:
        raise StoreNotShareable(
            f"the store {str(configured)!r} cannot be named, so it cannot be shared",
            "the configured store (storage.sqlite_path) cannot be named, so it cannot be shared",
        )
    try:
        frozen = resolve_store_path(configured)
        named = resolve_store_path(db_path)
    except (OSError, RuntimeError, ValueError) as exc:
        raise StoreNotShareable(
            f"the store {str(configured)!r} cannot be resolved: {exc}",
            f"the configured store (storage.sqlite_path) cannot be resolved ({type(exc).__name__})",
        ) from exc
    if frozen != named:
        raise StoreNotShareable(
            f"the store resolves two ways ({str(named)!r} and {str(frozen)!r}); "
            "refusing to share it",
            "the configured store (storage.sqlite_path) resolves two ways; refusing to share it",
        )
    return Path(db_path), frozen
