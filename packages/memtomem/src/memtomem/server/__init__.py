"""MCP server package — facade and tool registration.

All public symbols are re-exported here for backward compatibility:
    ``from memtomem.server import AppContext, mem_search, mcp, main``
"""

from __future__ import annotations

import contextlib
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from mcp.server.mcpserver import MCPServer

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
from memtomem.server.instructions import build_instructions
from memtomem.server.lifespan import app_lifespan

# Tool mode must be resolved BEFORE the MCPServer instance exists: the
# instructions text has to describe the tool surface this mode actually
# exposes (#1608), and ``instructions=`` is only accepted at construction.
_TOOL_MODE = os.environ.get("MEMTOMEM_TOOL_MODE", "core").lower()

# ── Tool mode: core | standard | full ─────────────────────────────────
# Set MEMTOMEM_TOOL_MODE env var to control which tools are exposed.
#   core     → these tools. Default. mem_do routes to all others.
#   standard → core + frequently used packs as individual tools + mem_do
#   full     → all tools registered individually (no mem_do needed)
#
# Defined above the MCPServer construction below because the instructions
# text renders this set's size; the count in the prose can then never
# drift from the set that is actually exposed.
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

# ── mcp instance — must be created before tool-module imports ──────────
# ``instructions=`` is auto-injected into every MCP client's session as
# the ``initialize`` response's ``instructions`` field — the only
# documentation surface most LLMs see before picking a tool. Source of
# truth lives in ``memtomem/server/instructions.py``; pinned by
# ``tests/test_server_instructions.py``.
#
# ``version=`` pins ``serverInfo.version`` in the ``initialize`` response to
# the memtomem package version (#383). Left unset it is not merely cosmetic:
# 1.x reported ``importlib.metadata.version("mcp")`` — the SDK's own version —
# and 2.0 reports an empty string. Either way, external consumers keying off
# ``serverInfo.version`` (telemetry, error reports, "which version are we both
# on") get something other than ``mm --version``.
from memtomem import __version__ as _memtomem_version

mcp = MCPServer(
    "memtomem",
    instructions=build_instructions(_TOOL_MODE, core_count=len(_CORE_TOOLS)),
    version=_memtomem_version,
    lifespan=app_lifespan,
)

# ── Register ALL tools (decorators bind to `mcp` on import) ───────────
from memtomem.server.tools.ask import mem_ask
from memtomem.server.tools.indexing import mem_index
from memtomem.server.tools.memory_crud import (
    mem_add,
    mem_add_redaction_stats,
    mem_batch_add,
    mem_delete,
    mem_edit,
)
from memtomem.server.tools.recall import mem_recall
from memtomem.server.tools.search import mem_search, mem_expand
from memtomem.server.tools.status_config import (
    mem_config,
    mem_embedding_reset,
    mem_reset,
    mem_stats,
    mem_status,
    mem_version,
)
from memtomem.server.tools.namespace import (
    mem_ns_assign,
    mem_ns_list,
    mem_ns_delete,
    mem_ns_set,
    mem_ns_get,
    mem_ns_rename,
    mem_ns_update,
)
from memtomem.server.tools.dedup_decay import (
    mem_cleanup_orphans,
    mem_dedup_scan,
    mem_dedup_merge,
    mem_decay_scan,
    mem_decay_expire,
)
from memtomem.server.tools.export_import import mem_export, mem_import
from memtomem.server.tools.auto_tag import mem_auto_tag
from memtomem.server.tools.browse import mem_list, mem_read
from memtomem.server.tools.tag_management import (
    mem_tag_list,
    mem_tag_rename,
    mem_tag_delete,
    mem_tag_merge,
)
from memtomem.server.tools.url_index import mem_fetch
from memtomem.server.tools.cross_ref import mem_link, mem_unlink, mem_related
from memtomem.server.tools.session import mem_session_start, mem_session_end, mem_session_list
from memtomem.server.tools.scratch import mem_scratch_set, mem_scratch_get, mem_scratch_promote
from memtomem.server.tools.procedure import mem_procedure_save, mem_procedure_list
from memtomem.server.tools.pinned import (
    mem_context_compose,
    mem_pinned_delete,
    mem_pinned_get,
    mem_pinned_list,
    mem_pinned_set,
)
from memtomem.server.tools.formation import (
    mem_candidate_evidence,
    mem_candidate_list,
    mem_candidate_recover,
    mem_candidate_propose,
    mem_candidate_review,
    mem_formation_scan,
)
from memtomem.server.tools.multi_agent import mem_agent_register, mem_agent_search, mem_agent_share
from memtomem.server.tools.evaluation import mem_eval
from memtomem.server.tools.consolidation import mem_consolidate, mem_consolidate_apply
from memtomem.server.tools.reflection import mem_reflect, mem_reflect_save
from memtomem.server.tools.search_history import (
    mem_search_feedback,
    mem_search_history,
    mem_search_suggest,
)
from memtomem.server.tools.quality import mem_quality_replay
from memtomem.server.tools.conflict import mem_conflict_check
from memtomem.server.tools.importance import mem_importance_scan
from memtomem.server.tools.importers import mem_import_notion, mem_import_obsidian
from memtomem.server.tools.entity import mem_entity_scan, mem_entity_search
from memtomem.server.tools.temporal import mem_timeline, mem_activity
from memtomem.server.tools.policy import (
    mem_policy_add,
    mem_policy_list,
    mem_policy_delete,
    mem_policy_run,
)
from memtomem.server.tools.context import (
    mem_context_detect,
    mem_context_init,
    mem_context_generate,
    mem_context_diff,
    mem_context_sync,
    mem_context_memory_migrate,
    mem_context_artifact_migrate,
    mem_context_artifact_transfer,  # ADR-0023 A-13 — cross-project/tier copy+move
    mem_context_version,  # ADR-0022 PR2 — version snapshots (list/create)
    mem_context_promote,  # ADR-0022 PR2 — label pointers (promote/delete)
    mem_context_pull,  # ADR-0030 PR-H — Pull a runtime artifact into the Store
)
from memtomem.server.tools.ingest import (
    mem_ingest,  # no @mcp.tool; import triggers @register("ingest") for mem_do routing
)
from memtomem.server.tools.watchdog import mem_watchdog
from memtomem.server.tools.schedule import (
    mem_schedule_delete,
    mem_schedule_list,
    mem_schedule_register,
    mem_schedule_run_now,
)
from memtomem.server.tools.meta import mem_do
import memtomem.server.resources  # side-effect import: registers MCP resources

# Action categories exposed as individual tools in ``standard`` mode.
# Module-level so tests (instructions/pruning pins) can derive the
# expected tool set from the same source the pruning uses.
_STANDARD_PACKS = frozenset(
    {
        "crud",
        "namespace",
        "tags",
        "sessions",
        "scratch",
        "relations",
        "schedule",
    }
)


def _registered_tool_names() -> frozenset[str]:
    """Enumerate registered tool names via MCPServer's public-ish surface.

    ``MCPServer`` exposes ``remove_tool`` publicly but has no *synchronous*
    public tool listing (``MCPServer.list_tools`` is async and yields wire
    objects), so we call the tool manager's synchronous ``list_tools``.
    Both that method and ``MCPServer.remove_tool`` (used below) are the
    coupling points to SDK internals — pinned by
    ``tests/test_tool_mode_pruning.py``. Guard the shape explicitly and
    raise if it changes: a silent failure here would ship every tool in
    ``core`` mode instead of pruning to 9 (#1609).
    """
    manager = getattr(mcp, "_tool_manager", None)
    lister = getattr(manager, "list_tools", None)
    if manager is None or not callable(lister):
        raise RuntimeError(
            "MCPServer tool-manager API changed: cannot enumerate registered tools "
            "for MEMTOMEM_TOOL_MODE pruning. Pin the mcp dependency or update the "
            "tool-mode pruning in memtomem/server/__init__.py."
        )
    return frozenset(t.name for t in lister())


# Snapshot of every tool registered by the imports above, taken BEFORE
# pruning — the only place the full surface is still observable once a
# non-full mode has pruned ``mcp``. Used by the per-mode pin tests.
_ALL_REGISTERED_TOOLS = _registered_tool_names()

if _TOOL_MODE != "full":
    if _TOOL_MODE == "standard":
        from memtomem.server.tool_registry import ACTIONS

        _allowed = _CORE_TOOLS | {
            f"mem_{name}" for name, info in ACTIONS.items() if info.category in _STANDARD_PACKS
        }
    else:
        _allowed = _CORE_TOOLS
    # ``MCPServer.remove_tool`` is the public removal API; raise rather
    # than silently no-op if a future release drops it, so pruning can't
    # fail open to full mode.
    if not callable(getattr(mcp, "remove_tool", None)):
        raise RuntimeError(
            "MCPServer.remove_tool is unavailable: cannot prune tools for "
            "MEMTOMEM_TOOL_MODE. Pin the mcp dependency or update "
            "memtomem/server/__init__.py."
        )
    for name in _ALL_REGISTERED_TOOLS:
        if name not in _allowed:
            mcp.remove_tool(name)


@contextlib.contextmanager
def _sigterm_deferred():
    """Hold SIGTERM across creating a runtime file and covering it.

    Between the moment a file exists on disk and the moment the SIGTERM
    handler knows to unlink it, a signal kills the process by default
    disposition and leaves the file behind. The window is short but real, and
    it is the whole reason the handler exists — so the two steps are made
    indivisible rather than made fast. Blocking, not ignoring: a SIGTERM that
    arrives here is delivered the instant the mask lifts, so this defers
    shutdown by microseconds and never swallows it.

    The previous mask is *restored*, not unblocked: an embedder may have
    entered with SIGTERM already blocked on this thread, and unblocking
    unconditionally would both hand back a mask the caller never chose and
    deliver a signal it was deliberately deferring.

    A no-op where the primitive is absent (Windows has no ``pthread_sigmask``,
    and no SIGTERM delivery to defer, #817).
    """
    import signal

    mask = getattr(signal, "pthread_sigmask", None)
    if mask is None or os.name == "nt":
        yield
        return
    previous = mask(signal.SIG_BLOCK, {signal.SIGTERM})
    try:
        yield
    finally:
        mask(signal.SIG_SETMASK, previous)


def _sigterm_targets(pid_file: Path | None, presence: object) -> tuple[Path, ...]:
    """Runtime files this process owns and must unlink on SIGTERM.

    ``presence`` is the :class:`~memtomem._instance_registry.RegisteredInstance`
    returned by the startup marker registration, or ``None`` when it did not
    register — the caller passes the value through rather than deciding, so the
    "which files do we own" question has one answer in one place.
    """
    targets: list[Path] = []
    if pid_file is not None:
        targets.append(pid_file)
    path = getattr(presence, "path", None)
    if isinstance(path, Path):
        targets.append(path)
    return tuple(targets)


def _install_sigterm_handler(*paths: Path | list[Path]) -> None:
    """Install a SIGTERM handler that unlinks ``paths`` and hard-exits.

    ``mcp.run()`` runs an asyncio event loop, and asyncio swallows
    ``SystemExit`` raised from a classic ``signal.signal`` handler — the
    integration test in ``test_server_sigterm.py`` is the live repro.
    So we can't rely on ``sys.exit(0)`` + ``atexit``: we unlink
    explicitly and call ``os._exit(0)`` to bypass the event loop.

    Variadic because the process can own more than one runtime file. It
    was variadic once before, to tear down the legacy
    ``~/.memtomem/.server.pid`` compatibility lock during the #412
    transition window (retired in #2003); today the callers pass the
    scoped pid file, the startup presence marker (#2230), or — on the
    lock-contended path, which owns no pid file — the marker alone.
    Every path passed must be one this process created: each is unlinked
    unconditionally, with no ownership re-check, because a signal handler
    cannot take the registry's mutation lock (it may be interrupting a
    thread that holds it) and the marker's per-registration nonce already
    makes its name unrepeatable, so there is no other inode to hit.

    Pass the pid file only after its flock succeeds, so we never unlink
    one another primary owns. ``atexit`` still handles the normal
    stdin-EOF shutdown path.

    What ``os._exit(0)`` intentionally abandons (#1574 item 8): the
    ``app_lifespan`` ``finally`` (``server/lifespan.py``) never runs on
    SIGTERM, so the webhook drain, the watcher / scheduler / policy /
    health-watchdog stops, and ``AppContext.close()``'s DB teardown —
    including the ``PRAGMA wal_checkpoint(TRUNCATE)`` in
    ``SqliteBackend.close()`` — are all skipped. That is deliberate:
    every one of those is an async teardown that must run on the event
    loop, and a signal handler cannot safely enter the loop it is
    interrupting. The consequences are bounded and self-healing — SQLite
    recovers the WAL on the next open (committed transactions are
    durable; the ``-wal``/``-shm`` files just persist until then),
    watchdog observer threads die with the process, and undelivered
    webhooks are already best-effort. Only the pid-file unlink above is
    performed. If a synchronous best-effort DB close is ever added here,
    it must not touch asyncio state.

    The startup presence marker (#2230) is unlinked here, like the pid
    file and for the same reason: ``atexit`` never runs, so nothing else
    would. A store *sentinel* is still abandoned — it is published from
    the async initialization path and released through the registry's
    mutation lock, which this handler must not take. That residue is
    bounded: the kernel drops the flock at exit, so the sentinel probes
    *stale* immediately, no reader counts it as a live server, and the
    next registration's sweep collects it once past the grace window.

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
        # A list is a live target set used during startup: marker paths are
        # appended before their files are created and the pid path only after
        # this process owns its lock.  This closes the create-before-handler
        # window without ever risking deletion of another process's pid file.
        targets = (path for item in paths for path in (item if isinstance(item, list) else [item]))
        for path in targets:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                # One unusable path must not strand the others, and the
                # hard exit below has to happen either way.
                pass
        _os._exit(0)

    if _os.name != "nt":
        signal.signal(signal.SIGTERM, _handle)


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
                f'  args: ["--isolated", "--from", "memtomem[all]=={_memtomem_version}", "memtomem-server"]',
                "",
                "Example:",
                "  claude mcp add memtomem -s user -- uvx --isolated "
                f"--from 'memtomem[all]=={_memtomem_version}' memtomem-server",
                "",
                "For a manually started network server, use:",
                "  memtomem-server --transport sse --host 127.0.0.1 --port 8000 --url http://127.0.0.1:8000/sse",
                "  memtomem-server --transport http --host 127.0.0.1 --port 8000 --url http://127.0.0.1:8000/mcp",
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
        epilog=(
            "Security: sse/http transports have no first-party authentication; "
            "treat them as trusted-network only and place an authenticated reverse "
            "proxy in front before exposing publicly."
        ),
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
        help=(
            "Local host/interface to listen on for sse/http transports, usually "
            "127.0.0.1 behind nginx or 0.0.0.0 for trusted direct network access."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for sse/http transports.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Full MCP endpoint URL clients connect to, e.g. https://example.com/mcp. "
            "The URL path is also used as this server's endpoint path; reverse "
            "proxies should forward that path unchanged."
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Advanced: extra allowed Host header for sse/http transports. Repeatable.",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="Advanced: extra allowed Origin header for sse/http transports. Repeatable.",
    )
    parser.add_argument(
        "--disable-dns-rebinding-protection",
        action="store_true",
        help=(
            "Advanced/dangerous: disables DNS rebinding protection; only safe "
            "behind an authenticated reverse proxy."
        ),
    )
    return parser.parse_args(argv)


def _normalize_transport(transport: str) -> str:
    if transport == "http":
        return "streamable-http"
    return transport


def _default_network_url(transport: str, host: str, port: int) -> str:
    path = "/sse" if transport == "sse" else "/mcp"
    display_host = "127.0.0.1" if host == "0.0.0.0" else host
    return f"http://{display_host}:{port}{path}"


def _normalize_endpoint_url(url: str) -> str:
    from urllib.parse import urlparse

    normalized = url.rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--url must be a full http(s) URL, e.g. https://example.com/mcp")
    if not parsed.path or parsed.path == "/":
        raise SystemExit("--url must include an endpoint path, e.g. https://example.com/mcp")
    return normalized


def _split_sse_url_path(path: str) -> tuple[str | None, str]:
    mount, _, endpoint = path.rstrip("/").rpartition("/")
    return (mount or None), f"/{endpoint}"


def _origin_from_url(url: str) -> str:
    from urllib.parse import urlparse

    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"


def _host_patterns(host: str | None) -> list[str]:
    if not host or host in {"0.0.0.0", "::"}:
        return []
    if ":" in host and not host.startswith("["):
        return [f"[{host}]", f"[{host}]:*"]
    return [host, f"{host}:*"]


def _configure_network_transport(args, transport: str) -> tuple[str, dict[str, Any]]:
    """Build the ``mcp.run()`` keyword arguments for a network transport.

    The 2.0 SDK moved host/port/paths/transport-security off the server
    instance: ``MCPServer.settings`` no longer carries them, and
    ``run(transport=..., **kwargs)`` forwards straight to
    ``run_sse_async`` / ``run_streamable_http_async``. So this returns the
    kwargs instead of mutating global state, which also means a failed
    parse leaves nothing half-applied.
    """
    from urllib.parse import urlparse

    from mcp.server.transport_security import TransportSecuritySettings

    public_url = _normalize_endpoint_url(
        args.url or _default_network_url(transport, args.host, args.port)
    )
    parsed = urlparse(public_url)

    run_kwargs: dict[str, Any] = {"host": args.host, "port": args.port}
    if transport == "sse":
        # 1.x took a separate ``mount_path`` and prefixed both the SSE route
        # and the advertised message endpoint with it. 2.0 has no
        # ``mount_path``: ``sse_app`` registers ``sse_path`` and
        # ``message_path`` verbatim, so the prefix is folded into both here.
        # ``_split_sse_url_path`` returns ``None`` for a single-segment path
        # like ``/sse``, hence the normalization to "".
        mount_path, endpoint_path = _split_sse_url_path(parsed.path)
        prefix = mount_path or ""
        run_kwargs["sse_path"] = f"{prefix}{endpoint_path}"
        run_kwargs["message_path"] = f"{prefix}/messages/"
    else:
        run_kwargs["streamable_http_path"] = parsed.path

    if args.disable_dns_rebinding_protection:
        # Pin the allow-lists to empty even though the SDK's current default
        # is `[]` — relying on that default means a future upstream change
        # could silently widen what we accept when DNS rebinding protection
        # is off. Pass them explicitly so the contract here is local.
        run_kwargs["transport_security"] = TransportSecuritySettings(
            enable_dns_rebinding_protection=False,
            allowed_hosts=[],
            allowed_origins=[],
        )
        return public_url, run_kwargs

    allowed_hosts = [
        "127.0.0.1",
        "127.0.0.1:*",
        "localhost",
        "localhost:*",
        "[::1]",
        "[::1]:*",
        *(_host_patterns(parsed.hostname)),
        *(_host_patterns(args.host)),
        *args.allowed_host,
    ]
    allowed_origins = [_origin_from_url(public_url), *args.allowed_origin]
    run_kwargs["transport_security"] = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(dict.fromkeys(allowed_hosts)),
        allowed_origins=list(dict.fromkeys(allowed_origins)),
    )
    return public_url, run_kwargs


def _internal_network_url(args, transport: str, run_kwargs: dict[str, Any]) -> str:
    if transport == "sse":
        path = run_kwargs["sse_path"]
    else:
        path = run_kwargs["streamable_http_path"]
    return f"http://{args.host}:{args.port}{path}"


def _print_network_server_info(
    transport: str, args, public_url: str, run_kwargs: dict[str, Any]
) -> None:
    transport_label = "http (streamable-http)" if args.transport == "http" else transport
    internal_url = _internal_network_url(args, transport, run_kwargs)
    lines = [
        "memtomem-server",
        f"Transport: {transport_label}",
        f"Internal URL: {internal_url}",
        f"Public URL:   {public_url}",
        "",
        # Mirror the ``--help`` epilog (see ``_parse_server_args``) at bind
        # time: network transports ship no first-party MCP authentication
        # (stance recorded in ADR-0029). Print this unconditionally for every
        # network bind so the no-auth posture is visible even to an operator
        # who never reads ``--help`` before exposing the port.
        "Security: no first-party authentication on this transport. Treat it as",
        "          trusted-network only and put an authenticated reverse proxy in",
        "          front before exposing it publicly (see SECURITY.md).",
        "",
        "Reverse proxy note:",
        "  Forward the public URL path unchanged to the internal URL path.",
    ]
    # ``--host 0.0.0.0`` binds on every interface but ``_host_patterns``
    # returns ``[]`` for the wildcard, so the DNS-rebinding allow-list only
    # contains loopback unless ``--url`` carries a non-loopback hostname.
    # The hint recommends only ``--url`` because the MCP SDK does exact
    # ``Host`` matching unless an allow-list entry ends in ``:*`` (see
    # ``mcp/server/transport_security.py``); ``--url`` derives both the
    # ``:*`` wildcard *and* the matching ``Origin`` automatically, while
    # a bare ``--allowed-host <reachable-host>`` does not match the
    # typical ``Host: <reachable-host>:<port>`` header and still leaves
    # Origin-bearing clients blocked.
    #
    # Only emit the hint when the user has not signalled they intend an
    # advanced configuration. ``--disable-dns-rebinding-protection``
    # skips Host/Origin checks entirely; explicit ``--allowed-host`` or
    # ``--allowed-origin`` values mean the user has already authorized
    # additional headers. In those cases the "only loopback ... accepted"
    # message is wrong, so suppress it rather than mislead.
    hint_applies = (
        args.host in {"0.0.0.0", "::"}
        and args.url is None
        and not args.disable_dns_rebinding_protection
        and not args.allowed_host
        and not args.allowed_origin
    )
    if hint_applies:
        lines.extend(
            [
                "",
                f"Note: bound on {args.host} but only loopback Host/Origin headers are",
                "      accepted. Pass --url http://<reachable-host>:<port>/... for",
                "      LAN clients (auto-populates the Host and Origin allow-lists).",
            ]
        )
    lines.extend(["", "Press Ctrl+C to stop."])
    print("\n".join(lines))


def _resolve_store_db_path() -> Path | None:
    """Best-effort store resolution for pid-file naming (#1990).

    Mirrors the effective config the lazy context will later open: dotenv
    first (the lifespan loads it before building ``Mem2MemConfig``, so the
    pid digest must see the same ``MEMTOMEM_STORAGE__SQLITE_PATH``), then
    the same env → config.d → overrides layering as the component factory —
    but with ``migrate=False``: pid naming is a read-only concern and must
    not rewrite ``config.json``. If a pending migration would change
    ``storage.sqlite_path``, the digest is stale for exactly one server run
    (equivalent to today's store-blind name), after which the persisted
    config converges. The same bound applies when ``config.json`` /
    ``config.d`` are edited after startup: the lazy context re-reads them
    at first tool call, so the pid name can lag the opened store for this
    run only — destructive CLI paths stay protected by the DB-lock probe
    and the instance registry regardless. Any failure returns ``None``
    and the caller falls back to the transitional bare ``server.pid``.
    """
    try:
        from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides
        from memtomem.server.lifespan import _load_dotenv

        _load_dotenv()
        cfg = Mem2MemConfig()
        load_config_d(cfg, quiet=True)
        load_config_overrides(cfg, migrate=False)
        return Path(cfg.storage.sqlite_path)
    except Exception:
        return None


def main(argv: list[str] | None = None) -> None:
    """Run the MCP server."""
    import atexit

    import portalocker

    from memtomem._runtime_paths import ensure_runtime_dir, server_pid_path

    # Capture this before configuration and lock setup. ``mm upgrade`` uses
    # the timestamp written beside the pid to distinguish a process that
    # began before the package swap from one launched after it completed.
    process_started = datetime.now(UTC).isoformat()

    args = _parse_server_args(argv)
    transport = _normalize_transport(args.transport)
    # These informational banners are safe on stdout: the stdio TTY guard exits
    # before mcp.run(), and network transports do not use stdout as the MCP stream.
    if transport == "stdio" and _is_direct_stdio_terminal():
        _print_direct_stdio_help()
        raise SystemExit(2)
    if transport != "stdio":
        # Resolve the transport kwargs up front so a bad ``--url`` fails
        # before the pid-lock dance, but defer the user-facing banner until
        # after the pid-lock decision: printing "Press Ctrl+C to stop" before
        # discovering the lock is held contradicts the "another instance is
        # already running" warning that the lock-contention branch logs a
        # moment later.
        public_url, run_kwargs = _configure_network_transport(args, transport)
    else:
        public_url = None
        run_kwargs = {}

    # The transition-era B1 interlock on ``~/.memtomem/.server.pid`` was
    # retired in #2003: pre-0.1.25 servers (Linux-only, live 2026-04-22..24)
    # are the only holders it ever mutually excluded, and every modern
    # server is gated by its own ``server[-<digest>].pid`` below. The CLI
    # liveness probes still fail closed on an *exclusive* legacy holder,
    # and ``mm uninstall`` still inventories the file for cleanup.

    # Runtime files (pid / flock) use one environment-independent per-user
    # anchor (#2037): literal ``/tmp/memtomem-<euid>`` on POSIX and the
    # LocalAppData Known Folder's Temp child on Windows. This keeps
    # ``~/.memtomem/`` untouched during MCP handshake — it is created only
    # when persistent storage is first written (#412).
    # The name is scoped to the resolved store (#1990) so servers on
    # different databases don't contend for one per-user lock; when the
    # store can't be resolved the bare transitional name keeps today's
    # fail-closed behavior. The file stays anchored to the exact directory
    # ``ensure_runtime_dir`` validated and created, preserving one resolution
    # and the path seam used by tests and embedders.
    db_path = _resolve_store_db_path()
    pid_file = ensure_runtime_dir() / server_pid_path(db_path).name

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
    # SIGTERM is held for this whole span (#2230). Each file below exists on
    # disk before the handler that unlinks it is installed, and a signal
    # landing in that gap kills the process by default disposition and leaves
    # the file behind — the residue the handler exists to prevent. Deferring
    # delivery until the handler covers both targets makes create-and-cover
    # indivisible instead of merely quick.
    with _sigterm_deferred():
        # Install the process-wide handler before any presence marker can be
        # published.  ``register_server_presence`` reserves its unique path in
        # this mutable list immediately before opening it; the pid path is
        # added only after the exclusive lock proves ownership.
        _sigterm_cleanup_targets: list[Path] = []
        _install_sigterm_handler(_sigterm_cleanup_targets)
        # Record this process in the instance registry's startup population
        # (#2230), before the pid-lock decision because it is independent of it:
        # the ``server[-<digest>].pid`` file is exclusive per store, so the
        # contended arm below keeps no pid record at all — yet a second server on
        # one store is exactly the accumulation this marker exists to make
        # visible. It is also the only registration a handshake-only session ever
        # performs, since the store-scoped sentinel's digest is the DB file's
        # inode identity and no store is open yet.
        #
        # Best-effort by contract, and reported: the registry returns ``None``
        # rather than raising for every ordinary failure (an untrusted directory,
        # a lock timeout, a contended sentinel), so the warning keys off the
        # return value, not off an exception. A server that cannot be counted
        # must still start.
        import logging

        _presence = None
        try:
            from memtomem._instance_registry import register_server_presence

            _presence = register_server_presence(
                db_path,
                on_path_reserved=_sigterm_cleanup_targets.append,
            )
        except Exception:  # pragma: no cover - defensive; the registry swallows its own
            # Only an import-time failure reaches here; the traceback is the
            # single piece of information the warning below cannot carry, so
            # it is logged rather than dropped.
            logging.getLogger(__name__).debug(
                "presence registration could not be attempted", exc_info=True
            )
        if _presence is None:
            logging.getLogger(__name__).warning(
                "Could not record this server in the instance registry "
                "(runtime dir: %s); `mm doctor` may under-report running servers.",
                pid_file.parent,
            )

        _lock_fp = open(pid_file, "a+")
        try:
            portalocker.lock(_lock_fp, portalocker.LOCK_EX | portalocker.LOCK_NB)
        except (portalocker.LockException, BlockingIOError, OSError) as exc:
            from memtomem._lock_errors import is_lock_contention, raise_lock_io_failure

            if not is_lock_contention(exc):
                _lock_fp.close()
                if _presence is not None:
                    _presence.cleanup()
                raise_lock_io_failure(exc, pid_file, label="server pid")
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
                "Another memtomem-server is already writing to this store (pid file: %s). "
                "Concurrent writes may be slow.",
                pid_file,
            )
        else:
            _sigterm_cleanup_targets.append(pid_file)
            _lock_fp.seek(0)
            _lock_fp.truncate()
            # Keep the pid on the first line for old readers. The blank second
            # line is the optional Web-UI port slot understood by
            # ``cli._liveness._parse_pid_payload``; the third line records this
            # process generation's UTC start time.
            _lock_fp.write(f"{os.getpid()}\n\n{process_started}\n")
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

    if transport != "stdio":
        # Banner runs after the lock decision so the warning log (if any)
        # and the "Press Ctrl+C to stop" line stay consistent with reality.
        assert public_url is not None  # narrowed by the configure branch above
        _print_network_server_info(transport, args, public_url, run_kwargs)

    # ``run`` forwards these kwargs to the matching async runner, and its
    # overloads key off a literal transport — hence the explicit branches
    # rather than one call with a ``str`` variable.
    if transport == "stdio":
        mcp.run()
    elif transport == "sse":
        mcp.run(transport="sse", **run_kwargs)
    else:
        mcp.run(transport="streamable-http", **run_kwargs)
