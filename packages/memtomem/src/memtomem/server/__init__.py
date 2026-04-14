# ruff: noqa: E402, F401
"""MCP server package — facade and tool registration.

All public symbols are re-exported here for backward compatibility:
    ``from memtomem.server import AppContext, mem_search, mcp, main``
"""

from __future__ import annotations

import os

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


def main() -> None:
    """Run the MCP server."""
    import atexit
    import fcntl
    from pathlib import Path

    pid_dir = Path("~/.memtomem").expanduser()
    pid_dir.mkdir(parents=True, exist_ok=True)
    pid_file = pid_dir / ".server.pid"

    # Advisory lock — prevents multiple MCP servers from writing concurrently.
    # The lock is held for the lifetime of the process and auto-released on exit.
    _lock_fp = open(pid_file, "w")  # noqa: SIM115
    try:
        fcntl.flock(_lock_fp, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Another server already holds the lock — proceed anyway (the editor
        # expects the process to stay alive), but log a warning.
        _lock_fp.close()
        import sys

        print(
            "memtomem-server: another instance is already running "
            f"(pid file: {pid_file}). Concurrent writes may be slow.",
            file=sys.stderr,
        )
    else:
        import os

        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()
        atexit.register(lambda: _lock_fp.close())  # LIFO: runs second
        atexit.register(lambda: pid_file.unlink(missing_ok=True))  # LIFO: runs first

    mcp.run()
