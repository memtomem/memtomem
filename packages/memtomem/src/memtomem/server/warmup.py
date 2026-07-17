"""Opt-in eager model warmup (#1621).

The embedder and reranker load their models lazily on first use
(``_get_model``), so the first query after server start pays the full
model download/load cost. ``config.warmup.enabled`` (default off) lets
operators front-load that: ``app_lifespan`` spawns :func:`spawn_warmup`
as a background task right after allocating the ``AppContext``, and
``mm warmup`` runs :func:`warm_models` one-shot for CLI users.

Only local providers are warmed. The three local model classes
(``OnnxEmbedder``, ``LocalReranker``, ``FastEmbedReranker``) expose a
sync ``_get_model``; remote providers (ollama, openai, cohere) don't,
so the ``getattr`` sniff below skips them by construction — warming a
remote provider would mean real (possibly billed) API calls. The sniff
reuses the private ``_get_model`` convention the model-readiness
endpoint and the web hot-reload path already introspect (warmup is
broader: readiness only inspects onnx/fastembed, warmup also warms the
``local`` reranker).

Lazy-import discipline: module level stays stdlib-only (plus the
itself-stdlib-only ``memtomem._settlement`` helper) so importing this
from the lifespan's flag gate adds nothing to the flag-off handshake
path.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from memtomem._settlement import settle_shielded

if TYPE_CHECKING:
    from memtomem.server.component_factory import Components
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class WarmupOutcome:
    """One component's warmup result — for ``mm warmup`` output and logs."""

    component: str  # "embedder" | "reranker"
    provider: str
    model: str
    status: str  # "loaded" | "already-loaded" | "skipped"


async def _warm_one(
    component: str, provider: str, model: str, holder: object | None
) -> WarmupOutcome:
    if holder is None:
        return WarmupOutcome(component, provider, model, "skipped")
    load_model = getattr(holder, "_get_model", None)
    if not callable(load_model):
        # No lazy local model on this provider (remote / noop) — skip.
        return WarmupOutcome(component, provider, model, "skipped")
    if getattr(holder, "_model", None) is not None:
        return WarmupOutcome(component, provider, model, "already-loaded")
    loop = asyncio.get_running_loop()
    future = loop.run_in_executor(None, load_model)
    # A worker thread can't be interrupted — cancelling only the awaiting
    # task would leave the load running while shutdown closes components
    # under it. ``settle_shielded`` owns the settlement contract (#1803,
    # #1806): repeated cancellation never cancels the queued/running load,
    # the first cancellation (message included) is re-raised once the load
    # settles, and a load failure after cancellation is logged rather than
    # displacing the cancellation.
    await settle_shielded(future, what=f"{component} model load")
    return WarmupOutcome(component, provider, model, "loaded")


async def warm_models(components: Components) -> list[WarmupOutcome]:
    """Force the lazy local model loads (embedder, then pipeline reranker).

    The reranker is read through ``search_pipeline`` rather than held
    separately because web hot-reload swaps ``_reranker`` in place — a
    snapshot taken at init time could warm an instance the pipeline no
    longer uses.

    Raises on load failure — callers decide policy: the lifespan task
    logs-and-continues, ``mm warmup`` surfaces the error.
    """
    cfg = components.config
    return [
        await _warm_one(
            "embedder", cfg.embedding.provider, cfg.embedding.model, components.embedder
        ),
        await _warm_one(
            "reranker",
            cfg.rerank.provider,
            cfg.rerank.model,
            getattr(components.search_pipeline, "_reranker", None),
        ),
    ]


def spawn_warmup(ctx: AppContext) -> asyncio.Task[None]:
    """Fire-and-forget lifespan warmup: full component init, then model loads.

    Failures are logged loudly and never propagate — a warmup problem
    must not take down the MCP server; models simply fall back to lazy
    loading on first use. Cancellation propagates so ``AppContext.close``
    can await the cancelled task during shutdown.
    """

    async def _run() -> None:
        try:
            components = await ctx.ensure_initialized()
            outcomes = await warm_models(components)
            logger.info(
                "Model warmup finished: %s",
                ", ".join(f"{o.component}={o.status}" for o in outcomes),
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "Model warmup failed — models will lazy-load on first use", exc_info=True
            )

    return asyncio.create_task(_run(), name="memtomem-warmup")
