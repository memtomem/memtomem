"""Tools: mem_stats, mem_status, mem_config, mem_embedding_reset, mem_version."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem import __version__
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _set_config_key

if TYPE_CHECKING:
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


@mcp.tool()
@tool_handler
async def mem_stats(
    ctx: CtxType = None,
) -> str:
    """Return current memory index statistics: total chunks, sources, and storage backend.

    Use this to quickly assess how many memories are indexed before searching.
    """
    app = await _get_app_initialized(ctx)
    data = await app.storage.get_stats()
    total_chunks = data.get("total_chunks", 0)
    total_sources = data.get("total_sources", 0)
    backend = app.config.storage.backend

    out = (
        f"Memory index statistics:\n"
        f"- Total chunks: {total_chunks}\n"
        f"- Total sources: {total_sources}\n"
        f"- Storage backend: {backend}"
    )

    # Surface live degraded-mode state so monitoring probes and the Web UI
    # can detect it without a second tool call. Reads from
    # ``storage.embedding_mismatch`` (not the startup-time
    # ``ctx.embedding_broken`` snapshot) so the line disappears as soon as
    # ``mem_embedding_reset`` clears the mismatch. See ``mem_status`` for
    # the full structured warning block.
    mismatch = getattr(app.storage, "embedding_mismatch", None)
    if mismatch is not None:
        stored = mismatch["stored"]
        cfg = mismatch["configured"]
        out += (
            "\n- Embedding: DEGRADED — "
            f"stored {stored['provider']}/{stored['model']} ({stored['dimension']}d) "
            f"vs configured {cfg['provider']}/{cfg['model']} ({cfg['dimension']}d). "
            'Run mem_embedding_reset(mode="apply_current") to repair.'
        )

    return out


async def collect_status_report(app: AppContext) -> dict:
    """Gather the status report as a structured dict.

    Single source of truth for both surfaces: ``mm status --format json``
    emits this dict verbatim, and ``render_status_report`` turns it into
    the human-readable text ``mem_status`` / ``mm status`` print. Keys are
    part of the CLI's machine-readable contract — treat renames as
    breaking. ``warnings`` entries keep the stable ``kind``/``fix``
    (plus optional ``doc``/``detail``) schema documented on
    ``mem_status``.
    """
    stats = await app.storage.get_stats()
    config = app.config

    stored = getattr(app.storage, "stored_embedding_info", None)
    if stored:
        embedding = {
            "provider": stored["provider"],
            "model": stored["model"],
            "dimension": stored["dimension"],
            "source": "stored",
        }
    else:
        embedding = {
            "provider": config.embedding.provider,
            "model": config.embedding.model,
            "dimension": config.embedding.dimension,
            "source": "configured",
        }

    # Orphan check — count source files no longer on disk
    orphaned = 0
    try:
        source_files = await app.storage.get_all_source_files()
        orphaned = sum(1 for sf in source_files if not sf.exists())
    except Exception:
        logger.debug("Orphan detection failed", exc_info=True)

    # Dense-vector coverage. The ``none`` state surfaces the BM25-only
    # run case loudly: an embedder that crashed mid-init or fell back to
    # NoopEmbedder will still index chunks into ``chunks`` +
    # ``chunks_fts`` but skip ``chunks_vec`` entirely, so semantic search
    # returns nothing while keyword search keeps working. The check is
    # gated on ``hasattr`` so older storage doubles that haven't grown
    # the method don't break the report; ``None`` means "unknown", not
    # "no coverage".
    dense_coverage = None
    if hasattr(app.storage, "get_dense_coverage"):
        try:
            cov = await app.storage.get_dense_coverage()
            total = int(cov["total"])
            with_dense = int(cov["with_dense"])
            if total > 0:
                if with_dense == total:
                    state = "full"
                elif with_dense == 0:
                    state = "none"
                else:
                    state = "partial"
                dense_coverage = {
                    "with_dense": with_dense,
                    "total": total,
                    "percent": round((with_dense / total) * 100, 1),
                    "state": state,
                }
            else:
                dense_coverage = {
                    "with_dense": with_dense,
                    "total": total,
                    "percent": None,
                    "state": "empty",
                }
        except Exception:
            logger.debug("dense coverage query failed", exc_info=True)

    warnings: list[dict] = []
    if config.scheduler.enabled and not config.health_watchdog.enabled:
        warnings.append(
            {
                "kind": "scheduler_watchdog_disabled",
                "detail": "scheduler.enabled=True but health_watchdog.enabled=False",
                "fix": "set health_watchdog.enabled=True (scheduler rides its tick)",
            }
        )
    mismatch = getattr(app.storage, "embedding_mismatch", None)
    if mismatch is not None:
        warnings.append(
            {
                "kind": "embedding_dim_mismatch",
                "stored": dict(mismatch["stored"]),
                "configured": dict(mismatch["configured"]),
                "fix": "uv run mm embedding-reset --mode apply-current",
                "doc": "docs/guides/configuration.md#reset-flow",
            }
        )

    return {
        "config": {
            "storage_backend": config.storage.backend,
            "db_path": str(Path(config.storage.sqlite_path).expanduser()),
            "embedding": embedding,
            "top_k": config.search.default_top_k,
            "rrf_k": config.search.rrf_k,
        },
        "index": {
            "total_chunks": stats["total_chunks"],
            "total_sources": stats["total_sources"],
            "orphaned_sources": orphaned,
            "dense_coverage": dense_coverage,
        },
        # Immutable fields — these cannot be changed via mem_config at
        # runtime. Keys are the dotted names ``mm config set`` takes, so
        # operators are not surprised when a set on one of these paths
        # fails silently.
        "immutable": {
            "embedding.provider": config.embedding.provider,
            "embedding.model": config.embedding.model or None,
            "embedding.dimension": config.embedding.dimension,
            "search.tokenizer": config.search.tokenizer,
            "storage.backend": config.storage.backend,
        },
        "warnings": warnings,
    }


# Dense-coverage hints, keyed by ``dense_coverage["state"]``.
_DENSE_HINTS = {
    "none": "  (BM25-only — dense retrieval will return nothing)",
    "partial": "  (partial dense coverage — some chunks BM25-only)",
}

_IMMUTABLE_GUIDANCE = (
    "  -> To change: re-run `mm init` for provider/tokenizer/backend, "
    "or `mm embedding-reset` to switch embedder (re-index required)."
)


@dataclass(frozen=True)
class StatusLine:
    """One rendered report line, split so stylers never re-parse text.

    ``key`` carries its column padding (and the ``"- "``/``"  "`` warning
    prefix) so ``key + value + suffix`` reproduces the plain line exactly;
    ``role``/``meta`` tell the CLI styler what the line is without the
    regex re-parsing this replaced (#1615).
    """

    role: str  # "title" | "rule" | "section" | "kv" | "immutable_kv"
    # | "dense" | "guidance" | "warning_kv" | "blank"
    key: str = ""
    value: str = ""
    suffix: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.key + self.value + self.suffix


def iter_status_lines(data: dict) -> list[StatusLine]:
    """Lay out a ``collect_status_report`` dict as report lines."""
    cfg = data["config"]
    emb = cfg["embedding"]
    index = data["index"]

    lines = [
        StatusLine("title", value="memtomem Status"),
        StatusLine("rule", value="==============", meta={"tone": "title"}),
        StatusLine("kv", key="Storage:".ljust(11), value=str(cfg["storage_backend"])),
        StatusLine("kv", key="DB path:".ljust(11), value=cfg["db_path"], meta={"value_fg": "cyan"}),
        StatusLine("kv", key="Embedding:".ljust(11), value=f"{emb['provider']} / {emb['model']}"),
        StatusLine("kv", key="Dimension:".ljust(11), value=str(emb["dimension"])),
        StatusLine("kv", key="Top-K:".ljust(11), value=str(cfg["top_k"])),
        StatusLine("kv", key="RRF k:".ljust(11), value=str(cfg["rrf_k"])),
        StatusLine("blank"),
        StatusLine("section", value="Index stats", meta={"tone": "plain"}),
        StatusLine("rule", value="-----------", meta={"tone": "plain"}),
        StatusLine("kv", key="Total chunks:".ljust(15), value=str(index["total_chunks"])),
        StatusLine(
            "kv",
            key="Source files:".ljust(15),
            value=str(index["total_sources"]),
            suffix=(
                f" ({index['orphaned_sources']} orphaned — run mem_cleanup_orphans)"
                if index["orphaned_sources"]
                else ""
            ),
        ),
    ]

    coverage = index["dense_coverage"]
    if coverage is not None:
        percent = coverage["percent"]
        lines.append(
            StatusLine(
                "dense",
                key="Dense vectors:".ljust(15),
                value=f"{coverage['with_dense']}/{coverage['total']}",
                suffix=(
                    f" ({percent}%){_DENSE_HINTS.get(coverage['state'], '')}"
                    if percent is not None
                    else ""
                ),
                meta={"state": coverage["state"]},
            )
        )

    lines += [
        StatusLine("blank"),
        StatusLine("section", value="Immutable fields (set once at init)", meta={"tone": "warn"}),
        StatusLine("rule", value="------------------------------------", meta={"tone": "warn"}),
    ]
    for key, value in data["immutable"].items():
        lines.append(
            StatusLine(
                "immutable_kv",
                key=f"{key}:".ljust(21),
                value="(unset)" if value is None else str(value),
            )
        )
    lines.append(StatusLine("guidance", value=_IMMUTABLE_GUIDANCE))

    if data["warnings"]:
        lines += [
            StatusLine("blank"),
            StatusLine("section", value="Warnings", meta={"tone": "warn"}),
            StatusLine("rule", value="--------", meta={"tone": "warn"}),
        ]
        for warning in data["warnings"]:
            for i, (key, value) in enumerate(warning.items()):
                if isinstance(value, dict):
                    # stored/configured embedding sub-blocks
                    text = f"{value['provider']}/{value['model']} ({value['dimension']}d)"
                else:
                    text = str(value)
                prefix = "- " if i == 0 else "  "
                lines.append(StatusLine("warning_kv", key=prefix + f"{key}:".ljust(12), value=text))

    return lines


def render_status_report(data: dict) -> str:
    """Render a ``collect_status_report`` dict as plain report text."""
    return "\n".join(line.text for line in iter_status_lines(data))


async def format_status_report(app: AppContext) -> str:
    """Render the status report shared by ``mem_status`` and ``mm status``.

    Kept as a free function so the CLI wrapper (#382) can reuse the exact
    same formatting without going through MCP — both surface the same
    text so users learn one output and can recognize it in either place.
    """
    return render_status_report(await collect_status_report(app))


@mcp.tool()
@tool_handler
async def mem_status(
    ctx: CtxType = None,
) -> str:
    """Show indexing statistics and current configuration summary.

    Reports storage backend, embedding info, chunk/source counts, and
    warns when orphaned source files are detected (files removed from
    disk but still indexed — run mem_cleanup_orphans to fix).

    When a configuration drift is detected (e.g. embedding dimension
    mismatch between the DB and the runtime config) the output carries
    a ``Warnings`` block whose entries follow this schema — kept stable
    across versions so external consumers (uptime probes, dashboards)
    can pattern-match on the keys:

    ``kind``    open enum describing the warning. Current values:
                ``embedding_dim_mismatch``. Future releases may add
                ``stale_index``, ``orphan_vectors``, etc. — consumers
                must tolerate unknown kinds rather than erroring.
    ``fix``     the canonical CLI command a user should run.
    ``doc``     optional — a relative-path link into ``docs/guides/``
                with the full remediation flow (see
                ``configuration.md#reset-flow``). Not every warning kind
                carries one (``scheduler_watchdog_disabled`` does not).

    Embedding-mismatch entries also include ``stored`` and ``configured``
    sub-blocks echoing the DB vs runtime provider/model/dimension so the
    user can see what changed without consulting another tool.
    """
    app = await _get_app_initialized(ctx)
    return await format_status_report(app)


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_config(
    key: str | None = None,
    value: str | None = None,
    persist: bool = False,
    ctx: CtxType = None,
) -> str:
    """View or update memtomem configuration values.

    Args:
        key: Dot-notation key to read or write (e.g. "search.default_top_k").
             If omitted, returns the full configuration as JSON.
        value: New value to assign. Omit to read the current value.
        persist: If True, save the change to ~/.memtomem/config.json so it
                 survives server restarts. Default is runtime-only.
    """
    app = await _get_app_initialized(ctx)

    if key and value is not None:
        result = _set_config_key(app.config, key, value)
        # Side effects for specific field changes
        if result.startswith("Set "):
            # Persist FIRST when requested, before any runtime fanout. If the
            # save fails (validation, or a cross-process lock timeout now that
            # save_config_overrides can raise TimeoutError — issue #1567) we
            # revert the config field and return early, so the tokenizer/FTS
            # rebuild below never runs. Otherwise a failed persist would leave
            # the FTS index and tokenizer ahead of the reverted config, and the
            # "rolled back" message would be a lie. Mirrors the CLI
            # ``mm config set`` ordering (persist, then fanout).
            if persist:
                from memtomem.config import save_config_overrides

                try:
                    save_config_overrides(app.config)
                except (ValueError, TimeoutError) as e:
                    # Rollback the runtime mutation by reloading the configuration
                    # from disk. TimeoutError means another process holds the
                    # config write lock — nothing was written, so reverting
                    # runtime keeps memory and disk consistent.
                    from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

                    fresh = Mem2MemConfig()
                    load_config_d(fresh, quiet=True)
                    load_config_overrides(fresh)
                    app.config = fresh
                    if isinstance(e, TimeoutError):
                        return (
                            "Could not persist config: another process is writing "
                            "config.json. Runtime change rolled back; retry in a moment."
                        )
                    return f"Failed to persist config: {e}. Runtime change rolled back."

            # Invalidate search cache so changes take effect immediately.
            app.search_pipeline.invalidate_cache()
            # Rebuild FTS index when tokenizer changes.
            if key == "search.tokenizer":
                from memtomem.storage.fts_tokenizer import set_tokenizer

                set_tokenizer(app.config.search.tokenizer)
                count = await app.storage.rebuild_fts()
                result += f"\nFTS index rebuilt ({count} chunks)."

            result += (
                " (persisted to config.json)" if persist else " (runtime only — not persisted)"
            )
        return result

    config_dict = app.config.model_dump()
    if config_dict.get("embedding", {}).get("api_key"):
        config_dict["embedding"]["api_key"] = "***"
    if config_dict.get("session_trace", {}).get("langfuse_secret_key"):
        config_dict["session_trace"]["langfuse_secret_key"] = "***"

    if key:
        parts = key.split(".")
        node: object = config_dict
        for part in parts:
            if isinstance(node, dict) and part in node:
                node = node[part]
            else:
                return f"Key '{key}' not found in configuration."
        return f"{key} = {node}"

    import json

    def _serialize(obj: object) -> object:
        if isinstance(obj, Path):
            return str(obj)
        if isinstance(obj, (set, frozenset)):
            return list(obj)
        return obj

    return json.dumps(config_dict, indent=2, default=_serialize)


def _revert_to_stored(app: AppContext) -> str:
    """Switch the runtime embedder to match stored DB settings (non-destructive)."""
    from memtomem.embedding.factory import create_embedder
    from memtomem.indexing.engine import IndexEngine
    from memtomem.search.pipeline import SearchPipeline

    storage = app.storage
    config = app.config
    mismatch = storage.embedding_mismatch
    if mismatch is None:
        return "No mismatch detected — nothing to revert."

    stored = mismatch["stored"]

    config.embedding.provider = stored["provider"]
    config.embedding.model = stored["model"]
    config.embedding.dimension = stored["dimension"]

    # ``app.embedder`` / ``app.search_pipeline`` / ``app.index_engine`` are
    # read-only properties that proxy to ``app._components.<name>`` (#399
    # Phase 1). Direct assignment would raise ``AttributeError``. The
    # ``Components`` dataclass is mutable, so we swap fields on the inner
    # container and the properties pick up the new values automatically.
    # ``app.storage`` above already dereferenced ``_components``, so the
    # container is guaranteed non-None by the time we reach here.
    comp = app._components
    assert comp is not None, (
        "_revert_to_stored called before ensure_initialized — "
        "handler must go through _get_app_initialized"
    )
    new_embedder = create_embedder(config.embedding)
    comp.embedder = new_embedder
    comp.search_pipeline = SearchPipeline(
        storage=storage,
        embedder=new_embedder,
        config=config.search,
        decay_config=config.decay,
        mmr_config=config.mmr,
        access_config=config.access,
        context_window_config=config.context_window,
        llm_provider=app.llm_provider,
        session_summary_config=config.session_summary,
    )
    comp.index_engine = IndexEngine(
        storage=storage,
        embedder=new_embedder,
        config=config.indexing,
        namespace_config=config.namespace,
        progress_threshold=config.embedding.progress_threshold,
        # Preserve the LLM provider on rebuild — the engine consumes it
        # for the per-source AI summary path (``maybe_update_ai_summary``
        # in ``_index_file``), and dropping it here would silently
        # disable summary generation after every embedding-reset /
        # revert-to-stored until the server restart re-runs
        # ``component_factory.create_components``.
        llm=app.llm_provider,
    )

    storage.clear_embedding_mismatch()

    return (
        f"Reverted to stored DB settings: "
        f"{stored['provider']}/{stored['model']} ({stored['dimension']}d). "
        f"Search should work normally now."
    )


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_embedding_reset(
    mode: str = "status",
    ctx: CtxType = None,
) -> str:
    """Check or resolve embedding configuration mismatches between DB and current config.

    Args:
        mode: One of:
            - "status" (default): Show DB stored values vs current config.
            - "apply_current": Reset DB to current config. DESTRUCTIVE — deletes all vectors, re-index required.
            - "revert_to_stored": Switch runtime embedder to match DB stored values. Non-destructive.
    """
    app = await _get_app_initialized(ctx)

    if mode not in ("status", "apply_current", "revert_to_stored"):
        return f"Invalid mode '{mode}'. Use: status, apply_current, or revert_to_stored."

    stored = getattr(app.storage, "stored_embedding_info", None)
    mismatch = getattr(app.storage, "embedding_mismatch", None)
    config = app.config

    if mode == "status":
        lines = ["Embedding Status"]
        if stored:
            lines.append(
                f"  DB stored:  {stored['provider']}/{stored['model']} ({stored['dimension']}d)"
            )
        lines.append(
            f"  Config:     {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d)"
        )
        if mismatch is None:
            lines.append("\nNo mismatch — DB and config are in sync.")
        else:
            lines.append("\nWarning: Mismatch detected!")
            lines.append('  -> "apply_current": reset DB to config (destructive, re-index needed)')
            lines.append('  -> "revert_to_stored": switch embedder to match DB (non-destructive)')
        return "\n".join(lines)

    if mode == "apply_current":
        await app.storage.reset_embedding_meta(
            dimension=config.embedding.dimension,
            provider=config.embedding.provider,
            model=config.embedding.model,
        )
        return (
            f"DB reset to {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d). All vectors deleted — run mem_index to re-index."
        )

    # mode == "revert_to_stored"
    return _revert_to_stored(app)


@mcp.tool()
@tool_handler
@register("advanced")
async def mem_reset(
    confirm: bool = False,
    ctx: CtxType = None,
) -> str:
    """Delete ALL data (chunks, sessions, history, etc.) and reinitialize the DB.

    Embedding configuration is preserved. A re-index is required afterwards.

    Args:
        confirm: Must be True to proceed. Prevents accidental data loss.
    """
    if not confirm:
        app = await _get_app_initialized(ctx)
        stats = await app.storage.get_stats()
        total = stats.get("total_chunks", 0)
        return (
            f"Database has {total} chunks. "
            "This will permanently delete ALL data. "
            "Pass confirm=True to proceed."
        )

    app = await _get_app_initialized(ctx)
    deleted = await app.storage.reset_all()
    summary = ", ".join(f"{t}: {c}" for t, c in deleted.items() if c > 0)
    return f"Database reset complete. Deleted: {summary or 'empty'}. Run mem_index to re-index."


@tool_handler
@register("advanced")
async def mem_version(
    ctx: CtxType = None,
) -> str:
    """Return server version and supported capabilities for protocol negotiation.

    Used by external systems (e.g. memtomem-stm) to discover which features
    are available before using them. Callable via mem_do(action="version").
    """
    return json.dumps(
        {
            "version": __version__,
            "capabilities": {
                "search_formats": ["compact", "verbose", "structured"],
            },
        },
        ensure_ascii=False,
    )
