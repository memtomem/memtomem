"""Poll-on-request hot-reload of ``~/.memtomem/config.json`` + ``config.d/``.

The web server loads the user's ``config.json`` / ``config.d`` fragments once
at startup and caches the resulting :class:`Mem2MemConfig` on
``app.state.config``. Without this module, CLI edits made while the server is
running (``mm config set``, external editor, etc.) stay invisible to the
running server AND get silently clobbered the next time a UI handler calls
:func:`save_config_overrides` against its stale in-memory copy.

Design (see ``project_web_hot_reload_bridge.md`` for the full rationale):

* **Trigger**: every read/write handler calls :func:`reload_if_stale` which
  compares a composite ``(path, mtime_ns)`` signature of ``config.json`` plus
  every ``config.d/*.json`` entry to the last known signature. If anything
  changed, a fresh :class:`Mem2MemConfig` is built via the canonical load path
  (``Mem2MemConfig()`` → :func:`load_config_d` → :func:`load_config_overrides`)
  and swapped into ``app.state.config``.
* **Runtime fanout**: tokenizer changes rebuild the FTS5 index and every
  config change invalidates the search cache — see
  :func:`apply_runtime_config_changes`, shared between the PATCH handler and
  the reload path so a hot-reload applies the same side-effects as an
  in-process PATCH.
* **Failure mode**: if the reload raises (bad JSON, pydantic validation,
  permission error), the existing ``app.state.config`` is left in place and
  the error is recorded on ``app.state.last_reload_error`` so the FE can
  surface a banner. Write handlers refuse with HTTP 409 while the error is
  active for the current on-disk mtime.

The public surface is intentionally small: :func:`current_signature`,
:func:`reload_if_stale`, :func:`apply_runtime_config_changes`, and the
helpers :func:`get_config_mtime_ns`, :func:`get_reload_error`. Everything
else is private.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from memtomem.config import (
    Mem2MemConfig,
    _config_d_path,
    _override_path,
    load_config_d,
    load_config_overrides,
)

if TYPE_CHECKING:
    from fastapi import FastAPI

    from memtomem.search.pipeline import SearchPipeline
    from memtomem.storage.sqlite_backend import SqliteBackend

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Stale signature
# ---------------------------------------------------------------------------

# Signature is a tuple of (path_str, mtime_ns) pairs sorted by path, including
# a sentinel for the config.d directory itself so newly created / removed
# fragments are detected even when their own files weren't touched.
Signature = tuple[tuple[str, int], ...]


def current_signature() -> Signature:
    """Build the composite ``(path, mtime_ns)`` signature for config state.

    Includes ``~/.memtomem/config.json`` plus every ``~/.memtomem/config.d/
    *.json`` entry plus the directory mtime itself. Missing files contribute
    a ``-1`` mtime rather than being skipped, so their appearance or removal
    still changes the signature.
    """
    entries: list[tuple[str, int]] = []

    override = _override_path()
    entries.append((str(override), _stat_mtime_ns(override)))

    d_path = _config_d_path()
    entries.append((str(d_path), _stat_mtime_ns(d_path) if d_path.is_dir() else -1))
    if d_path.is_dir():
        for frag in sorted(p for p in d_path.iterdir() if p.is_file() and p.suffix == ".json"):
            entries.append((str(frag), _stat_mtime_ns(frag)))

    return tuple(entries)


def _stat_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1
    except OSError as exc:
        logger.warning("stat(%s) failed during hot-reload check: %s", path, exc)
        return -1


def get_config_mtime_ns() -> int:
    """Return the current ``config.json`` mtime in ns, or ``-1`` if missing."""
    return _stat_mtime_ns(_override_path())


# ---------------------------------------------------------------------------
# Reload error surface
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ReloadError:
    """Captures a failed reload attempt for FE surfacing + write-handler gating."""

    message: str
    at_mtime_ns: int
    timestamp: float


def get_reload_error(app: FastAPI) -> ReloadError | None:
    return getattr(app.state, "last_reload_error", None)


def _set_reload_error(app: FastAPI, err: ReloadError | None) -> None:
    app.state.last_reload_error = err


def _get_last_signature(app: FastAPI) -> Signature | None:
    return getattr(app.state, "config_signature", None)


def _set_last_signature(app: FastAPI, sig: Signature) -> None:
    app.state.config_signature = sig


def commit_writer_signature(app: FastAPI) -> None:
    """Record the current on-disk signature after a successful write.

    Call this from any request handler that just invoked
    :func:`memtomem.config.save_config_overrides` inside ``_config_lock``.
    Without it, the next ``GET /api/config`` would see our own write as an
    "external change" and trigger a spurious reload from the same file we
    just wrote. The bump is cheap (one ``os.stat`` per fragment).

    This is the public alternative to the internal ``_set_last_signature``
    for the narrow "writer finalising its own change" use case.
    """
    _set_last_signature(app, current_signature())


# ---------------------------------------------------------------------------
# Reload
# ---------------------------------------------------------------------------


def _build_fresh_config() -> Mem2MemConfig:
    """Replay the canonical load path used at startup.

    Defaults (+ env via pydantic-settings) → ``config.d`` fragments →
    ``config.json`` overrides. Raises on ``config.json`` JSON / OS errors
    so the caller can switch to fail-closed mode.

    :func:`load_config_overrides` itself swallows parse errors with a
    warning log — startup historically wanted to boot with defaults
    rather than crash on a bad user file. Hot-reload needs strict
    behavior: a broken disk must surface as an error banner, not silently
    fall back to defaults (which would then be written back by the next
    save). So we pre-parse ``config.json`` here before delegating.
    """
    import json as _json

    override = _override_path()
    if override.exists():
        # Strict pre-parse — raises on malformed JSON / OS errors.
        _ = _json.loads(override.read_text(encoding="utf-8"))

    cfg = Mem2MemConfig()
    load_config_d(cfg)
    load_config_overrides(cfg)
    return cfg


def reload_if_stale(
    app: FastAPI,
    *,
    storage: SqliteBackend | None = None,
    search_pipeline: SearchPipeline | None = None,
) -> bool:
    """Reload config from disk if the composite signature changed.

    Returns ``True`` if ``app.state.config`` was swapped, ``False`` if the
    signature matched and nothing changed. On failure, keeps the existing
    config, records :class:`ReloadError` on ``app.state.last_reload_error``,
    and returns ``False``.

    ``storage`` / ``search_pipeline`` are optional; when provided, the
    runtime fanout (tokenizer FTS rebuild + cache invalidation) runs against
    them. Callers that already hold these (write handlers via ``Depends``)
    should pass them through so a disk-triggered tokenizer change still
    propagates.

    Note: FTS rebuild is fire-and-forget scheduled on the running event loop
    when invoked from an async context, so sync GET callers aren't blocked.
    Callers in a lock-free read context should still see the Settings swap
    immediately; the rebuild catches up out-of-band. The rebuild runs
    concurrently with any writer inside ``_config_lock``: it touches the
    FTS5 virtual table, not the ``config.json`` file, so there is no file-
    level race with ``save_config_overrides``.
    """
    sig = current_signature()
    last = _get_last_signature(app)
    if last == sig:
        # Signature matches; if a prior error was tied to a different
        # on-disk mtime than what we see now, clear it — the user either
        # fixed the file (mtime bumped forward) or the file vanished
        # (mtime == -1). If both signature matches AND at_mtime_ns still
        # equals current mtime, the error is still live, leave it.
        err = get_reload_error(app)
        if err is not None and err.at_mtime_ns != get_config_mtime_ns():
            _set_reload_error(app, None)
        return False

    try:
        new_cfg = _build_fresh_config()
    except Exception as exc:
        logger.warning(
            "Hot-reload failed for config at %s: %s", _override_path(), exc, exc_info=True
        )
        _set_reload_error(
            app,
            ReloadError(
                message=f"{type(exc).__name__}: {exc}",
                at_mtime_ns=get_config_mtime_ns(),
                timestamp=time.time(),
            ),
        )
        # Update the signature we've seen so we don't re-try on every hit;
        # we only retry once disk mtime changes again. Mirror of the
        # success-path CAS (#269 / issue #273): if a writer's
        # commit_writer_signature landed while _build_fresh_config was
        # failing, don't revert their bump — their view is strictly fresher
        # than the one we just failed to rebuild, and their signature will
        # satisfy the stale check on the next read.
        if _get_last_signature(app) == last:
            _set_last_signature(app, sig)
        return False

    # Compare-and-swap: while we were in _build_fresh_config (file I/O, can
    # take milliseconds), a writer inside _config_lock may have already
    # committed a fresh reload + signature bump via commit_writer_signature.
    # That view is at least as fresh as ours; discard our rebuild so we
    # don't revert the writer's signature and force a spurious next-GET
    # reload. Race eliminated per issue #268.
    if _get_last_signature(app) != last:
        return False

    old_cfg = getattr(app.state, "config", None)
    app.state.config = new_cfg
    _set_last_signature(app, sig)
    _set_reload_error(app, None)

    if old_cfg is not None and (storage is not None or search_pipeline is not None):
        apply_runtime_config_changes(
            old_cfg, new_cfg, storage=storage, search_pipeline=search_pipeline
        )

    logger.info("Hot-reloaded config from %s", _override_path())
    return True


# ---------------------------------------------------------------------------
# Runtime fanout — shared by PATCH handler and reload
# ---------------------------------------------------------------------------


def apply_runtime_config_changes(
    old_cfg: Any,
    new_cfg: Any,
    *,
    storage: SqliteBackend | None = None,
    search_pipeline: SearchPipeline | None = None,
) -> None:
    """Propagate runtime-mutable config changes to live components.

    * ``search.tokenizer`` changed → re-register global tokenizer + schedule
      ``storage.rebuild_fts()`` (async, fire-and-forget on current loop).
    * Any change → invalidate search cache.

    Older callers of this helper (e.g. the PATCH handler) may not provide
    ``storage`` / ``search_pipeline`` (rare), in which case the matching
    fanout step is skipped. This keeps the helper usable in unit tests and
    from non-web callers.
    """
    try:
        tokenizer_changed = old_cfg.search.tokenizer != new_cfg.search.tokenizer
    except AttributeError:
        tokenizer_changed = False

    if tokenizer_changed and storage is not None:
        from memtomem.storage.fts_tokenizer import set_tokenizer

        set_tokenizer(new_cfg.search.tokenizer)
        _schedule_fts_rebuild(storage, new_cfg.search.tokenizer)

    if search_pipeline is not None:
        search_pipeline.invalidate_cache()


def _schedule_fts_rebuild(storage: SqliteBackend, tokenizer: str) -> None:
    """Kick off ``storage.rebuild_fts()`` as a background task if possible.

    When called from an async request handler the rebuild runs on the current
    loop; when called from a sync context without a running loop, it falls
    back to ``asyncio.run`` so non-web callers (tests, future CLIs) still
    work.
    """
    import asyncio

    async def _runner() -> None:
        try:
            count = await storage.rebuild_fts()
            logger.info("FTS index rebuilt with tokenizer=%s (%d chunks)", tokenizer, count)
        except Exception:
            logger.warning("FTS rebuild after tokenizer change failed", exc_info=True)

    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        asyncio.run(_runner())
        return
    loop.create_task(_runner())
