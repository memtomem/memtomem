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
from memtomem.server.instructions import INSTRUCTIONS as _INSTRUCTIONS
from memtomem.server.lifespan import app_lifespan

# ── mcp instance — must be created before tool-module imports ──────────
# ``instructions=`` is auto-injected into every MCP client's session as
# the ``initialize`` response's ``instructions`` field — the only
# documentation surface most LLMs see before picking a tool. Source of
# truth lives in ``memtomem/server/instructions.py``; pinned by
# ``tests/test_server_instructions.py``.
mcp = FastMCP("memtomem", instructions=_INSTRUCTIONS, lifespan=app_lifespan)

# Pin ``serverInfo.version`` in the MCP ``initialize`` response to the
# memtomem package version (#383). ``FastMCP.__init__`` has no ``version``
# parameter; when the underlying ``Server.version`` stays ``None`` the
# lowlevel server falls back to ``importlib.metadata.version("mcp")`` —
# which made every memtomem handshake report the MCP SDK version
# (e.g. ``1.27.0``) instead of ``mm --version`` (e.g. ``0.1.24``).
# External consumers keying off ``serverInfo.version`` (telemetry,
# error reports, "which version are we both on") saw misleading data.
from memtomem import __version__ as _memtomem_version

mcp._mcp_server.version = _memtomem_version

# ── Register ALL tools (decorators bind to `mcp` on import) ───────────
from memtomem.server.tools.ask import mem_ask  # noqa: E402, F401
from memtomem.server.tools.indexing import mem_index  # noqa: E402, F401
from memtomem.server.tools.memory_crud import (  # noqa: E402, F401
    mem_add,
    mem_add_redaction_stats,
    mem_batch_add,
    mem_delete,
    mem_edit,
)
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
from memtomem.server.tools.tag_management import (
    mem_tag_list,
    mem_tag_rename,
    mem_tag_delete,
    mem_tag_merge,
)  # noqa: E402, F401
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
    mem_context_init,
    mem_context_generate,
    mem_context_diff,
    mem_context_sync,
    mem_context_migrate,
)  # noqa: E402, F401
from memtomem.server.tools.ingest import mem_ingest  # noqa: E402, F401  — no @mcp.tool; import triggers @register("ingest") for mem_do routing
from memtomem.server.tools.watchdog import mem_watchdog  # noqa: E402, F401
from memtomem.server.tools.schedule import (  # noqa: E402, F401
    mem_schedule_delete,
    mem_schedule_list,
    mem_schedule_register,
    mem_schedule_run_now,
)
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

        _standard_packs = {
            "crud",
            "namespace",
            "tags",
            "sessions",
            "scratch",
            "relations",
            "schedule",
        }
        _allowed = _CORE_TOOLS | {
            f"mem_{name}" for name, info in ACTIONS.items() if info.category in _standard_packs
        }
    else:
        _allowed = _CORE_TOOLS
    for name in list(mcp._tool_manager._tools):
        if name not in _allowed:
            mcp._tool_manager.remove_tool(name)


def _install_sigterm_handler(*pid_files: Path) -> None:
    """Install a SIGTERM handler that unlinks each ``pid_file`` and hard-exits.

    ``mcp.run()`` runs an asyncio event loop, and asyncio swallows
    ``SystemExit`` raised from a classic ``signal.signal`` handler — the
    integration test in ``test_server_sigterm.py`` is the live repro.
    So we can't rely on ``sys.exit(0)`` + ``atexit``: we unlink
    explicitly and call ``os._exit(0)`` to bypass the event loop.

    Variadic because we track two pid files during the #412 transition
    window: the new ``$XDG_RUNTIME_DIR/memtomem/server.pid`` AND the
    legacy ``~/.memtomem/.server.pid`` (when ``_try_hold_legacy_flock``
    succeeded). Both need the same teardown, or the next server hits
    the "pre-0.1.25 install" abort branch on a stale legacy file
    (issue #437).

    Only register after the flock succeeds, so we never unlink a pid
    file another primary owns. ``atexit`` still handles the normal
    stdin-EOF shutdown path.

    Windows note (#817): Python's ``signal.SIGTERM`` is a no-op on
    Windows — the OS has no equivalent of POSIX SIGTERM that the C
    runtime delivers to the Python signal layer. We skip registration
    entirely on ``os.name == "nt"`` so the call is honest about what it
    does, instead of silently installing a handler that will never
    fire. The ``atexit`` path remains the only teardown route on
    Windows; FastMCP's stdio loop exits via stdin-EOF when the MCP
    host disconnects, which triggers ``atexit`` cleanly.
    """
    import os as _os
    import signal

    def _handle(_signum: int, _frame: object) -> None:
        for pid_file in pid_files:
            try:
                pid_file.unlink(missing_ok=True)
            except OSError:
                pass
        _os._exit(0)

    if _os.name != "nt":
        signal.signal(signal.SIGTERM, _handle)


def _try_hold_legacy_flock(legacy_pid: Path) -> object | None:
    """Acquire a lifetime *shared* flock on the pre-#412 pid file, if present.

    During the transition window a user may still have a v0.1.24 or older
    ``memtomem-server`` running — it holds ``fcntl.flock(LOCK_EX)`` on
    ``~/.memtomem/.server.pid``. The new server's own flock target lives
    on ``$XDG_RUNTIME_DIR``, so without this probe two servers could run
    concurrently against the same SQLite DB and corrupt the WAL (#412
    review B1).

    Lock mode — **shared (``LOCK_SH``), not exclusive**:

    Multiple 0.1.26+ instances can legitimately coexist (e.g. one MCP
    server per Claude Code session across multiple projects — same
    user, same DB, XDG path already warns-and-continues on contention).
    Using ``LOCK_EX`` here would block that (#444). ``LOCK_SH``
    composes with other ``LOCK_SH`` holders but still conflicts with
    ``LOCK_EX``, which is exactly what we need:

    - 0.1.26 ⋈ 0.1.26: both ``LOCK_SH`` → coexist.
    - 0.1.26 after pre-0.1.25: pre-0.1.25 holds ``LOCK_EX``, our
      ``LOCK_SH`` fails → we skip (caller proceeds with a warning).
      The pre-0.1.25 side of the mutex is still enforced by the
      pre-0.1.25 process's own ``LOCK_EX`` check.
    - pre-0.1.25 after 0.1.26: pre-0.1.25 tries ``LOCK_EX``, our
      ``LOCK_SH`` blocks it → pre-0.1.25 exits on its own concurrent-
      detection path. ✓ cross-version protection preserved.

    Behavior:

    - If ``~/.memtomem/`` does not exist, skip — this is a fresh install
      with no upgrade history, and touching it would re-pollute the
      directory that #412 specifically keeps out of handshake.
    - Otherwise, open the legacy path (``a+b`` creates it if missing; we
      are inside an already-existing ``~/.memtomem/`` so no new
      pollution) and try ``LOCK_SH | LOCK_NB``.
    - Lock held exclusively by another process (pre-0.1.25) → log a
      warning and return ``None``. Don't ``sys.exit`` — the XDG path
      below is the authoritative lock for the current generation;
      refusing to start here would be strictly worse UX than a noisy
      concurrent start.
    - Lock acquired → return the file handle; caller holds it for the
      process lifetime so any *future* pre-0.1.25 server starting after
      us hits this shared lock and bails via its own ``LOCK_EX`` attempt.

    Returns ``None`` on the skip paths (fresh install, open error,
    shared-lock acquire failure). The returned fd must stay referenced
    for the lock to persist.

    Windows short-circuit (#817): pre-0.1.25 ``memtomem-server`` was
    Linux-only by construction — the ``mm`` CLI itself didn't load on
    Windows until #652 / 0.1.34, so a pre-0.1.25 Windows server is
    impossible. The whole legacy-flock probe exists only to interlock
    with that hypothetical holder, so on Windows we return ``None``
    immediately. This also sidesteps a real correctness concern:
    portalocker's Windows backend selection (``MsvcrtLocker`` vs
    ``Win32Locker``) does not uniformly implement ``LOCK_SH`` semantics,
    and we don't want to bet the cross-version mutex on backend
    internals.
    """
    import portalocker
    import logging

    if os.name == "nt":
        return None

    log = logging.getLogger(__name__)

    legacy_state_dir = Path.home() / ".memtomem"
    if not legacy_state_dir.is_dir():
        return None

    try:
        legacy_fp = open(legacy_pid, "a+b")
    except OSError:
        return None

    try:
        portalocker.lock(legacy_fp, portalocker.LOCK_SH | portalocker.LOCK_NB)
    except (portalocker.LockException, BlockingIOError, OSError):
        log.warning(
            "Legacy flock at %s is held exclusively (likely a pre-0.1.25 "
            "install). Continuing — if that holder is a pre-0.1.25 "
            "server, concurrent writes may race on the WAL; upgrade all "
            "instances to 0.1.26+.",
            legacy_pid,
        )
        legacy_fp.close()
        return None
    return legacy_fp


def _is_direct_stdio_terminal() -> bool:
    """Return True when stdio mode was launched directly in a terminal."""
    import sys

    return sys.stdin.isatty() and sys.stdout.isatty()


def _print_direct_stdio_help() -> None:
    """Explain why bare stdio server launches exit immediately."""
    print(
        "\n".join(
            [
                "memtomem-server is an MCP stdio server.",
                "",
                "This command is normally launched by an MCP client over stdin/stdout.",
                "Do not run it directly in a terminal.",
                "",
                "Configure your MCP client with:",
                "  command: uvx",
                '  args: ["--from", "memtomem", "memtomem-server"]',
                "",
                "Example:",
                "  claude mcp add memtomem -s user -- uvx --from memtomem memtomem-server",
                "",
                "For a manually started network server, use:",
                "  memtomem-server --transport sse --host 127.0.0.1 --port 8000",
                "  memtomem-server --transport http --host 127.0.0.1 --port 8000",
                "",
                "No MCP client is connected; exiting.",
            ]
        )
    )


def _parse_server_args(argv: list[str] | None = None):
    """Parse ``memtomem-server`` transport options."""
    import argparse

    parser = argparse.ArgumentParser(
        prog="memtomem-server",
        description="Run the memtomem MCP server.",
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http", "http"),
        default="stdio",
        help="MCP transport to use. 'http' is an alias for 'streamable-http'.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Host for sse/http transports.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for sse/http transports.",
    )
    parser.add_argument(
        "--mount-path",
        default=None,
        help="Optional mount path for SSE transport.",
    )
    parser.add_argument(
        "--sse-path",
        default="/sse",
        help="SSE endpoint path for --transport sse.",
    )
    parser.add_argument(
        "--http-path",
        default="/mcp",
        help="Streamable HTTP endpoint path for --transport http.",
    )
    return parser.parse_args(argv)


def _normalize_transport(transport: str) -> str:
    if transport == "http":
        return "streamable-http"
    return transport


def _configure_network_transport(args) -> None:
    mcp.settings.host = args.host
    mcp.settings.port = args.port
    mcp.settings.sse_path = args.sse_path
    mcp.settings.streamable_http_path = args.http_path


def _print_network_server_info(transport: str, args) -> None:
    if transport == "sse":
        path = args.sse_path
        if args.mount_path:
            mount = args.mount_path.rstrip("/")
            path = f"{mount}{path}"
    else:
        path = args.http_path
    print(
        "\n".join(
            [
                "memtomem-server",
                f"Transport: {transport}",
                f"Listening: http://{args.host}:{args.port}{path}",
                "",
                "Press Ctrl+C to stop.",
            ]
        )
    )


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server."""
    import atexit

    import portalocker

    from memtomem._runtime_paths import ensure_runtime_dir, legacy_server_pid_path

    args = _parse_server_args(argv)
    transport = _normalize_transport(args.transport)
    if transport == "stdio" and _is_direct_stdio_terminal():
        _print_direct_stdio_help()
        raise SystemExit(2)
    if transport != "stdio":
        _configure_network_transport(args)
        _print_network_server_info(transport, args)

    # B1: bidirectional mutual exclusion during the transition window.
    # Hold the legacy flock for the process lifetime so an old (pre-#412)
    # server running *now* is detected and a future one starting *after*
    # us also bails.
    legacy_pid_file = legacy_server_pid_path()
    _legacy_lock_fp = _try_hold_legacy_flock(legacy_pid_file)
    if _legacy_lock_fp is not None:
        # POSIX needs unlink-before-close so we delete exactly the inode we
        # own the flock on (issue #437); otherwise the next server's
        # ``_try_hold_legacy_flock`` races against a stale file and reports
        # a phantom "pre-0.1.25 install" holder. Composite cleanup keeps the
        # ordering correct on POSIX and stays Windows-safe in case a future
        # change removes the ``_try_hold_legacy_flock`` Windows short-circuit
        # (#818 review).
        def _legacy_cleanup() -> None:
            if os.name == "nt":
                try:
                    _legacy_lock_fp.close()
                finally:
                    try:
                        legacy_pid_file.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                legacy_pid_file.unlink(missing_ok=True)
                _legacy_lock_fp.close()

        atexit.register(_legacy_cleanup)

    # Runtime files (pid / flock) live on ``$XDG_RUNTIME_DIR/memtomem``
    # when the platform provides one, otherwise a per-user temp subdir.
    # This keeps ``~/.memtomem/`` untouched during MCP handshake — it is
    # created only when persistent storage is first written (#412).
    pid_file = ensure_runtime_dir() / "server.pid"

    # Advisory lock — prevents multiple MCP servers from writing concurrently.
    # The lock is held for the lifetime of the process and auto-released on exit.
    #
    # Mode is ``a+`` (not ``w``): ``open(..., "w")`` truncates the file at
    # open time, *before* we know whether ``flock`` will succeed. When a
    # second server starts while the first is still running, that pre-flock
    # truncate would zero out the live server's pid file — leaving an
    # empty file on disk while the original flock holder keeps running.
    # ``mm uninstall`` then sees ``pid file exists, content empty, flock
    # held`` and reports ``Server still running (pid None)``, which loses
    # the diagnostic value of the recorded pid (and broke ``lsof``-driven
    # debugging). ``a+`` keeps the existing content readable until the lock
    # decision is made; we ``truncate`` + write the pid only after acquiring
    # the lock.
    #
    # ``a+`` (read+write) is also load-bearing for Windows (#817): portalocker's
    # ``MsvcrtLocker`` backend calls ``msvcrt.locking``, which the C runtime
    # rejects on read-only handles with ``EACCES``. ``cli/_liveness.py`` uses
    # ``"rb+"`` for the same reason. Don't simplify this to ``"w"``.
    _lock_fp = open(pid_file, "a+")  # noqa: SIM115
    try:
        portalocker.lock(_lock_fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
    except (portalocker.LockException, BlockingIOError, OSError):
        # Another server already holds the lock — proceed anyway (the editor
        # expects the process to stay alive), but log a warning. Don't register
        # atexit unlink or the SIGTERM handler: either would yank the primary
        # server's pid file out from under it.
        #
        # Exception tuple matches ``cli/_liveness.py:probe_pid_file`` (#817):
        # POSIX raises ``BlockingIOError``; portalocker's Windows backend
        # wraps Win32 errors as ``LockException``. Keep all three explicit so
        # a future reader doesn't narrow this and accidentally swallow the
        # wrong exception.
        _lock_fp.close()
        import logging

        logging.getLogger(__name__).warning(
            "Another instance is already running (pid file: %s). Concurrent writes may be slow.",
            pid_file,
        )
    else:
        _lock_fp.seek(0)
        _lock_fp.truncate()
        _lock_fp.write(str(os.getpid()))
        _lock_fp.flush()

        # Composite cleanup — single atexit registration, platform-aware order
        # (#818 review). Splitting close+unlink across two ``atexit.register``
        # calls relies on LIFO so unlink runs before close, which works on
        # POSIX (you can unlink an open file and the inode persists until
        # close) but breaks on Windows: NTFS refuses to delete an open or
        # locked handle, so a clean shutdown via ``atexit`` would raise
        # ``PermissionError`` (WinError 32) and leave a stale ``server.pid``
        # behind — the next start then misreads it as a live holder.
        def _cleanup() -> None:
            if os.name == "nt":
                # Close → unlock → unlink. The close releases both the
                # file handle and the portalocker lock; the unlink only
                # then succeeds because no handle is open against the path.
                try:
                    _lock_fp.close()
                finally:
                    try:
                        pid_file.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                # POSIX: unlink while still holding the flock so we delete
                # exactly the inode we own; without that, a window opens
                # where another process could ``open`` the same path and
                # we'd close-then-unlink the wrong inode. Closing the fd
                # afterwards releases the flock.
                pid_file.unlink(missing_ok=True)
                _lock_fp.close()

        atexit.register(_cleanup)
        sigterm_targets = [pid_file]
        if _legacy_lock_fp is not None:
            sigterm_targets.append(legacy_pid_file)
        _install_sigterm_handler(*sigterm_targets)

    if transport == "stdio":
        mcp.run()
    elif transport == "sse":
        mcp.run(transport="sse", mount_path=args.mount_path)
    else:
        mcp.run(transport="streamable-http")
