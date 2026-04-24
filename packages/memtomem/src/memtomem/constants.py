"""Cross-cutting constants shared by config, search, MCP tools, and CLI.

These names are exported so a single change point updates every call site
that derives a namespace, builds the default ``system_namespace_prefixes``,
or refers to the shared multi-agent bucket. Keeping them here prevents the
parallel-literal drift that ``feedback_drift_close_must_derive`` warns
against — every importer must derive from these symbols rather than
re-declaring the string.
"""

from __future__ import annotations

from typing import Final

# Multi-agent buckets. The ``agent-runtime:<agent-id>`` namespace is a
# *convenience* isolation boundary, not a security boundary — see the
# multi-agent guide for the threat model.
AGENT_NAMESPACE_PREFIX: Final[str] = "agent-runtime:"
SHARED_NAMESPACE: Final[str] = "shared"

# Provider-ingestion buckets created by ``mm ingest claude-memory`` /
# ``mm ingest gemini-memory`` / ``mm ingest codex-memory``. Re-exported so
# downstream code can derive ``isinstance``-style checks without restating
# the literal.
CLAUDE_MEMORY_NAMESPACE_PREFIX: Final[str] = "claude-memory:"
GEMINI_MEMORY_NAMESPACE_PREFIX: Final[str] = "gemini-memory:"
CODEX_MEMORY_NAMESPACE_PREFIX: Final[str] = "codex-memory:"

# Default ``system_namespace_prefixes`` — namespaces matching any of these
# prefixes are excluded from default ``mem_search`` (``namespace=None``)
# but stay reachable when an explicit namespace is passed. ``archive:`` is
# the auto-archive / auto-consolidate bucket (since Phase A.5);
# ``agent-runtime:`` keeps one agent's private memories from leaking into
# another agent's default search results. Override with
# ``search.system_namespace_prefixes: []`` to restore the pre-multi-agent
# behaviour where every namespace is searchable by default.
_DEFAULT_SYSTEM_PREFIXES: Final[tuple[str, ...]] = (
    "archive:",
    AGENT_NAMESPACE_PREFIX,
)


def default_system_prefixes() -> list[str]:
    """Return a fresh ``list`` of the default system namespace prefixes.

    Pydantic ``Field(default_factory=...)`` requires a callable that yields
    a new list on each model instantiation; sharing a single list would
    leak mutations across instances.
    """

    return list(_DEFAULT_SYSTEM_PREFIXES)
