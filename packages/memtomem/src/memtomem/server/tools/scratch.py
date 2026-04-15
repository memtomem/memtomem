"""Tools: mem_scratch_set, mem_scratch_get, mem_scratch_promote."""

from __future__ import annotations

from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register


@mcp.tool()
@tool_handler
@register("scratch")
async def mem_scratch_set(
    key: str,
    value: str,
    ttl_minutes: int | None = None,
    ctx: CtxType = None,
) -> str:
    """Store a value in working memory (scratchpad).

    Working memory is for temporary data during a task. Entries can
    have a TTL or be bound to the current session for auto-cleanup.

    Args:
        key: Unique key for this entry
        value: The value to store
        ttl_minutes: Auto-expire after this many minutes (omit for manual cleanup)
    """
    from datetime import datetime, timedelta, timezone

    if ttl_minutes is not None and ttl_minutes <= 0:
        return f"Error: ttl_minutes must be a positive number, got {ttl_minutes}."
    app = _get_app(ctx)
    expires_at = None
    if ttl_minutes is not None and ttl_minutes > 0:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(
            timespec="seconds"
        )

    await app.storage.scratch_set(
        key, value, session_id=app.current_session_id, expires_at=expires_at
    )

    parts = [f"Stored: {key}"]
    if ttl_minutes:
        parts.append(f" (expires in {ttl_minutes}m)")
    if app.current_session_id:
        parts.append(f" [session: {app.current_session_id[:8]}...]")
    return "".join(parts)


@mcp.tool()
@tool_handler
@register("scratch")
async def mem_scratch_get(
    key: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Retrieve a value from working memory, or list all entries if no key given.

    Args:
        key: The key to look up. Omit to list all entries.
    """
    app = _get_app(ctx)

    if key is None:
        entries = await app.storage.scratch_list(session_id=app.current_session_id)
        if not entries:
            return "Working memory is empty."
        lines = [f"Working memory: {len(entries)} entries\n"]
        for e in entries:
            ttl = f" (expires: {e['expires_at']})" if e.get("expires_at") else ""
            promoted = " [promoted]" if e.get("promoted") else ""
            preview = e["value"][:60].replace("\n", " ")
            lines.append(f"  {e['key']}: {preview}...{ttl}{promoted}")
        return "\n".join(lines)

    entry = await app.storage.scratch_get(key)
    if entry is None:
        return f"Key '{key}' not found in working memory."

    parts = [f"**{entry['key']}**: {entry['value']}"]
    if entry.get("expires_at"):
        parts.append(f"\nExpires: {entry['expires_at']}")
    if entry.get("promoted"):
        parts.append("\n(promoted to long-term memory)")
    return "\n".join(parts)


@mcp.tool()
@tool_handler
@register("scratch")
async def mem_scratch_promote(
    key: str,
    title: str | None = None,
    tags: list[str] | None = None,
    file: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Promote a working memory entry to long-term memory.

    Saves the entry via mem_add and marks it as promoted in the scratchpad.

    Args:
        key: The working memory key to promote
        title: Optional title for the long-term entry
        tags: Optional tags
        file: Target file for the entry
    """
    from memtomem.server.tools.memory_crud import mem_add

    app = _get_app(ctx)
    entry = await app.storage.scratch_get(key)

    if entry is None:
        return f"Key '{key}' not found in working memory."

    # Save to long-term via mem_add
    result = await mem_add(
        content=entry["value"],
        title=title or key,
        tags=tags,
        file=file,
        ctx=ctx,
    )

    # Mark as promoted
    await app.storage.scratch_promote(key)

    return f"Promoted '{key}' to long-term memory.\n{result}"
