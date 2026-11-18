"""Tests for entity extraction logic and storage mixin."""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.models import Chunk, ChunkMetadata
from memtomem.tools.entity_extraction import extract_entities


def _make_chunk(content="test", tags=(), namespace="default", source="test.md"):
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=Path(f"/tmp/{source}"),
            tags=tuple(tags),
            namespace=namespace,
        ),
        content_hash=f"hash-{uuid4().hex[:8]}",
        embedding=[0.1] * 1024,
    )


# ── Extraction Logic ─────────────────────────────────────────────────


class TestExtractPersons:
    def test_by_author(self):
        entities = extract_entities("Written by John Smith on Monday", ["person"])
        names = [e.entity_value for e in entities]
        assert "John Smith" in names

    def test_from_field(self):
        entities = extract_entities("from: Alice Johnson\ncc: Bob Williams", ["person"])
        names = [e.entity_value for e in entities]
        assert "Alice Johnson" in names
        assert "Bob Williams" in names

    def test_mentions(self):
        entities = extract_entities("Assigned to @alice and @bob_dev", ["person"])
        values = [e.entity_value for e in entities]
        assert "@alice" in values
        assert "@bob_dev" in values

    def test_no_short_names(self):
        entities = extract_entities("by Jo", ["person"])
        assert len(entities) == 0


class TestExtractDates:
    def test_iso_date(self):
        entities = extract_entities("Meeting on 2025-03-15", ["date"])
        values = [e.entity_value for e in entities]
        assert "2025-03-15" in values

    def test_natural_date(self):
        entities = extract_entities("Due by January 15th, 2025", ["date"])
        assert len(entities) >= 1
        assert any("January" in e.entity_value for e in entities)

    def test_multiple_dates(self):
        entities = extract_entities("From 2025-01-01 to 2025-12-31", ["date"])
        assert len(entities) == 2


class TestExtractDecisions:
    def test_decision_prefix(self):
        entities = extract_entities("Decision: Use PostgreSQL for the backend", ["decision"])
        assert len(entities) == 1
        assert "PostgreSQL" in entities[0].entity_value

    def test_we_will(self):
        entities = extract_entities("We will migrate to AWS by Q3", ["decision"])
        assert len(entities) == 1

    def test_agreed(self):
        entities = extract_entities("Agreed: No more deployments on Friday", ["decision"])
        assert len(entities) == 1


class TestExtractActionItems:
    def test_todo(self):
        entities = extract_entities("TODO: Fix the login page", ["action_item"])
        assert len(entities) == 1
        assert "Fix the login page" in entities[0].entity_value

    def test_checkbox(self):
        entities = extract_entities("- [ ] Review PR #42\n- [x] Deploy staging", ["action_item"])
        assert len(entities) == 1  # only unchecked
        assert "Review PR #42" in entities[0].entity_value

    def test_fixme(self):
        entities = extract_entities("FIXME: Memory leak in worker pool", ["action_item"])
        assert len(entities) == 1


class TestExtractTechnologies:
    def test_known_tech(self):
        entities = extract_entities("We use Docker and Kubernetes for deployment", ["technology"])
        values = {e.entity_value.lower() for e in entities}
        assert "docker" in values
        assert "kubernetes" in values

    def test_pascal_case(self):
        entities = extract_entities("Integrated with SuperWidget library", ["technology"])
        values = [e.entity_value for e in entities]
        assert "SuperWidget" in values


def _values(text: str, entity_type: str) -> list[str]:
    return [e.entity_value for e in extract_entities(text, [entity_type])]


class TestCJKNeighbour:
    """Hangul, kana and Han next to an entity end its word; nothing else does.

    Korean and Japanese write a particle against the preceding word, so Unicode
    ``\\w`` / ``\\b`` made ``@alice에게`` one handle and lost ``2026-11-10에``.
    Rows cover each side of each pattern that has a boundary there.
    """

    @pytest.mark.parametrize(
        ("text", "entity_type", "expected"),
        [
            ("@alice에게 물어봄", "person", "@alice"),
            ("@aliceさんに聞く", "person", "@alice"),
            ("2026-11-10에 배포", "date", "2026-11-10"),
            ("배포일2026-11-10", "date", "2026-11-10"),
            ("2026-11-10にリリース", "date", "2026-11-10"),
            ("August 17에 출시", "date", "August 17"),
            ("May 5th에 회의", "date", "May 5th"),
            ("출시일August 17", "date", "August 17"),
            # main backtracked the optional year and kept "August 17".
            ("August 17, 2026에 출시", "date", "August 17, 2026"),
            ("OpenSearch를 쓰자", "technology", "OpenSearch"),
            ("用OpenSearch搜索", "technology", "OpenSearch"),
            ("OpenSearchを使う", "technology", "OpenSearch"),
        ],
    )
    def test_cjk_neighbour_ends_the_entity(self, text, entity_type, expected):
        assert _values(text, entity_type) == [expected]

    @pytest.mark.parametrize(
        ("text", "entity_type", "expected"),
        [
            ("@alice_bob", "person", ["@alice_bob"]),
            ("@rené", "person", ["@rené"]),  # an accented letter stays in the handle
            ("@ab에게", "person", []),  # the handle is "ab", under the 3-char minimum
            ("x2026-11-10", "date", []),
            ("2026-11-10x", "date", []),
            ("é2026-11-10", "date", []),
            ("2026-11-10١", "date", []),  # a non-ASCII digit is still a word character
            ("August 175", "date", []),
            ("éAugust 17", "date", []),
            ("August 17thé", "date", []),
            ("August 17١", "date", []),
            ("OpenSearché", "technology", []),
            ("éOpenSearch", "technology", []),
        ],
    )
    def test_other_word_characters_still_join_the_word(self, text, entity_type, expected):
        assert _values(text, entity_type) == expected

    @pytest.mark.parametrize(
        ("chunk_text", "query", "entity_type"),
        [
            ("Ask @alice about the rollout.", "@alice에게 물어봄", "person"),
            ("Released on 2026-11-10.", "2026-11-10에 배포한 것", "date"),
            ("We moved search to OpenSearch.", "OpenSearch를 쓰는 이유", "technology"),
        ],
    )
    def test_chunk_and_query_share_the_key(self, chunk_text, query, entity_type):
        # The chunk side writes ``chunk_entities`` from this extractor; the query
        # side builds its keys from it too. A particle on one side only must not
        # change the key.
        from memtomem.search.entity_boost import extract_query_entities
        from memtomem.tools.entity_extraction import fold_entity_value

        chunk_keys = {(entity_type, fold_entity_value(v)) for v in _values(chunk_text, entity_type)}
        query_keys = set(extract_query_entities(query, [entity_type]))
        assert query_keys and query_keys == chunk_keys


class TestExtractConcepts:
    def test_quoted_terms(self):
        entities = extract_entities('The "event sourcing" pattern is key', ["concept"])
        assert len(entities) == 1
        assert entities[0].entity_value == "event sourcing"


class TestExtractAll:
    def test_mixed_content(self):
        text = """
        Meeting notes 2025-03-15
        by John Smith

        Decision: Migrate to PostgreSQL
        TODO: Update connection strings
        We use Docker for deployment.
        The "microservices" approach was discussed.
        """
        entities = extract_entities(text)
        types = {e.entity_type for e in entities}
        assert "person" in types
        assert "date" in types
        assert "decision" in types
        assert "action_item" in types
        assert "technology" in types

    def test_deduplication(self):
        entities = extract_entities("Docker and docker and DOCKER", ["technology"])
        docker_entities = [e for e in entities if e.entity_value.lower() == "docker"]
        assert len(docker_entities) == 1

    def test_empty_text(self):
        assert extract_entities("") == []

    def test_type_filter(self):
        text = "by John Smith on 2025-03-15"
        entities = extract_entities(text, ["date"])
        types = {e.entity_type for e in entities}
        assert types == {"date"}


# ── Entity Storage Mixin ─────────────────────────────────────────────


class TestEntityMixin:
    @pytest.mark.asyncio
    async def test_upsert_and_search(self, storage):
        chunk = _make_chunk("Test with Docker and Python")
        await storage.upsert_chunks([chunk])

        entities = [
            {
                "entity_type": "technology",
                "entity_value": "Docker",
                "confidence": 0.9,
                "position": 10,
            },
            {
                "entity_type": "technology",
                "entity_value": "Python",
                "confidence": 0.9,
                "position": 22,
            },
        ]
        count = await storage.upsert_entities(str(chunk.id), entities)
        assert count == 2

        results = await storage.search_entities(entity_type="technology")
        assert len(results) == 2
        assert results[0]["entity_value"] in ("Docker", "Python")

    @pytest.mark.asyncio
    async def test_search_by_value(self, storage):
        chunk = _make_chunk("Assigned to John Smith")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "person", "entity_value": "John Smith", "confidence": 0.8},
            ],
        )

        results = await storage.search_entities(value="John")
        assert len(results) == 1
        assert results[0]["entity_value"] == "John Smith"

    @pytest.mark.asyncio
    async def test_get_entities_for_chunk(self, storage):
        chunk = _make_chunk("TODO: Fix bug")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "action_item", "entity_value": "Fix bug", "confidence": 0.9},
            ],
        )

        entities = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(entities) == 1
        assert entities[0]["entity_type"] == "action_item"

    @pytest.mark.asyncio
    async def test_overwrite(self, storage):
        chunk = _make_chunk("Docker")
        await storage.upsert_chunks([chunk])

        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "technology", "entity_value": "Docker", "confidence": 0.9},
            ],
        )
        # Overwrite with different entities
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "technology", "entity_value": "Kubernetes", "confidence": 0.8},
            ],
        )

        entities = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(entities) == 1
        assert entities[0]["entity_value"] == "Kubernetes"

    @pytest.mark.asyncio
    async def test_delete_entities(self, storage):
        chunk = _make_chunk("Test")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "concept", "entity_value": "test", "confidence": 0.7},
            ],
        )

        deleted = await storage.delete_entities_for_chunk(str(chunk.id))
        assert deleted == 1

        entities = await storage.get_entities_for_chunk(str(chunk.id))
        assert len(entities) == 0

    @pytest.mark.asyncio
    async def test_entity_type_counts(self, storage):
        chunk = _make_chunk("Docker by John")
        await storage.upsert_chunks([chunk])
        await storage.upsert_entities(
            str(chunk.id),
            [
                {"entity_type": "technology", "entity_value": "Docker", "confidence": 0.9},
                {"entity_type": "person", "entity_value": "John", "confidence": 0.8},
            ],
        )

        counts = await storage.get_entity_type_counts()
        assert counts["technology"] == 1
        assert counts["person"] == 1
