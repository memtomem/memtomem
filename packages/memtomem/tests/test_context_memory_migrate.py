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
        [src.resolve()],
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


@pytest.mark.asyncio
async def test_memory_migrate_nested_project_source_to_user_walks_dot_memtomem_ancestor(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review round 12 (P2) pin: a project-tier source
    nested under a subdirectory (e.g. ``.memtomem/memories/notes/foo.md``)
    must still infer the correct ``project_root`` when migrating to
    user scope.

    Pre-fix shape: the inference only handled depth=3
    (``<root>/.memtomem/memories[.local]/<file>``) by hardcoding
    ``source.parent.parent.parent``. For nested files, the check
    ``source.parent.parent.name == ".memtomem"`` was False, so
    ``project_root`` stayed None, AND the fallback to
    ``_find_project_root()`` only ran for ``to_scope != "user"``.
    Migrating a nested project_shared file BACK to user scope
    therefore errored out at ``resolve_memory_scope_dir(from_scope,
    None, ...)`` before any FS / DB mutation — valid project-tier
    files in subdirectories were unmigratable.

    Post-fix: walk up the source's parents looking for the
    ``.memtomem`` ancestor; project_root is its parent.
    """
    from memtomem.cli.context_cmd import _memory_migrate_run

    comp, _user_mem_dir = bm25_only_components
    project_root = tmp_path / "proj_nested"
    proj_shared = project_root / ".memtomem" / "memories"
    nested_dir = proj_shared / "notes" / "subtopic"
    nested_dir.mkdir(parents=True)
    (project_root / ".git").mkdir()
    comp.config.indexing.project_memory_dirs = [proj_shared]

    nested_src = nested_dir / "rule.md"
    nested_src.write_text("## Rule\n\nharmless body.\n", encoding="utf-8")

    chunk = Chunk(
        content="harmless body.",
        metadata=ChunkMetadata(
            source_file=nested_src,
            scope="project_shared",
            project_root=project_root,
            start_line=3,
            end_line=3,
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([chunk])
    pre_chunk_id = chunk.id

    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(project_root)

    # User-tier base under tmp_path so the migrate has a real
    # destination directory to land in.
    user_tier = tmp_path / "user_home" / ".memtomem" / "memories"
    user_tier.mkdir(parents=True)
    comp.config.indexing.memory_dirs = [user_tier]

    # Migrate from project_shared → user. Pre-fix: errors before
    # mutation. Post-fix: lands in user tier with chunk-id stable.
    await _memory_migrate_run(
        [nested_src.resolve()],
        from_scope="project_shared",
        to_scope="user",
        apply_=True,
        yes=True,
        confirm_project_shared=False,
    )

    # Source no longer exists under the project tier.
    assert not nested_src.exists(), "nested project source should have moved"
    # Target lands at the user-tier root with the original filename
    # (the migrate flattens to the user-tier root since user tier
    # doesn't have the same directory shape).
    user_target = user_tier / "rule.md"
    assert user_target.exists(), f"expected user-tier file at {user_target}"

    # SQL pin: chunk row preserved, scope flipped to user, project_root cleared.
    refreshed = await comp.storage.get_chunk(pre_chunk_id)
    assert refreshed is not None, "chunk row should still exist with same UUID"
    assert refreshed.metadata.scope == "user"
    assert refreshed.metadata.project_root is None
    assert refreshed.metadata.source_file == user_target


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
            [src.resolve()],
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


# ---------------------------------------------------------------------------
# 6) Issue #886 — chunk_links lineage preservation + glob input
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_memory_migrate_preserves_chunk_links_lineage_count(
    bm25_only_components, monkeypatch, tmp_path
):
    """Issue #886 regression pin: ``chunk_links`` row count is N pre/post.

    The v1 chunk-id-stable single-DB rename keeps ``chunks.id`` constant,
    so the entire ``chunk_links`` neighborhood (incoming + outgoing edges
    on the moved chunks) survives untouched. This test wires a real link
    between a chunk in source A (moving) and a chunk in source B (staying)
    and asserts the link row is byte-identical after the migrate. Pre-#886
    nothing pinned this — a future refactor that decided to DELETE link
    rows during rename would have been silent.
    """
    from memtomem.cli.context_cmd import _memory_migrate_run

    comp, mem_dir = bm25_only_components
    project_root = tmp_path / "proj_lineage"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_shared.mkdir(parents=True)
    (project_root / ".git").mkdir()
    comp.config.indexing.project_memory_dirs = [proj_shared]

    src_a = mem_dir / "rule_a.md"
    src_b = mem_dir / "rule_b.md"
    src_a.write_text("## A\n\nbody A.\n", encoding="utf-8")
    src_b.write_text("## B\n\nbody B.\n", encoding="utf-8")

    chunk_a = Chunk(
        content="body A.",
        metadata=ChunkMetadata(
            source_file=src_a, scope="user", project_root=None, start_line=3, end_line=3
        ),
        embedding=[0.1] * 1024,
    )
    chunk_b = Chunk(
        content="body B.",
        metadata=ChunkMetadata(
            source_file=src_b, scope="user", project_root=None, start_line=3, end_line=3
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([chunk_a, chunk_b])

    # Hand-wire a link a → b so the moved source has one outgoing edge
    # in its neighborhood. Direct SQL keeps the test independent of
    # mem_agent_share's surface (which has its own coverage).
    db = comp.storage._get_db()
    db.execute(
        "INSERT INTO chunk_links "
        "(source_id, target_id, link_type, namespace_target, created_at) "
        "VALUES (?, ?, 'shared', 'default', '2026-05-11T00:00:00')",
        (str(chunk_a.id), str(chunk_b.id)),
    )
    db.commit()

    pre_count = db.execute("SELECT COUNT(*) FROM chunk_links").fetchone()[0]
    pre_row = db.execute(
        "SELECT source_id, target_id, link_type, namespace_target "
        "FROM chunk_links WHERE target_id=?",
        (str(chunk_b.id),),
    ).fetchone()
    assert pre_count == 1

    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(project_root)

    # Migrate A only; B stays in user tier. Lineage row touches both
    # endpoints — if rename silently dropped it, post_count would be 0.
    await _memory_migrate_run(
        [src_a.resolve()],
        from_scope="user",
        to_scope="project_shared",
        apply_=True,
        yes=True,
        confirm_project_shared=True,
    )

    post_count = db.execute("SELECT COUNT(*) FROM chunk_links").fetchone()[0]
    post_row = db.execute(
        "SELECT source_id, target_id, link_type, namespace_target "
        "FROM chunk_links WHERE target_id=?",
        (str(chunk_b.id),),
    ).fetchone()

    assert post_count == pre_count, f"chunk_links count drifted: pre={pre_count} post={post_count}"
    assert post_row == pre_row, (
        "link row endpoints / metadata must be byte-identical after rename "
        "(chunk-id-stable single-DB rename promise)"
    )

    # And both chunks still reachable by ID; A's source flipped, B's didn't.
    refreshed_a = await comp.storage.get_chunk(chunk_a.id)
    refreshed_b = await comp.storage.get_chunk(chunk_b.id)
    assert refreshed_a is not None and refreshed_b is not None
    assert refreshed_a.metadata.scope == "project_shared"
    assert refreshed_a.metadata.source_file == proj_shared / "rule_a.md"
    assert refreshed_b.metadata.scope == "user"
    assert refreshed_b.metadata.source_file == src_b


def test_memory_migrate_lineage_display_reflects_neighborhood_size(
    monkeypatch, fake_project_layout
):
    """Issue #886: plan output reports the actual ``chunk_links``
    neighborhood size, not the hard-coded ``0 dropped`` string from v1.

    The displayed value pins the design contract for single-DB rename
    (the entire neighborhood is preserved); when cross-DB lands later,
    the same line surfaces "N preserved, K dropped" with a real K.
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    src = layout["src"]

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    comp.config.indexing.project_memory_dirs = [layout["proj_shared"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=5)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=3)
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
    assert "chunk_links lineage: 3 preserved, 0 dropped" in result.output
    # The pre-#886 hard-coded "0 dropped (chunk-id-stable single-DB rename)"
    # tail must not reappear via a future refactor.
    assert "chunk-id-stable single-DB rename" not in result.output


def test_memory_migrate_glob_pre_flight_all_or_nothing_on_privacy(monkeypatch, fake_project_layout):
    """Issue #886: glob pre-flight rejects the WHOLE batch on any privacy
    hit, before any FS move happens.

    Three user-tier files; the middle one carries a secret pattern; the
    target is ``project_shared`` (Gate A active). The command must exit
    non-zero and leave all three files at source — including the clean
    ones — because per ADR-0011 §5 we don't half-migrate a batch.
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    user_tier = layout["user_tier"]

    f1 = user_tier / "rule_one.md"
    f2 = user_tier / "rule_two.md"
    f3 = user_tier / "rule_three.md"
    f1.write_text("## One\n\nclean body.\n", encoding="utf-8")
    f2.write_text(f"## Two\n\n{_SECRET}\n", encoding="utf-8")
    f3.write_text("## Three\n\nclean body.\n", encoding="utf-8")
    # Default fixture's rule.md must not match the glob, so rename it out
    # of the way before patterning ``rule_*.md``.
    layout["src"].unlink()

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [user_tier]
    comp.config.indexing.project_memory_dirs = [layout["proj_shared"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=0)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(user_tier / "rule_*.md"),
            "--from",
            "user",
            "--to",
            "project_shared",
            "--apply",
            "--confirm-project-shared",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "Gate A" in out
    assert "rule_two.md" in out
    # All three files still at source — no half-batch on disk.
    assert f1.exists()
    assert f2.exists()
    assert f3.exists()
    target_dir = layout["proj_shared"]
    assert not (target_dir / "rule_one.md").exists()
    assert not (target_dir / "rule_two.md").exists()
    assert not (target_dir / "rule_three.md").exists()
    # And no DB UPDATE fired even for the clean files.
    comp.storage.update_chunks_scope_for_source.assert_not_called()


def test_memory_migrate_glob_apply_aborts_on_mid_batch_db_failure(monkeypatch, fake_project_layout):
    """Issue #886: mid-batch DB failure reverts THAT file, leaves earlier
    completed files migrated, and aborts before touching remaining files.

    Three user-tier files migrate to ``project_local`` (skip Gate A so
    we test the DB-failure branch cleanly). DB UPDATE raises on the 2nd
    call. Per-file FS-revert restores file 2 to its source path; file 1
    stays at target (already committed); file 3 is never attempted.
    Deterministic resumption point — the user can fix the cause and
    re-run on the remaining {f2, f3} glob.
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    user_tier = layout["user_tier"]
    proj_local = layout["proj_local"]

    f1 = user_tier / "rule_one.md"
    f2 = user_tier / "rule_two.md"
    f3 = user_tier / "rule_three.md"
    f1.write_text("## One\n\nclean body.\n", encoding="utf-8")
    f2.write_text("## Two\n\nclean body.\n", encoding="utf-8")
    f3.write_text("## Three\n\nclean body.\n", encoding="utf-8")
    layout["src"].unlink()

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [user_tier]
    comp.config.indexing.project_memory_dirs = [proj_local]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=0)

    call_count = {"n": 0}

    async def _fail_second(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated DB failure on file 2")
        return 1

    comp.storage.update_chunks_scope_for_source = AsyncMock(side_effect=_fail_second)
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(user_tier / "rule_*.md"),
            "--from",
            "user",
            "--to",
            "project_local",
            "--apply",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "DB update failed" in out
    assert "rule_two.md" in out
    assert "1 of 3 migrated" in out

    # File 1: migrated to project_local. File 2: reverted to source.
    # File 3: never touched.
    assert not f1.exists(), "file 1 should be moved to target (already committed)"
    assert (proj_local / "rule_one.md").exists()
    assert f2.exists(), "file 2 should be reverted to source on DB failure"
    assert not (proj_local / "rule_two.md").exists()
    assert f3.exists(), "file 3 should be untouched (batch aborted)"
    assert not (proj_local / "rule_three.md").exists()

    # update_chunks_scope_for_source was called twice (file 1 succeeded,
    # file 2 raised); file 3 was never attempted.
    assert comp.storage.update_chunks_scope_for_source.call_count == 2


# ---------------------------------------------------------------------------
# 7) Issue #886 — Codex review round 1 follow-ups (PR #912)
# ---------------------------------------------------------------------------


def test_memory_migrate_glob_rejects_duplicate_target_basenames(monkeypatch, fake_project_layout):
    """Codex Blocker 1: a recursive glob can match two sources in
    different subdirectories with the same basename. The flat rename
    ``to_dir / src.name`` would land both at the same destination —
    the second ``shutil.move`` silently overwriting the first migrated
    file. Pre-flight must reject the whole batch on collision so the
    user can disambiguate before any FS move happens.
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    user_tier = layout["user_tier"]

    sub_a = user_tier / "a"
    sub_b = user_tier / "b"
    sub_a.mkdir()
    sub_b.mkdir()
    (sub_a / "rule.md").write_text("## A\n\nbody.\n", encoding="utf-8")
    (sub_b / "rule.md").write_text("## B\n\nbody.\n", encoding="utf-8")
    # The fixture's top-level rule.md isn't matched by the recursive
    # glob below (it lives at user_tier/rule.md, the glob targets
    # subdirs) — keep it as a sentinel for "nothing else moved".

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [user_tier]
    comp.config.indexing.project_memory_dirs = [layout["proj_local"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=0)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(user_tier / "**" / "rule.md"),
            "--from",
            "user",
            "--to",
            "project_local",
            "--apply",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "Duplicate target after rename" in out
    assert "rule.md" in out
    # Both subdir sources still in place; no FS moves anywhere.
    assert (sub_a / "rule.md").exists()
    assert (sub_b / "rule.md").exists()
    assert not (layout["proj_local"] / "rule.md").exists()
    comp.storage.update_chunks_scope_for_source.assert_not_called()


def test_memory_migrate_apply_pass_aborts_on_post_preflight_secret(
    monkeypatch, fake_project_layout
):
    """Codex Blocker 2: pre-flight scans with ``record_outcome=False``;
    the apply pass re-scans with ``record_outcome=True``. The apply-pass
    guard's decision must be honored — if a secret was added to the
    source between pre-flight and apply (e.g. during the
    ``--confirm-project-shared`` prompt pause), the migration must abort
    before ``shutil.move`` lands the file in the git-tracked tier.

    Simulated by patching ``privacy.enforce_write_guard`` with a
    side-effect that returns ``pass`` on call #1 (pre-flight) and
    ``blocked_project_shared`` on call #2 (apply re-scan).
    """
    from memtomem.cli.context_cmd import memory_migrate_cmd
    from memtomem.privacy import RedactionHit, WriteGuardResult

    layout = fake_project_layout
    src = layout["src"]
    # Clean content at the time of pre-flight; the mock's call-count
    # gate is what changes the decision, not the file contents.
    src.write_text("## Clean\n\nharmless body.\n", encoding="utf-8")

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    comp.config.indexing.project_memory_dirs = [layout["proj_shared"]]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=0)
    comp.storage.update_chunks_scope_for_source = AsyncMock()
    comp.search_pipeline = AsyncMock()
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    call_count = {"n": 0}

    def _guard_changes_between_passes(*args, **kwargs):
        call_count["n"] += 1
        # Pre-flight: clean. Apply: blocked (simulated edit during the
        # prompt pause). Pre-flight uses ``record_outcome=False`` and
        # apply uses ``record_outcome=True`` — assert that ordering as
        # extra coverage of the contract.
        if call_count["n"] == 1:
            assert kwargs.get("record_outcome") is False
            return WriteGuardResult("pass", [])
        assert kwargs.get("record_outcome") is True
        return WriteGuardResult(
            "blocked_project_shared",
            [RedactionHit(pattern_index=0, span=(0, 8))],
        )

    monkeypatch.setattr("memtomem.privacy.enforce_write_guard", _guard_changes_between_passes)

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
            "--confirm-project-shared",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    assert "Gate A" in out
    assert "content changed since pre-flight" in out
    # Both guard calls happened (pre-flight + apply re-scan).
    assert call_count["n"] == 2
    # Source still at original path; no FS move and no DB UPDATE.
    assert src.exists()
    assert not (layout["proj_shared"] / src.name).exists()
    comp.storage.update_chunks_scope_for_source.assert_not_called()


def test_memory_migrate_double_failure_surfaces_batch_state(monkeypatch, fake_project_layout):
    """Codex Major 2: when DB update fails AND the filesystem rollback
    *also* fails (the highest-risk failure mode), the user must still
    learn the batch state — how many earlier files are already migrated.
    Pre-fix the inner branch was silent about K-of-N, so the user
    inspecting the divergent source/target pair didn't know whether
    other files needed inspection too.

    Two-file batch: file 1 succeeds; file 2's DB update raises AND its
    FS rollback raises. The inner branch must emit "Batch state: 1 of
    2 migrated" alongside the divergence warning.
    """
    import shutil as _shutil

    from memtomem.cli.context_cmd import memory_migrate_cmd

    layout = fake_project_layout
    user_tier = layout["user_tier"]
    proj_local = layout["proj_local"]

    f1 = user_tier / "rule_one.md"
    f2 = user_tier / "rule_two.md"
    f1.write_text("## One\n\nclean.\n", encoding="utf-8")
    f2.write_text("## Two\n\nclean.\n", encoding="utf-8")
    layout["src"].unlink()

    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [user_tier]
    comp.config.indexing.project_memory_dirs = [proj_local]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=0)

    db_calls = {"n": 0}

    async def _fail_on_second_db(*args, **kwargs):
        db_calls["n"] += 1
        if db_calls["n"] == 2:
            raise RuntimeError("simulated DB failure on file 2")
        return 1

    comp.storage.update_chunks_scope_for_source = AsyncMock(side_effect=_fail_on_second_db)
    comp.search_pipeline = AsyncMock()

    # Wrap shutil.move so it succeeds on outbound moves but raises on
    # the rollback attempt for file 2. The rollback is identified by
    # its argument pattern: forward = (user_tier_file → proj_local),
    # rollback = (proj_local → user_tier_file). We trip the second
    # case only when the source is f2's target.
    real_move = _shutil.move

    def _fake_move(src_path, dst_path):
        if str(src_path) == str(proj_local / "rule_two.md") and str(dst_path) == str(f2):
            raise OSError("simulated rollback failure for file 2")
        return real_move(src_path, dst_path)

    # ``shutil`` is imported locally inside ``_memory_migrate_run`` so
    # patching ``memtomem.cli.context_cmd.shutil.move`` doesn't resolve.
    # The shutil module object is a singleton per process — patching its
    # ``move`` attribute is observed by every importer.
    monkeypatch.setattr("shutil.move", _fake_move)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    runner = CliRunner()
    result = runner.invoke(
        memory_migrate_cmd,
        [
            str(user_tier / "rule_*.md"),
            "--from",
            "user",
            "--to",
            "project_local",
            "--apply",
            "--yes",
        ],
    )
    assert result.exit_code != 0
    out = result.output + str(result.exception or "")
    # Inner-branch divergence warning is present.
    assert "filesystem rollback failed" in out
    assert "rule_two.md" in out
    # Codex Major 2 fix: batch state surfaces even in the double-failure
    # branch.
    assert "Batch state: 1 of 2 migrated" in out
    # Both root causes preserved in the final exception message.
    assert "db_error" in out and "rollback_error" in out

    # FS pin: file 1 made it to the target tier; file 2 left divergent
    # (target side has the content because forward-move succeeded but
    # rollback failed). File 3 was never present.
    assert not f1.exists()
    assert (proj_local / "rule_one.md").exists()
    assert (proj_local / "rule_two.md").exists()
    assert not f2.exists()
