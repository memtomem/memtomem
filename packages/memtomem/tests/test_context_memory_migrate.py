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

    await _memory_migrate_run(
        src.resolve(),
        from_scope="user",
        to_scope="project_shared",
        apply_=True,
        yes=True,
        confirm_project_shared=False,
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
    # Refusal must happen before any FS / DB mutation.
    comp.storage.update_chunks_scope_for_source.assert_not_called()
    assert src.exists()
    assert not (layout["proj_shared"] / "rule.md").exists()
