"""SQL twin of :func:`memtomem.search.visibility.neighbor_visible` (#2237).

Context-window expansion used to read a whole source file and decide
visibility in Python. That does not survive a file with more chunks than the
listing cap, so the *counts* it reports — the anchor's ordinal and the file's
visible total — are computed in SQL instead, over rows that are never
materialised. This module emits the WHERE fragment those counts aggregate on.

It is a twin, in the same sense as ``NamespaceFilter.matches`` ↔
:func:`memtomem.storage.sqlite_helpers.namespace_sql` and
``chunk_in_scope_boundary`` ↔
:func:`memtomem.storage.sqlite_scope.scope_context_sql`: the Python predicate
stays authoritative for which neighbours are *shown* (a disagreement there
would leak a hidden chunk, while a disagreement in the counts only misreports
an ordinal), and ``test_context_window_storage.py`` executes both sides
against the same value matrix so they cannot drift apart.

Note what this is **not**: ``scope_context_sql`` narrows a search, whereas
``_scope_visible`` widens the always-on boundary and only when the caller is
out-of-project. The scope term below is built from the boundary rule plus
that widening, not from ``scope_context_sql``'s fragment.
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from memtomem.models import (
    CONSOLIDATED_SUFFIX,
    ORIGIN_CONSOLIDATION_POLICY,
    NamespaceFilter,
    ScopeFilter,
)
from memtomem.storage.base import StorageBackend
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD

from .sqlite_helpers import escape_like
from .sqlite_scope import _scopes_glob_clause, _scopes_in_clause


# A missing source is retained for recovery but hidden from ranked search,
# entity search, and maintenance discovery. ID-addressed reads still reach
# chunks under an availability hold until an explicit purge; a redaction hold
# is the exception, see ``_not_withheld_sql`` below.
def _visible_source_sql(alias: str) -> str:
    # Only the two fixed table spellings below reach this SQL fragment.
    if alias not in {"c", "chunks"}:
        raise ValueError("unsupported source visibility table alias")
    source = f"{alias}.source_file"
    original = f"substr({source}, 1, length({source}) - length('{CONSOLIDATED_SUFFIX}'))"
    direct = (
        f"NOT EXISTS (SELECT 1 FROM held_sources h WHERE h.source_file={source}) "  # nosec B608
        f"AND NOT EXISTS (SELECT 1 FROM pending_source_checks p WHERE p.source_file={source})"
    )
    derived = (
        f"NOT EXISTS (SELECT 1 FROM held_sources h WHERE h.source_file={original}) "  # nosec B608
        f"AND NOT EXISTS (SELECT 1 FROM pending_source_checks p WHERE p.source_file={original})"
    )
    return (
        f"({direct} AND ({alias}.origin IS NULL OR {alias}.origin <> "
        f"'{ORIGIN_CONSOLIDATION_POLICY}' OR ({derived})))"  # nosec B608
    )


VISIBLE_SOURCE_C = _visible_source_sql("c")
VISIBLE_SOURCE_CHUNKS = _visible_source_sql("chunks")


# The one hold that also applies to chunks reached by id (#2777). An
# availability hold says only that the file cannot be seen right now, so its
# chunks stay readable for recovery. A redaction hold is about the content:
# the guard refused the file's current state, and the retained chunks can
# carry text that same edit deleted. A policy summary derived from such a
# source is withheld with it, the way ``_visible_source_sql`` hides it with
# any hold. It belongs in the statement that reads the row (``get_chunk``,
# ``get_chunks_batch``, the context-window seek), not in a check beside it.
def _not_withheld_sql(alias: str) -> str:
    # Only the two fixed table spellings below reach this SQL fragment.
    if alias not in {"c", "chunks"}:
        raise ValueError("unsupported source visibility table alias")
    source = f"{alias}.source_file"
    original = f"substr({source}, 1, length({source}) - length('{CONSOLIDATED_SUFFIX}'))"

    def withheld(path: str) -> str:
        return (
            f"EXISTS (SELECT 1 FROM held_sources w WHERE w.source_file={path} "  # nosec B608
            f"AND w.reason='{REDACTION_BLOCKED_HOLD}')"
        )

    return (
        f"(NOT {withheld(source)} AND ({alias}.origin IS NULL OR {alias}.origin <> "
        f"'{ORIGIN_CONSOLIDATION_POLICY}' OR NOT {withheld(original)}))"  # nosec B608
    )


NOT_WITHHELD_CHUNKS = _not_withheld_sql("chunks")


# Per-path twin of ``SqliteBackend.get_hidden_source_files``: that query returns
# the hidden *set*, this tests one path expression against it, so a paginated
# listing drops hidden files before ``LIMIT`` without reading the set. Unlike
# ``_visible_source_sql`` it hides the whole file, which is what a path-level
# listing reports: a file is hidden when it is held or pending, or when any of
# its chunks is a consolidation-policy summary whose parent is (derived with
# the same unconditional ``substr``). ``test_mem_list_pagination`` checks both
# against one matrix. ``path`` is a fixed SQL expression from this package —
# ``chunks.source_file``, or a path stored under another key — never a value.
def visible_path_sql(path: str) -> str:
    parent = f"substr({path}, 1, length({path}) - length('{CONSOLIDATED_SUFFIX}'))"
    return (
        f"(NOT EXISTS (SELECT 1 FROM held_sources h WHERE h.source_file={path}) "  # nosec B608
        f"AND NOT EXISTS (SELECT 1 FROM pending_source_checks p WHERE p.source_file={path}) "
        # ``idx_chunks_policy_source`` answers this probe; without it, a
        # per-row screen scanned the whole file once per row it read.
        f"AND NOT (EXISTS (SELECT 1 FROM chunks o WHERE o.source_file={path} "
        f"AND o.origin='{ORIGIN_CONSOLIDATION_POLICY}') "
        f"AND (EXISTS (SELECT 1 FROM held_sources h WHERE h.source_file={parent}) "
        f"OR EXISTS (SELECT 1 FROM pending_source_checks p WHERE p.source_file={parent}))))"
    )


VISIBLE_SOURCE_PATH = visible_path_sql("chunks.source_file")


async def hidden_source_paths(storage: StorageBackend) -> set[Path]:
    """Fetch path-level visibility for Web and export, including policy summaries."""
    getter = (
        getattr(storage, "get_hidden_source_files", None)
        if hasattr(type(storage), "get_hidden_source_files")
        else None
    )
    if getter is not None:
        return set(await getter())
    held = await storage.get_held_sources()
    pending = await storage.get_pending_source_checks()
    return set([*held, *pending])


async def source_hidden(storage: StorageBackend, source: Path) -> bool:
    """Check a path after a read so newly committed holds stay hidden."""
    getter = (
        getattr(storage, "is_source_hidden", None)
        if hasattr(type(storage), "is_source_hidden")
        else None
    )
    return (
        bool(await getter(source)) if getter is not None else await storage.is_source_held(source)
    )


# ``_scopes_*_clause`` are private to ``sqlite_scope`` because they are half a
# rule on their own — the boundary has to be layered on top, which is exactly
# what ``_scope_sql`` below does. Imported rather than re-derived so the two
# emitters cannot spell the explicit-filter half differently.


def neighbor_visibility_sql(
    *,
    ns_filter: NamespaceFilter | None,
    system_prefixes: Sequence[str],
    scope_filter: ScopeFilter | None,
    project_context_root: Path | None,
    as_of_unix: int | None,
    column_alias: str = "",
) -> tuple[str, list]:
    """Build the ``(fragment, params)`` matching ``neighbor_visible``.

    The fragment is a self-contained boolean expression over ``chunks``
    columns, safe to drop into a ``CASE WHEN`` or a ``WHERE``. It is never
    empty: with no filters at all it emits ``1 = 1`` so a caller cannot lose
    the rule by treating an empty string as "no restriction".

    ``column_alias`` prefixes every column reference (pass ``"c."`` when the
    query aliases the table), mirroring :func:`scope_context_sql`.
    """
    a = column_alias
    terms: list[str] = []
    params: list = []

    for fragment, frag_params in (
        _namespace_sql(ns_filter, system_prefixes, a),
        _scope_sql(scope_filter, project_context_root, a),
        _validity_sql(as_of_unix, a),
    ):
        if fragment:
            terms.append(fragment)
            params.extend(frag_params)

    if not terms:
        return "1 = 1", []
    return " AND ".join(terms), params


def _namespace_sql(
    ns_filter: NamespaceFilter | None,
    system_prefixes: Sequence[str],
    alias: str,
) -> tuple[str, list]:
    """System namespaces are hidden unless the caller's filter names them.

    ``neighbor_visible``'s first axis, verbatim: hidden iff
    ``has_namespace_prefix(ns, system_prefixes) and not ns_filter.matches(ns)``.
    An explicit filter only ever widens here, so a chunk outside the system
    prefixes passes regardless of what the filter says — this is not
    ``namespace_sql``'s selection clause.
    """
    if not system_prefixes:
        return "", []

    # No length cap of its own: ``SearchConfig.prefix_count_capped`` rejects
    # more than 10 prefixes at startup, and both callers pass the validated
    # config list, so the runaway N × M LIKE clause the cap exists to prevent
    # cannot be reached from here.
    not_system = " AND ".join(f"{alias}namespace NOT LIKE ? ESCAPE '\\'" for _ in system_prefixes)
    params: list = [f"{escape_like(p)}%" for p in system_prefixes]

    match_sql, match_params = _namespace_matches_sql(ns_filter, alias)
    if not match_sql:
        return f"({not_system})", params
    return f"(({not_system}) OR {match_sql})", params + match_params


def _namespace_matches_sql(ns_filter: NamespaceFilter | None, alias: str) -> tuple[str, list]:
    """``NamespaceFilter.matches`` as SQL — branch order included.

    ``namespace_sql`` cannot be reused directly: it emits no alias, and its
    ``exclude_prefixes`` branch is the default-search exclusion rather than a
    match. The other two branches are spelled identically so the comparison
    semantics (exact ``IN`` is case-sensitive, ``LIKE`` folds ASCII case) stay
    the same on both sides.
    """
    if ns_filter is None:
        return "", []
    if ns_filter.namespaces:
        ph = ",".join("?" * len(ns_filter.namespaces))
        return f"{alias}namespace IN ({ph})", list(ns_filter.namespaces)
    if ns_filter.pattern:
        escaped = ns_filter.pattern.replace("_", r"\_").replace("*", "%")
        return f"{alias}namespace LIKE ? ESCAPE '\\'", [escaped]
    if ns_filter.exclude_prefixes:
        clauses = " AND ".join(
            f"{alias}namespace NOT LIKE ? ESCAPE '\\'" for _ in ns_filter.exclude_prefixes
        )
        return f"({clauses})", [f"{escape_like(p)}%" for p in ns_filter.exclude_prefixes]
    # An empty filter matches everything, mirroring ``matches``' final
    # ``return True`` — which makes the whole namespace term vacuous.
    return "1 = 1", []


def _scope_sql(
    scope_filter: ScopeFilter | None,
    project_context_root: Path | None,
    alias: str,
) -> tuple[str, list]:
    """``_scope_visible``: the ADR-0011 boundary, widened out-of-project only.

    In-project the boundary is ``scope = 'user' OR project_root = <root>`` and
    an explicit filter adds nothing — project-tier rows stay pinned to the
    current root. Out-of-project the boundary is ``scope = 'user'``, and a
    non-empty explicit filter is the deliberate cross-project opt-in, so it is
    OR-ed on top.
    """
    if project_context_root is not None:
        return (
            f"({alias}scope = 'user' OR {alias}project_root = ?)",
            [str(project_context_root)],
        )

    boundary = f"{alias}scope = 'user'"
    if scope_filter is None or not (scope_filter.scopes or scope_filter.pattern):
        return boundary, []

    if scope_filter.scopes:
        widen, widen_params = _scopes_in_clause(scope_filter.scopes, alias)
    else:
        widen, widen_params = _scopes_glob_clause(scope_filter.pattern or "", alias)
    return f"({boundary} OR {widen})", widen_params


def _validity_sql(as_of_unix: int | None, alias: str) -> tuple[str, list]:
    """``chunk_valid_at``: inclusive on both ends, ``NULL`` = unbounded."""
    if as_of_unix is None:
        return "", []
    return (
        f"(({alias}valid_from_unix IS NULL OR {alias}valid_from_unix <= ?) "
        f"AND ({alias}valid_to_unix IS NULL OR {alias}valid_to_unix >= ?))",
        [as_of_unix, as_of_unix],
    )
