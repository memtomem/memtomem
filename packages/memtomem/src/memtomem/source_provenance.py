"""Source-line evidence, independent of the text prepared for retrieval (#2371)."""

from __future__ import annotations

import hashlib
from collections.abc import Sequence


SOURCE_READ_ONLY_DETAIL = (
    "source_read_only: this chunk is a source fragment or projection. "
    "Edit the original source file and reindex it."
)


STALE_SOURCE_PROVENANCE_DETAIL = (
    "Chunk source-line provenance is stale; reindex the source and retry."
)


class StaleSourceProvenanceError(ValueError):
    """A rewrite refused before writing: its indexed source span cannot be verified."""


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
