"""Namespace resolution decisions, flag validation, and run-level move tallies."""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import Literal


class NamespaceMixedUnderForceError(Exception):
    """A forced re-index would collapse a multi-namespace file into one namespace.

    ``force=True`` promotes every unchanged chunk into ``to_upsert``, and the
    whole upsert carries one namespace — so a file whose rows are split across
    several namespaces would have all of them rewritten to the rule-resolved
    one, moving agent-scoped content into a searchable namespace with no way
    back (the day-file name encoding is one-way, so nothing on disk can
    restore it). Permanent, not retryable: re-running changes nothing. The
    caller picks an out — an explicit namespace, ``reassign_namespaces=True``,
    or splitting the file.

    The message carries ``file_path.name`` only. Bulk error strings are echoed
    verbatim through the web complete event and API responses, which redact
    host paths.
    """

    retryable = False


#: Why a file's chunks got the namespace they got (#2061). ``preserved`` and
#: ``preserved_against_rules`` both mean "the stored namespace won"; the
#: latter additionally means current rules would have chosen differently, and
#: is what the CLI advisory counts so a user whose rule edit did not take
#: effect is told which command applies it.
#: ``bound_new_source`` is the session-inheritance carve-out (#2104): a caller
#: that binds writes to a context namespace (an MCP agent session, a
#: ``mem_ns_set`` current namespace) passes it as ``new_source_namespace``, and
#: it applies only to sources with no stored rows. An already-indexed file
#: keeps its namespace, so a bulk re-index under a session cannot move content
#: the session did not write.
NamespaceDecisionReason = Literal[
    "explicit",
    "preserved",
    "preserved_against_rules",
    "resolved",
    "reassigned",
    "mixed_force_refused",
    "bound_new_source",
]


@dataclasses.dataclass(frozen=True)
class NamespaceDecision:
    """The namespace a file's chunks get, the rows it had, and why.

    One structured answer instead of a bare ``str | None`` so the reporting
    surfaces (advisory counters, move summary, the system-namespace warning)
    read the *authoritative in-lock* resolution rather than re-deriving it
    from a pre-write preview that a concurrent writer may have invalidated.
    """

    target: str | None
    stored: tuple[str, ...] = ()
    reason: NamespaceDecisionReason = "resolved"


def _reject_reassign_with_explicit_ns(
    namespace: str | None, reassign: bool, new_source_namespace: str | None = None
) -> None:
    """Refuse the flag pairs that cannot mean anything coherent.

    An explicit namespace short-circuits rule resolution, so pairing it with
    ``reassign_namespaces=True`` asks for two different targets at once. The
    CLI rejects the combination too, but the check belongs here as well: the
    engine entrypoints are public API, and silently letting the explicit
    namespace win would drop the reassignment — and with it the stored lookup
    that reporting depends on — without a word.

    ``new_source_namespace`` is refused alongside ``reassign`` for the same
    reason at the one place the two overlap: a source with no stored rows.
    Reassignment says "the rules decide", session binding says "the session
    decides", and a run cannot honor both. Files that *do* have rows are
    unaffected by the binding, so the conflict is real but narrow — narrow
    enough that letting one win silently would be indistinguishable from the
    other having been applied (#2104).
    """
    if reassign and namespace is not None:
        raise ValueError(
            "reassign_namespaces=True cannot be combined with an explicit namespace: "
            "an explicit namespace short-circuits the rules that reassignment exists "
            "to apply. Pass one or the other."
        )
    if reassign and new_source_namespace is not None:
        raise ValueError(
            "reassign_namespaces=True cannot be combined with new_source_namespace: "
            "reassignment resolves every file through the path rules, including the "
            "row-less ones the binding would claim. Pass one or the other."
        )


@dataclasses.dataclass
class _NamespaceTally:
    """Run-level roll-up of per-file :class:`NamespaceDecision` records.

    Shared by both bulk paths so the stream and non-stream surfaces cannot
    report different numbers for the same run.
    """

    preserved_against_rules: int = 0
    reassigned: int = 0
    moves: dict[tuple[str, str], int] = dataclasses.field(default_factory=dict)

    def record(
        self,
        decision: NamespaceDecision,
        *,
        written: bool,
        canonical: Callable[[str | None], str],
    ) -> None:
        if decision.reason == "preserved_against_rules":
            # Counted whether or not a write followed: an unchanged file that
            # kept a namespace the current rules disagree with is exactly the
            # case the advisory exists to name.
            self.preserved_against_rules += 1
            return
        if decision.reason != "reassigned" or not written:
            # A move is only real once its namespace-bearing upsert committed.
            return
        self.reassigned += 1
        target = canonical(decision.target)
        for old in decision.stored:
            old_canonical = canonical(old)
            if old_canonical == target:
                continue
            self.moves[(old_canonical, target)] = self.moves.get((old_canonical, target), 0) + 1

    def summary(self) -> tuple[dict[str, object], ...]:
        """Moves as structured records, not rendered lines.

        Rendering here would force every consumer to parse the string back
        apart. The CLI's system-scope warning needs both endpoints (a move
        from one system namespace to another exposes nothing) and the file
        count (one line can stand for many files), so the numbers travel as
        numbers.
        """
        return tuple(
            {"from": old, "to": new, "files": count}
            for (old, new), count in sorted(self.moves.items())
        )
