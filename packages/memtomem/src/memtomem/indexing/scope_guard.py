"""Project memory keeps its scope across re-indexing (#2923).

A re-index classifies a file by path against the tiers registered at that
moment. Once a project tier is unregistered, re-writing a file in it would
stamp its rows ``user``, readable from every project. The rule enforced here:
rows stored as ``project_shared`` / ``project_local`` are never rewritten under
another scope or another project root. Changing either on purpose is
``mm context memory-migrate``'s job, which moves the file and its rows together.

The judge is pure. The engine applies it at the indexing chokepoint, holding
the source instead of writing, and the by-id rewrite surfaces apply it before
they write, so the refusal reaches the caller with nothing changed.
"""

from __future__ import annotations

import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from memtomem._runtime_paths import _hint_quote
from memtomem.indexing.privacy_gate import RemedySurface
from memtomem.storage.sqlite_helpers import fold_path_form

PROJECT_SCOPES = frozenset({"project_shared", "project_local"})


@dataclass(frozen=True)
class ScopeChange:
    """A stored project scope that a rewrite would replace."""

    stored_scope: str
    stored_root: str | None
    new_scope: str
    new_root: Path | None


def scope_change(
    stored: Iterable[tuple[str, str | None]], scope: str, project_root: Path | None
) -> ScopeChange | None:
    """The first stored ``(scope, project_root)`` that writing ``scope`` / ``project_root`` loses.

    Only project rows are judged: a ``user`` row becoming project memory
    narrows who can read it. Roots compare by spelling, folded the way stored
    keys are, and are never resolved: a stored root whose directory has since
    become a symlink into another project must not compare equal to it.
    """
    new_root = fold_path_form(str(project_root)) if project_root is not None else None
    for stored_scope, stored_root in stored:
        if stored_scope not in PROJECT_SCOPES:
            continue
        old_root = fold_path_form(str(stored_root)) if stored_root is not None else None
        if stored_scope != scope or old_root != new_root:
            return ScopeChange(stored_scope, stored_root, scope, project_root)
    return None


def key_spellings(key: str) -> list[str]:
    """``key`` and, on macOS, its NFD spelling.

    Keys are written NFC-folded on macOS, but rows written before that fold
    keep their NFD key, which a lookup by the folded key does not find.
    """
    spellings = [key]
    if sys.platform == "darwin":
        nfd = unicodedata.normalize("NFD", key)
        if nfd != key:
            spellings.append(nfd)
    return spellings


def scope_change_detail(
    change: ScopeChange, path: str, *, op: str | None = None, surface: RemedySurface = "neutral"
) -> str:
    """Why the source at ``path`` was refused or held, and the way forward.

    Only ``mcp`` names commands and host paths. The web shows the message as
    is and keeps host paths out of its responses, and ``neutral`` text also
    reaches the web through bulk indexing results, so both use the bare file
    name and no command.
    """
    name = Path(path).name
    if change.new_scope != change.stored_scope:
        what = (
            f"{name} holds {change.stored_scope} memory, but its tier is no longer "
            f"registered, so re-indexing it would store it as {change.new_scope} memory"
        )
    else:
        what = (
            f"{name} holds {change.stored_scope} memory of one project, but re-indexing it "
            "would store it as another project's"
        )
    lead = f"{op} refused: {what}; nothing was written." if op else f"{what}."
    if surface != "mcp":
        if change.new_scope != change.stored_scope:
            return f"{lead} Register the project's {change.stored_scope} tier again, then retry."
        return f"{lead} Move the file back to the project it was indexed in, then retry."
    migrate = (
        f"mm context memory-migrate {_hint_quote(Path(path))} --from {change.stored_scope} "
        "--to user --apply"
    )
    if change.new_scope != change.stored_scope:
        root = change.stored_root or "<project root>"
        return (
            f"{lead} Register the tier again (cd {_hint_quote(root)} && mm mem init --scope "
            f"{change.stored_scope}), or move the file out of it on purpose with {migrate}."
        )
    return f"{lead} Move the file back to {change.stored_root}, then retry."


def relinked_detail(name: str, *, op: str) -> str:
    """The refusal for a rewrite through a key that now resolves to another file."""
    return (
        f"{op} refused: {name} now resolves to another file (it became a symlink after it "
        "was indexed), so rewriting it would change that file instead; nothing was written."
    )


def scope_held_notice(op: str) -> str:
    """What a by-id rewrite tells its caller when its re-index held the source instead.

    The early check passed, so the tier was unregistered while the rewrite ran.
    """
    return (
        f"The {op} is on disk, but the file's project tier stopped being registered while it "
        "ran, so the file's memory is held (hidden from search) instead of re-indexed as "
        "another scope. Register the tier again to lift the hold."
    )
