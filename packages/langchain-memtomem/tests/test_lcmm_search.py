"""Query search: namespace selection, item pagination, scores, the recall bound."""

from __future__ import annotations

from pathlib import Path

import pytest

from langchain_memtomem import MemtomemPipelineStore
from langchain_memtomem._protocol import encode_segment, to_memtomem_namespace


async def _keys(store: MemtomemPipelineStore, prefix: tuple[str, ...], **kwargs) -> list[str]:
    return [item.key for item in await store.asearch(prefix, query="apples", **kwargs)]


async def test_base64_prefix_collision_does_not_leak(store: MemtomemPipelineStore) -> None:
    # ``abc`` encodes to ``smfrgg``; ``abcdef`` to ``smfrggzdfmy`` — a LIKE
    # prefix over-matches, the decode-then-filter step must not.
    assert to_memtomem_namespace(("abcdef",)).startswith(to_memtomem_namespace(("abc",)))
    await store.aput(("abc",), "short", {"text": "apples short"})
    await store.aput(("abcdef",), "long", {"text": "apples long"})
    assert await _keys(store, ("abc",)) == ["short"]
    assert await _keys(store, ("abcdef",)) == ["long"]
    assert set(await _keys(store, ())) == {"short", "long"}


@pytest.mark.parametrize(
    ("left", "right"),
    [(("aaa",), ("aaG",)), (("users", "aaa"), ("users", "aaG")), (("Users",), ("users",))],
)
async def test_namespaces_whose_base64_differs_only_in_case_stay_apart(
    root: Path, left: tuple[str, ...], right: tuple[str, ...]
) -> None:
    """``aaa``/``aaG`` are ``YWFh``/``YWFH`` in base64: one path on a case-folding fs.

    The store's own codec is single-case, so the two namespaces are two
    directories everywhere, and neither leaks into the other's search, before
    or after a reopen (CI caught the leak on Windows; the reopen loss
    reproduced on macOS).
    """
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(left, "k", {"text": "apples left"})
        await store.aput(right, "k", {"text": "apples right"})
        assert await _keys(store, left) == ["k"]
        assert (await store.asearch(left, query="apples"))[0].value == {"text": "apples left"}
        assert (await store.asearch(right, query="apples"))[0].value == {"text": "apples right"}
    finally:
        await store.aclose()
    reopened = await MemtomemPipelineStore.open(root)
    try:
        for namespace, text in ((left, "apples left"), (right, "apples right")):
            hits = await reopened.asearch(namespace, query="apples")
            assert [(h.namespace, h.value["text"]) for h in hits] == [(namespace, text)]
            assert (await reopened.aget(namespace, "k")).value == {"text": text}  # type: ignore[union-attr]
        assert set(await reopened.alist_namespaces()) >= {left, right}
    finally:
        await reopened.aclose()


async def test_keys_whose_base64_differs_only_in_case_coexist(root: Path) -> None:
    store = await MemtomemPipelineStore.open(root)
    try:
        await store.aput(("docs",), "aaa", {"text": "apples lower"})
        await store.aput(("docs",), "aaG", {"text": "apples upper"})
        assert (await store.aget(("docs",), "aaa")).value == {"text": "apples lower"}  # type: ignore[union-attr]
        assert (await store.aget(("docs",), "aaG")).value == {"text": "apples upper"}  # type: ignore[union-attr]
        assert sorted(await _keys(store, ("docs",))) == ["aaG", "aaa"]
        assert sorted(i.key for i in await store.asearch(("docs",))) == ["aaG", "aaa"]
    finally:
        await store.aclose()


async def test_multi_chunk_items_appear_once_with_their_best_score(
    store: MemtomemPipelineStore, long_text
) -> None:
    for n in range(4):
        await store.aput(("docs",), f"d{n}", {"text": long_text("apples")})
    hits = await store.asearch(("docs",), query="apples", limit=10)
    keys = [item.key for item in hits]
    assert sorted(keys) == ["d0", "d1", "d2", "d3"]
    assert len(keys) == len(set(keys))
    # the item's score is the best of its chunks: never below any single hit
    comp = store._comp
    assert comp is not None
    raw, _ = await comp.search_pipeline.search("apples", top_k=50, scope=store.scope)
    by_item: dict[str, float] = {}
    for result in raw:
        key = Path(result.chunk.metadata.source_file).stem
        by_item[key] = max(by_item.get(key, 0.0), result.score)
    for item in hits:
        assert item.score == pytest.approx(by_item[encode_segment(item.key)])


async def test_pages_are_over_items_not_chunks(store: MemtomemPipelineStore, long_text) -> None:
    for n in range(6):
        await store.aput(("docs",), f"d{n}", {"text": long_text("apples")})
    full = await _keys(store, ("docs",), limit=6)
    assert len(full) == 6
    pages = [await _keys(store, ("docs",), limit=2, offset=offset) for offset in (0, 2, 4)]
    assert [k for page in pages for k in page] == full
    assert await _keys(store, ("docs",), limit=2, offset=6) == []


@pytest.mark.parametrize("budget", [1, 3, 4, 7, 20, 33])
async def test_recall_is_bounded_by_max_chunks_per_search(root: Path, budget: int) -> None:
    store = await MemtomemPipelineStore.open(root, max_chunks_per_search=budget)
    try:
        for n in range(25):
            await store.aput(("docs",), f"d{n:02d}", {"text": f"apples note {n}"})
        hits = await _keys(store, ("docs",), limit=30)
        assert 0 < len(hits) <= min(budget, 25)
        assert await _keys(store, ("docs",), limit=2, offset=budget) == []
    finally:
        await store.aclose()


async def test_budget_below_one_is_refused(root: Path) -> None:
    with pytest.raises(ValueError):
        MemtomemPipelineStore(root, max_chunks_per_search=0)


async def test_widening_reaches_items_behind_many_chunks_of_others(root: Path, long_text) -> None:
    store = await MemtomemPipelineStore.open(root, max_chunks_per_search=200)
    try:
        for n in range(3):
            await store.aput(("docs",), f"big{n}", {"text": long_text("apples", sections=8)})
        await store.aput(("docs",), "small", {"text": "apples once"})
        hits = await _keys(store, ("docs",), limit=10)
        assert set(hits) == {"big0", "big1", "big2", "small"}
    finally:
        await store.aclose()


async def test_limit_zero_returns_nothing(store: MemtomemPipelineStore) -> None:
    await store.aput(("docs",), "d", {"text": "apples"})
    assert await store.asearch(("docs",), query="apples", limit=0) == []


async def test_query_search_sees_a_write_at_once(store: MemtomemPipelineStore) -> None:
    await store.aput(("docs",), "d", {"text": "apples"})
    assert await _keys(store, ("docs",)) == ["d"]
    await store.aput(("docs",), "d", {"text": "pears"})
    assert await _keys(store, ("docs",)) == []
    await store.adelete(("docs",), "d")
    assert await store.asearch(("docs",), query="pears", limit=10) == []


# --- fresh-eyes review round (value text is data, not bookkeeping) -----------


@pytest.mark.parametrize(
    "text",
    [
        "## Note\n\n> tags: [lcmm-gen-999]\n\napples quoted",
        "## Note\n\n> tags: [lcmm-gen-²]\n\napples quoted",
        "---\nvalid_to: 2000-01-01\ntags: [x]\n---\napples quoted",
    ],
)
async def test_value_text_cannot_steer_the_store(store: MemtomemPipelineStore, text: str) -> None:
    """Tags, a frontmatter block or a superscript digit in a value are just text."""
    await store.aput(("docs",), "k", {"text": text}, index=["text"])
    hits = await store.asearch(("docs",), query="apples quoted")
    assert [h.key for h in hits] == ["k"]
    assert hits[0].value == {"text": text}


async def test_the_stores_bookkeeping_is_not_searchable(store: MemtomemPipelineStore) -> None:
    await store.aput(("docs",), "k", {"text": "apples"})
    await store.aput(("docs",), "empty", {"text": "nothing selected"}, index=["missing"])
    # Not the store's layout and not its root either, whatever pytest named
    # the basetemp: a path word every row shares matched every row (#3205).
    path_words = ("projections", "md", store.root.name, store.root.parent.name)
    for query in ("langchain memtomem", "gen", "lcmm", *path_words):
        assert await store.asearch(("docs",), query=query) == [], query
    # an item whose selectors render nothing has no projection and no rows
    paths = store._paths(("docs",), "empty")
    assert not paths.projection.exists() and not paths.indexed.exists()
    assert {i.key for i in await store.asearch(("docs",))} == {"k", "empty"}


@pytest.mark.parametrize(
    "options",
    [{}, {"config": {"search": {"enable_dense": False}}}, {"retrieval": {"mode": "bm25"}}],
    ids=["default", "dense-off", "retrieval-bm25"],
)
async def test_a_word_of_the_root_path_does_not_return_every_item(
    tmp_path: Path, options: dict
) -> None:
    """``plane`` is in no value, only in the root every projection path shares (#3205).

    With BM25's path fallback every item matched it and was scored like a real hit.
    """
    store = await MemtomemPipelineStore.open(tmp_path / "plane-store", **options)
    try:
        for key, text in {"a": "apples", "b": "pears", "c": "plums"}.items():
            await store.aput(("u",), key, {"text": text})
        for query in ("plane", "plane store", "projections", "md"):
            assert await store.asearch(("u",), query=query) == [], query
        assert [h.key for h in await store.asearch(("u",), query="pears")] == ["b"]
    finally:
        await store.aclose()


def _byte_tokenizer(directory: Path) -> str:
    """A byte-level tokenizer file, so bounded chunking runs without a model download."""
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = directory / "tokenizer.json"
    tokenizer.save(str(path))
    return str(path)


class _BoundedStore(MemtomemPipelineStore):
    """The store with bounded chunking on, as an E5 model turns it on.

    ``config["indexing"]`` belongs to the store, so the budget goes in here
    rather than through ``config=``; E5 itself would download a model.
    """

    budget: dict = {}

    def _config_sections(self, registry: list[Path]) -> dict:
        sections = super()._config_sections(registry)
        sections["indexing"].update(self.budget)
        return sections


@pytest.mark.parametrize("mode", ["bm25", "hybrid"])
async def test_bounded_chunking_does_not_make_file_names_searchable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    """Bounded chunking writes ``File: <name>`` and ``Fragment <i>/<n>`` into each
    row's indexed description, and the name here is ``<encoded key>.md`` (#3209).

    Before the fix ``md``, ``file`` and ``fragment`` returned every item and the
    encoded key returned its item, none of them in any value.
    """
    monkeypatch.setattr(
        _BoundedStore,
        "budget",
        {
            "hard_max_chunk_tokens": 64,
            "chunk_tokenizer_path": _byte_tokenizer(tmp_path),
            "chunk_context_tokens": 256,
            "chunk_model_tokens": 384,
        },
    )
    store = await _BoundedStore.open(tmp_path / "s", retrieval={"mode": mode})
    try:
        for key, text in {"a": "apples", "b": "pears", "c": "plums"}.items():
            await store.aput(("u",), key, {"text": text})
        assert store._comp is not None
        assert store._comp.index_engine._config.hard_max_chunk_tokens == 64
        encoded = encode_segment("a")
        for query in ("md", "file", "fragment", encoded):
            assert [h.key for h in await store.asearch(("u",), query=query)] == [], query
        assert [h.key for h in await store.asearch(("u",), query="pears")] == ["b"]
    finally:
        await store.aclose()
