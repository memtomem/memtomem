"""Tools: mem_session_start, mem_session_end, mem_session_list."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

from memtomem.constants import (
    AGENT_NAMESPACE_PREFIX,
    RESERVED_UNBOUND_AGENT_ID,
    normalize_bound_agent_id,
    validate_namespace,
)
from memtomem.models import NamespaceFilter
from memtomem.server import mcp
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import PROVENANCE_KIND
from memtomem.summarization import SessionTooLargeError, summarize_session

logger = logging.getLogger(__name__)

# How long a session transition waits for in-flight session-bound writes to
# land before giving up and saying so. Short on purpose: teardown must stay
# responsive, and a write that outlasts this is reported rather than waited on
# indefinitely. The transition claim seals new provenance before this drain;
# later writes keep the public handle for routing but do not join the closing
# session's event snapshot (see mem_session_end).
_WRITE_DRAIN_TIMEOUT_S = 2.0


async def _end_active_session_inline(
    app: AppContext, session_id: str, reason: str, *, drained: bool = True
) -> str:
    """End a superseded session without resetting ``current_*`` state.

    Returns a one-line warning describing what was rolled forward. The
    caller is responsible for resetting ``current_session_id`` /
    ``current_agent_id`` afterwards (typically by overwriting them as
    part of a fresh session start), and for having claimed ``session_id``
    and drained in-flight writes first — see the transition protocol in
    :func:`mem_session_start`.

    ``session_id`` is passed in rather than read off ``app`` here: this
    runs outside ``_session_lock`` (it awaits the DB, and reading a
    session's events must not happen under a lock its writers need), so
    the handle could change underfoot.

    ``drained=False`` says the caller's drain timed out, so the event
    snapshot below may be short. It is recorded on the row rather than
    only in the caller's notice, because the consumer of a superseded
    session reads the row and never sees the notice.
    """

    events = await app.storage.get_session_events(session_id)
    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

    end_metadata: dict[str, object] = {"event_counts": event_counts, "auto_ended": True}
    if not drained:
        end_metadata["provenance_incomplete"] = True

    await app.storage.end_session(session_id, f"[auto-ended: {reason}]", end_metadata)
    await app.storage.scratch_cleanup(session_id)
    logger.warning(
        "mem_session_start auto-ended previous session %s (%s events) — %s",
        session_id,
        len(events),
        reason,
    )
    return f"(auto-ended previous session {session_id[:8]}... — {reason})"


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_start(
    agent_id: str | None = None,
    title: str | None = None,
    namespace: str | None = None,
    ctx: CtxType = None,
) -> str:
    """Start a new episodic memory session.

    Creates a session record and sets it as the current session. All
    subsequent tool calls will be tracked as session events. A **named**
    ``agent_id`` is recorded on ``AppContext.current_agent_id`` so that
    multi-agent tools (``mem_agent_search`` and friends) can resolve the
    active agent without the caller passing it on every call.

    Omitting ``agent_id`` — or passing the reserved
    ``"default"`` — starts an *unbound* session: no agent is bound, so
    ``mem_add`` / ``mem_batch_add`` / ``mem_index`` route exactly as they
    would with no session at all (``app.current_namespace``, else the
    indexing engine's namespace rules / ``auto_ns`` /
    ``default_namespace``), and visibility follows the ordinary search
    filters from there. Before #1875 an unbound session bound the
    literal agent ``"default"`` and silently redirected every subsequent
    write into ``agent-runtime:default``, which is a hidden system
    namespace — callers could not read back what they had just written.
    ``agent-runtime:default`` itself is still reachable with an explicit
    ``namespace=`` / ``agent_id=`` argument.

    State transitions:

    * No active session → records the new session, sets
      ``current_session_id`` and ``current_agent_id`` (the latter stays
      ``None`` for an unbound session).
    * Active session present → the previous session is **auto-ended**
      (with a warning logged and an inline notice in the return string)
      and the new session takes its place. The previous ``agent_id`` is
      replaced by the new one — agents do not stack.
    * Active session already being ended by a concurrent
      ``mem_session_end`` → that end owns the teardown; this start only
      takes the handle over rather than ending the session twice.

    Superseding waits (briefly) for in-flight session-bound writes to
    land before reading the previous session's events, so its final
    event counts are not short by a write that was already under way. A
    write that outlasts the wait is reported in the return string rather
    than waited on indefinitely.

    Namespace derivation priority (matches the LangGraph adapter
    ``MemtomemStore.start_agent_session`` so MCP and Python entry points
    behave the same):

    1. Explicit ``namespace=`` argument (escape hatch — wins everything).
       Run through :func:`validate_namespace` so a hostile-shaped string
       like ``"agent-runtime:foo:bar"`` cannot smuggle past the
       ``agent_id`` gate via the override; see issue #496.
    2. ``agent-runtime:<agent_id>`` when an agent was actually named
       (i.e. ``agent_id`` is neither omitted nor the reserved
       ``"default"``). This is the common case for multi-agent workflows.
    3. ``app.current_namespace`` (pre-multi-agent fallback).
    4. ``"default"``.

    This priority chain derives the **session record's** namespace only.
    The *write* routing is a separate consequence of the agent binding:
    with an agent bound, ``mem_add`` / ``mem_batch_add`` / ``mem_index``
    without an explicit ``namespace=`` resolve to
    ``agent-runtime:<agent_id>``; unbound, they consult
    ``app.current_namespace`` and the indexing config exactly as they do
    with no session at all. Namespace and agent_id remain separate axes
    on ``AppContext``.

    Args:
        agent_id: Identifier for the agent starting the session. Omit
            (or pass ``"default"``) to start an unbound session.
        title: Optional human-readable session title (e.g. "Sprint Planning")
        namespace: Session namespace. When omitted and an agent was
            named, defaults to ``agent-runtime:<agent_id>``.
    """
    bound_agent_id = normalize_bound_agent_id(agent_id)
    # The row keeps the literal "default" for an unbound session: the
    # column is NOT NULL, ``mem_session_list`` renders it unguarded, and
    # ``mm session start --idempotent`` compares it as a key. Only the
    # runtime binding below collapses to None (#1875).
    stored_agent_id = agent_id or RESERVED_UNBOUND_AGENT_ID
    if namespace is not None:
        validate_namespace(namespace)
    app = await _get_app_initialized(ctx)
    session_id = str(uuid4())
    if namespace:
        effective_ns = namespace
    elif bound_agent_id:
        effective_ns = f"{AGENT_NAMESPACE_PREFIX}{bound_agent_id}"
    elif app.current_namespace:
        effective_ns = app.current_namespace
    else:
        effective_ns = "default"

    auto_end_notice: str | None = None
    drain_notice: str | None = None
    # Session transition protocol (shared with mem_session_end): the
    # transition lock is held across the whole swap, while _session_lock is
    # taken only in short bursts to touch the handles. The two cannot be one
    # lock — the drain below has to wait for writers that need _session_lock
    # to leave the gauge, so holding it across the drain would deadlock.
    #
    # _ending_session_ids alone would not serialize this: it makes ending
    # at-most-once, but nothing in it decides which new session wins when two
    # starts interleave, so one start could publish its handle over another's
    # and orphan a freshly created row.
    async with app._session_transition_lock:
        async with app._session_lock:
            superseded_id = app.current_session_id
            # An id already claimed belongs to a mem_session_end that took its
            # claim before reaching for the transition lock and is now waiting
            # on it. That end owns the teardown; this start must only take the
            # handle over, never end the session a second time.
            if superseded_id and superseded_id in app._ending_session_ids:
                superseded_id = None
            elif superseded_id:
                app._ending_session_ids.add(superseded_id)

        try:
            if superseded_id:
                # Drain before reading the superseded session's events: a
                # write admitted before this transition may still be
                # persisting, and its record has to be in the snapshot that
                # closes the session out.
                superseded_drained = await app.wait_writes_drained(_WRITE_DRAIN_TIMEOUT_S)
                if not superseded_drained:
                    drain_notice = (
                        "(warning: writes still in flight — the superseded "
                        "session's event counts may be short)"
                    )
                auto_end_notice = await _end_active_session_inline(
                    app,
                    superseded_id,
                    reason="superseded by new mem_session_start",
                    drained=superseded_drained,
                )

            # The provenance marker says "this session records what its
            # writes created" — the seven MCP write surfaces log a
            # provenance event, so a consumer can read the session's real
            # inputs instead of inferring them from the namespace. It does
            # NOT say the record is complete; that is
            # ``provenance_incomplete``, set separately when something was
            # lost. Sessions created elsewhere (the CLI, the LangGraph
            # adapter) carry no marker and stay on the namespace path.
            metadata: dict[str, object] = {"provenance": PROVENANCE_KIND}
            if title:
                metadata["title"] = title
            await app.storage.create_session(
                session_id, stored_agent_id, effective_ns, metadata=metadata
            )
            async with app._session_lock:
                app.current_session_id = session_id
                app.current_agent_id = bound_agent_id
        finally:
            if superseded_id:
                async with app._session_lock:
                    app._ending_session_ids.discard(superseded_id)

    lines = [f"Session started: {session_id}"]
    if auto_end_notice:
        lines.append(auto_end_notice)
    if drain_notice:
        lines.append(drain_notice)
    if title:
        lines.append(f"- Title: {title}")
    # Deliberately does not name a write namespace for the unbound case:
    # ``effective_ns`` only populates the session row, while an unbound
    # write resolves through app.current_namespace and the indexing
    # engine's own rules (namespace policy / auto_ns / default_namespace).
    if bound_agent_id:
        lines.append(f"- Agent: {bound_agent_id}")
    else:
        lines.append("- Agent: (none — no agent bound)")
    lines.append(f"- Namespace: {effective_ns}")
    return "\n".join(lines)


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_end(
    summary: str | None = None,
    force_unsafe: bool = False,
    ctx: CtxType = None,
) -> str:
    """End the current episodic memory session.

    Closes the session, saves an optional summary, and records event
    statistics. Working memory bound to this session is cleaned up.
    Resets both ``current_session_id`` and ``current_agent_id`` so the
    next ``mem_agent_search`` falls back to ``current_namespace``.

    The session is *claimed* atomically under ``_session_lock`` at entry by
    recording its id in ``_ending_session_ids``, so a concurrent or
    client-retried ``mem_session_end`` returns ``"No active session."``
    instead of re-running the effectful phase — the summarize/persist work
    runs **at most once** per session. The public ``current_session_id`` /
    ``current_agent_id`` handles stay set through the phase and are cleared
    only when it completes, so concurrent writes still preserve
    ``mem_add`` agent-namespace routing and ``mem_scratch_set`` binding
    instead of silently falling back to the default scope. The claim also
    seals chunk-write provenance: a write that reaches attribution after
    the claim keeps the resolved agent namespace but does not join the
    closing session. The claim is not released on
    mid-phase failure (a retry must not risk a duplicate billable summary);
    an active DB row orphaned by a mid-phase crash is reaped by
    the stale-session path (``find_stale_active_sessions``).

    A write can already be in flight when teardown takes the claim. The
    teardown therefore waits briefly for writes that captured the session
    before the seal to land before snapshotting its events. Because gauge
    admission precedes capture and both capture and claim use
    ``_session_lock``, a write retaining this session id is necessarily on
    the drain's side of the boundary; later writes continue in the same
    namespace without being attributed to the closing session. A wait that
    times out is reported in the return string and recorded as
    ``provenance_incomplete`` on the row rather than presented as complete.

    ``- Events: N (add:M, index:K)`` counts the provenance events the
    seven chunk-creating write tools record, so it reflects what the
    session actually wrote. ``mem_index`` over a large tree can outlast
    the drain budget; the resulting "writes still in flight" line is
    expected rather than a fault.

    Tools that change the session's chunk set without being summarizable
    from it appear in no count and instead mark the session incomplete:
    the mutations (``mem_edit``, ``mem_delete``), the bulk deletes
    (``mem_ns_delete``, ``mem_cleanup_orphans``) and the bulk importers.
    Their effect on the input set is real and undescribed, which is
    exactly what the flag is for.

    When ``summary`` is provided, the text is also promoted to a
    first-class chunk under ``archive:session:<session_id>`` (Phase A
    of the episodic-session-summary RFC). The chunk is hidden from
    default ``mem_search`` via the ``archive:`` system prefix and
    surfaces only under explicit ``namespace_filter``. Persisting the
    chunk is best-effort: a failure is logged but does not roll back
    the session-end DB write.

    When ``summary`` is omitted, Phase B's auto path runs: if the
    server has an LLM provider configured, ``session_summary.auto`` is
    True, and the session recorded at least
    ``session_summary.min_chunks`` written chunks, the server asks the
    LLM for a short narrative summary and persists it through the same
    archive-chunk path. Provenance-enabled sessions select only the
    chunk ids carried by marked write events, so namespace rules may
    scatter one session's writes without losing them and unrelated
    concurrent writes are excluded. Sessions created by older / non-MCP
    callers, and sessions whose provenance is marked incomplete, keep
    the legacy ``namespace`` + ``started_at`` selection path.
    Sessions whose serialized chunk body exceeds
    ``session_summary.max_input_chars`` skip the auto path with a
    log warning (callers can pass an explicit ``summary=`` instead).

    Args:
        summary: Optional summary of what was accomplished in this
            session. When provided, also written as a chunk under
            ``<memory_dir>/sessions/<YYYY-MM>/<session_id>.md``.
        force_unsafe: Bypass the redaction guard when the summary matches a
            secret pattern. The bypass is recorded with a ``bypassed``
            outcome and an audit line (see ``mem_add_redaction_stats``).
            It never applies to a ``project_shared`` destination — that
            combination is hard-refused, because git history cannot be
            retracted from clones.
    """
    app = await _get_app_initialized(ctx)

    # Claim-then-work (issue #1571): under _session_lock, record the active
    # session id in _ending_session_ids so a retried or concurrent
    # mem_session_end cannot double-run the effectful phase (billable LLM
    # auto-summary, archive-chunk write + index, end_session UPDATE). The
    # loser (session already claimed) sees no active session and returns
    # early. At-most-once: the finally below clears the public handle on
    # both success and failure, so a retry after a partial failure sees no
    # active session and does not re-run the phase — the summary is never
    # duplicated. Same contract as schedule_try_claim (issue #1564).
    #
    # The public handle is deliberately left set here (unlike a naive
    # null-at-entry): it is cleared only after the phase, in the finally, so
    # concurrent writes during the multi-second phase still preserve the
    # session's routing context (agent namespace / scratch binding) instead
    # of falling back to the default. Provenance capture treats the claim as
    # a seal, so later chunk writes do not join the closing session. agent_id
    # is captured for the archive helper.
    # The claim is taken *before* the transition lock, not under it. A
    # concurrent or retried end must return "No active session." immediately
    # rather than queue behind the in-flight teardown — that early return is
    # the at-most-once contract, and waiting for the lock would turn it into a
    # multi-second block (and a deadlock whenever the first end is parked).
    async with app._session_lock:
        session_id = app.current_session_id
        if not session_id or session_id in app._ending_session_ids:
            return "No active session."
        app._ending_session_ids.add(session_id)
        agent_id = app.current_agent_id

    try:
        # Held across the rest of the teardown so a supervening
        # mem_session_start cannot interleave its swap with this one. Taken
        # after _session_lock was released, so the two locks are never held
        # together and cannot invert against mem_session_start's order.
        async with app._session_transition_lock:
            return await _end_session_phase(
                app,
                session_id=session_id,
                agent_id=agent_id,
                summary=summary,
                force_unsafe=force_unsafe,
            )
    finally:
        # Release the claim and clear the public handle now that the phase
        # is over — on success OR failure. Running on failure too means a
        # dead session is not left active for routing, and a retry then
        # sees no active session and does not re-run the effectful phase
        # (at-most-once). The identity guard leaves a *newer* session
        # untouched: if a mem_session_start supervened during the phase it
        # overwrote current_session_id with a fresh id.
        async with app._session_lock:
            app._ending_session_ids.discard(session_id)
            if app.current_session_id == session_id:
                app.current_session_id = None
                app.current_agent_id = None


async def _end_session_phase(
    app: AppContext,
    *,
    session_id: str,
    agent_id: str | None,
    summary: str | None,
    force_unsafe: bool,
) -> str:
    """The effectful half of ``mem_session_end``, under the transition lock.

    The caller owns the claim and the handle cleanup; this runs the drain,
    the snapshot, and the summary/persist work.
    """
    # Drain before snapshotting: a write admitted before this teardown may
    # still be persisting, and the snapshot below is what the summary and
    # the event counts are built from. Deliberately outside _session_lock —
    # writers need that lock to leave the gauge.
    drained = await app.wait_writes_drained(_WRITE_DRAIN_TIMEOUT_S)

    # Gather session stats
    events = await app.storage.get_session_events(session_id)
    event_counts: dict[str, int] = {}
    for e in events:
        event_counts[e["event_type"]] = event_counts.get(e["event_type"], 0) + 1

    # A drain that timed out means the snapshot above may be short. The
    # response line below says so, but a consumer of the stored row never
    # sees the response — so record it where the row is read, or a marked
    # session presents a partial input set as the whole story.
    end_metadata: dict[str, object] = {"event_counts": event_counts}
    if not drained:
        end_metadata["provenance_incomplete"] = True

    await app.storage.end_session(session_id, summary, end_metadata)

    # Read the final row after end_session merged ``end_metadata``. In
    # particular, a drain timeout writes ``provenance_incomplete`` above;
    # reading before the merge would hand the auto-summary a stale row and
    # let it trust the short event snapshot as authoritative. Tolerate a
    # missing row defensively: absence indicates external tampering or a
    # backend bug, and the auto path skips rather than crashing teardown.
    session_row = await app.storage.get_session(session_id)

    effective_summary = summary
    auto_summary_skip_reason: str | None = None
    auto_source_chunks: list = []
    if not summary:
        (
            effective_summary,
            auto_summary_skip_reason,
            auto_source_chunks,
        ) = await _maybe_auto_summarize(
            app,
            session_id=session_id,
            session_row=session_row,
            events=events,
            transition_incomplete=not drained,
        )

    summary_chunk_line: str | None = None
    summary_chunk_id = None
    if effective_summary:
        try:
            summary_chunk_line, summary_chunk_id = await _persist_session_summary_chunk(
                app,
                session_id=session_id,
                agent_id=agent_id,
                summary=effective_summary,
                event_counts=event_counts,
                force_unsafe=force_unsafe,
            )
        except Exception:
            logger.warning(
                "session_summary_chunk_persist_failed session_id=%s",
                session_id,
                exc_info=True,
            )

    # Phase B-2: link the summary chunk back to the source chunks it
    # summarized. Only runs on the auto path (manual ``summary=`` did
    # not collect source chunks). Failures are best-effort: a broken
    # link writer must not roll back the session-end DB write or the
    # archive chunk that already landed.
    if summary_chunk_id is not None and auto_source_chunks:
        try:
            await _write_summary_links(
                app,
                summary_chunk_id=summary_chunk_id,
                source_chunks=auto_source_chunks,
                cap=app.config.session_summary.max_summary_links,
            )
        except Exception:
            logger.warning(
                "session_summary_links_failed session_id=%s",
                session_id,
                exc_info=True,
            )

    # Cleanup session-bound working memory
    cleaned = await app.storage.scratch_cleanup(session_id)

    lines = [
        f"Session ended: {session_id}",
        f"- Events: {len(events)} ({', '.join(f'{k}:{v}' for k, v in event_counts.items())})",
    ]
    if not drained:
        # Say so rather than presenting a short count as complete.
        lines.append("- Warning: writes still in flight — event counts may be short")
    if effective_summary:
        prefix = "Summary" if summary else "Auto summary"
        lines.append(f"- {prefix}: {effective_summary[:100]}...")
    elif auto_summary_skip_reason:
        lines.append(f"- Auto summary: skipped ({auto_summary_skip_reason})")
    if summary_chunk_line:
        lines.append(summary_chunk_line)
    if cleaned:
        lines.append(f"- Working memory cleaned: {cleaned} entries")
    return "\n".join(lines)


async def _maybe_auto_summarize(
    app: AppContext,
    *,
    session_id: str,
    session_row: dict | None,
    events: list[dict],
    transition_incomplete: bool,
) -> tuple[str | None, str | None, list]:
    """Run the Phase B auto-summary path when prerequisites are met.

    A complete ``write-v1`` session selects its marked event ids exactly.
    An unmarked or incomplete session uses the legacy namespace/time-window
    inference. A marked session with no write events is authoritative empty,
    not a request to widen back to the legacy scan.

    Returns ``(summary_text, skip_reason, source_chunks)``. When the
    auto path produced text, ``skip_reason`` is ``None`` and
    ``source_chunks`` carries the chunks fed to the LLM (newest first)
    so the caller can write Phase B-2 ``chunk_links`` rows. When the
    path was skipped, ``summary_text`` is ``None``, ``skip_reason``
    carries a short label suitable for the tool response (``"disabled"``,
    ``"no llm"``, ``"no session row"``, ``"no started_at"``,
    ``"below min_chunks"``, ``"too large"``, ``"empty output"``, or
    ``"llm error"``), and ``source_chunks`` is an empty list.

    Failures inside the LLM call are caught and surfaced as
    ``"llm error"`` so a misconfigured provider does not block
    ``mem_session_end`` from completing.
    """
    cfg = app.config.session_summary
    if not cfg.auto:
        return None, "disabled", []

    llm = app.llm_provider
    if llm is None:
        return None, "no llm", []

    if session_row is None:
        return None, "no session row", []

    # ADR-0011 PR-D round 9: thread project context onto the always-on
    # scope filter for both provenance recall and the legacy namespace
    # fallback.
    from memtomem.server.tools.search import _resolve_project_context_root

    project_context_root = _resolve_project_context_root(app)

    metadata = session_row.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    provenance_enabled = metadata.get("provenance") == PROVENANCE_KIND
    provenance_incomplete = transition_incomplete or bool(metadata.get("provenance_incomplete"))

    chunks: list
    use_provenance = provenance_enabled and not provenance_incomplete
    if use_provenance:
        chunk_ids, invalid_reason = _collect_provenance_chunk_ids(events)
        if invalid_reason is not None:
            logger.warning(
                "auto_summary_provenance_fallback session_id=%s reason=%s",
                session_id,
                invalid_reason,
            )
            use_provenance = False
        else:
            # Count first so an empty authoritative set stays empty and a
            # deleted / scope-hidden id is detected rather than silently
            # presenting a partial provenance record as complete. The raw
            # content total is intentionally not a size gate: the prompt
            # formatter strips leading/trailing whitespace, so it can be
            # larger than the authoritative assembled body.
            chunk_count, _content_chars = await app.storage.sum_chunk_content_chars(
                chunk_ids,
                project_context_root=project_context_root,
            )
            if chunk_count != len(chunk_ids):
                logger.warning(
                    "auto_summary_provenance_fallback "
                    "session_id=%s reason=chunk_id_shortfall expected=%s found=%s",
                    session_id,
                    len(chunk_ids),
                    chunk_count,
                )
                use_provenance = False
            elif chunk_count < cfg.min_chunks:
                return None, "below min_chunks", []
            else:
                chunks = await app.storage.recall_chunks(
                    chunk_ids=chunk_ids,
                    limit=chunk_count,
                    project_context_root=project_context_root,
                )
                if len(chunks) != chunk_count:
                    # A concurrent delete can land between the aggregate
                    # and hydration. Treat that exactly like the aggregate
                    # shortfall instead of summarizing a set that changed
                    # underneath us.
                    logger.warning(
                        "auto_summary_provenance_fallback "
                        "session_id=%s reason=hydration_shortfall expected=%s found=%s",
                        session_id,
                        chunk_count,
                        len(chunks),
                    )
                    use_provenance = False

    if not use_provenance:
        started_at_str = session_row.get("started_at")
        namespace = session_row.get("namespace") or "default"
        if not started_at_str:
            return None, "no started_at", []

        try:
            started_at = datetime.fromisoformat(started_at_str)
        except ValueError:
            logger.warning(
                "auto_summary_invalid_started_at session_id=%s value=%r",
                session_id,
                started_at_str,
            )
            return None, "no started_at", []
        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=timezone.utc)

        chunks = await app.storage.recall_chunks(
            since=started_at,
            namespace_filter=NamespaceFilter(namespaces=(namespace,)),
            limit=max(cfg.min_chunks * 4, 200),
            project_context_root=project_context_root,
        )

    if len(chunks) < cfg.min_chunks:
        return None, "below min_chunks", []

    try:
        summary = await summarize_session(
            session_id,
            chunks,
            llm=llm,
            max_tokens=cfg.max_summary_tokens,
            max_input_chars=cfg.max_input_chars,
        )
    except SessionTooLargeError as exc:
        logger.info("auto_summary_skipped session_id=%s reason=%s", session_id, exc)
        return None, "too large", []
    except Exception:
        logger.warning(
            "auto_summary_llm_failed session_id=%s",
            session_id,
            exc_info=True,
        )
        return None, "llm error", []

    if not summary:
        return None, "empty output", []
    return summary, None, chunks


def _collect_provenance_chunk_ids(events: list[dict]) -> tuple[list[UUID], str | None]:
    """Return the distinct ids from marked write-provenance events.

    ``session_events.chunk_ids`` is shared with query/read events, so the
    event marker — not a non-empty id list — is the authority. ``None`` is
    not used for the empty case: a marked session with zero write events
    authoritatively wrote nothing and must not widen into the legacy
    namespace/time-window scan.

    The failure reason is a fixed diagnostic token. Raw event values may
    be caller supplied and can be secret-shaped, so they are never copied
    into logs.
    """
    chunk_ids: list[UUID] = []
    seen: set[UUID] = set()

    for event in events:
        metadata = event.get("metadata")
        if not isinstance(metadata, dict) or metadata.get("provenance") != PROVENANCE_KIND:
            continue

        raw_ids = event.get("chunk_ids")
        chunk_count = metadata.get("chunk_count")
        if (
            not isinstance(raw_ids, list)
            or not isinstance(chunk_count, int)
            or isinstance(chunk_count, bool)
            or chunk_count < 0
        ):
            return [], "malformed_event"
        if metadata.get("truncated") or chunk_count != len(raw_ids):
            return [], "incomplete_event"

        for raw_id in raw_ids:
            if not isinstance(raw_id, str):
                return [], "malformed_chunk_id"
            try:
                chunk_id = UUID(raw_id)
            except ValueError:
                return [], "malformed_chunk_id"
            if chunk_id not in seen:
                seen.add(chunk_id)
                chunk_ids.append(chunk_id)

    return chunk_ids, None


async def _persist_session_summary_chunk(
    app: AppContext,
    *,
    session_id: str,
    agent_id: str | None,
    summary: str,
    event_counts: dict[str, int],
    force_unsafe: bool = False,
) -> tuple[str | None, UUID | None]:
    """Promote a session summary to a first-class chunk.

    Writes the markdown file at
    ``<memory_dir>/sessions/<YYYY-MM>/<session_id>.md`` and indexes it
    under ``archive:session:<session_id>``. The ``archive:`` prefix is
    a default system namespace, so the chunk is hidden from
    ``mem_search`` unless the caller passes an explicit
    ``namespace_filter``. Returns ``(status_line, summary_chunk_id)``;
    both are ``None`` when no memory directory is configured or when
    the indexer rejected the file (zero chunks). When the summary
    body landed as multiple chunks (uncommon for a short narrative),
    the returned id is the first one — Phase B-2's link writer attaches
    its rows to that anchor.
    """
    memory_dirs = app.config.indexing.memory_dirs
    if not memory_dirs:
        return None, None

    # Validate the derived namespace before doing any I/O so an invalid
    # session_id (defensive — uuid4 is safe in practice) can't leave a
    # half-written file behind.
    namespace = f"archive:session:{session_id}"
    validate_namespace(namespace)

    # Primary memory dir: when multiple are configured, summaries land
    # under the first one. Keeps the location predictable across runs;
    # users with multi-dir setups can re-home via memory_dirs ordering.
    base = Path(memory_dirs[0]).expanduser().resolve()
    now = datetime.now(timezone.utc)
    target = base / "sessions" / now.strftime("%Y-%m") / f"{session_id}.md"

    event_total = sum(event_counts.values())
    content = _format_session_summary(
        session_id=session_id,
        agent_id=agent_id,
        ended_at=now,
        event_total=event_total,
        summary=summary,
    )

    from memtomem.config import classify_scope
    from memtomem.context._atomic import atomic_write_text
    from memtomem.privacy import enforce_write_guard

    scope, _ = classify_scope(target, app.config.indexing.project_memory_dirs)
    guard = enforce_write_guard(
        content,
        surface="mcp_session_end",
        force_unsafe=force_unsafe,
        scope=scope,
        audit_context={"session_id": session_id},
    )
    if guard.decision.startswith("blocked"):
        return (
            "Session summary blocked by the redaction guard; no file was written.",
            None,
        )
    await asyncio.to_thread(atomic_write_text, target, content, 0o600)
    stats = await app.index_engine.index_file(target, namespace=namespace, already_scanned=True)
    app.search_pipeline.invalidate_cache()

    if not stats.indexed_chunks:
        # File written but indexer produced no chunks (empty body, dedup
        # collision, or a chunker reject). Surface the path so an
        # operator can investigate; suppress the misleading "0 chunks"
        # status line in the tool response.
        logger.warning(
            "session_summary_chunk_indexed_zero session_id=%s path=%s",
            session_id,
            target,
        )
        return None, None

    summary_chunk_id = stats.new_chunk_ids[0] if stats.new_chunk_ids else None
    return f"- Summary chunk: {namespace} ({stats.indexed_chunks} chunks)", summary_chunk_id


async def _write_summary_links(
    app: AppContext,
    *,
    summary_chunk_id: UUID,
    source_chunks: list,
    cap: int,
) -> None:
    """Write ``link_type="summarizes"`` rows from the summary back to sources.

    One row per source chunk capped to ``cap`` (RFC Open-Question-1).
    ``source_chunks`` arrives newest-first from ``recall_chunks`` so
    truncating with ``[:cap]`` keeps the most recent activity and drops
    the tail — the design choice the RFC settled on. Each row is
    independently best-effort: a single ``add_chunk_link`` failure
    (validation, primary-key collision under concurrent re-summary,
    etc.) is logged and skipped rather than aborting the rest.
    """
    if cap <= 0 or not source_chunks:
        return
    for source_chunk in source_chunks[:cap]:
        try:
            await app.storage.add_chunk_link(
                source_id=summary_chunk_id,
                target_id=source_chunk.id,
                link_type="summarizes",
                namespace_target=source_chunk.metadata.namespace or "default",
            )
        except Exception:
            logger.warning(
                "summary_link_write_failed summary=%s target=%s",
                summary_chunk_id,
                source_chunk.id,
                exc_info=True,
            )


def _format_session_summary(
    *,
    session_id: str,
    agent_id: str | None,
    ended_at: datetime,
    event_total: int,
    summary: str,
) -> str:
    """Render the session-summary markdown body.

    Layout: YAML frontmatter (session_id / agent_id / ended_at /
    event_count) → ``## Session summary: <id>`` heading → blockquote
    tags (``session-summary`` plus ``agent=<id>`` when an agent owned
    the session) → summary body. Matches the chunker's expected entry
    shape (heading + blockquote group + body) so both frontmatter and
    per-section tags promote cleanly to ``ChunkMetadata.tags``.

    ``agent_id`` is preserved as ``None`` rather than coerced to
    ``"default"``: since #1875 the literal never reaches here — it
    normalizes to ``None`` at bind time (see
    :func:`memtomem.constants.normalize_bound_agent_id`) — so
    ``agent_id: null`` in the frontmatter means the session genuinely
    had no agent owner, and the ``agent=<id>`` tag is omitted rather
    than emitted as a meaningless ``agent=default``.
    """
    iso_ts = ended_at.isoformat(timespec="seconds")
    tag_list = ["session-summary"]
    if agent_id:
        tag_list.append(f"agent={agent_id}")
    tags_json = json.dumps(tag_list)
    fm_agent = agent_id if agent_id else "null"
    body = summary.strip()
    return (
        f"---\n"
        f"session_id: {session_id}\n"
        f"agent_id: {fm_agent}\n"
        f"ended_at: {iso_ts}\n"
        f"event_count: {event_total}\n"
        f"---\n"
        f"\n"
        f"## Session summary: {session_id}\n"
        f"\n"
        f"> tags: {tags_json}\n"
        f"\n"
        f"{body}\n"
    )


@mcp.tool()
@tool_handler
@register("sessions")
async def mem_session_list(
    agent_id: str | None = None,
    since: str | None = None,
    limit: int = 10,
    ctx: CtxType = None,
) -> str:
    """List recent episodic memory sessions.

    Args:
        agent_id: Filter by agent (omit for all agents)
        since: Only sessions started after this date (YYYY-MM-DD or ISO)
        limit: Maximum sessions to return (default 10)
    """
    if not 1 <= limit <= 200:
        return f"Error: limit must be between 1 and 200, got {limit}."

    app = await _get_app_initialized(ctx)
    sessions = await app.storage.list_sessions(agent_id=agent_id, since=since, limit=limit)

    if not sessions:
        return "No sessions found."

    lines = [f"Sessions: {len(sessions)}\n"]
    for s in sessions:
        status = "active" if s["ended_at"] is None else "ended"
        meta = s.get("metadata") or {}
        if isinstance(meta, str):
            import json

            try:
                meta = json.loads(meta)
            except (json.JSONDecodeError, TypeError):
                meta = {}
        title = meta.get("title", "")
        label = f' "{title}"' if title else ""
        summary = f" — {s['summary'][:60]}..." if s.get("summary") else ""
        lines.append(
            f"  [{status}] {s['id'][:8]}...{label} ({s['agent_id']}) {s['started_at']}{summary}"
        )

    return "\n".join(lines)
