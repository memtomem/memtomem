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


def active_tag_filter(value: str | None) -> str | None:
    """``None`` when a ``--tag-filter`` value selects no tags at all.

    ``parse_tag_filter`` — the same function the query uses — reads ``""``
    and ``","`` as an empty tag tuple, which filters nothing. Naming such a
    value as a filter to drop is the weaker form of the wrong-cause claim
    this module exists to remove: the reader drops it and the result set does
    not move.
    """

    from memtomem.storage.base import parse_tag_filter

    if value is None:
        return None
    return value if parse_tag_filter(value) else None


def _count_flag_suggestion(namespace: str, count_flag: str) -> str | None:
    """The ``-n 3`` → ``-k 3`` hint, for values that are really a count.

    Strictly positive integers only. ``str.isdigit`` already rejects a sign,
    and rejecting ``0`` too keeps the hint from proposing a command that is
    valid but useless — while ``-1`` would be worse than useless, since
    SQLite reads ``LIMIT -1`` as no limit at all.
    """

    stripped = namespace.strip()
    if not stripped.isdigit() or int(stripped) <= 0:
        return None
    return (
        f"'-n' is --namespace, not the result count: did you mean `{count_flag} {int(stripped)}`?"
    )


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

    ``filters`` is every filter the query actually applied, in the order the
    command declares them, including ``--namespace`` itself. Emptiness is not
    a proxy for "inactive": ``--scope ''`` parses to ``scopes=('',)`` and
    empties a healthy store, so a call site must drop a value only where its
    own option is genuinely normalized away. ``count_flag`` is that command's
    "how many results" option (``-k`` for search, ``-l`` for recall) — the
    flag ``-n`` is most often mistaken for.

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

    # ``namespace`` is the value the query *ran with*, not the raw argument:
    # only ``mm search`` normalizes an empty one away, so each call site
    # resolves it and hands the result here. An empty string that survives is
    # a real filter (``NamespaceFilter.parse("")`` is ``namespaces=('',)``)
    # and matches nothing, which is worth saying out loud.
    if namespace is not None:
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
            suggestion = _count_flag_suggestion(namespace, count_flag)
            if suggestion is not None:
                lines.append(suggestion)
            return "\n".join(lines)

    return (
        f"No results found. Filters applied: {_format_filters(filters)}. "
        "Drop them one at a time to see whether any of them is excluding "
        "matches; `mm status` reports index size if you suspect the index "
        "instead."
    )
