"""LangGraph integration — use memtomem as a memory store in LangGraph agents.

Usage::

    from memtomem.integrations.langgraph import MemtomemStore

    store = MemtomemStore()

    # In a LangGraph node
    async def research_node(state):
        results = await store.search(state["query"])
        return {"context": results}

    async def save_node(state):
        await store.add(state["findings"], tags=["research"])
        return state

Multi-agent usage — bind a session to an agent identity once and let
``search`` / ``add`` derive the namespace automatically::

    await store.start_agent_session("planner")
    await store.add("our cache strategy", tags=["arch"])  # → agent-runtime:planner
    hits = await store.search("cache", include_shared=True)  # → planner + shared

The optional ``MemtomemBaseStore`` adapter implements LangGraph's tuple-
namespace ``BaseStore`` contract. It is imported lazily so the dependency-free
``MemtomemStore`` remains available in minimal installations.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, Self
from uuid import UUID, uuid4

from memtomem.config import classify_scope
from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    SHARED_NAMESPACE,
    SUMMARY_PROVENANCE_MANUAL,
    normalize_bound_agent_id,
    validate_namespace,
)
from memtomem.services.search_service import rrf_weights_from

__all__ = ["MemtomemBaseStore", "MemtomemStore"]

if TYPE_CHECKING:
    from memtomem.integrations.langgraph_store import MemtomemBaseStore
    from memtomem.runtime.components import Components


def __getattr__(name: str) -> Any:
    """Lazily expose adapters that require optional dependencies."""
    if name == "MemtomemBaseStore":
        from memtomem.integrations.langgraph_store import MemtomemBaseStore

        return MemtomemBaseStore
    raise AttributeError(name)


class _UnregisteredProjectTarget(Exception):
    """A target names a project tree that no config entry covers.

    Raised rather than returned so the refusal cannot be dropped. An optional
    return would let a caller that ignores it fall through as ``user`` tier —
    which is the exact failure ADR-0011 §5 exists to prevent, and the one this
    module was fixed for (#2321).
    """


class _TierWouldNotRoundTrip(Exception):
    """The tier the gates would use is not the tier the indexer would store.

    The gates need to know which tier they are protecting; the indexer decides,
    independently, which tier the resulting rows are labelled with. When those
    two answers differ the write is unsafe in one direction or the other, and
    which direction does not matter enough to write it anyway:

    * gated ``project_shared`` / stored ``project_local`` — the write is
      protected, but the row is not, and ``mem_edit`` / ``mem_delete`` read the
      tier from the row. A note nobody could add without consent becomes one
      anybody can rewrite without it.
    * gated ``user`` / stored ``project_shared`` — the row lands in the
      git-tracked tier having passed neither gate, which is #2321 itself,
      reached through a different door.

    Refusing is the honest answer, because making the two agree means changing
    the classifier every read surface shares — a decision for the classifier,
    not for this adapter.
    """


def _owned_tier(
    target: Path,
    memory_dirs: list,
    project_memory_dirs: list,
) -> str:
    """Which tier *owns* ``target``, by configured directory rather than spelling.

    ``classify_scope`` decides from the path's shape, and a path can be shaped
    to defeat it in both directions — see :func:`_resolve_target_scope`, which
    is what callers should use. This half answers only the ownership question.
    """
    # 1. A registered project root that covers the target decides the tier.
    #    Most specific root wins, so a registered ``memories.local`` nested
    #    under a registered ``memories`` is still read as its own tier. Two
    #    roots can only both cover the target by being ancestors of it, and so
    #    of each other, which makes the path-component count a total order
    #    over exactly the roots in contention.
    covering: list[tuple[int, str]] = []
    for raw in project_memory_dirs or ():
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:  # pragma: no cover - unreadable configured root
            continue
        if target == root or target.is_relative_to(root):
            root_tier, _ = classify_scope(root, None)
            # A registered root whose own path is not canonical cannot say
            # which tier it is, so it does not get to decide. It is not
            # ignored either: the round-trip check in the caller refuses the
            # write if the indexer reads the target differently, which is the
            # case that would otherwise slip past both gates.
            if root_tier != "user":
                covering.append((len(root.parts), root_tier))
    if covering:
        covering.sort()
        return covering[-1][1]

    # 2. A configured user memory dir covering the target makes it user-tier
    #    whatever the path happens to be spelled like. This runs *after* the
    #    project roots on purpose: the default user memory directory is
    #    ``~/.memtomem/memories``, so the two loops overlap by design and the
    #    more specific claim has to be asked first.
    for raw in memory_dirs or ():
        try:
            base = Path(raw).expanduser().resolve()
        except OSError:  # pragma: no cover - unreadable configured dir
            continue
        if target == base or target.is_relative_to(base):
            return "user"

    # 3. Covered by nothing, but shaped like a project canonical path: an
    #    unregistered project tree. Writing there would stamp user-tier rows
    #    onto a git-tracked path with both gates bypassed.
    pattern_scope, _ = classify_scope(target, None)
    if pattern_scope != "user":
        raise _UnregisteredProjectTarget(
            f"{target} is a project-canonical path, but no "
            "indexing.project_memory_dirs entry covers it. Register the tier "
            "or choose a target inside a configured memory directory."
        )

    return "user"


def _resolve_target_scope(
    target: Path,
    memory_dirs: list,
    project_memory_dirs: list,
) -> str:
    """The ADR-0011 tier to gate ``target`` on, or a refusal.

    Two questions have to give the same answer before anything is written:
    which tier *owns* the target (:func:`_owned_tier`, by configured
    directory), and which tier the indexer will *label its rows* with
    (``classify_scope``, by path shape). The gates protect the first; every
    later surface — ``mem_edit``, ``mem_delete`` — trusts the second. A write
    whose two answers differ is one this adapter declines.

    Raises :class:`_UnregisteredProjectTarget` or
    :class:`_TierWouldNotRoundTrip`; never returns a tier it is unsure of.
    """
    owned = _owned_tier(target, memory_dirs, project_memory_dirs)
    indexed, _ = classify_scope(target, project_memory_dirs)
    if owned != indexed:
        raise _TierWouldNotRoundTrip(
            f"{target} would be gated as {owned!r} but stored as {indexed!r}, so "
            "the gates and every later edit or delete would disagree about which "
            "tier it is in. Choose a target that is not nested inside another "
            ".memtomem directory, and register project tiers at their canonical "
            "memories or memories.local directory."
        )
    return owned


def _gate_scope_for(comp: "Components", target: Path) -> str | dict:
    """``_resolve_target_scope`` with this adapter's error-dict contract.

    Both destinations — a caller's ``file=`` and the derived day file — go
    through it, so neither can be given a tier the other would not be.
    """
    try:
        return _resolve_target_scope(
            target,
            comp.config.indexing.memory_dirs,
            comp.config.indexing.project_memory_dirs,
        )
    except _UnregisteredProjectTarget as exc:
        return {"error": "unregistered_project_target", "detail": str(exc)}
    except _TierWouldNotRoundTrip as exc:
        return {"error": "tier_would_not_round_trip", "detail": str(exc)}


class MemtomemStore:
    """LangGraph-compatible memory store wrapping memtomem components.

    Provides a simple async API for search, add, sessions, and working memory.
    Components are lazily initialized on first use.

    Args:
        config_overrides: Optional dict of config overrides
            (e.g. ``{"storage": {"sqlite_path": "..."}}``). Sections and
            keys that do not exist on :class:`Mem2MemConfig` raise
            ``ValueError`` from :meth:`_ensure_init` so a typo cannot
            silently land writes/index calls in the default
            ``~/.memtomem`` location. The constructor itself does not
            validate (the config object is built lazily) — the first
            ``await``-ed call surfaces the error.
    """

    def __init__(self, config_overrides: dict[str, Any] | None = None):
        self._components: Components | None = None
        self._config_overrides = config_overrides or {}
        self._current_session_id: str | None = None
        self._current_agent_id: str | None = None
        self._session_lock: asyncio.Lock = asyncio.Lock()

    async def _ensure_init(self) -> Components:
        """Initialize components on first call; return the cached instance."""
        if self._components is None:
            from memtomem.config import (
                Mem2MemConfig,
                load_config_d,
                load_config_overrides,
            )
            from memtomem.runtime.components import create_components

            config = Mem2MemConfig()
            load_config_d(config)
            load_config_overrides(config)

            # Apply programmatic overrides after ambient config. Unknown
            # sections / keys raise immediately:
            # ``config_overrides`` is a programmatic constructor argument, so
            # a typo like ``{"storge": ...}`` or ``{"storage":
            # {"sqlite_pat": ...}}`` would otherwise fall back to the default
            # DB / memory_dirs and silently land writes in the wrong place.
            # Logging-only would be hidden by callers who don't surface
            # ``WARNING`` from ``memtomem.integrations.langgraph``.
            for section, updates in self._config_overrides.items():
                section_obj = getattr(config, section, None)
                if section_obj is None:
                    raise ValueError(
                        f"MemtomemStore.config_overrides: unknown section {section!r}. "
                        "Section must match a Mem2MemConfig section "
                        "(e.g. 'storage', 'indexing', 'embedding')."
                    )
                if not isinstance(updates, dict):
                    raise ValueError(
                        f"MemtomemStore.config_overrides: section {section!r} value "
                        f"is {type(updates).__name__}, expected dict."
                    )
                for key, value in updates.items():
                    if not hasattr(section_obj, key):
                        raise ValueError(
                            f"MemtomemStore.config_overrides: unknown key {key!r} "
                            f"in section {section!r}."
                        )
                    setattr(section_obj, key, value)

            # Ambient config was resolved above so the constructor arguments
            # remain the final, highest-precedence layer. Loading it again in
            # the factory would overwrite an isolated sqlite_path or
            # memory_dirs with ~/.memtomem settings.
            self._components = await create_components(config, load_ambient_config=False)
        return self._components

    async def close(self) -> None:
        """Close all components and release resources."""
        if self._components:
            from memtomem.runtime.components import close_components

            await close_components(self._components)
            self._components = None

    # ── Search ────────────────────────────────────────────────────────────

    async def search(
        self,
        query: str,
        top_k: int = 10,
        namespace: str | None = None,
        source_filter: str | None = None,
        tag_filter: str | None = None,
        bm25_weight: float | None = None,
        dense_weight: float | None = None,
        include_shared: bool | None = None,
    ) -> list[dict]:
        """Search indexed memories.

        Returns list of dicts with keys: id, content, score, source, tags, namespace.

        ``bm25_weight`` / ``dense_weight`` must be finite and non-negative,
        and not both zero; anything else raises ``InvalidRrfWeightError``
        (a ``ValueError``) before initialization, in keeping with this class
        treating a malformed request as a programming error rather than
        degrading it. ``0.0`` disables that leg — its retrieval is skipped
        and none of its candidates appear (#2092).

        ``include_shared`` is the multi-agent semantic toggle. State table:

        ============== ===================== =======================================
        ``include_shared`` ``_current_agent_id``  Resulting ``namespace`` filter
        ============== ===================== =======================================
        ``None`` (auto) set ("planner")        ``"agent-runtime:planner,shared"``
        ``None`` (auto) unset                  caller's ``namespace=`` (legacy)
        ``True``        set ("planner")        ``"agent-runtime:planner,shared"``
        ``True``        unset                  raises ``ValueError``
        ``False``       set ("planner")        ``"agent-runtime:planner"`` (no shared)
        ``False``       unset                  caller's ``namespace=``
        ============== ===================== =======================================

        ``True`` + no agent session is treated as a programming error
        (the caller asked to include the *shared* slice of an agent's
        view but never bound an agent) — raised explicitly so the bug
        surfaces immediately rather than degrading to a silent
        un-pinned search.
        """
        # Refuse an unusable weight before paying for initialization, so a
        # startup failure cannot mask the actionable error.
        rrf_weights = rrf_weights_from(bm25_weight, dense_weight)
        comp = await self._ensure_init()

        effective_namespace = self._resolve_search_namespace(namespace, include_shared)

        # ADR-0011 PR-D round 9: thread project context — LangGraph
        # agents running inside a registered project should still see
        # the project's tier rows under the always-on scope filter.
        from memtomem.runtime.project_context import _resolve_project_context_root

        project_context_root = _resolve_project_context_root(comp)

        results, stats = await comp.search_pipeline.search(
            query=query,
            top_k=top_k,
            namespace=effective_namespace,
            source_filter=source_filter,
            tag_filter=tag_filter,
            rrf_weights=rrf_weights,
            project_context_root=project_context_root,
            origin="langgraph",
        )
        return [
            {
                "id": str(r.chunk.id),
                "content": r.chunk.content,
                "score": r.score,
                "source": str(r.chunk.metadata.source_file),
                "tags": list(r.chunk.metadata.tags),
                "namespace": r.chunk.metadata.namespace,
                "rank": r.rank,
            }
            for r in results
        ]

    def _resolve_search_namespace(
        self, namespace: str | None, include_shared: bool | None
    ) -> str | None:
        """Translate ``include_shared`` + bound agent into a namespace filter.

        Public contract is documented in ``search``'s docstring; this helper
        only encodes the lookup table so it can be unit-tested without
        spinning up components.

        ``self._current_agent_id`` is concatenated into ``AGENT_NAMESPACE_PREFIX``
        without re-validation here: ``start_agent_session`` is the sole writer
        of that field and runs ``normalize_bound_agent_id`` before binding, so
        any value that reaches this point is already gate-checked — and is
        never the reserved ``"default"``, which binds ``None`` instead (#1875).
        """

        if include_shared is True and self._current_agent_id is None:
            raise ValueError(
                "include_shared=True requires an active agent session. "
                "Call start_agent_session(agent_id) first or set include_shared=False."
            )
        if include_shared is False and self._current_agent_id is not None:
            return f"{AGENT_NAMESPACE_PREFIX}{self._current_agent_id}"
        if include_shared in (None, True) and self._current_agent_id is not None:
            return f"{AGENT_NAMESPACE_PREFIX}{self._current_agent_id},{SHARED_NAMESPACE}"
        # No agent bound and the caller did not force include_shared=True →
        # fall back to whatever the caller passed (legacy behaviour).
        return namespace

    def _resolve_add_namespace(self, namespace: str | None) -> str | None:
        """Default the ``add`` namespace to the bound agent's private bucket.

        If the caller passes an explicit ``namespace`` it wins (escape hatch
        for "I want to write to ``shared`` while my session is bound to
        ``planner``"). Otherwise, when an agent session is active, writes
        land in ``agent-runtime:<id>``.

        ``self._current_agent_id`` reaches the concat path pre-validated —
        ``start_agent_session`` is the only writer and runs
        ``normalize_bound_agent_id`` before binding (same invariant as
        ``_resolve_search_namespace``).
        """

        if namespace is not None:
            return namespace
        if self._current_agent_id is not None:
            return f"{AGENT_NAMESPACE_PREFIX}{self._current_agent_id}"
        return None

    # ── CRUD ──────────────────────────────────────────────────────────────

    async def add(
        self,
        content: str,
        title: str | None = None,
        tags: list[str] | None = None,
        file: str | None = None,
        namespace: str | None = None,
        template: str | None = None,
        force_unsafe: bool = False,
        confirm_project_shared: bool = False,
    ) -> dict:
        """Add a memory entry. Returns dict with file path and chunk count.

        When an agent session is active (``start_agent_session`` was
        called), ``namespace=None`` defaults to the agent's private
        ``agent-runtime:<id>`` bucket. Pass an explicit ``namespace=`` to
        override (e.g. ``"shared"``).

        Content passes through the trust-boundary redaction guard before
        any filesystem write. On a hit the call returns ``{"error":
        "redaction_blocked", "hits": N}`` instead of writing; pass
        ``force_unsafe=True`` to bypass with audit logging.

        ADR-0011 §5 (#2321): an explicit ``file=`` chooses the destination
        tier, so the tier is derived from the resolved target *before*
        either gate runs. A target under a registered ``project_shared``
        directory is git-tracked and needs ``confirm_project_shared=True``
        (Gate B); the same tier reaches the redaction guard (Gate A), where
        ``force_unsafe=True`` is hard-refused because git history cannot be
        retracted from clones.

        Args:
            confirm_project_shared: Required when the resolved ``file=``
                target lives in a registered ``project_shared`` tier. That
                write is what the project commits and everyone who pulls it
                reads, so it takes the same explicit consent ``mem_add``
                takes for the same destination.
        """
        comp = await self._ensure_init()
        from datetime import datetime, timezone

        from memtomem import privacy
        from memtomem.tools.memory_writer import append_entry

        # Apply template
        if template:
            from memtomem.templates import render_template

            content = render_template(template, content, title=title)

        effective_namespace = self._resolve_add_namespace(namespace)
        default_ns = comp.config.namespace.default_namespace

        # ADR-0011 §5 (#2321): resolve the target before scanning the content.
        # The guard used to run twelve lines above this block, which left both
        # gates unreachable on the one path that can reach the shared tier — a
        # scan cannot know the destination tier while the destination is still
        # unchosen.
        if file:
            target = Path(file).expanduser().resolve()
            resolved = _gate_scope_for(comp, target)
            if isinstance(resolved, dict):
                return resolved
            effective_scope = resolved
        else:
            from memtomem.errors import ConfigError
            from memtomem.memory_scope import day_file_name, require_user_base

            try:
                base = require_user_base(comp.config.indexing.memory_dirs)
            except ConfigError as exc:
                return {"error": str(exc)}
            date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            # Issue #2005: one day file per namespace. The adapter resolves
            # its namespace before choosing the file (no session can change
            # it mid-write here), so no in-lock re-target is needed.
            target = base / day_file_name(effective_namespace, default_ns, date_str=date_str)
            # Resolved the same way an explicit target is, not assumed.
            # ``memory_dirs`` and ``project_memory_dirs`` may name the same
            # directory, and when they do this day file really is in the
            # git-tracked tier — so both gates have to see that, exactly as
            # they would for a caller-supplied path. #2322 covers four *other*
            # derived-target writers and says in its own text that the
            # caller-controlled case belongs here, so there was nothing to
            # defer this branch to.
            resolved = _gate_scope_for(comp, target)
            if isinstance(resolved, dict):
                return resolved
            effective_scope = resolved

        # ADR-0011 §5 Gate B, ordered ahead of Gate A to match
        # ``_mem_add_core``: a caller who never consented to a git-tracked
        # write should be told that, not that their content looked like a
        # secret. It applies to every destination this method writes to, not
        # only a caller-supplied one — a derived day file that lands in the
        # tracked tier is the same write with the destination chosen for you.
        if effective_scope == "project_shared" and not confirm_project_shared:
            return {
                "error": "project_shared_confirmation_required",
                "detail": (
                    f"{target} resolves into a registered project_shared tier, "
                    "which is git-tracked; pass confirm_project_shared=True to "
                    "proceed."
                ),
            }
        # Mirrors the gate's predicate rather than falling through it: an
        # ``!= "user"`` widening here would file a consent for project_local,
        # a tier nobody was asked about. The AST guard in
        # ``test_project_shared_confirmation_audit_guard.py`` cannot check
        # predicate equivalence — the surface tests carry that half.
        if effective_scope == "project_shared":
            privacy.emit_project_shared_confirmation(
                surface="langgraph_add",
                mechanism="param",
                action="write",
                audit_context={"namespace": effective_namespace},
            )

        # ADR-0011 §5 Gate A, now told which tier it is scanning for.
        guard = privacy.enforce_write_guard(
            content,
            surface="langgraph_add",
            force_unsafe=force_unsafe,
            scope=effective_scope,
            audit_context={
                "namespace": namespace,
                "file": file,
                "scope": effective_scope,
            },
        )
        if guard.decision == "blocked":
            return {
                "error": "redaction_blocked",
                "hits": len(guard.hits),
                "surface": "langgraph_add",
            }
        # Separate from ``blocked`` because the remedy differs: this one is
        # never retryable with ``force_unsafe``. Handling it is also what keeps
        # threading ``scope=`` from becoming a regression — the lone
        # ``== "blocked"`` test this replaced would let the hard refusal fall
        # through as a pass and write the secret into git.
        if guard.decision == "blocked_project_shared":
            return {
                "error": "redaction_blocked_project_shared",
                "hits": len(guard.hits),
                "surface": "langgraph_add",
            }

        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            memory_lock_path,
            async_file_lock,
        )
        from memtomem.memory_scope import namespace_mix_refusal

        # Issue #2005: refuse rather than let re-chunking restamp the existing
        # entries' namespace. Guard, append and re-index share one lock span —
        # a guard that inspects a file another writer may change before the
        # append is decoration. This adapter previously took no lock at all;
        # adding it here is what makes the guard mean something.
        try:
            async with async_file_lock(
                memory_lock_path(target), timeout=_CRUD_SIDECAR_LOCK_BUDGET_S
            ):
                mix_err = await namespace_mix_refusal(
                    index_engine=comp.index_engine,
                    storage=comp.storage,
                    default_namespace=default_ns,
                    target=target,
                    effective_ns=effective_namespace,
                    override_hint="a different target file",
                )
                if mix_err is not None:
                    return {"error": "namespace_mix_refused", "detail": mix_err}

                append_entry(target, content, title=title, tags=tags)
                # Guarded above (``enforce_write_guard``); skip the engine gate
                # (ADR-0006 PR-A).
                stats = await comp.index_engine.index_file(
                    target, namespace=effective_namespace, already_scanned=True, lock_held=True
                )
        except TimeoutError:
            return {"error": "locked", "detail": f"{target} is locked by another process; retry."}

        # #2141: the store holds one ``Components`` — and one warmed search
        # cache — for its whole lifetime, so an un-invalidated write stays
        # invisible to ``search`` for up to ``search.cache_ttl``.
        if stats.mutated:
            comp.search_pipeline.invalidate_cache()

        return {
            "file": str(target),
            "indexed_chunks": stats.indexed_chunks,
        }

    async def get(self, chunk_id: str) -> dict | None:
        """Get a chunk by UUID, inside the caller's project boundary.

        ``None`` covers both "no such chunk" and "belongs to another project"
        (ADR-0011 §6, ADR-0036) — the same rule ``search`` on this adapter
        already applies, and the same answer ``mem_read`` gives.
        """
        from memtomem.runtime.project_context import _resolve_project_context_root
        from memtomem.search.visibility import resolve_visible_chunk

        comp = await self._ensure_init()
        chunk = await resolve_visible_chunk(
            comp.storage,
            UUID(chunk_id),
            project_context_root=_resolve_project_context_root(comp),
        )
        if chunk is None:
            return None
        return {
            "id": str(chunk.id),
            "content": chunk.content,
            "source": str(chunk.metadata.source_file),
            "tags": list(chunk.metadata.tags),
            "namespace": chunk.metadata.namespace,
        }

    async def delete(self, chunk_id: str) -> bool:
        """Delete a chunk by UUID, inside the caller's project boundary.

        Returns ``False`` for an out-of-boundary id, exactly as for one that
        does not exist. A caller who cannot read a chunk cannot delete it
        either (ADR-0036).
        """
        comp = await self._ensure_init()
        if await self.get(chunk_id) is None:
            return False
        deleted = await comp.storage.delete_chunks([UUID(chunk_id)])
        if deleted:
            comp.search_pipeline.invalidate_cache()
        return deleted > 0

    # ── Sessions (Episodic Memory) ────────────────────────────────────────

    async def start_session(self, agent_id: str = "default", namespace: str | None = None) -> str:
        """Start an episodic memory session. Returns session_id.

        Low-level escape hatch — for multi-agent scenarios prefer
        :meth:`start_agent_session`, which derives the namespace from
        ``agent-runtime:<id>`` and binds ``_current_agent_id`` so
        :meth:`search` / :meth:`add` can default to the agent scope.

        ``agent_id`` is **not** run through ``validate_agent_id`` here:
        this method does not concatenate it into ``AGENT_NAMESPACE_PREFIX``,
        so a malformed value cannot produce an ``"agent-runtime:foo:bar"``
        namespace string. The id still lands in the sessions row as
        metadata; downstream code that reads it back must not feed it
        into a namespace concat without validating first. New paths that
        derive a namespace from ``agent_id`` should use
        :meth:`start_agent_session` (or call ``validate_agent_id``
        directly) so the gate isn't reintroduced as a regression.

        ``namespace`` *is* run through :func:`validate_namespace` because
        an explicit override lands verbatim in the session row — without
        the gate a Python caller could write ``"agent-runtime:foo:bar"``
        through this entry point even though the equivalent
        ``start_agent_session`` path now refuses it (issue #496).
        """
        comp = await self._ensure_init()
        if namespace is not None:
            validate_namespace(namespace)
        session_id = str(uuid4())
        ns = namespace or "default"
        await comp.storage.create_session(session_id, agent_id, ns)
        async with self._session_lock:
            self._current_session_id = session_id
            # Agent binding follows the session lifecycle: replacing an
            # agent-bound session with a low-level one must not leave
            # ``add`` / ``search`` defaulting to the previous agent's
            # ``agent-runtime:<id>`` scope while events log to the new
            # session (same reset contract as ``end_session``).
            self._current_agent_id = None
        return session_id

    async def start_agent_session(
        self,
        agent_id: str,
        *,
        namespace: str | None = None,
    ) -> str:
        """Start a multi-agent-aware episodic memory session.

        Derives the namespace from ``agent-runtime:<agent_id>`` (override
        with explicit ``namespace=``), records the session in storage, and
        binds ``_current_agent_id`` so subsequent ``search`` /
        ``add`` calls inherit the agent scope without the caller passing
        ``namespace=`` on every call.

        Passing the reserved ``agent_id="default"`` starts an *unbound*
        session instead: the row namespace stays ``"default"`` and
        ``_current_agent_id`` stays ``None``, so ``add`` / ``search``
        behave as they do with no agent session. Mirrors the MCP
        ``mem_session_start`` surface (#1875); prefer :meth:`start_session`
        when that is what you meant.

        Returns the session id.

        Raises:
            InvalidNameError: ``agent_id`` is empty, contains ``:``, ``/``,
                ``..``, whitespace, control characters, or anything outside
                ``[A-Za-z0-9._-]`` — the same gate the MCP / CLI session
                surfaces apply (see ``memtomem.constants.validate_agent_id``).
                This blocks malformed values from concatenating into
                ``agent-runtime:<agent_id>`` and round-tripping into
                storage as ``"agent-runtime:foo:bar"``.

                Or ``namespace`` is supplied with a malformed value (see
                ``memtomem.constants.validate_namespace``). The override is
                an escape hatch but not a bypass: a Python caller cannot
                land ``"agent-runtime:foo:bar"`` in the session row even
                though ``agent_id`` itself was clean (issue #496 — closes
                the kin gap to the ``agent_id`` work in #486 / #492).
        """
        # Validate-then-normalize: malformed ids still raise, while the
        # reserved "default" collapses to an unbound session (#1875), the
        # same rule the MCP ``mem_session_start`` surface applies. Without
        # it this method would bind ``agent-runtime:default`` and route
        # every subsequent ``add`` into a hidden system namespace.
        #
        # ``required=True``: ``agent_id`` is a mandatory positional here,
        # unlike the MCP surface where omitting it is the documented way to
        # start an unbound session. Without it a ``None`` would read as
        # "nothing to bind" and land in the NOT NULL ``sessions.agent_id``
        # column as a backend IntegrityError instead of the
        # InvalidNameError callers have always gotten.
        bound_agent_id = normalize_bound_agent_id(agent_id, required=True)
        if namespace is not None:
            validate_namespace(namespace)

        comp = await self._ensure_init()
        session_id = str(uuid4())
        if namespace:
            ns = namespace
        elif bound_agent_id:
            ns = f"{AGENT_NAMESPACE_PREFIX}{bound_agent_id}"
        else:
            ns = "default"
        # The row keeps the literal; only the runtime binding is None.
        await comp.storage.create_session(session_id, agent_id, ns)
        async with self._session_lock:
            self._current_session_id = session_id
            self._current_agent_id = bound_agent_id
        return session_id

    async def end_session(self, summary: str | None = None) -> dict:
        """End the current session. Returns session stats.

        Resets both ``_current_session_id`` and ``_current_agent_id``,
        so subsequent ``search(include_shared=True)`` calls without a
        new ``start_agent_session`` will raise.
        """
        comp = await self._ensure_init()
        if not self._current_session_id:
            return {"error": "no active session"}

        events = await comp.storage.get_session_events(self._current_session_id)
        event_counts: dict[str, int] = {}
        for e in events:
            event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

        end_metadata: dict = {"event_counts": event_counts}
        # A summary passed here is caller-supplied, not the server's
        # write-provenance selection — recorded as ``manual``. Ending with no
        # summary leaves the origin absent (unknown), never a bare marker.
        if summary:
            end_metadata["summary_provenance"] = SUMMARY_PROVENANCE_MANUAL
        await comp.storage.end_session(
            self._current_session_id,
            summary,
            end_metadata,
        )
        await comp.storage.scratch_cleanup(session_id=self._current_session_id)

        sid = self._current_session_id
        async with self._session_lock:
            self._current_session_id = None
            self._current_agent_id = None
        return {"session_id": sid, "events": len(events), "event_counts": event_counts}

    async def log_event(
        self, event_type: str, content: str, chunk_ids: list[str] | None = None
    ) -> None:
        """Log an event to the current session."""
        if not self._current_session_id:
            return
        comp = await self._ensure_init()
        await comp.storage.add_session_event(
            self._current_session_id,
            event_type,
            content,
            chunk_ids,
        )

    # ── Working Memory ────────────────────────────────────────────────────

    async def scratch_set(self, key: str, value: str, ttl_minutes: int | None = None) -> None:
        """Store a value in working memory."""
        comp = await self._ensure_init()
        from datetime import datetime, timedelta, timezone

        expires_at = None
        if ttl_minutes:
            expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl_minutes)).isoformat(
                timespec="seconds"
            )
        await comp.storage.scratch_set(
            key, value, session_id=self._current_session_id, expires_at=expires_at
        )

    async def scratch_get(self, key: str) -> str | None:
        """Get a value from working memory."""
        comp = await self._ensure_init()
        entry = await comp.storage.scratch_get(key)
        return entry["value"] if entry else None

    async def scratch_list(self) -> list[dict]:
        """List all working memory entries."""
        comp = await self._ensure_init()
        return await comp.storage.scratch_list(session_id=self._current_session_id)

    # ── Index ─────────────────────────────────────────────────────────────

    async def index(
        self, path: str = ".", recursive: bool = True, namespace: str | None = None
    ) -> dict:
        """Index files and return indexing statistics for search.

        ``retryable_errors`` is the same-string subset of ``errors`` whose
        cause may be resolved by retrying the operation.
        """
        comp = await self._ensure_init()
        stats = await comp.index_engine.index_path(
            Path(path).expanduser().resolve(),
            recursive=recursive,
            namespace=namespace,
        )
        if stats.mutated:
            comp.search_pipeline.invalidate_cache()
        return {
            "total_files": stats.total_files,
            "indexed_chunks": stats.indexed_chunks,
            "duration_ms": stats.duration_ms,
            "blocked_files": stats.blocked_files,
            "blocked_paths": list(stats.blocked_paths),
            "errors": list(stats.errors),
            "retryable_errors": list(stats.retryable_errors),
        }

    # ── Context Manager ───────────────────────────────────────────────────

    async def __aenter__(self) -> Self:
        await self._ensure_init()
        return self

    async def __aexit__(self, *args: object) -> None:
        await self.close()
