"""Tool: mem_hermes_mirror_apply — mirror Hermes's built-in memory writes (via mem_do).

The ``memtomem-memory`` Hermes provider sends one call per committed write to
Hermes's built-in memory (``MEMORY.md`` / ``USER.md``): add, replace or remove
of one entry. The store tables are the source of truth (a last-writer-wins
element set, see ``storage/mixins/hermes_mirror.py``); the Markdown file
``<first user memory dir>/hermes-mirror/<owner>/<target>.md`` is rendered from
them and indexed like any other memory file.

One call, in order:

1. Validate, scan new content with the write guard, and pick the destination.
   A destination that is excluded, read-only, a link, or outside the user tier
   is refused here, and nothing is stored.
2. Take the file's in-process lock and its cross-process sidecar, then check
   the destination again under them.
3. Commit the operation in one store transaction. This is the commit point:
   ``deadline_ms`` bounds only the waits before it.
4. Render and index the file, still under the locks. A failure here leaves the
   operation committed and the target marked pending; the reply says
   ``rendered: false`` and the next call or ``op="reconcile"`` renders again.

A parent directory swapped for a link after the check under the lock is not
caught, the same as for every other memory writer: the sidecar serializes
cooperating writers, it does not stop a directory rename.
"""

from __future__ import annotations

import asyncio
import contextvars
import functools
import hashlib
import json
import logging
import re
import time
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from memtomem import privacy
from memtomem.errors import ConfigError, RetryableError, StorageError, TransactionOwnedError
from memtomem.memory_scope import MemoryScopeError, require_user_base, resolve_scope_target
from memtomem.server.context import AppContext, CtxType, _get_app_initialized
from memtomem.server.error_handler import tool_handler
from memtomem.server.helpers import _check_embedding_mismatch
from memtomem.server.tool_registry import register
from memtomem.server.tools._provenance import (
    capture_session_for_untracked_write,
    flag_untracked_write,
)
from memtomem.source_provenance import (
    EXCLUDED_TARGET_DETAIL,
    READ_ONLY_TARGET_DETAIL,
    refuse_replace_target,
)
from memtomem.storage.base import WRITER_WAIT_S
from memtomem.storage.mixins.hermes_mirror import MirrorEntry, MirrorWrite
from memtomem.storage.sqlite_errors import is_writer_busy

logger = logging.getLogger(__name__)

MIRROR_DIR = "hermes-mirror"
REPLY_SCHEMA_VERSION = 1
MAX_ENTRY_BYTES = 64 * 1024

_OWNER_RE = re.compile(r"[a-f0-9]{16,64}")
_TARGET_RE = re.compile(r"[a-z][a-z0-9_-]{0,31}")
_OPS = frozenset({"add", "remove", "replace", "reconcile", "purge"})
_OP_LABEL = "hermes_mirror_apply"

_SYMLINK_TARGET_DETAIL = (
    "mirror_target_link: a link sits on the mirror file's path, so memtomem will not "
    "write through it. Nothing was written. Remove the link under the memory directory's "
    f"{MIRROR_DIR}/ folder."
)


def entry_key(text: str) -> str:
    """The identity of one native entry: the sha256 of its exact text."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def mirror_relpath(owner: str, mirror_target: str) -> Path:
    return Path(MIRROR_DIR) / owner / f"{mirror_target}.md"


def render_mirror_markdown(mirror_target: str, entries: list[MirrorEntry]) -> str:
    """The derived document: a fixed header, then one section per live entry.

    Deterministic for a given entry list, so an entry that did not change keeps
    its chunk's hash across renders. Each section is headed by the entry key's
    first 12 hex digits: stable, and no copy of the entry text in the heading.
    """
    parts = [
        f"# Hermes {mirror_target} mirror\n",
        "\n",
        "Rendered by memtomem from Hermes's built-in memory. Hermes's store is "
        "authoritative; edits to this file are overwritten.\n",
    ]
    for entry in entries:
        body = entry.content.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
        parts.append(f"\n## Entry {entry.entry_key[:12]}\n\n{body}\n")
    return "".join(parts)


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _entry_error(name: str, value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return f"Error: {name} must be a non-empty string."
    if len(value.encode("utf-8")) > MAX_ENTRY_BYTES:
        return f"Error: {name} is longer than {MAX_ENTRY_BYTES} bytes."
    return None


def _validate(
    owner: Any,
    mirror_target: Any,
    op: Any,
    content: Any,
    previous_content: Any,
    ts: Any,
    deadline_ms: Any,
) -> str | None:
    if not isinstance(owner, str) or not _OWNER_RE.fullmatch(owner):
        return "Error: owner must be 16-64 lowercase hex characters."
    if not isinstance(mirror_target, str) or not _TARGET_RE.fullmatch(mirror_target):
        return "Error: target must match [a-z][a-z0-9_-]{0,31}."
    if op not in _OPS:
        return f"Error: op must be one of {sorted(_OPS)}."
    if op in ("add", "replace"):
        err = _entry_error("content", content)
        if err:
            return err
    if op in ("remove", "replace"):
        err = _entry_error("previous_content", previous_content)
        if err:
            return err
    if op != "reconcile" and ts is None:
        # Purge too: its fence is compared with the provider's timestamps, so it must come
        # from the same clock, not from this server's (which may run on another host).
        return f"Error: ts is required for op={op!r}."
    if ts is not None and (not _is_int(ts) or ts < 0):
        return "Error: ts must be a non-negative integer (nanoseconds)."
    if deadline_ms is not None and (not _is_int(deadline_ms) or deadline_ms < 1):
        return "Error: deadline_ms must be a positive integer."
    return None


def _remaining(deadline: float | None) -> float | None:
    return None if deadline is None else deadline - time.monotonic()


def _expired(stage: str) -> RetryableError:
    return RetryableError(f"deadline exceeded before the commit ({stage}); nothing was stored")


def _destination_refusal(
    app: AppContext, base: Path, dest: Path, owner: str, mirror_target: str
) -> str | None:
    """Why ``dest`` may not take the mirror file, judged on the path as given.

    ``refuse_replace_target`` judges the leaf link and the read-only and exclude
    rules on the lexical path; ``resolve_scope_target`` then must land exactly
    on it, so a link anywhere under ``hermes-mirror/`` refuses too.
    """
    why = refuse_replace_target(dest, app.index_engine)
    if why == "symlink":
        return f"Error: {_SYMLINK_TARGET_DETAIL}"
    if why == "excluded":
        return f"Error: {EXCLUDED_TARGET_DETAIL}"
    if why == "read_only":
        return f"Error: {READ_ONLY_TARGET_DETAIL}"
    try:
        resolved = resolve_scope_target(
            base,
            mirror_relpath(owner, mirror_target),
            scope="user",
            project_memory_dirs=app.config.indexing.project_memory_dirs,
        )
    except MemoryScopeError as exc:
        return f"Error: {exc}"
    if resolved != dest:
        return f"Error: {_SYMLINK_TARGET_DETAIL}"
    return None


async def _commit(
    app: AppContext,
    *,
    owner: str,
    mirror_target: str,
    op: str,
    writes: list[MirrorWrite],
    previous_key: str | None,
    ts: int,
    deadline: float | None,
) -> dict[str, Any]:
    remaining = _remaining(deadline)
    if remaining is not None and remaining <= 0:
        raise _expired("store writer")
    storage = app.storage
    try:
        # Every transaction in this repo queues for the writer on WRITER_WAIT_S;
        # the deadline is checked before and after, never used as the wait.
        async with storage.transaction(wait=WRITER_WAIT_S):
            # The writer queue and ``BEGIN IMMEDIATE``'s busy wait are not bounded
            # by the budget; a deadline that passed meanwhile stops here.
            if deadline is not None and time.monotonic() >= deadline:
                raise _expired("store writer")
            if op == "reconcile":
                # An explicit reconcile rewrites the file even when nothing is pending: it
                # may have been edited or deleted since its last render.
                await storage.hermes_mirror_mark_pending(owner, mirror_target)
                return {"fenced": False, "changed": False, "previous_matched": None}
            if op == "purge":
                return await storage.hermes_mirror_purge(owner, mirror_target, ts)
            return await storage.hermes_mirror_apply(
                owner, mirror_target, writes, previous_key=previous_key
            )
    except (TransactionOwnedError, StorageError) as exc:
        if not is_writer_busy(exc):
            raise
        raise RetryableError(f"the store's writer is busy; nothing was stored ({exc})") from exc


async def _render(
    app: AppContext, *, owner: str, mirror_target: str, dest: Path, session_id: str | None
) -> dict[str, Any]:
    """Render the target if it is pending; the caller holds the file's locks."""
    from memtomem.context._atomic import atomic_write_text
    from memtomem.indexing.engine import PrivacyRejection
    from memtomem.tools.memory_mutation import preflight_refusal

    storage = app.storage
    out: dict[str, Any] = {
        "live_entries": 0,
        "revision": None,
        "rendered": False,
        "rendered_revision": 0,
        "render_error": None,
    }

    def failed(reason: str) -> dict[str, Any]:
        logger.warning("hermes mirror %s/%s not rendered: %s", owner[:8], mirror_target, reason)
        out.update(rendered=False, render_error=reason)
        return out

    # Everything here runs after the commit: a failure at any step is reported as
    # "accepted, not rendered", never raised as if nothing had been stored.
    try:
        state = await storage.hermes_mirror_target_state(owner, mirror_target)
        revision, entries = await storage.hermes_mirror_snapshot(owner, mirror_target)
        out.update(
            live_entries=len(entries),
            revision=revision,
            rendered_revision=state["rendered_revision"] if state else 0,
        )
        if state is None or not state["render_pending"]:
            out.update(rendered=True)
            return out
        refusal, _ = await preflight_refusal(
            app.index_engine, dest, op=_OP_LABEL, needs_valve=False, surface="mcp"
        )
        if refusal is not None:
            return failed(refusal)
        text = render_mirror_markdown(mirror_target, entries)
        try:
            await _write_draining(atomic_write_text, dest, text)
            stats = await app.index_engine.index_file(dest, ingress_guarded=True, lock_held=True)
            # Mirror entries are not a session's own writes: tell an open session its
            # provenance does not describe this change, as the bulk importers do.
            await flag_untracked_write(app, session_id)
        finally:
            app.search_pipeline.invalidate_cache()
    except PrivacyRejection as exc:
        return failed(exc.mutation_detail(_OP_LABEL, outcome="appended", surface="mcp"))
    except Exception as exc:  # the operation is committed; report, never raise
        return failed(f"{type(exc).__name__}: {getattr(exc, 'public_message', None) or exc}")
    if stats.errors or stats.retryable_errors or stats.scope_held:
        return failed("; ".join(stats.errors) or "the file's index update did not complete")
    try:
        # Indexing can succeed while the file stays held from search: the engine defers a
        # hold release it could not take under contention and reports no error for it.
        from memtomem.storage.sqlite_helpers import norm_path

        still_hidden = await storage.is_source_held(Path(norm_path(dest)))
    except Exception as exc:
        return failed(f"{type(exc).__name__}: cannot confirm the file is searchable")
    if still_hidden:
        return failed("the file is indexed but still held from search; reconcile again later")
    try:
        async with storage.transaction(wait=WRITER_WAIT_S):
            await storage.hermes_mirror_mark_rendered(owner, mirror_target, revision)
    except Exception as exc:
        # Rendered and indexed; only the bookkeeping is late. The flag stays
        # set and the next call renders the same bytes again (a receipt hit).
        logger.info("hermes mirror render acknowledgement deferred: %s", exc)
    out.update(rendered=True, rendered_revision=revision)
    return out


async def _write_draining(fn: Any, *args: Any) -> None:
    """Run a file write on a thread; a cancel waits for the thread before propagating.

    The caller holds the file's locks. Released while the thread still writes, a later
    render could land its newer bytes first and be overwritten by this older ones.

    The worker is an executor future, not a Task: ``asyncio.run`` cancels every Task
    when it shuts down, and a cancelled Task wrapping the thread reports done while
    the thread is still running.
    """
    loop = asyncio.get_running_loop()
    # ``asyncio.to_thread`` carries the caller's contextvars; keep that.
    ctx = contextvars.copy_context()
    future = loop.run_in_executor(None, functools.partial(ctx.run, fn, *args))
    try:
        await asyncio.shield(future)
    except asyncio.CancelledError:
        while not future.done():
            try:
                await asyncio.wait({future})
            except asyncio.CancelledError:
                continue
        if not future.cancelled():
            future.exception()  # retrieved; the cancel is what propagates
        raise


@tool_handler
@register("ingest")
async def mem_hermes_mirror_apply(
    owner: str,
    target: str,
    op: str,
    content: str | None = None,
    previous_content: str | None = None,
    ts: int | None = None,
    deadline_ms: int | None = None,
    ctx: CtxType = None,
) -> str:
    """Apply one Hermes built-in memory write to its memtomem mirror.

    Called by the memtomem-memory Hermes provider, one call per committed write
    to Hermes's MEMORY.md / USER.md. Entries are kept last-writer-wins by ``ts``
    and rendered to ``hermes-mirror/<owner>/<target>.md`` under the first user
    memory directory. Returns JSON.

    Args:
        owner: Opaque hex id of the Hermes profile (16-64 chars).
        target: Hermes memory target, e.g. ``memory`` or ``user``.
        op: ``add``, ``remove``, ``replace``, ``reconcile`` (render only) or ``purge``.
        content: The entry's new text (add, replace).
        previous_content: The entry's text before the write (remove, replace).
        ts: Write time in ns on the Hermes host's clock; newer wins (purge too).
        deadline_ms: Budget for the waits before the commit; expiry stores nothing.
    """
    started = time.monotonic()
    err = _validate(owner, target, op, content, previous_content, ts, deadline_ms)
    if err:
        return err
    app = await _get_app_initialized(ctx)
    mismatch = _check_embedding_mismatch(app)
    if mismatch:
        return mismatch
    deadline = None if deadline_ms is None else started + deadline_ms / 1000

    # ``_validate`` guaranteed these for the ops that use them; the ``or`` defaults only
    # give the type checker non-optional values.
    new_text = content or ""
    old_text = previous_content or ""
    write_ts = ts or 0
    content_refused = False
    writes: list[MirrorWrite] = []
    previous_key: str | None = None
    if op in ("add", "replace"):
        guard = privacy.enforce_write_guard(
            new_text,
            surface="mem_hermes_mirror_apply",
            scope="user",
            audit_context={"owner": owner, "target": target, "op": op},
        )
        if guard.decision not in ("pass", "exempted"):
            if op == "add":
                return (
                    f"Error: content matches {len(guard.hits)} privacy pattern(s); "
                    "mirror write refused. Nothing was stored."
                )
            # The native store no longer has the old entry: its tombstone still
            # lands, so a refused replacement never leaves it searchable.
            content_refused = True
    if op in ("remove", "replace"):
        previous_key = entry_key(old_text)
        writes.append(MirrorWrite(previous_key, "", write_ts, True))
    if op in ("add", "replace") and not content_refused:
        writes.append(MirrorWrite(entry_key(new_text), new_text, write_ts, False))
    commit_ts = ts if ts is not None else 0

    try:
        base = require_user_base(
            app.config.indexing.memory_dirs, app.config.indexing.project_memory_dirs
        )
    except ConfigError as exc:
        return f"Error: {exc}"
    dest = base / mirror_relpath(owner, target)
    refusal = _destination_refusal(app, base, dest, owner, target)
    if refusal:
        return refusal

    from memtomem.context._atomic import (
        _CRUD_SIDECAR_LOCK_BUDGET_S,
        async_file_lock,
        memory_lock_path,
    )

    # Before any await of the write, so the flag lands on the session open now.
    session_id = await capture_session_for_untracked_write(app)
    async with app.write_in_flight(), AsyncExitStack() as stack:
        remaining = _remaining(deadline)
        try:
            async with asyncio.timeout(remaining):
                await stack.enter_async_context(app.get_memory_file_lock(dest))
        except TimeoutError:
            raise _expired("file lock") from None
        remaining = _remaining(deadline)
        if remaining is not None and remaining <= 0:
            raise _expired("file lock")
        budget = (
            _CRUD_SIDECAR_LOCK_BUDGET_S
            if remaining is None
            else min(_CRUD_SIDECAR_LOCK_BUDGET_S, remaining)
        )
        try:
            await stack.enter_async_context(async_file_lock(memory_lock_path(dest), timeout=budget))
        except TimeoutError:
            raise RetryableError(
                "another writer holds the mirror file; nothing was stored"
            ) from None
        refusal = _destination_refusal(app, base, dest, owner, target)
        if refusal:
            return refusal
        committed = await _commit(
            app,
            owner=owner,
            mirror_target=target,
            op=op,
            writes=writes,
            previous_key=previous_key,
            ts=commit_ts,
            deadline=deadline,
        )
        if op != "reconcile":
            if committed["fenced"]:
                return (
                    "Error: purged: this operation is not newer than the last purge of "
                    "this mirror target. Nothing was stored."
                )
        rendered = await _render(
            app, owner=owner, mirror_target=target, dest=dest, session_id=session_id
        )

    previous: str | None = None
    if committed.get("previous_matched") is not None:
        previous = "matched" if committed["previous_matched"] else "unmatched"
    # The committed revision when this call changed state; otherwise the revision its render
    # read. Never a fresh read here: after the locks another call's revision may be current.
    accepted = committed.get("revision")
    if accepted is None:
        accepted = rendered.get("revision")
    rendered = {k: v for k, v in rendered.items() if k != "revision"}
    reply = {
        "schema_version": REPLY_SCHEMA_VERSION,
        "op": op,
        "applied": op != "reconcile",
        "changed": bool(committed["changed"]),
        "accepted_revision": accepted,
        "previous": previous,
        "content_refused": content_refused,
        **rendered,
    }
    return json.dumps(reply)
