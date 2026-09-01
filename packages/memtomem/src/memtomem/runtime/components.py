"""Shared initialisation factory for the in-process component graph.

Used by the MCP server, the CLI, the web app, and in-process embedders
(:mod:`memtomem.integrations`). Nothing here may import a transport layer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import asyncio
import inspect
import logging

from memtomem.chunking.base import Chunker
from memtomem.chunking.markdown import MarkdownChunker
from memtomem.chunking.registry import ChunkerRegistry
from memtomem.chunking.restructured_text import ReStructuredTextChunker
from memtomem.chunking.structured import StructuredChunker
from memtomem.config import Mem2MemConfig, embedding_policy_fingerprint
from memtomem.embedding.factory import create_embedder
from memtomem.errors import EmbeddingDimensionMismatchError
from memtomem.generation import ComponentGeneration
from memtomem.indexing.engine import IndexEngine
from memtomem.search.pipeline import SearchPipeline
from memtomem.storage.factory import create_storage
from memtomem.storage.sqlite_backend import SqliteBackend

if TYPE_CHECKING:
    from memtomem.embedding.base import EmbeddingProvider
    from memtomem.llm.base import LLMProvider

_log = logging.getLogger(__name__)


async def _close_resource(
    resource: object | None, label: str
) -> tuple[bool, asyncio.CancelledError | None]:
    """Best-effort close used by both startup rollback and normal shutdown.

    Returns ``(closed, cancelled)``: ``closed`` is ``True`` only when the
    close completed without error — callers that gate follow-up work on a
    *confirmed* close (the instance-registry release must not advertise a
    closed store while the sqlite handle may still be open, #1935) branch
    on it. ``cancelled`` carries a :class:`asyncio.CancelledError` caught
    mid-close so teardown orchestrators can defer and re-raise it after
    settlement instead of silently swallowing it (the pre-#1935 behavior
    of the bare ``except BaseException``).
    """
    if resource is None:
        return True, None
    close = getattr(resource, "close", None)
    if not callable(close):
        return True, None
    try:
        result = close()
        if inspect.isawaitable(result):
            await result
        return True, None
    except asyncio.CancelledError as exc:
        _log.warning("Cancelled while closing %s", label)
        return False, exc
    except BaseException:
        _log.warning("Failed to close %s", label, exc_info=True)
        return False, None


@dataclass(frozen=True)
class TeardownResult:
    """Outcome of :func:`close_components`.

    ``storage_closed`` is the load-bearing bit: only a confirmed storage
    close permits releasing the instance-registry sentinel (a failed or
    cancelled storage close retains it — a possibly-open store must stay
    advertised). ``cancelled`` is the first cancellation caught across
    the close sequence, deferred to the caller.
    """

    storage_closed: bool
    cancelled: asyncio.CancelledError | None = None


@dataclass
class Components:
    """Container for initialised core components."""

    config: Mem2MemConfig
    storage: SqliteBackend
    embedder: EmbeddingProvider
    index_engine: IndexEngine
    search_pipeline: SearchPipeline
    llm: LLMProvider | None = None
    # Populated when startup detected a ``chunks_vec`` / provider mismatch
    # (``EmbeddingDimensionMismatchError``) and the server came up in
    # degraded mode instead of crashing. The dict has the same shape as
    # ``SqliteBackend.embedding_mismatch``. See issue #349.
    embedding_broken: dict | None = None
    # Lease handle for the currently published ``(embedder, index_engine,
    # search_pipeline)`` triple, shared with those two components (#2180).
    # ``revert_to_stored`` swaps all three at once and retires this handle;
    # the retired generation is closed on its last lease release. Left unset,
    # it is adopted from the pipeline in ``__post_init__`` — never defaulted
    # to a fresh handle, which would count nobody (see below).
    generation: ComponentGeneration | None = None
    # Generations retired by a swap, kept so shutdown can still close one
    # whose leaseholder never released (or whose deferred close is pending).
    retired_generations: list[ComponentGeneration] = field(default_factory=list)

    def __post_init__(self) -> None:
        # The container, the pipeline and the engine must count into ONE
        # handle (#2180). A hand-assembled ``Components`` — CLI stacks, tests,
        # ``AppContext.from_components`` callers — otherwise pairs two private
        # per-component handles with a third on the container, and a revert
        # reads zero leases on a generation two live components are using and
        # closes the embedder under them. Adopting the pipeline's handle makes
        # the safe wiring the default instead of something every caller has to
        # remember; a mismatch that cannot be repaired is a bug, not a
        # fallback.
        pipeline_generation = getattr(self.search_pipeline, "_generation", None)
        engine_generation = getattr(self.index_engine, "_generation", None)
        if self.generation is None:
            self.generation = pipeline_generation or ComponentGeneration()
        if engine_generation is not None and engine_generation is not self.generation:
            self.index_engine._generation = self.generation
        if pipeline_generation is not None and pipeline_generation is not self.generation:
            self.search_pipeline._generation = self.generation


async def create_components(
    config: Mem2MemConfig | None = None,
    *,
    load_ambient_config: bool = True,
    entity_backfill: bool = True,
) -> Components:
    """Create and initialise all core components.

    ``load_ambient_config=False`` is reserved for callers that have already
    resolved the complete configuration precedence chain.  The default keeps
    the existing server and CLI behaviour of loading ``config.d``, persisted
    overrides, and environment variables before constructing components.

    ``entity_backfill=False`` skips the one-time #2133 entity backfill — for
    callers standing up transient stacks over stores they must not mutate
    (Quality Lab replays). Every durable entry point keeps the default.
    """
    from memtomem.config import load_config_d, load_config_overrides

    config = config or Mem2MemConfig()
    if load_ambient_config:
        load_config_d(config)
        load_config_overrides(config)

    # Initialize FTS tokenizer from config
    from memtomem.storage.fts_tokenizer import set_tokenizer

    if config.search.tokenizer != "unicode61":
        set_tokenizer(config.search.tokenizer)

    storage = create_storage(config)
    embedder: EmbeddingProvider | None = None
    embedding_broken: dict | None = None
    reranker: object | None = None
    llm: LLMProvider | None = None
    search_pipeline: SearchPipeline | None = None
    try:
        embedder = create_embedder(config.embedding)
        await storage.initialize()
    except EmbeddingDimensionMismatchError as mismatch_exc:
        # Stored DB has ``embedding_dimension=0`` (prior NoopEmbedder / BM25
        # install) but the runtime config points at a real provider. Instead
        # of crashing the server — which leaves the user no MCP-level path to
        # repair — come up in degraded mode: the storage is re-opened with
        # ``strict_dim_check=False`` (same seam the ``mm embedding-reset``
        # CLI uses) so the mismatch surfaces as a structured flag and the
        # recovery tool (``mem_embedding_reset``) stays callable over MCP.
        # Vector-dependent tools (``mem_add`` / ``mem_index`` / …) are gated
        # separately via ``_check_embedding_mismatch``. See issue #349.
        storage_closed, cancelled = await _close_resource(storage, "storage")
        if cancelled is not None:
            raise cancelled
        if not storage_closed:
            # Reopening the same store after an unconfirmed close could put two
            # live sqlite handles behind one Components object.  Preserve the
            # initialization failure that caused rollback; cleanup failures
            # have already been logged by _close_resource.
            raise mismatch_exc
        _log.warning(
            "Embedding dimension mismatch detected at startup — entering "
            "degraded mode. Non-vector tools (mem_status, mem_stats, "
            "mem_embedding_reset, mem_list, mem_read) stay available; "
            "vector-dependent tools (mem_add, mem_index, ...) will return "
            "an actionable error until `mem_embedding_reset` is run."
        )
        storage = SqliteBackend(
            config.storage,
            dimension=config.embedding.dimension,
            embedding_provider=config.embedding.provider,
            embedding_model=config.embedding.model,
            embedding_policy_fingerprint=embedding_policy_fingerprint(config.embedding),
            embedding_max_sequence_tokens=config.embedding.max_sequence_tokens,
            strict_dim_check=False,
        )
        try:
            await storage.initialize()
        except BaseException:
            await _close_resource(embedder, "embedder")
            await _close_resource(storage, "storage")
            raise
        embedding_broken = storage.embedding_mismatch
    except BaseException:
        await _close_resource(embedder, "embedder")
        await _close_resource(storage, "storage")
        raise
    assert embedder is not None

    # Model/policy mismatches are non-fatal at schema initialization, but use
    # the same degraded-mode signal as dimension mismatches so watchers and
    # startup soft-sync cannot write mixed-vector data.
    if embedding_broken is None:
        embedding_broken = storage.embedding_mismatch

    try:
        # One-time entity backfill for stores indexed before #2145 (#2133).
        # Before ``SearchPipeline`` exists, so the search cache is born after
        # these writes and there is no invalidation to coordinate. A failure
        # degrades entity coverage, never startup — except cancellation, which
        # the enclosing cleanup path must still see.
        if entity_backfill:
            try:
                from memtomem.tools.entity_backfill import backfill_entities

                await backfill_entities(storage, enabled=config.indexing.extract_entities)
            except asyncio.CancelledError:
                raise
            except Exception:
                _log.warning(
                    "entity backfill failed — continuing startup without it", exc_info=True
                )

        # Build chunker registry with optional code chunkers
        chunkers: list[Chunker] = [
            MarkdownChunker(indexing_config=config.indexing),
            StructuredChunker(indexing_config=config.indexing),
            ReStructuredTextChunker(),
        ]
        try:
            from memtomem.chunking.python_code import PythonChunker

            chunkers.append(PythonChunker())
        except Exception:
            _log.warning(
                "PythonChunker unavailable — install memtomem[all] to enable tree-sitter code chunking",
                exc_info=True,
            )
        try:
            from memtomem.chunking.javascript import JavaScriptChunker

            chunkers.append(JavaScriptChunker())
        except Exception:
            _log.warning(
                "JavaScriptChunker unavailable — install memtomem[all] to enable tree-sitter code chunking",
                exc_info=True,
            )
        registry = ChunkerRegistry(chunkers)

        if config.rerank.enabled:
            from memtomem.search.reranker.factory import create_reranker

            reranker = create_reranker(config.rerank)

        # One shared LLM client serves indexing and search.
        if config.llm.enabled:
            from memtomem.llm.factory import create_llm

            llm = create_llm(config.llm)

        # One lease handle for the whole triple (#2180) — the engine and the
        # pipeline must count into the same generation, or a revert would
        # close the embedder while the other one is still using it.
        generation = ComponentGeneration()

        index_engine = IndexEngine(
            storage=storage,
            embedder=embedder,
            config=config.indexing,
            registry=registry,
            namespace_config=config.namespace,
            progress_threshold=config.embedding.progress_threshold,
            llm=llm,
            generation=generation,
        )

        search_pipeline = SearchPipeline(
            storage=storage,
            embedder=embedder,
            config=config.search,
            decay_config=config.decay,
            mmr_config=config.mmr,
            access_config=config.access,
            reranker=reranker,
            rerank_config=config.rerank,
            expansion_config=config.query_expansion,
            importance_config=config.importance,
            entity_boost_config=config.entity_boost,
            context_window_config=config.context_window,
            llm_provider=llm,
            session_summary_config=config.session_summary,
            generation=generation,
        )

        return Components(
            config=config,
            storage=storage,
            embedder=embedder,
            index_engine=index_engine,
            search_pipeline=search_pipeline,
            llm=llm,
            embedding_broken=embedding_broken,
            generation=generation,
        )
    except BaseException:
        # Once SearchPipeline exists it owns the reranker. Before that point
        # the factory owns and closes a standalone reranker itself.
        await _close_resource(search_pipeline, "search pipeline")
        if search_pipeline is None:
            await _close_resource(reranker, "reranker")
        await _close_resource(llm, "LLM provider")
        await _close_resource(embedder, "embedder")
        await _close_resource(storage, "storage")
        raise


#: Observation passes ``close_components`` gives one retired generation before
#: giving up on it. See the loop at the end of ``close_components``.
_MAX_DRAIN_PASSES = 3


def prune_settled_generations(comp: Components) -> None:
    """Drop retired generations the shutdown drain no longer needs (#2201).

    ``retired_generations`` exists so ``close_components`` can close a
    generation whose last leaseholder never released. An entry that has
    finished closing — the common idle-server revert, which closes inline —
    is dead weight the process would otherwise hold until exit. Mutates in
    place: the list is a field other holders of the same ``Components`` read.
    """

    comp.retired_generations[:] = [g for g in comp.retired_generations if not g.settled]


async def close_components(comp: Components) -> TeardownResult:
    """Shut down every component even when an earlier close fails."""
    first_cancel: asyncio.CancelledError | None = None
    # Retired generations first (#2180): a swap deferred their close to the
    # last lease release, which may never have come (a hung or cancelled
    # leaseholder). Normally this preserves retired-before-live ordering. If
    # shutdown cancellation interrupts a shielded close, the second pass below
    # observes it after the live resources have had a chance to release it.
    for retired in comp.retired_generations:
        cancelled = await retired.drain()
        if first_cancel is None:
            first_cancel = cancelled
    for resource, label in (
        (comp.search_pipeline, "search pipeline"),
        (comp.llm, "LLM provider"),
        (comp.embedder, "embedder"),
    ):
        _, cancelled = await _close_resource(resource, label)
        if first_cancel is None:
            first_cancel = cancelled
    storage_closed, cancelled = await _close_resource(comp.storage, "storage")
    if first_cancel is None:
        first_cancel = cancelled

    # A cancellation delivered during the first drain is returned promptly so
    # teardown can release any live resource the shielded retired close needs.
    # Do not drop that task with the Components container: after the remaining
    # resources are down, settle and observe every retained close before the
    # owner clears its component reference.
    for retired in comp.retired_generations:
        # Bounded, not ``while True``: ``drain`` reports a cancellation
        # *without* settling whenever the shielded close is still running, so
        # under a supervisor that re-delivers cancellation on every ``await``
        # — an expired ``asyncio.timeout``/``wait_for`` wrapped around
        # shutdown — each pass raises immediately and an unbounded retry would
        # spin without ever yielding. A hung leaseholder close has the same
        # shape. Give it a small finite number of observation passes, then
        # leave the close running and let process exit take it, rather than
        # trading a leaked task for a wedged shutdown.
        for _ in range(_MAX_DRAIN_PASSES):
            cancelled = await retired.drain()
            if first_cancel is None:
                first_cancel = cancelled
            if retired.settled:
                break
        else:
            _log.warning(
                "Retired component generation did not settle within %d drain passes; "
                "leaving its close running",
                _MAX_DRAIN_PASSES,
            )
    prune_settled_generations(comp)
    return TeardownResult(storage_closed=storage_closed, cancelled=first_cancel)
