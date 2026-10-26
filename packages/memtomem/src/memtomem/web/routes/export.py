"""Export / import endpoints."""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from memtomem.errors import EmbeddingPolicyChangedError
from memtomem.web.deps import get_config, get_embedder, get_storage
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
    from memtomem.tools.bundle_format import BundleSizeError, StoredBundleValidationError

    try:
        bundle = await export_chunks(
            storage,
            source_filter=source,
            tag_filter=tag,
            since=since,
            namespace_filter=namespace,
        )
    except BundleSizeError as exc:
        raise HTTPException(status_code=413, detail=str(exc)) from exc
    except StoredBundleValidationError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return Response(
        content=bundle._export_json if bundle._export_json is not None else bundle.to_json(),
        media_type="application/json",
        headers={"Content-Disposition": "attachment; filename=memtomem_export.json"},
    )


@router.get("/stats", response_model=ExportStatsResponse)
async def export_stats(
    source: str | None = Query(None),
    tag: str | None = Query(None),
    since: datetime | None = Query(None),
    namespace: str | None = Query(None),
    storage=Depends(get_storage),
) -> ExportStatsResponse:
    """Return chunk count that would be exported (without downloading)."""
    from memtomem.storage.sqlite_export import read_export

    total, omitted = await read_export(
        storage,
        source_filter=source,
        tag_filter=tag,
        since=since,
        namespace_filter=namespace,
        count_only=True,
    )
    return ExportStatsResponse(total_chunks=total, omitted_held_sources=omitted)


@router.post("/import", response_model=ImportResponse)
async def import_memories(
    file: UploadFile,
    on_conflict: str = Form("skip"),
    preserve_ids: bool = Form(False),
    force_unsafe: bool = Form(False),
    project_root_map: str | None = Form(None),
    confirm_project_shared: bool = Form(False),
    storage=Depends(get_storage),
    embedder=Depends(get_embedder),
    config=Depends(get_config),
) -> ImportResponse:
    """Import chunks from a previously exported JSON bundle (multipart upload).

    Foreign bundles (not exported by this install) are scanned per-record and
    rejected with HTTP 403 ``redaction_blocked`` if a record contains a
    secret-shaped value, unless ``force_unsafe`` is set (ADR-0006 Axis F.3).
    """
    import tempfile
    from pathlib import Path

    from memtomem.tools.export_import import (
        ImportPrivacyError,
        _VALID_ON_CONFLICT,
        import_chunks,
    )

    if not file.filename or not file.filename.endswith(".json"):
        raise HTTPException(status_code=422, detail="Only .json bundle files are accepted.")
    if on_conflict not in _VALID_ON_CONFLICT:
        raise HTTPException(
            status_code=422,
            detail=f"on_conflict must be one of {sorted(_VALID_ON_CONFLICT)}",
        )

    from memtomem.tools.bundle_format import MAX_BUNDLE_BYTES, validate_root_map
    import json

    mapping = None
    if project_root_map is not None:
        try:
            # Reuse duplicate-key rejection, but this input is options, not a bundle.
            def unique(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate mapping")
                    result[key] = value
                return result

            mapping = json.loads(project_root_map, object_pairs_hook=unique)
            validate_root_map(mapping)
        except (ValueError, TypeError, OSError):
            raise HTTPException(status_code=422, detail="Invalid project_root_map") from None
    max_size = MAX_BUNDLE_BYTES
    content = await file.read(max_size + 1)
    if len(content) > max_size:
        raise HTTPException(status_code=413, detail="File too large (max 100 MiB).")
    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        tmp.write(content)
        tmp_path = Path(tmp.name)

    try:
        stats = await import_chunks(
            storage,
            embedder,
            tmp_path,
            on_conflict=on_conflict,  # type: ignore[arg-type]
            preserve_ids=preserve_ids,
            force_unsafe=force_unsafe,
            surface="web_api_import",
            extract_entities=config.indexing.extract_entities,
            indexing_config=config.indexing,
            project_root_map=mapping,
            confirm_project_shared=confirm_project_shared,
        )
    except ImportPrivacyError as exc:
        # Mirrors the web_api_add 403 shape (system.py); ``blocked_records`` is a
        # record count (not a regex-span count). Matched bytes never leave the
        # server. The SPA override toggle is the ADR-0006 PR-B follow-up.
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "redaction_blocked_project_shared"
                if exc.shared_blocked
                else "redaction_blocked",
                "blocked_records": exc.blocked_records,
                "surface": "web_api_import",
            },
        ) from exc
    except EmbeddingPolicyChangedError as exc:
        raise HTTPException(
            status_code=409,
            detail={"detail": "embedding_policy_changed", "message": exc.public_message},
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    finally:
        tmp_path.unlink(missing_ok=True)

    return ImportResponse(
        total_chunks=stats.total_chunks,
        imported_chunks=stats.imported_chunks,
        skipped_chunks=stats.skipped_chunks,
        failed_chunks=stats.failed_chunks,
        conflict_skipped_chunks=stats.conflict_skipped_chunks,
        updated_chunks=stats.updated_chunks,
        restored_relations=stats.restored_relations,
        skipped_relations=stats.skipped_relations,
        failed_relations=stats.failed_relations,
        warnings=stats.warnings,
        missing_source_files=stats.missing_source_files,
        unavailable_source_files=stats.unavailable_source_files,
    )
