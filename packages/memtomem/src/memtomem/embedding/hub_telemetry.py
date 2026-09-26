"""Run memtomem's Hub downloads with huggingface_hub telemetry off (#2550, #2552, #2556)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from contextlib import contextmanager

_hub_telemetry_lock = threading.Lock()
_hub_telemetry_depth = 0
_hub_telemetry_saved = False


@contextmanager
def hub_telemetry_off() -> Iterator[None]:
    """Run memtomem's Hub downloads with huggingface_hub telemetry off.

    Covers the pinned E5 calls (#2550), the fastembed ``TextEmbedding`` and
    ``TextCrossEncoder`` constructors (#2552) and the sentence-transformers
    ``CrossEncoder`` constructor (#2556); the constructors download through
    huggingface_hub on a cache miss. With telemetry on, huggingface_hub tags the user agent with
    the host AI agent (``agent/<id>``) and caches its agent registry at
    ``HF_HOME/.agent_harnesses.json``, ignoring the ``cache_dir`` passed to it. The
    flag is read per request, so flipping it after import works. This overrides an
    explicit opt-in in the environment.

    The flag is process-global: another Hub request in this process that builds its
    headers while a window is open is also sent without telemetry. Windows may
    overlap across threads or nest; the last exit restores the saved value only if
    the flag still reads True, so a False written by another component meanwhile is
    kept, while a True written meanwhile cannot be told apart from ours and is
    reverted. An async exception during the bookkeeping itself can leave the flag set.
    """
    global _hub_telemetry_depth, _hub_telemetry_saved
    from huggingface_hub import constants

    with _hub_telemetry_lock:
        if _hub_telemetry_depth == 0:
            _hub_telemetry_saved = constants.HF_HUB_DISABLE_TELEMETRY
            constants.HF_HUB_DISABLE_TELEMETRY = True
        _hub_telemetry_depth += 1
    try:
        yield
    finally:
        with _hub_telemetry_lock:
            _hub_telemetry_depth -= 1
            if _hub_telemetry_depth == 0 and constants.HF_HUB_DISABLE_TELEMETRY is True:
                constants.HF_HUB_DISABLE_TELEMETRY = _hub_telemetry_saved
