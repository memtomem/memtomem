"""Module-level asyncio locks shared across web route handlers.

Two independent locks serialize different write paths:

* ``_config_lock`` — guards every write path that touches
  ``~/.memtomem/config.json`` and/or mutates ``app.state.config``. Used by
  PATCH/save/memory-dirs handlers in :mod:`memtomem.web.routes.system`.
  History: started life as ``_config_patch_lock`` (PATCH-only); hot-reload
  in #267 extended it to ``/config/save``, ``/memory-dirs/add``, and
  ``/memory-dirs/remove`` so a concurrent disk edit + UI save can't
  interleave.

* ``_gateway_lock`` — guards every context-gateway write path that touches
  ``.memtomem/{settings,agents,commands,skills}/`` or fans out to runtime
  targets like ``~/.claude/settings.json``. Wraps POST / PUT / DELETE
  handlers in :mod:`memtomem.web.routes.settings_sync`,
  :mod:`memtomem.web.routes.context_agents`,
  :mod:`memtomem.web.routes.context_commands`, and
  :mod:`memtomem.web.routes.context_skills`. Without it two concurrent
  ``POST /api/settings-sync`` (or a Web UI + ``mm context sync`` racing
  in the same loop) can interleave on the same target file. The
  ``st_mtime_ns`` guards in update handlers narrow the race window but
  do not close it without the lock for cross-process writers.

The two locks are independent: ``config.json`` operations never block
gateway operations and vice versa.
"""

from __future__ import annotations

import asyncio

_config_lock = asyncio.Lock()
_gateway_lock = asyncio.Lock()
