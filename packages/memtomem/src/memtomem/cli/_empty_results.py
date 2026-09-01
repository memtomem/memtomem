"""Why a CLI query came back empty (issue #2255).

``No results found. See `mm status` to confirm your index has chunks.`` is
right for an empty index and actively misleading for anything else: a
mistyped ``-n 3`` (``--namespace``, not ``--top-k``) empties every retrieval
leg before ranking runs, and the message then points the reader at the one
subsystem that is not wrong. Zero results is a plausible answer, so the
stated cause gets believed.

This module builds the replacement text. It **never rejects** a filter —
"nothing indexed under this namespace yet" is a legitimate answer, and the
existing behaviour of returning it is preserved; only the explanation
changes.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

INDEX_HINT = "No results found. See `mm status` to confirm your index has chunks."

#: Namespaces listed before the message elides the rest. Long enough to show
#: a real store's shape, short enough not to bury the hint under it.
_MAX_LISTED = 8


class _NamespaceLister(Protocol):
    async def list_namespaces(self) -> list[tuple[str, int]]: ...


def _format_filters(filters: Sequence[tuple[str, str]]) -> str:
    return ", ".join(f"{flag} {value!r}" for flag, value in filters)


def _format_namespaces(known: list[tuple[str, int]]) -> str:
    listed = known[:_MAX_LISTED]
    rendered = ", ".join(f"{name} ({count})" for name, count in listed)
    remaining = len(known) - len(listed)
    if remaining > 0:
        rendered += f", … (+{remaining} more)"
    return rendered


async def explain_empty_result(
    storage: _NamespaceLister,
    *,
    namespace: str | None,
    filters: Sequence[tuple[str, str]],
    count_flag: str,
) -> str:
    """Return the message to print when a query returned nothing.

    ``filters`` is every filter the user actually supplied, in the order the
    command declares them, including ``--namespace`` itself. ``count_flag``
    is that command's "how many results" option (``-k`` for search, ``-l``
    for recall) — the flag ``-n`` is most often mistaken for.

    Falls back to :data:`INDEX_HINT` whenever the index is the plausible
    suspect: no filter was supplied, or the store has no namespaces at all
    (which is what an empty index looks like from here).
    """

    from memtomem.models import InvalidNamespaceFilterError, NamespaceFilter

    if not filters:
        return INDEX_HINT

    # Checked for every filtered query, not only namespaced ones: a store with
    # nothing in it answers every filter with zero, and blaming the filter
    # there is the same wrong-subsystem diagnosis in the other direction.
    known = await storage.list_namespaces()
    if not known:
        return INDEX_HINT

    # An empty ``--namespace`` is not a namespace: ``run_search`` normalizes it
    # away (``effective_ns = namespace or current_namespace``), so the query
    # ran unfiltered and naming it as the cause would be a lie.
    if namespace:
        try:
            parsed = NamespaceFilter.parse(namespace)
        except InvalidNamespaceFilterError:  # pragma: no cover - rejected upstream
            parsed = None
        # ``matches`` is the documented Python twin of the SQL emitter, so
        # "no known namespace matches" is the same question the query asked.
        if parsed is not None and not any(parsed.matches(name) for name, _ in known):
            lines = [
                f"No results found: --namespace {namespace!r} matches none of the "
                "namespaces this index has.",
                f"Indexed namespaces: {_format_namespaces(known)}",
            ]
            if namespace.strip().lstrip("+-").isdigit():
                lines.append(
                    f"'-n' is --namespace, not the result count: did you mean "
                    f"`{count_flag} {namespace}`?"
                )
            return "\n".join(lines)

    return (
        f"No results found. Filters applied: {_format_filters(filters)}. "
        "Drop them one at a time to see whether any of them is excluding "
        "matches; `mm status` reports index size if you suspect the index "
        "instead."
    )
