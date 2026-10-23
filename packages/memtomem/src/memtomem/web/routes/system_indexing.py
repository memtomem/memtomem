"""Indexing, SSE stream, file upload, and memory-add endpoints."""

from __future__ import annotations

import asyncio as _asyncio
import contextlib
import json
import logging
import stat
from collections.abc import AsyncGenerator
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from memtomem.errors import NamespaceResolutionError
from memtomem.source_provenance import (
    EXCLUDED_TARGET_DETAIL,
    READ_ONLY_TARGET_DETAIL,
    ExcludedSourceError,
    ReadOnlySourceError,
)
from memtomem.tools.memory_writer import append_entry
from memtomem.web.deps import (
    get_config,
    get_index_engine,
    get_project_root,
    get_search_pipeline,
    get_storage,
    require_configured,
)
from memtomem.web.routes._errors import (
    NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL,
    _redact_message,
    index_blocked_http,
    privacy_index_blocked_http,
)
from memtomem.web.schemas.memory import (
    AddMemoryRequest,
    AddMemoryResponse,
    IndexRequest,
    IndexResponse,
    PreviewNamespaceRequest,
    PreviewNamespaceResponse,
    UploadFileResult,
    UploadResponse,
    UploadUsageResponse,
)

logger = logging.getLogger("memtomem.web.routes.system")

router = APIRouter(tags=["system"])

_ADD_PATH_ERROR = "File path must be a relative path contained by the configured memory directory"


def _resolve_add_target(base: Path, raw: str) -> Path:
    """Resolve an ``/api/add`` target without platform or symlink escapes."""
    if "\x00" in raw:
        raise ValueError(_ADD_PATH_ERROR)

    # Treat both separator families as separators on every host. Otherwise a
    # Windows drive/UNC path can pass on POSIX and become absolute when the
    # same request reaches a Windows server.
    windows = PureWindowsPath(raw)
    portable = PurePosixPath(raw.replace("\\", "/"))
    if windows.drive or windows.root or portable.is_absolute():
        raise ValueError(_ADD_PATH_ERROR)
    if not portable.parts or any(part in ("", ".", "..") for part in portable.parts):
        raise ValueError(_ADD_PATH_ERROR)

    # A registered memory root is allowed to be absent until its first write.
    # ``strict=False`` still canonicalizes every existing prefix (including
    # symlinks), while letting the safe mkdir below create the new root.
    root = base.expanduser().resolve(strict=False)
    lexical = root.joinpath(*portable.parts)

    # Reject every existing symlink component, even one that currently points
    # inside the root. Following it would leave a swap-to-external TOCTOU gap.
    current = root
    for part in portable.parts:
        current = current / part
        try:
            if stat.S_ISLNK(current.lstat().st_mode):
                raise ValueError(_ADD_PATH_ERROR)
        except FileNotFoundError:
            break

    resolved = lexical.resolve(strict=False)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError(_ADD_PATH_ERROR) from exc
    return resolved


def _revalidate_add_target(base: Path, target: Path) -> None:
    """Repeat containment and component checks immediately before writing."""
    root = base.expanduser().resolve(strict=True)
    try:
        relative = target.relative_to(root)
    except ValueError as exc:
        raise ValueError(_ADD_PATH_ERROR) from exc
    if _resolve_add_target(root, relative.as_posix()) != target:
        raise ValueError(_ADD_PATH_ERROR)


#: Per-root twin of ``NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL`` for ``/api/reindex``.
#: Deliberately does NOT promise "nothing was changed": this route walks every
#: registered root, so by the time one root's lookup fails the earlier roots
#: have really been indexed, and their entries are in the same response.
_REINDEX_ROOT_NAMESPACE_ERROR = (
    "Could not determine the stored namespace for one or more files in this "
    "root; it was not indexed. Other roots in this response may have "
    "completed. Retry once the chunk store is reachable."
)


@router.post("/reindex", dependencies=[Depends(require_configured)])
async def reindex_all(
    force: bool = False,
    config=Depends(get_config),
    index_engine=Depends(get_index_engine),
    search_pipeline=Depends(get_search_pipeline),
):
    """Re-index every registered index root (user-tier + project-tier per ADR-0011)."""
    results: list[dict] = []
    for d in config.indexing.all_index_roots():
        resolved = d.expanduser().resolve()
        if not resolved.is_dir():
            results.append(
                {
                    "path": str(resolved),
                    "error": "not a directory",
                    "errors": [f"{resolved}: not a directory"],
                    "retryable_errors": [],
                }
            )
            continue
        try:
            stats = await index_engine.index_path(resolved, recursive=True, force=force)
        except NamespaceResolutionError as exc:
            logger.warning("Namespace lookup failed while reindexing %s: %s", resolved, exc)
            results.append(
                {
                    "path": str(resolved),
                    "errors": [_REINDEX_ROOT_NAMESPACE_ERROR],
                    "retryable_errors": [_REINDEX_ROOT_NAMESPACE_ERROR],
                }
            )
            continue
        # Per root, not once after the loop (#2141): a later root that raises
        # must not discard the cache drop the roots before it already earned.
        if stats.mutated:
            search_pipeline.invalidate_cache()
        entry: dict = {
            "path": str(resolved),
            "total_files": stats.total_files,
            "indexed_chunks": stats.indexed_chunks,
            "skipped_chunks": stats.skipped_chunks,
            "deleted_chunks": stats.deleted_chunks,
            "duration_ms": stats.duration_ms,
            "blocked_files": stats.blocked_files,
            "blocked_project_shared_files": stats.blocked_project_shared_files,
            "exempted_files": stats.exempted_files,
            "errors": list(stats.errors),
            "retryable_errors": list(stats.retryable_errors),
            "namespaces_preserved_against_rules": stats.namespaces_preserved_against_rules,
            "chunks_missing_vectors": stats.chunks_missing_vectors,
            "namespaces_reassigned": stats.namespaces_reassigned,
            "namespace_moves": list(stats.namespace_moves),
        }
        if stats.blocked_files:
            entry["blocked_paths"] = list(stats.blocked_paths)
        if stats.exempted_files:
            entry["exempted_paths"] = list(stats.exempted_paths)
        results.append(entry)
    all_errors = [e for r in results for e in r.get("errors", [])]
    all_retryable_errors = [
        error for result in results for error in result.get("retryable_errors", [])
    ]
    total_blocked = 0
    total_blocked_ps = 0
    total_exempted = 0
    for r in results:
        b = r.get("blocked_files", 0)
        total_blocked += b if isinstance(b, int) else 0
        bps = r.get("blocked_project_shared_files", 0)
        total_blocked_ps += bps if isinstance(bps, int) else 0
        ex = r.get("exempted_files", 0)
        total_exempted += ex if isinstance(ex, int) else 0
    return {
        "ok": len(all_errors) == 0,
        "results": results,
        "errors": all_errors,
        "retryable_errors": all_retryable_errors,
        "blocked_files": total_blocked,
        "blocked_project_shared_files": total_blocked_ps,
        "exempted_files": total_exempted,
    }


@router.get("/indexing/active", dependencies=[Depends(require_configured)])
async def indexing_active(index_engine=Depends(get_index_engine)) -> JSONResponse:
    """Report whether any indexing run is in flight server-side."""
    return JSONResponse(
        {"active": index_engine.is_active},
        headers={"Cache-Control": "no-store"},
    )


class _ClosingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that closes its body generator when it is done."""

    def __init__(self, content: AsyncGenerator[Any, None], **kwargs: Any) -> None:
        if not isinstance(content, AsyncGenerator):
            raise TypeError(
                f"{type(self).__name__} requires an async generator body, "
                f"got {type(content).__name__}"
            )
        super().__init__(content, **kwargs)

    async def __call__(self, scope, receive, send) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            try:
                await self.body_iterator.aclose()
            except BaseException:
                logger.warning("Failed to close index-stream body", exc_info=True)
            raise
        await self.body_iterator.aclose()


@router.post("/index/stream", dependencies=[Depends(require_configured)])
async def index_stream(
    req: IndexRequest,
    index_engine=Depends(get_index_engine),
    search_pipeline=Depends(get_search_pipeline),
) -> StreamingResponse:
    """Stream CSRF-protected indexing progress as Server-Sent Events."""
    resolved = Path(req.path).expanduser().resolve()

    async def _generate():
        try:
            async with contextlib.aclosing(
                index_engine.index_path_stream(
                    resolved,
                    recursive=req.recursive,
                    force=req.force,
                    namespace=req.namespace,
                    force_unsafe=req.force_unsafe,
                    path_scope="explicit",
                )
            ) as stream:
                async for event in stream:
                    yield f"data: {json.dumps(event)}\n\n"
        except NamespaceResolutionError as exc:
            logger.warning("Namespace lookup failed during index stream: %s", exc)
            error_event = {
                "type": "error",
                "retryable": True,
                "message": NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL,
            }
            yield f"data: {json.dumps(error_event)}\n\n"
        except Exception as exc:
            error_event = {"type": "error", "message": _redact_message(str(exc))}
            yield f"data: {json.dumps(error_event)}\n\n"
        finally:
            search_pipeline.invalidate_cache()

    return _ClosingStreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@router.get("/index/stream")
async def index_stream_get_disabled() -> None:
    raise HTTPException(status_code=405, detail="Use POST /api/index/stream")


@router.post("/index", response_model=IndexResponse, dependencies=[Depends(require_configured)])
async def trigger_index(
    req: IndexRequest = IndexRequest(),
    index_engine=Depends(get_index_engine),
    search_pipeline=Depends(get_search_pipeline),
) -> IndexResponse:
    resolved = Path(req.path).expanduser().resolve()
    try:
        stats = await index_engine.index_path(
            resolved,
            recursive=req.recursive,
            force=req.force,
            force_unsafe=req.force_unsafe,
            namespace=req.namespace,
            path_scope="explicit",
        )
    except NamespaceResolutionError as exc:
        raise HTTPException(
            status_code=503,
            detail=NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL,
        ) from exc
    if stats.mutated:
        search_pipeline.invalidate_cache()
    return IndexResponse(
        total_files=stats.total_files,
        total_chunks=stats.total_chunks,
        indexed_chunks=stats.indexed_chunks,
        skipped_chunks=stats.skipped_chunks,
        deleted_chunks=stats.deleted_chunks,
        duration_ms=stats.duration_ms,
        errors=list(stats.errors) if stats.errors else [],
        retryable_errors=list(stats.retryable_errors),
        resolved_namespaces=list(stats.resolved_namespaces),
        applied_namespaces=list(stats.applied_namespaces),
        blocked_files=stats.blocked_files,
        blocked_paths=list(stats.blocked_paths),
        blocked_project_shared_files=stats.blocked_project_shared_files,
        exempted_files=stats.exempted_files,
        exempted_paths=list(stats.exempted_paths),
        namespaces_preserved_against_rules=stats.namespaces_preserved_against_rules,
        chunks_missing_vectors=stats.chunks_missing_vectors,
        namespaces_reassigned=stats.namespaces_reassigned,
        namespace_moves=list(stats.namespace_moves),
    )


_PREVIEW_FILE_CAP = 200


@router.post(
    "/index/preview-namespace",
    response_model=PreviewNamespaceResponse,
    dependencies=[Depends(require_configured)],
)
async def preview_namespace(
    body: PreviewNamespaceRequest,
    index_engine=Depends(get_index_engine),
) -> PreviewNamespaceResponse:
    """Preview which namespace(s) would be applied if ``path`` were indexed."""
    resolved = Path(body.path).expanduser().resolve()
    files = index_engine.discover_indexable_files(resolved, body.recursive, path_scope="explicit")
    truncated = len(files) > _PREVIEW_FILE_CAP
    walked = files[:_PREVIEW_FILE_CAP]
    try:
        resolved_namespaces = await index_engine.resolve_namespaces_for(walked)
    except NamespaceResolutionError as exc:
        raise HTTPException(
            status_code=503,
            detail=NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL,
        ) from exc
    return PreviewNamespaceResponse(
        resolved_namespaces=resolved_namespaces,
        truncated=truncated,
        scanned_files=len(walked),
    )


_ALLOWED_UPLOAD_EXTS = {".md", ".txt", ".json", ".yaml", ".yml", ".toml"}


@router.post(
    "/upload",
    response_model=UploadResponse,
    dependencies=[Depends(require_configured)],
    openapi_extra={
        "requestBody": {
            "required": True,
            "content": {
                "multipart/form-data": {
                    "schema": {
                        "type": "object",
                        "required": ["files"],
                        "properties": {
                            "files": {
                                "type": "array",
                                "items": {"type": "string", "format": "binary"},
                            }
                        },
                    }
                }
            },
        }
    },
)
async def upload_files(
    request: Request,
    force_unsafe: bool = False,
    index_engine=Depends(get_index_engine),
    search_pipeline=Depends(get_search_pipeline),
) -> UploadResponse:
    """Upload one or more files, save to ~/.memtomem/uploads/, and index them."""
    from memtomem import privacy
    from memtomem.indexing.engine import PrivacyRejection
    from memtomem.web.upload_quarantine import (
        QuarantinedUpload,
        UploadQuarantineError,
        promote_no_overwrite,
        quarantine_uploads,
    )

    upload_dir = Path("~/.memtomem/uploads").expanduser()
    if index_engine.is_read_only_source(upload_dir):
        raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)

    try:
        async with quarantine_uploads(request, upload_dir) as quarantined:
            decisions: list[tuple[QuarantinedUpload, str, bool, str | None, str, bool]] = []
            for item in quarantined:
                fname = item.filename
                suffix = Path(fname).suffix.lower()
                if suffix not in _ALLOWED_UPLOAD_EXTS:
                    decisions.append((item, fname, False, f"Unsupported type: {suffix}", "", False))
                    continue
                try:
                    content = await _asyncio.to_thread(item.path.read_bytes)
                    text = content.decode("utf-8")
                except UnicodeDecodeError as exc:
                    decisions.append((item, fname, False, f"Decode failed: {exc}", "", False))
                    continue
                except Exception:
                    logger.exception("Upload adjudication failed for %s", fname)
                    decisions.append((item, fname, False, "Upload processing failed", "", False))
                    continue

                try:
                    guard = privacy.enforce_write_guard(
                        text,
                        surface="web_api_upload",
                        force_unsafe=force_unsafe,
                        audit_context={"filename": fname},
                    )
                except Exception:
                    logger.exception("Upload privacy check failed for %s", fname)
                    decisions.append((item, fname, False, "Upload processing failed", "", False))
                    continue
                if guard.decision == "blocked":
                    decisions.append(
                        (
                            item,
                            fname,
                            False,
                            f"redaction_blocked (hits={len(guard.hits)})",
                            "",
                            False,
                        )
                    )
                else:
                    decisions.append((item, fname, True, None, text, guard.decision == "bypassed"))

            results: list[UploadFileResult] = []
            for item, fname, accepted, error, guarded_text, bypassed in decisions:
                if not accepted:
                    results.append(UploadFileResult(filename=fname, indexed_chunks=0, error=error))
                    continue
                dest: Path | None = None
                try:
                    dest = promote_no_overwrite(
                        item.path, upload_dir, fname, index_guard=index_engine
                    )
                    stats = await index_engine.index_file(
                        dest,
                        ingress_guarded=True,
                        force_unsafe=bypassed,
                        valve_text=guarded_text,
                    )
                    if stats.mutated:
                        search_pipeline.invalidate_cache()
                    results.append(
                        UploadFileResult(
                            filename=fname,
                            indexed_chunks=stats.indexed_chunks,
                            path=str(dest),
                        )
                    )
                except ExcludedSourceError:
                    results.append(
                        UploadFileResult(filename=fname, indexed_chunks=0, error="source_excluded")
                    )
                except PrivacyRejection as exc:
                    results.append(
                        UploadFileResult(
                            filename=fname,
                            indexed_chunks=0,
                            path=str(dest) if dest is not None else None,
                            error=exc.error_detail(),
                        )
                    )
                except ReadOnlySourceError:
                    results.append(
                        UploadFileResult(filename=fname, indexed_chunks=0, error="source_read_only")
                    )
                except Exception:
                    logger.exception("Upload processing failed for %s", fname)
                    results.append(
                        UploadFileResult(
                            filename=fname,
                            indexed_chunks=0,
                            path=str(dest) if dest is not None and dest.exists() else None,
                            error="Upload processing failed",
                        )
                    )

            return UploadResponse(
                files=results,
                total_indexed=sum(r.indexed_chunks for r in results),
            )
    except UploadQuarantineError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    except BaseException as exc:
        if isinstance(exc, _asyncio.CancelledError):
            raise
        logger.exception("Upload batch processing failed")
        raise HTTPException(status_code=500, detail="Upload processing failed") from exc


@router.get("/uploads/usage", response_model=UploadUsageResponse)
async def uploads_usage() -> UploadUsageResponse:
    """Cumulative disk footprint of files saved via /api/upload."""
    upload_dir = Path("~/.memtomem/uploads").expanduser()
    if not upload_dir.is_dir():
        return UploadUsageResponse(file_count=0, total_bytes=0, oldest_mtime=None)
    file_count = 0
    total_bytes = 0
    oldest: float | None = None
    for entry in upload_dir.iterdir():
        if not entry.is_file():
            continue
        try:
            st = entry.stat()
        except OSError:
            continue
        file_count += 1
        total_bytes += st.st_size
        if oldest is None or st.st_mtime < oldest:
            oldest = st.st_mtime
    return UploadUsageResponse(file_count=file_count, total_bytes=total_bytes, oldest_mtime=oldest)


@router.post("/add", response_model=AddMemoryResponse, dependencies=[Depends(require_configured)])
async def add_memory(
    req: AddMemoryRequest,
    request: Request,
    index_engine=Depends(get_index_engine),
    storage=Depends(get_storage),
    config=Depends(get_config),
    server_project_root=Depends(get_project_root),
    search_pipeline=Depends(get_search_pipeline),
) -> AddMemoryResponse:
    from datetime import datetime, timezone

    from memtomem import privacy

    if req.scope == "project_shared" and not req.confirm_project_shared:
        logger.info(
            "web add_memory rejected project_shared write without confirmation",
            extra={"file": req.file, "namespace": req.namespace},
        )
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "blocked_project_shared",
                "surface": "web_api_add",
                "scope": req.scope,
                "message": (
                    "scope='project_shared' writes to a git-tracked directory. "
                    "Re-submit with confirm_project_shared=true to proceed."
                ),
                "cli_hint": "mm add --scope project_shared",
                "docs_url": (
                    "https://github.com/memtomem/memtomem/blob/main/docs/adr/"
                    "0011-canonical-artifact-scope-hierarchy.md"
                ),
            },
        )

    if req.scope == "project_shared":
        privacy.emit_project_shared_confirmation(
            surface="web_api_add",
            mechanism="request",
            audit_context={"namespace": req.namespace},
        )

    guard = privacy.enforce_write_guard(
        req.content,
        surface="web_api_add",
        force_unsafe=req.force_unsafe,
        scope=req.scope,
        audit_context={
            "namespace": req.namespace,
            "file": req.file,
            "scope": req.scope,
        },
    )
    if guard.decision == "blocked":
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "redaction_blocked",
                "hits": len(guard.hits),
                "surface": "web_api_add",
            },
        )
    if guard.decision == "blocked_project_shared":
        raise HTTPException(
            status_code=403,
            detail={
                "detail": "blocked_project_shared",
                "hits": len(guard.hits),
                "surface": "web_api_add",
                "message": (
                    "force_unsafe is not permitted on scope='project_shared' "
                    "writes (git history is forever). Re-submit with "
                    "scope='project_local' or scope='user' to bypass, or "
                    "hand-edit the canonical file."
                ),
            },
        )

    if req.scope == "user":
        from memtomem.memory_scope import require_user_base

        base = require_user_base(config.indexing.memory_dirs, config.indexing.project_memory_dirs)
    else:
        from memtomem.memory_scope import (
            MemoryScopeError,
            is_project_tier_registered,
            project_tier_registration_error,
            resolve_memory_scope_dir,
        )
        from memtomem.runtime.project_context import _resolve_project_context_from_dirs

        pmdirs = config.indexing.project_memory_dirs
        project_root = _resolve_project_context_from_dirs(pmdirs)
        if project_root is None:
            project_root = Path(server_project_root)
        try:
            base = resolve_memory_scope_dir(req.scope, project_root)
        except MemoryScopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        if not is_project_tier_registered(base, pmdirs):
            raise HTTPException(
                status_code=422,
                detail=project_tier_registration_error(base, req.scope),
            )

    if req.file:
        try:
            target = _resolve_add_target(base, req.file)
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=_ADD_PATH_ERROR) from exc
    else:
        from memtomem.memory_scope import MemoryScopeError, day_file_name, resolve_scope_target

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        try:
            target = resolve_scope_target(
                base,
                day_file_name(req.namespace, config.namespace.default_namespace, date_str=date_str),
                scope=req.scope,
                project_memory_dirs=config.indexing.project_memory_dirs,
            )
        except MemoryScopeError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        async_file_lock,
        memory_lock_path,
    )

    tags = req.tags or []
    if index_engine.is_read_only_source(target):
        raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)
    try:
        async with async_file_lock(memory_lock_path(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S):
            if index_engine.is_excluded(target):
                raise HTTPException(status_code=409, detail=EXCLUDED_TARGET_DETAIL)
            if index_engine.is_read_only_source(target):
                raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)
            if not req.allow_namespace_mix:
                from memtomem.memory_scope import namespace_mix_refusal

                mix_err = await namespace_mix_refusal(
                    index_engine=index_engine,
                    storage=storage,
                    target=target,
                    effective_ns=req.namespace,
                    override_hint="allow_namespace_mix=true",
                )
                if mix_err is not None:
                    raise HTTPException(status_code=409, detail=mix_err)
            write_ns = req.namespace
            if write_ns is None:
                from memtomem.models import stored_namespace

                try:
                    write_ns = stored_namespace(await index_engine.effective_namespace_for(target))
                except NamespaceResolutionError as exc:
                    raise HTTPException(
                        status_code=503, detail=NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL
                    ) from exc
            target.parent.mkdir(parents=True, exist_ok=True)
            try:
                _revalidate_add_target(base, target)
            except (OSError, ValueError) as exc:
                raise HTTPException(status_code=422, detail=_ADD_PATH_ERROR) from exc
            from memtomem.indexing.engine import PrivacyRejection
            from memtomem.tools.memory_mutation import appended_valve_text
            from memtomem.tools.memory_writer import read_pre_image_or_empty

            needs_valve = guard.decision == "bypassed"
            pre = await _asyncio.to_thread(read_pre_image_or_empty, target)
            preflight = await _asyncio.to_thread(index_engine.preflight_mutation, target, pre)
            if preflight.rejection is not None:
                rejection = await index_engine.refuse_mutation(target, preflight)
                search_pipeline.invalidate_cache()
                raise privacy_index_blocked_http(rejection, "add")
            blocker = preflight.blocker("add", target.name, needs_valve=needs_valve)
            if blocker is not None:
                raise index_blocked_http(blocker)
            pre_lines = (
                len(target.read_text(encoding="utf-8", errors="replace").splitlines())
                if target.exists()
                else 0
            )
            appended = await _asyncio.to_thread(
                append_entry, target, req.content, title=req.title, tags=tags
            )
            try:
                stats = await index_engine.index_file(
                    target,
                    namespace=write_ns,
                    ingress_guarded=True,
                    force_unsafe=needs_valve,
                    valve_text=appended_valve_text(pre, appended) if needs_valve else None,
                    lock_held=True,
                )
            except PrivacyRejection as exc:
                search_pipeline.invalidate_cache()
                exc.mutation_outcome = "appended"
                raise privacy_index_blocked_http(exc, "add") from exc
            if stats.mutated:
                search_pipeline.invalidate_cache()

            if tags and stats.indexed_chunks > 0:
                chunks = await storage.list_chunks_by_source(target, limit=None)
                updated = []
                for c in chunks:
                    if c.metadata.start_line is not None and c.metadata.start_line <= pre_lines:
                        continue
                    merged = set(c.metadata.tags) | set(tags)
                    if merged != set(c.metadata.tags):
                        c.metadata = c.metadata.__class__(
                            **{
                                **{
                                    f: getattr(c.metadata, f)
                                    for f in c.metadata.__dataclass_fields__
                                },
                                "tags": tuple(sorted(merged)),
                            }
                        )
                        updated.append(c)
                if updated:
                    await storage.upsert_chunks(updated)
                    search_pipeline.invalidate_cache()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Memory file is locked by another writer; try again."
        ) from exc

    return AddMemoryResponse(file=str(target), indexed_chunks=stats.indexed_chunks)
