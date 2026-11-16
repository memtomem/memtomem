"""``get_chunks_with_topology``: a chunk's current row and where it sits, in one read.

The duplicate brief renders a pair's members from this read, so each case here
compares every topology field of every row it seeds: a field left unasserted is
a field the brief could print wrongly.

Rows are seeded with SQL rather than indexed, so the line spans, edges and
links are exactly the ones a case names. The one test that needs a real indexer
(the span the store holds for a structured chunk) says so and uses the component
stack.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from memtomem.config import StorageConfig
from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY, ChunkTopology
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_backend import SqliteBackend

pytestmark = pytest.mark.asyncio

_HOLD_KINDS = ("scan_missing", REDACTION_BLOCKED_HOLD, "pending")
_STAMP = "2026-01-01T00:00:00"

# What a chunk with nothing around it reports; cases override the fields they move.
_ALONE = ChunkTopology(
    only_in_source=True,
    at_source_start=True,
    span_shared=False,
    consolidated_into=0,
    consolidates=0,
    other_relations=0,
    links_out=(),
    links_in=(),
)


def _topology(**moved) -> ChunkTopology:
    fields = {name: getattr(_ALONE, name) for name in ChunkTopology.__slots__}
    return ChunkTopology(**{**fields, **moved})


@pytest.fixture
async def backend(tmp_path):
    be = SqliteBackend(
        config=StorageConfig(sqlite_path=tmp_path / "topology.db"),
        dimension=0,
        embedding_provider="none",
        embedding_model="",
    )
    await be.initialize()
    yield be
    await be.close()


def _seed(
    backend: SqliteBackend,
    source: str,
    start: int,
    end: int,
    *,
    origin: str | None = None,
) -> UUID:
    chunk_id = uuid4()
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, start_line, end_line, "
        "origin, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            str(chunk_id),
            f"body {chunk_id}",
            str(chunk_id),
            source,
            start,
            end,
            origin,
            _STAMP,
            _STAMP,
        ),
    )
    db.commit()
    return chunk_id


def _relate(backend: SqliteBackend, source: UUID, target: UUID, kind: str) -> None:
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunk_relations (source_id, target_id, relation_type, created_at) "
        "VALUES (?, ?, ?, ?)",
        (str(source), str(target), kind, _STAMP),
    )
    db.commit()


def _link(backend: SqliteBackend, source: UUID | None, target: UUID, kind: str) -> None:
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunk_links (source_id, target_id, link_type, namespace_target, created_at) "
        "VALUES (?, ?, ?, 'default', ?)",
        (None if source is None else str(source), str(target), kind, _STAMP),
    )
    db.commit()


def _hide(backend: SqliteBackend, source: str, kind: str) -> None:
    db = backend._get_db()
    if kind == "pending":
        db.execute("INSERT INTO pending_source_checks VALUES (?, ?)", (source, _STAMP))
    else:
        db.execute("INSERT INTO held_sources VALUES (?, ?, ?, ?)", (source, _STAMP, _STAMP, kind))
    db.commit()


async def _read(backend: SqliteBackend, *ids: UUID) -> dict[UUID, ChunkTopology]:
    found = await backend.get_chunks_with_topology(list(ids))
    return {chunk_id: topology for chunk_id, (_, topology) in found.items()}


class TestPositionInSource:
    async def test_the_only_chunk_of_a_source(self, backend):
        alone = _seed(backend, "/m/a.md", 1, 10)

        assert await _read(backend, alone) == {alone: _ALONE}

    async def test_adjacent_spans_do_not_overlap(self, backend):
        first = _seed(backend, "/m/a.md", 1, 10)
        second = _seed(backend, "/m/a.md", 11, 20)

        assert await _read(backend, first, second) == {
            first: _topology(only_in_source=False),
            second: _topology(only_in_source=False, at_source_start=False),
        }

    async def test_spans_meeting_on_one_line_overlap(self, backend):
        first = _seed(backend, "/m/a.md", 1, 10)
        second = _seed(backend, "/m/a.md", 10, 20)

        assert await _read(backend, first, second) == {
            first: _topology(only_in_source=False, span_shared=True),
            second: _topology(only_in_source=False, at_source_start=False, span_shared=True),
        }

    async def test_a_span_inside_another_overlaps_it(self, backend):
        outer = _seed(backend, "/m/a.md", 1, 30)
        inner = _seed(backend, "/m/a.md", 10, 12)

        assert await _read(backend, outer, inner) == {
            outer: _topology(only_in_source=False, span_shared=True),
            inner: _topology(only_in_source=False, at_source_start=False, span_shared=True),
        }

    async def test_two_chunks_starting_on_one_line_are_both_at_the_start(self, backend):
        first = _seed(backend, "/m/a.md", 1, 1)
        second = _seed(backend, "/m/a.md", 1, 1)

        both = _topology(only_in_source=False, span_shared=True)
        assert await _read(backend, first, second) == {first: both, second: both}

    async def test_the_same_lines_in_another_source_do_not_count(self, backend):
        here = _seed(backend, "/m/a.md", 5, 9)
        _seed(backend, "/m/b.md", 1, 9)

        assert await _read(backend, here) == {here: _ALONE}


class TestUnrecordedSpan:
    async def test_a_zero_span_has_no_position(self, backend):
        first = _seed(backend, "/m/data.json", 0, 0)
        second = _seed(backend, "/m/data.json", 0, 0)

        unplaced = _topology(only_in_source=False, at_source_start=None, span_shared=None)
        assert await _read(backend, first, second) == {first: unplaced, second: unplaced}

    @pytest.mark.parametrize("span", [(1, 0), (7, 3)])
    async def test_an_end_before_its_start_has_no_position(self, backend, span):
        broken = _seed(backend, "/m/a.md", *span)

        assert await _read(backend, broken) == {
            broken: _topology(at_source_start=None, span_shared=None)
        }

    async def test_an_unplaced_sibling_takes_no_part(self, backend):
        placed = _seed(backend, "/m/a.md", 5, 9)
        _seed(backend, "/m/a.md", 0, 0)
        # Its start is below 5 and its range would cover 5-9, were it counted.
        _seed(backend, "/m/a.md", 0, 40)

        assert await _read(backend, placed) == {placed: _topology(only_in_source=False)}

    async def test_the_structured_chunker_stores_zero_spans(self, bm25_only_components):
        # Real indexer: pins that the shape the two tests above seed by hand is
        # one the store actually holds.
        comp, mem_dir = bm25_only_components
        path = mem_dir / "data.json"
        path.write_text(json.dumps({"alpha": "first value", "beta": "second value"}), "utf-8")
        assert not (await comp.index_engine.index_file(path)).errors
        rows = await comp.storage.list_chunks_by_source(path.resolve(), limit=None)
        spans = [(c.metadata.start_line, c.metadata.end_line) for c in rows]
        # The chunker emits ``0, 0`` and the store holds ``1, 0``: either way
        # not a positive range.
        assert spans and all(not 1 <= start <= end for start, end in spans), spans

        found = await comp.storage.get_chunks_with_topology([c.id for c in rows])

        assert len(found) == len(rows)
        assert {t.at_source_start for _, t in found.values()} == {None}
        assert {t.span_shared for _, t in found.values()} == {None}


class TestRelations:
    async def test_each_direction_and_type_lands_in_its_own_count(self, backend):
        # Unequal counts throughout, so a reader that swaps the two ends, or
        # counts one end twice, cannot land on the expected numbers.
        original = _seed(backend, "/m/original.md", 1, 5)
        summary = _seed(backend, "/m/summary.md", 1, 5)
        hub = _seed(backend, "/m/hub.md", 1, 5)
        others = [_seed(backend, f"/m/n{i}.md", 1, 5) for i in range(8)]
        for target in others[:2]:
            _relate(backend, original, target, "consolidated_into")
        for source in others[2:5]:
            _relate(backend, source, summary, "consolidated_into")
        _relate(backend, hub, others[5], "related")
        _relate(backend, others[6], hub, "related")
        _relate(backend, others[7], hub, "supersedes")

        assert await _read(backend, original, summary, hub) == {
            original: _topology(consolidated_into=2),
            summary: _topology(consolidates=3),
            hub: _topology(other_relations=3),
        }

    async def test_a_self_edge_is_one_relation(self, backend):
        loop = _seed(backend, "/m/a.md", 1, 5)
        _relate(backend, loop, loop, "related")

        assert await _read(backend, loop) == {loop: _topology(other_relations=1)}


class TestLinks:
    async def test_outgoing_links_are_counted_by_type(self, backend):
        source = _seed(backend, "/m/source.md", 1, 5)
        targets = [_seed(backend, f"/m/t{i}.md", 1, 5) for i in range(3)]
        _link(backend, source, targets[0], "shared")
        _link(backend, source, targets[1], "summarizes")
        _link(backend, source, targets[2], "summarizes")

        found = await _read(backend, source, *targets)

        assert found[source] == _topology(links_out=(("shared", 1), ("summarizes", 2)))
        assert found[targets[0]] == _topology(links_in=("shared",))
        assert found[targets[1]] == _topology(links_in=("summarizes",))

    async def test_a_session_summary_is_a_link_source_without_sharing(self, backend):
        summary = _seed(backend, "/m/summary.md", 1, 5)
        covered = _seed(backend, "/m/covered.md", 1, 5)
        _link(backend, summary, covered, "summarizes")

        assert await _read(backend, summary) == {summary: _topology(links_out=(("summarizes", 1),))}

    async def test_a_target_can_carry_one_link_of_each_type(self, backend):
        target = _seed(backend, "/m/target.md", 1, 5)
        _link(backend, _seed(backend, "/m/s1.md", 1, 5), target, "summarizes")
        _link(backend, _seed(backend, "/m/s2.md", 1, 5), target, "shared")

        assert await _read(backend, target) == {
            target: _topology(links_in=("shared", "summarizes"))
        }

    async def test_a_link_whose_source_was_deleted_still_counts(self, backend):
        target = _seed(backend, "/m/target.md", 1, 5)
        _link(backend, None, target, "shared")

        assert await _read(backend, target) == {target: _topology(links_in=("shared",))}


class TestHiddenSources:
    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_a_chunk_on_a_hidden_path_is_absent(self, backend, kind):
        hidden = _seed(backend, "/m/hidden.md", 1, 5)
        listed = _seed(backend, "/m/listed.md", 1, 5)
        _hide(backend, "/m/hidden.md", kind)

        assert await _read(backend, hidden, listed) == {listed: _ALONE}

    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_an_edge_to_a_hidden_chunk_is_not_counted(self, backend, kind):
        listed = _seed(backend, "/m/listed.md", 1, 5)
        hidden = _seed(backend, "/m/hidden.md", 1, 5)
        kept = _seed(backend, "/m/kept.md", 1, 5)
        _relate(backend, listed, hidden, "consolidated_into")
        _relate(backend, hidden, listed, "consolidated_into")
        _relate(backend, listed, kept, "related")
        before = await _read(backend, listed)
        assert before == {listed: _topology(consolidated_into=1, consolidates=1, other_relations=1)}

        _hide(backend, "/m/hidden.md", kind)

        assert await _read(backend, listed) == {listed: _topology(other_relations=1)}

    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_another_relation_with_a_hidden_chunk_is_not_counted(self, backend, kind):
        listed = _seed(backend, "/m/listed.md", 1, 5)
        hidden_a = _seed(backend, "/m/hidden.md", 1, 5)
        hidden_b = _seed(backend, "/m/hidden.md", 6, 9)
        _relate(backend, listed, hidden_a, "related")
        _relate(backend, hidden_b, listed, "related")
        assert await _read(backend, listed) == {listed: _topology(other_relations=2)}

        _hide(backend, "/m/hidden.md", kind)

        assert await _read(backend, listed) == {listed: _ALONE}

    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_a_link_with_a_hidden_chunk_is_not_counted(self, backend, kind):
        listed = _seed(backend, "/m/listed.md", 1, 5)
        hidden_a = _seed(backend, "/m/hidden.md", 1, 5)
        hidden_b = _seed(backend, "/m/hidden.md", 6, 9)
        _link(backend, listed, hidden_a, "shared")
        _link(backend, hidden_b, listed, "summarizes")
        assert await _read(backend, listed) == {
            listed: _topology(links_out=(("shared", 1),), links_in=("summarizes",))
        }

        _hide(backend, "/m/hidden.md", kind)

        assert await _read(backend, listed) == {listed: _ALONE}

    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_a_path_with_a_policy_summary_follows_its_parent(self, backend, kind):
        """An ordinary chunk on a summary's path is hidden with it, as a scan hides it.

        The scan lists by path: a path holding a policy summary is hidden
        whole when the summary's parent is. A row-level rule would keep the
        ordinary chunk and let the hidden summary beside it shape its
        position.
        """
        # Spelled by ``Path`` so the listing below matches the seeded key on
        # every platform, rather than finding no rows for another reason.
        parent = str(Path("/m/parent.md"))
        virtual = parent + CONSOLIDATED_SUFFIX
        _seed(backend, parent, 1, 5)
        ordinary = _seed(backend, virtual, 6, 9)
        summary = _seed(backend, virtual, 1, 5, origin=ORIGIN_CONSOLIDATION_POLICY)

        async def scan_lists() -> set[UUID]:
            rows = await backend.list_chunks_by_source(
                Path(virtual), limit=None, exclude_hidden=True, as_stored=True
            )
            return {row.id for row in rows}

        assert set(await _read(backend, ordinary, summary)) == {ordinary, summary}
        assert await scan_lists() == {ordinary, summary}

        _hide(backend, parent, kind)

        assert await _read(backend, ordinary, summary) == {}
        assert await scan_lists() == set()


class TestTheRowItReturns:
    async def test_the_chunk_is_the_one_the_batch_reader_returns(self, backend):
        chunk_id = _seed(backend, "/m/a.md", 3, 9)
        db = backend._get_db()
        db.execute(
            "UPDATE chunks SET heading_hierarchy=?, tags=?, namespace='work', scope='project_local', "
            "project_root='/p', valid_from_unix=10, valid_to_unix=20, source_read_only=1, "
            "redaction_count=2, access_count=4 WHERE id=?",
            (json.dumps(["Guide", "Setup"]), json.dumps(["a", "b"]), str(chunk_id)),
        )
        db.commit()

        found = await backend.get_chunks_with_topology([chunk_id])

        assert found[chunk_id][0] == (await backend.get_chunks_batch([chunk_id]))[chunk_id]
        assert found[chunk_id][0].metadata.heading_hierarchy == ("Guide", "Setup")
        assert found[chunk_id][0].metadata.start_line == 3

    async def test_a_missing_id_is_absent_and_no_ids_is_empty(self, backend):
        present = _seed(backend, "/m/a.md", 1, 5)

        assert set(await backend.get_chunks_with_topology([present, uuid4()])) == {present}
        assert await backend.get_chunks_with_topology([]) == {}

    async def test_a_repeated_id_is_answered_once(self, backend):
        present = _seed(backend, "/m/a.md", 1, 5)

        assert await _read(backend, present, present) == {present: _ALONE}

    async def test_more_ids_than_the_host_parameter_limit(self, backend):
        ids = [_seed(backend, f"/m/f{i}.md", 1, 5) for i in range(1200)]
        # This runtime's limit is far above 1,200, which would let one
        # parameter per id pass; lower it on every connection the read can use.
        for conn in [backend._get_db(), *backend._read_pool]:
            conn.setlimit(sqlite3.SQLITE_LIMIT_VARIABLE_NUMBER, 999)

        assert set(await backend.get_chunks_with_topology(ids)) == set(ids)
