"""``retrieval=``: which legs a query search runs, and what each mode refuses.

A fake embedder stands in for a model: ``seat`` and ``chair`` share one axis,
so the dense leg finds an item BM25 cannot, and BM25 finds the item whose text
holds the query word. ``score_scale`` is read from the pipeline's own stats.
"""

from __future__ import annotations

import copy
import dataclasses
import math
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pytest

from langchain_memtomem import DenseSearchUnavailable, MemtomemPipelineStore
from memtomem.config import Mem2MemConfig
from memtomem.embedding.noop import NoopEmbedder

DIM = 8
_AXES = {"seat": 0, "chair": 0, "window": 1, "peanut": 2, "allergic": 2}
EMBEDDING = {"provider": "onnx", "model": "fake-bow", "dimension": DIM}

#: ``a`` holds the query word; ``b`` only its synonym; ``c`` neither.
ITEMS = {"a": "the seat by the window", "b": "a comfy chair", "c": "allergic to peanuts"}


def _vector(text: str) -> list[float]:
    vector = [0.0] * DIM
    for word in re.findall(r"[a-z]+", text.lower()):
        word = word if word in _AXES else word.removesuffix("s")
        if word in _AXES:
            vector[_AXES[word]] += 1.0
    vector[DIM - 1] += 0.1
    norm = math.sqrt(sum(x * x for x in vector))
    return [x / norm for x in vector]


class FakeEmbedder:
    preferred_concurrency = 1
    fail_queries = False

    def __init__(self, model_name: str, dimension: int) -> None:
        # Like the OpenAI and Ollama providers: the dimension is the configured one.
        self.model_name = model_name
        self.dimension = dimension

    async def embed_texts(self, texts: Sequence[str], **_kwargs: Any) -> list[list[float]]:
        return [_vector(text) for text in texts]

    async def embed_query(self, query: str) -> list[float]:
        if FakeEmbedder.fail_queries:
            raise RuntimeError("query embedding failed")
        return _vector(query)

    async def close(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fake_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    def create(config: Any) -> Any:
        if config.provider.lower() == "none":
            return NoopEmbedder()
        return FakeEmbedder(config.model, config.dimension)

    monkeypatch.setattr("memtomem.runtime.components.create_embedder", create)
    monkeypatch.setattr(FakeEmbedder, "fail_queries", False)


def _spy(store: MemtomemPipelineStore, *, alter: Any = None) -> list[Any]:
    """Record each pipeline pass's stats; ``alter(n, stats)`` may replace them."""
    assert store._comp is not None
    pipeline = store._comp.search_pipeline
    original = pipeline.search
    seen: list[Any] = []

    async def search(*args: Any, **kwargs: Any) -> Any:
        results, stats = await original(*args, **kwargs)
        if alter is not None:
            stats = alter(len(seen), stats)
        seen.append(stats)
        return results, stats

    pipeline.search = search  # type: ignore[method-assign]
    return seen


async def _filled(root: Path, **kwargs: Any) -> MemtomemPipelineStore:
    store = await MemtomemPipelineStore.open(root, **kwargs)
    for key, text in ITEMS.items():
        await store.aput(("u",), key, {"text": text})
    return store


async def _hits(store: MemtomemPipelineStore, query: str = "seat") -> list[tuple[str, float]]:
    return [(hit.key, hit.score) for hit in await store.asearch(("u",), query=query, limit=5)]


def _l2(left: list[float], right: list[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(left, right, strict=True)))


# -- the legs ---------------------------------------------------------------


async def test_each_mode_runs_its_legs(tmp_path: Path) -> None:
    found = {}
    for mode in ("bm25", "dense", "hybrid"):
        store = await _filled(
            tmp_path / mode, config={"embedding": EMBEDDING}, retrieval={"mode": mode}
        )
        try:
            found[mode] = [key for key, _ in await _hits(store)]
        finally:
            await store.aclose()
    assert found["bm25"] == ["a"]
    assert found["dense"][0] == "b"
    assert "b" in found["hybrid"] and "a" in found["hybrid"]


@pytest.mark.parametrize(
    ("retrieval", "scale"),
    [
        ({"mode": "bm25"}, "bm25"),
        ({"mode": "dense"}, "dense"),
        ({"mode": "hybrid"}, "rrf"),
        ({"mode": "hybrid", "weights": [1, 0]}, "bm25"),
        ({"mode": "hybrid", "weights": [0, 1]}, "dense"),
    ],
)
async def test_score_scale_follows_the_active_legs(
    tmp_path: Path, retrieval: dict[str, Any], scale: str
) -> None:
    store = await _filled(tmp_path / "s", config={"embedding": EMBEDDING}, retrieval=retrieval)
    try:
        seen = _spy(store)
        assert await _hits(store)
        assert {stats.score_scale for stats in seen} == {scale}
    finally:
        await store.aclose()


async def test_dense_scores_are_one_over_one_plus_l2_distance(tmp_path: Path) -> None:
    store = await _filled(
        tmp_path / "d", config={"embedding": EMBEDDING}, retrieval={"mode": "dense"}
    )
    try:
        scores = dict(await _hits(store))
    finally:
        await store.aclose()
    query = _vector("seat")
    for key in ("a", "b"):
        expected = 1.0 / (1.0 + _l2(query, _vector(ITEMS[key])))
        assert scores[key] == pytest.approx(expected, rel=1e-5)
    # Not cosine similarity, which would equal 1 - L2²/2 on unit vectors.
    cosine_a = 1.0 - _l2(query, _vector(ITEMS["a"])) ** 2 / 2
    assert scores["a"] != pytest.approx(cosine_a, rel=1e-3)


# -- 0.1.0 parity -----------------------------------------------------------


def _config_sections_0_1_0(store: MemtomemPipelineStore) -> dict[str, Any]:
    """``_config_sections`` as langchain-memtomem 0.1.0 shipped it (the oracle)."""
    overrides = copy.deepcopy(store._config)
    search = dict(overrides.pop("search", None) or {})
    search["system_namespace_prefixes"] = []
    return {
        **overrides,
        "search": search,
        "storage": {"sqlite_path": store.root / "index.db"},
        "indexing": {"memory_dirs": [store.projections_dir], "project_memory_dirs": []},
    }


@pytest.mark.parametrize(
    "config", [{}, {"search": {"rrf_k": 7, "enable_dense": False}}, {"embedding": EMBEDDING}]
)
def test_omitted_retrieval_builds_the_0_1_0_config(tmp_path: Path, config: dict) -> None:
    store = MemtomemPipelineStore(tmp_path / "p", config=config)
    built = Mem2MemConfig.without_environment(**store._config_sections([]))
    oracle = Mem2MemConfig.without_environment(**_config_sections_0_1_0(store))
    assert built.model_dump() == oracle.model_dump()


@pytest.mark.parametrize("retrieval", [None, {"mode": "hybrid"}, {}])
async def test_without_an_embedder_hybrid_ranks_as_0_1_0_did(
    tmp_path: Path, retrieval: dict[str, Any] | None
) -> None:
    # Measured on origin/main 91b1342f: one BM25 hit at RRF rank 1, both legs
    # enabled and the dense leg empty, so the score is 1 / (rrf_k + 1).
    store = await _filled(tmp_path / "h", retrieval=retrieval)
    try:
        seen = _spy(store)
        hits = await _hits(store)
    finally:
        await store.aclose()
    assert [key for key, _ in hits] == ["a"]
    assert hits[0][1] == pytest.approx(1 / 61)
    assert {stats.score_scale for stats in seen} == {"rrf"}


async def test_bm25_mode_is_not_the_default(tmp_path: Path) -> None:
    store = await _filled(tmp_path / "b", retrieval={"mode": "bm25"})
    try:
        seen = _spy(store)
        hits = await _hits(store)
    finally:
        await store.aclose()
    assert [key for key, _ in hits] == ["a"]
    assert hits[0][1] != pytest.approx(1 / 61)
    assert {stats.score_scale for stats in seen} == {"bm25"}


# -- refusals ---------------------------------------------------------------


@pytest.mark.parametrize("provider", ["none", "NONE", "None"])
async def test_dense_without_an_embedder_is_refused_before_anything_is_written(
    tmp_path: Path, provider: str
) -> None:
    # A dimension is given so the provider check, not the dimension check,
    # must refuse: the no-op embedder reports 0 whatever the config says.
    root = tmp_path / "r"
    with pytest.raises(ValueError, match="pass an embedding section"):
        await MemtomemPipelineStore.open(
            root,
            config={"embedding": {"provider": provider, "dimension": DIM}},
            retrieval={"mode": "dense"},
        )
    assert not root.exists() or not any(root.iterdir())


@pytest.mark.parametrize("dimension", [None, 0])
async def test_dense_with_an_embedder_of_no_dimension_is_refused(
    tmp_path: Path, dimension: int | None
) -> None:
    embedding: dict[str, Any] = {"provider": "openai", "model": "text-embedding-3-small"}
    if dimension is not None:
        embedding["dimension"] = dimension
    root = tmp_path / "z"
    with pytest.raises(ValueError, match="no vector dimension"):
        await MemtomemPipelineStore.open(
            root, config={"embedding": embedding}, retrieval={"mode": "dense"}
        )
    assert not root.exists() or not any(root.iterdir())


async def test_an_e5_profile_has_its_dimension_before_open(tmp_path: Path) -> None:
    # The guard reads the built config; E5's 384 is filled in by validation.
    store = MemtomemPipelineStore(
        tmp_path / "e5",
        config={"embedding": {"provider": "onnx", "model": "multilingual-e5-small"}},
        retrieval={"mode": "dense"},
    )
    built = Mem2MemConfig.without_environment(**store._config_sections([]))
    assert built.embedding.dimension == 384


async def test_dense_with_the_default_embedding_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="dense mode requires embeddings"):
        await MemtomemPipelineStore.open(tmp_path / "r", retrieval={"mode": "dense"})


@pytest.mark.parametrize("mode", ["bm25", "dense", "hybrid"])
@pytest.mark.parametrize(
    "bad",
    [
        {"weights": [0, 0]},
        {"weights": [-1, 1]},
        {"weights": [True, False]},
        {"weights": [math.nan, 1]},
        {"weights": [1, 1, 1]},
        {"rrf_k": 0},
    ],
)
def test_bad_weights_and_rrf_k_are_refused_in_every_mode(
    tmp_path: Path, mode: str, bad: dict[str, Any]
) -> None:
    with pytest.raises(ValueError):
        MemtomemPipelineStore(tmp_path / "v", retrieval={"mode": mode, **bad})


@pytest.mark.parametrize("retrieval", [{"mode": "vector"}, {"mode": "hybrid", "alpha": 0.5}])
def test_unknown_mode_and_keys_are_refused(tmp_path: Path, retrieval: dict[str, Any]) -> None:
    with pytest.raises(ValueError, match="Unknown retrieval"):
        MemtomemPipelineStore(tmp_path / "v", retrieval=retrieval)


@pytest.mark.parametrize("retrieval", [{}, {"mode": "bm25"}])
@pytest.mark.parametrize(
    "search",
    [
        {"enable_bm25": True},
        {"enable_dense": True},
        {"rrf_weights": [1.0, 1.0]},
        {"rrf_k": 60},
    ],
)
async def test_retrieval_and_the_same_search_keys_are_refused_together(
    tmp_path: Path, retrieval: dict[str, Any], search: dict[str, Any]
) -> None:
    root = tmp_path / "c"
    with pytest.raises(ValueError, match="choose the retrieval legs in one place"):
        await MemtomemPipelineStore.open(root, config={"search": search}, retrieval=retrieval)
    assert not root.exists() or not any(root.iterdir())


# -- the knobs are honoured -------------------------------------------------


async def test_hybrid_weights_order_the_legs(tmp_path: Path) -> None:
    order = {}
    for name, weights in (("lexical", [1, 0.01]), ("semantic", [0.01, 1])):
        store = await _filled(
            tmp_path / name,
            config={"embedding": EMBEDDING},
            retrieval={"mode": "hybrid", "weights": weights},
        )
        try:
            order[name] = [key for key, _ in await _hits(store)]
        finally:
            await store.aclose()
    assert order["lexical"][0] == "a"
    assert order["semantic"][0] == "b"


@pytest.mark.parametrize(
    ("mode", "weights", "top"), [("dense", [1, 0], "b"), ("bm25", [0, 1], "a")]
)
async def test_single_leg_modes_ignore_weights(
    tmp_path: Path, mode: str, weights: list[float], top: str
) -> None:
    store = await _filled(
        tmp_path / mode,
        config={"embedding": EMBEDDING},
        retrieval={"mode": mode, "weights": weights},
    )
    try:
        hits = await _hits(store)
    finally:
        await store.aclose()
    assert hits and hits[0][0] == top


async def test_rrf_k_reaches_fusion(tmp_path: Path) -> None:
    # ``a`` ranks first in both legs: its fused score is 2 / (rrf_k + 1).
    store = await _filled(
        tmp_path / "k", config={"embedding": EMBEDDING}, retrieval={"mode": "hybrid", "rrf_k": 9}
    )
    try:
        scores = dict(await _hits(store, "seat window"))
    finally:
        await store.aclose()
    assert scores["a"] == pytest.approx(2 / 10)


async def test_the_callers_dict_is_copied(tmp_path: Path) -> None:
    retrieval: dict[str, Any] = {"mode": "hybrid", "weights": [1.0, 1.0]}
    store = MemtomemPipelineStore(
        tmp_path / "m", config={"embedding": EMBEDDING}, retrieval=retrieval
    )
    # An aliased mode would run dense; an aliased weights list would fail to open.
    retrieval["mode"] = "dense"
    retrieval["weights"].append(9.0)
    await store._open()
    try:
        for key, text in ITEMS.items():
            await store.aput(("u",), key, {"text": text})
        retrieval["mode"] = "bm25"
        retrieval["weights"][:] = [0.0, 1.0]
        seen = _spy(store)
        assert await _hits(store)
        assert {stats.score_scale for stats in seen} == {"rrf"}
    finally:
        await store.aclose()


# -- the dense leg did not run ------------------------------------------------


def _fail_pass(target: int) -> Any:
    def alter(n: int, stats: Any) -> Any:
        if n == target:
            return dataclasses.replace(stats, dense_error="injected")
        return stats

    return alter


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_a_failed_first_pass(tmp_path: Path, mode: str) -> None:
    store = await _filled(
        tmp_path / mode, config={"embedding": EMBEDDING}, retrieval={"mode": mode}
    )
    try:
        _spy(store, alter=_fail_pass(0))
        if mode == "dense":
            with pytest.raises(DenseSearchUnavailable, match="injected"):
                await _hits(store)
        else:
            assert await _hits(store)
    finally:
        await store.aclose()


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_a_failed_repair_re_search(
    tmp_path: Path, mode: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = await _filled(
        tmp_path / mode, config={"embedding": EMBEDDING}, retrieval={"mode": mode}
    )
    try:
        judge = store._judge_hits
        first = True

        async def judged(*args: Any, **kwargs: Any) -> Any:
            nonlocal first
            served, repaired = await judge(*args, **kwargs)
            if first:
                first = False
                return served, True
            return served, repaired

        monkeypatch.setattr(store, "_judge_hits", judged)
        seen = _spy(store, alter=_fail_pass(1))
        if mode == "dense":
            with pytest.raises(DenseSearchUnavailable, match="injected"):
                await _hits(store)
        else:
            assert await _hits(store)
        assert len(seen) >= 2
    finally:
        await store.aclose()


@pytest.mark.parametrize("mode", ["dense", "hybrid"])
async def test_a_failed_second_window(tmp_path: Path, mode: str) -> None:
    # Three items and ``limit=5`` never fill the page, so the search widens.
    store = await _filled(
        tmp_path / mode, config={"embedding": EMBEDDING}, retrieval={"mode": mode}
    )
    try:
        seen = _spy(store, alter=_fail_pass(1))
        if mode == "dense":
            with pytest.raises(DenseSearchUnavailable, match="injected"):
                await _hits(store)
            assert len(seen) == 2
        else:
            assert await _hits(store)
            assert len(seen) > 2
    finally:
        await store.aclose()


async def test_a_failed_query_embedding(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = {mode: tmp_path / mode for mode in ("dense", "hybrid")}
    stores = {
        mode: await _filled(path, config={"embedding": EMBEDDING}, retrieval={"mode": mode})
        for mode, path in root.items()
    }
    try:
        monkeypatch.setattr(FakeEmbedder, "fail_queries", True)
        with pytest.raises(DenseSearchUnavailable, match="dense search failed"):
            await _hits(stores["dense"])
        assert [key for key, _ in await _hits(stores["hybrid"])] == ["a"]
    finally:
        for store in stores.values():
            await store.aclose()


async def test_stored_embeddings_from_another_model(tmp_path: Path) -> None:
    other = {**EMBEDDING, "model": "fake-other"}
    for mode in ("dense", "hybrid"):
        root = tmp_path / mode
        await (
            await _filled(root, config={"embedding": EMBEDDING}, retrieval={"mode": mode})
        ).aclose()
        store = await MemtomemPipelineStore.open(
            root, config={"embedding": other}, retrieval={"mode": mode}
        )
        try:
            seen = _spy(store)
            if mode == "dense":
                with pytest.raises(DenseSearchUnavailable, match="another embedding model"):
                    await _hits(store)
            else:
                assert [key for key, _ in await _hits(store)] == ["a"]
                assert {stats.score_scale for stats in seen} == {"bm25"}
        finally:
            await store.aclose()


# -- reopen -------------------------------------------------------------------


async def test_switching_mode_on_reopen_needs_no_reindex(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memtomem.indexing.engine import IndexEngine

    root = tmp_path / "s"
    first = await _filled(root, config={"embedding": EMBEDDING}, retrieval={"mode": "bm25"})
    await first.aclose()
    calls: list[Any] = []
    index_file = IndexEngine.index_file

    async def counted(self: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(args)
        return await index_file(self, *args, **kwargs)

    # Installed before the reopen, so the startup repair pass is counted too.
    monkeypatch.setattr(IndexEngine, "index_file", counted)
    store = await MemtomemPipelineStore.open(
        root, config={"embedding": EMBEDDING}, retrieval={"mode": "dense"}
    )
    try:
        assert (await _hits(store))[0][0] == "b"
    finally:
        await store.aclose()
    assert calls == []
