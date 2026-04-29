"""Trust-boundary redaction guard at mem_add / mem_batch_add ingress.

Pinned behavior:

- ``mem_add`` blocks content that matches a privacy pattern; counter
  records ``blocked``; no on-disk write occurs.
- ``mem_add(force_unsafe=True)`` bypasses the block; counter records
  ``bypassed``; the file is created.
- Clean content always passes; counter records ``pass``.
- ``mem_batch_add`` rejects the whole batch on any hit (transactional);
  the error message lists the hit indices and no file is created.
- ``mem_batch_add(force_unsafe=True)`` records ``bypassed`` per hit item
  and ``pass`` per clean item.
- ``mem_add_redaction_stats`` surfaces the live counter snapshot.

Counter assertions are paired with each behavior pin so a future change
that drops the ``record(...)`` calls without touching block / pass logic
fails the test instead of regressing silently.
"""

from __future__ import annotations

import json

import pytest

from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import (
    mem_add,
    mem_add_redaction_stats,
    mem_batch_add,
)

from helpers import StubCtx

# Sample matches pattern #4 (sk-...) — no other ambiguity, single-pattern hit.
_SECRET_SAMPLE = "Notes on token: sk-" + "a" * 30
_CLEAN_SAMPLE = "Met with the team about Q2 deploy plans."


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


class TestMemAddRedactionGuard:
    @pytest.mark.asyncio
    async def test_blocks_secret_and_records_blocked_outcome(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "block.md"

        before = privacy.snapshot()["outcomes"]["blocked"]
        result = await mem_add(  # type: ignore[arg-type]
            content=_SECRET_SAMPLE,
            file=str(target),
            ctx=ctx,
        )
        after = privacy.snapshot()["outcomes"]["blocked"]

        assert "Error" in result
        assert "privacy pattern" in result
        assert "force_unsafe" in result
        assert after == before + 1, "Block must increment the blocked counter"
        assert not target.exists(), "Blocked write must not create the file"

    @pytest.mark.asyncio
    async def test_force_unsafe_passes_and_records_bypassed(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "bypass.md"

        before = privacy.snapshot()["outcomes"]
        result = await mem_add(  # type: ignore[arg-type]
            content=_SECRET_SAMPLE,
            file=str(target),
            force_unsafe=True,
            ctx=ctx,
        )
        after = privacy.snapshot()["outcomes"]

        assert "Memory added" in result, f"Expected success, got: {result!r}"
        assert after["bypassed"] == before["bypassed"] + 1
        assert after["blocked"] == before["blocked"], (
            "Bypass must not increment the blocked counter"
        )
        assert target.exists()

    @pytest.mark.asyncio
    async def test_clean_content_records_pass(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "clean.md"

        before = privacy.snapshot()["outcomes"]
        await mem_add(  # type: ignore[arg-type]
            content=_CLEAN_SAMPLE,
            file=str(target),
            ctx=ctx,
        )
        after = privacy.snapshot()["outcomes"]

        assert after["pass"] == before["pass"] + 1
        assert after["blocked"] == before["blocked"]
        assert after["bypassed"] == before["bypassed"]

    @pytest.mark.asyncio
    async def test_clean_content_with_force_unsafe_records_pass_not_bypassed(
        self, bm25_only_components
    ):
        """``force_unsafe=True`` without a hit must still record ``pass``.

        ``bypassed`` is only meaningful when the guard would have blocked;
        a clean write with the kwarg set is no different from a clean
        write without it. Pin so the bypass label keeps measuring real
        escape-hatch usage rather than degrading into "kwarg was passed."
        """
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "clean_with_force.md"

        before = privacy.snapshot()["outcomes"]
        await mem_add(  # type: ignore[arg-type]
            content=_CLEAN_SAMPLE,
            file=str(target),
            force_unsafe=True,
            ctx=ctx,
        )
        after = privacy.snapshot()["outcomes"]

        assert after["pass"] == before["pass"] + 1
        assert after["bypassed"] == before["bypassed"], (
            "force_unsafe with no hit must not increment bypassed"
        )
        assert after["blocked"] == before["blocked"]
        assert target.exists()


class TestMemBatchAddRedactionGuard:
    @pytest.mark.asyncio
    async def test_full_reject_lists_hit_indices(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "batch_block.md"

        before = privacy.snapshot()["by_tool"].get(
            "mem_batch_add", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        result = await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "Clean", "value": _CLEAN_SAMPLE},
                {"key": "Secret", "value": _SECRET_SAMPLE},
                {"key": "Also clean", "value": "Another normal note."},
                {"key": "Also secret", "value": "AKIAIOSFODNN7EXAMPLE"},
            ],
            file=str(target),
            ctx=ctx,
        )
        after = privacy.snapshot()["by_tool"]["mem_batch_add"]

        assert "Error" in result
        assert "[1, 3]" in result, f"Hit indices missing from error: {result!r}"
        assert "whole batch rejected" in result
        # Two hit items → two blocked records; clean items get nothing
        # because no write happened (transactional reject).
        assert after["blocked"] == before["blocked"] + 2
        assert after["pass"] == before["pass"], "Pass must not record on rejected batch"
        assert not target.exists()

    @pytest.mark.asyncio
    async def test_force_unsafe_records_bypassed_per_hit_and_pass_per_clean(
        self, bm25_only_components
    ):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "batch_bypass.md"

        before = privacy.snapshot()["by_tool"].get(
            "mem_batch_add", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        result = await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "Clean", "value": _CLEAN_SAMPLE},
                {"key": "Secret", "value": _SECRET_SAMPLE},
            ],
            file=str(target),
            force_unsafe=True,
            ctx=ctx,
        )
        after = privacy.snapshot()["by_tool"]["mem_batch_add"]

        assert "Batch add complete" in result
        assert after["bypassed"] == before["bypassed"] + 1
        assert after["pass"] == before["pass"] + 1
        assert target.exists()

    @pytest.mark.asyncio
    async def test_clean_batch_records_pass_per_entry(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)
        target = mem_dir / "batch_clean.md"

        before = privacy.snapshot()["by_tool"].get(
            "mem_batch_add", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        await mem_batch_add(  # type: ignore[arg-type]
            entries=[
                {"key": "A", "value": _CLEAN_SAMPLE},
                {"key": "B", "value": "Another normal note."},
            ],
            file=str(target),
            ctx=ctx,
        )
        after = privacy.snapshot()["by_tool"]["mem_batch_add"]

        assert after["pass"] == before["pass"] + 2
        assert after["blocked"] == before["blocked"]


class TestRedactionStatsTool:
    @pytest.mark.asyncio
    async def test_snapshot_tool_returns_outcomes_and_by_tool(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        # Generate one of each outcome via mem_add (covers two tools' worth
        # of by_tool keys via the batch test below).
        await mem_add(  # type: ignore[arg-type]
            content=_CLEAN_SAMPLE,
            file=str(mem_dir / "stats_pass.md"),
            ctx=ctx,
        )
        await mem_add(  # type: ignore[arg-type]
            content=_SECRET_SAMPLE,
            file=str(mem_dir / "stats_block.md"),
            ctx=ctx,
        )
        await mem_add(  # type: ignore[arg-type]
            content=_SECRET_SAMPLE,
            file=str(mem_dir / "stats_bypass.md"),
            force_unsafe=True,
            ctx=ctx,
        )

        result = await mem_add_redaction_stats(ctx=ctx)  # type: ignore[arg-type]
        snap = json.loads(result)

        assert snap["outcomes"]["pass"] >= 1
        assert snap["outcomes"]["blocked"] >= 1
        assert snap["outcomes"]["bypassed"] >= 1
        assert "mem_add" in snap["by_tool"]
        assert snap["by_tool"]["mem_add"]["blocked"] >= 1
