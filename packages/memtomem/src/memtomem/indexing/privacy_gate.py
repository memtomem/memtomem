"""Privacy refusal models, remedies, and mutation preflight checks."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from memtomem._runtime_paths import _hint_quote

_UNHIDDEN_BLOCKED_ERROR = "could not hide the indexed chunks of this blocked file; retry"

#: Whose vocabulary a privacy refusal names its remedy in. ``shell`` is the CLI
#: read from inside ``mm shell``, where a typed ``mm index`` would run a search;
#: ``neutral`` names no command, for a caller that may not share the ambient
#: config (``MemtomemStore``) and for text persisted where another surface reads it.
RemedySurface = Literal["cli", "shell", "mcp", "web", "neutral"]

MutationOutcome = Literal["not_written", "rolled_back", "not_rolled_back", "superseded", "appended"]


def privacy_remedy(surface: RemedySurface, *, path: Path, bypassable: bool) -> str:
    """The sentence telling ``surface`` how to index ``path`` after a privacy refusal.

    The hold lifts once the file indexes cleanly, so the remedy is a re-index of
    this exact file: ``mem_index`` defaults to the server's cwd and a relative
    name can resolve elsewhere, so commands carry the full path. ``bypassable``
    is false for ``project_shared``, whose ``force_unsafe`` is refused, so no
    bypass is offered there. ``mem_index`` has no ``force_unsafe``: an MCP
    caller's bypass is the CLI's. The web names its own control and the bare
    file name only (host paths stay out of web responses).
    """
    if surface in ("cli", "shell", "mcp"):
        target = _hint_quote(path)
        if surface == "mcp":
            reindex = f"call `mem_index(path={json.dumps(str(path))})`"
            force = f"authorize it from the CLI with `mm index --force-unsafe {target}`"
        else:
            where = " (outside the shell)" if surface == "shell" else ""
            reindex = f"run `mm index {target}`{where}"
            force = f"authorize it with `mm index --force-unsafe {target}`{where}"
    elif surface == "web":
        reindex = "re-index the file from the Index tab"
        force = "index it there with 'Index without privacy gate' (Advanced options)"
    else:
        reindex = "re-index the file"
        force = "index a reviewed false positive with the privacy gate bypassed"
    if not bypassable:
        return f"Remove that content, then {reindex}."
    return f"Remove that content and {reindex}, or {force}."


def _read_text_view(text: str) -> str:
    """``text`` with the newline translation ``Path.read_text`` applies on read."""
    return text.replace("\r\n", "\n").replace("\r", "\n")


class PrivacyRejection(Exception):
    """Raised by :meth:`IndexEngine._index_file` when a file's content trips the
    secret-redaction guard during **un-adjudicated** indexing (ADR-0006 PR-A).

    Bulk entrypoints (``index_path`` / ``index_path_stream``) catch this per file
    and aggregate it into :attr:`IndexingStats.blocked_files`; single-file
    ``index_file`` callers let it propagate so their own rollback / error
    surfacing runs. Memory tools that modify an existing file meet it twice:
    from :meth:`IndexEngine.preflight_mutation` before they write, and from the
    re-index after it, which judges the whole file like every other index.

    Carries only the hit **count** and file path — never the matched bytes
    (secret-in-log hygiene).

    ``held`` says what happened to the chunks the file already had (#2629):
    ``True`` they were hidden behind a ``redaction_blocked`` hold, ``False``
    the hold failed and they may still be served, so the caller should retry,
    ``None`` no hold was attempted.
    """

    retryable = False

    def __init__(self, *, path: Path, hit_count: int, scope: str, decision: str) -> None:
        self.path = path
        self.hit_count = hit_count
        self.scope = scope
        self.decision = decision
        self.held: bool | None = None
        self.retryable = False
        # Set by a memory tool that re-raises this to its route: what became of
        # the write it stopped (see ``mutation_detail``).
        self.mutation_outcome: MutationOutcome | None = None
        super().__init__(f"redaction_blocked: {path.name} (hits={hit_count}, decision={decision})")

    def error_detail(self) -> str:
        """The per-file ``redaction_blocked`` text bulk runs report.

        Its shape is a contract: the CLI recognises it and shows the file in
        the blocked summary instead (``cli/_index_progress.py``).
        """
        return (
            f"redaction_blocked (hits={self.hit_count}, scope={self.scope}, "
            f"decision={self.decision})"
        )

    def unhidden_error(self) -> str | None:
        """A separate, retryable error when the older chunks could not be held."""
        if self.held is False:
            return _UNHIDDEN_BLOCKED_ERROR
        return None

    def mark_unheld(self) -> None:
        """Record that the hold failed: the refusal stands, but it is now retryable.

        A held block is permanent until the file changes; one whose older
        chunks are still served is not settled, so queue consumers that read
        ``retryable`` (the debounce queue) must retry it, and its message says
        why.
        """
        self.held = False
        self.retryable = True
        self.args = (f"{self.args[0]}; {_UNHIDDEN_BLOCKED_ERROR}",)

    def mutation_detail(self, op: str, *, outcome: MutationOutcome, surface: RemedySurface) -> str:
        """The refusal a memory tool reports when this rejection stopped its write.

        ``outcome`` says what became of the write: ``not_written`` (refused by
        the preflight, the file is as it was), ``rolled_back`` (refused after
        the write, the pre-image was put back), ``not_rolled_back`` (refused
        after the write and the restore failed, so the file may still hold it),
        ``superseded`` (another process removed or replaced the file while the
        write ran; nothing was written back over it) or ``appended`` (refused
        after a durable append, which stays on disk unindexed). Hit count only, never
        the matched bytes. The remedy is :func:`privacy_remedy` in ``surface``'s
        vocabulary.
        """
        if outcome == "not_written":
            subject = f"{self.path.name} holds content"
            result = "nothing was written"
        elif outcome == "rolled_back":
            subject = f"the result would hold content in {self.path.name}"
            result = "the change was rolled back"
        elif outcome == "not_rolled_back":
            subject = f"the result would hold content in {self.path.name}"
            result = "the rollback failed, so the file may still hold the change"
        elif outcome == "superseded":
            subject = f"the result would hold content in {self.path.name}"
            result = (
                "another process removed or replaced the file while the change ran, "
                "so nothing was written back over it"
            )
        else:
            subject = f"the result holds content in {self.path.name}"
            result = "the entry was appended to the file but NOT indexed"
        parts = [
            f"Error: {op} refused: {subject} matching {self.hit_count} privacy "
            f"pattern(s) that the current policy does not allow to be indexed; {result}."
        ]
        if self.held is True:
            parts.append("Its indexed chunks are held until the file indexes cleanly.")
        elif self.held is False:
            parts.append(f"Also: {_UNHIDDEN_BLOCKED_ERROR}.")
        parts.append(
            privacy_remedy(surface, path=self.path, bypassable=self.scope != "project_shared")
        )
        return " ".join(parts)


class MutationBlockedError(Exception):
    """A memory-tool write the preflight forbids for a reason other than privacy.

    ``detail`` is the user-facing refusal (:meth:`MutationPreflight.blocker`).
    Nothing was written.
    """

    def __init__(self, detail: str) -> None:
        self.detail = detail
        super().__init__(detail)


@dataclass(frozen=True)
class MutationPreflight:
    """What :meth:`IndexEngine.preflight_mutation` decided about a file as it stands.

    ``rejection`` — the file may not be indexed now; the tool must not write,
    and calls :meth:`IndexEngine.refuse_mutation` to record and hold.
    ``masking_locked`` — a reviewed masking manifest masks part of the file.
    The manifest is bound to the file's exact digest, so any change drops the
    masking and the masked text would be judged, and possibly stored, raw.
    ``valve_ok`` — the file holds nothing flagged and nothing masked, so every
    hit in the result of this write comes from the write itself and the
    caller's ``force_unsafe`` may cover it.
    """

    rejection: PrivacyRejection | None
    masking_locked: bool
    valve_ok: bool
    replay: Callable[[], object] | None = field(default=None, repr=False, compare=False)

    def blocker(self, op: str, name: str, *, needs_valve: bool) -> str | None:
        """The refusal text for a write the preflight forbids, other than ``rejection``."""
        if self.masking_locked:
            return (
                f"Error: {op} refused: {name} is under a reviewed masking manifest, "
                "which any change would invalidate; nothing was written. Edit it by "
                "hand and review the manifest again."
            )
        if needs_valve and not self.valve_ok:
            return (
                f"Error: {op} refused: force_unsafe covers only a file that holds "
                f"nothing flagged or masked, and {name} does; nothing was written. "
                "Write the forced content to a file of its own."
            )
        return None
