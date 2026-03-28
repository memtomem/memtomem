"""Result formatting functions for search output."""

from __future__ import annotations

import sys


def _display_path(path) -> str:
    """Return a user-friendly path string.

    On macOS, /tmp is a symlink to /private/tmp. Resolve back to the
    user-facing path so output isn't confusing.
    """
    s = str(path)
    if sys.platform == "darwin" and s.startswith("/private/tmp/"):
        return s[len("/private"):]
    return s


def _format_results(results: list) -> str:
    """Format search results."""
    parts: list[str] = []
    for r in results:
        parts.append(_format_single_result(r))
    return f"Found {len(results)} results:\n\n" + "\n\n".join(parts)


def _format_single_result(r) -> str:
    """Format a single SearchResult."""
    meta = r.chunk.metadata
    hierarchy = " > ".join(meta.heading_hierarchy) if meta.heading_hierarchy else ""
    ns_badge = f" [{meta.namespace}]" if meta.namespace != "default" else ""

    chunk_id = str(r.chunk.id)
    source = _display_path(meta.source_file)
    return (
        f"**[{r.rank}]** score={r.score:.4f} | id={chunk_id} |{ns_badge} {source}"
        + (f" | {hierarchy}" if hierarchy else "")
        + f"\n```\n{r.chunk.content[:500]}\n```"
    )
