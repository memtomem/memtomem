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

from memtomem.context._names import InvalidNameError as InvalidNameError
from memtomem.context._names import validate_name

# Multi-agent buckets. The ``agent-runtime:<agent-id>`` namespace is a
# *convenience* isolation boundary, not a security boundary — see the
# multi-agent guide for the threat model. Provider-ingestion namespace
# prefixes (``claude-memory:``, ``gemini-memory:``, ``codex-memory:``)
# stay as locally-defined literals in ``cli/ingest_cmd.py`` for now;
# promoting them here belongs in the same change that wires them up,
# not this PR.
#
# ``agent_id`` is interpolated directly into ``AGENT_NAMESPACE_PREFIX``
# without further escaping, so every entry point that builds an
# agent-runtime namespace MUST first run ``validate_agent_id`` on the
# user-provided id. Accepted charset: ``[A-Za-z0-9._-]`` (1–64 chars,
# no leading dash, not ``"."`` / ``".."``). Rejecting ``:``, ``/``,
# ``..``, whitespace, and control characters keeps malformed-but-stored
# values like ``"agent-runtime:foo:bar"`` from round-tripping into
# storage and search.
AGENT_NAMESPACE_PREFIX: Final[str] = "agent-runtime:"
SHARED_NAMESPACE: Final[str] = "shared"


def validate_agent_id(value: object) -> str:
    """Return *value* unchanged if it is a valid agent identifier.

    Applied at every surface that concatenates ``AGENT_NAMESPACE_PREFIX``
    with caller input. Session-start: ``mem_session_start`` (MCP),
    ``mm session start`` / ``mm session wrap`` (CLI),
    ``integrations.langgraph.MemtomemStore.start_agent_session``
    (Python adapter). Multi-agent registration / search:
    ``mem_agent_register`` / ``mem_agent_search`` (MCP), ``mm agent
    register`` (CLI).

    Raises :class:`InvalidNameError` (a ``ValueError`` subclass,
    surfaced by ``tool_handler`` as ``"Error: ..."`` on the MCP path
    and as a ``ClickException`` on the CLI path) when the id contains
    ``:``, ``/``, ``..``, whitespace, control characters, or anything
    outside the canonical ``[A-Za-z0-9._-]`` charset documented above.

    The read/write contract is symmetric: an id either works on every
    surface or fails on every surface. Pre-#493 the multi-agent surfaces
    silently called ``sanitize_namespace_segment`` instead, which let
    hostile shapes round-trip into storage under a rewritten namespace
    while ``mem_session_start`` rejected the same shape — see issue
    tracker history for the breaking-change note.
    """

    return validate_name(value, kind="agent-id")


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
