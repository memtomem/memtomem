# ruff: noqa: E402, F401
"""MCP server package — facade and tool registration.

All public symbols are re-exported here for backward compatibility:
    ``from memtomem.server import AppContext, mem_search, mcp, main``
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from memtomem.server.component_factory import (
    Components as Components,
    close_components as close_components,
    create_components as create_components,
)
from memtomem.server.context import (
    AppContext as AppContext,
    CtxType as CtxType,
    _get_app as _get_app,
    _get_app_initialized as _get_app_initialized,
)
from memtomem.server.formatters import (
    _format_compact_result as _format_compact_result,
    _format_results as _format_results,
    _format_single_result as _format_single_result,
    _format_structured_results as _format_structured_results,
    _format_verbose_result as _format_verbose_result,
    _short_path as _short_path,
)
from memtomem.server.helpers import (
    _parse_recall_date as _parse_recall_date,
    _set_config_key as _set_config_key,
)
from memtomem.server.lifespan import app_lifespan

# ── mcp instance — must be created before tool-module imports ──────────
mcp = FastMCP("memtomem", lifespan=app_lifespan)

# ── Register ALL tools (decorators bind to `mcp` on import) ───────────
from memtomem.server.tools.ask import mem_ask  # noqa: E402, F401
from memtomem.server.tools.indexing import mem_index  # noqa: E402, F401
from memtomem.server.tools.memory_crud import mem_add, mem_batch_add, mem_delete, mem_edit  # noqa: E402, F401
from memtomem.server.tools.recall import mem_recall  # noqa: E402, F401
from memtomem.server.tools.search import mem_search, mem_expand  # noqa: E402, F401
from memtomem.server.tools.status_config import (
    mem_config,
    mem_embedding_reset,
    mem_reset,
    mem_stats,
    mem_status,
    mem_version,
)  # noqa: E402, F401
from memtomem.server.tools.namespace import (
    mem_ns_assign,
    mem_ns_list,
    mem_ns_delete,
    mem_ns_set,
    mem_ns_get,
    mem_ns_rename,
    mem_ns_update,
)  # noqa: E402, F401
from memtomem.server.tools.dedup_decay import (
    mem_cleanup_orphans,
    mem_dedup_scan,
    mem_dedup_merge,
    mem_decay_scan,
    mem_decay_expire,
)  # noqa: E402, F401
from memtomem.server.tools.export_import import mem_export, mem_import  # noqa: E402, F401
from memtomem.server.tools.auto_tag import mem_auto_tag  # noqa: E402, F401
from memtomem.server.tools.browse import mem_list, mem_read  # noqa: E402, F401
from memtomem.server.tools.tag_management import mem_tag_list, mem_tag_rename, mem_tag_delete  # noqa: E402, F401
from memtomem.server.tools.url_index import mem_fetch  # noqa: E402, F401
from memtomem.server.tools.cross_ref import mem_link, mem_unlink, mem_related  # noqa: E402, F401
from memtomem.server.tools.session import mem_session_start, mem_session_end, mem_session_list  # noqa: E402, F401
from memtomem.server.tools.scratch import mem_scratch_set, mem_scratch_get, mem_scratch_promote  # noqa: E402, F401
from memtomem.server.tools.procedure import mem_procedure_save, mem_procedure_list  # noqa: E402, F401
from memtomem.server.tools.multi_agent import mem_agent_register, mem_agent_search, mem_agent_share  # noqa: E402, F401
from memtomem.server.tools.evaluation import mem_eval  # noqa: E402, F401
from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply  # noqa: E402, F401
from memtomem.server.tools.reflection import mem_reflect, mem_reflect_save  # noqa: E402, F401
from memtomem.server.tools.search_history import mem_search_history, mem_search_suggest  # noqa: E402, F401
from memtomem.server.tools.conflict import mem_conflict_check  # noqa: E402, F401
from memtomem.server.tools.importance import mem_importance_scan  # noqa: E402, F401
from memtomem.server.tools.importers import mem_import_notion, mem_import_obsidian  # noqa: E402, F401
from memtomem.server.tools.entity import mem_entity_scan, mem_entity_search  # noqa: E402, F401
from memtomem.server.tools.temporal import mem_timeline, mem_activity  # noqa: E402, F401
from memtomem.server.tools.policy import (
    mem_policy_add,
    mem_policy_list,
    mem_policy_delete,
    mem_policy_run,
)  # noqa: E402, F401
from memtomem.server.tools.context import (
    mem_context_detect,
    mem_context_generate,
    mem_context_diff,
    mem_context_sync,
)  # noqa: E402, F401
from memtomem.server.tools.ingest import mem_ingest  # noqa: E402, F401  — no @mcp.tool; import triggers @register("ingest") for mem_do routing
from memtomem.server.tools.watchdog import mem_watchdog  # noqa: E402, F401
from memtomem.server.tools.meta import mem_do  # noqa: E402, F401
import memtomem.server.resources  # noqa: E402, F401  — register MCP resources

# ── Tool mode: core | standard | full ─────────────────────────────────
# Set MEMTOMEM_TOOL_MODE env var to control which tools are exposed.
#   core     → 9 tools (8 core + mem_do). Default. mem_do routes to all others.
#   standard → core + frequently used packs as individual tools + mem_do
#   full     → all tools registered individually (no mem_do needed)

_CORE_TOOLS = {
    "mem_search",
    "mem_add",
    "mem_index",
    "mem_recall",
    "mem_status",
    "mem_stats",
    "mem_list",
    "mem_read",
    "mem_do",
}

_TOOL_MODE = os.environ.get("MEMTOMEM_TOOL_MODE", "core").lower()

if _TOOL_MODE != "full":
    if _TOOL_MODE == "standard":
        from memtomem.server.tool_registry import ACTIONS

        _standard_packs = {"crud", "namespace", "tags", "sessions", "scratch", "relations"}
        _allowed = _CORE_TOOLS | {
            f"mem_{name}" for name, info in ACTIONS.items() if info.category in _standard_packs
        }
    else:
        _allowed = _CORE_TOOLS
    for name in list(mcp._tool_manager._tools):
        if name not in _allowed:
            mcp._tool_manager.remove_tool(name)


def _install_sigterm_handler(pid_file: Path) -> None:
    """Install a SIGTERM handler that unlinks ``pid_file`` and hard-exits.

    ``mcp.run()`` runs an asyncio event loop, and asyncio swallows
    ``SystemExit`` raised from a classic ``signal.signal`` handler — the
    integration test in ``test_server_sigterm.py`` is the live repro.
    So we can't rely on ``sys.exit(0)`` + ``atexit``: we unlink
    explicitly and call ``os._exit(0)`` to bypass the event loop.

    Only register after the flock succeeds, so we never unlink a pid
    file another primary owns. ``atexit`` still handles the normal
    stdin-EOF shutdown path.
    """
    import os as _os
    import signal

    def _handle(_signum: int, _frame: object) -> None:
        try:
            pid_file.unlink(missing_ok=True)
        except OSError:
            pass
        _os._exit(0)

    signal.signal(signal.SIGTERM, _handle)


def _try_hold_legacy_flock(legacy_pid: Path) -> object | None:
    """Acquire a lifetime flock on the pre-#412 pid file, if present.

    During the transition window a user may still have a v0.1.24 or older
    ``memtomem-server`` running — it holds ``fcntl.flock`` on
    ``~/.memtomem/.server.pid``. The new server's own flock target lives
    on ``$XDG_RUNTIME_DIR``, so without this probe two servers could run
    concurrently against the same SQLite DB and corrupt the WAL (#412
    review B1).

    Behavior:

    - If ``~/.memtomem/`` does not exist, skip — this is a fresh install
      with no upgrade history, and touching it would re-pollute the
      directory that #412 specifically keeps out of handshake.
    - Otherwise, open the legacy path (``a+b`` creates it if missing; we
      are inside an already-existing ``~/.memtomem/`` so no new
      pollution) and try ``LOCK_EX | LOCK_NB``.
    - Lock held by another process → an old server is alive; print to
      stderr and ``sys.exit(1)``. Two concurrent writers is strictly
      worse than one missed MCP handshake.
    - Lock acquired → return the file handle; caller holds it for the
      process lifetime so a *future* old server starting after us also
      hits this lock and bails via its own concurrent-detection path.

    Returns ``None`` on the skip paths (fresh install, open error). The
    returned fd must stay referenced for the lock to persist.
    """
    import fcntl
    import sys

    legacy_state_dir = Path.home() / ".memtomem"
    if not legacy_state_dir.is_dir():
        return None

    try:
        legacy_fp = open(legacy_pid, "a+b")
    except OSError:
        return None

    try:
        fcntl.flock(legacy_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (BlockingIOError, OSError):
        print(
            f"error: another memtomem-server holds a lock at {legacy_pid} "
            f"(likely a pre-0.1.25 install). Stop it before starting a new "
            f"server; `mm uninstall` will also refuse until it is gone.",
            file=sys.stderr,
        )
        legacy_fp.close()
        sys.exit(1)
    return legacy_fp


def main() -> None:
    """Run the MCP server."""
    import atexit
    import fcntl

    from memtomem._runtime_paths import ensure_runtime_dir, legacy_server_pid_path

    # B1: bidirectional mutual exclusion during the transition window.
    # Hold the legacy flock for the process lifetime so an old (pre-#412)
    # server running *now* is detected and a future one starting *after*
    # us also bails.
    _legacy_lock_fp = _try_hold_legacy_flock(legacy_server_pid_path())
    if _legacy_lock_fp is not None:
        atexit.register(lambda: _legacy_lock_fp.close())

    # Runtime files (pid / flock) live on ``$XDG_RUNTIME_DIR/memtomem``
    # when the platform provides one, otherwise a per-user temp subdir.
    # This keeps ``~/.memtomem/`` untouched during MCP handshake — it is
    # created only when persistent storage is first written (#412).
    pid_file = ensure_runtime_dir() / "server.pid"

    # Advisory lock — prevents multiple MCP servers from writing concurrently.
    # The lock is held for the lifetime of the process and auto-released on exit.
    _lock_fp = open(pid_file, "w")  # noqa: SIM115
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another server already holds the lock — proceed anyway (the editor
        # expects the process to stay alive), but log a warning. Don't register
        # atexit unlink or the SIGTERM handler: either would yank the primary
        # server's pid file out from under it.
        _lock_fp.close()
        import logging

        logging.getLogger(__name__).warning(
            "Another instance is already running (pid file: %s). Concurrent writes may be slow.",
            pid_file,
        )
    else:
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
        atexit.register(lambda: _lock_fp.close())  # LIFO: runs second
        atexit.register(lambda: pid_file.unlink(missing_ok=True))  # LIFO: runs first
        _install_sigterm_handler(pid_file)

    mcp.run()
