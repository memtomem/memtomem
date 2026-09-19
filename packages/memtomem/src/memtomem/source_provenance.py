"""Source-line evidence, independent of the text prepared for retrieval (#2371)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Literal, Protocol


# Covers both reasons a chunk's source may not be rewritten here, because the
# gates that raise it cannot always tell them apart: the chunk is a fragment or
# masked projection (no safe whole-line rewrite exists), or its file lives under
# a ``read_only_memory_dirs`` root (another tool owns those bytes). The remedy is
# the same either way, which is why one vocabulary still fits.
SOURCE_READ_ONLY_DETAIL = (
    "source_read_only: this chunk's source cannot be rewritten from here — it is a source "
    "fragment or projection, or it lives under a read-only index root. "
    "Edit the original source file and reindex it."
)


STALE_SOURCE_PROVENANCE_DETAIL = (
    "Chunk source-line provenance is stale; reindex the source and retry."
)


EXCLUDED_SOURCE_DETAIL = (
    "source_excluded: indexing now skips this source (an exclude pattern, a built-in rule, "
    "or a nested git worktree), so an edit would not reach the index. Nothing was written. "
    "Register a nested worktree as its own memory directory or remove the matching pattern "
    "to edit it here, or run `mm purge --matching-excluded` to remove its stored chunks."
)


READ_ONLY_TARGET_DETAIL = (
    "read_only_target: this destination is under a read-only index root "
    "(indexing.read_only_memory_dirs), so memtomem will not write it. Nothing was written. "
    "Write to a different memory directory, or remove the root from read_only_memory_dirs."
)


EXCLUDED_TARGET_DETAIL = (
    "source_excluded: indexing skips this file (an exclude pattern, a built-in rule, or a "
    "nested git worktree), so new content written to it would not be indexed. Nothing was "
    "written. Remove the matching pattern, register a nested worktree as its own memory "
    "directory, or write to a different file."
)


class StaleSourceProvenanceError(ValueError):
    """A rewrite refused before writing: its indexed source span cannot be verified."""


class ExcludedSourceError(ValueError):
    """A rewrite refused before writing: indexing skips the source, so the edit would not land.

    Writing the file and then re-indexing it would return zeroed stats without
    raising, and the old chunks would stay searchable beside the new bytes
    (#2488). Refusing first keeps the file and the index in agreement.
    """


class ReadOnlySourceError(ValueError):
    """A rewrite refused before writing: the source lives under a read-only index root.

    Distinct from :class:`ExcludedSourceError` because the two say opposite
    things about the index: an excluded source would not be re-indexed at all,
    whereas a read-only source is indexed and searchable — only its bytes are
    off limits, because another tool owns them.

    Raised by the shared mutation helper so a caller that reaches it without
    having consulted ``is_read_only_source`` still refuses rather than writing.
    The per-chunk ``source_read_only`` gates catch the common case earlier; this
    is the backstop that also covers a file indexed *before* its root was
    declared read-only, whose stored chunks still carry the flag unset.
    """


class WriteTargetGuard(Protocol):
    """The two configuration questions every replace-target writer must ask.

    Deliberately an object rather than the loose ``is_excluded`` callable this
    replaced: the checks are not independently optional, and a signature taking
    one predicate let a caller wire up half the protection and look complete.
    :class:`~memtomem.indexing.engine.IndexEngine` satisfies it structurally —
    callers pass the engine itself.
    """

    def is_excluded(self, file_path: Path) -> bool: ...

    def is_read_only_source(self, file_path: Path) -> bool: ...


def refuse_replace_target(
    target: Path, guard: WriteTargetGuard
) -> Literal["excluded", "symlink", "read_only"] | None:
    """Why a file about to be *replaced* must not be written, or ``None`` (#2488).

    For writers that go through ``atomic_write_text``: ``os.replace`` swaps out a
    symlink at ``target`` rather than writing through it, while ``is_excluded``
    resolves the link and judges whatever it points at. Asked of a link, the
    predicate would answer for a file the write never touches, so a link is
    refused before the predicate is consulted.

    ``read_only`` is the same refusal the chunk-mutation surfaces make, asked
    here because these writers do not go through them. It is checked on the
    target as given: ``is_read_only_source`` resolves the whole path, so a
    *parent* symlink pointing into a protected root is caught even though the
    target itself is an ordinary file — the case root-level disjointness in
    ``IndexingConfig`` cannot see, because neither configured root contains the
    other. The symlink refusal above still comes first: for a link the write
    replaces the link, so what the predicates say about its destination is not
    what the write would do.
    """
    if target.is_symlink():
        return "symlink"
    if guard.is_excluded(target):
        return "excluded"
    if guard.is_read_only_source(target):
        return "read_only"
    return None


def source_span_hash(lines: Sequence[str], start_line: int, end_line: int) -> str | None:
    """Hash a 1-based inclusive source span, or return None for unusable provenance.

    Both indexing and rewriting split the original text with ``splitlines``.
    Joining with LF follows the writer's newline semantics, including ignoring
    the final terminator. No whitespace stripping, Unicode normalization,
    heading removal, or retrieval-text transformations belong in this digest.
    """
    if not 1 <= start_line <= end_line <= len(lines):
        return None
    return hashlib.sha256("\n".join(lines[start_line - 1 : end_line]).encode("utf-8")).hexdigest()
