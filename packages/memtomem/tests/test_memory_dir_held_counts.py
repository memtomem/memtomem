"""Per-root status counts agree with the Sources tree about held sources (#2561).

``GET /api/sources`` hides held and pending sources, and consolidation-policy
summaries of them, through ``hidden_source_paths``. ``GET
/api/memory-dirs/status`` reports each root's totals plus the part the tree
hides, under the same rule, so totals minus held are what the tree lists for
the root. The delete-chunks preview stays the full total, because the remove
sweep deletes held rows too.

Real storage, real routes: the fixture holds, queues and derives the hidden
states the way production does, so the agreement is not between two mocks.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

from httpx import ASGITransport, AsyncClient

from memtomem.models import ORIGIN_CONSOLIDATION_POLICY
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured


async def _index(components, path: Path, word: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # Two sections, so a source has more than one chunk and a chunk total
    # cannot be mistaken for a source count.
    path.write_text(f"# One\n\n{word} one.\n\n# Two\n\n{word} two.\n", encoding="utf-8")
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=lambda texts, **_: [[0.1] * 1024 for _ in texts])
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    result = await components.index_engine.index_file(path)
    assert result.indexed_chunks > 0


async def _client_get(components, url: str) -> dict:
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(components, name))
    app.dependency_overrides[require_configured] = lambda: None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        resp = await client.get(url)
    assert resp.status_code == 200, resp.text
    return resp.json()


async def test_status_held_counts_match_what_sources_hides(components, memory_dir):
    storage = components.storage
    nested = memory_dir / "nested"
    components.config.indexing.memory_dirs = [memory_dir, nested]

    visible = memory_dir / "visible.md"
    held = memory_dir / "held.md"
    summary = memory_dir / "held.md.consolidated.md"
    pending = memory_dir / "pending.md"
    nested_visible = nested / "nested-visible.md"
    nested_held = nested / "nested-held.md"
    for path, word in (
        (visible, "visibleword"),
        (held, "heldword"),
        (summary, "summaryword"),
        (pending, "pendingword"),
        (nested_visible, "nestedvisible"),
        (nested_held, "nestedheld"),
    ):
        await _index(components, path, word)

    db = storage._get_db()
    db.execute(
        "UPDATE chunks SET origin=? WHERE source_file=?",
        (ORIGIN_CONSOLIDATION_POLICY, str(summary)),
    )
    db.commit()
    assert await storage.hold_source(held, "source_missing")
    assert await storage.hold_source(nested_held, "source_missing")
    pending.unlink()
    assert storage.queue_source_check_sync(pending)

    chunks_of = {Path(row[0]): row[1] for row in await storage.get_source_files_with_counts()}
    assert all(chunks_of[p] >= 2 for p in chunks_of), chunks_of

    listed = await _client_get(components, "/api/sources?limit=10000")
    listed_paths = {Path(s["path"]) for s in listed["sources"]}
    # The tree hides exactly the held, pending and derived-summary sources.
    assert listed_paths == {visible, nested_visible}

    status = await _client_get(components, "/api/memory-dirs/status")
    by_path = {Path(d["path"]): d for d in status["dirs"]}
    root, sub = by_path[memory_dir], by_path[nested]

    # Totals are unchanged: every source the root owns in the index.
    root_sources = (visible, held, summary, pending)
    assert root["source_file_count"] == len(root_sources)
    assert root["chunk_count"] == sum(chunks_of[p] for p in root_sources)
    # Held is what the tree hides; the nested root's held file is its own.
    root_hidden = (held, summary, pending)
    assert root["held_source_file_count"] == len(root_hidden)
    assert root["held_chunk_count"] == sum(chunks_of[p] for p in root_hidden)
    assert sub["source_file_count"] == 2
    assert sub["held_source_file_count"] == 1
    assert sub["held_chunk_count"] == chunks_of[nested_held]

    # Totals minus held equal the rows the tree lists for each root.
    for entry in (root, sub):
        rows = [s for s in listed["sources"] if s["memory_dir"] == entry["path"]]
        assert rows, entry["path"]
        assert entry["source_file_count"] - entry["held_source_file_count"] == len(rows)
        assert entry["chunk_count"] - entry["held_chunk_count"] == sum(
            s["chunk_count"] for s in rows
        )

    # The delete preview still includes held chunks: the sweep removes them.
    # The nested root sits inside ``memory_dir``, which keeps its files, so
    # removing it deletes nothing (#2534).
    assert root["delete_chunk_count"] == root["chunk_count"]
    assert sub["delete_chunk_count"] == 0


class _RowsOnly:
    def __init__(self, rows: list[tuple]) -> None:
        self._rows = rows

    async def get_source_files_with_counts(self) -> list[tuple]:
        return list(self._rows)


async def test_without_hidden_the_held_counts_are_zero(tmp_path):
    """Callers that pass no ``hidden`` set get the old numbers and zero held
    counts, so the new fields never claim a partition nobody computed."""
    from memtomem.indexing.engine import memory_dir_stats

    root = tmp_path / "root"
    root.mkdir()
    rows = [(root / "a.md", 3, "2026-09-26", "default", 1, 1, 1)]
    (entry,) = await memory_dir_stats(_RowsOnly(rows), [root])
    assert (entry["chunk_count"], entry["source_file_count"]) == (3, 1)
    assert (entry["held_chunk_count"], entry["held_source_file_count"]) == (0, 0)

    (entry,) = await memory_dir_stats(_RowsOnly(rows), [root], hidden={str(root / "a.md")})
    assert (entry["chunk_count"], entry["source_file_count"]) == (3, 1)
    assert (entry["held_chunk_count"], entry["held_source_file_count"]) == (3, 1)
