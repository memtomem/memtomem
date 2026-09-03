"""Why a CLI query came back empty (issue #2255).

``No results found. See `mm status` to confirm your index has chunks.`` is
right for an empty index and actively misleading for anything else: a
mistyped ``-n 3`` (``--namespace``, not ``--top-k``) empties every retrieval
leg before ranking runs, and the message then points the reader at the one
subsystem that is not wrong. Zero results is a plausible answer, so the
stated cause gets believed.

The branch is **evidence-first**: it turns on what the store reports, not on
a judgement about which supplied option narrowed the query. An earlier
version tried the latter and could not be finished — whether a value narrows
depends on each option's own parser (``--scope ''`` filters, ``--tag-filter
','`` does not, ``'*'`` matches everything, ``-k 0`` returns nothing at all),
so every case answered raised another, and the answers had to stay in step
with parsers this module does not own.

Reading ``list_namespaces()`` settles the only question that matters for the
original complaint — is the index actually empty — and it settles it by
observation. So:

* empty store → the index hint, which is right there;
* a ``--namespace`` that matches nothing the store has → say exactly that,
  because ``NamespaceFilter.matches`` is the same question the query asked;
* otherwise → report what the index holds and what the command included,
  and claim nothing about which option is responsible.

It **never rejects** a filter: "nothing indexed under this namespace yet" is
a legitimate answer, and returning it is preserved. Only the explanation
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


def _count_flag_suggestion(namespace: str, count_flag: str) -> str | None:
    """The ``-n 3`` → ``-k 3`` hint, for values that are really a count.

    Strictly positive decimals only, and the conversion is guarded rather
    than assumed: ``"²".isdigit()`` is true while ``int("²")`` raises, and
    ``int()`` also refuses a digit string past CPython's conversion limit.
    A namespace is unbounded user input, so both reach here.

    Rejecting ``0`` keeps the hint from proposing a command that is valid and
    useless; a negative value never gets this far, but would be worse than
    useless — SQLite reads ``LIMIT -1`` as no limit at all.
    """

    stripped = namespace.strip()
    if not stripped.isascii() or not stripped.isdecimal():
        return None
    try:
        count = int(stripped)
    except ValueError:
        return None
    if count <= 0:
        return None
    return f"'-n' is --namespace, not the result count: did you mean `{count_flag} {count}`?"


def _format_filters(filters: Sequence[tuple[str, str | None]]) -> str:
    """Render the options the caller reports having been given.

    A ``None`` value is a flag that takes no argument (``--no-include-shared``),
    printed bare. Absent options never reach here: each call site drops them
    while building its own list, because only the command knows which of its
    options were typed.
    """

    return ", ".join(flag if value is None else f"{flag} {value!r}" for flag, value in filters)


def _format_namespaces(known: list[tuple[str, int]]) -> str:
    listed = known[:_MAX_LISTED]
    rendered = ", ".join(f"{name} ({count})" for name, count in listed)
    remaining = len(known) - len(listed)
    if remaining > 0:
        rendered += f", … (+{remaining} more)"
    return rendered


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


async def explain_empty_result(
    storage: _NamespaceLister,
    *,
    namespace: str | None,
    filters: Sequence[tuple[str, str | None]],
    count_flag: str | None,
    namespace_label: str = "--namespace",
    scope_note: str | None = None,
) -> str:
    """Return the message to print when a query returned nothing.

    ``namespace`` is the value the query *ran with* — only ``mm search``
    normalizes an empty one away, so each call site resolves it. ``filters``
    is what the command line carried, reported as a fact about the invocation
    rather than as a verdict on any option. ``count_flag`` is that command's
    "how many results" option (``-k`` for search, ``-l`` for recall) — the
    flag ``-n`` is most often mistaken for.

    Both of the last two are the *caller's* vocabulary, not this module's, and
    a command whose namespace is derived rather than typed has to say so:
    ``mm agent search`` merges its namespace out of ``--agent-id``,
    ``--include-shared`` and ``--shared-namespace``, has no ``--namespace`` and
    no ``-n``, so it passes a ``namespace_label`` naming what it resolved and
    ``count_flag=None`` to drop a suggestion about a flag it does not accept.
    Naming an option the command does not have turns a diagnostic into an
    instruction the reader cannot follow.

    ``scope_note`` is for a command that narrowed the query without being told
    to on the command line — ``mm agent search`` scopes to the active session's
    agent when no ``--agent-id`` is given. The inventory branch otherwise
    reports a healthy index and no options at all, which is true of the
    invocation and hides the narrowing that actually emptied it. It is appended
    only there: the branch above already names the namespace, and an empty
    store has nothing to have been scoped away from.
    """

    from memtomem.models import InvalidNamespaceFilterError, NamespaceFilter

    known = await storage.list_namespaces()
    if not known:
        # Nothing indexed anywhere: the index really is the answer.
        return INDEX_HINT

    if namespace is not None:
        try:
            parsed = NamespaceFilter.parse(namespace)
        except InvalidNamespaceFilterError:  # pragma: no cover - rejected upstream
            parsed = None
        # ``matches`` is the documented Python twin of the SQL emitter, so
        # "no known namespace matches" is the same question the query asked.
        if parsed is not None and not any(parsed.matches(name) for name, _ in known):
            lines = [
                f"No results found: {namespace_label} {namespace!r} matches none of the "
                "namespaces this index has.",
                f"Indexed namespaces: {_format_namespaces(known)}",
            ]
            suggestion = (
                _count_flag_suggestion(namespace, count_flag) if count_flag is not None else None
            )
            if suggestion is not None:
                lines.append(suggestion)
            return "\n".join(lines)

    total = sum(count for _, count in known)
    inventory = (
        f"No results found. The index has {_plural(total, 'chunk')} across "
        f"{_plural(len(known), 'namespace')}"
    )
    trailer = f" {scope_note}" if scope_note else ""
    if not filters:
        # Only what was observed. "nothing matched" would be a claim about
        # retrieval that ``-k 0`` falsifies — it returns nothing without
        # matching being involved at all.
        return f"{inventory}, so the index is not the empty one.{trailer}"
    return (
        f"{inventory}. This query included: {_format_filters(filters)}. "
        f"Review those options, or rerun with fewer of them.{trailer}"
    )
