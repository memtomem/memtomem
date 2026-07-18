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
  ``.memtomem/{settings,agents,commands,skills,mcp-servers}/``, the wiki, or
  fans out to runtime targets like ``~/.claude/settings.json``. Wraps
  POST / PUT / DELETE handlers in :mod:`memtomem.web.routes.settings_sync`,
  the four per-kind route modules (``context_agents`` / ``context_commands``
  / ``context_skills`` / ``context_mcp_servers``), ``context_sync_all``,
  ``context_transfer``, ``context_versions``, ``context_mutations``, and
  ``wiki_mutations``. Handler shape varies: the per-kind CRUD handlers run
  their engine call synchronously inside ``async with _gateway_lock``, while
  handlers whose engines block on a cross-process file lock (install/update,
  transfer, versions, wiki commit) offload to ``asyncio.to_thread`` inside
  the lock with a bounded engine ``lock_timeout`` (#1145 shape).

  This is an **in-process** ``asyncio.Lock`` — layer 1 of a two-layer model.
  It fully serializes concurrent async mutators in *this* server process, so
  two concurrent ``POST /api/settings-sync`` cannot interleave; it does NOT,
  on its own, serialize against a separate-process writer (a CLI
  ``mm context sync`` or the MCP server). Cross-process safety is layer 2,
  provided per-engine by :mod:`memtomem.context._atomic`.

  **Canonical mutations (ADR-0030 §6)** — every first-party write to a
  reverse-import canonical (skills / agents / commands: Pull, CRUD, version &
  label ops, transfer, migrate, wiki install/update) now holds the name-keyed,
  layout-independent canonical sidecar lock
  (``context/_canonical_txn.canonical_sidecar_lock``,
  ``<canonical_root>/.{name}.lock``). The full web lock order is
  **``_gateway_lock`` → sorted canonical sidecar(s) → ``versions.json`` /
  ``lock.json``**; the blocking ``_file_lock`` runs in ``asyncio.to_thread``
  so it never sits on the event loop (holding ``_gateway_lock`` there is safe
  because it serializes in-process callers, so the worker never self-contends).

  Fan-out (Push) stays asymmetric — it writes RUNTIME targets, not canonicals:

  - **skills** hold a ``portalocker`` ``_file_lock`` across the whole
    move-aside → rename-in staging swap, so a cross-process skill sync is
    fully serialized.
  - **agents / commands** fan-out takes no file lock: each runtime artifact is
    written with a single full-content atomic ``os.replace`` (torn-file-proof
    on its own), and partial cross-file fan-out is intentional by design
    (``context._sync_atomic``), so there is no read-modify-write window to
    guard. (Their *canonical* writes DO take the C0 lock above.)
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


class _LoopLocalLock:
    """An ``asyncio.Lock`` proxy that resolves to a per-event-loop lock.

    A bare module-level ``asyncio.Lock`` binds to the first event loop that
    acquires it and then raises ``RuntimeError: ... is bound to a different
    event loop`` when reused from another loop. In production this never
    happens — the web server runs a single long-lived loop — but pytest gives
    each ``async`` test its own loop, so whichever test acquires the lock first
    binds it and a later test on a fresh loop fails non-deterministically.

    Keying the underlying lock by the running loop keeps the contract intact:
    *within* one loop every caller shares the same lock (so two concurrent
    gateway mutators still serialise), while distinct loops get distinct locks.
    Call sites use the proxy exactly like a lock — ``async with _gateway_lock:``
    and ``.locked()`` — so nothing downstream changes, and ``is`` identity (the
    module singleton) still holds because the proxy object itself is the shared
    singleton.

    A :class:`weakref.WeakKeyDictionary` cannot bound the registry here: once a
    lock takes its contended slow path it strongly references its bound loop
    (``lock._loop``), so the loop value keeps the weak key alive and entries for
    closed loops never collect. Instead the registry is a plain dict pruned of
    closed-loop entries each time a new loop is seen — in production it holds
    exactly one entry; across pytest's per-test loops it stays bounded.
    """

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: dict[asyncio.AbstractEventLoop, asyncio.Lock] = {}

    def _current(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        lock = self._locks.get(loop)
        if lock is None:
            # Each stored lock strongly references its bound loop, so closed
            # loops must be pruned explicitly (a WeakKeyDictionary would never
            # reclaim them). Sweep on the new-loop path — cheap and bounded.
            for dead in [lp for lp in self._locks if lp.is_closed()]:
                del self._locks[dead]
            lock = asyncio.Lock()
            self._locks[loop] = lock
        return lock

    async def __aenter__(self) -> asyncio.Lock:
        lock = self._current()
        await lock.acquire()
        return lock

    async def __aexit__(self, *exc: object) -> None:
        self._current().release()

    def locked(self) -> bool:
        """Whether the gateway lock is held.

        A single shared ``asyncio.Lock`` answered ``.locked()`` the same way
        from any thread. Handlers offload the synchronous engine call to a
        worker thread (no running loop) while holding the lock, and the
        lock-held tests probe from inside that thread — so when no loop is
        current we report whether *any* per-loop lock is held (in production
        that is the one lock; in tests it is the request loop's). With a running
        loop we report that loop's lock without creating one as a side effect.
        """
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # Snapshot first: the registry may be mutated on the loop thread
            # (a first contended acquire) while we iterate on a worker thread.
            return any(lock.locked() for lock in list(self._locks.values()))
        lock = self._locks.get(loop)
        return lock.locked() if lock is not None else False


_config_lock = _LoopLocalLock()
_gateway_lock = _LoopLocalLock()
