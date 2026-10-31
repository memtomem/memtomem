"""Tool: mem_project_bind (#2920).

A host plugin can start the stdio server outside the user's project — the
Antigravity CLI starts it in the plugin's own directory — and the project
context comes from the working directory, so ``project_local`` never resolves
there. Binding names the project explicitly, once, for the process.

The binding is set-once and never re-checked. A rebind or a clear would let an
operation that spans awaits see two roots, and re-checking registration on
each call would have to answer "unregistered" with ``None`` (every project's
rows under an explicit scope filter) or an exception (raised after a write
already landed). Registration is checked here; after an unregistration the
binding stays until the server restarts, while the write paths that check
registration themselves keep refusing adds to the tier.

The binding is the memory context boundary (ADR-0011 §6) that
``_resolve_project_context_root`` serves. The context gateway's
``context_*`` actions resolve their project separately, from the working
directory, and do not follow it.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.runtime.project_context import _resolve_project_context_from_dirs
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized, _get_server_transport
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_project_bind(project_root: str, ctx: CtxType = None) -> str:
    """Bind this server to a registered project for project-scoped memory.

    For a stdio server started outside the project: project_local and
    project_shared then resolve against this project instead of the
    server's working directory. Once per server process; restart the
    server to bind another project.

    Args:
        project_root: Absolute path inside a project with a registered .memtomem tier.
    """
    transport = _get_server_transport()
    if transport != "stdio":
        return (
            "Error: project_bind works only on a stdio server, where one client owns the "
            f"process (this server's transport: {transport or 'unknown'})."
        )
    # mem_do passes params through unvalidated.
    if not isinstance(project_root, str) or not project_root.strip():
        return "Error: project_root must be a non-empty absolute path."
    given = Path(project_root)
    if not given.is_absolute():
        return f"Error: project_root must be an absolute path, got {project_root!r}."
    try:
        resolved = given.resolve(strict=True)
    except OSError:
        return f"Error: project_root does not exist: {project_root}"
    if not resolved.is_dir():
        return f"Error: project_root is not a directory: {project_root}"

    app = await _get_app_initialized(ctx)
    root = _resolve_project_context_from_dirs(
        app.config.indexing.project_memory_dirs, anchor=resolved
    )
    # No await from here to the assignment, so two concurrent binds cannot both
    # pass the check.
    bound = app.bound_project_root
    if bound is not None:
        # A path in the bound project repeats the binding even after its tier
        # was unregistered, when registration finds nothing (``None``) or the
        # registered parent; only a deeper registered project is a different one.
        if resolved.is_relative_to(bound) and (root is None or bound.is_relative_to(root)):
            return f"Project root already bound: {bound}"
        return (
            f"Error: this server is already bound to {bound}; restart the server to bind "
            f"{root or resolved} instead."
        )
    if root is None:
        return (
            f"Error: no registered project tier covers {resolved}. Register one with: "
            f"cd {resolved} && mm mem init --scope project_local"
        )
    app.bound_project_root = root
    return (
        f"Bound project root: {root}\n"
        "Project-scoped memory now resolves against it for the rest of this server process."
    )
