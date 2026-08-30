"""``get_context_windows`` against a real SQLite backend (#2237).

Two things only a real backend can pin. First, that the window and its counts
do not depend on the file's length: expansion used to list the file with a
10,000-row cap, so an anchor past it came back with no context at all and one
inside it reported a total that stopped at the cap.

Second, that ``neighbor_visibility_sql`` and ``neighbor_visible`` agree. They
are twins by hand, and the counts the SQL produces are published as the
anchor's ordinal and the file's visible total, so a disagreement either
misreports a position or leaks how many chunks the caller may not see. The
parity matrix below runs both over the same rows under every filter shape.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from memtomem.config import StorageConfig
from memtomem.models import Chunk, ChunkMetadata, NamespaceFilter, ScopeFilter
from memtomem.search.visibility import neighbor_visible
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_helpers import norm_path

pytestmark = pytest.mark.asyncio

SOURCE = Path("/tmp/ctx-window.md")
SYSTEM_PREFIXES = ("archive:", "agent-runtime:")
NOW = 1_700_000_000


def _chunk(
    i: int,
    *,
    source: Path = SOURCE,
    start_line: int | None = None,
    namespace: str = "default",
    scope: str = "user",
    project_root: Path | None = None,
    valid_from_unix: int | None = None,
    valid_to_unix: int | None = None,
) -> Chunk:
    return Chunk(
        content=f"chunk {i}",
        metadata=ChunkMetadata(
            source_file=source,
            start_line=i * 10 if start_line is None else start_line,
            end_line=(i * 10 if start_line is None else start_line) + 9,
            namespace=namespace,
            scope=scope,
            project_root=project_root,
            valid_from_unix=valid_from_unix,
            valid_to_unix=valid_to_unix,
        ),
        id=uuid4(),
        content_hash=f"hash-{uuid4().hex[:12]}",
        embedding=[],
    )


@pytest.fixture
async def backend(tmp_path):
    storage = SqliteBackend(
        StorageConfig(sqlite_path=tmp_path / "ctx.db"),
        dimension=8,
        embedding_provider="onnx",
        embedding_model="test-model",
    )
    await storage.initialize()
    try:
        yield storage
    finally:
        await storage.close()


def _spec(**overrides):
    """The default visibility axes, as both surfaces pass them."""
    spec = {
        "ns_filter": NamespaceFilter.parse(None, system_prefixes=SYSTEM_PREFIXES),
        "system_prefixes": SYSTEM_PREFIXES,
        "scope_filter": None,
        "project_context_root": None,
        "as_of_unix": NOW,
    }
    spec.update(overrides)
    return spec


class TestWindowRows:
    async def test_middle_anchor_gets_both_sides(self, backend):
        chunks = [_chunk(i) for i in range(7)]
        await backend.upsert_chunks(chunks)

        rows = (await backend.get_context_windows(SOURCE, [chunks[3].id], 2, **_spec()))[
            chunks[3].id
        ]

        assert [c.content for c in rows.before] == ["chunk 1", "chunk 2"]
        assert [c.content for c in rows.after] == ["chunk 4", "chunk 5"]
        assert rows.visible_before == 3
        # Anchor-free by contract: 7 rows, minus the anchor.
        assert rows.visible_total_excluding_anchor == 6

    async def test_first_and_last_anchors_run_out_of_neighbours(self, backend):
        chunks = [_chunk(i) for i in range(5)]
        await backend.upsert_chunks(chunks)

        windows = await backend.get_context_windows(
            SOURCE, [chunks[0].id, chunks[4].id], 2, **_spec()
        )

        assert windows[chunks[0].id].before == []
        assert len(windows[chunks[0].id].after) == 2
        assert windows[chunks[0].id].visible_before == 0
        assert len(windows[chunks[4].id].before) == 2
        assert windows[chunks[4].id].after == []
        assert windows[chunks[4].id].visible_before == 4

    async def test_zero_window_still_counts(self, backend):
        """``window=0`` is a legal request: the position without the context."""
        chunks = [_chunk(i) for i in range(4)]
        await backend.upsert_chunks(chunks)

        rows = (await backend.get_context_windows(SOURCE, [chunks[2].id], 0, **_spec()))[
            chunks[2].id
        ]

        assert rows.before == [] and rows.after == []
        assert rows.visible_before == 2
        assert rows.visible_total_excluding_anchor == 3

    async def test_unknown_and_foreign_anchors_are_absent(self, backend):
        """The one remaining "no context" answer: the id is not a row here."""
        chunks = [_chunk(i) for i in range(3)]
        other = _chunk(0, source=Path("/tmp/other.md"))
        await backend.upsert_chunks([*chunks, other])

        stranger = uuid4()
        windows = await backend.get_context_windows(
            SOURCE, [stranger, other.id, chunks[1].id], 1, **_spec()
        )

        assert windows[stranger] is None
        assert windows[other.id] is None
        assert windows[chunks[1].id] is not None

    async def test_shared_start_line_orders_by_rowid(self, backend):
        """Chunks of a virtual source share a ``start_line``.

        The ordinal has to agree with ``list_chunks_by_source``, which breaks
        that tie on ``rowid`` — a position counted in a different order would
        describe a listing the caller never sees.
        """
        chunks = [_chunk(i, start_line=0) for i in range(5)]
        await backend.upsert_chunks(chunks)

        listed = await backend.list_chunks_by_source(SOURCE, limit=None)
        anchor = listed[3]
        rows = (await backend.get_context_windows(SOURCE, [anchor.id], 1, **_spec()))[anchor.id]

        assert rows.visible_before == 3
        assert [c.id for c in rows.before] == [listed[2].id]
        assert [c.id for c in rows.after] == [listed[4].id]

    async def test_neighbours_come_back_unscreened(self, backend):
        """Storage returns physical neighbours; the caller screens them.

        The split is deliberate — the leak-sensitive decision stays on the one
        Python predicate — so a hidden neighbour must still arrive here.
        """
        chunks = [_chunk(0), _chunk(1, namespace="archive:old"), _chunk(2)]
        await backend.upsert_chunks(chunks)

        rows = (await backend.get_context_windows(SOURCE, [chunks[2].id], 2, **_spec()))[
            chunks[2].id
        ]

        assert [c.id for c in rows.before] == [chunks[0].id, chunks[1].id]
        # ...but it is not counted: one visible row precedes the anchor.
        assert rows.visible_before == 1


class TestConcurrentReindex:
    async def test_an_anchor_that_moves_mid_read_is_not_its_own_neighbour(
        self, backend, monkeypatch
    ):
        """A re-index between the counts and the seeks must not duplicate the match.

        ``upsert_chunks`` updates ``start_line`` in place and keeps the
        ``rowid``, so an anchor can move past the key the earlier statement
        read it at — and a seek that excluded it only by position would then
        return the anchor itself as a neighbour.
        """
        chunks = [_chunk(i) for i in range(4)]
        await backend.upsert_chunks(chunks)
        anchor = chunks[1]

        original = backend._neighbor_rows
        moved = False

        def _move_then_read(*args, **kwargs):
            nonlocal moved
            if not moved:
                moved = True
                # The anchor is re-indexed a little further down the file, id
                # and rowid intact — exactly what an in-place update does. It
                # has to land *inside* the window it is about to be read for,
                # or the ``LIMIT`` hides the duplicate instead of the fix.
                db = backend._get_db()
                db.execute(
                    "UPDATE chunks SET start_line=?, end_line=? WHERE id=?",
                    (25, 34, str(anchor.id)),
                )
                db.commit()
            return original(*args, **kwargs)

        monkeypatch.setattr(backend, "_neighbor_rows", _move_then_read)

        rows = (await backend.get_context_windows(SOURCE, [anchor.id], 2, **_spec()))[anchor.id]

        assert anchor.id not in {c.id for c in rows.before}
        assert anchor.id not in {c.id for c in rows.after}


class TestPastTheOldCap:
    """#2237: the failure the cap produced, at a size that would trip it."""

    @pytest.fixture
    async def big_file(self, backend):
        # 10,050 rows: past the old 10,000-row listing cap. Inserted straight
        # into ``chunks`` because this method reads no other table, and
        # upserting them through the embedding path would spend the test's
        # budget on vectors it never looks at.
        total = 10_050
        db = backend._get_db()
        db.executemany(
            "INSERT INTO chunks (id, content, content_hash, source_file, start_line, end_line, "
            "created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,'2026-01-01T00:00:00+00:00','2026-01-01T00:00:00+00:00')",
            [
                (str(uuid4()), f"chunk {i}", f"h{i}", norm_path(SOURCE), i * 10, i * 10 + 9)
                for i in range(total)
            ],
        )
        db.commit()
        listed = await backend.list_chunks_by_source(SOURCE, limit=None)
        assert len(listed) == total
        return listed

    async def test_anchor_past_the_cap_still_expands(self, backend, big_file):
        anchor = big_file[10_020]

        rows = (await backend.get_context_windows(SOURCE, [anchor.id], 2, **_spec()))[anchor.id]

        assert rows is not None, "an anchor past the old cap read as 'not in the listing'"
        assert [c.content for c in rows.before] == ["chunk 10018", "chunk 10019"]
        assert [c.content for c in rows.after] == ["chunk 10021", "chunk 10022"]
        assert rows.visible_before == 10_020
        assert rows.visible_total_excluding_anchor == 10_049

    async def test_anchor_inside_the_cap_reports_the_whole_file(self, backend, big_file):
        """The other half: the total used to stop at the cap, not the file."""
        anchor = big_file[5]

        rows = (await backend.get_context_windows(SOURCE, [anchor.id], 1, **_spec()))[anchor.id]

        assert rows.visible_before == 5
        assert rows.visible_total_excluding_anchor == 10_049


class TestVisibilityParity:
    """``neighbor_visibility_sql`` vs ``neighbor_visible`` over one matrix."""

    @staticmethod
    def _matrix() -> list[Chunk]:
        here = Path("/tmp/proj-here")
        there = Path("/tmp/proj-there")
        specs = [
            {},  # plain visible row
            {"namespace": "archive:2024"},
            {"namespace": "agent-runtime:planner"},
            {"namespace": "ARCHIVE:shouty"},  # LIKE folds ASCII case
            {"namespace": "archived-but-not-a-prefix"},
            {"scope": "project_shared", "project_root": here},
            {"scope": "project_local", "project_root": here},
            {"scope": "project_shared", "project_root": there},
            {"scope": "project_local", "project_root": there},
            {"valid_to_unix": NOW - 1},  # expired
            {"valid_from_unix": NOW + 1},  # not yet valid
            {"valid_from_unix": NOW, "valid_to_unix": NOW},  # inclusive both ends
            {"valid_from_unix": NOW - 10},  # open-ended upper
            {"namespace": "archive:x", "scope": "project_shared", "project_root": there},
        ]
        return [_chunk(i, **spec) for i, spec in enumerate(specs)]

    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({}, id="defaults"),
            pytest.param({"as_of_unix": None}, id="no-temporal-bound"),
            pytest.param({"ns_filter": None}, id="no-namespace-filter"),
            pytest.param(
                {"ns_filter": NamespaceFilter.parse("archive:2024")},
                id="explicit-namespace-widens",
            ),
            pytest.param(
                {"ns_filter": NamespaceFilter.parse("archive:*")},
                id="namespace-glob-widens",
            ),
            pytest.param({"system_prefixes": ()}, id="no-system-prefixes"),
            pytest.param(
                {"project_context_root": Path("/tmp/proj-here")},
                id="in-project-boundary",
            ),
            pytest.param(
                {
                    "project_context_root": Path("/tmp/proj-here"),
                    "scope_filter": ScopeFilter.parse("project_shared"),
                },
                id="in-project-filter-does-not-widen",
            ),
            pytest.param(
                {"scope_filter": ScopeFilter.parse("project_shared")},
                id="out-of-project-filter-widens",
            ),
            pytest.param(
                {"scope_filter": ScopeFilter.parse("project_*")},
                id="out-of-project-glob-widens",
            ),
            pytest.param(
                {"scope_filter": ScopeFilter.parse("")},
                id="empty-scope-filter-carries-no-intent",
            ),
        ],
    )
    async def test_counts_match_the_python_predicate(self, backend, overrides):
        chunks = self._matrix()
        await backend.upsert_chunks(chunks)
        spec = _spec(**overrides)

        listed = await backend.list_chunks_by_source(SOURCE, limit=None)
        windows = await backend.get_context_windows(SOURCE, [c.id for c in listed], 0, **spec)

        expected = [neighbor_visible(c, **spec) for c in listed]
        # Every row is used as an anchor, so the anchor's own visibility is
        # exercised on both sides of the carve-out arithmetic.
        for pos, chunk in enumerate(listed):
            rows = windows[chunk.id]
            assert rows.visible_before == sum(expected[:pos]), f"row {pos} prefix count"
            assert rows.visible_total_excluding_anchor == (
                sum(expected) - (1 if expected[pos] else 0)
            ), f"row {pos} total"

    async def test_the_scope_boundary_survives_every_axis_being_off(self, backend):
        """Switching off the *filters* does not switch off the boundary.

        With no namespace prefixes, no filters and no temporal bound, the only
        rule left is ADR-0011 — and out-of-project that is ``scope = 'user'``.
        A fragment that read "no filters" as "no restriction" would count
        other projects' rows into the total, which is the leak the always-on
        half exists to prevent.
        """
        chunks = self._matrix()
        await backend.upsert_chunks(chunks)
        user_tier = sum(1 for c in chunks if c.metadata.scope == "user")

        rows = (
            await backend.get_context_windows(
                SOURCE,
                [chunks[0].id],
                0,
                ns_filter=None,
                system_prefixes=(),
                scope_filter=None,
                project_context_root=None,
                as_of_unix=None,
            )
        )[chunks[0].id]

        assert user_tier < len(chunks)  # the matrix does hold project-tier rows
        assert rows.visible_total_excluding_anchor == user_tier - 1


class TestExpansionSurfaces:
    """The callers, end to end on the real backend."""

    async def test_mem_expand_reads_past_the_old_cap(self, backend, monkeypatch):
        from memtomem.server.tools.search import mem_expand

        chunks = [_chunk(i) for i in range(6)]
        await backend.upsert_chunks(chunks)
        anchor = chunks[3]

        app = type("App", (), {})()
        app.storage = backend
        app.config = type("Cfg", (), {})()
        app.config.search = type("S", (), {"system_namespace_prefixes": list(SYSTEM_PREFIXES)})()

        async def _app(_ctx):
            return app

        monkeypatch.setattr("memtomem.server.tools.search._get_app_initialized", _app)
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda _app: None
        )

        out = await mem_expand(chunk_id=str(anchor.id), window=1, ctx=None)

        assert "chunk 4/6" in out
        assert "chunk 2" in out and "chunk 4" in out
        assert "chunk 1" not in out  # outside the window


async def test_expired_neighbour_shrinks_the_window_not_the_total(backend):
    """The two counts stay on the visible set while ``as_of`` moves."""
    chunks = [
        _chunk(0),
        _chunk(1, valid_to_unix=NOW - 1),
        _chunk(2),
    ]
    await backend.upsert_chunks(chunks)

    rows = (await backend.get_context_windows(SOURCE, [chunks[2].id], 2, **_spec()))[chunks[2].id]

    assert rows.visible_before == 1
    assert rows.visible_total_excluding_anchor == 1
