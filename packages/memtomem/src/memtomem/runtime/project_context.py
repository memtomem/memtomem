"""Project-context resolution for the ADR-0011 scope boundary.

Answers "which registered project root contains the current process?" — the
always-on anchor every read surface threads as ``project_context_root``. Lives
in the runtime layer because all four entry points need it: the MCP tools, the
CLI, the web routes, and the in-process LangGraph adapter. It reads only
configuration and the cwd, so it pulls in no transport.

``memtomem.server.tools.search`` re-exports both names, so no call site had to
move. That leaves three different places a test can patch, and which one works
depends on how the consumer binds the name:

- ``memtomem.runtime.project_context`` — for callers that import from the
  defining module (``integrations/langgraph.py``).
- ``memtomem.server.tools.search`` — for callers that import from the
  re-export *inside a function*, so the lookup happens per call. Most MCP
  tools and ``cli/memory.py`` work this way, and the long-standing patch
  target in the suite is this one.
- the consumer's own module — for callers that bind at import time
  (``cli/search.py``, ``web/routes/{search,chunks,timeline}.py``). Patching
  either module above does not reach these; their local name is already
  resolved.
"""

from __future__ import annotations

import os
from pathlib import Path


def _resolve_project_context_from_dirs(project_memory_dirs) -> Path | None:
    """Same as :func:`_resolve_project_context_root` but takes the dirs
    list directly (no ``app`` / ``comp`` wrapper).

    Used by web routes that have ``Mem2MemConfig`` directly via
    ``get_config`` and by any caller that already extracted the
    registered project tier list. The wrapper :func:`_resolve_project_context_root`
    delegates here so MCP tool callers (``app``) and CLI callers
    (``comp``) keep their existing one-arg signature.
    """
    project_dirs = list(project_memory_dirs)
    if not project_dirs:
        return None
    try:
        cwd = Path(os.getcwd()).resolve()
    except OSError:
        return None
    best_root: Path | None = None
    best_depth = -1
    for d in project_dirs:
        try:
            resolved = Path(d).expanduser().resolve()
        except OSError:
            continue
        # ``resolved`` is expected to be ``<root>/.memtomem/memories``
        # or ``<root>/.memtomem/memories.local``. Project root is
        # grandparent.
        if resolved.parent.name != ".memtomem":
            continue
        project_root = resolved.parent.parent
        try:
            cwd.relative_to(project_root)
        except ValueError:
            continue
        depth = len(project_root.parts)
        if depth > best_depth:
            best_depth = depth
            best_root = project_root
    return best_root


def _resolve_project_context_root(app) -> Path | None:
    """Find the registered project root that contains the current cwd.

    Returns the project root for the current process, or ``None`` if no
    registered project tier covers the current cwd. Used by MCP read
    tools as the always-on context-boundary anchor (ADR-0011 §6) so a
    memtomem server started from inside a project naturally pins memory
    queries to that project's project_shared / project_local rows.

    Resolution: for each ``project_memory_dir`` registered in the user
    config, derive its project root (the grandparent of the
    ``.memtomem/memories[.local]`` entry); if the current cwd lives
    under that root, return it. Multiple matching roots → return the
    deepest match (most specific project context wins for nested
    project layouts).

    Empty ``project_memory_dirs`` → ``None``. Permission errors during
    resolve → ``None``.

    Accepts either ``app`` (MCP) or ``comp`` (CLI) — both expose
    ``.config.indexing.project_memory_dirs`` so the duck-typed access
    is symmetric.
    """
    return _resolve_project_context_from_dirs(app.config.indexing.project_memory_dirs)
