"""Tests for indexing/differ.py compute_diff — pure hash-based chunk diffing."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.indexing.differ import compute_diff
from memtomem.models import Chunk, ChunkMetadata


def _mk(content: str) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path("/t.md")),
        embedding=[],
    )


class TestComputeDiff:
    def test_all_new_when_existing_is_empty(self):
        chunks = [_mk("a"), _mk("b")]
        result = compute_diff({}, chunks)

        assert result.to_upsert == chunks
        assert result.to_delete == []
        assert result.unchanged == []

    def test_all_unchanged_when_every_hash_matches(self):
        chunks = [_mk("a"), _mk("b")]
        existing = {
            str(uuid4()): chunks[0].content_hash,
            str(uuid4()): chunks[1].content_hash,
        }

        result = compute_diff(existing, chunks)

        assert len(result.unchanged) == 2
        assert result.to_upsert == []
        assert result.to_delete == []

    def test_mixed_new_unchanged_and_stale(self):
        unchanged = _mk("keep")
        new = _mk("new")
        stale_id = uuid4()
        existing = {
            str(uuid4()): unchanged.content_hash,
            str(stale_id): "hash-no-longer-present",
        }

        result = compute_diff(existing, [unchanged, new])

        assert result.unchanged == [unchanged]
        assert result.to_upsert == [new]
        assert result.to_delete == [stale_id]

    def test_deletions_when_new_chunks_empty(self):
        stale_a, stale_b = uuid4(), uuid4()
        existing = {str(stale_a): "h1", str(stale_b): "h2"}

        result = compute_diff(existing, [])

        assert set(result.to_delete) == {stale_a, stale_b}
        assert result.to_upsert == []
        assert result.unchanged == []

    def test_duplicate_content_hash_reuses_each_id_at_most_once(self):
        # Two new chunks with identical content share one hash. Two existing
        # chunk IDs also share that hash. Each ID must be reused exactly once
        # — no ID collisions allowed.
        chunks = [_mk("dup"), _mk("dup")]
        assert chunks[0].content_hash == chunks[1].content_hash

        id_a, id_b = uuid4(), uuid4()
        existing = {str(id_a): chunks[0].content_hash, str(id_b): chunks[0].content_hash}

        result = compute_diff(existing, chunks)

        assert len(result.unchanged) == 2
        assert result.to_upsert == []
        assert result.to_delete == []
        reused_ids = {str(c.id) for c in result.unchanged}
        assert reused_ids == {str(id_a), str(id_b)}

    def test_duplicate_hash_partial_reuse_spills_to_upsert(self):
        # Three new chunks share a hash but only two existing IDs match —
        # the third chunk must go to to_upsert rather than silently
        # reusing a duplicate ID.
        chunks = [_mk("x"), _mk("x"), _mk("x")]
        id_a, id_b = uuid4(), uuid4()
        existing = {str(id_a): chunks[0].content_hash, str(id_b): chunks[0].content_hash}

        result = compute_diff(existing, chunks)

        assert len(result.unchanged) == 2
        assert len(result.to_upsert) == 1
        assert result.to_delete == []

    def test_reordering_is_recognized_as_unchanged(self):
        a, b = _mk("first"), _mk("second")
        existing = {str(uuid4()): a.content_hash, str(uuid4()): b.content_hash}

        # Pass in the opposite order — hash-based matching should not care.
        result = compute_diff(existing, [b, a])

        assert len(result.unchanged) == 2
        assert result.to_upsert == []
        assert result.to_delete == []

    def test_heading_change_promotes_hash_match_to_upsert_with_stable_id(self):
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("New heading",))
        existing_id = uuid4()
        existing = {str(existing_id): (chunk.content_hash, ("Old heading",))}

        result = compute_diff(existing, [chunk])

        assert result.to_upsert == [chunk]
        assert result.unchanged == []
        assert result.to_delete == []
        assert chunk.id == existing_id

    def test_duplicate_hash_prefers_matching_heading_identity(self):
        first, second = _mk("duplicate body"), _mk("duplicate body")
        first.metadata = replace(first.metadata, heading_hierarchy=("Beta",))
        second.metadata = replace(second.metadata, heading_hierarchy=("Alpha",))
        alpha_id, beta_id = uuid4(), uuid4()
        existing = {
            str(alpha_id): (first.content_hash, ("Alpha",)),
            str(beta_id): (first.content_hash, ("Beta",)),
        }

        result = compute_diff(existing, [first, second])

        assert result.to_upsert == []
        assert result.unchanged == [first, second]
        assert first.id == beta_id
        assert second.id == alpha_id

    def test_tag_change_is_metadata_only_not_unchanged(self):
        # The section blockquote is stripped from chunk text, so a tag edit
        # moves neither hash nor hierarchy. With stored tags supplied, the
        # chunk lands in ``metadata_only`` and keeps its id (#2124).
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("s",), tags=("bravo",))
        existing_id = uuid4()
        existing = {str(existing_id): (chunk.content_hash, ("s",), ("alpha",))}

        result = compute_diff(existing, [chunk])

        assert result.metadata_only == [chunk]
        assert result.unchanged == []
        assert result.to_upsert == []
        assert result.to_delete == []
        assert chunk.id == existing_id

    def test_unsupported_state_width_is_rejected(self):
        # A width nobody knows how to read must fail loudly. Truncating it would
        # silently drop a field a caller had just started supplying — which is
        # exactly how these bugs (#2124, #2140) arise in the first place.
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("s",))
        six = {str(uuid4()): (chunk.content_hash, ("s",), (), None, None, "future-field")}
        four = {str(uuid4()): (chunk.content_hash, ("s",), (), None)}

        for existing in (six, four):
            with pytest.raises(ValueError, match="unsupported chunk state width"):
                compute_diff(existing, [chunk])

    def test_unsupported_state_type_is_rejected(self):
        # A non-string scalar would match no hash and send every existing id to
        # ``to_delete`` — silent data loss wearing a diff's clothes.
        chunk = _mk("same body")
        for bad in (None, 7, b"hash-as-bytes", ["hash"]):
            with pytest.raises(ValueError, match="unsupported chunk state type"):
                compute_diff({str(uuid4()): bad}, [chunk])  # type: ignore[dict-item]

    def test_validity_change_is_metadata_only(self):
        # The validity window comes from file-level frontmatter and is stamped
        # on every chunk, so editing it moves no chunk's text (#2140).
        chunk = _mk("same body")
        chunk.metadata = replace(
            chunk.metadata,
            heading_hierarchy=("s",),
            tags=("alpha",),
            valid_from_unix=100,
            valid_to_unix=999,
        )
        existing_id = uuid4()
        existing = {str(existing_id): (chunk.content_hash, ("s",), ("alpha",), 100, 200)}

        result = compute_diff(existing, [chunk])

        assert result.metadata_only == [chunk]
        assert result.unchanged == []
        assert result.to_upsert == []
        assert chunk.id == existing_id

    def test_dropping_a_validity_bound_is_metadata_only(self):
        # Removing the frontmatter key means unbounded — a real change, not
        # "the caller said nothing".
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("s",), tags=("alpha",))
        existing = {str(uuid4()): (chunk.content_hash, ("s",), ("alpha",), None, 200)}

        result = compute_diff(existing, [chunk])

        assert result.metadata_only == [chunk]
        assert result.unchanged == []

    def test_matching_validity_stays_unchanged(self):
        chunk = _mk("same body")
        chunk.metadata = replace(
            chunk.metadata,
            heading_hierarchy=("s",),
            tags=("alpha",),
            valid_from_unix=100,
            valid_to_unix=200,
        )
        existing = {str(uuid4()): (chunk.content_hash, ("s",), ("alpha",), 100, 200)}

        result = compute_diff(existing, [chunk])

        assert result.unchanged == [chunk]
        assert result.metadata_only == []

    def test_three_element_state_says_nothing_about_validity(self):
        # A caller that supplies tags but not the window must not have its
        # silence read as "unbounded" — that would report drift on every file
        # carrying a validity window.
        chunk = _mk("same body")
        chunk.metadata = replace(
            chunk.metadata,
            heading_hierarchy=("s",),
            tags=("alpha",),
            valid_from_unix=100,
            valid_to_unix=200,
        )
        existing = {str(uuid4()): (chunk.content_hash, ("s",), ("alpha",))}

        result = compute_diff(existing, [chunk])

        assert result.unchanged == [chunk]
        assert result.metadata_only == []

    def test_matching_tags_stay_unchanged(self):
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("s",), tags=("alpha",))
        existing = {str(uuid4()): (chunk.content_hash, ("s",), ("alpha",))}

        result = compute_diff(existing, [chunk])

        assert result.unchanged == [chunk]
        assert result.metadata_only == []

    def test_tag_order_alone_is_not_drift(self):
        # ``tag_filter`` is set membership (ADR-0002); rewriting a row because
        # its stored order differs would churn on every re-index.
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("s",), tags=("a", "b"))
        existing = {str(uuid4()): (chunk.content_hash, ("s",), ("b", "a"))}

        result = compute_diff(existing, [chunk])

        assert result.unchanged == [chunk]
        assert result.metadata_only == []

    def test_states_without_tags_never_report_metadata_drift(self):
        # Two-element and bare-hash states mean "this caller did not say what
        # the row's tags are" — not "the row has no tags".
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("s",), tags=("alpha",))
        two_element = {str(uuid4()): (chunk.content_hash, ("s",))}
        bare_hash = {str(uuid4()): chunk.content_hash}

        for existing in (two_element, bare_hash):
            result = compute_diff(existing, [chunk])
            assert result.metadata_only == []
            assert result.unchanged == [chunk]

    def test_heading_change_wins_over_tag_change(self):
        # A moved heading needs a re-embed, so it stays in ``to_upsert`` even
        # when the tags moved too — the cheap path must not swallow it.
        chunk = _mk("same body")
        chunk.metadata = replace(chunk.metadata, heading_hierarchy=("new",), tags=("bravo",))
        existing = {str(uuid4()): (chunk.content_hash, ("old",), ("alpha",))}

        result = compute_diff(existing, [chunk])

        assert result.to_upsert == [chunk]
        assert result.metadata_only == []

    def test_reordered_tagged_duplicates_keep_their_own_ids(self):
        # Two byte-identical sections under the same heading, distinguishable
        # only by their tags. Reordering them must not swap ids — the ids carry
        # access counts, links and line positions.
        first, second = _mk("duplicate body"), _mk("duplicate body")
        first.metadata = replace(first.metadata, heading_hierarchy=("s",), tags=("beta",))
        second.metadata = replace(second.metadata, heading_hierarchy=("s",), tags=("alpha",))
        alpha_id, beta_id = uuid4(), uuid4()
        existing = {
            str(alpha_id): (first.content_hash, ("s",), ("alpha",)),
            str(beta_id): (first.content_hash, ("s",), ("beta",)),
        }

        result = compute_diff(existing, [first, second])

        assert result.metadata_only == []
        assert result.unchanged == [first, second]
        assert first.id == beta_id
        assert second.id == alpha_id

    def test_wildcard_state_is_spent_last_in_a_mixed_shape_map(self):
        # A state map that mixes shapes: one bare hash (wildcard — matches any
        # hierarchy), one two-element and one three-element entry. The wildcard
        # must be reserved last, or it takes an id an exact-hierarchy chunk
        # needed and strands that chunk in ``to_upsert`` for no reason.
        a, b, c = _mk("dup body"), _mk("dup body"), _mk("dup body")
        a.metadata = replace(a.metadata, heading_hierarchy=("B",), tags=("x",))
        b.metadata = replace(b.metadata, heading_hierarchy=("A",), tags=("x",))
        c.metadata = replace(c.metadata, heading_hierarchy=("B",), tags=("x",))
        wild_id, a_id, b_tagged_id = uuid4(), uuid4(), uuid4()
        existing = {
            str(wild_id): a.content_hash,
            str(a_id): (a.content_hash, ("A",)),
            str(b_tagged_id): (a.content_hash, ("B",), ("x",)),
        }

        result = compute_diff(existing, [a, b, c])

        assert result.to_upsert == []
        assert result.to_delete == []
        assert a.id == b_tagged_id, "the exact hierarchy+tags match is reserved first"
        assert b.id == a_id, "the exact-hierarchy id must not be spent on a wildcard match"
        assert c.id == wild_id

    def test_collapsing_duplicate_hash_deletes_unused_ids(self):
        # A file that held two byte-identical sections now holds one. The hash
        # survives, but the second id is not reused by anything and nothing
        # upserts over it, so it must be deleted rather than left searchable
        # under a heading the file no longer has (#2123).
        kept = _mk("duplicate body")
        kept.metadata = replace(kept.metadata, heading_hierarchy=("one",))
        one_id, two_id = uuid4(), uuid4()
        existing = {
            str(one_id): (kept.content_hash, ("one",)),
            str(two_id): (kept.content_hash, ("two",)),
        }

        result = compute_diff(existing, [kept])

        assert result.unchanged == [kept]
        assert result.to_upsert == []
        assert result.to_delete == [two_id]
        assert kept.id == one_id

    def test_collapsing_duplicate_hash_without_hierarchy_deletes_the_surplus(self):
        # Same collapse through the backward-compatible plain-hash input, where
        # hash equality alone decides reuse: exactly one id is reused and the
        # other is deleted.
        kept = _mk("duplicate body")
        id_a, id_b = uuid4(), uuid4()
        existing = {str(id_a): kept.content_hash, str(id_b): kept.content_hash}

        result = compute_diff(existing, [kept])

        assert result.unchanged == [kept]
        assert result.to_upsert == []
        assert len(result.to_delete) == 1
        assert {str(kept.id), str(result.to_delete[0])} == {str(id_a), str(id_b)}

    def test_renamed_duplicate_does_not_steal_later_exact_heading_id(self):
        renamed, unchanged = _mk("duplicate body"), _mk("duplicate body")
        renamed.metadata = replace(renamed.metadata, heading_hierarchy=("Gamma",))
        unchanged.metadata = replace(unchanged.metadata, heading_hierarchy=("Alpha",))
        alpha_id, beta_id = uuid4(), uuid4()
        existing = {
            str(alpha_id): (renamed.content_hash, ("Alpha",)),
            str(beta_id): (renamed.content_hash, ("Beta",)),
        }

        result = compute_diff(existing, [renamed, unchanged])

        assert result.to_upsert == [renamed]
        assert result.unchanged == [unchanged]
        assert renamed.id == beta_id
        assert unchanged.id == alpha_id
