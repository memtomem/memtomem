"""Keep huggingface_hub off its shared-blob lock where ``flock`` does not work (#3261).

huggingface-hub 1.x links a downloaded file to a store shared by the whole cache,
under a lock it takes with filelock. On a filesystem whose ``flock`` answers
``ENOSYS`` (some network mounts), filelock 4.2.1 and later read the empty lock file
the Hub creates as a held soft lock, and the Hub waits for it with no timeout: the
first model download never finishes. memtomem's dependencies do not cap filelock, so
an install made without the lockfile gets such a release.

``prepare_hub_locks`` turns the shared store off for the process when the cache
directory fails a ``flock`` probe. The Hub reads ``constants.HF_HUB_DISABLE_SHARED_BLOBS``
each time a download decides whether to use the store, so setting it here applies to
the downloads that start after it. Without the store a file another repository
already stored is downloaded again instead of linked; nothing stored changes.

It covers downloads only. The Hub's cache-cleanup helpers take the same lock without
reading the flag, and memtomem calls none of them. A zero-byte lock file left under
``.locks/`` by an earlier run can still block a download on such a filesystem.
"""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_checked_lock = threading.Lock()
_checked: set[str] = set()


def _flock_works(cache_dir: Path) -> bool:
    """Return True only when an exclusive ``flock`` succeeds in ``cache_dir``.

    An outcome that proves nothing, such as a cache root the probe cannot write
    while the directories the Hub writes under it are writable, also returns False:
    turning the shared store off without need costs reuse, leaving it on where its
    lock cannot be taken costs a download that never ends. Windows has no ``fcntl``;
    filelock locks there with ``msvcrt`` and never falls back to a soft lock.
    """
    try:
        import fcntl
    except ImportError:
        return True
    try:
        with tempfile.TemporaryFile(dir=cache_dir) as probe:
            fcntl.flock(probe.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def prepare_hub_locks(cache_dir: Path) -> None:
    """Turn huggingface_hub's shared blob store off if ``cache_dir`` cannot ``flock``.

    Called before every Hub download into the fastembed cache. Each directory is
    probed once per process. The setting is process-wide: later downloads into any
    cache skip the store too. A huggingface_hub without the setting (before the
    shared store existed) is left alone, as is one where the user already set it.
    """
    key = str(cache_dir)
    with _checked_lock:
        if key in _checked:
            return
        _checked.add(key)
        if _flock_works(cache_dir):
            return
        try:
            from huggingface_hub import constants
        except ImportError:
            return
        if getattr(constants, "HF_HUB_DISABLE_SHARED_BLOBS", True):
            return
        constants.HF_HUB_DISABLE_SHARED_BLOBS = True
    logger.warning(
        "File locks do not work in the model cache %s, so huggingface_hub's shared "
        "blob store is off for this process: with filelock 4.2.1 or later its lock "
        "would wait forever there. Files another model already stored are downloaded "
        "again instead of linked.",
        cache_dir,
    )
