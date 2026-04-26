"""Cross-cutting constants shared by config, search, MCP tools, and CLI.

These names are exported so a single change point updates every call site
that derives a namespace, builds the default ``system_namespace_prefixes``,
or refers to the shared multi-agent bucket. Keeping them here prevents the
parallel-literal drift that ``feedback_drift_close_must_derive`` warns
against — every importer must derive from these symbols rather than
re-declaring the string.
"""

from __future__ import annotations

import re
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

    Applied at every MCP + CLI + Python-adapter surface that
    concatenates ``AGENT_NAMESPACE_PREFIX`` with caller input.
    Session-start: ``mem_session_start`` (MCP), ``mm session start`` /
    ``mm session wrap`` (CLI),
    ``integrations.langgraph.MemtomemStore.start_agent_session``
    (Python adapter). Multi-agent registration / search:
    ``mem_agent_register`` / ``mem_agent_search`` (MCP), ``mm agent
    register`` (CLI).

    Raises :class:`InvalidNameError` (a ``ValueError`` subclass,
    surfaced by ``tool_handler`` as ``"Error: ..."`` on the MCP path
    and as a ``ClickException`` on the CLI path) when the id contains
    ``:``, ``/``, ``..``, whitespace, control characters, or anything
    outside the canonical ``[A-Za-z0-9._-]`` charset documented above.

    The read/write contract is symmetric across all surfaces listed
    above: an id either works on every surface or fails on every
    surface. Pre-#493 the multi-agent surfaces silently called
    ``sanitize_namespace_segment`` instead, which let hostile shapes
    round-trip into storage under a rewritten namespace while
    ``mem_session_start`` rejected the same shape; pre-#492 the
    LangGraph adapter applied only an ``if not agent_id`` empty-check
    and likewise let malformed values through. See the
    ``Changed (BREAKING)`` entries in ``CHANGELOG.md`` (Unreleased) and
    issues #492 / #493 for the migration notes.
    """

    return validate_name(value, kind="agent-id")


# Namespace charset for caller-supplied ``namespace=`` / ``target=``
# overrides on session-start and ``mem_agent_share``. Covers the existing
# in-tree shapes (``default``, ``shared``, ``archive:summary``,
# ``claude-memory:project-x``, ``agent-runtime:planner``) without adding
# anything that would let an untrusted ``agent_id`` smuggle through the
# override gate. ``,`` is **not** in the charset — comma-joined namespace
# lists are a search-time filter shape, not a storable namespace value;
# letting one through would silently widen scope at write time.
_NAMESPACE_SEGMENT_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_MAX_NAMESPACE_LEN: Final[int] = 256


def validate_namespace(value: object) -> str:
    """Return *value* unchanged if it is a storable namespace string.

    Applied to every public surface that accepts a user-supplied
    ``namespace=`` or ``target=`` argument before storage / search ever
    sees it: ``mem_session_start(namespace=...)`` (MCP),
    ``mm session start --namespace`` (CLI),
    ``MemtomemStore.start_session`` /
    ``MemtomemStore.start_agent_session(namespace=...)`` (Python
    adapter), and ``mem_agent_share(target=...)`` (MCP).

    Internally-derived namespaces (``f"{AGENT_NAMESPACE_PREFIX}{agent_id}"``
    after ``validate_agent_id`` succeeded, ``"default"``,
    ``SHARED_NAMESPACE``) are safe by construction and skip this gate —
    re-validating them is dead weight and would tie the agent_id charset
    to the namespace charset for no benefit. The gate is a forward shield
    on caller input, not a tripwire on internal derivation.

    Accepted shape: one or more ``:``-separated segments where each
    segment is non-empty, matches ``[A-Za-z0-9._-]+``, and is neither
    ``"."``, ``".."``, nor a leading-``-``. Total length capped at 256
    (well above any in-tree namespace today, narrow enough to bound
    storage rows). When the first segment is the bare ``agent-runtime``
    prefix, exactly one further segment is required and that segment is
    re-validated through :func:`validate_agent_id` (which adds the
    1–64-char cap and the rest of the agent_id contract). This is the
    bypass issue #496 was filed to close: a Python / MCP / CLI caller
    could otherwise smuggle ``"agent-runtime:foo:bar"`` into a session
    row even though ``agent_id`` itself is gated.

    The per-segment length cap is intentionally NOT applied uniformly —
    only the ``agent-runtime:`` segment inherits agent_id's 64-char cap.
    Other namespace shapes (``claude-memory:<long-project-id>``) retain
    the broader 256-char total budget so legitimate long project ids
    don't trip this gate.

    Raises :class:`InvalidNameError` (a ``ValueError`` subclass) on
    rejection, mirroring the ``agent_id`` gate's UX so error scrapers
    see one shape across surfaces.
    """

    if not isinstance(value, str):
        raise InvalidNameError(f"invalid namespace: expected str, got {type(value).__name__}")
    if not value or not value.strip():
        raise InvalidNameError(f"invalid namespace {value!r}: empty")
    if len(value) > _MAX_NAMESPACE_LEN:
        raise InvalidNameError(
            f"invalid namespace {value!r}: length {len(value)} exceeds {_MAX_NAMESPACE_LEN}"
        )

    segments = value.split(":")
    for seg in segments:
        if not seg:
            raise InvalidNameError(
                f"invalid namespace {value!r}: empty segment (leading / trailing / consecutive ':')"
            )
        if seg in (".", ".."):
            raise InvalidNameError(
                f"invalid namespace {value!r}: segment {seg!r} is a reserved path token"
            )
        if seg.startswith("-"):
            raise InvalidNameError(
                f"invalid namespace {value!r}: segment {seg!r} has a leading dash"
            )
        if not _NAMESPACE_SEGMENT_RE.fullmatch(seg):
            raise InvalidNameError(
                f"invalid namespace {value!r}: segment {seg!r} must match "
                f"[A-Za-z0-9._-]+ (no slash / backslash / whitespace / control chars)"
            )

    # ``agent-runtime:<agent_id>`` is the one prefix where the second
    # segment is interpolated *back into* an agent_id-shaped storage row
    # — the bypass shape #491 / #494 / #498 closed on the agent_id side.
    # Re-route the segment through ``validate_agent_id`` so the override
    # path can't widen the contract that the direct ``agent_id=`` path
    # already enforces. ``agent-runtime:foo:bar`` lands here as 3
    # segments and trips the strict-arity check before any agent-id
    # validation — that's the canonical shape this gate exists for.
    runtime_prefix = AGENT_NAMESPACE_PREFIX.rstrip(":")
    if segments[0] == runtime_prefix:
        if len(segments) != 2:
            raise InvalidNameError(
                f"invalid namespace {value!r}: ``{runtime_prefix}:`` prefix "
                f"requires exactly one trailing segment "
                f"(``{runtime_prefix}:<agent_id>``); got {len(segments) - 1}"
            )
        try:
            validate_agent_id(segments[1])
        except InvalidNameError as e:
            # Wrap so log scrapers grepping ``"invalid namespace"`` catch
            # this path too — without the wrap, the user passes
            # ``namespace=agent-runtime:<X>`` but sees ``"invalid agent-id
            # ..."``, which splits the alerting surface across two
            # fragments. ``__cause__`` preserves the agent_id detail for
            # anyone debugging the underlying contract violation.
            raise InvalidNameError(
                f"invalid namespace {value!r}: {runtime_prefix} segment {segments[1]!r} "
                f"failed agent-id contract — {e}"
            ) from e

    return value


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
