"""Stable cache directory for fastembed model snapshots.

fastembed's default cache lives under ``tempfile.gettempdir()`` (on macOS
that resolves to ``/var/folders/.../T/fastembed_cache/``), which the OS
periodically reaps. After a reap, the smaller graph blob (``model.onnx``)
disappears while the larger weight blob (``model.onnx_data``) — accessed
recently during reindex — survives, leaving dangling symlinks. fastembed
then logs ``Local file sizes do not match the metadata`` and ONNX Runtime
fails with ``NO_SUCHFILE``, silently turning every ``Auto-reindexed`` run
into ``indexed=0``.

Pin the cache to a stable path so the reaper does not see it.
"""

from __future__ import annotations

import os
from pathlib import Path

_DEFAULT_CACHE_PATH = "~/.memtomem/cache/fastembed"


def resolve_fastembed_cache_dir() -> Path:
    """Return the directory fastembed should use for model snapshots.

    Precedence (first non-empty wins):

    1. ``MEMTOMEM_FASTEMBED_CACHE`` — memtomem-specific override.
    2. ``FASTEMBED_CACHE_PATH`` — fastembed's own convention; honoured so
       users who already set it for other tools see consistent behaviour.
    3. ``~/.memtomem/cache/fastembed`` — default, alongside the rest of the
       memtomem state under ``~/.memtomem/``.

    The directory is created if missing — fastembed expects to be able to
    write into it on first use.
    """
    for env in ("MEMTOMEM_FASTEMBED_CACHE", "FASTEMBED_CACHE_PATH"):
        raw = os.environ.get(env)
        if raw:
            raw_path = raw
            break
    else:
        raw_path = _DEFAULT_CACHE_PATH
    # ``Path.expanduser()`` raises ``RuntimeError("Could not determine home
    # directory.")`` on Python 3.12+ when ``~`` cannot be resolved (no
    # ``$HOME`` and no pwent — happens in some container/CI setups). Wrap
    # that in a message that names the env vars the operator can set.
    try:
        path = Path(raw_path).expanduser()
    except RuntimeError as exc:
        raise RuntimeError(
            f"fastembed cache path {raw_path!r} could not be expanded — "
            "$HOME is unset and no pwent is available. Set MEMTOMEM_FASTEMBED_CACHE "
            "to an absolute path, or ensure $HOME is set in the environment."
        ) from exc
    path.mkdir(parents=True, exist_ok=True)
    return path
