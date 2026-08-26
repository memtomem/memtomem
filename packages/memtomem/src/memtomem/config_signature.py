"""Detect on-disk config changes without reading the files.

Both long-running processes need to notice that someone else edited
``~/.memtomem/config.json`` or a ``config.d`` fragment: ``mm web`` reloads its
whole config from it (``web/hot_reload.py``), and the MCP server reconciles its
watched index roots from it (``server/context.py``, issue #2186). The stat-level
signature and the strict config rebuild live here so neither has to import the
other — ``server/`` deliberately depends on nothing under ``web/``.

Nothing here holds state: callers keep their own last-seen signature.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from memtomem.config import (
    Mem2MemConfig,
    _config_d_path,
    _override_path,
    load_config_d,
    load_config_overrides,
)

logger = logging.getLogger(__name__)

# A tuple of (path_str, mtime_ns) pairs sorted by path, including a sentinel for
# the config.d directory itself so newly created / removed fragments are
# detected even when their own files weren't touched.
Signature = tuple[tuple[str, int], ...]


def current_signature() -> Signature:
    """Build the composite ``(path, mtime_ns)`` signature for config state.

    Includes ``~/.memtomem/config.json`` plus every ``~/.memtomem/config.d/
    *.json`` entry plus the directory mtime itself. Missing files contribute
    a ``-1`` mtime rather than being skipped, so their appearance or removal
    still changes the signature.

    Cost is one ``stat`` for ``config.json``, one ``iterdir`` of ``config.d``,
    and one ``stat`` per fragment — no file is read. Callers on a hot path
    (every MCP tool call, every web request) pay that and nothing more.
    """
    entries: list[tuple[str, int]] = []

    override = _override_path()
    entries.append((str(override), _stat_mtime_ns(override)))

    d_path = _config_d_path()
    entries.append((str(d_path), _stat_mtime_ns(d_path) if d_path.is_dir() else -1))
    try:
        fragments = (
            sorted(p for p in d_path.iterdir() if p.is_file() and p.suffix == ".json")
            if d_path.is_dir()
            else []
        )
    except OSError as exc:
        # The directory can be replaced or removed between the ``is_dir`` and
        # the walk. Callers sample this on a hot path (every MCP tool call), so
        # a concurrent directory swap must not raise into whatever they were
        # doing — the directory's own mtime entry above already moved, so the
        # signature still reads as changed and the next sample retries.
        logger.warning("Listing %s during a config-change check failed: %s", d_path, exc)
        fragments = []
    for frag in fragments:
        entries.append((str(frag), _stat_mtime_ns(frag)))

    return tuple(entries)


def _stat_mtime_ns(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return -1
    except OSError as exc:
        logger.warning("stat(%s) failed during config-change check: %s", path, exc)
        return -1


def get_config_mtime_ns() -> int:
    """Return the current ``config.json`` mtime in ns, or ``-1`` if missing."""
    return _stat_mtime_ns(_override_path())


def build_fresh_config(*, migrate: bool = True, strict_fragments: bool = False) -> Mem2MemConfig:
    """Replay the canonical load path used at startup, strictly.

    Defaults (+ env via pydantic-settings) → ``config.d`` fragments →
    ``config.json`` overrides. Raises on ``config.json`` JSON / OS errors so
    the caller can switch to fail-closed mode.

    ``migrate=False`` skips the legacy ``auto_discover`` → explicit
    ``memory_dirs`` migration, which **writes** ``config.json``. A caller that
    only wants to look at the current config — as opposed to one standing in
    for startup — must pass it: re-reading on a hot path would otherwise turn
    into a surprise write to the user's config file.

    :func:`load_config_overrides` itself swallows parse errors with a warning
    log — startup historically wanted to boot with defaults rather than crash
    on a bad user file. A reader that re-reads the file mid-process needs the
    opposite: silently getting defaults back would read a broken file as
    "every setting was reset", which the next save would then write back, and
    which a roots-reconciler would act on by unwatching every directory. So
    ``config.json`` is pre-parsed here before delegating.

    ``strict_fragments=True`` extends that strictness to the ``config.d``
    fragments, which :func:`load_config_d` otherwise logs and skips one at a
    time. It is passed down to the loader rather than pre-checked here, so it
    covers every skip the loader can make — a fragment whose JSON parses but
    whose ``memory_dirs`` is a string, say — and does it on the same read the
    config is built from, leaving no window for a write to land between a
    validation pass and the real one.
    """
    override = _override_path()
    if override.exists():
        # Strict pre-parse — raises on malformed JSON / OS errors.
        _ = json.loads(override.read_text(encoding="utf-8"))

    cfg = Mem2MemConfig()
    load_config_d(cfg, strict=strict_fragments)
    load_config_overrides(cfg, migrate=migrate)
    return cfg
