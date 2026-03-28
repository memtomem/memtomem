"""Working memory (scratch) endpoints."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException

from memtomem.web.deps import get_config, get_storage
from memtomem.web.schemas.scratch import (
    ScratchDeleteResponse,
    ScratchEntryOut,
    ScratchListResponse,
    ScratchPromoteRequest,
    ScratchPromoteResponse,
    ScratchSetRequest,
    ScratchSetResponse,
)

router = APIRouter(prefix="/scratch", tags=["scratch"])


@router.get("", response_model=ScratchListResponse)
async def list_scratch(
    storage=Depends(get_storage),
) -> ScratchListResponse:
    """List all working memory entries."""
    rows = await storage.scratch_list()
    entries = [ScratchEntryOut(**r) for r in rows]
    return ScratchListResponse(entries=entries, total=len(entries))


@router.post("", response_model=ScratchSetResponse)
async def set_scratch(
    body: ScratchSetRequest,
    storage=Depends(get_storage),
) -> ScratchSetResponse:
    """Set a working memory entry."""
    expires_at = None
    if body.ttl_minutes:
        expires_at = (datetime.now(timezone.utc) + timedelta(minutes=body.ttl_minutes)).isoformat(
            timespec="seconds"
        )
    await storage.scratch_set(
        body.key, body.value, session_id=body.session_id, expires_at=expires_at
    )
    return ScratchSetResponse(key=body.key)


@router.delete("/{key}", response_model=ScratchDeleteResponse)
async def delete_scratch(
    key: str,
    storage=Depends(get_storage),
) -> ScratchDeleteResponse:
    """Delete a working memory entry."""
    deleted = await storage.scratch_delete(key)
    return ScratchDeleteResponse(key=key, deleted=deleted)


@router.post("/{key}/promote", response_model=ScratchPromoteResponse)
async def promote_scratch(
    key: str,
    body: ScratchPromoteRequest,
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> ScratchPromoteResponse:
    """Promote a working memory entry to long-term memory."""
    entry = await storage.scratch_get(key)
    if not entry:
        raise HTTPException(status_code=404, detail=f"Key '{key}' not found")

    from memtomem.tools.memory_writer import append_entry

    if body.file:
        target = Path(body.file).expanduser().resolve()
    else:
        base = Path(config.indexing.memory_dirs[0]).expanduser().resolve()
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = base / f"{date_str}.md"

    append_entry(target, entry["value"], title=body.title, tags=body.tags)

    # Mark as promoted
    await storage.scratch_promote(key)

    return ScratchPromoteResponse(
        key=key,
        promoted=True,
        file=str(target),
        indexed_chunks=1,
    )
