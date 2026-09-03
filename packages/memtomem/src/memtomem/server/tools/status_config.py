"""Tools: mem_stats, mem_status, mem_config, mem_embedding_reset, mem_version."""

from __future__ import annotations

import asyncio
import json
import importlib.util
from importlib.metadata import PackageNotFoundError, version as distribution_version
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from memtomem import __version__
from memtomem._instance_registry import (
    enumerate_live_instances as _enumerate_live_instances,
    store_digest_for as _store_digest_for,
)
from memtomem.embedding.runtime import publish_onnx_batch_size
from memtomem.generation import ComponentGeneration
from memtomem.runtime.components import prune_settled_generations
from memtomem.server import mcp
from memtomem.server.context import CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.helpers import _set_config_key
from memtomem.secret_masking import is_secret_key, mask_secrets

if TYPE_CHECKING:
    from memtomem.config import SaveReceipt
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


def _dependency_state(module: str, distribution: str | None = None) -> dict[str, object]:
    """Return a secret-free, non-importing dependency availability report."""
    try:
        available = importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        # Test doubles and partially initialized imports may have no __spec__.
        available = False
    installed_version: str | None = None
    if available:
        try:
            installed_version = distribution_version(distribution or module)
        except PackageNotFoundError:
            pass
    return {"available": available, "version": installed_version}


def collect_runtime_profile() -> dict[str, object]:
    """Describe the effective retrieval runtime without initializing models or storage."""
    try:
        from memtomem.config import Mem2MemConfig, load_config_d, load_config_overrides

        config = Mem2MemConfig()
        load_config_d(config, quiet=True)
        load_config_overrides(config, migrate=False)
    except Exception:
        logger.debug("Unable to collect runtime profile", exc_info=True)
        return {"schema_version": 1, "config_state": "error"}

    fastembed = _dependency_state("fastembed")
    kiwi = _dependency_state("kiwipiepy")
    provider = str(config.embedding.provider)
    dense_configured = bool(config.search.enable_dense and provider != "none")
    dense_available = dense_configured and not (
        provider == "onnx" and not bool(fastembed["available"])
    )
    bm25_available = bool(config.search.enable_bm25)

    def retrieval_mode(*, dense: bool) -> str:
        if bm25_available and dense:
            return "hybrid"
        if bm25_available:
            return "bm25_only"
        if dense:
            return "dense_only"
        return "disabled"

    configured_mode = retrieval_mode(dense=dense_configured)
    effective_mode = retrieval_mode(dense=dense_available)

    missing_extras: list[str] = []
    fastembed_required_for: list[str] = []
    if provider == "onnx":
        fastembed_required_for.append("embedding")
    if config.rerank.enabled and config.rerank.provider == "fastembed":
        fastembed_required_for.append("rerank")
    if fastembed_required_for and not fastembed["available"]:
        missing_extras.append("onnx")
    if config.search.tokenizer == "kiwipiepy" and not kiwi["available"]:
        missing_extras.append("korean")

    fastembed["required_for"] = fastembed_required_for
    kiwi["required_for"] = ["tokenizer"] if config.search.tokenizer == "kiwipiepy" else []
    return {
        "schema_version": 1,
        "config_state": "ok",
        "embedding": {
            "provider": provider,
            "model": str(config.embedding.model),
            "dimension": int(config.embedding.dimension),
        },
        "search": {
            "enable_bm25": bool(config.search.enable_bm25),
            "enable_dense": bool(config.search.enable_dense),
            "tokenizer": str(config.search.tokenizer),
            "configured_mode": configured_mode,
            "effective_mode": effective_mode,
        },
        "rerank": {
            "enabled": bool(config.rerank.enabled),
            "provider": str(config.rerank.provider),
        },
        "dependencies": {"fastembed": fastembed, "kiwipiepy": kiwi},
        "missing_extras": missing_extras,
    }


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


def _collect_concurrent_writers(db_path: Path) -> dict | None:
    """Build the ``concurrent_server_writers`` warning, or ``None``.

    Runs in a worker thread (registry probing takes a bounded
    cross-process lock). Live registrations are grouped by ``procid`` —
    the per-process random identity — because pid values can collide
    across pid namespaces while two flagged contexts in one process must
    still count as one server. The kind is deliberately cause-neutral:
    two live servers on one store is *also* the documented-legitimate
    multi-editor-session state ("One server at a time" in
    ``docs/guides/mcp-clients.md``), so the wording names both readings
    and the machine fields carry observations only. Values are scalar
    strings (the generic warning renderer requires it) and contain pids
    only — no paths, no digests.
    """
    digest = _store_digest_for(db_path)
    if digest is None:
        return None
    result = _enumerate_live_instances(digest)
    if not result.complete:
        return None
    groups: dict[str, object] = {}
    for info in result.instances:
        groups.setdefault(info.procid, info)
    if len(groups) < 2:
        return None
    ordered = sorted(groups.values(), key=lambda i: (i.pid, i.procid))  # type: ignore[attr-defined]
    pids = ", ".join(str(i.pid) for i in ordered)  # type: ignore[attr-defined]
    detail = (
        f"{len(groups)} live memtomem-server processes (pids {pids}) have this "
        "store open. Expected when multiple editor sessions are open; with a "
        "single session it often means one client has two memtomem "
        "registrations (e.g. manual + plugin)."
    )
    warning: dict = {
        "kind": "concurrent_server_writers",
        "detail": detail,
        "fix": "If unintended, keep exactly one memtomem registration in your "
        "MCP client and restart it — see the coexistence guide.",
        "doc": "docs/guides/mcp-clients.md#one-server-at-a-time",
    }
    # Per-GROUP representatives, matching the grouping semantics above —
    # a second registration inside one process contributes no extra vote.
    ppids = {info.ppid for info in ordered}  # type: ignore[attr-defined]
    if len(ppids) == 1:
        # Observation, not a verdict: equal recorded parent pids suggest —
        # but do not prove — one client launched both (pid reuse and
        # namespaces exist). The cause hypothesis stays in prose above.
        warning["same_parent"] = "true"
        warning["detail"] = detail + " All entries recorded the same parent PID."
    return warning


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
    try:
        cwd = Path(os.getcwd()).resolve()
    except OSError:
        cwd = None
    from memtomem.server.tools.search import _resolve_project_context_root

    project_root = _resolve_project_context_root(app)

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

    db_path_resolved = Path(config.storage.sqlite_path).expanduser().resolve()

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
        warning_kind = (
            "embedding_policy_mismatch"
            if mismatch.get("policy_mismatch")
            and not mismatch.get("dimension_mismatch")
            and not mismatch.get("model_mismatch")
            else "embedding_dim_mismatch"
        )
        warnings.append(
            {
                "kind": warning_kind,
                "stored": dict(mismatch["stored"]),
                "configured": dict(mismatch["configured"]),
                "fix": "uv run mm embedding-reset --mode apply-current",
                "doc": "docs/guides/configuration.md#reset-flow",
            }
        )
    # #1619: an explicitly enabled MMR silently does nothing without dense
    # retrieval (no vectors to diversify over) — surface the mismatch where
    # BM25-only operators will see it. MMR defaults to disabled, so this
    # never fires out of the box.
    if config.mmr.enabled and not config.search.enable_dense:
        warnings.append(
            {
                "kind": "mmr_disabled_no_dense",
                "detail": "mmr.enabled=True but search.enable_dense=False — diversity "
                "re-ranking is skipped",
                "fix": "set search.enable_dense=True (needs an embedding provider) "
                "or set mmr.enabled=False",
            }
        )

    # Entity-match boost ranks on ``chunk_entities`` rows. The indexer writes
    # those for every chunk it stores (#2145), so an empty table means a store
    # last indexed before that landed, or one indexed with
    # ``indexing.extract_entities`` off — either way an enabled boost over it is
    # inert rather than broken, and silently so. Same shape as the MMR advisory
    # above.
    if config.entity_boost.enabled and hasattr(app.storage, "get_entity_type_counts"):
        if not await app.storage.get_entity_type_counts():
            warnings.append(
                {
                    "kind": "entity_boost_no_entities",
                    "detail": "entity_boost.enabled=True but no entities have been "
                    "extracted — the boost is inert until this store is indexed "
                    "by a build that extracts entities, or a scan runs",
                    # ``mm index <path>`` alone will not do it: a file whose
                    # content is unchanged bucket as ``unchanged``, never
                    # ``to_upsert``, so no chunk is rewritten and no entity is
                    # extracted. ``--force`` is what re-writes them.
                    "fix": 'run mem_do(action="entity_scan") to populate entities, or '
                    "re-index with mm index --force <path> (with "
                    "indexing.extract_entities on, the default)",
                }
            )

    # #1935: two live memtomem-server processes with this store open. The
    # registry probe is filesystem work behind a bounded cross-process
    # lock, so it runs off the event loop; any failure or incomplete pass
    # yields no warning (fail-open — this is an advisory, never a guess).
    try:
        concurrent = await asyncio.to_thread(_collect_concurrent_writers, db_path_resolved)
        if concurrent is not None:
            warnings.append(concurrent)
    except Exception:
        logger.debug("concurrent-writer detection failed", exc_info=True)

    from memtomem.indexing.watcher import effective_watcher_backend

    return {
        "config": {
            "storage_backend": config.storage.backend,
            "db_path": str(db_path_resolved),
            "embedding": embedding,
            "top_k": config.search.default_top_k,
            "rrf_k": config.search.rrf_k,
            "memory_dirs": [
                str(Path(p).expanduser().resolve()) for p in config.indexing.memory_dirs
            ],
            "project_memory_dirs": [
                str(Path(p).expanduser().resolve()) for p in config.indexing.project_memory_dirs
            ],
            "watcher_backend": effective_watcher_backend(config.indexing),
        },
        "runtime": {
            "cwd": str(cwd) if cwd is not None else None,
            "project_context_root": str(project_root) if project_root is not None else None,
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

_MAX_STATUS_SOURCE_ROWS = 8
_PROVIDER_SOURCE_LABELS = {
    "claude-memory": "Claude project memories",
    "claude-plans": "Claude plans",
    "codex": "Codex memories",
}


@dataclass(frozen=True)
class StatusLine:
    """One rendered report line, split so stylers never re-parse text.

    ``key`` carries its column padding (and the ``"- "``/``"  "`` warning
    prefix) so ``key + value + suffix`` reproduces the plain line exactly;
    ``role``/``meta`` tell the CLI styler what the line is without the
    regex re-parsing this replaced (#1615).
    """

    role: str  # "title" | "rule" | "section" | "kv" | "immutable_kv"
    # | "dense" | "guidance" | "source_item" | "source_hint"
    # | "warning_kv" | "blank"
    key: str = ""
    value: str = ""
    suffix: str = ""
    meta: dict = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.key + self.value + self.suffix


def _shorten_status_path(path: str, *, home: str | Path | None = None) -> str:
    """Contract a path below the user's home for deterministic human output.

    ``collect_status_report`` deliberately keeps resolved absolute paths for
    the JSON contract.  This helper is presentation-only and handles either
    slash style so a Windows-shaped fixture behaves the same on POSIX CI.
    """
    home_text = str(Path.home().resolve() if home is None else home)
    normalized_path = path.replace("\\", "/").rstrip("/")
    normalized_home = home_text.replace("\\", "/").rstrip("/")
    if not normalized_home:
        return path

    windows_style = (
        len(normalized_home) >= 2 and normalized_home[1] == ":"
    ) or normalized_home.startswith("//")
    compared_path = normalized_path.casefold() if windows_style else normalized_path
    compared_home = normalized_home.casefold() if windows_style else normalized_home
    if compared_path != compared_home and not compared_path.startswith(compared_home + "/"):
        return path

    relative = normalized_path[len(normalized_home) :].lstrip("/")
    if not relative:
        return "~"
    separator = "\\" if "\\" in path and "/" not in path else "/"
    return f"~{separator}{relative.replace('/', separator)}"


def _status_source_entries(paths: list[str], *, group_providers: bool) -> list[tuple[str, int]]:
    """Return stable display rows paired with the source count each represents."""
    if not group_providers:
        return [(_shorten_status_path(path), 1) for path in paths]

    from memtomem.config import categorize_memory_dir

    categories = [categorize_memory_dir(path) for path in paths]
    category_counts: dict[str, int] = {}
    for category in categories:
        category_counts[category] = category_counts.get(category, 0) + 1

    entries: list[tuple[str, int]] = []
    emitted_categories: set[str] = set()
    for path, category in zip(paths, categories, strict=True):
        count = category_counts[category]
        if category != "user" and count > 1:
            if category in emitted_categories:
                continue
            emitted_categories.add(category)
            label = _PROVIDER_SOURCE_LABELS.get(category, category.replace("-", " ").title())
            entries.append((f"{label} ({count} dirs)", count))
            continue
        entries.append((_shorten_status_path(path), 1))
    return entries


def _status_path_is_within(path: str, root: str) -> bool:
    """Return whether *path* is at or below *root* across either slash style."""
    normalized_path = path.replace("\\", "/").rstrip("/")
    normalized_root = root.replace("\\", "/").rstrip("/")
    if not normalized_path or not normalized_root:
        return False

    windows_style = (
        len(normalized_root) >= 2 and normalized_root[1] == ":"
    ) or normalized_root.startswith("//")
    compared_path = normalized_path.casefold() if windows_style else normalized_path
    compared_root = normalized_root.casefold() if windows_style else normalized_root
    return compared_path == compared_root or compared_path.startswith(compared_root + "/")


def _prioritize_status_paths(paths: list[str], priority_root: str | None) -> list[str]:
    """Stable-partition paths so the active project's sources remain visible."""
    if not priority_root:
        return paths
    active = [path for path in paths if _status_path_is_within(path, priority_root)]
    if not active:
        return paths
    other = [path for path in paths if not _status_path_is_within(path, priority_root)]
    return [*active, *other]


def _status_source_lines(
    label: str,
    paths: list[str],
    *,
    group_providers: bool,
    priority_root: str | None = None,
) -> list[StatusLine]:
    """Render one source tier as a count plus a bounded, readable list."""
    header_key = f"{label}:".ljust(17)
    if not paths:
        return [StatusLine("kv", key=header_key, value="0 (none)")]

    ordered_paths = _prioritize_status_paths(paths, priority_root)
    entries = _status_source_entries(ordered_paths, group_providers=group_providers)
    shown = entries[:_MAX_STATUS_SOURCE_ROWS]
    lines = [StatusLine("kv", key=header_key, value=str(len(paths)))]
    lines.extend(StatusLine("source_item", value=f"  - {text}") for text, _ in shown)

    hidden_count = sum(count for _, count in entries[_MAX_STATUS_SOURCE_ROWS:])
    if hidden_count:
        lines.append(
            StatusLine(
                "source_hint",
                value=f"  … (+{hidden_count} more; use `mm status --json`)",
            )
        )
    elif any(count > 1 for _, count in shown):
        lines.append(
            StatusLine(
                "source_hint",
                value="  … (use `mm status --json` for full paths)",
            )
        )
    return lines


def iter_status_lines(data: dict) -> list[StatusLine]:
    """Lay out a ``collect_status_report`` dict as report lines."""
    cfg = data["config"]
    emb = cfg["embedding"]
    index = data["index"]
    runtime = data.get("runtime", {})

    lines = [
        StatusLine("title", value="memtomem Status"),
        StatusLine("rule", value="==============", meta={"tone": "title"}),
        StatusLine("kv", key="Storage:".ljust(11), value=str(cfg["storage_backend"])),
        StatusLine(
            "kv",
            key="DB path:".ljust(11),
            value=_shorten_status_path(cfg["db_path"]),
            meta={"value_fg": "cyan"},
        ),
        StatusLine("kv", key="Embedding:".ljust(11), value=f"{emb['provider']} / {emb['model']}"),
        StatusLine("kv", key="Dimension:".ljust(11), value=str(emb["dimension"])),
        StatusLine("kv", key="Top-K:".ljust(11), value=str(cfg["top_k"])),
        StatusLine("kv", key="RRF k:".ljust(11), value=str(cfg["rrf_k"])),
        StatusLine("kv", key="Watcher:".ljust(11), value=str(cfg["watcher_backend"])),
        StatusLine("blank"),
        StatusLine("section", value="Runtime context", meta={"tone": "plain"}),
        StatusLine("rule", value="---------------", meta={"tone": "plain"}),
        StatusLine(
            "kv",
            key="CWD:".ljust(17),
            value=(
                _shorten_status_path(str(runtime["cwd"])) if runtime.get("cwd") else "(unavailable)"
            ),
        ),
        StatusLine(
            "kv",
            key="Project root:".ljust(17),
            value=(
                _shorten_status_path(str(runtime["project_context_root"]))
                if runtime.get("project_context_root")
                else "(none registered for CWD)"
            ),
        ),
    ]
    lines.extend(
        _status_source_lines(
            "User sources",
            [str(path) for path in cfg.get("memory_dirs", [])],
            group_providers=True,
        )
    )
    lines.extend(
        _status_source_lines(
            "Project sources",
            [str(path) for path in cfg.get("project_memory_dirs", [])],
            group_providers=False,
            priority_root=(
                str(runtime["project_context_root"])
                if runtime.get("project_context_root")
                else None
            ),
        )
    )
    lines += [
        StatusLine("blank"),
        StatusLine("section", value="Index stats", meta={"tone": "plain"}),
        StatusLine("rule", value="-----------", meta={"tone": "plain"}),
        StatusLine("kv", key="Total chunks:".ljust(15), value=str(index["total_chunks"])),
        StatusLine(
            "kv",
            key="Source files:".ljust(15),
            value=str(index["total_sources"]),
            suffix=(
                f" ({index['orphaned_sources']} orphaned — run `mm gc orphan-sources`)"
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
                # ``ljust`` alone loses the key/value separator once the
                # key reaches the pad width (``same_parent:`` is exactly
                # 12 chars — #1935); guarantee at least one space while
                # keeping shorter keys byte-identical to the historic
                # layout.
                padded = f"{key}:".ljust(12)
                if not padded.endswith(" "):
                    padded += " "
                lines.append(StatusLine("warning_kv", key=prefix + padded, value=text))

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

    Reports storage backend, embedding info, chunk/source counts, and warns
    about orphaned source files (indexed but gone from disk — run
    mem_cleanup_orphans).

    Configuration drift adds a ``Warnings`` block. Each entry has ``kind``
    (an open enum — tolerate unrecognised values rather than erroring),
    ``fix`` and an optional ``doc`` link. Some kinds add their own keys:
    embedding mismatches carry ``stored``/``configured``;
    ``concurrent_server_writers`` may carry ``same_parent``. These keys are
    stable across versions, so probes can pattern-match on them.
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
            receipt = None
            if persist:
                from memtomem.config import save_config_overrides

                try:
                    receipt = save_config_overrides(app.config)
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
            if key == "embedding.onnx_batch_size":
                publish_onnx_batch_size(app.embedder, app.config.embedding.onnx_batch_size)
            # Rebuild FTS index when tokenizer changes.
            if key == "search.tokenizer":
                from memtomem.storage.fts_tokenizer import set_tokenizer

                set_tokenizer(app.config.search.tokenizer)
                count = await app.storage.rebuild_fts()
                result += f"\nFTS index rebuilt ({count} chunks)."

            suffix = _persistence_suffix(key, receipt)
            result += suffix
            if is_secret_key(key.rsplit(".", 1)[-1]):
                return f"Set {key} = '***'{suffix}"
        return result

    config_dict = mask_secrets(app.config.model_dump())

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


def _persistence_suffix(key: str, receipt: "SaveReceipt | None") -> str:
    """Say what ``persist=True`` actually left in config.json (issue #2108).

    ``save_config_overrides`` writes a delta, so a value matching the lower
    layers (an env var, a ``config.d`` fragment, or the default) is pruned
    rather than stored — "(persisted to config.json)" would be a claim the
    file does not back. The runtime mutation happened either way; only the
    durability half of the message changes. The receipt is read instead of
    the file so a concurrent writer cannot rewrite the answer underneath us.
    """
    from memtomem.config import MISSING, env_var_owning

    if receipt is None:
        return " (runtime only — not persisted)"

    section_name, _, field_name = key.partition(".")
    env_var = env_var_owning(section_name, field_name)
    if receipt.pinned_after(section_name, field_name) is not MISSING:
        if env_var is None:
            return " (persisted to config.json)"
        # "survives server restarts" is the documented promise of persist=True,
        # and an env var breaks exactly that half: the next start reads the
        # variable, not the file.
        return (
            f" (persisted to config.json, but {env_var} takes precedence — "
            f"a restart reads that variable, not this value)"
        )
    reason = (
        f"{env_var} takes precedence"
        if env_var
        else "the value already comes from a lower layer (default or config.d)"
    )
    return f" (runtime only — not written to config.json: {reason})"


async def _revert_to_stored(app: AppContext) -> str:
    """Switch the runtime embedder to match stored DB settings (non-destructive).

    Publish-first, then retire: the new embedder/pipeline/engine generation is
    installed on the ``Components`` container before the old one is closed
    (the ``swap_reranker`` contract, #1777). Without the close, every revert
    leaked the retired ONNX InferenceSession plus its dedicated executor
    thread and the retired pipeline's reranker; without the watcher rebind,
    auto-reindexes kept running through the retired engine and embedder while
    cache invalidation hit a pipeline nobody queries.

    The retirement is lease-counted (#2180): a search or index run that entered
    before the swap holds the old generation, so the close waits for its last
    release instead of pulling the embedder out from under it. With nothing in
    flight the close still runs inline here — retirement never waits on a
    timeout. The embedder users outside the pipeline and the engine — the dedup
    scanner, ``mem_conflicts``, formation, bundle import, and model warmup —
    count into the same handle since #2199, so the accounting covers every
    production path that reaches the embedder.

    Serialized on ``app._config_lock``: without it, two concurrent reverts
    both observe the mismatch, both publish a generation, and the loser
    closes the winner's freshly published embedder while requests use it.
    The mismatch is cleared in the same synchronous phase as publication —
    before the first retirement ``await`` — so a second caller entering the
    lock sees "nothing to revert" rather than a repeatable swap.
    """
    from memtomem.embedding.factory import create_embedder
    from memtomem.indexing.engine import IndexEngine
    from memtomem.search.dedup import DedupScanner
    from memtomem.search.pipeline import SearchPipeline

    async with app._config_lock:
        return await _revert_to_stored_locked(
            app, create_embedder, IndexEngine, DedupScanner, SearchPipeline
        )


async def _revert_to_stored_locked(
    app: AppContext, create_embedder, IndexEngine, DedupScanner, SearchPipeline
) -> str:
    storage = app.storage
    config = app.config
    mismatch = storage.embedding_mismatch
    if mismatch is None:
        # Nothing to swap, but this is still the non-destructive retry for a
        # service whose recovery start failed on an earlier reset (#2181).
        # Without it the only in-process retry would be ``apply_current``,
        # which drops every vector to restart a scheduler.
        await app.recover_from_degraded()
        return "No mismatch detected — nothing to revert."

    stored = mismatch["stored"]

    config.embedding.provider = stored["provider"]
    config.embedding.model = stored["model"]
    config.embedding.dimension = stored["dimension"]
    if stored.get("max_sequence_tokens") is not None:
        config.embedding.max_sequence_tokens = stored["max_sequence_tokens"]
    storage._embedding_policy_fingerprint = stored.get("policy_fingerprint", "")
    storage._embedding_max_sequence_tokens = stored.get("max_sequence_tokens")

    # ``app.embedder`` / ``app.search_pipeline`` / ``app.index_engine`` are
    # read-only properties that proxy to ``app._components.<name>`` (#399
    # Phase 1). Direct assignment would raise ``AttributeError``. The
    # ``Components`` dataclass is mutable, so we swap fields on the inner
    # container and the properties pick up the new values automatically.
    # ``app.storage`` above already dereferenced ``_components``, so the
    # container is guaranteed non-None by the time we reach here.
    runtime_app = app._runtime_owner or app
    comp = runtime_app._components
    assert comp is not None, (
        "_revert_to_stored called before ensure_initialized — "
        "handler must go through _get_app_initialized"
    )
    old_embedder = comp.embedder
    old_pipeline = comp.search_pipeline
    # The new triple counts its in-flight users on a fresh handle (#2180).
    # Once the components below are published nothing new can reach the old
    # handle through ``app.*``, so its count only falls from here — except for
    # a caller that read the components before the swap and enters afterwards,
    # which keeps the pre-#2180 exposure.
    old_generation = comp.generation
    assert old_generation is not None, "Components.__post_init__ always sets a generation"
    new_generation = ComponentGeneration()
    new_embedder = create_embedder(config.embedding)
    comp.embedder = new_embedder
    comp.generation = new_generation
    comp.search_pipeline = SearchPipeline(
        storage=storage,
        embedder=new_embedder,
        config=config.search,
        decay_config=config.decay,
        mmr_config=config.mmr,
        access_config=config.access,
        # Kept in step with the full wiring in ``component_factory`` for the
        # scoring stages: omitting a boost config here silently disables that
        # stage until restart (``importance_config`` was missing here).
        importance_config=config.importance,
        entity_boost_config=config.entity_boost,
        context_window_config=config.context_window,
        llm_provider=app.llm_provider,
        session_summary_config=config.session_summary,
        generation=new_generation,
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
        generation=new_generation,
    )

    # The watcher and the dedup scanner captured the old engine/embedder at
    # init (server/context.py); without a rebind they keep the retired
    # generation alive and doing work after this swap.
    watcher = runtime_app._watcher
    if watcher is not None:
        watcher.rebind(comp.index_engine, comp.search_pipeline)
    if runtime_app.dedup_scanner is not None:
        runtime_app._dedup_scanner = DedupScanner(
            storage=storage,
            embedder=new_embedder,
            # The freshly published generation, not the retired one: this
            # scanner's scans must count into what the *next* revert retires.
            generation=new_generation,
        )

    # Publication is complete: clear the mismatch in the same synchronous
    # phase, before the first retirement ``await``, so a concurrent caller
    # (serialized behind _config_lock) observes "nothing to revert" instead
    # of swapping — and closing — this freshly published generation.
    storage.clear_embedding_mismatch()

    # Start what degraded startup skipped (#2181). Placed here, not after the
    # retirement loop below: that loop re-raises a deferred cancellation, which
    # would skip recovery entirely. It has to run after the rebind above so the
    # watcher starts against the new generation, not the retired one.
    # Its own cancellation joins the deferred accumulation rather than
    # propagating here — a cancel mid-recovery must not skip the retirement
    # closes and reintroduce the #2176 leak.
    first_cancel: asyncio.CancelledError | None = None
    try:
        await app.recover_from_degraded()
    except asyncio.CancelledError as exc:
        first_cancel = exc

    # New generation is published everywhere — retire the old one. A close
    # that fails must not fail the revert the user asked for: the swap
    # already happened, so log and continue (the leak is then no worse than
    # the pre-close behavior). Cancellation is accumulated and deferred
    # (the lifespan teardown pattern): every retirement step is attempted
    # even when this task is cancelled mid-close, then the cancellation
    # propagates.
    async def _close_old_generation() -> None:
        cancelled: asyncio.CancelledError | None = None
        for resource, label in ((old_pipeline, "search pipeline"), (old_embedder, "embedder")):
            close = getattr(resource, "close", None)
            if close is None:
                continue
            try:
                await close()
            except asyncio.CancelledError as exc:
                if cancelled is None:
                    cancelled = exc
            except Exception:
                logger.warning(
                    "Failed to close the retired %s after revert_to_stored; "
                    "its resources are leaked until restart",
                    label,
                    exc_info=True,
                )
        if cancelled is not None:
            raise cancelled

    # Tracked before the retire call so shutdown can still close this
    # generation if its last leaseholder never releases (#2180).
    comp.retired_generations.append(old_generation)
    pending_close = old_generation.retire(_close_old_generation)
    if pending_close is not None:
        # Idle: close inline, exactly as the pre-#2180 revert did.
        try:
            await pending_close
        except asyncio.CancelledError as exc:
            if first_cancel is None:
                first_cancel = exc
    else:
        # In flight: the last release runs the close as a background task, so
        # this revert returns without waiting on it. A cancellation raised
        # there ends that task instead of this call — nothing to accumulate.
        logger.info(
            "revert_to_stored: %d in-flight lease(s) on the retired generation; "
            "deferring its close to the last release",
            old_generation.leases,
        )
    # The entry above is only needed while its close is outstanding. An
    # inline close is already done by now, so pruning here keeps the list to
    # the generations shutdown actually has to drain instead of growing it by
    # one per revert (#2201).
    prune_settled_generations(comp)
    if first_cancel is not None:
        raise first_cancel

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
            if stored.get("max_sequence_tokens") is not None:
                lines.append(f"  DB max sequence tokens: {stored['max_sequence_tokens']}")
        lines.append(
            f"  Config:     {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d)"
        )
        lines.append(f"  Config max sequence tokens: {config.embedding.max_sequence_tokens}")
        if mismatch is None:
            lines.append("\nNo mismatch — DB and config are in sync.")
        else:
            lines.append("\nWarning: Mismatch detected!")
            lines.append('  -> "apply_current": reset DB to config (destructive, re-index needed)')
            lines.append('  -> "revert_to_stored": switch embedder to match DB (non-destructive)')
        return "\n".join(lines)

    if mode == "apply_current":
        from memtomem.config import embedding_policy_fingerprint

        await app.storage.reset_embedding_meta(
            dimension=config.embedding.dimension,
            provider=config.embedding.provider,
            model=config.embedding.model,
            policy_fingerprint=embedding_policy_fingerprint(config.embedding),
            max_sequence_tokens=config.embedding.max_sequence_tokens,
        )
        # The mismatch is cleared, so the background loops a degraded startup
        # skipped can run again (#2181). Called unconditionally: on a context
        # that already recovered this no-ops, and it is also the retry path
        # for a service whose earlier recovery start failed.
        await app.recover_from_degraded()
        # The remedy names the CLI because re-embedding a whole tree is a
        # long, interruptible job better run from a shell than from a tool
        # call holding a client's turn open. It is no longer a safety caveat:
        # both paths preserve each file's stored namespace, including under a
        # session (ADR-0033, #2104).
        return (
            f"DB reset to {config.embedding.provider}/{config.embedding.model} "
            f"({config.embedding.dimension}d). All vectors deleted — re-embed "
            "with `mm index --force <memory_dir>` (CLI) or "
            "mem_index(force=true); either way each file keeps the namespace "
            "its chunks are stored under. Until the re-embed runs, dense "
            "search finds nothing."
        )

    # mode == "revert_to_stored"
    return await _revert_to_stored(app)


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
    runtime_profile = collect_runtime_profile()
    return json.dumps(
        {
            "version": __version__,
            "capabilities": {
                "search_formats": ["compact", "verbose", "structured"],
                "context_compose": {"schema_version": 4},
                "candidate_propose": {"schema_version": 1},
                "runtime_profile": {"schema_version": 1},
            },
            "runtime_profile": runtime_profile,
        },
        ensure_ascii=False,
    )
