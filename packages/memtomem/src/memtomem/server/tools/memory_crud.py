"""Tools: mem_add, mem_edit, mem_delete, mem_batch_add."""

from __future__ import annotations

import asyncio
import logging
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import TYPE_CHECKING, cast
from uuid import UUID

from memtomem.config import TargetScope
from memtomem.server import mcp
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _announce_dim_mismatch_once, _check_embedding_mismatch
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import (
    capture_session_and_namespace,
    mark_provenance_incomplete,
    record_write_provenance,
)
from memtomem.server.validation import MAX_CONTENT_LENGTH, MAX_IDEMPOTENCY_KEY_LENGTH
from memtomem.server.webhooks import webhook_error_cb

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable

    from memtomem.models import Chunk, IndexingStats

logger = logging.getLogger(__name__)

# Bound on how many times ``_locked_chunk`` re-keys onto a moved file before
# giving up. A migrate (``mm context memory-migrate``) that races the lock a
# few times is transient; a chunk that keeps moving returns a retryable error
# instead of spinning.
_CHUNK_LOCK_MOVE_RETRIES = 3

# Appended to a result string returned from the idempotency ledger (issue
# #1573) so a replayed keyed write is distinguishable from the original —
# nothing machine-parses these strings, but the marker stops an LLM caller
# from concluding a second physical write happened.
_REPLAY_MARKER = "\n\n[idempotent replay] Result returned from ledger; no new write was performed."


def _idempotency_in_progress_error(key: str) -> str:
    """Retryable message when a concurrent call already holds the claim.

    Only reachable when two same-key calls race to *different* targets (a
    same-target race is serialized by the per-file lock, so the second sees a
    completed claim and replays). A sequential retry after this resolves once
    the in-flight write completes.
    """
    return (
        f"Error: a memory write with idempotency_key '{key}' is already in progress; retry shortly."
    )


def _validate_idempotency_key(key: str) -> str | None:
    """Return an ``Error: ...`` string for an invalid idempotency key, else None.

    Over-long keys are rejected loudly rather than truncated: truncation could
    collide two distinct keys and silently drop a write, which is strictly
    worse than an error the caller can see and fix.
    """
    if not key.strip():
        return "Error: idempotency_key must be non-empty."
    if len(key) > MAX_IDEMPOTENCY_KEY_LENGTH:
        return (
            f"Error: idempotency_key too long "
            f"(max {MAX_IDEMPOTENCY_KEY_LENGTH} chars, got {len(key)})."
        )
    return None


async def _release_idempotency_claim(app: AppContext, tool: str, key: str) -> None:
    """Best-effort release of a won-but-failed claim so the key stays re-runnable.

    A release failure must not mask the write error that triggered it, so it is
    swallowed and logged.
    """
    try:
        await app.storage.idempotency_release(tool, key)
    except Exception:
        logger.warning("idempotency claim release failed for %s", tool, exc_info=True)


@asynccontextmanager
async def _locked_chunk(
    app: AppContext, uid: UUID, chunk_id: str
) -> AsyncIterator[tuple[Chunk | None, str | None]]:
    """Yield ``(chunk, None)`` with the source file's L1+L2 locks held and the
    chunk re-fetched fresh under them, or ``(None, error_message)``.

    Three-step acquire (issues #1570, #1587):

    1. Fetch the chunk *unlocked* to learn its ``source_file`` — the lock key,
       which we can only get by reading the chunk.
    2. Acquire that file's in-process per-file lock (L1,
       ``get_memory_file_lock``) *and* its cross-process sidecar (L2,
       ``async_file_lock``). L2 is held for the whole span, so a second MCP
       server, the CLI, or ``memory-migrate`` cannot mutate or move the file
       under us — this is what closes the cross-process hole #1570 left open.
    3. Re-fetch *under both locks* so ``start_line`` / ``end_line`` reflect any
       CRUD write that committed while we waited (chunk UUIDs are stable across
       incremental re-index, ADR-0005 and #1788).

    If ``memory-migrate`` grabbed L2 first and moved the file between the
    unlocked fetch and our re-fetch, the fresh chunk's ``source_file`` differs;
    re-key onto the new path and retry, bounded by ``_CHUNK_LOCK_MOVE_RETRIES``.
    A sidecar acquire that times out (another process holds it past the budget)
    surfaces a retryable error instead of blocking. Exactly one ``yield`` runs
    on every path so the ``@asynccontextmanager`` protocol holds.
    """
    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        _lock_path_for,
        async_file_lock,
    )

    chunk = await app.storage.get_chunk(uid)
    if chunk is None:
        yield None, f"Error: chunk {chunk_id} not found."
        return
    source_file = chunk.metadata.source_file
    for _ in range(_CHUNK_LOCK_MOVE_RETRIES):
        key = AppContext.memory_file_lock_key(source_file)
        sidecar = _lock_path_for(source_file.expanduser().resolve())
        try:
            async with app.get_memory_file_lock(key):
                async with async_file_lock(sidecar, timeout=_CRUD_SIDECAR_LOCK_BUDGET_S):
                    fresh = await app.storage.get_chunk(uid)
                    if fresh is None:
                        yield None, f"Error: chunk {chunk_id} not found."
                        return
                    if AppContext.memory_file_lock_key(fresh.metadata.source_file) == key:
                        yield fresh, None
                        return
                    # Moved out from under us (migrate re-scoped the chunk before
                    # we took L2): re-key onto the new path and retry there.
                    source_file = fresh.metadata.source_file
        except TimeoutError:
            yield (
                None,
                (
                    f"Error: chunk {chunk_id} source file is locked by another process "
                    "(migration in flight?); retry."
                ),
            )
            return
    yield None, f"Error: chunk {chunk_id} source file is being moved concurrently; retry."


async def _flag_mutation_on_active_session(app: AppContext) -> None:
    """Tell the active session that a mutation happened inside it.

    ``mem_edit`` and ``mem_delete`` record no provenance event. Their
    ``new_chunk_ids`` are re-chunk artifacts, not new material: feeding
    them to a session summary would describe a rewrite as something newly
    written, while the ids an earlier write recorded for the same file
    have just gone dangling.

    Staying silent is the one option that is not available. An edit-only
    session would then carry the provenance marker, report zero writes,
    and leave no dangling id for a consumer to trip over — so "this
    session wrote nothing" would read as fact rather than as a gap. The
    flag makes the gap visible without pretending to describe it.

    No write gauge here on purpose: this writes no session *event*, and
    the flag lands correctly even on an already-ended row, so there is
    nothing for session teardown to snapshot around.
    """
    async with app._session_lock:
        session_id = app.current_session_id
    await mark_provenance_incomplete(app, session_id)


async def _mutate_file_and_reindex(
    app: AppContext,
    source_file: Path,
    mutate: Callable[[], None],
    op: str,
) -> tuple[IndexingStats | None, str | None]:
    """Backup-read → ``mutate`` → incremental re-index, rolling back on failure.

    Shared tail of ``mem_edit`` and ``mem_delete``'s chunk branch so the
    rollback contract lives in exactly one place. The caller MUST hold the
    file's L1 *and* L2 locks (via ``_locked_chunk``): under them, no other
    CRUD writer — in this process or any other, and no ``memory-migrate`` —
    can commit between the backup read and the rollback ``write_text``, so
    restoring ``original`` reverts only this call's own mutation. Because L2
    is already held, both ``index_file`` calls pass ``lock_held=True`` to skip
    the nested sidecar acquire that would otherwise self-deadlock (#1587).

    Returns ``(stats, None)`` on success or ``(None, error_message)`` after
    a rollback; ``op`` ("edit"/"delete") only shapes the messages.
    """
    original = await asyncio.to_thread(source_file.read_text, encoding="utf-8")
    try:
        await asyncio.to_thread(mutate)
        stats = await app.index_engine.index_file(source_file, already_scanned=True, lock_held=True)
        app.search_pipeline.invalidate_cache()
        await _flag_mutation_on_active_session(app)
        return stats, None
    except Exception as exc:
        await asyncio.to_thread(source_file.write_text, original, encoding="utf-8")
        try:
            await app.index_engine.index_file(source_file, already_scanned=True, lock_held=True)
        except Exception:
            logger.warning("Rollback re-index also failed", exc_info=True)
        app.search_pipeline.invalidate_cache()
        logger.error("mem_%s rollback after indexing failure: %s", op, exc, exc_info=True)
        return None, f"Error: {op} failed and rolled back: {exc}"


def _validate_path(
    path_str: str,
    memory_dirs: list,
    project_memory_dirs: list | None = None,
    *,
    scope: TargetScope = "user",
    project_root: Path | None = None,
) -> tuple[Path | None, str | None]:
    """Validate and resolve a user-supplied path.

    Relative paths default to the first user-tier ``memory_dir``. When
    the caller passes an explicit project-tier ``scope`` together with a
    ``project_root``, relative paths instead resolve under the matching
    project tier (``<project_root>/.memtomem/memories[.local]``) so the
    explicit scope kwarg is honoured (ADR-0011 PR-D round 8). Absolute
    paths must live under one of ``memory_dirs`` or
    ``project_memory_dirs``.

    Without the scope/project_root override the function preserves its
    historical behaviour for unmodified callers (e.g. ``mem_delete``
    which never accepts a scope kwarg).

    Returns (resolved_path, None) on success, or (None, error_message)
    on failure.
    """
    raw = Path(path_str).expanduser()
    user_bases = [Path(d).expanduser().resolve() for d in memory_dirs]
    project_bases = [Path(d).expanduser().resolve() for d in (project_memory_dirs or [])]
    bases = user_bases + project_bases

    if raw.is_absolute():
        target = raw.resolve()
    elif scope in ("project_shared", "project_local"):
        # ADR-0011 PR-D round 8 — explicit project-tier scope on a
        # relative path: resolve under the requested tier base instead
        # of ``user_bases[0]``. Without this branch ``classify_scope``
        # downstream would see a user-tier path and silently flip the
        # effective scope back to ``user``, dropping the caller's
        # explicit project-scope intent.
        if project_root is None:
            return (
                None,
                (
                    f"Error: scope='{scope}' with a relative file path "
                    "requires a registered project context "
                    "(no project_memory_dirs entry covers the current cwd)."
                ),
            )
        from memtomem.memory_scope import (
            MemoryScopeError,
            resolve_memory_scope_dir,
        )

        try:
            # ``user_base`` is unused for project tiers — don't index a
            # possibly-empty ``memory_dirs`` here (#1768).
            base = resolve_memory_scope_dir(scope, project_root)
        except MemoryScopeError as exc:
            return None, f"Error: {exc}"
        target = (base / raw).resolve()
    else:
        # Resolve relative paths against the first user-tier memory_dir.
        # Project-tier roots are absolute-only; mixing them in here would
        # surprise existing callers that pass plain filenames.
        if not user_bases:
            from memtomem.memory_scope import EMPTY_MEMORY_DIRS_ERROR

            return None, f"Error: {EMPTY_MEMORY_DIRS_ERROR}"
        target = (user_bases[0] / raw).resolve()

    if not any(target.is_relative_to(b) for b in bases):
        return None, "Error: path is outside configured memory directories."

    return target, None


async def _mem_add_core(
    content: str,
    title: str | None,
    tags: list[str] | None,
    file: str | None,
    namespace: str | None,
    template: str | None,
    ctx: CtxType,
    force_unsafe: bool = False,
    scope: TargetScope = "user",
    confirm_project_shared: bool = False,
    project_root_override: Path | None = None,
    idempotency_key: str | None = None,
    *,
    event_type: str,
) -> tuple[str, "IndexingStats | None"]:
    """Core logic for ``mem_add`` — also usable from internal callers that
    need the ``IndexingStats`` (e.g. ``mem_consolidate_apply`` linking new
    summary chunks by id without the old ``recall_chunks(limit=1)`` race).

    ``scope`` is ADR-0011 Gate B: passing anything other than ``"user"``
    requires explicit caller intent. ``project_shared`` additionally
    requires ``confirm_project_shared=True`` (the gate-B confirm
    surrogate for MCP callers; the CLI uses an interactive prompt).

    ``project_root_override`` pins the project tier write target to a
    specific project regardless of the MCP server's current cwd. Used
    by ``mem_consolidate_apply`` so a summary written from chunks that
    live under ``/projA`` lands in ``/projA/.memtomem/...`` even when
    the server itself is running with cwd in ``/projB``. Without this,
    ``_resolve_project_context_root(app)`` would resolve to the server
    cwd and the summary would silently cross project boundaries
    (ADR-0011 PR-D review round 7).

    ``idempotency_key`` makes the write idempotent (issue #1573): a retry
    with the same key returns the original result and performs no second
    write. On such a replay ``stats`` is ``None`` (the write already
    happened), so id-consuming internal callers must not pass a key.

    ``event_type`` names the *public* surface this write arrived on, for
    the session-provenance event (issue #1876). It is required and has no
    default on purpose: this helper serves four different tools, so a
    default would silently mislabel a fifth caller — or let it skip
    provenance altogether, which is the exact failure #1876 is about.
    Instrumenting here rather than in each tool is what makes the
    attribution correct: the session id has to be captured under the file
    lock, after the wait, and that only exists inside this function.

    Returns:
        Tuple of ``(user_facing_message, stats)``. ``stats`` is ``None``
        for early error returns (empty content, oversized content,
        redaction-guard hit without ``force_unsafe``, template failure,
        invalid path, missing project_shared confirm) and for an
        idempotent replay, so callers must tolerate ``None``.
    """
    if idempotency_key is not None:
        key_err = _validate_idempotency_key(idempotency_key)
        if key_err:
            return (key_err, None)
    if not content.strip():
        return ("Error: content cannot be empty.", None)
    if len(content) > MAX_CONTENT_LENGTH:
        return ("Error: content too large (max 100,000 characters).", None)

    from datetime import datetime, timezone

    from memtomem import privacy
    from memtomem.config import classify_scope
    from memtomem.tools.memory_writer import append_entry

    app = await _get_app_initialized(ctx)

    # Fast-path idempotent replay: return the stored result before re-running
    # the redaction gates (avoids double-counting privacy outcomes). The
    # authoritative re-check happens under the file lock below.
    if idempotency_key is not None:
        stored = await app.storage.idempotency_get("mem_add", idempotency_key)
        if stored is not None:
            return (stored + _REPLAY_MARKER, None)

    # Block vector-dependent writes when the server is in degraded mode
    # (see issue #349). Without this gate the subsequent ``index_file``
    # call hits ``upsert_chunks`` and crashes on a missing ``chunks_vec``.
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return (mismatch_msg, None)

    mdirs = app.config.indexing.memory_dirs
    pmdirs = app.config.indexing.project_memory_dirs

    # ADR-0011: derive the *effective* scope before running the gates.
    # When the caller passes a ``file=`` path and that path lands in a
    # registered project tier directory, the indexer will tag the
    # resulting chunks with that project tier — so Gate A/B must see
    # the same tier the chunks will end up in. A caller leaving scope
    # at its default ``user`` while pointing ``file=`` at a project-
    # shared path would otherwise bypass Gate B (no confirm required)
    # and Gate A (force_unsafe=True still allowed). Mirrors the
    # ``mem_edit`` / ``mem_delete`` inferred-scope contract.
    target: Path | None = None
    if file:
        # ADR-0011 PR-D round 8: thread caller's explicit scope into the
        # path validator so a relative ``file=`` under project-tier
        # scope resolves to the project's ``.memtomem/...`` directory
        # instead of the user-tier base. Project root for the relative
        # branch comes from the override (used by consolidate-apply for
        # cross-project summaries) or the server-cwd resolver.
        validate_project_root: Path | None = None
        if scope in ("project_shared", "project_local"):
            from memtomem.server.tools.search import _resolve_project_context_root

            validate_project_root = (
                project_root_override
                if project_root_override is not None
                else _resolve_project_context_root(app)
            )
        target, err = _validate_path(
            file,
            mdirs,
            pmdirs,
            scope=scope,
            project_root=validate_project_root,
        )
        if err:
            return (err, None)
        assert target is not None
        inferred_scope, inferred_root = classify_scope(target, pmdirs)
        inferred_scope = cast(TargetScope, inferred_scope)
        effective_scope = inferred_scope
        effective_project_root: Path | None = inferred_root
    else:
        effective_scope = scope
        effective_project_root = None

    # Gate B (surface layer). project_shared writes go to git; require
    # explicit confirm so MCP callers cannot silently commit PII to a
    # tracked tier through a default-bool oversight.
    if effective_scope == "project_shared" and not confirm_project_shared:
        hint = (
            ""
            if effective_scope == scope
            else " (scope inferred from file= path; the target directory is "
            f"a registered project_shared tier under {effective_project_root})"
        )
        return (
            "Error: scope='project_shared' writes to a git-tracked "
            f"directory. Pass confirm_project_shared=True to proceed.{hint}",
            None,
        )

    # Gate A (chokepoint). enforce_write_guard hard-refuses
    # ``force_unsafe=True`` when scope=='project_shared'.
    #
    # The scan covers the entire ``content`` regardless of length — a secret
    # pasted past any byte offset still trips it. The asymmetry with STM's
    # compression-side scanner is intentional: STM's window is a routing
    # signal, while this is the write-rejection gate at the trust boundary.
    # (Kept as a comment rather than in the tool description: it explains why
    # the server behaves this way, which no caller needs in order to call it.)
    guard = privacy.enforce_write_guard(
        content,
        surface="mem_add",
        force_unsafe=force_unsafe,
        scope=effective_scope,
        audit_context={
            "namespace": namespace,
            "file": file,
            "scope": effective_scope,
            "scope_inferred_from_path": effective_scope != scope,
        },
    )
    if guard.decision == "blocked":
        return (
            f"Error: content matches {len(guard.hits)} privacy pattern(s); "
            "write rejected. Retry with force_unsafe=True to bypass "
            "(audit-logged).",
            None,
        )
    if guard.decision == "blocked_project_shared":
        return (
            f"Error: content matches {len(guard.hits)} privacy pattern(s) "
            "and force_unsafe=True is not permitted on scope='project_shared' "
            "(git history is forever). Retry with scope='project_local' "
            "or scope='user' to bypass; manually edit the canonical file "
            "if a project_shared write is required.",
            None,
        )

    # Apply template if specified
    if template:
        from memtomem.templates import list_templates, render_template

        try:
            content = render_template(template, content, title=title)
            # Template already includes its own heading — don't duplicate
            title = None
        except ValueError as exc:
            return (f"Error: {exc}\n\nAvailable templates:\n{list_templates()}", None)

    if target is None:
        # ADR-0011: route the default-dated file to the canonical
        # directory for the requested scope. Without this branch, MCP
        # ``mem_add(scope='project_shared')`` would still write to the
        # user-tier path even though the gate accepted the call — the
        # CLI/MCP divergence flagged in PR-D review.
        from memtomem.errors import ConfigError
        from memtomem.memory_scope import (
            MemoryScopeError,
            is_project_tier_registered,
            project_tier_registration_error,
            require_user_base,
            resolve_memory_scope_dir,
        )
        from memtomem.server.tools.search import _resolve_project_context_root

        if effective_scope == "user":
            # No silent cwd fallback: an empty ``memory_dirs`` must name
            # the config field, not write under the server's cwd (#1768).
            try:
                base = require_user_base(mdirs)
            except ConfigError as exc:
                return (f"Error: {exc}", None)
        else:
            # ADR-0011 PR-D review round 7: prefer the explicit
            # ``project_root_override`` (set by ``mem_consolidate_apply``
            # to the source chunks' persisted project_root) over the
            # server-cwd fallback so cross-project summaries land in
            # the source project's tier, not the server's project.
            project_root: Path | None = _resolve_project_context_root(app)
            if project_root_override is not None:
                project_root = project_root_override
            try:
                base = resolve_memory_scope_dir(effective_scope, project_root)
            except MemoryScopeError as exc:
                return (f"Error: {exc}", None)
            # ADR-0011: refuse if the resolved tier directory is not
            # registered — otherwise the row's scope flips to project
            # but the read surface / watcher cannot see it. Mirrors
            # the ``mm context memory-migrate`` registration guard.
            if not is_project_tier_registered(base, pmdirs):
                return (
                    f"Error: {project_tier_registration_error(base, effective_scope)}",
                    None,
                )
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = base / f"{date_str}.md"

    assert target is not None

    # Serialize append + re-index under the per-file lock (issue #1570) plus the
    # cross-process sidecar (issue #1587): a concurrent mem_edit/mem_delete
    # rollback — in this process OR another server/CLI — restores its own
    # pre-image, so an append landing mid-span would be silently erased without
    # both. ``lock_held=True`` skips index_file's nested sidecar acquire.
    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        _lock_path_for,
        async_file_lock,
    )

    # The gauge spans capture -> append -> index -> provenance event, not
    # just the indexing. Released any earlier, session teardown could
    # observe idle, snapshot the event list, and miss a write that was
    # still persisting its own provenance a moment later.
    async with app.write_in_flight():
        try:
            async with (
                app.get_memory_file_lock(target),
                async_file_lock(
                    _lock_path_for(target.expanduser().resolve()),
                    timeout=_CRUD_SIDECAR_LOCK_BUDGET_S,
                ),
            ):
                # Idempotency claim under the lock (issue #1573). The claim is a
                # global (tool, key) row, not a file lock, so it also blocks a
                # concurrent same-key call that targets a *different* file: exactly
                # one caller wins the write, the rest replay or get "in progress".
                if idempotency_key is not None:
                    state, stored = await app.storage.idempotency_claim("mem_add", idempotency_key)
                    if state == "completed":
                        assert stored is not None  # completed rows always carry a result
                        return (stored + _REPLAY_MARKER, None)
                    if state == "pending":
                        return (_idempotency_in_progress_error(idempotency_key), None)
                # Resolve the session-derived namespace *inside* the lock: waiting on
                # the lock is a suspension point, and the active session can change
                # during it — the entry must land under the namespace active at write
                # time, not one captured before the wait.
                #
                # The session id is captured in the *same* lock acquisition as the
                # namespace, not separately: a transition landing between the two
                # reads would file this write's chunks under the new session's
                # namespace and its provenance under the old session's id.
                provenance_session_id, effective_ns = await capture_session_and_namespace(
                    app, namespace
                )
                # Release the claim only for a failure *before* the append is
                # durable (mkdir / append itself) — nothing landed, so the key must
                # stay re-runnable. Once the append lands we NEVER release: a keyed
                # retry must not re-append. If index_file (below) raises, the claim
                # is left pending (retry gets "in progress", not a duplicate) and
                # the watcher / ``mm index --force`` recovers the un-indexed entry.
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    await asyncio.to_thread(append_entry, target, content, title=title, tags=tags)
                except Exception:
                    if idempotency_key is not None:
                        await _release_idempotency_claim(app, "mem_add", idempotency_key)
                    raise
                # Re-index the whole file via the standard pipeline so the watcher
                # (which also calls index_file) produces identical hashes → no dups.
                stats = await app.index_engine.index_file(
                    target, namespace=effective_ns, already_scanned=True, lock_held=True
                )
                display_ns = effective_ns or app.config.namespace.default_namespace
                result = (
                    f"Memory added to {target}\n"
                    f"- Namespace: {display_ns}\n"
                    f"- Chunks indexed: {stats.indexed_chunks}\n"
                    f"- File: {target}"
                )
                # Fill in the won claim with the base result, under the lock. Only
                # the deterministic base message is stored — the advisory tails
                # below are non-deterministic and original-only. A complete failure
                # leaves the row pending (never released — the append is durable),
                # so a retry replays/blocks instead of duplicating.
                if idempotency_key is not None:
                    try:
                        await app.storage.idempotency_complete("mem_add", idempotency_key, result)
                    except Exception:
                        logger.warning(
                            "idempotency ledger complete failed; mem_add key left pending "
                            "(retry blocks until TTL)",
                            exc_info=True,
                        )
        except TimeoutError:
            return (
                f"Error: {target} is locked by another process (migration in flight?); retry.",
                None,
            )
        app.search_pipeline.invalidate_cache()
        # After the lock, deliberately: ``add_session_event`` commits, and a
        # commit inside the CRUD lock span would flush while another process
        # may still be waiting on the same sidecar.
        await record_write_provenance(
            app,
            session_id=provenance_session_id,
            event_type=event_type,
            stats=stats,
        )

    # Semantic duplicate check: warn if very similar content already exists
    try:
        if len(content) > 20:
            # ADR-0011 PR-D round 9: thread project context so the
            # duplicate check sees the same scope set the just-written
            # chunk lives under. Without this, a project_shared write
            # would only get matched against user-tier candidates and
            # genuine in-project duplicates would slip through.
            from memtomem.server.tools.search import _resolve_project_context_root

            similar, _ = await app.search_pipeline.search(
                content,
                top_k=5,
                project_context_root=effective_project_root or _resolve_project_context_root(app),
            )
            dupes = [
                s
                for s in similar
                if s.score >= 0.90 and s.score < 0.9999  # exclude exact self-match
            ]
            if dupes:
                result += "\n\n⚠ Similar memories found:"
                for d in dupes[:3]:
                    preview = d.chunk.content[:80].replace("\n", " ")
                    result += f"\n  - ({d.score:.0%}) {preview}..."
    except Exception:
        logger.warning("Duplicate check after mem_add failed", exc_info=True)

    # Fire webhook
    if app.webhook_manager:
        task = asyncio.create_task(
            app.webhook_manager.fire("add", {"file": str(target), "chunks_indexed": 1})
        )
        task.add_done_callback(webhook_error_cb)

    # One-shot dim-mismatch hint — only emitted the first time per MCP session.
    dim_notice = await _announce_dim_mismatch_once(app)
    if dim_notice:
        result += f"\n\n{dim_notice}"

    return (result, stats)


@mcp.tool()
@tool_handler
async def mem_add(
    content: str,
    title: str | None = None,
    tags: list[str] | None = None,
    file: str | None = None,
    namespace: str | None = None,
    template: str | None = None,
    force_unsafe: bool = False,
    scope: TargetScope = "user",
    confirm_project_shared: bool = False,
    idempotency_key: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Add a new memory entry to a markdown file and immediately index it.

    The entry is appended to the target file (or a new timestamped file is
    created in the first configured memory directory), then re-indexed so it
    is immediately searchable.

    All content is scanned for secrets before any filesystem write; a match
    rejects the write. See ``force_unsafe`` below for the one escape hatch
    and ``mem_add_redaction_stats`` for the outcome counters.

    Args:
        content: The memory content to store
        title: Optional heading title for the entry
        tags: Optional tags for categorisation
        file: Target .md filename (relative or absolute). If omitted, a
              timestamped file is created in the first memory_dir.
        namespace: Assign indexed chunks to this namespace (default: config default)
        template: Use a built-in template (adr, meeting, debug, decision,
                  procedure). Content can be JSON with field values or plain text.
        force_unsafe: When True, bypass the redaction guard for this call
                      even when content matches a secret pattern. Use only
                      when matches are known false positives (e.g.,
                      documenting an example credential schema). The bypass
                      is recorded with a ``bypassed`` outcome and an audit
                      line, and it never applies to
                      ``scope="project_shared"`` — that combination is
                      hard-refused, because git history cannot be retracted
                      from clones.
        scope: Write tier: ``user`` (default), ``project_local``, or
               ``project_shared``.
        confirm_project_shared: Required explicit consent for a Git-tracked
                                ``project_shared`` write.
        idempotency_key: Optional key (max 256 chars) making this write
                         idempotent for 24h — a retry with the same key
                         returns the original result and writes nothing.
                         Only successful writes are recorded, so a failed
                         call may be retried with the same key. Without a
                         key, semantics are at-least-once.

    Returns a confirmation message, plus a duplicate warning when a highly
    similar memory (≥90% match) already exists.
    """
    message, _stats = await _mem_add_core(
        content=content,
        title=title,
        tags=tags,
        file=file,
        namespace=namespace,
        template=template,
        force_unsafe=force_unsafe,
        scope=scope,
        confirm_project_shared=confirm_project_shared,
        idempotency_key=idempotency_key,
        ctx=ctx,
        event_type="add",
    )
    return message


@mcp.tool()
@tool_handler
@register("crud")
async def mem_edit(
    chunk_id: str,
    new_content: str,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """Edit an existing memory entry in its source markdown file.

    ``new_content`` is treated as body-only: the heading line and the
    section-leading ``> created:`` / ``> tags:`` blockquote header are
    preserved automatically. To override the heading explicitly,
    prefix ``new_content`` with ``## `` and the call reverts to a
    full replacement of the chunk's line range.

    ``new_content`` passes through the same trust-boundary redaction
    guard as ``mem_add``. A match rejects the edit unless
    ``force_unsafe=True``; bypass events are audit-logged. See
    ``mem_add_redaction_stats`` for the counter snapshot.

    ADR-0011: the gate's scope is **inferred from the loaded chunk**,
    not from a caller parameter. Editing a chunk whose persisted
    ``scope == 'project_shared'`` enforces the same hard-refusal of
    ``force_unsafe=True`` that applies to ``mem_add(scope='project_shared',
    ...)`` — a client cannot bypass Gate A by omitting an explicit
    scope kwarg on the edit path.

    Args:
        chunk_id: The UUID of the chunk to edit (shown in mem_search results)
        new_content: The replacement body. Heading + per-entry metadata
            blockquote are preserved unless the value starts with ``## ``.
        force_unsafe: When True, bypass the redaction guard for this call
            even when ``new_content`` matches a secret pattern. Use only
            when matches are known false positives. The bypass is recorded
            with a ``bypassed`` outcome and an audit line. It does not apply
            when the edited chunk's own scope is ``project_shared``: that is
            hard-refused, because git history cannot be retracted from
            clones.
    """
    if not new_content.strip():
        return "Error: new_content cannot be empty."

    from memtomem import privacy
    from memtomem.tools.memory_writer import replace_chunk_body

    app = await _get_app_initialized(ctx)
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    try:
        uid = UUID(chunk_id)
    except (ValueError, TypeError):
        return f"Error: invalid chunk ID format: {chunk_id}"

    # Serialize the whole read → rewrite → re-index → rollback span on the
    # chunk's source file, re-fetching the chunk fresh under the lock so the
    # line range reflects any concurrent CRUD write (issue #1570).
    async with _locked_chunk(app, uid, chunk_id) as (chunk, lock_err):
        if lock_err:
            return lock_err
        assert chunk is not None
        meta = chunk.metadata

        # ADR-0011: infer scope from the loaded chunk's persisted metadata.
        # The privacy gate sees the same scope the chunk lives under, so
        # editing a project_shared chunk gets the project_shared refusal
        # rule even when the caller did not pass an explicit scope kwarg.
        # Evaluated on the fresh chunk: a migrate could have re-scoped it
        # while we waited, and validating a stale snapshot would reopen the
        # Gate-A bypass ADR-0011 closed.
        inferred_scope = meta.scope or "user"
        guard = privacy.enforce_write_guard(
            new_content,
            surface="mem_edit",
            force_unsafe=force_unsafe,
            scope=inferred_scope,
            audit_context={"chunk_id": chunk_id, "scope": inferred_scope},
        )
        if guard.decision == "blocked":
            return (
                f"Error: new_content matches {len(guard.hits)} privacy pattern(s); "
                "edit rejected. Retry with force_unsafe=True to bypass (audit-logged)."
            )
        if guard.decision == "blocked_project_shared":
            return (
                f"Error: new_content matches {len(guard.hits)} privacy pattern(s) "
                "and force_unsafe=True is not permitted on scope='project_shared' "
                "chunks (git history is forever). Move the chunk to a different "
                "scope first, or hand-edit the canonical file with explicit review."
            )
        # ``replace_chunk_body`` preserves the heading + section-leading
        # blockquote header (``> created:`` / ``> tags:``) so that callers
        # supplying body-only ``new_content`` don't accidentally erase the
        # metadata. Pass a content prefixed with ``## `` to override the
        # heading explicitly and bypass preservation.
        stats, mutate_err = await _mutate_file_and_reindex(
            app,
            meta.source_file,
            lambda: replace_chunk_body(
                meta.source_file, meta.start_line, meta.end_line, new_content
            ),
            op="edit",
        )
        if mutate_err:
            return mutate_err
        assert stats is not None

        return (
            f"Memory updated in {meta.source_file}\n"
            f"- Lines {meta.start_line}-{meta.end_line} replaced\n"
            f"- Re-indexed: {stats.indexed_chunks} chunks"
        )


@mcp.tool()
@tool_handler
@register("crud")
async def mem_delete(
    chunk_id: str | None = None,
    source_file: str | None = None,
    namespace: str | None = None,
    confirm_project_shared: bool = False,
    ctx: CtxType = None,
) -> str:
    """Delete memory entries from the index (and optionally from the source file).

    When chunk_id is given, the specific chunk's line range is removed from
    the markdown file and the file is re-indexed.
    When source_file is given, all chunks from that file are removed from the
    index (the file itself is NOT deleted).
    When namespace is given, all chunks in that namespace are removed from the index.
    ADR-0011: deleting project_shared chunks requires
    ``confirm_project_shared=True``. Bulk source deletes are all-or-nothing:
    if any matched chunk is project_shared, the whole source delete is rejected.

    Args:
        chunk_id: UUID of a specific chunk to delete
        source_file: Path to remove all indexed chunks from
        namespace: Namespace to delete all chunks from
        confirm_project_shared: Required for project_shared chunks
    """
    from memtomem.tools.memory_writer import remove_lines

    app = await _get_app_initialized(ctx)

    if chunk_id:
        try:
            uid = UUID(chunk_id)
        except (ValueError, TypeError):
            return f"Error: invalid chunk ID format: {chunk_id}"

        # Serialize the read → rewrite → re-index → rollback span on the
        # chunk's source file; re-fetch fresh under the lock (issue #1570).
        async with _locked_chunk(app, uid, chunk_id) as (chunk, lock_err):
            if lock_err:
                return lock_err
            assert chunk is not None
            meta = chunk.metadata
            # Confirm gate on the fresh chunk: a migrate could have re-scoped
            # it while we waited for the lock (see mem_edit for the rationale).
            inferred_scope = meta.scope or "user"
            if inferred_scope == "project_shared" and not confirm_project_shared:
                logger.info(
                    "mem_delete rejected project_shared chunk without confirmation",
                    extra={"chunk_id": chunk_id, "scope": inferred_scope},
                )
                return (
                    "Error: deleting scope='project_shared' chunks requires "
                    "confirm_project_shared=True."
                )
            stats, mutate_err = await _mutate_file_and_reindex(
                app,
                meta.source_file,
                lambda: remove_lines(meta.source_file, meta.start_line, meta.end_line),
                op="delete",
            )
            if mutate_err:
                return mutate_err
            assert stats is not None
            return (
                f"Memory deleted from {meta.source_file}\n"
                f"- Lines {meta.start_line}-{meta.end_line} removed\n"
                f"- Re-indexed: {stats.indexed_chunks} chunks"
            )

    if source_file:
        sf_path, sf_err = _validate_path(
            source_file,
            app.config.indexing.memory_dirs,
            app.config.indexing.project_memory_dirs,
        )
        if sf_err:
            return sf_err
        assert sf_path is not None
        # Same per-file lock as the chunk branch (issue #1570) plus the
        # cross-process sidecar (#1587): without both locks, a concurrent
        # incremental ``index_file`` — in this process or another — can land
        # after this delete and re-upsert the whole file, silently
        # resurrecting the rows just removed. The Gate-B scope probe runs under
        # the locks too so it sees the same state the delete acts on.
        from memtomem.context._atomic import (
            _CRUD_SIDECAR_LOCK_BUDGET_S,
            _lock_path_for,
            async_file_lock,
        )

        try:
            async with (
                app.get_memory_file_lock(sf_path),
                async_file_lock(
                    _lock_path_for(sf_path.expanduser().resolve()),
                    timeout=_CRUD_SIDECAR_LOCK_BUDGET_S,
                ),
            ):
                scopes = await app.storage.list_scopes_by_source(sf_path)
                if "project_shared" in scopes and not confirm_project_shared:
                    logger.info(
                        "mem_delete rejected bulk project_shared source without confirmation",
                        extra={"source_file": str(sf_path), "scopes": sorted(scopes)},
                    )
                    return (
                        "Error: source_file delete would remove scope='project_shared' chunks; "
                        "pass confirm_project_shared=True to proceed. Bulk source deletes are "
                        "all-or-nothing; use chunk_id for per-chunk control."
                    )
                deleted = await app.storage.delete_by_source(sf_path)
        except TimeoutError:
            return (
                f"Error: {source_file} is locked by another process (migration in flight?); retry."
            )
        app.search_pipeline.invalidate_cache()
        return f"Removed {deleted} chunks from index for {source_file}"

    if namespace:
        # Lock every source file currently holding chunks in the namespace
        # (issue #1570): a concurrent locked CRUD span on any of those files
        # would otherwise re-upsert it after this delete and resurrect its
        # rows. Keys are acquired in sorted order, so two multi-lock deletes
        # cannot deadlock each other, and every other CRUD path holds at most
        # one of these locks without acquiring more — no cycle is possible.
        # A file that gains namespace chunks after this snapshot is not
        # locked (same point-in-time semantics the unlocked delete had).
        #
        # This branch stays L1-only (no L2 sidecar) by design (#1587): it is a
        # DB-only bulk delete over N files with no file read/rewrite, and taking
        # N sidecars would add a shared-deadline multi-resource acquire for a
        # race whose only cross-process outcome — a concurrent add re-inserting
        # rows for a file that still exists on disk — is coherent
        # file-is-source-of-truth behavior, not corruption.
        sources = await app.storage.list_sources_by_namespace(namespace)
        lock_keys = sorted({AppContext.memory_file_lock_key(p) for p in sources})
        async with AsyncExitStack() as stack:
            for lock_key in lock_keys:
                await stack.enter_async_context(app.get_memory_file_lock(lock_key))
            # ADR-0011 PR-D Gate B on bulk namespace delete. project_shared
            # memories can sit in the default namespace alongside user
            # memories, so the namespace string alone does not imply the
            # trust tier — probe the persisted scope set (under the locks,
            # so the gate sees the same state the delete acts on).
            ns_scopes = await app.storage.list_scopes_by_namespace(namespace)
            if "project_shared" in ns_scopes and not confirm_project_shared:
                logger.info(
                    "mem_delete rejected bulk project_shared namespace without confirmation",
                    extra={"namespace": namespace, "scopes": sorted(ns_scopes)},
                )
                return (
                    f"Error: namespace='{namespace}' delete would remove "
                    "scope='project_shared' chunks; pass confirm_project_shared=True "
                    "to proceed. Bulk namespace deletes are all-or-nothing; use "
                    "chunk_id for per-chunk control."
                )
            deleted = await app.storage.delete_by_namespace(namespace)
        app.search_pipeline.invalidate_cache()
        return f"Removed {deleted} chunks from namespace '{namespace}'"

    return "Provide chunk_id, source_file, or namespace."


@mcp.tool()
@tool_handler
@register("crud")
async def mem_batch_add(
    entries: list[dict],
    namespace: str | None = None,
    file: str | None = None,
    force_unsafe: bool = False,
    scope: TargetScope = "user",
    confirm_project_shared: bool = False,
    idempotency_key: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Add multiple memory entries in one call (KV batch).

    Each entry dict should have "key" (title) and "value" (content), and
    optionally "tags" (list[str]).  All entries are appended to the same file
    and indexed once.

    Each entry's content passes through the same trust-boundary redaction
    guard as ``mem_add`` — routed through ``enforce_write_guard`` per
    entry (ADR-0011 PR-D refactor of the earlier inline-scan path) so
    the project_shared hard refusal is unbypassable on the batch path.
    If any entry matches a secret pattern, the whole batch is rejected
    — partial-success on a flagged batch would leak the transactional
    contract callers rely on. Pass ``force_unsafe=True`` to bypass for
    the whole batch (each hit item is recorded with a ``bypassed``
    outcome label per audit). When ``scope='project_shared'``,
    ``force_unsafe=True`` is hard-refused regardless: git history is
    forever (ADR-0011 §5).

    Each entry's full value is scanned regardless of length — the scan
    no longer truncates at a fixed window, so a secret embedded past any
    byte offset still trips the guard.

    Args:
        entries: List of {"key": "title", "value": "content", "tags": [...]}
        namespace: Namespace for all entries (default: config default)
        file: Target .md file.  If omitted, a timestamped file is created.
        force_unsafe: When True, bypass the redaction guard for any flagged
                      entries. Each bypass is recorded per item with a
                      ``bypassed`` outcome and an audit line. It never
                      applies to ``scope="project_shared"`` — that
                      combination is hard-refused for the whole batch,
                      because git history cannot be retracted from clones.
        scope: ADR-0011 scope axis (``user`` / ``project_shared`` /
               ``project_local``). Applies to every entry in the batch.
        confirm_project_shared: Required when ``scope='project_shared'``
                                — Gate B explicit opt-in for git-tracked writes.
        idempotency_key: Optional client-chosen key (max 256 chars) making
                         this write idempotent for 24h: a retried call with
                         the same key returns the original result and performs
                         no new write. Only successful writes are recorded — a
                         failed call may be retried with the same key. Without
                         a key, semantics stay at-least-once (a transport retry
                         may duplicate the entries).
    """
    if len(entries) > 500:
        return f"Error: batch too large (max 500 entries, got {len(entries)})."
    if idempotency_key is not None:
        key_err = _validate_idempotency_key(idempotency_key)
        if key_err:
            return key_err

    from datetime import datetime, timezone

    from memtomem import privacy
    from memtomem.config import classify_scope
    from memtomem.tools.memory_writer import append_blocks, format_entry_block

    app = await _get_app_initialized(ctx)
    mismatch_msg = _check_embedding_mismatch(app)
    if mismatch_msg:
        return mismatch_msg

    # Fast-path idempotent replay before the redaction loop so a retry doesn't
    # re-record per-entry privacy outcomes. Authoritative re-check is under the
    # file lock below.
    if idempotency_key is not None:
        stored = await app.storage.idempotency_get("mem_batch_add", idempotency_key)
        if stored is not None:
            return stored + _REPLAY_MARKER
    mdirs = app.config.indexing.memory_dirs
    pmdirs = app.config.indexing.project_memory_dirs

    # ADR-0011: derive the *effective* scope before running Gate A/B.
    # When the caller passes a ``file=`` path that lands in a
    # registered project tier directory, the indexer will tag the
    # resulting chunks with that project tier — so the gates must see
    # the same tier the chunks will end up in. A caller leaving scope
    # at its default ``user`` while pointing ``file=`` at a project-
    # shared path would otherwise bypass Gate B (no confirm required)
    # and Gate A (force_unsafe=True still allowed). Mirrors the
    # ``_mem_add_core`` inferred-scope contract.
    target: Path | None = None
    if file:
        # ADR-0011 PR-D round 8: relative ``file=`` under project-tier
        # scope must resolve to the project's tier base, not the user
        # tier — see ``_mem_add_core`` for the rationale.
        validate_project_root: Path | None = None
        if scope in ("project_shared", "project_local"):
            from memtomem.server.tools.search import _resolve_project_context_root

            validate_project_root = _resolve_project_context_root(app)
        target, err = _validate_path(
            file,
            mdirs,
            pmdirs,
            scope=scope,
            project_root=validate_project_root,
        )
        if err:
            return err
        assert target is not None
        inferred_scope, _ = classify_scope(target, pmdirs)
        inferred_scope = cast(TargetScope, inferred_scope)
        effective_scope = inferred_scope
    else:
        effective_scope = scope

    # Gate B (surface layer). Mirrors mem_add — explicit confirm
    # required for any project_shared write, batch or single.
    if effective_scope == "project_shared" and not confirm_project_shared:
        hint = (
            ""
            if effective_scope == scope
            else " (scope inferred from file= path; the target directory is "
            "a registered project_shared tier)"
        )
        return (
            "Error: scope='project_shared' writes to a git-tracked "
            f"directory. Pass confirm_project_shared=True to proceed.{hint}"
        )

    # Trust-boundary redaction guard. Each entry routes through
    # ``enforce_write_guard(record_outcome=False)`` so the
    # project_shared force_unsafe hard refusal applies on the batch
    # path too — the earlier inline ``privacy.scan`` + manual
    # ``record`` pattern bypassed gate A and was the bypass route
    # ADR-0011 §5 explicitly closes. We collect decisions first and
    # only record outcomes after deciding whether to commit the
    # whole batch (transactional invariant: no pass record on a
    # rejected batch).
    decisions: list[tuple[int, str, int]] = []  # (idx, decision, hit_count)
    for idx, entry in enumerate(entries):
        value = entry.get("value") or entry.get("content", "")
        if not value:
            continue
        guard = privacy.enforce_write_guard(
            value,
            surface="mem_batch_add",
            force_unsafe=force_unsafe,
            scope=effective_scope,
            audit_context={
                "namespace": namespace,
                "file": file,
                "item_idx": idx,
                "scope": effective_scope,
                "scope_inferred_from_path": effective_scope != scope,
            },
            record_outcome=False,
        )
        decisions.append((idx, guard.decision, len(guard.hits)))

    blocked = [d for d in decisions if d[1] == "blocked"]
    blocked_shared = [d for d in decisions if d[1] == "blocked_project_shared"]
    if blocked_shared:
        # Hard-refusal on project_shared force_unsafe — record blocked_project_shared
        # for each hit item, no pass for clean items (transactional reject).
        for _ in blocked_shared:
            privacy.record("blocked_project_shared", "mem_batch_add")
        idxs = [d[0] for d in blocked_shared]
        return (
            f"Error: items at indices {sorted(idxs)} match privacy patterns "
            "and force_unsafe=True is not permitted on scope='project_shared' "
            "(git history is forever). Whole batch rejected. Move flagged "
            "items to scope='project_local' or scope='user' to bypass."
        )
    if blocked:
        for _ in blocked:
            privacy.record("blocked", "mem_batch_add")
        idxs = [d[0] for d in blocked]
        return (
            f"Error: items at indices {sorted(idxs)} match privacy patterns; "
            "whole batch rejected. Resubmit with hit items removed, or pass "
            "force_unsafe=True to bypass (audit-logged)."
        )

    # Batch will commit — record per-entry outcomes and emit bypass
    # audits for any force-unsafe hits.
    for idx, decision, hit_count in decisions:
        if decision == "bypassed":
            privacy.record("bypassed", "mem_batch_add")
            value = entries[idx].get("value") or entries[idx].get("content", "")
            privacy.emit_bypass_audit(
                surface="mem_batch_add",
                content_chars=len(value),
                hits=hit_count,
                audit_context={
                    "namespace": namespace,
                    "file": file,
                    "item_idx": idx,
                    "scope": effective_scope,
                },
            )
        else:
            privacy.record("pass", "mem_batch_add")

    if target is None:
        # ADR-0011: scope-aware default-dated file target. Mirrors
        # ``_mem_add_core`` so MCP ``mem_batch_add(scope='project_shared')``
        # lands in the project's ``.memtomem/memories/`` directory, not
        # the user-tier path.
        from memtomem.errors import ConfigError
        from memtomem.memory_scope import (
            MemoryScopeError,
            is_project_tier_registered,
            project_tier_registration_error,
            require_user_base,
            resolve_memory_scope_dir,
        )
        from memtomem.server.tools.search import _resolve_project_context_root

        if effective_scope == "user":
            # No silent cwd fallback: an empty ``memory_dirs`` must name
            # the config field, not write under the server's cwd (#1768).
            try:
                base = require_user_base(mdirs)
            except ConfigError as exc:
                return f"Error: {exc}"
        else:
            project_root = _resolve_project_context_root(app)
            try:
                base = resolve_memory_scope_dir(effective_scope, project_root)
            except MemoryScopeError as exc:
                return f"Error: {exc}"
            # ADR-0011 PR-D round 6: refuse if the resolved tier dir is
            # not registered in IndexingConfig.project_memory_dirs.
            # Otherwise the row's scope flips to project but the read
            # surface / watcher cannot see it.
            if not is_project_tier_registered(base, pmdirs):
                return f"Error: {project_tier_registration_error(base, effective_scope)}"
        date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        target = base / f"{date_str}.md"

    assert target is not None

    def _append_entries() -> int:
        """Append all non-empty entries in ONE write; returns the skipped
        count. Composing every block up front and doing a single append
        makes the batch all-or-nothing (issue #1573) — a mid-batch failure
        can no longer leave entries ``0..k-1`` stranded on disk. Runs in one
        worker thread so formatting up to 500 blocks doesn't block the loop.
        """
        skipped = 0
        blocks: list[str] = []
        for entry in entries:
            key = entry.get("key") or entry.get("title", "")
            value = entry.get("value") or entry.get("content", "")
            entry_tags = entry.get("tags")
            if not value:
                skipped += 1
                continue
            blocks.append(format_entry_block(value, title=key or None, tags=entry_tags))
        append_blocks(target, blocks)
        return skipped

    # Serialize the batch append + re-index under the per-file lock (issue
    # #1570) plus the cross-process sidecar (#1587) so a concurrent
    # mem_edit/mem_delete rollback — in this process or another — cannot erase
    # these entries, same as the single-entry mem_add path above.
    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        _lock_path_for,
        async_file_lock,
    )

    # Same gauge span as ``_mem_add_core``: capture through provenance
    # event, so session teardown cannot snapshot around this write.
    async with app.write_in_flight():
        try:
            async with (
                app.get_memory_file_lock(target),
                async_file_lock(
                    _lock_path_for(target.expanduser().resolve()),
                    timeout=_CRUD_SIDECAR_LOCK_BUDGET_S,
                ),
            ):
                # Idempotency claim under the lock (issue #1573), same protocol as
                # ``_mem_add_core``: the global (tool, key) claim blocks a concurrent
                # same-key batch even when it targets a different file.
                if idempotency_key is not None:
                    state, stored = await app.storage.idempotency_claim(
                        "mem_batch_add", idempotency_key
                    )
                    if state == "completed":
                        assert stored is not None  # completed rows always carry a result
                        return stored + _REPLAY_MARKER
                    if state == "pending":
                        return _idempotency_in_progress_error(idempotency_key)
                # Inside the lock for the same write-time-namespace reason as
                # ``_mem_add_core`` — the session can change during the lock wait
                # — and in one acquisition with the session id for the same
                # reason: split, a transition between them would file the chunks
                # and their provenance under different sessions.
                provenance_session_id, effective_ns = await capture_session_and_namespace(
                    app, namespace
                )
                # Release only for a failure before the append is durable (same rule
                # as ``_mem_add_core``); once the single append lands the claim is
                # never released, so a keyed retry can't duplicate the batch.
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    skipped = await asyncio.to_thread(_append_entries)
                except Exception:
                    if idempotency_key is not None:
                        await _release_idempotency_claim(app, "mem_batch_add", idempotency_key)
                    raise
                stats = await app.index_engine.index_file(
                    target, namespace=effective_ns, already_scanned=True, lock_held=True
                )
                display_ns = effective_ns or app.config.namespace.default_namespace
                result = (
                    f"Batch add complete ({len(entries)} entries) → {target}\n"
                    f"- Namespace: {display_ns}\n"
                    f"- Chunks indexed: {stats.indexed_chunks}"
                )
                if skipped:
                    result += f"\n- Skipped: {skipped} entries (empty content)"
                # Fill in the won claim under the lock. A complete failure leaves the
                # row pending (never released — the append is durable), so a retry
                # replays/blocks instead of duplicating.
                if idempotency_key is not None:
                    try:
                        await app.storage.idempotency_complete(
                            "mem_batch_add", idempotency_key, result
                        )
                    except Exception:
                        logger.warning(
                            "idempotency ledger complete failed; mem_batch_add key left "
                            "pending (retry blocks until TTL)",
                            exc_info=True,
                        )
        except TimeoutError:
            return f"Error: {target} is locked by another process (migration in flight?); retry."
        app.search_pipeline.invalidate_cache()
        await record_write_provenance(
            app,
            session_id=provenance_session_id,
            event_type="batch_add",
            stats=stats,
        )

    return result


@mcp.tool()
@tool_handler
@register("crud")
async def mem_add_redaction_stats(
    ctx: CtxType = None,
) -> str:
    """Return a JSON snapshot of redaction-guard outcomes since process start.

    Outcome labels:
        blocked  — write rejected because content matched a privacy pattern.
        pass     — write proceeded; content matched no patterns.
        bypassed — write proceeded with ``force_unsafe=True`` despite a match.

    The ``by_tool`` map breaks the same outcomes down by ingress tool
    (``mem_add``, ``mem_batch_add``).

    Counts reflect attempted *write outcomes*, not raw scans. A rejected
    ``mem_batch_add`` records ``blocked`` once per hit item but does not
    record ``pass`` for the clean siblings in the same rejected batch
    (no write occurred for them). Summing
    ``blocked + pass + bypassed`` therefore equals the count of actual
    or attempted writes that reached the guard, not the total number
    of entries inspected.
    """
    import json

    from memtomem import privacy

    return json.dumps(privacy.snapshot(), indent=2)
