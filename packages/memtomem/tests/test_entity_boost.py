"""Tests for the Stage-7b entity-match boost."""

from pathlib import Path
from uuid import uuid4

import pytest
from memtomem.models import Chunk, ChunkMetadata, SearchResult
from memtomem.search.entity_boost import (
    _MAX_QUERY_ENTITIES,
    apply_entity_boost,
    entity_boost_factor,
    extract_query_entities,
)

_DEFAULT_TYPES = ["technology", "person", "date"]


def _make_result(score, chunk_id=None):
    cid = chunk_id or uuid4()
    chunk = Chunk(
        content="test",
        metadata=ChunkMetadata(source_file=Path("/tmp/test.md")),
        id=cid,
        embedding=[],
    )
    return SearchResult(chunk=chunk, score=score, rank=1, source="test")


class TestEntityBoostFactor:
    def test_no_match(self):
        assert entity_boost_factor(0, 3) == 1.0

    def test_full_coverage(self):
        assert entity_boost_factor(3, 3, max_boost=1.5) == pytest.approx(1.5)

    def test_half_coverage_is_midpoint(self):
        assert entity_boost_factor(1, 2, max_boost=1.5) == pytest.approx(1.25)

    def test_monotonic_in_matches(self):
        factors = [entity_boost_factor(m, 4, max_boost=2.0) for m in range(5)]
        assert factors == sorted(factors)
        assert factors[0] == 1.0
        assert factors[-1] == pytest.approx(2.0)

    def test_zero_total_is_inert(self):
        # No query entities -> no signal. The stage skips, but the scalar must
        # still be safe on its own.
        assert entity_boost_factor(0, 0) == 1.0
        assert entity_boost_factor(3, 0) == 1.0

    def test_matches_clamped_to_total(self):
        assert entity_boost_factor(9, 2, max_boost=1.5) == pytest.approx(1.5)

    def test_degenerate_max_boost(self):
        assert entity_boost_factor(2, 2, max_boost=1.0) == pytest.approx(1.0)


class TestExtractQueryEntities:
    def test_known_technology(self):
        assert extract_query_entities("how to use sqlite here", _DEFAULT_TYPES) == [
            ("technology", "sqlite")
        ]

    def test_technology_needs_word_boundary(self):
        # The extractor's known-tech scan is a plain substring `find`, so "git"
        # hits inside "digit". Query-side that would be a phantom third of the
        # coverage denominator.
        assert extract_query_entities("digit parsing", ["technology"]) == []
        assert extract_query_entities("git rebase", ["technology"]) == [("technology", "git")]

    def test_values_are_lowercased(self):
        keys = extract_query_entities("SQLite migration", ["technology"])
        assert keys == [("technology", "sqlite")]

    def test_person_mention(self):
        assert ("person", "@alice") in extract_query_entities("ping @alice", _DEFAULT_TYPES)

    def test_iso_date(self):
        assert ("date", "2026-08-17") in extract_query_entities("notes 2026-08-17", _DEFAULT_TYPES)

    def test_restricted_types_are_honored(self):
        keys = extract_query_entities("sqlite notes from @alice", ["person"])
        assert keys == [("person", "@alice")]

    def test_deduplicates(self):
        keys = extract_query_entities("sqlite and sqlite again", ["technology"])
        assert keys == [("technology", "sqlite")]

    def test_deterministic_and_capped(self):
        query = " ".join(f"@user{i}" for i in range(_MAX_QUERY_ENTITIES + 4))
        first = extract_query_entities(query, ["person"])
        assert len(first) == _MAX_QUERY_ENTITIES
        # Same query -> same keys, in the same order (cached/replayed searches
        # must reproduce the boost exactly).
        assert extract_query_entities(query, ["person"]) == first
        # Cap keeps the earliest query terms.
        assert first[0] == ("person", "@user0")

    def test_empty_inputs(self):
        assert extract_query_entities("", _DEFAULT_TYPES) == []
        assert extract_query_entities("sqlite", []) == []


class TestApplyEntityBoost:
    def test_reorders_by_coverage(self):
        r1 = _make_result(0.50)
        r2 = _make_result(0.40)
        matches = {str(r2.chunk.id): {("technology", "sqlite"), ("person", "@alice")}}
        boosted = apply_entity_boost([r1, r2], matches, 2, max_boost=1.5)
        assert boosted[0].chunk.id == r2.chunk.id
        assert boosted[0].score == pytest.approx(0.40 * 1.5)

    def test_unmatched_chunks_keep_score(self):
        r = _make_result(0.5)
        boosted = apply_entity_boost([r], {}, 2)
        assert boosted[0].score == pytest.approx(0.5)

    def test_partial_coverage(self):
        r = _make_result(0.5)
        matches = {str(r.chunk.id): {("technology", "sqlite")}}
        boosted = apply_entity_boost([r], matches, 2, max_boost=1.5)
        assert boosted[0].score == pytest.approx(0.5 * 1.25)

    def test_ranks_renumbered(self):
        r1, r2, r3 = _make_result(0.3), _make_result(0.2), _make_result(0.1)
        matches = {str(r3.chunk.id): {("technology", "sqlite")}}
        boosted = apply_entity_boost([r1, r2, r3], matches, 1, max_boost=2.0)
        assert [b.rank for b in boosted] == [1, 2, 3]
        assert boosted[0].chunk.id == r1.chunk.id  # 0.3 still beats 0.1 * 2.0

    def test_never_penalizes(self):
        # Sparse coverage is the norm (rows exist only for scanned chunks), so
        # an unmatched chunk must be untouched, not demoted.
        rs = [_make_result(0.5), _make_result(0.4)]
        boosted = apply_entity_boost(rs, {}, 3)
        assert [b.score for b in boosted] == pytest.approx([0.5, 0.4])

    def test_empty_results(self):
        assert apply_entity_boost([], {}, 2) == []

    def test_zero_query_entities_passthrough(self):
        r = _make_result(0.5)
        out = apply_entity_boost([r], {str(r.chunk.id): {("technology", "x")}}, 0)
        assert out[0].score == pytest.approx(0.5)


class TestNegativeScoreBoost:
    """A boost must never demote — including on the rerank scale, where local
    cross-encoders emit raw logits that are routinely negative."""

    def test_scalar_moves_negative_scores_up(self):
        from memtomem.search.access import boosted_score

        assert boosted_score(-0.3, 1.5) == pytest.approx(-0.2)
        assert boosted_score(0.3, 1.5) == pytest.approx(0.45)
        assert boosted_score(0.0, 1.5) == pytest.approx(0.0)
        assert boosted_score(-0.3, 1.0) == pytest.approx(-0.3)  # no-op factor

    def test_stronger_factor_never_lowers_the_score(self):
        from memtomem.search.access import boosted_score

        for score in (-2.0, -0.3, 0.0, 0.3, 2.0):
            weak, strong = boosted_score(score, 1.2), boosted_score(score, 2.0)
            assert strong >= weak >= score

    def test_matched_result_outranks_unmatched_on_negative_scale(self):
        matched = _make_result(-0.30)
        unmatched = _make_result(-0.90)
        boosted = apply_entity_boost(
            [matched, unmatched],
            {str(matched.chunk.id): {("technology", "sqlite")}},
            1,
            max_boost=1.5,
        )
        assert boosted[0].chunk.id == matched.chunk.id
        assert boosted[0].score == pytest.approx(-0.2)
        assert boosted[1].score == pytest.approx(-0.9)

    def test_sibling_boosts_share_the_contract(self):
        from memtomem.search.access import apply_access_boost
        from memtomem.search.importance import apply_importance_boost

        hi, lo = _make_result(-0.3), _make_result(-0.9)
        for boosted in (
            apply_access_boost([hi, lo], {str(hi.chunk.id): 100}),
            apply_importance_boost([hi, lo], {str(hi.chunk.id): 1.0}),
        ):
            assert boosted[0].chunk.id == hi.chunk.id
            assert boosted[0].score > hi.score
