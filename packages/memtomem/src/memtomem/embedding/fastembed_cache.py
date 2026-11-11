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
# Owner-only, so another local account cannot traverse the cache or the lock
# files huggingface-hub keeps in it. Windows keeps the default: a POSIX mode
# does not describe its ACLs, and CPython gives 0o700 a special meaning there.
_DEFAULT_CACHE_MODE = 0o777 if os.name == "nt" else 0o700


def resolve_fastembed_cache_dir() -> Path:
    """Return the directory fastembed should use for model snapshots.

    Precedence (first non-empty wins):

    1. ``MEMTOMEM_FASTEMBED_CACHE`` — memtomem-specific override.
    2. ``FASTEMBED_CACHE_PATH`` — fastembed's own convention; honoured so
       users who already set it for other tools see consistent behaviour.
    3. ``~/.memtomem/cache/fastembed`` — default, alongside the rest of the
       memtomem state under ``~/.memtomem/``.

    The directory is created if missing — fastembed expects to be able to
    write into it on first use. On POSIX the default directory is created
    owner-only (0700). A directory named by either variable is created with
    the umask default, and a directory that already exists is not changed.
    """
    for env in ("MEMTOMEM_FASTEMBED_CACHE", "FASTEMBED_CACHE_PATH"):
        raw = os.environ.get(env)
        if raw:
            raw_path = raw
            mode = 0o777
            break
    else:
        raw_path = _DEFAULT_CACHE_PATH
        mode = _DEFAULT_CACHE_MODE
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
    # ``mode`` reaches only a leaf this call creates: missing parents get the
    # umask default, and an existing directory keeps the mode it has.
    path.mkdir(mode=mode, parents=True, exist_ok=True)
    return path
