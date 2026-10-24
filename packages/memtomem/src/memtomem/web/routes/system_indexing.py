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

# Mounted only through ``system.router``, which carries the ``system`` tag.
router = APIRouter()

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
            # ``error`` (singular) is kept for existing clients, but it alone
            # never reached ``all_errors`` below — so a registered root that
            # had been deleted or renamed produced ``ok: true`` with an empty
            # top-level ``errors``, and every first-party client rendered
            # "reindex complete". Emit the list form too so aggregation sees
            # it. Not retryable: a missing root is a config problem that
            # retrying cannot fix, so it stays out of ``retryable_errors``.
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
            # Per root, not per request (#2005 follow-up): this route walks
            # every registered root, and the roots indexed before this one
            # were really indexed. A 503 for the whole call would discard
            # their results and claim "nothing was changed", which is the one
            # thing a detail string must never say untruthfully — which is
            # also why this does not reuse ``NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL``:
            # that string promises nothing was changed, and on this route the
            # earlier roots may well have been. The entry uses ``errors`` (a
            # list) rather than the ``error`` key the not-a-directory branch
            # above uses, so it reaches ``all_errors`` and flips ``ok`` to false.
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
            # #2076, unconditional for the same reason as ``retryable_errors``
            # below: a zero must be distinguishable from a server that
            # predates the field. ``exempted_paths`` stays conditional, like
            # ``blocked_paths`` — it is a detail of this counter.
            "exempted_files": stats.exempted_files,
            # Both keys are unconditional (``errors`` was previously omitted
            # when empty): a client reading ``retryable_errors`` must be able
            # to tell "this root had no retryable failures" from "this server
            # predates the field", and that only works if a healthy root still
            # carries the empty list. ``blocked_paths`` below stays conditional
            # — it is a detail of the ``blocked_files`` counter, not a
            # standalone classification the client branches on.
            "errors": list(stats.errors),
            "retryable_errors": list(stats.retryable_errors),
            # #2061: this entry is hand-built, so a new ``IndexingStats``
            # field reaches the client only by being listed here. Kept
            # unconditional for the same reason as ``retryable_errors`` — a
            # zero must be distinguishable from a server that predates it.
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
    """Report whether any indexing run is in flight server-side.

    Drives cross-tab / post-reload survival of the header indicator
    introduced in #602 (umbrella #582 item 4.11). Covers ``index_path``,
    ``index_file``, and ``index_path_stream`` uniformly — neither the SSE
    stream nor (since #2105) the bulk ``index_path`` run holds
    ``_index_lock`` for its duration, so we cannot rely on
    ``_index_lock.locked()``.

    Response shape is intentionally minimal (``{"active": bool}``) to
    match the client's single-boolean ``STATE.indexing`` model. Adding
    ``started_at`` / ``path`` / progress fields later is purely additive.

    ``Cache-Control: no-store`` mirrors ``/index/stream``: this endpoint
    is polled every few seconds while a run is in flight, and a cached
    ``{"active": false}`` from an intermediary would mask the
    false→true transition the client is waiting for.
    """
    return JSONResponse(
        {"active": index_engine.is_active},
        headers={"Cache-Control": "no-store"},
    )


class _ClosingStreamingResponse(StreamingResponse):
    """A ``StreamingResponse`` that closes its body generator when it is done.

    #2200: Starlette never closes ``body_iterator``. On a client disconnect it
    unwinds ``stream_response`` — the frame doing the ``async for`` — and
    whether that reaches the body generator depends on where the cancellation
    lands. Cancelled while awaiting the *generator* (``__anext__``), the error
    is thrown into it and its ``finally`` runs; cancelled while awaiting
    ``send`` (backpressure, the common case for an SSE client that stopped
    reading), the generator is left suspended at its ``yield`` and only
    asyncio's async-generator finalizer — a *scheduled* ``aclose`` — gets to
    it. Under uvicorn's ASGI ``spec_version`` 2.3 path both windows are live.

    That distinction matters here because the body generator holds indexing
    state: it drives ``index_path_stream``, whose ``finally`` drops
    ``_active_runs`` and releases the #2180 generation lease. Closing the body
    iterator here makes that release happen in the same unwind as the
    disconnect, on every path out of ``__call__``.
    """

    def __init__(self, content: AsyncGenerator[Any, None], **kwargs: Any) -> None:
        # Async generators only, enforced rather than documented. Starlette
        # accepts any ``AsyncIterable``, and for one of those ``async for``
        # drives whatever ``__aiter__()`` returns — potentially a different
        # object from the one stored here, which would leave us closing
        # something the response never iterated. A generator is its own
        # iterator, so for this response the two cannot diverge.
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
            # Cleanup must not become the error the caller sees: the reason we
            # are unwinding (``ClientDisconnect``, a cancellation, a send
            # failure) is what the server needs to classify this request by.
            # A body whose own cleanup fails must not replace it — including
            # when that failure is a ``CancelledError`` (cancellation landing
            # on the close itself), which is why this catches ``BaseException``
            # and re-raises the original rather than the cleanup's.
            try:
                await self.body_iterator.aclose()
            except BaseException:
                logger.warning("Failed to close index-stream body", exc_info=True)
            raise
        # Normal completion: the body is exhausted and closing it is a no-op,
        # but a failure here is this response's own and belongs to the caller.
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
        # #2141: unconditional, in ``finally``, and deliberately NOT gated on
        # the ``mutated`` flag the events carry. A client can disconnect after
        # a file's chunk transaction commits but before its progress event is
        # produced — the generator is cancelled mid-run and the flag never
        # reaches this frame — so the only sound post-condition here is "this
        # run may have written". Dropping a still-valid cache costs one cold
        # search; keeping a stale one hides a committed write.
        try:
            # #2200: ``aclosing`` rather than a bare ``async for``. This frame
            # is itself a generator, and Starlette abandons it on client
            # disconnect without necessarily unwinding it; the engine releases
            # ``_active_runs`` and the #2180 generation lease in the inner
            # generator's ``finally``, which only runs when that generator is
            # closed. Without this, a disconnected run stays "active" in
            # ``GET /api/indexing/active`` and pins a retired ONNX session
            # until GC finalizes the frame.
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
            # Before the generic branch (#2005 follow-up): SSE has no status
            # code, so the *message* is the only place this surface can say
            # "transient — retry" rather than handing the client a redacted
            # engine string it cannot classify. Since #2018 the stream
            # resolves every file's namespace before its first write (the
            # per-file loop folds later exceptions into that file's errors),
            # so an escaping lookup failure means this run changed nothing —
            # the same post-condition, and the same string, as the
            # single-shot routes.
            logger.warning("Namespace lookup failed during index stream: %s", exc)
            error_event = {
                "type": "error",
                "retryable": True,
                "message": NAMESPACE_LOOKUP_UNAVAILABLE_DETAIL,
            }
            yield f"data: {json.dumps(error_event)}\n\n"
        except Exception as exc:
            # Engine-level failures escape to this handler (per-file errors
            # are caught inside the engine and reported as basenames in the
            # "complete" event). A raw ``str(exc)`` here can embed absolute
            # paths — leaking ``$HOME``/username — or secret-shaped fragments
            # to the client, so route it through the same redactor every
            # other error surface at this trust boundary uses.
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
        # The engine refuses to re-resolve a namespace whose stored value it
        # could not read (#2005) — transient, and nothing was written, so it
        # is a 503 like the chunk-delete path rather than the generic 500 a
        # bug would produce. Stated here rather than by a shared handler: the
        # "nothing was changed" promise in the detail is a claim about this
        # route's own ordering (``_index_path_inner`` resolves every namespace
        # before it writes anything), and only the call site knows that.
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


# Cap on files walked by the preview endpoint. Large memory_dirs (10k+
# files) would otherwise stall the synchronous focus event for seconds;
# the truncated flag lets the UI surface "scanned N+, more not shown".
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
    """Preview which namespace(s) would be applied if ``path`` were indexed.

    Walks the same file set ``trigger_index`` would walk (via
    ``IndexEngine.discover_indexable_files``) and returns the distinct
    namespaces ``_resolve_namespace`` produces with no explicit override.
    Capped at ``_PREVIEW_FILE_CAP`` files to keep focus-event latency
    bounded; ``truncated=True`` flags when the cap was hit.
    """
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
    """Upload one or more files, save to ~/.memtomem/uploads/, and index them.

    Multipart parts first stream into an owner-only temporary disk quarantine.
    Each file's text content then passes through the trust-boundary redaction
    guard before durable promotion into ``uploads/``. A flagged file is
    rejected (``error="redaction_blocked"``) and only the quarantine copy
    exists until context cleanup; ``force_unsafe=True`` (query param) bypasses
    for the whole batch with audit logging.
    """
    from memtomem import privacy
    from memtomem.indexing.engine import PrivacyRejection
    from memtomem.web.upload_quarantine import (
        QuarantinedUpload,
        UploadQuarantineError,
        promote_no_overwrite,
        quarantine_uploads,
    )

    upload_dir = Path("~/.memtomem/uploads").expanduser()
    # Before ``quarantine_uploads``, not inside it: that context manager calls
    # ``prepare_upload_dir`` (which chmods the directory to 0700) and then
    # creates a ``.quarantine-*`` subdirectory and writes the uploaded bytes
    # into it. All three are writes into the destination, so refusing only at
    # promotion time would already have modified a protected directory before
    # deciding not to write to it.
    if index_engine.is_read_only_source(upload_dir):
        raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)

    try:
        async with quarantine_uploads(request, upload_dir) as quarantined:
            # (item, name, accepted, error, the text guarded, whether the valve was used)
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
                    # The engine judges the file again under its own scope.
                    # ``valve_text`` keeps this upload's valve to this upload's
                    # bytes: the index runs without the file's lock, so another
                    # writer may have changed it since the promote.
                    stats = await index_engine.index_file(
                        dest,
                        ingress_guarded=True,
                        force_unsafe=bypassed,
                        valve_text=guarded_text,
                    )
                    # Per file (#2141): a later file in the batch can raise,
                    # and the files already promoted are already searchable.
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
                    # #2488: refused before any candidate name was linked, so the
                    # file exists only in quarantine, which context cleanup removes.
                    results.append(
                        UploadFileResult(filename=fname, indexed_chunks=0, error="source_excluded")
                    )
                except PrivacyRejection as exc:
                    # Promoted but refused by the indexer (e.g. a project tier
                    # where the valve is not honoured); the file is held.
                    results.append(
                        UploadFileResult(
                            filename=fname,
                            indexed_chunks=0,
                            path=str(dest) if dest is not None else None,
                            error=exc.error_detail(),
                        )
                    )
                except ReadOnlySourceError:
                    # Same shape as the refusal above, and ahead of the generic
                    # handler on purpose: falling through would log a traceback
                    # and answer "Upload processing failed", telling the user to
                    # report a bug about a rule they configured themselves.
                    # Nothing was linked, so quarantine cleanup is the only undo.
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
    """Cumulative disk footprint of files saved via /api/upload.

    Read-only directory stat; intentionally **no** ``require_configured``
    gate so the panel surfaces the empty state on a fresh install before
    the user finishes the config wizard.
    """
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

    # ADR-0011 §5 Gate B (PR-F slice 4): project_shared writes go to git;
    # require an explicit ``confirm_project_shared=true`` so the SPA
    # cannot silently commit PII to a tracked tier through a default-bool
    # oversight. Mirrors the MCP ``mem_add`` gate at
    # ``memory_crud.py:204`` and the Web parallel on the chunks DELETE
    # path at ``chunks.py:157``. project_local is NOT Gate B-gated —
    # only the canonical-residency tier choice + ADR-0011 §3's
    # zero-fan-out rule applies. The 4xx body carries the CLI hint /
    # docs link so the SPA renders "rejected, here's the equivalent
    # invocation" without rewriting the prose client-side.
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

    # ADR-0011 §5 Gate B consent (#2306). Gate A below can still refuse:
    # the line records the consent, not the landed write.
    if req.scope == "project_shared":
        privacy.emit_project_shared_confirmation(
            surface="web_api_add",
            mechanism="request",
            audit_context={"namespace": req.namespace},
        )

    # Trust-boundary redaction guard. Mirrors MCP ``mem_add`` so the
    # same secret patterns block writes regardless of which surface
    # the agent or user came in through. ``force_unsafe`` is opt-in
    # via the SPA's confirm-and-retry UX after the first 403. Threading
    # ``scope`` here makes Gate A's hard-refusal of ``force_unsafe=True``
    # on project_shared writes fire on the Web path too (ADR-0011 PR-D
    # round 7 parity — same fix as the chunks PATCH path at
    # ``chunks.py:73-86``).
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

    # ADR-0011 §4 PR-F slice 4: resolve the canonical-residency base
    # directory per tier. User tier stays on ``memory_dirs[0]`` for
    # write-surface parity with MCP ``mem_add`` and the CLI; an empty
    # ``memory_dirs`` refuses with ``ConfigError`` → 409 instead of
    # falling back to the historical ``~/.memtomem/memories`` — the
    # "index nothing" state must not silently write into a directory the
    # active config disabled (#1768). Project tiers route through
    # ``resolve_memory_scope_dir`` against the server's project root so
    # writes land in ``<proj>/.memtomem/memories[/.local]/`` — the same
    # path the MCP ``mem_add(scope=...)`` flow uses. Refuses unregistered
    # project-tier dirs upfront so the row's persisted scope cannot
    # diverge from what the read surface / watcher can actually see.
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

        # ADR-0011 PR-F parity with MCP ``mem_add`` (memory_crud.py:285
        # via search.py:73-96): resolve the *registered* project root
        # containing the server's cwd rather than using ``cwd`` itself.
        # ``app.state.project_root`` was set to ``Path.cwd()`` in the
        # lifespan; if the server is launched from a subdirectory of a
        # registered project, the raw cwd has no ``.memtomem/memories``
        # tree, so ``resolve_memory_scope_dir`` would land on the wrong
        # directory and ``is_project_tier_registered`` would 422 — while
        # MCP correctly writes under the project root. Falls back to
        # the lifespan cwd only if no registered tier covers the cwd
        # (the 422 below then surfaces the operator-actionable
        # "register your project_memory_dirs first" error).
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
        # Issue #2005: one day file per namespace — a shared day file loses one
        # namespace as soon as chunk merging joins two entries. The web surface
        # has no session-derived namespace, so unlike the CLI/MCP paths the
        # name is final here and needs no in-lock re-target.
        from memtomem.memory_scope import MemoryScopeError, day_file_name, resolve_scope_target

        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        # Judged where it resolves (#2807): a link at the day file must not
        # carry the write past the scope Gate B was run for.
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
    # Before the sidecar acquire below, which creates ``.<name>.lock`` next to
    # the target — a write into the very directory a read-only root exists to
    # keep memtomem out of. The in-lock gate stays as the authority on the
    # final target; this one keeps the refusal from leaving a file behind.
    if index_engine.is_read_only_source(target):
        raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)
    # #1587: hold the target file's cross-process sidecar (L2) across append +
    # reindex + tag-merge so a concurrent MCP mem_edit/mem_delete rollback — in
    # this process or another — cannot erase this appended entry. ``mm web`` has
    # no AppContext L1 lock; L2's in-process guard serializes web handlers too.
    # ``lock_held=True`` skips the nested engine acquire.
    try:
        async with async_file_lock(memory_lock_path(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S):
            # #2488: an excluded target would take the append and then re-index
            # to zeroed stats. Ahead of the overridable mix refusal.
            if index_engine.is_excluded(target):
                raise HTTPException(status_code=409, detail=EXCLUDED_TARGET_DETAIL)
            if index_engine.is_read_only_source(target):
                raise HTTPException(status_code=409, detail=READ_ONLY_TARGET_DETAIL)
            # Issue #2005: refuse before appending when the target already
            # holds another namespace — re-chunking would restamp its chunks
            # with this write's namespace. Inside the lock so the check and
            # the write see the same file state.
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
            # Ask the lookup *before* appending (#2005 follow-up). The
            # re-index below resolves the namespace itself when the caller
            # named none, and that resolution can fail — after the append is
            # already durable. This route has no idempotency key, so a caller
            # told to retry a half-completed write appends the entry twice.
            # Pre-flighting moves the failure to a point where refusing is
            # honest: nothing has been written, and a retry costs nothing.
            # Only for the resolving case — an explicit ``namespace`` short-
            # circuits the lookup, so there is nothing to pre-flight. The
            # mixed-namespace guard above already asks this question when the
            # file has content; this covers the new-file case it skips.
            #
            # The answer is *kept* and passed to ``index_file`` below, which
            # is what actually closes the window: asking and then letting the
            # re-index ask again leaves a second lookup on the far side of the
            # append, and that one has nowhere honest to fail. An explicit
            # ``namespace`` short-circuits the resolver, so it needs neither.
            write_ns = req.namespace
            if write_ns is None:
                from memtomem.models import stored_namespace

                try:
                    # The resolver returns ``None`` for the untagged carve-out,
                    # and passing that back through would read as "no caller
                    # namespace" and re-enter rule resolution — the very thing
                    # being avoided. ``stored_namespace`` names what the
                    # carve-out stores, which is not
                    # ``namespace.default_namespace`` when that setting is ""
                    # (#2738).
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
            # The re-index after the append judges the whole file: refuse a
            # file whose content may not be indexed before anything is written,
            # and let the valve cover this entry only in a file with nothing
            # flagged or masked in it.
            from memtomem.indexing.engine import PrivacyRejection
            from memtomem.tools.memory_mutation import appended_valve_text
            from memtomem.tools.memory_writer import read_pre_image_or_empty
            from memtomem.web.routes import system as _sys

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
                _sys.append_entry, target, req.content, title=req.title, tags=tags
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
                # The entry is on disk; its result may not be indexed (a title
                # the ingress never scanned). A 409 of its own, so the SPA does
                # not resend the append with force_unsafe.
                search_pipeline.invalidate_cache()
                exc.mutation_outcome = "appended"
                raise privacy_index_blocked_http(exc, "add") from exc
            # #2141, before the tag merge below: that merge can raise (or the
            # lock can time out) *after* the chunk write committed, and the
            # 500 the caller then sees must not leave a warmed query answering
            # as if the entry had never been added. Invalidating twice is
            # harmless; skipping it once is not.
            if stats.mutated:
                search_pipeline.invalidate_cache()

            # Apply tags only to newly appended chunks (so existing entries in
            # the same file are not polluted with the new entry's tags).
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
                    # Written outside the engine, so it carries no ``mutated``
                    # flag of its own — and it rewrites exactly the tag column
                    # search filters on (#2141).
                    search_pipeline.invalidate_cache()
    except TimeoutError as exc:
        raise HTTPException(
            status_code=503, detail="Memory file is locked by another writer; try again."
        ) from exc

    return AddMemoryResponse(file=str(target), indexed_chunks=stats.indexed_chunks)
