"""MCP contract tests for ``mem_search_feedback`` (#1801).

The tool is a thin surface over ``storage.save_search_feedback`` /
``get_search_feedback``: every validation error must arrive as an
``Error: ...`` string (never an exception), the argument truth table is
closed, and search itself stays independent of feedback availability.
"""

from __future__ import annotations

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools.search_history import mem_search_feedback


async def _observed_run(components, memory_dir) -> tuple[str, str]:
    """Index one note, run a ranked search, return (run_id, chunk_id)."""
    note = memory_dir / "quality.md"
    note.write_text("# Quality\n\nRelevance feedback loop for search runs.\n", encoding="utf-8")
    await components.index_engine.index_file(note)
    results, stats = await components.search_pipeline.search("relevance feedback", top_k=5)
    assert results and stats.query_run_id is not None
    return stats.query_run_id, str(results[0].chunk.id)


async def test_write_idempotent_replace_and_read_modes(bm25_only_components):
    components, memory_dir = bm25_only_components
    run_id, chunk_id = await _observed_run(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    recorded = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, judgment="relevant", ctx=ctx
    )
    assert recorded.startswith(f"Feedback recorded: run={run_id} chunk={chunk_id}")

    unchanged = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, judgment="relevant", ctx=ctx
    )
    assert unchanged.startswith("Feedback unchanged:")

    conflict = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, judgment="not_relevant", ctx=ctx
    )
    assert conflict.startswith("Error:") and "replace=true" in conflict

    replaced = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, judgment="not_relevant", replace=True, ctx=ctx
    )
    assert replaced.startswith("Feedback replaced:") and "not_relevant" in replaced

    listing = await mem_search_feedback(run_id=run_id, ctx=ctx)  # type: ignore[arg-type]
    assert f"Feedback for run {run_id} (1 judgments):" in listing
    assert f"{chunk_id}: not_relevant" in listing


async def test_read_mode_empty_run(bm25_only_components):
    components, memory_dir = bm25_only_components
    run_id, _ = await _observed_run(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    output = await mem_search_feedback(run_id=run_id, ctx=ctx)  # type: ignore[arg-type]
    assert output == f"No feedback recorded for run {run_id}."


async def test_error_strings_for_invalid_ids_and_vocabulary(bm25_only_components):
    components, memory_dir = bm25_only_components
    run_id, chunk_id = await _observed_run(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    unknown_run = await mem_search_feedback(  # type: ignore[arg-type]
        run_id="00000000-0000-4000-8000-000000000000",
        chunk_id=chunk_id,
        judgment="relevant",
        ctx=ctx,
    )
    assert unknown_run.startswith("Error:") and "not found" in unknown_run

    ghost_chunk = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id="ghost", judgment="relevant", ctx=ctx
    )
    assert ghost_chunk.startswith("Error:") and "result snapshot" in ghost_chunk

    bad_judgment = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, judgment="maybe", ctx=ctx
    )
    assert bad_judgment.startswith("Error:") and "judgment must be one of" in bad_judgment


async def test_argument_truth_table_rejects_meaningless_combinations(bm25_only_components):
    components, memory_dir = bm25_only_components
    run_id, chunk_id = await _observed_run(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))

    missing_chunk = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, judgment="relevant", ctx=ctx
    )
    assert missing_chunk == "Error: chunk_id is required when judgment is given."

    chunk_only = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, ctx=ctx
    )
    assert chunk_only == "Error: judgment is required when chunk_id is given."

    replace_only = await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, replace=True, ctx=ctx
    )
    assert replace_only == "Error: replace is only valid when judgment is given."


async def test_search_stays_independent_of_feedback(bm25_only_components):
    """#1801 acceptance: search never reads or writes the feedback table."""
    components, memory_dir = bm25_only_components
    run_id, chunk_id = await _observed_run(components, memory_dir)
    ctx = StubCtx(AppContext.from_components(components))
    await mem_search_feedback(  # type: ignore[arg-type]
        run_id=run_id, chunk_id=chunk_id, judgment="relevant", ctx=ctx
    )
    before = (
        components.storage._get_db()
        .execute("SELECT run_id, chunk_id, judgment, created_at, updated_at FROM search_feedback")
        .fetchall()
    )

    results, stats = await components.search_pipeline.search("relevance feedback", top_k=5)

    assert results and stats.query_run_id != run_id
    after = (
        components.storage._get_db()
        .execute("SELECT run_id, chunk_id, judgment, created_at, updated_at FROM search_feedback")
        .fetchall()
    )
    assert after == before
