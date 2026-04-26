"""FastAPI web application for memtomem Web UI."""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Literal, get_args

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

WebMode = Literal["prod", "dev"]
# Derive the runtime validator from the Literal so adding a future value
# (e.g. "preview") in one place updates both type-checking and runtime
# membership tests — see `feedback_literal_drives_frozenset.md`.
_VALID_WEB_MODES: frozenset[str] = frozenset(get_args(WebMode))
_WEB_MODE_ENV = "MEMTOMEM_WEB__MODE"

# Routers that define the polished surface shipped to `uv tool install` users.
# `_DEV_ONLY_ROUTERS` is the opt-in extension mounted only when
# ``mode == "dev"`` — those pages have rougher UX, narrower audiences, or
# are still in flux, so they stay hidden by default until they graduate.
# Edit carefully: these lists are the source of truth; the SPA's
# ``data-ui-tier`` attributes in ``index.html`` must match.
_PROD_ROUTERS: list[ModuleType] = [
    search,
    chunks,
    sources,
    system,
    tags,
    dedup,
    decay,
    export,
    timeline,
    context_gateway,
    context_skills,
    context_commands,
    context_agents,
]
_DEV_ONLY_ROUTERS: list[ModuleType] = [
    namespaces,
    sessions,
    scratch,
    procedures,
    evaluation,
    watchdog,
    settings_sync,
]


def resolve_web_mode_from_env(*, strict: bool = False) -> WebMode:
    """Return the web mode from ``MEMTOMEM_WEB__MODE``.

    With ``strict=True`` an invalid value raises ``ValueError`` (used by the
    ``mm web`` CLI, which also enforces mutual exclusion with ``--mode`` /
    ``--dev``). With ``strict=False`` an invalid value falls back to ``prod``
    with a warning — this path is taken when ``uvicorn`` mounts the
    module-level app without going through the CLI (e.g. tests, ASGI hosts).
    """
    raw = os.environ.get(_WEB_MODE_ENV, "").strip().lower()
    if not raw:
        return "prod"
    if raw in _VALID_WEB_MODES:
        return raw  # type: ignore[return-value]
    if strict:
        raise ValueError(
            f"Invalid {_WEB_MODE_ENV}={raw!r}; expected one of {sorted(_VALID_WEB_MODES)}"
        )
    logger.warning(
        "Ignoring invalid %s=%r; falling back to 'prod'. Valid values: %s",
        _WEB_MODE_ENV,
        raw,
        sorted(_VALID_WEB_MODES),
    )
    return "prod"


def create_app(lifespan=None, mode: WebMode = "prod") -> FastAPI:
    """Factory for creating the FastAPI app (testable without lifespan).

    ``mode`` controls which routers are mounted:

    * ``prod`` (default) — the polished surface only.
    * ``dev`` — adds the routers in ``_DEV_ONLY_ROUTERS`` for maintainers.

    The SPA reads ``GET /api/system/ui-mode`` on boot and filters tabs /
    sections accordingly.
    """
    if mode not in _VALID_WEB_MODES:
        raise ValueError(f"Invalid web mode {mode!r}; expected one of {sorted(_VALID_WEB_MODES)}")

    app = FastAPI(
        title="memtomem Web UI",
        description="Web UI for memtomem memory infrastructure",
        version=__version__,
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
    )
    app.state.web_mode = mode

    for router_mod in _PROD_ROUTERS:
        app.include_router(router_mod.router, prefix="/api")
    if mode == "dev":
        for router_mod in _DEV_ONLY_ROUTERS:
            app.include_router(router_mod.router, prefix="/api")

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

    # Sync config to match DB-stored embedding info (prevents mismatch banner).
    # Skipped when the server entered degraded mode (issue #349) — in the
    # dim=0 / real-provider case the stored "embedding" is NoopEmbedder
    # (provider=none, dim=0), so an auto-sync would silently downgrade the
    # user's configured onnx/bge-m3 to BM25-only and swallow the broken
    # state instead of surfacing it. The banner + ``/api/embedding-reset``
    # flow recovers explicitly; soft-syncing would defeat it.
    stored_info = getattr(comp.storage, "stored_embedding_info", None)
    if stored_info and comp.embedding_broken is None:
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

    try:
        yield
    finally:
        await close_components(comp)


_app_singleton: FastAPI | None = None


def __getattr__(name: str):
    """Lazy module-level ``app`` construction, memoized.

    Only build the default ASGI app when something actually asks for it
    (``uvicorn memtomem.web.app:app``). Avoids a second ``create_app`` call —
    and its ``MEMTOMEM_WEB__MODE`` resolution warning — when the CLI imports
    ``resolve_web_mode_from_env`` or ``create_app`` directly.

    The cached ``_app_singleton`` is critical: ``__getattr__`` runs on every
    attribute access that isn't already in the module ``__dict__``, so
    without memoization two ``from memtomem.web.app import app`` call sites
    would each get a distinct ``FastAPI`` instance with its own routers,
    state, and lifespan handlers.
    """
    global _app_singleton
    if name == "app":
        if _app_singleton is None:
            _app_singleton = create_app(lifespan=_lifespan, mode=resolve_web_mode_from_env())
        return _app_singleton
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def main() -> None:
    """Run the web UI server."""
    import argparse

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
