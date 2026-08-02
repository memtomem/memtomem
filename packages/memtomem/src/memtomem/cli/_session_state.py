"""CLI session state — the active-session file and the write scope it implies.

Split out of ``cli/session_cmd.py`` so CLI writers (``mm add``) can resolve
the active session's agent scope without importing the ``mm session``
command module. Keep this module's imports to stdlib + ``memtomem.constants``
so it stays free to import from anywhere under ``cli/``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    InvalidNameError,
    normalize_bound_agent_id,
)

logger = logging.getLogger(__name__)


# Session state file — stores active session UUID.
def _state_dir() -> Path:
    """Return the memtomem state directory, resolving HOME at call time."""
    return Path.home() / ".memtomem"


def _state_file() -> Path:
    """Return the path to the current-session state file (lazy — resolves HOME at call time)."""
    return _state_dir() / ".current_session"


def _read_current_session() -> str | None:
    """Read the active session ID from the state file, or None."""
    try:
        text = _state_file().read_text(encoding="utf-8").strip()
        return text if text else None
    except FileNotFoundError:
        return None


def _write_current_session(session_id: str) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    _state_file().write_text(session_id + "\n", encoding="utf-8")


def _clear_current_session() -> None:
    try:
        _state_file().unlink()
    except FileNotFoundError:
        pass


async def resolve_session_write_namespace(storage: Any) -> str | None:
    """Return the namespace a CLI write inherits from the active session.

    The CLI mirror of ``server.tools.multi_agent._resolve_agent_namespace``
    (#1991). Each ``mm`` invocation is a fresh process, so where MCP reads
    ``AppContext.current_agent_id`` this reads the session id parked in
    ``~/.memtomem/.current_session`` and re-derives the binding from that
    session's row. The MCP chain's third step (``app.current_namespace``,
    the pre-multi-agent fallback) has no CLI equivalent and is simply
    absent; ``None`` means "un-pinned", which leaves the indexing engine's
    namespace rules / auto-NS / config default in charge exactly as before.

    Routing keys off the row's ``agent_id``, **not** its ``namespace``
    column, because that is what MCP does: a session started with an
    explicit ``--namespace`` but no agent binds no agent and must not
    redirect writes. The ``"default"`` sentinel is collapsed to "unbound"
    by :func:`normalize_bound_agent_id`, the single producer of that rule
    (#1875).

    Degrades to ``None`` — never raises — for every "no usable session"
    shape: no state file, a state file that cannot be read as text (it
    has been replaced by a directory, or holds invalid UTF-8), a row that
    no longer exists, a row already ended (``mm session end`` clears the
    state file, but ``--auto-end-stale`` ends rows without touching it,
    so the file can outlive its session), or an ``agent_id`` corrupted
    out of band. Losing the agent scope is the right cost for unusable
    session state; failing the write is not, and this is the only caller
    of ``_read_current_session`` on a write path. This is also a read
    path, so it does not clean up a stale state file: mutating it belongs
    to ``mm session start`` / ``mm session end``.
    """
    try:
        session_id = _read_current_session()
    except (OSError, UnicodeError):
        logger.warning(
            "Could not read %s; writing without a session-derived namespace.",
            _state_file(),
        )
        return None
    if not session_id:
        return None
    row = await storage.get_session(session_id)
    if row is None or row.get("ended_at") is not None:
        return None
    try:
        bound_agent_id = normalize_bound_agent_id(row.get("agent_id"))
    except InvalidNameError:
        logger.warning(
            "Session %s has an unusable agent_id; writing without a session-derived namespace.",
            session_id,
        )
        return None
    if not bound_agent_id:
        return None
    return f"{AGENT_NAMESPACE_PREFIX}{bound_agent_id}"
