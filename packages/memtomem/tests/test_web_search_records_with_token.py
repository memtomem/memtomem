"""``GET /api/search`` records a search only when it carries the CSRF token.

A recorded web search bumps the returned chunks' access counts, stores the
query in the search history and returns a run id for feedback. A GET needs no
token to be answered, and one with neither ``Origin`` nor ``Referer`` passes the
origin check as a script does, so a page elsewhere could make the user's
browser write those records. The same search runs either way (an unrecorded one
also skips the result and query-expansion caches); only the recording needs the
token, which the UI sends on its searches.
"""

from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.web.app import create_app
from memtomem.web.deps import require_configured


def _app(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return app


@pytest.fixture
async def indexed(bm25_only_components):
    comp, mem_dir = bm25_only_components
    note = mem_dir / "note.md"
    note.write_text("## Falcon\n\nthe falcon migration notes\n", encoding="utf-8")
    await comp.index_engine.index_file(note)
    (chunk,) = await comp.storage.list_chunks_by_source(note)
    return comp, chunk


def _history_rows(comp) -> int:
    db = comp.storage._get_db()
    return db.execute("SELECT COUNT(*) FROM query_history").fetchone()[0]


async def _search(app, headers: dict[str, str]):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get("/api/search", params={"q": "falcon"}, headers=headers)


@pytest.mark.parametrize(
    "headers",
    [{}, {"X-Memtomem-CSRF": "not-the-token"}],
    ids=["no-token", "wrong-token"],
)
async def test_a_search_without_the_token_is_answered_but_not_recorded(indexed, headers):
    comp, chunk = indexed
    app = _app(comp)
    runs_before = _history_rows(comp)

    resp = await _search(app, headers)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["chunk"]["id"] for r in body["results"]] == [str(chunk.id)]
    assert body["query_run_id"] is None
    assert (await comp.storage.get_access_counts([chunk.id])).get(str(chunk.id), 0) == 0
    assert _history_rows(comp) == runs_before
    # A token that was sent but is not this process's is reported, so the UI
    # can fetch the current one; no token at all is a script, not a stale page.
    stale = resp.headers.get("X-Memtomem-CSRF-Stale")
    assert stale == ("1" if headers else None)


async def test_a_search_with_the_token_is_recorded(indexed):
    comp, chunk = indexed
    app = _app(comp)
    runs_before = _history_rows(comp)

    resp = await _search(app, {"X-Memtomem-CSRF": app.state.csrf_token})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert [r["chunk"]["id"] for r in body["results"]] == [str(chunk.id)]
    assert body["query_run_id"] is not None
    assert (await comp.storage.get_access_counts([chunk.id]))[str(chunk.id)] == 1
    assert _history_rows(comp) == runs_before + 1
    assert "X-Memtomem-CSRF-Stale" not in resp.headers
