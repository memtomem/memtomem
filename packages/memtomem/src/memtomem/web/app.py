"""FastAPI web application for memtomem Web UI."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from memtomem import __version__
from memtomem.web.routes import (
    chunks,
    context_agents,
    context_commands,
    context_gateway,
    context_skills,
    decay,
    dedup,
    evaluation,
    export,
    namespaces,
    procedures,
    scratch,
    search,
    sessions,
    settings_sync,
    sources,
    system,
    tags,
    timeline,
    watchdog,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"


def create_app(lifespan=None) -> FastAPI:
    """Factory for creating the FastAPI app (testable without lifespan)."""
    app = FastAPI(
        title="memtomem Web UI",
        description="Web UI for memtomem memory infrastructure",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )

    app.include_router(search.router, prefix="/api")
    app.include_router(chunks.router, prefix="/api")
    app.include_router(sources.router, prefix="/api")
    app.include_router(system.router, prefix="/api")
    app.include_router(tags.router, prefix="/api")
    app.include_router(dedup.router, prefix="/api")
    app.include_router(decay.router, prefix="/api")
    app.include_router(export.router, prefix="/api")
    app.include_router(namespaces.router, prefix="/api")
    app.include_router(timeline.router, prefix="/api")
    app.include_router(sessions.router, prefix="/api")
    app.include_router(scratch.router, prefix="/api")
    app.include_router(procedures.router, prefix="/api")
    app.include_router(evaluation.router, prefix="/api")
    app.include_router(watchdog.router, prefix="/api")
    app.include_router(settings_sync.router, prefix="/api")
    app.include_router(context_gateway.router, prefix="/api")
    app.include_router(context_skills.router, prefix="/api")
    app.include_router(context_commands.router, prefix="/api")
    app.include_router(context_agents.router, prefix="/api")

    @app.exception_handler(ValueError)
    async def value_error_handler(request: Request, exc: ValueError) -> JSONResponse:
        import re

        msg = re.sub(r"(?:[A-Za-z]:)?(?:[/\\][\w.\-]+){2,}", "<path>", str(exc))
        return JSONResponse(status_code=400, content={"detail": msg})

    @app.exception_handler(KeyError)
    async def key_error_handler(request: Request, exc: KeyError) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "Not found"})

    @app.exception_handler(Exception)
    async def generic_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.error("Unhandled exception: %s", exc, exc_info=True)
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    app.add_middleware(
        CORSMiddleware,
        allow_origin_regex=r"https?://(localhost|127\.0\.0\.1)(:\d+)?",
        allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
    )

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next) -> Response:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self' https://cdnjs.cloudflare.com; "
                "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; "
                "img-src 'self' data:; "
                "connect-src 'self'; "
                "frame-ancestors 'none'"
            )
            return response

    app.add_middleware(SecurityHeadersMiddleware)

    _favicon = _STATIC_DIR / "favicon.svg"

    @app.get("/favicon.ico", include_in_schema=False)
    @app.get("/apple-touch-icon.png", include_in_schema=False)
    @app.get("/apple-touch-icon-precomposed.png", include_in_schema=False)
    async def _favicon_fallback() -> FileResponse:
        return FileResponse(_favicon, media_type="image/svg+xml")

    @app.api_route(
        "/api/{path:path}",
        methods=["GET", "POST", "PATCH", "DELETE"],
        include_in_schema=False,
    )
    async def api_not_found() -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": "API endpoint not found"})

    if _STATIC_DIR.exists():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    from memtomem.server.component_factory import close_components, create_components

    comp = await create_components()

    from memtomem.search.dedup import DedupScanner

    app.state.project_root = Path.cwd()
    app.state.config = comp.config
    app.state.storage = comp.storage
    app.state.embedder = comp.embedder
    app.state.search_pipeline = comp.search_pipeline
    app.state.index_engine = comp.index_engine
    app.state.dedup_scanner = DedupScanner(comp.storage, comp.embedder)

    # Sync config to match DB-stored embedding info (prevents mismatch banner)
    stored_info = getattr(comp.storage, "stored_embedding_info", None)
    if stored_info:
        cfg = comp.config.embedding
        if cfg.model != stored_info["model"] or cfg.dimension != stored_info["dimension"]:
            logger.info(
                "Syncing config to DB embedding: %s/%s (%dd)",
                stored_info["provider"],
                stored_info["model"],
                stored_info["dimension"],
            )
            cfg.model = stored_info["model"]
            cfg.dimension = stored_info["dimension"]
            if stored_info.get("provider"):
                cfg.provider = stored_info["provider"]
            # Clear mismatch flags since config now matches DB
            comp.storage.clear_embedding_mismatch()

    # Ensure memory_dirs exist
    for d in comp.config.indexing.memory_dirs:
        Path(d).expanduser().resolve().mkdir(parents=True, exist_ok=True)

    # One-shot background scan of memory_dirs that have no chunks yet. Closes
    # the gap where a dir was added to config (``mm init`` wizard step, or the
    # one-shot ``auto_discover`` migration) but its existing files never made
    # it into the index — the watcher only reacts to change events.
    from memtomem.server.lifespan import _initial_scan_missing_dirs

    initial_scan_task: asyncio.Task[None] | None = asyncio.create_task(
        _initial_scan_missing_dirs(comp.index_engine, comp.storage, comp.config.indexing),
        name="memtomem-web-initial-scan",
    )

    try:
        yield
    finally:
        if initial_scan_task is not None and not initial_scan_task.done():
            initial_scan_task.cancel()
            try:
                await initial_scan_task
            except (asyncio.CancelledError, Exception):
                pass
        await close_components(comp)


app = create_app(lifespan=_lifespan)


def main() -> None:
    """Run the web UI server."""
    import argparse
    import os

    import uvicorn

    parser = argparse.ArgumentParser(description="memtomem Web UI")
    parser.add_argument("--host", default=None, help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=None, help="Bind port (default: 8080)")
    args = parser.parse_args()

    host = args.host or os.environ.get("MEMTOMEM_WEB__HOST", "127.0.0.1")
    port = args.port or int(os.environ.get("MEMTOMEM_WEB__PORT", "8080"))
    uvicorn.run("memtomem.web.app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
