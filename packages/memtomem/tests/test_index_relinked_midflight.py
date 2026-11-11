"""A path relinked while ``index_file`` awaits never files one source's content under another (#3180).

``_index_file`` reads the file, then awaits (namespace resolution, the writer
waits, enrichment, the embedding, the AI summary) before and after it writes.
The writes resolve the path again, so a path swapped for a symlink to another
indexed source during one of those awaits used to file the content read from
the first source under the second one's key, and release or place holds on the
second one. The pass now keeps the key it read and refuses, or rolls back,
anything that would land elsewhere.

Each test blocks one await, swaps ``a.md`` for a link to the indexed ``b.md``
while it is blocked, then lets it continue.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest import mock
from unittest.mock import AsyncMock

import pytest

import memtomem.indexing.summarizer as summarizer_mod
from memtomem.errors import RetryableError
from memtomem.indexing.engine import PrivacyRejection, SourceRelinkedError
from memtomem.indexing.watcher import FileWatcher
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD, RELINKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path

pytestmark = pytest.mark.requires_symlinks

_A = "# A\n\n## Plans\n\nalpha falcon body\n"
_A_EDITED = "# A\n\n## Plans\n\nalpha falcon edited\n"
_B = "# B\n\n## Plans\n\nbeta heron body\n"
# Assembled at runtime so the literal never sits in the tree as a key.
_SECRET = "AKIA" + "IOSFODNN7EXAMPLE"


def _vectors(texts, **_):
    return [[0.1] * 1024 for _ in texts]


@pytest.fixture
def engine(components):
    """A real store with a stub embedder of positive dimension (receipts need vectors)."""
    embedder = AsyncMock()
    embedder.embed_texts = AsyncMock(side_effect=_vectors)
    embedder.dimension = 1024
    components.index_engine._embedder = embedder
    return components.index_engine


class _Gate:
    """Blocks the await it wraps until the test has relinked the path."""

    def __init__(self) -> None:
        self.entered, self.release = asyncio.Event(), asyncio.Event()

    async def wait(self) -> None:
        self.entered.set()
        await asyncio.wait_for(self.release.wait(), 10)


async def _relinked_during(gate: _Gate, call, path: Path, target: Path):
    """Run ``call``; while ``gate`` blocks it, replace ``path`` by a link to ``target``."""
    task = asyncio.ensure_future(call)
    entered = asyncio.ensure_future(gate.entered.wait())
    await asyncio.wait({task, entered}, timeout=10, return_when=asyncio.FIRST_COMPLETED)
    entered.cancel()
    assert gate.entered.is_set(), "the gated await was never reached"
    path.unlink()
    path.symlink_to(target)
    gate.release.set()
    return await asyncio.wait_for(task, 10)


def _contents_under(storage, key: str) -> list[str]:
    return [
        row[0]
        for row in storage._get_db()
        .execute("SELECT content FROM chunks WHERE source_file=? ORDER BY content", (key,))
        .fetchall()
    ]


def _reason_under(storage, key: str) -> str | None:
    row = (
        storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file=?", (key,))
        .fetchone()
    )
    return row[0] if row else None


async def _two_sources(engine, memory_dir: Path, b_text: str = _B) -> tuple[Path, Path]:
    a, b = memory_dir / "a.md", memory_dir / "b.md"
    a.write_text(_A, encoding="utf-8")
    b.write_text(b_text, encoding="utf-8")
    for path in (a, b):
        stats = await engine.index_file(path)
        assert not stats.errors, stats.errors
        assert stats.indexed_chunks > 0
    return a, b


def _gate_embedding(engine) -> _Gate:
    gate = _Gate()

    async def blocking(texts, **_):
        await gate.wait()
        return _vectors(texts)

    engine._embedder.embed_texts = AsyncMock(side_effect=blocking)
    return gate


# ---- the chunk write ---------------------------------------------------------


@pytest.mark.parametrize("force", [True, False], ids=["force", "plain"])
async def test_relinked_during_the_embedding_writes_nothing(components, engine, memory_dir, force):
    """The measured failure: a.md's edit landed under b.md's key and a.md's rows were deleted.

    The plain pass re-reads the file in its transaction to record a receipt;
    the target's bytes differ, and the pass must still say it was relinked.
    """
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    a_before = _contents_under(components.storage, a_key)
    b_before = _contents_under(components.storage, b_key)
    a.write_text(_A_EDITED, encoding="utf-8")
    gate = _gate_embedding(engine)

    with pytest.raises(SourceRelinkedError):
        await _relinked_during(gate, engine.index_file(a, force=force), a, b)

    assert _contents_under(components.storage, b_key) == b_before
    assert _contents_under(components.storage, a_key) == a_before


async def test_relinked_during_namespace_resolution_writes_nothing(components, engine, memory_dir):
    """The key used to be taken after this await, so every check compared against the target."""
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    a_before = _contents_under(components.storage, a_key)
    b_before = _contents_under(components.storage, b_key)
    a.write_text(_A_EDITED, encoding="utf-8")
    gate = _Gate()
    resolve = engine._namespace_decision

    async def blocking(*args, **kwargs):
        await gate.wait()
        return await resolve(*args, **kwargs)

    with mock.patch.object(engine, "_namespace_decision", side_effect=blocking):
        with pytest.raises(SourceRelinkedError):
            await _relinked_during(gate, engine.index_file(a, force=True), a, b)

    assert _contents_under(components.storage, b_key) == b_before
    assert _contents_under(components.storage, a_key) == a_before


async def test_a_caller_that_wrote_the_file_holds_it_instead(components, engine, memory_dir):
    """``hold_if_relinked`` callers get the hold they get when the swap precedes the pass."""
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    a_before = _contents_under(components.storage, a_key)
    b_before = _contents_under(components.storage, b_key)
    a.write_text(_A_EDITED, encoding="utf-8")
    gate = _gate_embedding(engine)

    stats = await _relinked_during(gate, engine.index_file(a, hold_if_relinked=True), a, b)

    assert stats.relinked_files == 1
    assert not stats.errors
    assert _reason_under(components.storage, a_key) == RELINKED_HOLD
    assert _contents_under(components.storage, a_key) == a_before
    assert _contents_under(components.storage, b_key) == b_before
    assert _reason_under(components.storage, b_key) is None


async def test_a_relink_reads_as_one_ahead_of_the_linked_chunk_guard(
    components, engine, memory_dir
):
    """The edit would remove a linked chunk, which the transaction refuses with its own error.

    A relink during the embedding is reported first, so the caller gets its relink hold.
    """
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    db = components.storage._get_db()
    (a_id,) = db.execute("SELECT id FROM chunks WHERE source_file=?", (a_key,)).fetchone()
    (b_id,) = db.execute("SELECT id FROM chunks WHERE source_file=?", (b_key,)).fetchone()
    db.execute(
        "INSERT INTO chunk_links(source_id, target_id, namespace_target, created_at) "
        "VALUES (?, ?, 'default', '2026-11-09T00:00:00+00:00')",
        (a_id, b_id),
    )
    db.commit()
    a.write_text(_A_EDITED, encoding="utf-8")
    gate = _gate_embedding(engine)

    stats = await _relinked_during(gate, engine.index_file(a, hold_if_relinked=True), a, b)

    assert stats.relinked_files == 1
    assert _reason_under(components.storage, a_key) == RELINKED_HOLD


async def test_a_link_when_read_is_still_indexed_through(components, engine, memory_dir):
    """Only a change between the read and the write is refused."""
    a, b = await _two_sources(engine, memory_dir)
    a.unlink()
    a.symlink_to(b)
    b.write_text(_B.replace("body", "edited"), encoding="utf-8")

    stats = await engine.index_file(a)

    assert not stats.errors
    assert any("edited" in t for t in _contents_under(components.storage, norm_path(b)))


# ---- the receipt hit ---------------------------------------------------------


async def test_relinked_during_receipt_validation_is_not_a_hit(components, engine, memory_dir):
    """With equal bytes the content check passes; identity is what tells the two apart."""
    a, b = await _two_sources(engine, memory_dir, b_text=_A)
    gate = _Gate()
    unchanged = engine._source_unchanged

    async def blocking(*args, **kwargs):
        await gate.wait()
        return await unchanged(*args, **kwargs)

    with mock.patch.object(engine, "_source_unchanged", side_effect=blocking):
        with pytest.raises(SourceRelinkedError):
            await _relinked_during(gate, engine.index_file(a), a, b)


# ---- enrichment ----------------------------------------------------------------


def _byte_tokenizer(tmp_path: Path) -> str:
    """A tiny byte-level tokenizer file, as ``test_bounded_chunks`` builds one."""
    tokenizers = pytest.importorskip("tokenizers")
    alphabet = sorted(tokenizers.pre_tokenizers.ByteLevel.alphabet())
    tokenizer = tokenizers.Tokenizer(
        tokenizers.models.BPE(vocab={c: i for i, c in enumerate(alphabet)}, merges=[])
    )
    tokenizer.pre_tokenizer = tokenizers.pre_tokenizers.ByteLevel(add_prefix_space=False)
    path = tmp_path / "tokenizer.json"
    tokenizer.save(str(path))
    return str(path)


async def test_relinked_during_enrichment_caches_nothing_under_the_target(
    components, engine, memory_dir, tmp_path
):
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    b_before = _contents_under(components.storage, b_key)
    gate = _Gate()

    class _LLM:
        async def generate(self, prompt: str, **_kwargs) -> str:
            await gate.wait()
            return "Describes the falcon plans."

    engine._llm = _LLM()
    engine._config.chunk_tokenizer_path = _byte_tokenizer(tmp_path)
    engine._config.hard_max_chunk_tokens = 64
    engine._config.chunk_context_tokens = 256
    engine._config.chunk_model_tokens = 384
    engine._config.enrich_chunk_context = True
    a.write_text(_A_EDITED, encoding="utf-8")

    with pytest.raises(SourceRelinkedError):
        await _relinked_during(gate, engine.index_file(a), a, b)

    assert await components.storage.get_chunk_descriptions(Path(b_key)) == {}
    assert _contents_under(components.storage, b_key) == b_before
    assert not any("edited" in t for t in _contents_under(components.storage, a_key))


# ---- holds -------------------------------------------------------------------


async def _hold_both(storage, *keys: str) -> None:
    async with storage.transaction():
        for key in keys:
            await storage.hold_source(Path(key), "watch_reindex_unresolved", as_stored=True)


def _gate_summary(monkeypatch) -> _Gate:
    gate = _Gate()

    async def blocking(*_args, **_kwargs) -> None:
        await gate.wait()

    monkeypatch.setattr(summarizer_mod, "maybe_update_ai_summary", blocking)
    return gate


async def test_relinked_during_the_summary_releases_only_the_source_it_indexed(
    components, engine, memory_dir, monkeypatch
):
    """The chunk write committed under a.md's key; the release comes after the summary await.

    Released by the path, it reached b.md's hold, which no pass validated (#3180).
    """
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    await _hold_both(components.storage, a_key, b_key)
    a.write_text(_A_EDITED, encoding="utf-8")
    gate = _gate_summary(monkeypatch)

    stats = await _relinked_during(gate, engine.index_file(a), a, b)

    assert not stats.errors
    assert any("edited" in t for t in _contents_under(components.storage, a_key))
    assert _reason_under(components.storage, a_key) is None
    assert _reason_under(components.storage, b_key) is not None


async def test_a_refused_source_relinked_before_its_hold_is_the_one_held(
    components, engine, memory_dir
):
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    a.write_text(_A + f"\naws key {_SECRET}\n", encoding="utf-8")
    hold = engine._hold_waiting

    async def relink_first(path, reason, *, key=None):
        if reason == REDACTION_BLOCKED_HOLD:
            await asyncio.sleep(0)
            path.unlink()
            path.symlink_to(b)
        return await hold(path, reason, key=key)

    with mock.patch.object(engine, "_hold_waiting", side_effect=relink_first):
        with pytest.raises(PrivacyRejection) as caught:
            await engine.index_file(a)

    assert caught.value.held is True
    assert _reason_under(components.storage, a_key) == REDACTION_BLOCKED_HOLD
    assert _reason_under(components.storage, b_key) is None


# ---- the watcher -------------------------------------------------------------


async def test_the_watcher_retries_a_path_relinked_mid_index(components, memory_dir):
    path = memory_dir / "a.md"
    path.write_text(_A, encoding="utf-8")
    watcher = FileWatcher(
        index_engine=components.index_engine,
        config=components.config.indexing,
        debounce_ms=100,
    )
    with mock.patch.object(
        components.index_engine,
        "index_file",
        side_effect=SourceRelinkedError("Source path now names another file than the one read"),
    ):
        assert await watcher._reindex(path) == path


def test_the_relink_error_is_retryable():
    assert issubclass(SourceRelinkedError, RetryableError)


async def test_a_pass_that_read_another_source_releases_no_hold(
    components, engine, memory_dir, monkeypatch
):
    """Swapped before the read and restored before the release, the path names a.md again.

    The pass read and indexed b.md through the link (forced, so b.md is written rather
    than taken from its receipt), so a.md's rows were never validated
    and its hold stays, though the path presents a.md by the time the release runs.
    """
    a, b = await _two_sources(engine, memory_dir)
    a_key = norm_path(a)
    await _hold_both(components.storage, a_key)
    a.write_text(_A_EDITED, encoding="utf-8")
    snapshot_gate, summary_gate = _Gate(), _gate_summary(monkeypatch)
    snapshot = components.storage.source_hold_snapshot

    async def blocking_snapshot(*args, **kwargs):
        generation = await snapshot(*args, **kwargs)
        await snapshot_gate.wait()
        return generation

    monkeypatch.setattr(components.storage, "source_hold_snapshot", blocking_snapshot)
    saved = a.read_text(encoding="utf-8")
    task = asyncio.ensure_future(
        _relinked_during(snapshot_gate, engine.index_file(a, force=True), a, b)
    )
    await asyncio.wait_for(summary_gate.entered.wait(), 10)
    a.unlink()
    a.write_text(saved, encoding="utf-8")
    summary_gate.release.set()
    stats = await asyncio.wait_for(task, 10)

    assert not stats.errors
    assert _reason_under(components.storage, a_key) is not None


async def test_a_hold_snapshot_by_stored_key_does_not_follow_a_link(components, engine, memory_dir):
    """The pass snapshots the key it will release; resolved again, it read the target's."""
    a, b = await _two_sources(engine, memory_dir)
    a_key, b_key = norm_path(a), norm_path(b)
    storage = components.storage
    await _hold_both(storage, a_key, b_key)
    # Released and held again, b's generation moves past a's.
    await storage.release_source_hold(Path(b_key), as_stored=True)
    await _hold_both(storage, b_key)
    a_generation = await storage.source_hold_snapshot(Path(a_key), as_stored=True)
    assert a_generation != await storage.source_hold_snapshot(Path(b_key), as_stored=True)
    a.unlink()
    a.symlink_to(b)

    assert await storage.source_hold_snapshot(Path(a_key), as_stored=True) == a_generation
