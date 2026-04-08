"""Export / import endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, UploadFile
from fastapi.responses import Response

from memtomem.web.deps import get_embedder, get_storage
from memtomem.web.schemas import ExportStatsResponse, ImportResponse

router = APIRouter(prefix="/export", tags=["export"])


@router.get("", response_class=Response)
async def export_memories(
    source: str | None = Query(None, description="Source path substring filter"),
    tag: str | None = Query(None, description="Exact tag filter"),
    since: datetime | None = Query(None, description="ISO datetime lower bound (created_at)"),
    namespace: str | None = Query(None, description="Namespace filter"),
    storage=Depends(get_storage),
) -> Response:
    """Export indexed chunks as a downloadable JSON bundle."""
    from memtomem.tools.export_import import export_chunks

    bundle = await export_chunks(
        storage,
        source_filter=source,
        tag_filter=tag,
        since=since,
        namespace_filter=namespace,
    )
    return Response(
        content=bundle.to_json(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=memtomem_export.json"},
    )


@router.get("/stats", response_model=ExportStatsResponse)
async def export_stats(
    source: str | None = Query(None),
    tag: str | None = Query(None),
    since: datetime | None = Query(None),
    storage=Depends(get_storage),
) -> ExportStatsResponse:
    """Return chunk count that would be exported (without downloading)."""
    from memtomem.tools.export_import import export_chunks

    bundle = await export_chunks(storage, source_filter=source, tag_filter=tag, since=since)
    return ExportStatsResponse(total_chunks=bundle.total_chunks)


@router.post("/import", response_model=ImportResponse)
async def import_memories(
    file: UploadFile,
    storage=Depends(get_storage),
    embedder=Depends(get_embedder),
) -> ImportResponse:
    """Import chunks from a previously exported JSON bundle (multipart upload)."""
    import tempfile
    from pathlib import Path

    from memtomem.tools.export_import import import_chunks

    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=422, detail="Only .json bundle files are accepted.")

    content = await file.read()
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        stats = await import_chunks(storage, embedder, tmp_path)
    finally:
        tmp_path.unlink(missing_ok=True)

    return ImportResponse(
        total_chunks=stats.total_chunks,
        imported_chunks=stats.imported_chunks,
        skipped_chunks=stats.skipped_chunks,
        failed_chunks=stats.failed_chunks,
    )
