"""Write provenance: what a session actually wrote (issue #1876).

``_maybe_auto_summarize`` used to choose the chunks it summarizes by the
session row's namespace. For an unbound session the writes are not
pinned to that namespace — the indexing engine resolves it from
namespace policy rules, the parent folder, or ``default_namespace`` — so
the recall missed them and the session ended with no summary.

The fix is to stop inferring and start recording. Every chunk-creating
MCP write surface appends one ``session_events`` row naming the chunk
ids it just created; the auto-summary reads those ids back instead of
guessing a namespace.

Three properties this module exists to hold:

**Only marked events count.** ``session_events.chunk_ids`` is a general
column — the LangGraph adapter lets a caller attach *recall* result ids
to a ``"query"`` event. Reading writes off the bare presence of
``chunk_ids`` would summarize what the session read. Consumers must
filter on ``metadata["provenance"] == PROVENANCE_KIND``, never on the
column being non-empty.

**One event per write call.** An earlier draft sharded large id lists
across several events. That turns ``event_counts`` from a count of
logical writes into a count of pages — one ``mem_index`` showing up as
twenty ``index`` events — and that number is rendered by
``mem_session_end``, stored in the session metadata, written into the
archive frontmatter, and reported by ``mm session show``, the web totals
and ``langgraph.end_agent_session``. Keeping it 1:1 leaves all six
consumers untouched.

**A short list says so.** The session row carries a marker meaning
"this session records provenance" — *not* "this provenance is
complete". Completeness is decided by ``provenance_incomplete`` alone,
which is set on truncation, on a failed event write, when the write
outran session teardown, and by the mutation surfaces below. A consumer
that trusts the marker without checking the flag will present a partial
input set as the whole story.

``mem_edit`` and ``mem_delete`` record no event and instead set the flag
unconditionally. They do produce ``new_chunk_ids``, but an edit
re-chunks the file: those ids are replacements, not additions, and
feeding them to a summary would describe a rewrite as new material —
while the ids an earlier write recorded for the same file have just gone
dangling. Silently skipping them is the one thing that is not allowed:
an edit-only session would then carry the marker, report zero writes,
and offer no dangling id for the consumer to notice, so "wrote nothing"
would read as fact.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from memtomem.server.tools.multi_agent import _resolve_agent_namespace

if TYPE_CHECKING:
    from memtomem.models import IndexingStats
    from memtomem.server.context import AppContext

logger = logging.getLogger(__name__)

PROVENANCE_KIND = "write-v1"

# Cap on ids carried by a single event. ``chunk_ids`` is TEXT, so 10k
# UUID strings (~370 KB) fits without a row-size problem, and a write
# that large already trips the summary's own input-size gate. The cost
# is on the read side — ``get_session_events`` re-parses every row on
# each session teardown — which is why this is a cap and not a target.
# Lowering it is not free: past the cap a session is marked incomplete,
# and a consumer treats an incomplete session more conservatively.
MAX_IDS_PER_EVENT = 10_000


def render_event_content(event_type: str, chunk_count: int, *, truncated: bool) -> str:
    """Build the event's ``content``: a fixed descriptor, never prose.

    ``formation.scan_session_candidates`` regex-classifies *every*
    session event's ``content`` into review candidates and copies it
    verbatim into both the candidate body and its proposed diff, and the
    web API renders it. So this string must never carry the note body, a
    filesystem path, or a URL, and its vocabulary must not collide with
    the classifier's patterns. ``test_write_provenance`` pins that for
    every event type rather than leaving it to inspection.
    """
    base = f"{PROVENANCE_KIND} {event_type} chunks={chunk_count}"
    return f"{base} truncated={MAX_IDS_PER_EVENT}" if truncated else base


async def capture_session_and_namespace(
    app: AppContext, namespace: str | None
) -> tuple[str | None, str | None]:
    """Read the active session id and resolve the namespace as one unit.

    Both reads have to happen under a single ``_session_lock``
    acquisition. ``_resolve_agent_namespace`` is synchronous and takes no
    lock of its own, so splitting them lets a session transition land
    between the two: the chunks would be written under the new session's
    namespace and attributed to the old session's provenance, which is
    the same class of mismatch #1876 is about.

    Call this at the point the namespace is resolved — for the CRUD
    surfaces that is *inside* the file lock, deliberately: waiting on the
    lock is a suspension point, and the write must be attributed to the
    session live when it actually happened, not the one live when the
    tool was invoked.
    """
    async with app._session_lock:
        session_id = app.current_session_id
        resolved = _resolve_agent_namespace(app, None)
    return session_id, (namespace or resolved)


async def mark_provenance_incomplete(app: AppContext, session_id: str | None) -> None:
    """Record that this session's provenance is not the whole story.

    Best effort and never raises: it runs alongside a memory write that
    has already landed on disk and in the index, and bookkeeping must not
    fail it. The ERROR level is deliberate and distinct — this is the one
    state where a consumer can be actively *wrong* rather than merely
    conservative, because the session still claims to record provenance
    while nothing says the record is short.
    """
    if session_id is None:
        return
    try:
        await app.storage.update_session_metadata(session_id, {"provenance_incomplete": True})
    except Exception:
        logger.error("provenance_flag_write_failed session_id=%s", session_id, exc_info=True)


async def record_write_provenance(
    app: AppContext,
    *,
    session_id: str | None,
    event_type: str,
    stats: IndexingStats | None,
) -> None:
    """Append one provenance event naming the chunks this write created.

    ``session_id`` is passed in, never read from ``app`` here: the caller
    captured it at write time via :func:`capture_session_and_namespace`,
    and re-reading it now would undo that.

    Never raises. A failed event write is logged and downgraded to
    :func:`mark_provenance_incomplete` — a consumer that sees the flag
    falls back instead of trusting a short list.
    """
    if session_id is None:
        return
    if stats is None:
        return
    new_ids: tuple[Any, ...] = tuple(getattr(stats, "new_chunk_ids", ()) or ())
    if not new_ids:
        return

    chunk_count = len(new_ids)
    truncated = chunk_count > MAX_IDS_PER_EVENT
    # ``IndexingStats.new_chunk_ids`` holds UUIDs; ``add_session_event``
    # hands the list straight to ``json.dumps``, which cannot serialize
    # them. ``chunk_count`` stays the *true* count so a consumer knows
    # how much a truncated event lost.
    recorded = [str(cid) for cid in (new_ids[:MAX_IDS_PER_EVENT] if truncated else new_ids)]
    metadata: dict[str, Any] = {"provenance": PROVENANCE_KIND, "chunk_count": chunk_count}
    if truncated:
        metadata["truncated"] = True

    try:
        await app.storage.add_session_event(
            session_id,
            event_type,
            render_event_content(event_type, chunk_count, truncated=truncated),
            recorded,
            metadata,
        )
    except Exception:
        logger.warning(
            "provenance_event_write_failed session_id=%s event_type=%s",
            session_id,
            event_type,
            exc_info=True,
        )
        await mark_provenance_incomplete(app, session_id)
        return

    # Did this write outrun its session? The drain only waits for writes
    # admitted before it started, and the session handle stays live
    # through teardown, so a write can legitimately land its event after
    # teardown snapshotted the event list. Checked *after* the write, so
    # a claim taken while the write was in flight is still seen.
    async with app._session_lock:
        sealed = session_id in app._ending_session_ids or app.current_session_id != session_id

    if truncated or sealed:
        await mark_provenance_incomplete(app, session_id)


async def capture_session_for_untracked_write(app: AppContext) -> str | None:
    """Read the active session id before an operation that changes the
    session's inputs without recording them.

    Read *before* the operation's awaits, not after: reading afterwards
    would let a session that ended meanwhile lose the flag entirely, or a
    session that started meanwhile inherit the previous one's mutation.
    That is the same attribution mistake this issue exists to fix, so it
    must not be reintroduced by the code fixing it.
    """
    async with app._session_lock:
        return app.current_session_id


async def flag_untracked_write(app: AppContext, session_id: str | None) -> None:
    """Tell a session its provenance does not describe everything that
    happened inside it.

    Used by the surfaces that change a session's chunk set without
    recording a provenance event: ``mem_edit`` and ``mem_delete``, whose
    ``new_chunk_ids`` are re-chunk artifacts rather than new material
    (summarizing them would describe a rewrite as something newly
    written, while the ids an earlier write recorded for the same file
    have just gone dangling); the bulk delete branches, which remove
    chunks an earlier event still names; and the bulk importers, whose
    output is an ingest rather than session work.

    Staying silent is the one option not available for any of them. The
    session would carry the provenance marker, report only the writes
    that *were* tracked, and leave nothing for a consumer to trip over —
    so a partial record would read as a complete one. The flag makes the
    gap visible without pretending to describe it.

    No write gauge on these paths on purpose: they write no session
    *event*, and the flag lands correctly even on an already-ended row,
    so there is nothing for session teardown to snapshot around.
    """
    await mark_provenance_incomplete(app, session_id)
