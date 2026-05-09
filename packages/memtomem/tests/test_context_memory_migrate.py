"""ADR-0011 PR-D — ``mm context memory-migrate`` v1 tests.

Pins for the chunk-id-stable single-DB rename:

1. **Dry-run reports the plan without mutating** disk or DB.
2. **Apply user → project_shared** preserves chunk UUIDs and updates
   ``metadata.scope`` / ``metadata.project_root`` in-place; the
   filesystem move lands at the project tier path.
3. **Gate A on migrate** rejects when the file content matches a
   privacy pattern AND the target tier is ``project_shared``. No
   force bypass available — git history is forever.
4. **Compensation rolls back the FS move** when the DB UPDATE
   fails, so the source path remains canonical.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.models import Chunk, ChunkMetadata


_SECRET = "api_key=AKIA1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


@pytest.fixture
def fake_project_layout(tmp_path):
    """A throwaway project root with both ``memories`` and ``memories.local``
    canonical directories pre-created and a clean source markdown under
    the user tier.
    """
    project_root = tmp_path / "proj"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_local = project_root / ".memtomem" / "memories.local"
    proj_shared.mkdir(parents=True)
    proj_local.mkdir(parents=True)
    # Mark as a project root so ``_find_project_root`` can pick it up.
    (project_root / ".git").mkdir()

    user_tier = tmp_path / "user_home" / ".memtomem" / "memories"
    user_tier.mkdir(parents=True)
    src = user_tier / "rule.md"
    src.write_text("## Rule\n\nharmless team rule body.\n", encoding="utf-8")
    return {
        "project_root": project_root,
        "proj_shared": proj_shared,
        "proj_local": proj_local,
        "user_tier": user_tier,
        "src": src,
    }


def _patch_cli_components(monkeypatch, comp):
    """Replace ``cli_components`` with a no-op context yielding ``comp``."""

    @asynccontextmanager
    async def _fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake)


# ---------------------------------------------------------------------------
# 1) Dry-run
# ---------------------------------------------------------------------------


def test_memory_migrate_dry_run_reports_plan_without_mutating(monkeypatch, fake_project_layout):
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    src = layout["src"]
    proj_shared = layout["proj_shared"]

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    # ADR-0011: target tier must be registered for the migrate to be
    # discoverable post-move. Tests now mirror the production setup.
    comp.config.indexing.project_memory_dirs = [layout["proj_shared"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=2)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)

    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [str(src), "--from", "user", "--to", "project_shared"],
    )
    assert result.exit_code == 0, result.output
    assert "Plan: migrate rule.md" in result.output
    assert "chunks affected: 2" in result.output
    assert "Run with --apply" in result.output

    # Dry-run must not call the DB UPDATE.
    comp.storage.update_chunks_scope_for_source.assert_not_called()
    # File must not have moved.
    assert src.exists()
    assert not (proj_shared / "rule.md").exists()


# ---------------------------------------------------------------------------
# 2) Apply: chunk-id-stable rename
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_migrate_apply_user_to_project_shared_chunk_ids_preserved(
    bm25_only_components, monkeypatch, tmp_path
):
    """Apply path against a real BM25 storage backend.

    Pre-stages a user-tier file with one chunk row; runs the core
    coroutine (bypassing the click wrapper to avoid nested
    ``asyncio.run`` inside pytest's event loop); asserts the chunk
    UUID is unchanged and ``metadata.scope`` / ``project_root``
    flipped to the project tier on the same row.
    """
    from memtomem.cli.context_cmd import _memory_migrate_run

    comp, mem_dir = bm25_only_components
    project_root = tmp_path / "proj_x"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_shared.mkdir(parents=True)
    (project_root / ".git").mkdir()
    # Register the target tier so the migrate guard accepts the move.
    comp.config.indexing.project_memory_dirs = [proj_shared]

    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nharmless body.\n", encoding="utf-8")

    chunk = Chunk(
        content="harmless body.",
        metadata=ChunkMetadata(
            source_file=src,
            scope="user",
            project_root=None,
            start_line=3,
            end_line=3,
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([chunk])
    pre_chunk_id = chunk.id

    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(project_root)

    # ADR-0011 PR-D round 10 (M2): ``--to project_shared`` requires an
    # explicit ``--confirm-project-shared``; ``--yes`` alone is no
    # longer sufficient (mirrors the round-7 ``mm mem add`` parity fix).
    await _memory_migrate_run(
        src.resolve(),
        from_scope="user",
        to_scope="project_shared",
        apply_=True,
        yes=True,
        confirm_project_shared=True,
    )

    # Source no longer exists; target lives in project tier.
    target = proj_shared / "rule.md"
    assert not src.exists()
    assert target.exists()

    # chunk UUID preserved; scope/project_root flipped.
    refreshed = await comp.storage.get_chunk(pre_chunk_id)
    assert refreshed is not None, "chunk row should still exist with same UUID"
    assert refreshed.metadata.scope == "project_shared"
    assert refreshed.metadata.project_root == project_root
    assert refreshed.metadata.source_file == target


# ---------------------------------------------------------------------------
# 3) Gate A on migrate — privacy hits reject project_shared target
# ---------------------------------------------------------------------------


def test_memory_migrate_apply_project_shared_target_blocks_on_secret(
    monkeypatch, fake_project_layout
):
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    src = layout["src"]
    src.write_text(f"## Token\n\n{_SECRET}\n", encoding="utf-8")

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    comp.config.indexing.project_memory_dirs = [layout["proj_shared"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(src),
            "--from",
            "user",
            "--to",
            "project_shared",
            "--apply",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "Gate A" in out
    assert "git history is forever" in out
    # Source untouched on rejection.
    assert src.exists()
    comp.storage.update_chunks_scope_for_source.assert_not_called()


@pytest.mark.asyncio
async def test_memory_migrate_update_uses_begin_immediate_transaction(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review round 10 (B2) pin: the SELECT-then-UPDATE
    inside ``update_chunks_scope_for_source`` runs under an explicit
    ``BEGIN IMMEDIATE`` so a concurrent watcher INSERT cannot sneak in
    between the rowid lookup and the UPDATE — otherwise duplicate
    chunks land at the destination, defeating chunk-id stability.

    Uses a SQL-trace spy on ``db.execute`` to confirm the transaction
    boundary is opened explicitly. Pre-fix relied on Python sqlite3's
    lazy-DML transaction promotion which left the SELECT phase
    exposed (sqlite_backend.py round-10 docstring).
    """
    comp, mem_dir = bm25_only_components

    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")
    target = tmp_path / "moved" / "rule.md"
    target.parent.mkdir(parents=True)

    chunk = Chunk(
        content="body.",
        metadata=ChunkMetadata(
            source_file=src,
            scope="user",
            project_root=None,
            start_line=1,
            end_line=2,
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([chunk])

    # ``sqlite3.Connection.execute`` is a C-level slot, can't be
    # monkeypatched directly. Use ``set_trace_callback`` — sqlite3's
    # canonical hook for SQL tracing (fires on every prepared statement
    # before execution).
    db = comp.storage._get_db()
    sql_trace: list[str] = []
    db.set_trace_callback(lambda sql: sql_trace.append(sql.strip().split("\n", 1)[0]))

    try:
        await comp.storage.update_chunks_scope_for_source(
            src,
            target,
            "user",
            None,
        )
    finally:
        db.set_trace_callback(None)

    # The transaction boundary fires before the SELECT.
    assert any("BEGIN IMMEDIATE" in s.upper() for s in sql_trace), (
        f"update_chunks_scope_for_source must wrap SELECT+UPDATE in "
        f"BEGIN IMMEDIATE, observed SQL trace: {sql_trace}"
    )
    # And the BEGIN IMMEDIATE precedes the SELECT (lock acquired up-front).
    begin_idx = next(i for i, s in enumerate(sql_trace) if "BEGIN IMMEDIATE" in s.upper())
    select_idx = next(i for i, s in enumerate(sql_trace) if "SELECT rowid FROM chunks" in s)
    assert begin_idx < select_idx, (
        "BEGIN IMMEDIATE must fire BEFORE the SELECT phase, otherwise the "
        "RESERVED lock is acquired only after the rowid set is read and a "
        "concurrent writer can race in. Trace: " + str(sql_trace)
    )


@pytest.mark.asyncio
async def test_engine_index_file_acquires_sidecar_lock_for_watcher_cooperation(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review round 11 (B2 carry-over) pin: the indexing
    engine's public ``index_file`` entry point now acquires the same
    sidecar advisory lock that ``mm context memory-migrate`` holds.
    Without this, the migrate's lock is one-sided — the watcher
    (which routes through ``index_file``) never asks for it, so a
    concurrent watcher firing ``index_file(target)`` between
    migrate's ``shutil.move`` and the DB UPDATE still produces
    duplicate chunks at the destination.

    Spy on ``memtomem.context._atomic._file_lock`` to confirm
    ``index_file`` enters the lock for the resolved file path. The
    presence assertion is sufficient — the lock primitive itself
    is exercised by ``test_memory_migrate_compensation_*`` and the
    sidecar lockfile pattern's own pin in
    ``test_atomic_lockfile.py`` (#548 line of work).
    """
    comp, mem_dir = bm25_only_components

    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nbody.\n", encoding="utf-8")

    # Capture every (lock_path, kind) pair entering ``_file_lock``.
    from contextlib import contextmanager

    from memtomem.context import _atomic as atomic_mod

    real_file_lock = atomic_mod._file_lock
    lock_calls: list[Path] = []

    @contextmanager
    def _spy_file_lock(lock_path):
        lock_calls.append(lock_path)
        with real_file_lock(lock_path):
            yield

    monkeypatch.setattr(atomic_mod, "_file_lock", _spy_file_lock)
    # The engine imports lazily inside ``index_file`` so the symbol
    # we want to patch is the module-attribute view there too.
    monkeypatch.setattr(
        "memtomem.context._atomic._file_lock",
        _spy_file_lock,
    )

    # ``index_file`` is the canonical entry the watcher uses
    # (``watcher.py:230`` calls ``self._engine.index_file(file_path)``).
    await comp.index_engine.index_file(src.resolve())

    # The expected lockfile sits next to the source path with
    # ``.<name>.lock`` (``feedback_sidecar_lockfile_for_replaced_files.md``
    # via ``_lock_path_for``).
    expected_lock = src.parent / f".{src.name}.lock"
    assert any(lp == expected_lock for lp in lock_calls), (
        f"engine.index_file did not acquire {expected_lock} — the migrate "
        f"sidecar lock is one-sided. Captured locks: {lock_calls}"
    )


def test_memory_migrate_yes_alone_rejects_project_shared(monkeypatch, fake_project_layout):
    """ADR-0011 PR-D review round 10 (M2) pin: ``--yes`` alone must
    NOT satisfy Gate B for ``--to project_shared``. Mirrors the
    round-7 fix on ``mm mem add`` (cli/memory.py:202-217).
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    src = layout["src"]

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    comp.config.indexing.project_memory_dirs = [layout["proj_shared"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(src),
            "--from",
            "user",
            "--to",
            "project_shared",
            "--apply",
            "--yes",
            # NO --confirm-project-shared
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "--confirm-project-shared" in out
    assert "git-tracked" in out
    # Source untouched on rejection.
    assert src.exists()
    comp.storage.update_chunks_scope_for_source.assert_not_called()


# ---------------------------------------------------------------------------
# 4) Compensation — FS rollback on DB UPDATE failure
# ---------------------------------------------------------------------------


def test_memory_migrate_compensation_rolls_back_on_db_failure(monkeypatch, fake_project_layout):
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    src = layout["src"]
    proj_shared = layout["proj_shared"]

    async def _fail_update(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    comp.config.indexing.project_memory_dirs = [layout["proj_local"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.update_chunks_scope_for_source = _fail_update
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(src),
            "--from",
            "user",
            "--to",
            "project_local",  # avoid Gate A re-scan
            "--apply",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "DB update failed" in out
    assert "filesystem move reverted" in out
    # Source must be back at its original location after compensation.
    assert src.exists(), "FS rollback should restore source path"
    assert not (proj_shared / "rule.md").exists()


@pytest.mark.asyncio
async def test_memory_migrate_compensation_real_backend_preserves_chunk_scope(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D round 8 pin: rollback claim against a REAL SQLite
    backend, not AsyncMock.

    The earlier compensation test (above) only proved that the FS path
    was restored when the (mocked) DB update raised — the AsyncMock
    storage means a true SQL transactionality bug (e.g. partial UPDATE
    + FS revert leaving the chunk row half-flipped) would not trip
    the assertion (``feedback_mocked_storage_hides_sql_bugs.md``).
    This test runs the same flow against the bm25_only_components
    SQLite backend and pins:

    1. FS path back at the original user-tier location.
    2. The chunk row's persisted ``metadata.scope`` is STILL ``user``
       and ``metadata.project_root`` is STILL ``None`` — the DB-level
       state is consistent with the post-compensation FS state.
    """
    from memtomem.cli.context_cmd import _memory_migrate_run

    comp, mem_dir = bm25_only_components
    project_root = tmp_path / "proj_compensation"
    proj_local = project_root / ".memtomem" / "memories.local"
    proj_local.mkdir(parents=True)
    (project_root / ".git").mkdir()
    comp.config.indexing.project_memory_dirs = [proj_local]

    src = mem_dir / "rule.md"
    src.write_text("## Rule\n\nharmless body.\n", encoding="utf-8")

    chunk = Chunk(
        content="harmless body.",
        metadata=ChunkMetadata(
            source_file=src,
            scope="user",
            project_root=None,
            start_line=3,
            end_line=3,
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([chunk])
    pre_chunk_id = chunk.id

    # Wrap the real backend so the DB UPDATE raises mid-flight. The
    # FS move has already happened by the time the DB step runs, so
    # the failure exercises the compensation branch
    # (``cli/context_cmd.py``: filesystem rollback).
    real_update = comp.storage.update_chunks_scope_for_source

    async def _fail_update(*args, **kwargs):
        raise RuntimeError("simulated DB failure")

    monkeypatch.setattr(comp.storage, "update_chunks_scope_for_source", _fail_update)

    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(project_root)

    with pytest.raises(Exception):  # SystemExit from click.ClickException
        await _memory_migrate_run(
            src.resolve(),
            from_scope="user",
            to_scope="project_local",
            apply_=True,
            yes=True,
            confirm_project_shared=False,
        )

    # FS pin: source restored, project tier empty.
    assert src.exists(), "FS rollback should restore source path"
    assert not (proj_local / "rule.md").exists(), "project tier must be clean post-rollback"

    # SQL pin: the chunk row's persisted metadata is still user-scope.
    # If a future regression makes the DB UPDATE non-atomic with the
    # FS revert (e.g. UPDATE commits before rollback fires), this
    # assertion catches the half-flipped state — the AsyncMock
    # variant would have happily returned whatever pre-staged value
    # the mock's ``_fail_update`` sentinel was configured with.
    refreshed = await comp.storage.get_chunk(pre_chunk_id)
    assert refreshed is not None, "chunk row should still exist post-rollback"
    assert refreshed.metadata.scope == "user", (
        f"scope must remain 'user' after rollback, got {refreshed.metadata.scope!r}"
    )
    assert refreshed.metadata.project_root is None, (
        f"project_root must remain None after rollback, got {refreshed.metadata.project_root!r}"
    )
    # And the chunk's source_file should still resolve to the original path.
    assert refreshed.metadata.source_file == src

    # Restore so other tests sharing the storage instance see the real method.
    monkeypatch.setattr(comp.storage, "update_chunks_scope_for_source", real_update)


# ---------------------------------------------------------------------------
# 5) Pre-flight registration check (PR-D review round 5)
# ---------------------------------------------------------------------------


def test_memory_migrate_unregistered_target_tier_refused(monkeypatch, fake_project_layout):
    """ADR-0011 PR-D review pin: refuse migrating into a project tier
    that is not registered in ``IndexingConfig.project_memory_dirs``.

    The new read/search boundary and the indexing watcher derive
    project context from ``project_memory_dirs``. A migrated row whose
    target tier is missing from that list flips ``metadata.scope`` to
    ``project_shared`` / ``project_local`` but stays invisible to
    default search, recall, and the watcher — silent data loss from
    the user's perspective.
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    src = layout["src"]

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    # Empty registry — the project tier exists on disk but is not
    # registered with the indexer.
    comp.config.indexing.project_memory_dirs = []
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [str(src), "--from", "user", "--to", "project_shared"],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "not registered" in out
    assert "project_memory_dirs" in out
    # Hint must NOT mention the broken ``mm config set ...
    # indexing.project_memory_dirs[+]=...`` form (PR-D review round 6:
    # that command shape doesn't exist — ``mm config set`` rejects
    # ``project_memory_dirs`` because it's outside MUTABLE_FIELDS, and
    # the bracketed assignment isn't supported syntax). The hint must
    # point at the only path that actually works: editing
    # ``~/.memtomem/config.json`` directly.
    assert "mm config set" not in out
    assert "config.json" in out
    # Refusal must happen before any FS / DB mutation, including the
    # cheap ``count_chunks_by_source`` probe.
    comp.storage.count_chunks_by_source.assert_not_called()
    comp.storage.update_chunks_scope_for_source.assert_not_called()
    assert src.exists()
    assert not (layout["proj_shared"] / "rule.md").exists()
