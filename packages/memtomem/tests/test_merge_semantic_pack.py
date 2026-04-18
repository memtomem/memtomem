"""Per-failure-mode tests for the three-pass merge in _merge_short_chunks.

Each test pins one behaviour so regressions surface at the exact pass that
broke: Pass 1 (min enforcement, hierarchy-agnostic), Pass 2 (greedy packing,
hierarchy-respecting), Pass 3 (tail backward sweep).
"""

from __future__ import annotations

from pathlib import Path

from memtomem.indexing.engine import _merge_short_chunks
from memtomem.models import Chunk, ChunkMetadata


def _chunk(
    content: str,
    heading: tuple[str, ...] = (),
    source: str = "/tmp/t.md",
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(source),
            heading_hierarchy=heading,
        ),
    )


class TestSemanticPack:
    def test_ancestor_absorbs_short_parent(self):
        """Pass 1: short parent absorbs descendant under same root.

        Common-prefix unification keeps hierarchy honest across chained
        merges; the dropped descendant heading is prepended to the body so
        retrieval still picks up the subsection signal.
        """
        parent = _chunk("x" * 120, heading=("# A",))  # ~30 tokens
        child = _chunk("y" * 1600, heading=("# A", "## B"))  # ~400 tokens

        result = _merge_short_chunks(
            [parent, child], min_tokens=128, max_tokens=512, target_tokens=0
        )
        assert len(result) == 1
        # Common-prefix rule: hierarchy collapses to the shared ancestor.
        assert result[0].metadata.heading_hierarchy == ("# A",)
        # Both bodies preserved and dropped heading restored inline
        assert "x" * 120 in result[0].content
        assert "y" * 1600 in result[0].content
        assert "## B" in result[0].content

    def test_sibling_pack_up_to_target(self):
        """Pass 2: 3× 200-token siblings with target=384 produce exactly 2 chunks.

        Packing stops as soon as cur >= target (400 >= 384), leaving the
        third sibling alone. Enforces the target-as-soft-goal semantics.
        """
        s1 = _chunk("x" * 800, heading=("# P", "## S1"))  # ~200 tokens
        s2 = _chunk("y" * 800, heading=("# P", "## S2"))
        s3 = _chunk("z" * 800, heading=("# P", "## S3"))

        result = _merge_short_chunks(
            [s1, s2, s3], min_tokens=128, max_tokens=512, target_tokens=384
        )
        assert len(result) == 2

    def test_tail_orphan_merged_backward(self):
        """Pass 3: final short chunk merges backward into its predecessor."""
        head = _chunk("x" * 800, heading=("# A",))  # ~200 tokens, above min
        tail = _chunk("y" * 120, heading=("# A",))  # ~30 tokens, below min, alone

        result = _merge_short_chunks([head, tail], min_tokens=128, max_tokens=512, target_tokens=0)
        assert len(result) == 1
        assert "y" * 120 in result[0].content

    def test_max_ceiling_respected(self):
        """Pass 2: combined > max blocks merge even when cur < target."""
        big1 = _chunk("x" * 1200, heading=("# P", "## S1"))  # ~300 tokens
        big2 = _chunk("y" * 1200, heading=("# P", "## S2"))  # ~300 tokens
        # target > max is nonsense at config level, but the function must
        # still honour the max ceiling rather than merge greedily.
        result = _merge_short_chunks(
            [big1, big2], min_tokens=128, max_tokens=512, target_tokens=1000
        )
        assert len(result) == 2

    def test_rollback_target_zero(self):
        """target_tokens=0 disables Pass 2 packing (behavioural rollback)."""
        s1 = _chunk("x" * 800, heading=("# P", "## S1"))
        s2 = _chunk("y" * 800, heading=("# P", "## S2"))
        s3 = _chunk("z" * 800, heading=("# P", "## S3"))

        result = _merge_short_chunks([s1, s2, s3], min_tokens=128, max_tokens=512, target_tokens=0)
        assert len(result) == 3

    def test_target_equals_min_no_packing(self):
        """target_tokens <= min_tokens disables Pass 2 (gate is strict >)."""
        s1 = _chunk("x" * 600, heading=("# P", "## S1"))  # ~150 tokens
        s2 = _chunk("y" * 600, heading=("# P", "## S2"))
        s3 = _chunk("z" * 600, heading=("# P", "## S3"))

        result = _merge_short_chunks(
            [s1, s2, s3], min_tokens=128, max_tokens=512, target_tokens=128
        )
        assert len(result) == 3


class TestSiblingMergeContentPreservation:
    def test_sibling_merge_prepends_dropped_leaves(self):
        """When sibling merge collapses to a common parent, each side's
        diverging leaf heading is prepended to its body so retrieval keeps
        the breadcrumb signal.
        """
        s1 = _chunk("alpha body", heading=("# P", "## S1"))
        s2 = _chunk("beta body", heading=("# P", "## S2"))

        result = _merge_short_chunks([s1, s2], min_tokens=128, max_tokens=512, target_tokens=0)
        assert len(result) == 1
        merged = result[0]
        assert merged.metadata.heading_hierarchy == ("# P",)
        # Dropped leaves restored in body
        assert "## S1" in merged.content
        assert "## S2" in merged.content
        assert "alpha body" in merged.content
        assert "beta body" in merged.content
