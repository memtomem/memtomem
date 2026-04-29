"""Source file management endpoints."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from memtomem.config import memory_dir_kind
from memtomem.indexing.engine import resolve_owning_memory_dir
from memtomem.web.deps import get_config, get_storage, require_indexed_source
from memtomem.web.schemas.core import DeleteResponse
from memtomem.web.schemas.sources import ChunkSizeBucket, SourceOut, SourcesResponse

router = APIRouter(prefix="/sources", tags=["sources"])


@router.get("", response_model=SourcesResponse)
async def list_sources(
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    kind: Literal["memory", "general"] | None = Query(
        None,
        description=(
            "Filter to one bucket of the Sources page sub-toggle. "
            "``memory`` keeps only sources under a configured memory_dir "
            "whose kind is ``memory``; ``general`` keeps the rest, "
            "including orphan sources whose owning dir is no longer "
            "registered (so they don't disappear from the UI)."
        ),
    ),
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> SourcesResponse:
    rows = await storage.get_source_files_with_counts()
    configured_dirs = list(config.indexing.memory_dirs)
    all_sources: list[SourceOut] = []
    for p, cnt, last_indexed_iso, ns_csv, avg_tok, min_tok, max_tok in sorted(rows):
        last_indexed_at: datetime | None = None
        if last_indexed_iso:
            try:
                last_indexed_at = datetime.fromisoformat(last_indexed_iso)
                if last_indexed_at.tzinfo is None:
                    last_indexed_at = last_indexed_at.replace(tzinfo=timezone.utc)
            except (ValueError, TypeError):
                pass

        file_size: int | None = None
        try:
            file_size = p.stat().st_size
        except OSError:
            pass

        namespaces = ns_csv.split(",") if ns_csv else ["default"]

        owning_dir = resolve_owning_memory_dir(p, configured_dirs)
        source_kind: Literal["memory", "general"] | None
        memory_dir_str: str | None
        if owning_dir is None:
            # Orphan: indexed source whose configured dir was removed
            # after indexing. Show in General view rather than hiding so
            # users can still find and prune the chunks; ``kind=None``
            # signals that the categorisation is unknowable, not that
            # the source is "general" by intent.
            source_kind = None
            memory_dir_str = None
        else:
            source_kind = memory_dir_kind(owning_dir)
            memory_dir_str = str(owning_dir)

        if kind is not None:
            # Orphans (kind=None) ride along with ``general`` so they
            # remain reachable from the Sources page; filtering to
            # ``memory`` excludes them. This is the only place orphans
            # need special handling — every other call site can treat
            # ``kind`` as the authoritative bucket.
            if kind == "memory" and source_kind != "memory":
                continue
            if kind == "general" and source_kind == "memory":
                continue

        all_sources.append(
            SourceOut(
                path=str(p),
                chunk_count=cnt,
                last_indexed_at=last_indexed_at,
                file_size=file_size,
                namespaces=namespaces,
                avg_tokens=avg_tok,
                min_tokens=min_tok,
                max_tokens=max_tok,
                memory_dir=memory_dir_str,
                kind=source_kind,
            )
        )
    total = len(all_sources)
    page = all_sources[offset : offset + limit]
    return SourcesResponse(sources=page, total=total, offset=offset, limit=limit)


@router.delete("", response_model=DeleteResponse)
async def delete_source(
    path: str = Query(..., description="Absolute path of the source file to remove"),
    storage=Depends(get_storage),
) -> DeleteResponse:
    indexed_sources = await storage.get_all_source_files()
    request_path = require_indexed_source(path, indexed_sources)

    deleted = await storage.delete_by_source(request_path)
    return DeleteResponse(deleted=deleted)


@router.get("/content")
async def source_content(
    path: str = Query(..., description="Absolute path of the source file"),
    storage=Depends(get_storage),
):
    """Return the raw text content of an indexed source file (max 1 MB)."""
    indexed_sources = await storage.get_all_source_files()
    request_path = require_indexed_source(path, indexed_sources)

    if not request_path.exists():
        raise HTTPException(status_code=404, detail="Source file not found on disk.")

    # Reject symlinks to prevent traversal via symlinked indexed files
    if request_path.is_symlink():
        raise HTTPException(status_code=403, detail="Symlinked files are not served.")

    size = request_path.stat().st_size
    if size > 1_048_576:
        raise HTTPException(status_code=413, detail="File too large (max 1 MB).")

    try:
        text = request_path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise HTTPException(status_code=500, detail="Cannot read file.") from exc

    return {"path": str(request_path), "content": text, "size": size}


@router.get("/chunk-sizes", response_model=list[ChunkSizeBucket])
async def source_chunk_sizes(
    path: str = Query(..., description="Absolute path of the source file"),
    storage=Depends(get_storage),
) -> list[ChunkSizeBucket]:
    """Return chunk size distribution for a single source file."""
    dist = await storage.get_chunk_size_distribution(source_file=Path(path))
    return [ChunkSizeBucket(**d) for d in dist]
