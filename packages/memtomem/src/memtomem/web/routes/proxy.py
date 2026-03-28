"""STM Proxy monitoring API endpoints."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel

from memtomem.web.deps import get_config

router = APIRouter(prefix="/proxy", tags=["proxy"])


# ── Response models ──────────────────────────────────────────────────


class ProxyServerInfo(BaseModel):
    name: str
    prefix: str
    transport: str
    compression: str
    max_result_chars: int
    auto_index: bool | None = None
    tool_overrides_count: int = 0


class ProxyStatusResponse(BaseModel):
    enabled: bool
    installed: bool
    servers: list[ProxyServerInfo] = []
    surfacing_enabled: bool = False
    auto_index_enabled: bool = False
    cache_enabled: bool = False
    langfuse_enabled: bool = False


class ProxyMetricsResponse(BaseModel):
    total_calls: int = 0
    total_original_chars: int = 0
    total_compressed_chars: int = 0
    total_savings_pct: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    by_server: dict = {}
    by_tool: dict = {}


class ProxyCacheResponse(BaseModel):
    total_entries: int = 0
    expired_entries: int = 0
    by_server: dict = {}  # server_name → entry_count


class ProxyCacheClearRequest(BaseModel):
    server: str | None = None
    tool: str | None = None


class ProxyCacheClearResponse(BaseModel):
    cleared: int = 0


class SurfacingEventInfo(BaseModel):
    surfacing_id: str
    tool: str
    query: str
    score_count: int = 0
    timestamp: float = 0.0


class ProxySurfacingResponse(BaseModel):
    total_surfacings: int = 0
    total_feedback: int = 0
    by_rating: dict = {}
    helpfulness_pct: float = 0.0
    recent_events: list[SurfacingEventInfo] = []
    pending_feedback: int = 0  # surfacings without feedback


class ProxyHistoryEntry(BaseModel):
    server: str
    tool: str
    original_chars: int
    compressed_chars: int
    cleaned_chars: int = 0
    savings_pct: float = 0.0
    timestamp: float = 0.0


class ProxyHistoryResponse(BaseModel):
    entries: list[ProxyHistoryEntry] = []
    total: int = 0


# ── Helpers ──────────────────────────────────────────────────────────


def _stm_available() -> bool:
    """Check if memtomem-stm is installed."""
    try:
        import memtomem_stm  # noqa: F401
        return True
    except ImportError:
        return False


_proxy_config_cache: dict | None = None
_proxy_config_mtime: float = 0.0


def _load_proxy_config() -> dict | None:
    """Load STM proxy config from ~/.memtomem/stm_proxy.json (mtime-cached)."""
    global _proxy_config_cache, _proxy_config_mtime
    import json
    path = Path("~/.memtomem/stm_proxy.json").expanduser()
    if not path.exists():
        _proxy_config_cache = None
        return None
    try:
        mtime = path.stat().st_mtime
        if _proxy_config_cache is not None and mtime == _proxy_config_mtime:
            return _proxy_config_cache
        _proxy_config_cache = json.loads(path.read_text(encoding="utf-8"))
        _proxy_config_mtime = mtime
        return _proxy_config_cache
    except (json.JSONDecodeError, OSError):
        return None


def _get_metrics_db() -> sqlite3.Connection | None:
    """Open read-only connection to proxy metrics DB."""
    path = Path("~/.memtomem/proxy_metrics.db").expanduser()
    if not path.exists():
        return None
    try:
        db = sqlite3.connect(str(path), check_same_thread=False)
        db.execute("PRAGMA query_only=ON")
        return db
    except sqlite3.Error:
        return None


# ── Endpoints ────────────────────────────────────────────────────────


@router.get("/status", response_model=ProxyStatusResponse)
async def proxy_status(config=Depends(get_config)) -> ProxyStatusResponse:
    """Check STM proxy status and configuration."""
    if not _stm_available():
        return ProxyStatusResponse(enabled=False, installed=False)

    proxy_cfg = _load_proxy_config()
    if not proxy_cfg or not proxy_cfg.get("enabled"):
        return ProxyStatusResponse(enabled=False, installed=True)

    servers = []
    for name, srv in proxy_cfg.get("upstream_servers", {}).items():
        if name.startswith("_"):
            continue
        servers.append(ProxyServerInfo(
            name=name,
            prefix=srv.get("prefix", ""),
            transport=srv.get("transport", "stdio"),
            compression=srv.get("compression", "selective"),
            max_result_chars=srv.get("max_result_chars", 2000),
            auto_index=srv.get("auto_index"),
            tool_overrides_count=len(srv.get("tool_overrides", {})),
        ))

    langfuse_enabled = False
    try:
        from memtomem_stm.observability.tracing import _langfuse_client
        langfuse_enabled = _langfuse_client is not None
    except (ImportError, AttributeError):
        pass

    return ProxyStatusResponse(
        enabled=True,
        installed=True,
        servers=servers,
        surfacing_enabled=True,
        auto_index_enabled=proxy_cfg.get("auto_index", {}).get("enabled", False)
        if isinstance(proxy_cfg.get("auto_index"), dict) else False,
        cache_enabled=proxy_cfg.get("cache", {}).get("enabled", False)
        if isinstance(proxy_cfg.get("cache"), dict) else False,
        langfuse_enabled=langfuse_enabled,
    )


@router.get("/metrics", response_model=ProxyMetricsResponse)
async def proxy_metrics(
    since: float | None = Query(None, description="Unix timestamp to filter from"),
) -> ProxyMetricsResponse:
    """Get compression metrics summary."""
    db = _get_metrics_db()
    if db is None:
        return ProxyMetricsResponse()

    try:
        where = ""
        params: list = []
        if since:
            where = "WHERE created_at >= ?"
            params = [since]

        # Totals
        row = db.execute(
            f"SELECT COUNT(*), COALESCE(SUM(original_chars),0), COALESCE(SUM(compressed_chars),0) "
            f"FROM proxy_metrics {where}", params
        ).fetchone()
        total_calls, total_orig, total_comp = row

        savings = round((1 - total_comp / total_orig) * 100, 1) if total_orig > 0 else 0.0

        # By server
        by_server = {}
        for r in db.execute(
            f"SELECT server, COUNT(*), SUM(original_chars), SUM(compressed_chars) "
            f"FROM proxy_metrics {where} GROUP BY server", params
        ).fetchall():
            s_pct = round((1 - r[3] / r[2]) * 100, 1) if r[2] > 0 else 0.0
            by_server[r[0]] = {
                "calls": r[1], "original_chars": r[2],
                "compressed_chars": r[3], "savings_pct": s_pct,
            }

        # By tool
        by_tool = {}
        for r in db.execute(
            f"SELECT server || '/' || tool, COUNT(*), SUM(original_chars), SUM(compressed_chars) "
            f"FROM proxy_metrics {where} GROUP BY server, tool", params
        ).fetchall():
            t_pct = round((1 - r[3] / r[2]) * 100, 1) if r[2] > 0 else 0.0
            by_tool[r[0]] = {
                "calls": r[1], "original_chars": r[2],
                "compressed_chars": r[3], "savings_pct": t_pct,
            }

        return ProxyMetricsResponse(
            total_calls=total_calls,
            total_original_chars=total_orig,
            total_compressed_chars=total_comp,
            total_savings_pct=savings,
            by_server=by_server,
            by_tool=by_tool,
        )
    finally:
        db.close()


@router.get("/cache", response_model=ProxyCacheResponse)
async def proxy_cache() -> ProxyCacheResponse:
    """Get cache statistics."""
    cache_path = Path("~/.memtomem/proxy_cache.db").expanduser()
    if not cache_path.exists():
        return ProxyCacheResponse()

    try:
        db = sqlite3.connect(str(cache_path), check_same_thread=False)
        db.execute("PRAGMA query_only=ON")
        total = db.execute("SELECT COUNT(*) FROM proxy_cache").fetchone()
        expired = db.execute(
            "SELECT COUNT(*) FROM proxy_cache WHERE ttl_seconds IS NOT NULL AND created_at + ttl_seconds < ?",
            (time.time(),),
        ).fetchone()
        by_server: dict = {}
        for r in db.execute("SELECT server, COUNT(*) FROM proxy_cache GROUP BY server").fetchall():
            by_server[r[0]] = r[1]
        db.close()
        return ProxyCacheResponse(
            total_entries=total[0] if total else 0,
            expired_entries=expired[0] if expired else 0,
            by_server=by_server,
        )
    except sqlite3.Error:
        return ProxyCacheResponse()


@router.post("/cache/clear", response_model=ProxyCacheClearResponse)
async def proxy_cache_clear(body: ProxyCacheClearRequest) -> ProxyCacheClearResponse:
    """Clear cache entries."""
    cache_path = Path("~/.memtomem/proxy_cache.db").expanduser()
    if not cache_path.exists():
        return ProxyCacheClearResponse()

    try:
        db = sqlite3.connect(str(cache_path), check_same_thread=False)
        if body.server and body.tool:
            count = db.execute("DELETE FROM proxy_cache WHERE server=? AND tool=?",
                               (body.server, body.tool)).rowcount
        elif body.server:
            count = db.execute("DELETE FROM proxy_cache WHERE server=?",
                               (body.server,)).rowcount
        else:
            count = db.execute("DELETE FROM proxy_cache").rowcount
        db.commit()
        db.close()
        return ProxyCacheClearResponse(cleared=count)
    except sqlite3.Error:
        return ProxyCacheClearResponse()


@router.get("/surfacing", response_model=ProxySurfacingResponse)
async def proxy_surfacing() -> ProxySurfacingResponse:
    """Get surfacing feedback statistics."""
    fb_path = Path("~/.memtomem/stm_feedback.db").expanduser()
    if not fb_path.exists():
        return ProxySurfacingResponse()

    try:
        db = sqlite3.connect(str(fb_path), check_same_thread=False)
        db.execute("PRAGMA query_only=ON")

        total_s = db.execute("SELECT COUNT(*) FROM surfacing_events").fetchone()
        total_f = db.execute("SELECT COUNT(*) FROM surfacing_feedback").fetchone()

        by_rating: dict = {}
        for r in db.execute("SELECT rating, COUNT(*) FROM surfacing_feedback GROUP BY rating").fetchall():
            by_rating[r[0]] = r[1]

        total_fb = total_f[0] if total_f else 0
        total_sf = total_s[0] if total_s else 0
        helpful = by_rating.get("helpful", 0)
        helpfulness = round(helpful / total_fb * 100, 1) if total_fb > 0 else 0.0

        # Recent events
        recent: list[SurfacingEventInfo] = []
        try:
            for r in db.execute(
                "SELECT id, tool, query, scores, created_at FROM surfacing_events "
                "ORDER BY created_at DESC LIMIT 5"
            ).fetchall():
                import json as _json
                scores = _json.loads(r[3]) if r[3] else []
                recent.append(SurfacingEventInfo(
                    surfacing_id=r[0], tool=r[1], query=r[2],
                    score_count=len(scores), timestamp=r[4],
                ))
        except Exception:
            pass

        # Pending feedback count
        pending = max(0, total_sf - total_fb)

        db.close()
        return ProxySurfacingResponse(
            total_surfacings=total_sf,
            total_feedback=total_fb,
            by_rating=by_rating,
            helpfulness_pct=helpfulness,
            recent_events=recent,
            pending_feedback=pending,
        )
    except sqlite3.Error:
        return ProxySurfacingResponse()


@router.get("/history", response_model=ProxyHistoryResponse)
async def proxy_history(
    server: str | None = Query(None),
    tool: str | None = Query(None),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> ProxyHistoryResponse:
    """Get proxy call history."""
    db = _get_metrics_db()
    if db is None:
        return ProxyHistoryResponse()

    try:
        where_clauses = []
        params: list = []
        if server:
            where_clauses.append("server = ?")
            params.append(server)
        if tool:
            where_clauses.append("tool = ?")
            params.append(tool)

        where = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""

        total = db.execute(
            f"SELECT COUNT(*) FROM proxy_metrics {where}", params
        ).fetchone()[0]

        rows = db.execute(
            f"SELECT server, tool, original_chars, compressed_chars, cleaned_chars, created_at "
            f"FROM proxy_metrics {where} ORDER BY created_at DESC LIMIT ? OFFSET ?",
            params + [limit, offset],
        ).fetchall()

        entries = []
        for r in rows:
            orig = r[2]
            comp = r[3]
            pct = round((1 - comp / orig) * 100, 1) if orig > 0 else 0.0
            entries.append(ProxyHistoryEntry(
                server=r[0], tool=r[1],
                original_chars=orig, compressed_chars=comp,
                cleaned_chars=r[4], savings_pct=pct,
                timestamp=r[5],
            ))

        return ProxyHistoryResponse(entries=entries, total=total)
    finally:
        db.close()
