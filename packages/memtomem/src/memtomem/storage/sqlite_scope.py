"""Scope-axis SQL fragment helpers (ADR-0011 §6).

Sibling of :mod:`memtomem.storage.sqlite_namespace`. Builds the SQL
WHERE fragment that pins memory search to the project-aware default
merge and (when the caller passes an explicit filter) narrows further.

The "context boundary" rule — out-of-project searches see only
``user``; in-project searches see ``user`` plus the current project's
project-tier rows; everything else requires explicit opt-in via the
filter — is implemented entirely here, not in the filter dataclass.
This keeps the filter a pure user-intent value and lets every caller
share one rule for what "no filter" means.

The fragment is **always** emitted (as long as ``scope_context_sql``
is called). Storage methods that previously could call
``namespace_sql`` only when a filter was present must call
``scope_context_sql`` unconditionally — the entire purpose of the
always-on fragment is to prevent cross-project leak when the caller
does not pass an explicit scope.
"""

from __future__ import annotations

from pathlib import Path

from memtomem.models import ScopeFilter


def scope_context_sql(
    explicit_filter: ScopeFilter | None,
    project_context_root: Path | None,
    *,
    column_alias: str = "",
) -> tuple[str, list]:
    """Build SQL WHERE fragment + params for the scope context (ADR-0011 §6).

    Returns ``(fragment, params)`` where ``fragment`` is a SQL snippet
    that the caller composes into a WHERE clause via ``AND``. The
    fragment is non-empty in every case — never returns ``("", [])`` —
    so callers cannot accidentally drop the context rule by treating an
    empty fragment as "no filter".

    ``column_alias``: prefix prepended to ``scope`` and ``project_root``
    column references in the emitted fragment. Use ``"c."`` when the
    caller's SQL aliases the chunks table as ``c``; default empty
    string is correct for unaliased queries against ``chunks``.

    Three semantic cases:

    - **No explicit filter, project context detected.** Fragment is
      ``(scope = 'user' OR project_root = ?)``. Returns user-tier rows
      (always shared across projects) plus the current project's
      project_shared / project_local rows. Other projects' project-tier
      rows are excluded.
    - **No explicit filter, no project context.** Fragment is
      ``scope = 'user'``. Project-tier rows from any project are
      excluded — the caller did not pin to a specific project, so we
      cannot tell whose project_shared chunks would be safe to surface.
      The user must pass an explicit ``--scope=project_*`` to opt into
      cross-project search.
    - **Explicit filter passed.** The filter narrows further. If the
      user picked ``project_shared`` from inside project X, they get
      project_shared rows from project X only (intersection of explicit
      filter and project context). If they picked ``project_shared``
      from outside any project context, they get project_shared rows
      from EVERY project — a deliberate cross-project search. The
      ``user`` scope filter passed explicitly always returns user-tier
      rows regardless of project context.
    """
    a = column_alias  # local short alias for readability
    if explicit_filter is None:
        if project_context_root is not None:
            return (f"({a}scope = 'user' OR {a}project_root = ?)", [str(project_context_root)])
        return (f"{a}scope = 'user'", [])

    # Explicit filter narrows the candidate scopes. Then the project
    # context layers on top: project-tier rows are pinned to the
    # current project_root unless the caller is out-of-project, in
    # which case they get cross-project union.
    if explicit_filter.scopes:
        scope_clause, scope_params = _scopes_in_clause(explicit_filter.scopes, a)
    elif explicit_filter.pattern:
        scope_clause, scope_params = _scopes_glob_clause(explicit_filter.pattern, a)
    else:
        # Empty filter (parser returned an instance with no scopes / no
        # pattern). Fall back to the no-filter context rule.
        if project_context_root is not None:
            return (f"({a}scope = 'user' OR {a}project_root = ?)", [str(project_context_root)])
        return (f"{a}scope = 'user'", [])

    if project_context_root is None:
        return (scope_clause, scope_params)

    # In-project + explicit filter: pin project-tier rows to the
    # current project_root. ``user`` scope rows always pass (they have
    # NULL project_root); other rows must match the current project.
    return (
        f"({scope_clause} AND ({a}scope = 'user' OR {a}project_root = ?))",
        scope_params + [str(project_context_root)],
    )


def _scopes_in_clause(scopes: tuple[str, ...], alias: str) -> tuple[str, list]:
    if not scopes:
        return (f"{alias}scope IS NULL", [])  # Pathological, but produce no rows.
    placeholders = ",".join("?" * len(scopes))
    return (f"{alias}scope IN ({placeholders})", list(scopes))


def _scopes_glob_clause(pattern: str, alias: str) -> tuple[str, list]:
    # Translate the user-supplied glob (only ``*`` is meaningful for the
    # 3-value scope alphabet) into a SQL LIKE pattern.
    escaped = pattern.replace("_", r"\_").replace("*", "%")
    return (f"{alias}scope LIKE ? ESCAPE '\\'", [escaped])


def scope_sort_priority_case(column_alias: str = "") -> str:
    """Return a SQL ``CASE`` expression mapping scope to sort priority.

    Tie-break ranking (ADR-0011 §3): same-relevance results order
    ``project_local > project_shared > user``. Smaller integer = higher
    priority; the column is intended to be appended to ``ORDER BY``
    after the relevance-based ordering, so it only affects ties.

    ``column_alias`` mirrors :func:`scope_context_sql` — pass ``"c."``
    when the SELECT aliases the chunks table.

    Caller composes this directly into the SELECT and ORDER BY:

    .. code-block:: sql

        SELECT c.*, {scope_sort_priority_case("c.")} AS scope_prio
        FROM chunks c
        ...
        ORDER BY rank, scope_prio
    """
    return (
        f"CASE {column_alias}scope "
        "WHEN 'project_local' THEN 0 "
        "WHEN 'project_shared' THEN 1 "
        "ELSE 2 END"
    )
