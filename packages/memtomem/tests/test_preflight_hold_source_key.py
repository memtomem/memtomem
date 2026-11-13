"""A memory tool's preflight refusal holds the source whose bytes it judged (#3183).

A tool reads the file, the engine's preflight judges those bytes, and a refusal
holds the file's chunks with ``redaction_blocked``. The hold used to resolve the
path last, after the preflight and the refusal's replay. A path replaced by a
symlink to another indexed source in between held the link's target: the target
was hidden, and the refused source's chunks stayed searchable.

The key is now resolved with the read (``read_with_source_key``) and carried on
the rejection. Each surface below runs through a different caller of
``preflight_mutation``. The relink is injected after the read and before the
preflight, the window a key resolved by the preflight itself would still have.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from helpers import StubCtx
from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD, RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.web.app import create_app
from memtomem.web.deps import require_configured

# AWS's documented example key, assembled at runtime so push protection does not
# see a literal credential in the repository.
_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_REFUSED = "## A\n\nAlphaword entry about deploys\n"
_TARGET = "## B\n\nBravoword entry about reviews\n"

SURFACES = ("mcp_add", "mcp_edit", "mcp_delete", "web_add", "web_edit", "web_delete", "promote")


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _link_or_skip(link: Path, target: Path) -> None:
    link.unlink(missing_ok=True)
    try:
        link.symlink_to(target)
    except OSError as exc:  # Windows without symlink privilege
        pytest.skip(f"cannot create symlink: {exc}")


def _reason(comp, key: str) -> str | None:
    """The hold on exactly ``key``, never a re-resolved path (that would read the target's)."""
    row = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


async def _hits(comp, query: str) -> list[str]:
    comp.search_pipeline.invalidate_cache()
    results, _ = await comp.search_pipeline.search(query, top_k=10)
    return [r.chunk.content for r in results]


async def _refused_and_target(comp, mem_dir: Path):
    """``alpha.md`` indexed clean then given a secret no tool scanned; ``bravo.md`` indexed."""
    refused = mem_dir / "alpha.md"
    refused.write_text(_REFUSED, encoding="utf-8")
    target = mem_dir / "bravo.md"
    target.write_text(_TARGET, encoding="utf-8")
    for path in (refused, target):
        stats = await comp.index_engine.index_file(path, ingress_guarded=True)
        assert stats.indexed_chunks == 1, stats
    with refused.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## K\n\naws {_KEY}\n")
    return refused, target, norm_path(refused), norm_path(target)


def _relink_before_the_preflight(monkeypatch, engine, leaf: Path, target: Path) -> list[Path]:
    """Replace ``leaf`` with a link to ``target`` after the tool's read, before the preflight."""
    real = engine.preflight_mutation
    calls: list[Path] = []

    def preflight(file_path, pre_image, **kwargs):
        if not calls:
            _link_or_skip(leaf, target)
        calls.append(file_path)
        return real(file_path, pre_image, **kwargs)

    monkeypatch.setattr(engine, "preflight_mutation", preflight)
    return calls


def _web_client(comp):
    app = create_app(lifespan=None, mode="dev")
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.state.csrf_enforce = False
    app.dependency_overrides[require_configured] = lambda: None
    return AsyncClient(
        transport=ASGITransport(app=app, raise_app_exceptions=False), base_url="http://test"
    )


async def _send(comp, surface: str, refused: Path, chunk_id: str):
    """Drive ``surface`` at ``refused``; the tool's text for MCP, the response for web."""
    if surface.startswith("mcp"):
        ctx = StubCtx(AppContext.from_components(comp))
        if surface == "mcp_add":
            return await memory_crud.mem_add(content="gamma entry", file=str(refused), ctx=ctx)  # type: ignore[arg-type]
        if surface == "mcp_edit":
            return await memory_crud.mem_edit(chunk_id, "revised", ctx=ctx)  # type: ignore[arg-type]
        return await memory_crud.mem_delete(chunk_id=chunk_id, ctx=ctx)  # type: ignore[arg-type]
    async with _web_client(comp) as client:
        if surface == "web_add":
            return await client.post("/api/add", json={"content": "gamma", "file": refused.name})
        if surface == "web_edit":
            return await client.patch(f"/api/chunks/{chunk_id}", json={"new_content": "revised"})
        if surface == "web_delete":
            return await client.delete(f"/api/chunks/{chunk_id}")
        await comp.storage.scratch_set("note", "gamma")
        return await client.post("/api/scratch/note/promote", json={"file": str(refused)})


async def _request(comp, surface: str, refused: Path, chunk_id: str):
    """Drive ``surface`` at ``refused`` and require the refusal with nothing written."""
    resp = await _send(comp, surface, refused, chunk_id)
    if isinstance(resp, str):
        assert "refused" in resp, resp
        return
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"]["detail"] == "index_blocked", resp.text
    assert resp.json()["detail"]["written"] is False, resp.text


async def test_the_engine_holds_the_key_it_was_given(bm25_only_components, tmp_path):
    comp, mem_dir = bm25_only_components
    refused, target, refused_key, target_key = await _refused_and_target(comp, mem_dir)
    preflight = comp.index_engine.preflight_mutation(
        refused, refused.read_bytes(), source_path=refused.resolve()
    )
    assert preflight.rejection is not None
    _link_or_skip(refused, target)

    rejection = await comp.index_engine.refuse_mutation(refused, preflight)

    assert rejection.held is True
    assert _reason(comp, refused_key) == REDACTION_BLOCKED_HOLD
    assert _reason(comp, target_key) is None


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_path_relinked_after_the_read_holds_the_refused_source(
    bm25_only_components, monkeypatch, surface
):
    comp, mem_dir = bm25_only_components
    refused, target, refused_key, target_key = await _refused_and_target(comp, mem_dir)
    (chunk,) = await comp.storage.list_chunks_by_source(refused)
    assert await _hits(comp, "Alphaword") and await _hits(comp, "Bravoword")
    calls = _relink_before_the_preflight(monkeypatch, comp.index_engine, refused, target)

    await _request(comp, surface, refused, str(chunk.id))

    assert calls, "the preflight never ran, so nothing was relinked"
    assert refused.is_symlink()
    assert target.read_text(encoding="utf-8") == _TARGET
    # The source whose bytes were refused is held, not the link's target.
    assert _reason(comp, refused_key) == REDACTION_BLOCKED_HOLD
    assert _reason(comp, target_key) is None
    assert await _hits(comp, "Alphaword") == []
    assert await _hits(comp, "Bravoword")


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_clean_source_relinked_after_the_read_is_held_as_relinked(
    bm25_only_components, monkeypatch, surface
):
    """No refusal from the judge, and the source that was read is held all the same (#3291).

    Each surface is a different caller of ``preflight_mutation``. The query is
    warmed first and asked again without dropping the cache: the hold moves the
    store's visibility epoch, and the pipeline drops its warmed answers when it
    sees that. With the epoch held still, this assertion fails for six of the
    seven surfaces; ``mcp_add`` passes either way, because its wrapper
    (``memory_crud._preflight_refusal``) drops the cache on any refusal.
    """
    comp, mem_dir = bm25_only_components
    clean = mem_dir / "alpha.md"
    clean.write_text(_REFUSED, encoding="utf-8")
    target = mem_dir / "bravo.md"
    target.write_text(_TARGET, encoding="utf-8")
    for path in (clean, target):
        stats = await comp.index_engine.index_file(path, ingress_guarded=True)
        assert stats.indexed_chunks == 1, stats
    clean_key, target_key = norm_path(clean), norm_path(target)
    (chunk,) = await comp.storage.list_chunks_by_source(clean)
    warmed, _ = await comp.search_pipeline.search("Alphaword", top_k=10)
    assert warmed and await _hits(comp, "Bravoword")
    warmed, _ = await comp.search_pipeline.search("Alphaword", top_k=10)
    calls = _relink_before_the_preflight(monkeypatch, comp.index_engine, clean, target)

    await _request(comp, surface, clean, str(chunk.id))

    assert calls, "the preflight never ran, so nothing was relinked"
    assert clean.is_symlink()
    assert target.read_text(encoding="utf-8") == _TARGET
    assert _reason(comp, clean_key) == RELINKED_HOLD
    assert _reason(comp, target_key) is None
    still_warm, _ = await comp.search_pipeline.search("Alphaword", top_k=10)
    assert still_warm == [], "the source is held but a warmed answer still serves it"
    assert await _hits(comp, "Bravoword")


@pytest.mark.parametrize("surface", SURFACES)
async def test_a_path_that_still_resolves_is_written_and_nothing_is_held(
    bm25_only_components, surface
):
    """The relinked hold is asked for only by a preflight that found a relink."""
    comp, mem_dir = bm25_only_components
    clean = mem_dir / "alpha.md"
    clean.write_text(_REFUSED, encoding="utf-8")
    stats = await comp.index_engine.index_file(clean, ingress_guarded=True)
    assert stats.indexed_chunks == 1, stats
    (chunk,) = await comp.storage.list_chunks_by_source(clean)

    resp = await _send(comp, surface, clean, str(chunk.id))

    if isinstance(resp, str):
        assert "refused" not in resp and not resp.startswith("Error"), resp
    else:
        assert resp.status_code == 200, resp.text
    assert _reason(comp, norm_path(clean)) is None
    assert not clean.is_symlink()
    assert clean.read_text(encoding="utf-8") != _REFUSED
