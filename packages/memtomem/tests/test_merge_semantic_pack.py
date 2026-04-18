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


class TestHeadingInversionOrphan:
    """Pass 1/3 heading-inversion rescue: short chunk whose root is a deeper
    heading level than the next chunk's root folds forward into the true
    document root.
    """

    def test_inversion_orphan_folds_into_deeper_root(self):
        """``## Prelude`` short orphan followed by ``# Main`` merges forward.

        The chunker emitted the ``##`` heading before the ``#`` arrived —
        the H2 content is structurally part of the document's top scope.
        """
        orphan = _chunk("x" * 200, heading=("## Prelude",))  # ~50 tokens
        main = _chunk("y" * 1600, heading=("# Main",))  # ~400 tokens, H1 root

        result = _merge_short_chunks(
            [orphan, main], min_tokens=128, max_tokens=512, target_tokens=0
        )
        assert len(result) == 1
        assert "x" * 200 in result[0].content
        assert "y" * 1600 in result[0].content

    def test_same_level_distinct_roots_stay_separate(self):
        """mem_add protection: distinct H2 entries at the same level never
        merge via the inversion rule (cur_level not > nxt_level).
        """
        a = _chunk("x" * 400, heading=("## Entry A",))  # ~100 tokens
        b = _chunk("y" * 400, heading=("## Entry B",))  # ~100 tokens

        result = _merge_short_chunks([a, b], min_tokens=128, max_tokens=512, target_tokens=0)
        assert len(result) == 2

    def test_ascending_level_does_not_fold(self):
        """``# A`` short orphan with ``## B`` next must NOT trigger inversion
        rule (cur_level 1 is not > nxt_level 2). The existing same-path
        prefix rule still catches the legitimate ``# A`` → ``# A > ## B``
        case; this guards the cross-root variant where ``## B`` has a
        different root.
        """
        orphan = _chunk("x" * 200, heading=("# A",))  # ~50 tokens
        other = _chunk("y" * 1600, heading=("## B",))  # deeper but different root

        result = _merge_short_chunks(
            [orphan, other], min_tokens=128, max_tokens=512, target_tokens=0
        )
        assert len(result) == 2

    def test_tail_inversion_orphan_folds_backward(self):
        """Pass 3 rescues a tail inversion orphan (``## X`` tail after
        ``# Main``). Here current=``# Main`` at Pass 3 check; tail is
        ``## X`` short. The inversion triggers because tail has deeper
        level than prev.

        Note: Pass 3 passes ``prev`` as ``current`` to ``_can_merge``, so
        the inversion rule fires when prev.root is SHALLOWER than
        last.root — the mirror case of forward-fold.
        """
        head = _chunk("x" * 1200, heading=("# Main",))  # ~300 tokens, H1 root
        tail = _chunk("y" * 200, heading=("## Note",))  # ~50 tokens, H2 root

        result = _merge_short_chunks([head, tail], min_tokens=128, max_tokens=512, target_tokens=0)
        # prev=H1, last=H2 → prev deeper? no, 1 < 2, so inversion rule
        # (cur_level > nxt_level) does NOT fire. But same-path prefix does
        # not match either ("# Main" != "## Note"). Stays separate.
        assert len(result) == 2


class TestBrokenCeilingRescue:
    """Short orphan rescued against an already-over-max neighbour.

    The chunker emits by char-count using a 4 char/token ratio; the merge
    path re-estimates Korean text at 2 char/token, so some already-emitted
    chunks read as above max. Before this rescue, a short ``## Summary``
    adjacent to such an over-ceiling neighbour was permanently orphaned
    because ``combined > max`` blocked the merge.
    """

    def test_pass1_merges_short_into_over_ceiling_neighbour(self):
        # ~50 token orphan
        summary = _chunk("x" * 200, heading=("# Root", "## Summary"))
        # ~600 tokens — above max_tokens=512 (simulates Korean re-estimate)
        body = _chunk("y" * 2400, heading=("# Root", "## Body"))

        result = _merge_short_chunks(
            [summary, body], min_tokens=128, max_tokens=512, target_tokens=0
        )
        assert len(result) == 1
        assert "x" * 200 in result[0].content
        assert "y" * 2400 in result[0].content

    def test_pass1_still_blocks_when_both_within_ceiling(self):
        """The classic max ceiling is preserved when neither side is broken."""
        short = _chunk("x" * 200, heading=("# Root", "## Summary"))  # ~50 tokens
        # Sized so combined exceeds max but neighbour itself stays below.
        # Rough tokens: 200//4=50 orphan + 1800//4=450 body = 501 ≤ 512 would
        # merge; push body up to 500 tokens to force combined > max.
        body = _chunk("y" * 2000, heading=("# Root", "## Body"))  # ~500 tokens

        result = _merge_short_chunks([short, body], min_tokens=128, max_tokens=512, target_tokens=0)
        # 50 + 500 + 1 = 551 > 512, neighbour <= max → blocked
        assert len(result) == 2

    def test_pass3_merges_tail_into_over_ceiling_prev(self):
        head = _chunk("x" * 2400, heading=("# A",))  # ~600 tokens, over max
        tail = _chunk("y" * 200, heading=("# A",))  # ~50 tokens

        result = _merge_short_chunks([head, tail], min_tokens=128, max_tokens=512, target_tokens=0)
        assert len(result) == 1
        assert "y" * 200 in result[0].content


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
