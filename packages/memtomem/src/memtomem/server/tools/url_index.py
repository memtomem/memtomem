"""Tool: mem_fetch — fetch a URL and index its content."""

from __future__ import annotations


from memtomem.errors import ConfigError
from memtomem.memory_scope import require_user_base
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools.multi_agent import _resolve_agent_namespace


@mcp.tool()
@tool_handler
@register("importers")
async def mem_fetch(
    url: str,
    tags: list[str] | None = None,
    namespace: str | None = None,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Fetch a URL, convert to markdown, and index it for search.

    Supports HTML pages (converted to markdown), plain text, and raw content.
    The fetched content is saved as a .md file in the first memory directory
    and immediately indexed.

    Args:
        url: The URL to fetch and index
        tags: Optional tags to apply to indexed chunks
        namespace: Namespace for indexed chunks (default: config default)
        force_unsafe: Bypass the redaction guard when the fetched page matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line (see ``mem_add_redaction_stats``).
            It never applies to a ``project_shared`` destination — that
            combination is hard-refused, because git history cannot be
            retracted from clones.
    """
    from memtomem.indexing.url_fetcher import fetch_url

    if not url.startswith(("http://", "https://")):
        return "Error: URL must start with http:// or https://"

    app = await _get_app_initialized(ctx)
    try:
        memory_dir = require_user_base(app.config.indexing.memory_dirs)
    except ConfigError as exc:
        return f"Error: {exc}"
    output_dir = memory_dir / "_fetched"

    from memtomem.config import classify_scope
    from memtomem.indexing.url_fetcher import FetchPrivacyError

    scope, _ = classify_scope(output_dir, app.config.indexing.project_memory_dirs)
    try:
        file_path = await fetch_url(url, output_dir, force_unsafe=force_unsafe, scope=scope)
    except FetchPrivacyError:
        return "Fetch blocked by the redaction guard; no file was written."
    except Exception as exc:
        return f"Error fetching URL: {exc}"

    # Index the fetched file. ADR-0006 PR-A: fetched content is un-adjudicated,
    # so the engine redaction gate is active — a secret-bearing page is saved
    # but not indexed, and surfaced here instead of silently reporting success.
    from memtomem.indexing.engine import PrivacyRejection

    effective_ns = namespace or _resolve_agent_namespace(app, None)
    try:
        stats = await app.index_engine.index_file(
            file_path, namespace=effective_ns, already_scanned=True
        )
    except PrivacyRejection as exc:
        app.search_pipeline.invalidate_cache()
        return (
            f"Fetched but NOT indexed — blocked by the redaction guard: {url}\n"
            f"- Saved to: {file_path}\n"
            f"- Secret-pattern hits: {exc.hit_count}. Review the file; re-index "
            f"with 'mm index --force-unsafe' if it is a false positive."
        )

    # Apply tags if provided
    if tags and stats.indexed_chunks > 0:
        chunks = await app.storage.list_chunks_by_source(file_path)
        updated = []
        for c in chunks:
            merged = set(c.metadata.tags) | set(tags)
            if merged != set(c.metadata.tags):
                c.metadata = c.metadata.__class__(
                    **{
                        **{f: getattr(c.metadata, f) for f in c.metadata.__dataclass_fields__},
                        "tags": tuple(sorted(merged)),
                    }
                )
                updated.append(c)
        if updated:
            await app.storage.upsert_chunks(updated)

    app.search_pipeline.invalidate_cache()

    return (
        f"Fetched and indexed: {url}\n"
        f"- Saved to: {file_path}\n"
        f"- Chunks indexed: {stats.indexed_chunks}"
    )
