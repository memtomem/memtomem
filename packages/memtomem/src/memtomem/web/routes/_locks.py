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
  :mod:`memtomem.web.routes.context_skills` (each handler runs the engine
  call synchronously inside ``async with _gateway_lock``).

  This is an **in-process** ``asyncio.Lock`` — layer 1 of a two-layer model.
  It fully serializes concurrent async mutators in *this* server process, so
  two concurrent ``POST /api/settings-sync`` cannot interleave; it does NOT,
  on its own, serialize against a separate-process writer (a CLI
  ``mm context sync`` or the MCP server). Cross-process safety is layer 2,
  provided per-engine by :mod:`memtomem.context._atomic`, and it is
  asymmetric because each engine's write shape differs:

  - **skills** hold a ``portalocker`` ``_file_lock`` across the whole
    move-aside → rename-in staging swap (``context.skills`` batch + single
    paths), so a cross-process skill sync is fully serialized.
  - **agents / commands** take no file lock: each runtime artifact is written
    with a single full-content atomic ``os.replace`` (torn-file-proof on its
    own), and partial cross-file fan-out is intentional by design
    (``context._sync_atomic``), so there is no read-modify-write window to
    guard.
  - **settings** hold a per-target ``_file_lock`` across read-merge-write in
    ``context.settings.generate_all_settings``; the ``st_mtime_ns`` recheck is
    kept as a second layer that also catches a non-gateway direct disk edit.

  A single gateway-wide cross-process lock was deliberately rejected: there is
  no all-or-nothing snapshot invariant across settings + agents + commands +
  skills to protect, a blocking ``portalocker`` lock held inside the async
  handler would stall the event loop, and it would nest with the per-target
  ``_file_lock`` above into a non-reentrant self-deadlock.

The two locks are independent: ``config.json`` operations never block
gateway operations and vice versa.
"""

from __future__ import annotations

import asyncio

_config_lock = asyncio.Lock()
_gateway_lock = asyncio.Lock()
