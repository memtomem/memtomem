"""Result formatting functions for search output."""

from __future__ import annotations

import sys
from pathlib import PurePosixPath


def _display_path(path) -> str:
    """Return a user-friendly path string.

    On macOS, /tmp is a symlink to /private/tmp. Resolve back to the
    user-facing path so output isn't confusing.
    """
    s = str(path)
    if sys.platform == "darwin" and s.startswith("/private/tmp/"):
        return s[len("/private") :]
    return s


def _short_path(path) -> str:
    """Return just the filename from a path."""
    return PurePosixPath(_display_path(path)).name


def _format_results(results: list, *, verbose: bool = False) -> str:
    """Format search results."""
    parts: list[str] = []
    for r in results:
        parts.append(_format_single_result(r, verbose=verbose))
    return f"Found {len(results)} results:\n\n" + "\n\n".join(parts)


def _format_single_result(r, *, verbose: bool = False) -> str:
    """Format a single SearchResult."""
    if verbose:
        return _format_verbose_result(r)
    return _format_compact_result(r)


def _format_compact_result(r) -> str:
    """Compact format: minimal tokens, no UUID, short path, score 2dp."""
    meta = r.chunk.metadata
    hierarchy = " > ".join(meta.heading_hierarchy) if meta.heading_hierarchy else ""
    ns_badge = f" [{meta.namespace}]" if meta.namespace != "default" else ""
    source = _short_path(meta.source_file)

    header = f"[{r.rank}] {r.score:.2f} |{ns_badge} {source}"
    if hierarchy:
        header += f" > {hierarchy}"

    ctx = getattr(r, "context", None)
    if ctx and (ctx.window_before or ctx.window_after):
        pos_info = f" [{ctx.chunk_position}/{ctx.total_chunks_in_file}]"
        parts = [header + pos_info]
        if ctx.window_before:
            for wc in ctx.window_before:
                parts.append(f"...{wc.content[-200:]}")
        parts.append(r.chunk.content[:500])
        if ctx.window_after:
            for wc in ctx.window_after:
                parts.append(f"{wc.content[:200]}...")
        return "\n".join(parts)

    return header + "\n" + r.chunk.content[:500]


def _format_verbose_result(r) -> str:
    """Verbose format: full UUID, full path, score 4dp, code blocks."""
    meta = r.chunk.metadata
    hierarchy = " > ".join(meta.heading_hierarchy) if meta.heading_hierarchy else ""
    ns_badge = f" [{meta.namespace}]" if meta.namespace != "default" else ""

    chunk_id = str(r.chunk.id)
    source = _display_path(meta.source_file)
    header = f"**[{r.rank}]** score={r.score:.4f} | id={chunk_id} |{ns_badge} {source}" + (
        f" | {hierarchy}" if hierarchy else ""
    )

    ctx = getattr(r, "context", None)
    if ctx and (ctx.window_before or ctx.window_after):
        pos_info = f"[chunk {ctx.chunk_position}/{ctx.total_chunks_in_file}]"
        parts = [f"{header} {pos_info}"]
        if ctx.window_before:
            parts.append("--- context before ---")
            for wc in ctx.window_before:
                parts.append(f"...{wc.content[-200:]}")
        parts.append("--- matched ---")
        parts.append(f"```\n{r.chunk.content[:500]}\n```")
        if ctx.window_after:
            parts.append("--- context after ---")
            for wc in ctx.window_after:
                parts.append(f"{wc.content[:200]}...")
        return "\n".join(parts)

    return header + f"\n```\n{r.chunk.content[:500]}\n```"
