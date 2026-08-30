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

from memtomem.models import NamespaceFilter, ScopeFilter

from .sqlite_helpers import escape_like
from .sqlite_scope import _scopes_glob_clause, _scopes_in_clause

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

    # Belt-and-suspenders cap, matching ``namespace_sql``: refuse to emit a
    # pathologically long clause if a caller hand-builds the prefix tuple.
    assert len(system_prefixes) <= 10, (
        f"neighbor_visibility_sql: {len(system_prefixes)} system prefixes, cap is 10"
    )
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
