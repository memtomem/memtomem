"""``mem_add``'s duplicate warning judges stored-vector distance, not a search score.

The check used to run a search over the new note and keep results scoring in
``[0.90, 0.9999)``, reading the top as "self". That window only means something
on a dense-only store: with a reranker the scores are logits (the note itself
warned at 0.92, a real neighbour at 1.04 was dropped as "self"), and on the
default BM25+dense store they are RRF sums under 0.033, so it never fired.

These tests run a real SQLite stack with an embedder whose vectors are chosen per
note, so every dense score below is known in advance.
"""

from __future__ import annotations

import hashlib
import math
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.config import Mem2MemConfig
from memtomem.server.component_factory import Components, close_components, create_components

_DIM = 8


def _unit(*values: float) -> list[float]:
    padded = list(values) + [0.0] * (_DIM - len(values))
    norm = math.sqrt(sum(v * v for v in padded))
    return [v / norm for v in padded]


# Markers a note carries to pick its vector: plain words, since the chunker
# rewrites ``[[wikilinks]]`` before embedding. Storage scores ``1/(1+L2)``.
_VECTORS = {
    "vecmarkALPHA": _unit(1.0),
    "vecmarkNEAR": _unit(1.0, 0.03),  # L2 ~0.030 -> ~0.971
    "vecmarkMID": _unit(1.0, 0.0, 0.08),  # L2 ~0.080 -> ~0.926
    "vecmarkFAR": _unit(1.0, 0.25),  # L2 ~0.245 -> ~0.803
    "vecmarkABOVE": _unit(1.0, 0.0, 0.0, 0.1700),  # just above 0.85 (~0.856)
    "vecmarkBELOW": _unit(1.0, 0.0, 0.0, 0.1840),  # just below 0.85 (~0.846)
}
_QUERY_VECTOR = _unit(0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.0)


def _dense_score(a: str, b: str) -> float:
    l2 = math.sqrt(sum((x - y) ** 2 for x, y in zip(_VECTORS[a], _VECTORS[b])))
    return 1.0 / (1.0 + l2)


class _MarkerEmbedder:
    """Documents embed by marker; anything unmarked lands far from every marker.

    ``embed_query`` returns a vector orthogonal to all documents and counts its
    calls, so a query-side probe would find nothing and is detectable.
    """

    def __init__(self) -> None:
        self.query_calls = 0

    @property
    def dimension(self) -> int:
        return _DIM

    @property
    def model_name(self) -> str:
        return "marker-test"

    def _vector(self, text: str) -> list[float]:
        for marker, vector in _VECTORS.items():
            if marker in text:
                return vector
        digest = hashlib.sha256(text.encode()).digest()
        return _unit(0.0, 0.0, 0.0, *(1.0 + b for b in digest[:4]))

    async def embed_texts(self, texts, *, on_progress=None):
        return [self._vector(t) for t in texts]

    async def embed_query(self, query: str):
        self.query_calls += 1
        return list(_QUERY_VECTOR)

    async def close(self) -> None:
        pass


async def _stack(tmp_path: Path, *, dense: bool = True) -> tuple[Components, Path]:
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir(parents=True, exist_ok=True)
    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "dup.db"
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "marker-test"
    config.embedding.dimension = _DIM
    config.search.enable_dense = dense
    config.rerank.enabled = False
    config.llm.enabled = False

    import memtomem.config as _cfg
    from memtomem.runtime import components as _factory

    saved = (_cfg.load_config_d, _cfg.load_config_overrides, _factory.create_embedder)
    _cfg.load_config_d = lambda c: None
    _cfg.load_config_overrides = lambda c: None
    _factory.create_embedder = lambda _c: _MarkerEmbedder()
    try:
        comp = await create_components(config)
    finally:
        _cfg.load_config_d, _cfg.load_config_overrides, _factory.create_embedder = saved
    return comp, mem_dir


@pytest.fixture
async def stack(tmp_path, monkeypatch):
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    comp, mem_dir = await _stack(tmp_path)
    try:
        yield comp, mem_dir
    finally:
        await close_components(comp)


async def _add(comp: Components, content: str, **kwargs) -> str:
    from memtomem.server.context import AppContext
    from memtomem.server.tools.memory_crud import mem_add

    ctx = StubCtx(AppContext.from_components(comp))
    return await mem_add(content=content, ctx=ctx, **kwargs)  # type: ignore[arg-type]


def _note(marker: str, words: str) -> str:
    return f"{words} {marker} — a note long enough to pass the length gate."


def _warned(result: str) -> list[str]:
    _, sep, tail = result.partition("⚠ Similar memories found:")
    if not sep:
        return []
    return [line.strip() for line in tail.splitlines() if line.strip().startswith("- (")]


class TestSelfIsNeverAWarning:
    @pytest.mark.asyncio
    async def test_a_note_into_an_empty_store_warns_about_nothing(self, stack):
        comp, _ = stack
        result = await _add(comp, _note("vecmarkALPHA", "Deploys bake five minutes"), file="a.md")
        assert "Chunks indexed: 1" in result
        assert _warned(result) == []

    @pytest.mark.asyncio
    async def test_more_new_chunks_than_probes_are_all_excluded(self, stack, monkeypatch):
        """Every chunk of the write is "self", not only the ones that probe."""
        from memtomem.server.tools import memory_crud

        monkeypatch.setattr(memory_crud, "_DUPLICATE_PROBES", 2)
        comp, _ = stack
        sections = "\n\n".join(
            f"### Part {i}\n\n" + _note("vecmarkALPHA", f"Section {i} of one long write " * 20)
            for i in range(10)
        )
        result = await _add(comp, sections, title="Long", file="long.md")
        indexed = int(result.split("Chunks indexed: ")[1].split("\n")[0])
        assert indexed > memory_crud._DUPLICATE_PROBES, result
        assert _warned(result) == []


class TestRealNeighboursAreReported:
    @pytest.mark.asyncio
    async def test_a_byte_identical_note_in_another_file_is_reported(self, stack):
        """Score 1.0 used to fall outside ``< 0.9999`` and read as the note itself."""
        comp, _ = stack
        body = _note("vecmarkALPHA", "Deploys bake five minutes")
        await _add(comp, body, title="First", file="first.md")
        result = await _add(comp, body, title="Second", file="second.md")
        warned = _warned(result)
        assert len(warned) == 1, result
        assert warned[0].startswith("- (dense 1.00) ")

    @pytest.mark.asyncio
    async def test_threshold_is_on_the_dense_score(self, stack):
        from memtomem.server.tools.memory_crud import _DUPLICATE_DENSE_MIN

        assert _DUPLICATE_DENSE_MIN == 0.85
        assert 0.85 < _dense_score("vecmarkALPHA", "vecmarkABOVE") < 0.86
        assert 0.84 < _dense_score("vecmarkALPHA", "vecmarkBELOW") < 0.85
        comp, _ = stack
        await _add(comp, _note("vecmarkABOVE", "Above neighbour"), title="Above", file="a.md")
        await _add(comp, _note("vecmarkBELOW", "Below neighbour"), title="Below", file="b.md")
        result = await _add(comp, _note("vecmarkALPHA", "The new note"), title="New", file="x.md")
        warned = _warned(result)
        assert len(warned) == 1, result
        assert "Above neighbour" in warned[0]
        assert f"(dense {_dense_score('vecmarkALPHA', 'vecmarkABOVE'):.2f})" in warned[0]
        assert "%" not in result

    @pytest.mark.asyncio
    async def test_expired_rows_cannot_crowd_out_a_valid_neighbour(self, stack):
        """Validity is judged after ``dense_search``'s LIMIT, so the pool widens."""
        comp, mem_dir = stack
        expired = mem_dir / "expired.md"
        expired.write_text(
            "---\nvalid_to: 2000-01-01\n---\n\n"
            + "\n\n".join(
                f"## Old {i}\n\n" + _note("vecmarkNEAR", f"Expired twin {i}") for i in range(12)
            ),
            encoding="utf-8",
        )
        await comp.index_engine.index_file(expired)
        await _add(comp, _note("vecmarkMID", "Still valid"), title="Valid", file="v.md")
        result = await _add(comp, _note("vecmarkALPHA", "The new note"), title="New", file="x.md")
        warned = _warned(result)
        assert [w for w in warned if "Expired twin" in w] == [], result
        assert any("Still valid" in w for w in warned), result

    @pytest.mark.asyncio
    async def test_a_pool_that_stays_full_widens_only_once(self, stack, monkeypatch):
        comp, mem_dir = stack
        expired = mem_dir / "expired.md"
        expired.write_text(
            "---\nvalid_to: 2000-01-01\n---\n\n"
            + "\n\n".join(
                f"## Old {i}\n\n" + _note("vecmarkNEAR", f"Expired twin {i}") for i in range(40)
            ),
            encoding="utf-8",
        )
        await comp.index_engine.index_file(expired)
        pools: list[int] = []
        real = comp.storage.dense_search

        async def spy(embedding, top_k=20, **kwargs):
            pools.append(top_k)
            return await real(embedding, top_k=top_k, **kwargs)

        monkeypatch.setattr(comp.storage, "dense_search", spy)
        result = await _add(comp, _note("vecmarkALPHA", "The new note"), title="New", file="x.md")
        assert _warned(result) == [], result
        # One probe: 3 neighbours + 1 excluded self, then one 4x retry.
        assert pools == [4, 16]


class TestScopeOfTheCheck:
    @pytest.mark.asyncio
    async def test_a_system_namespace_neighbour_is_not_reported(self, stack):
        comp, _ = stack
        await _add(
            comp,
            _note("vecmarkALPHA", "Archived copy"),
            title="Old",
            file="old.md",
            namespace="archive:old",
        )
        result = await _add(comp, _note("vecmarkALPHA", "Fresh copy"), title="New", file="new.md")
        assert _warned(result) == [], result

    @pytest.mark.asyncio
    async def test_the_check_runs_no_search_and_embeds_no_query(self, stack, monkeypatch):
        """No search score, rerank, modifier or rescue can reach the threshold."""
        comp, _ = stack

        async def _no_search(*args, **kwargs):
            raise AssertionError("the duplicate check must not run a search")

        await _add(comp, _note("vecmarkALPHA", "First"), title="First", file="1.md")
        monkeypatch.setattr(comp.search_pipeline, "search", _no_search)
        embedder = comp.embedder
        before = embedder.query_calls
        result = await _add(comp, _note("vecmarkALPHA", "Second"), title="Second", file="2.md")
        assert embedder.query_calls == before
        assert len(_warned(result)) == 1, result

    @pytest.mark.asyncio
    async def test_a_check_that_warns_records_no_access_and_no_query(self, stack):
        """#2683's contract: the check is a read. It ran here (it warned)."""
        comp, _ = stack
        await _add(comp, _note("vecmarkALPHA", "First"), title="First", file="1.md")
        result = await _add(comp, _note("vecmarkALPHA", "Second"), title="Second", file="2.md")
        assert len(_warned(result)) == 1, result
        accessed = (
            comp.storage._get_read_db()
            .execute("SELECT COUNT(*) FROM chunks WHERE access_count > 0")
            .fetchone()[0]
        )
        assert accessed == 0
        assert await comp.storage.get_search_runs() == []


@pytest.mark.asyncio
async def test_a_store_without_vectors_adds_cleanly_with_no_warning(bm25_only_components):
    comp, _ = bm25_only_components
    body = _note("vecmarkALPHA", "Deploys bake five minutes")
    await _add(comp, body, title="First", file="first.md")
    result = await _add(comp, body, title="Second", file="second.md")
    assert result.startswith("Memory added to ")
    assert _warned(result) == []
