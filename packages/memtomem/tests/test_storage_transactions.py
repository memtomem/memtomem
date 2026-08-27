"""Regression coverage for task-affine SQLite transactions (#1896)."""

import asyncio
import logging
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest


from helpers import make_chunk
from memtomem.errors import StorageError


async def test_transaction_takes_immediate_write_lock_on_entry(storage):
    contender = sqlite3.connect(str(storage._config.sqlite_path), timeout=0)
    try:
        async with storage.transaction():
            assert storage._get_db().in_transaction is True
            with pytest.raises(sqlite3.OperationalError, match="(?i)(locked|busy)"):
                contender.execute("BEGIN IMMEDIATE")

        assert storage._get_db().in_transaction is False
    finally:
        if contender.in_transaction:
            contender.rollback()
        contender.close()


async def test_foreign_task_write_fails_closed_then_can_retry(storage):
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.create_session("rolled-back", "owner", "default")
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.create_session("survivor", "other", "default")
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.get_session("rolled-back")
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_session("rolled-back") is None
    assert await storage.get_session("survivor") is None

    await storage.create_session("survivor", "other", "default")
    survivor = await storage.get_session("survivor")
    assert survivor is not None
    assert survivor["agent_id"] == "other"
    assert storage._get_db().in_transaction is False


async def test_foreign_unconditional_committer_cannot_flush_owner(storage):
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.create_session("owner-row", "owner", "default")
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.set_namespace_meta("foreign", description="other task")
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_session("owner-row") is None
    assert await storage.get_namespace_meta("foreign") is None

    await storage.set_namespace_meta("foreign", description="retry")
    metadata = await storage.get_namespace_meta("foreign")
    assert metadata is not None
    assert metadata["description"] == "retry"


async def test_foreign_namespace_writers_do_not_borrow_owner_transaction(storage):
    """Namespace writers must not report success inside another task's txn.

    These three methods suppress their own commit when they borrow an outer
    transaction. A backend-wide ownership flag used to let a foreign task take
    that branch, after which the owner's rollback silently discarded the
    foreign task's successful result.
    """
    delete_me = make_chunk(content="delete candidate", namespace="delete-ns")
    assign_me = make_chunk(content="assign candidate", namespace="assign-src")
    await storage.upsert_chunks([delete_me, assign_me])

    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.create_session("owner-row", "owner", "default")
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.delete_by_namespace("delete-ns")
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.set_namespace_meta("foreign-ns", description="foreign")
        with pytest.raises(StorageError, match="owned by another task"):
            await storage.assign_namespace("assign-dst", old_namespace="assign-src")
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_session("owner-row") is None
    assert await storage.get_chunk(delete_me.id) is not None
    assigned = await storage.get_chunk(assign_me.id)
    assert assigned is not None
    assert assigned.metadata.namespace == "assign-src"
    assert await storage.get_namespace_meta("foreign-ns") is None


async def test_pooled_reader_sees_only_committed_state(storage):
    chunk = make_chunk(content="uncommitted transaction row")
    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        with pytest.raises(RuntimeError, match="roll back owner"):
            async with storage.transaction():
                await storage.upsert_chunks([chunk])
                embeddings = await storage.get_embeddings_for_chunks([str(chunk.id)])
                assert str(chunk.id) in embeddings
                owner_ready.set()
                await release_owner.wait()
                raise RuntimeError("roll back owner")

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        assert await storage.get_chunk(chunk.id) is None
        assert await storage.get_embeddings_for_chunks([str(chunk.id)]) == {}
    finally:
        release_owner.set()
        await owner_task

    assert await storage.get_chunk(chunk.id) is None


async def test_writer_connection_bypasses_refuse_active_transaction(storage):
    async with storage.transaction():
        with pytest.raises(StorageError, match="rebuild_fts.*transaction is active"):
            await storage.rebuild_fts()
        with pytest.raises(StorageError, match="close.*transaction is active"):
            await storage.close()

    assert storage._get_db().in_transaction is False


async def test_self_managed_operations_reject_without_rolling_back_owner(storage):
    async with storage.transaction():
        await storage.create_session("owner-row", "owner", "default")

        with pytest.raises(
            StorageError,
            match="recover_stale_memory_candidates.*transaction is active",
        ):
            await storage.recover_stale_memory_candidates(stale_before="2026-01-01T00:00:00+00:00")
        assert storage._get_db().in_transaction is True

        with pytest.raises(
            StorageError,
            match="sweep_orphan_project_root.*transaction is active",
        ):
            await storage.sweep_orphan_project_root("/missing/project")
        assert storage._get_db().in_transaction is True

    assert await storage.get_session("owner-row") is not None


async def test_cancellation_rolls_back_and_releases_owner(storage):
    owner_ready = asyncio.Event()
    wait_forever = asyncio.Event()

    async def owner() -> None:
        async with storage.transaction():
            await storage.create_session("cancelled", "owner", "default")
            owner_ready.set()
            await wait_forever.wait()

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    owner_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await owner_task

    assert storage._transaction_owner is None
    assert storage._get_db().in_transaction is False
    assert await storage.get_session("cancelled") is None

    await storage.create_session("after-cancel", "other", "default")
    assert await storage.get_session("after-cancel") is not None


async def test_same_task_nested_transaction_raises_and_cleans_up(storage):
    with pytest.raises(StorageError, match="Nested transactions"):
        async with storage.transaction():
            async with storage.transaction():
                pass

    assert storage._transaction_owner is None
    assert storage._get_db().in_transaction is False


async def test_preexisting_connection_transaction_is_not_adopted(storage):
    db = storage._get_db()
    db.execute("BEGIN")
    try:
        with pytest.raises(StorageError, match="already has an open transaction"):
            async with storage.transaction():
                pass
        assert db.in_transaction is True
        assert storage._transaction_owner is None
    finally:
        db.rollback()


async def test_relation_and_importance_writers_roll_back_with_the_owner(storage):
    """``add_relation`` / ``update_importance_scores`` must join an open txn.

    Both used to commit unconditionally, which ended the owner's transaction
    from the inside: ``apply_consolidation`` writes its ``consolidated_into``
    edges and the originals' decay in the same transaction as the summary they
    describe, and a premature commit there is exactly the partial write #2158
    is about.
    """
    original = make_chunk(content="original chunk")
    summary = make_chunk(content="summary chunk")
    await storage.upsert_chunks([original, summary])
    await storage.update_importance_scores({str(original.id): 0.8})

    with pytest.raises(RuntimeError, match="roll back owner"):
        async with storage.transaction():
            await storage.add_relation(original.id, summary.id, "consolidated_into")
            await storage.update_importance_scores({str(original.id): 0.4})
            raise RuntimeError("roll back owner")

    assert await storage.get_related(original.id) == []
    scores = await storage.get_importance_scores([str(original.id)])
    assert scores[str(original.id)] == pytest.approx(0.8)


async def test_relation_and_importance_writers_commit_standalone(storage):
    """Outside a transaction both writers still commit on their own.

    Observed through a second connection: the writer connection would report
    its own uncommitted rows and turn a lost commit into a passing test.
    """
    original = make_chunk(content="standalone original")
    summary = make_chunk(content="standalone summary")
    await storage.upsert_chunks([original, summary])

    await storage.add_relation(original.id, summary.id, "consolidated_into")
    await storage.update_importance_scores({str(original.id): 0.25})

    observer = sqlite3.connect(str(storage._config.sqlite_path), timeout=0)
    try:
        edge = observer.execute(
            "SELECT relation_type FROM chunk_relations WHERE source_id=? AND target_id=?",
            (str(original.id), str(summary.id)),
        ).fetchone()
        score = observer.execute(
            "SELECT importance_score FROM chunks WHERE id=?",
            (str(original.id),),
        ).fetchone()
    finally:
        observer.close()

    assert edge is not None and edge[0] == "consolidated_into"
    assert score[0] == pytest.approx(0.25)


def _candidate(candidate_id: str, session_id: str = "txn-session") -> dict:
    """A minimal ``memory_candidates`` row, shaped as ``formation.py`` builds it."""
    now = datetime.now(timezone.utc)
    return {
        "id": candidate_id,
        "session_id": session_id,
        "kind": "fact",
        "operation": "add",
        "destination": "memory",
        "content": "candidate content",
        "evidence": [],
        "matched_existing_ids": [],
        "confidence": 0.5,
        "sensitivity": "normal",
        "proposed_diff": "+ candidate content",
        "extractor_version": "test-v1",
        "fingerprint": candidate_id,
        "status": "pending",
        "created_at": now.isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(days=30)).isoformat(timespec="seconds"),
    }


def _committed_rows(storage, sql: str, params: tuple) -> list[tuple]:
    """Query through a second connection.

    The writer connection reports its own uncommitted rows, which would turn a
    lost commit into a passing test.
    """
    observer = sqlite3.connect(str(storage._config.sqlite_path), timeout=0)
    try:
        return observer.execute(sql, params).fetchall()
    finally:
        observer.close()


async def _add_candidate_with_session(storage) -> bool:
    """``memory_candidates.session_id`` is a foreign key, so the session first."""
    await storage.create_session("txn-session", "tester", "default")
    return await storage.add_memory_candidate(_candidate("txn-candidate"))


async def _link_a_stored_chunk(storage) -> None:
    """``chunk_links.target_id`` is a foreign key, so the chunk first."""
    target = make_chunk(content="link target")
    await storage.upsert_chunks([target])
    await storage.add_chunk_link(None, target.id, "shared", "default")


# One writer per mixin swept in #2162, exercised through the public API:
# ``(label, run_writer, sql, params)``. The AST guard proves the *shape* holds
# for all 46 sites; these prove the shape means what it is supposed to mean.
_SWEPT_WRITERS = [
    (
        "formation",
        _add_candidate_with_session,
        "SELECT id FROM memory_candidates WHERE id=?",
        ("txn-candidate",),
    ),
    (
        "schedules",
        lambda s: s.schedule_insert("0 3 * * *", "consolidate"),
        "SELECT id FROM schedules WHERE job_kind=?",
        ("consolidate",),
    ),
    (
        "scratch",
        lambda s: s.scratch_set("txn-key", "txn-value"),
        "SELECT value FROM working_memory WHERE key=?",
        ("txn-key",),
    ),
    (
        "share_links",
        _link_a_stored_chunk,
        "SELECT link_type FROM chunk_links WHERE link_type=?",
        ("shared",),
    ),
    (
        "policies",
        lambda s: s.policy_add("txn-policy", "decay", {"half_life_days": 30}),
        "SELECT id FROM memory_policies WHERE name=?",
        ("txn-policy",),
    ),
    (
        "history",
        lambda s: s.save_query_history("txn query", [], [], []),
        "SELECT query_text FROM query_history WHERE query_text=?",
        ("txn query",),
    ),
]


@pytest.mark.parametrize(
    "label,run_writer,sql,params",
    _SWEPT_WRITERS,
    ids=[case[0] for case in _SWEPT_WRITERS],
)
async def test_swept_writers_roll_back_with_the_owner(storage, label, run_writer, sql, params):
    """A writer inside ``transaction()`` must leave the decision to the owner.

    Each of these committed unconditionally before #2162, which ended the
    owner's transaction from the inside and made everything written up to that
    point durable regardless of how the block finished.
    """
    with pytest.raises(RuntimeError, match="roll back owner"):
        async with storage.transaction():
            await run_writer(storage)
            raise RuntimeError("roll back owner")

    assert _committed_rows(storage, sql, params) == []


@pytest.mark.parametrize(
    "label,run_writer,sql,params",
    _SWEPT_WRITERS,
    ids=[case[0] for case in _SWEPT_WRITERS],
)
async def test_swept_writers_still_commit_standalone(storage, label, run_writer, sql, params):
    """Joining a transaction must not cost these writers their own commit."""
    await run_writer(storage)

    assert _committed_rows(storage, sql, params) != []
    assert storage._get_db().in_transaction is False


async def test_cleanup_old_sessions_closes_its_transaction_at_zero_rows(storage):
    """A DELETE that matches nothing still opens an implicit transaction.

    The commit used to be conditional on ``rowcount``, so a no-op cleanup left
    that transaction open on the shared writer connection for the next
    unrelated commit to flush (#1572), and made the next ``BEGIN IMMEDIATE``
    fail.
    """
    assert await storage.cleanup_old_sessions(max_age_days=1) == 0
    assert storage._get_db().in_transaction is False


async def test_prune_old_history_inside_a_transaction_leaves_it_open(storage):
    """Pruning is bookkeeping; it must not end a caller's transaction.

    It commits unconditionally when standalone — including at zero rows, for
    the reason above — which is exactly the shape that ends someone else's
    transaction when it is not guarded.
    """
    async with storage.transaction():
        await storage.create_session("owner-row", "owner", "default")
        storage._prune_old_history()
        assert storage._get_db().in_transaction is True

    assert await storage.get_session("owner-row") is not None


async def test_scratch_cleanup_closes_its_transaction_at_zero_rows(storage):
    """Same zero-row leak as ``cleanup_old_sessions``, in the other cleanup.

    The commit used to be conditional on the delete count, so a cleanup that
    matched nothing left its implicit transaction open on the shared writer
    connection for the next unrelated commit to flush (#1572).
    """
    assert await storage.scratch_cleanup() == 0
    assert storage._get_db().in_transaction is False


# Writers whose durability cannot be someone else's to decide: each is a claim
# taken *before* a durable write that happens outside SQLite, or the record
# that closes one out. Joining a caller's transaction would let a rollback undo
# the claim while the write it authorised still stands.
_CLAIM_WRITERS = [
    ("idempotency_claim", lambda s: s.idempotency_claim("mem_add", "k")),
    ("idempotency_complete", lambda s: s.idempotency_complete("mem_add", "k", "{}")),
    ("idempotency_release", lambda s: s.idempotency_release("mem_add", "k")),
    ("claim_memory_candidate", lambda s: s.claim_memory_candidate("cand-1", "reviewer")),
    ("release_memory_candidate", lambda s: s.release_memory_candidate("cand-1")),
    ("finalize_memory_candidate", lambda s: s.finalize_memory_candidate("cand-1")),
    (
        "mark_memory_candidate_write_uncertain",
        lambda s: s.mark_memory_candidate_write_uncertain("cand-1", actor="a", reason="r"),
    ),
    ("schedule_try_claim", lambda s: s.schedule_try_claim("sched-1", None)),
    ("schedule_mark_run", lambda s: s.schedule_mark_run("sched-1", "ok")),
]


@pytest.mark.parametrize(
    "name,run_writer", _CLAIM_WRITERS, ids=[case[0] for case in _CLAIM_WRITERS]
)
async def test_claim_writers_refuse_to_join_a_caller_transaction(storage, name, run_writer):
    """Refusing is the point: silently joining is what breaks at-most-once.

    These bracket a durable write outside SQLite — a memory file the caller
    appended, a job the scheduler already started. Deferring their commit to a
    caller's transaction means a rollback can un-claim work that really
    happened, and the retry duplicates it.
    """
    async with storage.transaction():
        with pytest.raises(StorageError, match=f"{name}.*transaction is active"):
            await run_writer(storage)
        # Refusing must not take the caller's transaction down with it.
        assert storage._get_db().in_transaction is True


@pytest.mark.parametrize(
    "name,run_writer", _CLAIM_WRITERS, ids=[case[0] for case in _CLAIM_WRITERS]
)
async def test_claim_writers_still_work_standalone(storage, name, run_writer):
    """The refusal is about composition only; the ordinary path is unchanged."""
    await run_writer(storage)
    assert storage._get_db().in_transaction is False


@pytest.mark.parametrize("stray", ["commit", "rollback"])
async def test_transaction_raises_when_a_participant_ends_it(storage, stray):
    """Ending the transaction from inside the block must fail the owner (#2162).

    Composition relies on participating writers suppressing both their commit
    and their rollback, which most storage writers do not do. Either one ends
    the owner's transaction: after a stray commit the owner's commit below is a
    no-op over already-durable work, and after a stray rollback it is a no-op
    over work that is gone. Both used to let the block return successfully
    having persisted only part of itself, or none of it.
    """
    chunk = make_chunk(content=f"half-written work via {stray}")

    with pytest.raises(StorageError, match="integrity violation"):
        async with storage.transaction():
            await storage.upsert_chunks([chunk])
            getattr(storage._get_db(), stray)()  # stands in for an unguarded writer

    assert storage._transaction_owner is None
    assert storage._get_db().in_transaction is False


async def test_transaction_integrity_error_does_not_claim_an_outcome(storage):
    """The tripwire cannot tell a stray commit from a stray rollback.

    ``in_transaction`` reports only that the transaction ended. A message that
    named one outcome would be wrong half the time, so it must claim neither:
    here the same error covers a rollback that discarded everything.
    """
    chunk = make_chunk(content="discarded by a stray rollback")

    with pytest.raises(StorageError) as excinfo:
        async with storage.transaction():
            await storage.upsert_chunks([chunk])
            storage._get_db().rollback()

    assert "committed or discarded" in str(excinfo.value)
    assert await storage.get_chunk(chunk.id) is None


async def test_transaction_failure_after_a_stolen_commit_reraises_the_original(storage, caplog):
    """The stolen-transaction report must not mask the caller's exception.

    Callers catch specific types raised from inside the block, so the rollback
    branch reports the stray commit through the log and re-raises what it was
    given rather than substituting a ``StorageError`` (#2162).
    """
    chunk = make_chunk(content="durable despite the failure")

    with caplog.at_level(logging.ERROR, logger="memtomem.storage.sqlite_backend"):
        with pytest.raises(RuntimeError, match="caller failure"):
            async with storage.transaction():
                await storage.upsert_chunks([chunk])
                storage._get_db().commit()  # stands in for an unguarded writer
                raise RuntimeError("caller failure")

    assert any("rollback skipped" in record.message for record in caplog.records)
    # The log reports the lost rollback, not a durability verdict it cannot
    # reach: the same branch runs when the stray call was a rollback.
    assert not any("stays durable" in record.message for record in caplog.records)
    assert storage._transaction_owner is None

    observer = sqlite3.connect(str(storage._config.sqlite_path), timeout=0)
    try:
        row = observer.execute("SELECT id FROM chunks WHERE id=?", (str(chunk.id),)).fetchone()
    finally:
        observer.close()

    # Why the lost rollback matters: this row survived the caller's failure.
    assert row is not None


async def test_transaction_failure_after_a_stolen_rollback_reraises_the_original(storage, caplog):
    """A stray rollback takes the same branch, and must report as tentatively.

    Nothing is durable here, so the log line must not assert that anything is.
    """
    chunk = make_chunk(content="discarded before the failure")

    with caplog.at_level(logging.ERROR, logger="memtomem.storage.sqlite_backend"):
        with pytest.raises(RuntimeError, match="caller failure"):
            async with storage.transaction():
                await storage.upsert_chunks([chunk])
                storage._get_db().rollback()  # stands in for an unguarded writer
                raise RuntimeError("caller failure")

    assert any("may already be durable" in record.message for record in caplog.records)
    assert storage._transaction_owner is None
    assert await storage.get_chunk(chunk.id) is None


# ---- #2167: a failed writer closes its own transaction ----------------------
#
# The AST guard in ``test_mixin_commit_guard.py`` proves every writer has the
# rollback-protected shape. These prove the shape does what it claims: the
# failure escapes with its own type, the connection is left usable, and the
# statements that ran before the failure are gone rather than waiting for the
# next unrelated commit to flush them (#1572).


def _connection_is_idle(storage) -> bool:
    """Whether the shared writer connection has no transaction pending.

    ``in_transaction`` is the direct reading; ``transaction()`` succeeding
    afterwards is the consequence that actually breaks for callers, since it
    takes ``BEGIN IMMEDIATE`` and fails outright on a connection that already
    has one open.
    """
    return storage._get_db().in_transaction is False


async def test_add_assertion_closes_its_transaction_when_it_cannot_resolve(storage):
    """The failure #2167 was filed for.

    ``add_assertion`` inserts into ``canonical_entities`` and then raises
    ``ValueError`` if the row it needs is not there — between the INSERT and
    the commit. The entity write sat pending on the shared connection.
    """
    db = storage._get_db()
    # An id collision is the one way the INSERT OR IGNORE loses: the same id
    # already names a different canonical entity, so the SELECT by name misses.
    db.execute(
        "INSERT INTO canonical_entities (id, canonical_name, entity_type, aliases, created_at) "
        "VALUES (?, ?, ?, '[]', ?)",
        ("ent-collide", "already-here", "person", "2026-01-01T00:00:00+00:00"),
    )
    db.commit()

    with pytest.raises(ValueError, match="unable to resolve canonical entity"):
        await storage.add_assertion(
            assertion_id="a1",
            entity_id="ent-collide",
            canonical_name="different-name",
            entity_type="person",
            predicate="works_at",
            object_value="acme",
            source_chunk_id=None,
            recorded_at="2026-01-02T00:00:00+00:00",
        )

    assert _connection_is_idle(storage)
    # The consequence: before the fix this raised "cannot start a transaction
    # within a transaction" instead of running the caller's work.
    async with storage.transaction():
        pass
    # The INSERT OR IGNORE is what opened the transaction; it wrote no row here
    # (losing to the id collision is the whole reason the SELECT missed), so
    # what the rollback bought is the closed transaction above, not a discarded
    # row. The assertion the caller was told did not happen must also be absent.
    assert _committed_rows(storage, "SELECT id FROM memory_assertions WHERE id=?", ("a1",)) == []


async def test_a_single_statement_writer_leaves_no_open_transaction(storage):
    """A lone statement that fails has written nothing, so what the rollback
    buys is the closed transaction — assert that directly, because a row-count
    check would pass with or without the fix."""
    with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        await storage.scratch_set("k", object())  # unbindable value

    assert _connection_is_idle(storage)
    async with storage.transaction():
        pass


async def test_idempotency_claim_rolls_back_its_purge(storage):
    """A multi-statement REFUSES writer: the purge DELETE really did run, so
    the rollback has something to undo. Before the fix the expired row stayed
    pending and the next unrelated commit deleted it for good."""
    db = storage._get_db()
    db.execute(
        "INSERT INTO idempotency_ledger (tool, key, result, created_at, expires_at) "
        "VALUES (?, ?, NULL, ?, ?)",
        ("mem_add", "stale", "2020-01-01T00:00:00+00:00", "2020-01-02T00:00:00+00:00"),
    )
    db.commit()

    with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
        await storage.idempotency_claim("mem_add", object())

    assert _connection_is_idle(storage)
    # An unrelated commit is the flush that used to carry the purge with it.
    await storage.scratch_set("unrelated", "value")
    assert _committed_rows(
        storage,
        "SELECT key FROM idempotency_ledger WHERE key=?",
        ("stale",),
    ) == [("stale",)]


async def test_cleanup_old_sessions_rolls_back_a_failed_delete(storage):
    """Failure injected at the connection boundary *inside* the unit, armed for
    one statement only: an always-raising proxy would take down the unrelated
    commit this test needs afterwards."""
    await storage.create_session("old-session", "tester", "default")
    await storage.end_session("old-session", "done", {})
    db = storage._get_db()
    db.execute(
        "UPDATE sessions SET ended_at=? WHERE id=?", ("2020-01-01T00:00:00+00:00", "old-session")
    )
    db.commit()

    class _FailsAfterTheDelete:
        """Forwards everything, but raises once the DELETE has run."""

        def __init__(self, real):
            self._real = real
            self.armed = True

        def execute(self, sql, *args):
            cur = self._real.execute(sql, *args)
            if self.armed and sql.strip().upper().startswith("DELETE FROM SESSIONS"):
                self.armed = False
                raise sqlite3.OperationalError("injected failure after the DELETE")
            return cur

        def __getattr__(self, name):
            return getattr(self._real, name)

    proxy = _FailsAfterTheDelete(db)
    real_get_db = storage._get_db
    storage._get_db = lambda: proxy
    try:
        with pytest.raises(sqlite3.OperationalError, match="injected failure"):
            await storage.cleanup_old_sessions(max_age_days=1)
    finally:
        storage._get_db = real_get_db

    assert _connection_is_idle(storage)
    await storage.scratch_set("unrelated-2", "value")
    assert _committed_rows(storage, "SELECT id FROM sessions WHERE id=?", ("old-session",)) == [
        ("old-session",)
    ]


async def test_a_failing_rollback_does_not_replace_the_original_failure(storage, caplog):
    """When the rollback itself raises, the caller still sees its own error.

    The connection is deliberately *not* poisoned here. A rollback only fails
    when the connection is already unusable, and the state it leaves behind —
    an open transaction — is exactly what this code path found; refusing every
    later operation would promote a rare transient into a dead store, which is
    strictly worse than what shipped before #2167. The lost rollback is
    reported at ERROR instead, and that report is what this pins.
    """
    db = storage._get_db()

    class _RollbackFails:
        def __init__(self, real):
            self._real = real

        def rollback(self):
            raise sqlite3.OperationalError("rollback cannot run")

        def __getattr__(self, name):
            return getattr(self._real, name)

    proxy = _RollbackFails(db)
    real_get_db = storage._get_db
    storage._get_db = lambda: proxy
    try:
        with caplog.at_level(logging.ERROR, logger="memtomem.storage.sqlite_backend"):
            # Unbindable value: the writer raises, and its rollback then fails.
            with pytest.raises((sqlite3.InterfaceError, sqlite3.ProgrammingError)):
                await storage.scratch_set("rollback-fails", object())
    finally:
        storage._get_db = real_get_db
        if db.in_transaction:
            db.rollback()

    assert any("rollback after a failed write raised" in r.message for r in caplog.records)


# ---- #2168 residue outside storage/mixins -----------------------------------
#
# The mixin sweep converted every writer under ``storage/mixins/``, but four
# writers in ``sqlite_backend.py`` / ``sqlite_meta.py`` kept the exact shapes
# the AST guard rejects: an unconditional commit that ends an owner's
# transaction early (#2158/#2162), and a ``BEGIN IMMEDIATE`` with no refusal
# of a pending transaction the task does not own. These pin the converted
# behavior through the public writers that reach them.


async def test_increment_access_inside_transaction_defers_to_owner(storage):
    chunk = make_chunk(content="counted")
    await storage.upsert_chunks([chunk])

    async with storage.transaction():
        await storage.increment_access([chunk.id])
        # Before the fix increment_access committed unconditionally, ending
        # the owner's transaction from underneath it; transaction() would
        # then raise its integrity violation on exit.
        assert storage._get_db().in_transaction is True

    counts = await storage.get_access_counts([chunk.id])
    assert counts[str(chunk.id)] == 1


async def test_set_ai_summary_inside_transaction_defers_to_owner(storage, tmp_path):
    src = tmp_path / "note.md"

    async with storage.transaction():
        await storage.set_ai_summary(src, "kept", "sig", "en")
        # set_meta's unconditional commit used to end the owner's
        # transaction here.
        assert storage._get_db().in_transaction is True

    summaries = await storage.get_all_ai_summaries()
    assert [rec["summary"] for rec in summaries.values()] == ["kept"]


async def test_set_ai_summary_rolls_back_with_its_owner(storage, tmp_path):
    src = tmp_path / "note.md"

    with pytest.raises(RuntimeError, match="owner failure"):
        async with storage.transaction():
            await storage.set_ai_summary(src, "doomed", "sig", "en")
            raise RuntimeError("owner failure")

    # Before the fix the summary was already durable: set_meta committed it
    # mid-transaction, so the owner's rollback had nothing left to undo.
    assert await storage.get_all_ai_summaries() == {}


async def test_delete_by_source_empty_branch_defers_to_owner(storage, tmp_path):
    src = tmp_path / "gone.md"
    await storage.set_ai_summary(src, "orphaned", "sig", "en")

    with pytest.raises(RuntimeError, match="owner failure"):
        async with storage.transaction():
            deleted = await storage.delete_by_source(src)
            assert deleted == 0  # no chunks, only the summary cache row
            raise RuntimeError("owner failure")

    # The owner rolled back, so the summary-cache DELETE must roll back too.
    summaries = await storage.get_all_ai_summaries()
    assert [rec["summary"] for rec in summaries.values()] == ["orphaned"]


async def test_update_chunks_scope_refuses_pending_unowned_transaction(storage, tmp_path):
    """A stranded implicit transaction must be refused with a named error.

    Before the fix ``BEGIN IMMEDIATE`` raised sqlite3's "cannot start a
    transaction within a transaction" outside the writer's try block,
    stranding the transaction a second time.
    """
    db = storage._get_db()
    db.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(StorageError, match="refused"):
            await storage.update_chunks_scope_for_source(
                tmp_path / "old.md", tmp_path / "new.md", "user", None
            )
        assert db.in_transaction is True  # the refusal must not touch it
    finally:
        db.rollback()


async def test_reset_all_refuses_pending_unowned_transaction(storage, tmp_path):
    """reset_all must not adopt a transaction it did not open.

    ``owns_txn`` is derived from ``_in_transaction`` (task ownership), which
    says nothing about a stranded implicit transaction on the shared writer
    connection. Without the refusal the wipe joined that transaction and the
    owned-branch commit ended it, flushing the stranger's half-written rows
    along with the reset.
    """
    survivor = make_chunk(content="must survive a refused reset")
    await storage.upsert_chunks([survivor])

    db = storage._get_db()
    db.execute("BEGIN IMMEDIATE")
    db.execute("INSERT INTO _memtomem_meta(key, value) VALUES ('pending_probe', 'uncommitted')")
    try:
        with pytest.raises(StorageError, match="refused"):
            await storage.reset_all()
        # Checked before the rollback, so a version that deleted first and
        # raised afterwards cannot hide behind the cleanup: the refusal has to
        # come before any write, and it must not end the stranger's
        # transaction.
        assert db.in_transaction is True
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 1
        assert (
            db.execute("SELECT value FROM _memtomem_meta WHERE key='pending_probe'").fetchone()[0]
            == "uncommitted"
        )
    finally:
        db.rollback()

    # The stranger's row went out with its own rollback rather than being
    # committed by the reset, and the survivor is still there.
    assert (
        db.execute("SELECT COUNT(*) FROM _memtomem_meta WHERE key='pending_probe'").fetchone()[0]
        == 0
    )
    assert await storage.get_chunk(survivor.id) is not None


async def test_reset_embedding_meta_refuses_an_outer_transaction(storage):
    """reset mutates in-memory embedding state alongside the DB, so it owns
    its transaction and refuses to participate in someone else's: an owner
    rollback would restore the old schema while the in-memory fields kept
    pointing at the new config."""
    prior_dim = storage._dimension
    async with storage.transaction():
        with pytest.raises(StorageError, match="cannot run while a transaction is active"):
            await storage.reset_embedding_meta(dimension=8)

    assert storage._dimension == prior_dim


async def test_reset_embedding_meta_failure_rolls_back_the_drop(storage, monkeypatch):
    """The whole reset is one owned transaction: a failure after the DROP must
    put chunks_vec back and restore the in-memory embedding state. Before the
    fix set_meta committed mid-reset, making the DROP durable on its own."""
    db = storage._get_db()
    assert db.execute("SELECT name FROM sqlite_master WHERE name='chunks_vec'").fetchone()
    prior_dim = storage._dimension

    def boom(*args, **kwargs):
        raise RuntimeError("meta write failure")

    monkeypatch.setattr(storage._meta, "reset_embedding_meta", boom)
    with pytest.raises(RuntimeError, match="meta write failure"):
        await storage.reset_embedding_meta(dimension=8)

    assert db.in_transaction is False
    assert storage._dimension == prior_dim
    assert db.execute("SELECT name FROM sqlite_master WHERE name='chunks_vec'").fetchone()
    async with storage.transaction():
        pass


async def test_delete_by_source_empty_branch_failure_closes_its_transaction(
    storage, tmp_path, monkeypatch
):
    """The empty-source branch's DELETE + commit sat outside the writer's try:
    a failure after the DELETE stranded the transaction. The commit step is
    forced to fail so the DELETE is pending when the guard must clean up."""
    src = tmp_path / "gone.md"
    await storage.set_ai_summary(src, "orphaned", "sig", "en")

    def boom(db):
        raise RuntimeError("commit failure")

    monkeypatch.setattr(storage, "_commit_if_standalone", boom)
    with pytest.raises(RuntimeError, match="commit failure"):
        await storage.delete_by_source(src)

    monkeypatch.undo()
    assert storage._get_db().in_transaction is False
    async with storage.transaction():
        pass


async def test_reset_embedding_meta_rollback_failure_preserves_the_reset_error(
    storage, monkeypatch, caplog
):
    """A rollback that itself fails must not replace the reset's own failure
    (the _rolls_back_if_standalone preservation contract)."""
    real_db = storage._get_db()

    class _RollbackBomb:
        def __init__(self, real):
            self._real = real

        def __getattr__(self, name):
            return getattr(self._real, name)

        @property
        def in_transaction(self):
            return self._real.in_transaction

        def rollback(self):
            raise sqlite3.OperationalError("rollback failed")

    proxy = _RollbackBomb(real_db)
    monkeypatch.setattr(storage, "_get_db", lambda: proxy)

    def boom(*args, **kwargs):
        raise RuntimeError("meta write failure")

    monkeypatch.setattr(storage._meta, "reset_embedding_meta", boom)

    with caplog.at_level(logging.ERROR, logger="memtomem.storage.sqlite_backend"):
        with pytest.raises(RuntimeError, match="meta write failure"):
            await storage.reset_embedding_meta(dimension=8)

    assert any("failed embedding reset" in r.message for r in caplog.records)
    monkeypatch.undo()
    # The logged-but-lost rollback leaves the transaction open by design;
    # close it for real so the fixture teardown sees an idle connection.
    if real_db.in_transaction:
        real_db.rollback()


def test_meta_standalone_guard_preserves_the_write_failure(caplog):
    """The default MetaManager guard (no backend injection) has the same
    preservation contract as _rolls_back_if_standalone."""
    from memtomem.storage.sqlite_meta import MetaManager

    real = sqlite3.connect(":memory:")
    try:

        class _Bomb:
            def __getattr__(self, name):
                return getattr(real, name)

            def execute(self, *args):
                raise sqlite3.OperationalError("disk I/O error")

            def rollback(self):
                raise sqlite3.OperationalError("rollback failed")

        meta = MetaManager(lambda: _Bomb())
        with caplog.at_level(logging.ERROR, logger="memtomem.storage.sqlite_meta"):
            with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
                meta.set_meta("k", "v")

        assert any("failed meta write" in r.message for r in caplog.records)
    finally:
        real.close()


async def test_ns_prefix_counters_answer_while_another_task_owns_a_transaction(storage):
    """The hidden-namespace hint counters are pure reads on the search hot
    path. Routed through the ownership-guarded writer connection they failed
    closed for the whole span of any other task's transaction — silently
    starving the hint (the pipeline wraps them in try/except debug). From the
    read pool they answer, and see only committed state."""
    hidden = make_chunk(content="hidden", namespace="agent-runtime:planner", source="h.md")
    await storage.upsert_chunks([hidden])

    owner_ready = asyncio.Event()
    release_owner = asyncio.Event()

    async def owner() -> None:
        async with storage.transaction():
            # A matching-but-uncommitted chunk: the foreign task below must
            # count only the committed row, not this pending one.
            await storage.upsert_chunks(
                [make_chunk(content="pending", namespace="agent-runtime:pending", source="q.md")]
            )
            owner_ready.set()
            await release_owner.wait()

    owner_task = asyncio.create_task(owner())
    await owner_ready.wait()
    try:
        total, by_prefix = await storage.count_chunks_by_ns_prefix_detail(["agent-runtime:"])
        assert total == 1
        assert by_prefix == {"agent-runtime:": 1}
        assert await storage.count_chunks_by_ns_prefix(["agent-runtime:"]) == 1
    finally:
        release_owner.set()
        await owner_task


async def test_ns_prefix_counters_see_own_uncommitted_writes_inside_a_transaction(storage):
    """Inside this task's own transaction the counters must keep reading the
    writer connection: the read pool's WAL snapshot cannot see uncommitted
    rows, and a caller counting mid-transaction is asking about its own work."""
    async with storage.transaction():
        await storage.upsert_chunks(
            [make_chunk(content="pending", namespace="agent-runtime:x", source="p.md")]
        )
        assert await storage.count_chunks_by_ns_prefix(["agent-runtime:"]) == 1
        total, by_prefix = await storage.count_chunks_by_ns_prefix_detail(["agent-runtime:"])
        assert total == 1
        assert by_prefix == {"agent-runtime:": 1}
