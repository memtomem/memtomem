"""FastAPI web application for memtomem Web UI."""

from __future__ import annotations

import logging
import os
import secrets
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from types import ModuleType
from typing import Literal, TypeGuard, get_args

from fastapi import FastAPI, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware

from memtomem import __version__
from memtomem.web.middleware.csrf import CSRFGuardMiddleware
from memtomem.web.routes import (
    chunks,
    context_agents,
    context_commands,
    context_gateway,
    context_mcp_servers,
    context_mutations,
    context_projects,
    context_skills,
    context_sync_all,
    context_transfer,
    context_versions,
    decay,
    dedup,
    evaluation,
    export,
    fs,
    namespaces,
    namespaces_read,
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
    wiki,
    wiki_mutations,
)

logger = logging.getLogger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

WebMode = Literal["prod", "dev"]
# Derive the runtime validator from the Literal so adding a future value
# (e.g. "preview") in one place updates both type-checking and runtime
# membership tests — see `feedback_literal_drives_frozenset.md`.
_VALID_WEB_MODES: frozenset[str] = frozenset(get_args(WebMode))


def _is_valid_web_mode(value: str) -> TypeGuard[WebMode]:
    """Narrow ``str`` to ``WebMode`` when ``value`` matches a known mode.

    Lets ``resolve_web_mode_from_env`` return without ``# type: ignore`` —
    membership in the runtime ``_VALID_WEB_MODES`` set is now also a
    type-level narrowing.
    """
    return value in _VALID_WEB_MODES


_WEB_MODE_ENV = "MEMTOMEM_WEB__MODE"

# CSRF enforcement default flipped to True in RFC #787 stage 2. The env var
# is an emergency-rollback hatch — set to ``0``/``false``/``no``/``off`` to
# fall back to observe-only without a code change. Anything else (including
# unset) keeps the default-on behavior.
_CSRF_ENFORCE_ENV = "MEMTOMEM_WEB__CSRF_ENFORCE"
_CSRF_ENFORCE_DISABLED: frozenset[str] = frozenset({"0", "false", "no", "off"})

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
    fs,
    timeline,
    namespaces_read,
    context_gateway,
    context_projects,
    context_skills,
    context_commands,
    context_agents,
    context_mcp_servers,
    context_sync_all,
    context_transfer,
    context_versions,
    settings_sync,
    wiki,
]
_DEV_ONLY_ROUTERS: list[ModuleType] = [
    namespaces,
    sessions,
    scratch,
    procedures,
    evaluation,
    watchdog,
    context_mutations,
    wiki_mutations,
]


def resolve_csrf_enforce_from_env() -> bool:
    """Return whether the CSRF/Origin/Host guard runs in enforce or observe.

    Default-on: missing or unrecognized values keep enforcement enabled. Only
    the explicit disable tokens in ``_CSRF_ENFORCE_DISABLED`` turn it off, so
    a typo (``MEMTOMEM_WEB__CSRF_ENFORCE=ture``) fails safe rather than
    silently dropping the gate.
    """
    raw = os.environ.get(_CSRF_ENFORCE_ENV, "").strip().lower()
    return raw not in _CSRF_ENFORCE_DISABLED


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
    if _is_valid_web_mode(raw):
        return raw
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

    # docs_url=None disables FastAPI's default ``/api/docs`` page so we can
    # serve a Swagger UI built against the locally vendored
    # ``swagger-ui-dist`` (web/static/vendor/swagger/) instead of the
    # jsdelivr CDN. The redoc default is dropped entirely — it duplicates
    # Swagger's purpose, also pulled from jsdelivr, and nothing in the SPA
    # links to it. Re-introduce it the same way as ``/api/docs`` if a
    # consumer asks for it.
    app = FastAPI(
        title="memtomem Web UI",
        description="Web UI for memtomem memory infrastructure",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
    )
    app.state.web_mode = mode

    # Per-process CSRF token (RFC #787). Generated fresh on every
    # ``create_app`` so token rotation is just a restart; never persisted.
    # ``GET /api/session`` exposes this to the SPA, and ``CSRFGuardMiddleware``
    # checks ``X-Memtomem-CSRF`` against it on unsafe ``/api/*`` requests.
    # Operator allow-lists default to empty — populated by the ``mm web`` CLI
    # when ``--trusted-host`` / ``--trusted-origin`` are passed alongside
    # ``--allow-remote-ui``.
    app.state.csrf_token = secrets.token_urlsafe(32)
    app.state.csrf_trusted_hosts = frozenset()
    app.state.csrf_trusted_origins = frozenset()
    # Stage-2 default: enforce. Setting ``MEMTOMEM_WEB__CSRF_ENFORCE`` to one
    # of ``0`` / ``false`` / ``no`` / ``off`` falls back to observe-only for
    # emergency rollback without a code change.
    app.state.csrf_enforce = resolve_csrf_enforce_from_env()

    # Hand-rolled instead of ``fastapi.openapi.docs.get_swagger_ui_html``
    # for two reasons that combine on the same page:
    # * The default helper bakes a Swagger UI bootstrap into an inline
    #   ``<script>`` block, which the locked-down CSP (``script-src
    #   'self'``) blocks. Loading the bootstrap as an external file lets
    #   the policy stay strict instead of growing back to ``'unsafe-inline'``.
    # * The default helper also points the favicon at
    #   ``https://fastapi.tiangolo.com/img/favicon.png`` — an external
    #   image fetch that ``img-src 'self' data:`` would block in the
    #   browser. Reusing the SPA's own ``favicon.svg`` keeps the page
    #   first-party end-to-end.
    _SWAGGER_HTML = (
        "<!DOCTYPE html>\n"
        '<html lang="en">\n'
        "<head>\n"
        '  <meta charset="UTF-8" />\n'
        f"  <title>{app.title} — Swagger UI</title>\n"
        '  <link rel="icon" href="/favicon.svg" type="image/svg+xml" />\n'
        '  <link rel="stylesheet" href="/vendor/swagger/swagger-ui.css?v=1" />\n'
        "</head>\n"
        "<body>\n"
        '  <div id="swagger-ui"></div>\n'
        '  <script src="/vendor/swagger/swagger-ui-bundle.js?v=1"></script>\n'
        '  <script src="/vendor/swagger/swagger-init.js?v=1"></script>\n'
        "</body>\n"
        "</html>\n"
    )

    @app.get("/api/docs", include_in_schema=False)
    async def custom_swagger_ui_html() -> HTMLResponse:
        return HTMLResponse(_SWAGGER_HTML)

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

    # CSRF / Origin / Host guard (RFC #787).
    #
    # Order with SecurityHeaders matters: ``add_middleware`` stacks last-added
    # outermost on the request path. We want CSRFGuard *inside*
    # SecurityHeaders so a 403 from the gate still picks up nosniff /
    # frame-options / CSP on its way back to the client.
    app.add_middleware(CSRFGuardMiddleware)

    class SecurityHeadersMiddleware(BaseHTTPMiddleware):
        async def dispatch(self, request: Request, call_next) -> Response:
            response = await call_next(request)
            response.headers["X-Content-Type-Options"] = "nosniff"
            response.headers["X-Frame-Options"] = "DENY"
            response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
            response.headers["Content-Security-Policy"] = (
                "default-src 'self'; "
                "script-src 'self'; "
                "style-src 'self' 'unsafe-inline'; "
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
    from memtomem.indexing.watcher import FileWatcher
    from memtomem.server.component_factory import close_components, create_components

    comp = await create_components()

    from memtomem.search.dedup import DedupScanner
    from memtomem.context.scope_resolver import find_project_root

    # Walk up to the project root (.git / pyproject.toml) so launching
    # ``mm web`` from a subdirectory resolves the same canonical .memtomem tree
    # the CLI/MCP write to — not ``<subdir>/.memtomem``. Single shared helper.
    app.state.project_root = find_project_root()
    app.state.config = comp.config
    app.state.storage = comp.storage
    app.state.embedder = comp.embedder
    app.state.search_pipeline = comp.search_pipeline
    app.state.index_engine = comp.index_engine
    app.state.dedup_scanner = DedupScanner(comp.storage, comp.embedder)
    # Per-source AI summary regeneration job state (singleton, in-memory).
    # ``None`` when no job has run; otherwise a counter dict mutated by the
    # background task and read by ``GET /api/sources/regenerate-status``.
    app.state.summary_regen = None
    # Shared LLM provider (or ``None``) — exposed for the bulk-regenerate
    # endpoint, which mirrors the indexing engine's per-source flow.
    app.state.llm = comp.llm

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

    # P2 cron Phase A footgun: ``mm web`` does not run the schedule
    # dispatcher (HealthWatchdog is wired only in the MCP server lifespan,
    # see server/context.py). Mirror the warning emitted there so users who
    # register schedules against a web-only entry get a loud signal at
    # startup instead of silently null ``last_run_status``.
    if comp.config.scheduler.enabled:
        logger.warning(
            "scheduler.enabled=true but ``mm web`` does not dispatch schedules — "
            "run ``memtomem-server`` (MCP) for the watchdog tick that fires registered jobs"
        )
    if comp.config.policy.enabled:
        logger.warning(
            "policy.enabled=true but ``mm web`` does not run the policy scheduler — "
            "run ``memtomem-server`` (MCP) for the lifespan that starts PolicyScheduler"
        )

    # File watcher: monitors memory_dirs for fs-event-driven re-indexing
    # and runs a one-shot startup backfill so files added while ``mm web``
    # was down (or before the dir was registered) get indexed without the
    # user clicking Reindex. Skipped in degraded mode (broken embedding) —
    # the indexer would crash on the missing chunks_vec table; recovery
    # via ``mem_embedding_reset``. Mirrors the wiring in
    # ``server/context.py``; without this ``mm web`` ran with no fs
    # watcher at all.
    watcher: FileWatcher | None = None
    if comp.embedding_broken is None:
        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        await watcher.start()
        app.state.file_watcher = watcher

    try:
        yield
    finally:
        if watcher is not None:
            try:
                await watcher.stop()
            except Exception as exc:
                logger.warning("file watcher stop failed: %s", exc)
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
