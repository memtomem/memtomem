"""Source-line evidence, independent of the text prepared for retrieval (#2371)."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Literal


SOURCE_READ_ONLY_DETAIL = (
    "source_read_only: this chunk is a source fragment or projection. "
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


def refuse_replace_target(
    target: Path, is_excluded: Callable[[Path], bool]
) -> Literal["excluded", "symlink"] | None:
    """Why a file about to be *replaced* must not be written, or ``None`` (#2488).

    For writers that go through ``atomic_write_text``: ``os.replace`` swaps out a
    symlink at ``target`` rather than writing through it, while ``is_excluded``
    resolves the link and judges whatever it points at. Asked of a link, the
    predicate would answer for a file the write never touches, so a link is
    refused before the predicate is consulted.
    """
    if target.is_symlink():
        return "symlink"
    if is_excluded(target):
        return "excluded"
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
