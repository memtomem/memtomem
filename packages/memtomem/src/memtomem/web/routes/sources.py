"""Source file management endpoints."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from memtomem.config import MemoryDirKind, TargetScope, classify_scope, memory_dir_kind
from memtomem.indexing.engine import norm_dir_prefix
from memtomem.indexing.summarizer import regenerate_for_paths
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.web.deps import get_config, get_storage, require_indexed_source
from memtomem.web.schemas.core import DeleteResponse
from memtomem.web.schemas.sources import (
    ChunkSizeBucket,
    LanguageDriftInfo,
    RegenerateStartResponse,
    RegenerateStatusResponse,
    SourceContentMatchesResponse,
    SourceOut,
    SourcesResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/sources", tags=["sources"])


# Heuristic preview cap. Mirrored in the docstring for ``SourceOut.excerpt``
# — keep both in sync if you tune this value.
_EXCERPT_MAX_CHARS = 200


def _derive_summary(
    heading_hierarchy: list[str], first_chunk_content: str
) -> tuple[str | None, str | None]:
    """Turn a stored ``(hierarchy, content)`` pair into ``(title, excerpt)``.

    Title: the most-general heading entry (``"# Foo"`` → ``"Foo"``); falls
    back to ``None`` for files without headings (raw notes, generated
    manifests). Excerpt: the first non-blank lines of the first chunk's
    body, whitespace-collapsed and trimmed to ``_EXCERPT_MAX_CHARS`` with a
    trailing ellipsis when truncated. The chunker already strips heading
    lines from ``content``, so we don't re-strip.
    """
    title: str | None = None
    if heading_hierarchy:
        raw = heading_hierarchy[0].lstrip("#").strip()
        title = raw or None

    excerpt: str | None = None
    body = (first_chunk_content or "").strip()
    if body:
        flat = " ".join(body.split())
        if len(flat) > _EXCERPT_MAX_CHARS:
            excerpt = flat[: _EXCERPT_MAX_CHARS - 1].rstrip() + "…"
        else:
            excerpt = flat

    return title, excerpt


@router.get("", response_model=SourcesResponse)
async def list_sources(
    limit: int = Query(100, ge=1, le=10000),
    offset: int = Query(0, ge=0),
    kind: MemoryDirKind | None = Query(
        None,
        description=(
            "Filter to one bucket of the Sources page sub-toggle. "
            "``memory`` keeps only sources under a configured memory_dir "
            "whose kind is ``memory``; ``general`` keeps the rest, "
            "including orphan sources whose owning dir is no longer "
            "registered (so they don't disappear from the UI)."
        ),
    ),
    target_scope: TargetScope | None = Query(
        None,
        description=(
            "ADR-0016 §7 canonical-residency tier filter. Default (omit) "
            "shows ``user`` + ``project_shared`` only; ``project_local`` "
            "is hidden until explicitly requested (ADR-0015 §4a — keeps "
            "the draft tier out of overview unless the operator opts in). "
            "Passing one of the three literal tokens narrows the list "
            "to that tier; passing ``project_local`` is the only way to "
            "surface those sources."
        ),
    ),
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> SourcesResponse:
    rows = await storage.get_source_files_with_counts()
    # Heuristic preview (first heading + first chunk body), populated for
    # every source so the UI has a readable fallback when no LLM summary
    # is cached yet.
    summaries = await storage.get_source_summaries()
    # Cached LLM summaries (path → record dict). Empty dict when the
    # ``auto_summarize`` flag has never produced a row.
    ai_summaries = await storage.get_all_ai_summaries()

    # Pre-compute (prefix, dir_path, kind) once per request so the
    # per-source classification stays O(D × startswith) instead of
    # O(D × Path.resolve()). For ~30 dirs × ~300 sources that drops
    # ~9000 syscalls / NFC normalisations off the hot path of
    # ``GET /api/sources``. Sorted by prefix length descending so the
    # first match in the inner loop is the longest-prefix-wins one —
    # matches :func:`resolve_owning_memory_dir`'s tie-break rule for
    # nested configured dirs without repeating the comparison logic.
    #
    # ``dir_path`` is resolved (not just expanded) so the response
    # ``memory_dir`` matches sibling ``/api/memory-dirs/status`` paths
    # — both produce ``Path(d).expanduser().resolve()`` strings. Without
    # this, frontend ``STATE.memoryStatusByPath[source.memory_dir]``
    # lookups miss whenever a memory_dir is registered under a symlinked
    # prefix (macOS ``/tmp`` → ``/private/tmp``, Docker bind mounts).
    # Same one-line treatment as #668 / engine.py:memory_dir_stats. (#675)
    indexed_dirs: list[tuple[str, Path, MemoryDirKind]] = sorted(
        (
            (norm_dir_prefix(d), Path(d).expanduser().resolve(), memory_dir_kind(d))
            for d in config.indexing.memory_dirs
        ),
        key=lambda t: -len(t[0]),
    )

    # ADR-0011 / ADR-0016: pre-resolve project_memory_dirs so the
    # per-source ``classify_scope`` lookup can refuse unregistered
    # project-tier paths (the public API of ``classify_scope`` falls
    # back to ``"user"`` for any path under ``.memtomem/...`` whose
    # owning project_memory_dir was not registered, mirroring the
    # indexer's safety net at config.py:1495-1507).
    pmdirs = config.indexing.project_memory_dirs

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

        target = norm_path(p)
        match = next(
            (
                (dir_path, dir_kind)
                for prefix, dir_path, dir_kind in indexed_dirs
                if target.startswith(prefix)
            ),
            None,
        )
        source_kind: MemoryDirKind | None
        memory_dir_str: str | None
        if match is None:
            # Orphan: indexed source whose configured dir was removed
            # after indexing. Show in General view rather than hiding so
            # users can still find and prune the chunks; ``kind=None``
            # signals that the categorisation is unknowable, not that
            # the source is "general" by intent.
            source_kind = None
            memory_dir_str = None
        else:
            owning_dir, source_kind = match
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

        # ADR-0016 §7: path-classify the source's canonical-residency
        # tier. ``classify_scope`` returns ``"user"`` for any path that
        # is not under a registered project_memory_dir (including
        # otherwise project-shaped paths whose owning dir was not
        # registered) — matches the indexer's persisted scope so the
        # badge agrees with the chunk's stored ``meta.scope``.
        source_scope, _src_project_root = classify_scope(p, pmdirs)

        # ADR-0015 §4a project_local default-hidden rule. When the
        # caller passes ``?target_scope=`` we narrow to exactly that
        # tier (the only way to surface ``project_local`` sources).
        # When omitted, ``project_local`` rows fall out — keeps the
        # draft tier out of overview / list views unless the operator
        # explicitly asks for it.
        if target_scope is None:
            if source_scope == "project_local":
                continue
        elif source_scope != target_scope:
            continue

        path_str = str(p)
        hh, first_content = summaries.get(path_str, ([], ""))
        title, excerpt = _derive_summary(hh, first_content)

        ai_record = ai_summaries.get(path_str) or {}
        ai_summary_text = ai_record.get("summary") or None
        ai_summary_lang = ai_record.get("language") or None

        all_sources.append(
            SourceOut(
                path=path_str,
                chunk_count=cnt,
                last_indexed_at=last_indexed_at,
                file_size=file_size,
                namespaces=namespaces,
                avg_tokens=avg_tok,
                min_tokens=min_tok,
                max_tokens=max_tok,
                memory_dir=memory_dir_str,
                kind=source_kind,
                target_scope=source_scope,
                title=title,
                excerpt=excerpt,
                ai_summary=ai_summary_text,
                ai_summary_language=ai_summary_lang,
            )
        )
    total = len(all_sources)
    page = all_sources[offset : offset + limit]

    # Language-drift summary — count cached AI summaries whose language
    # tag differs from the current ``summary_language`` setting. Only
    # populated when count > 0 so the UI's "no banner" branch is the
    # trivial ``language_drift is None`` check.
    target_lang = config.indexing.summary_language
    drift_count = sum(1 for rec in ai_summaries.values() if rec.get("language") != target_lang)
    drift_info: LanguageDriftInfo | None = None
    if drift_count > 0:
        drift_info = LanguageDriftInfo(count=drift_count, current_setting=target_lang)

    return SourcesResponse(
        sources=page,
        total=total,
        offset=offset,
        limit=limit,
        language_drift=drift_info,
    )


@router.get("/content-matches", response_model=SourceContentMatchesResponse)
async def source_content_matches(
    q: str = Query(..., min_length=1, max_length=500, description="Plain text to match in indexed chunk content"),
    limit: int = Query(10000, ge=1, le=10000),
    storage=Depends(get_storage),
) -> SourceContentMatchesResponse:
    """Return source paths whose indexed text contains ``q``.

    This powers the Sources tab's lightweight body-aware filter. It uses
    indexed chunk text and cached source summaries, not raw file reads, so
    deleted/unavailable files still match as long as their chunks remain in
    storage.
    """
    query = q.strip()
    if not query:
        raise HTTPException(status_code=400, detail="Query must not be blank.")
    paths = list(await storage.search_source_files_by_content(query, limit=limit))
    seen = {str(p) for p in paths}
    needle = query.casefold()
    for path, record in (await storage.get_all_ai_summaries()).items():
        if len(paths) >= limit:
            break
        summary = (record or {}).get("summary") or ""
        if path not in seen and needle in summary.casefold():
            paths.append(Path(path))
            seen.add(path)
    return SourceContentMatchesResponse(query=query, paths=[str(p) for p in paths])


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


# ---------------------------------------------------------------------------
# AI summary bulk regenerate (language drift opt-in)
# ---------------------------------------------------------------------------


async def _run_summary_regen(
    request_app_state,
    storage,
    llm,
    paths: list[Path],
    indexing_config,
) -> None:
    """Background task body — iterates ``paths`` and rewrites cached
    summaries in ``indexing_config.summary_language``.

    Mutates ``request_app_state.summary_regen`` in place so the status
    endpoint can report progress. Always clears ``running`` on exit, even
    if :func:`regenerate_for_paths` raises.
    """
    state = request_app_state.summary_regen
    assert state is not None  # caller initialised it before scheduling

    async def _chunks_for(p: Path):
        # Unbounded ``limit_per_file`` would risk loading enormous files
        # into memory; the summarizer only consumes the first 5 chunks
        # anyway, so cap conservatively.
        return await storage.list_chunks_by_source(p, limit=64)

    def _progress(processed: int, total: int, failed: int) -> None:
        state["done"] = processed
        state["failed"] = failed

    try:
        result = await regenerate_for_paths(
            storage,
            llm,
            paths,
            _chunks_for,
            indexing_config,
            progress=_progress,
        )
        state["done"] = result["processed"]
        state["failed"] = result["failed"]
        state["skipped"] = result["skipped"]
    except Exception as exc:
        # Defensive — fail-soft path lives inside ``regenerate_for_paths``,
        # but a buggy storage method or cancelled task could still bubble
        # up. Surface via logs so operators see why progress stalled.
        logger.exception("Summary regenerate task aborted: %s", exc)
    finally:
        state["running"] = False


@router.post("/regenerate-summaries", response_model=RegenerateStartResponse)
async def regenerate_summaries(
    request: Request,
    storage=Depends(get_storage),
    config=Depends(get_config),
) -> RegenerateStartResponse:
    """Kick off a background regeneration of every cached AI summary
    whose language doesn't match ``IndexingConfig.summary_language``.

    Idempotent — calling while a job is running returns ``started=False``
    with the running job's ``total``. The frontend's [Regenerate all]
    button polls :func:`get_summary_regen_status` for progress.
    """
    if not config.indexing.auto_summarize:
        raise HTTPException(
            status_code=400,
            detail=(
                "auto_summarize is disabled. Enable indexing.auto_summarize "
                "in config before regenerating summaries."
            ),
        )

    llm = getattr(request.app.state, "llm", None)
    if llm is None:
        raise HTTPException(
            status_code=400,
            detail="LLM is not configured. Enable llm.enabled in config.",
        )

    state = getattr(request.app.state, "summary_regen", None)
    if state and state.get("running"):
        return RegenerateStartResponse(started=False, total=state.get("total", 0))

    target_lang = config.indexing.summary_language
    drift_paths = await storage.list_language_drift_paths(target_lang)

    new_state = {
        "running": True,
        "total": len(drift_paths),
        "done": 0,
        "failed": 0,
        "skipped": 0,
    }
    request.app.state.summary_regen = new_state

    if not drift_paths:
        # Nothing to do — mark complete immediately so the status
        # endpoint converges without a polling round trip.
        new_state["running"] = False
        return RegenerateStartResponse(started=True, total=0)

    import asyncio

    asyncio.create_task(
        _run_summary_regen(request.app.state, storage, llm, drift_paths, config.indexing)
    )
    return RegenerateStartResponse(started=True, total=len(drift_paths))


@router.get("/regenerate-status", response_model=RegenerateStatusResponse)
async def get_summary_regen_status(request: Request) -> RegenerateStatusResponse:
    """Return the current bulk-regenerate job's progress, or zeros if no
    job has run since startup."""
    state = getattr(request.app.state, "summary_regen", None)
    if not state:
        return RegenerateStatusResponse(running=False, total=0, done=0, failed=0, skipped=0)
    return RegenerateStatusResponse(
        running=bool(state.get("running")),
        total=int(state.get("total", 0)),
        done=int(state.get("done", 0)),
        failed=int(state.get("failed", 0)),
        skipped=int(state.get("skipped", 0)),
    )
