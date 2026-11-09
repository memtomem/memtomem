"""A Notion or Obsidian import reports the files it wrote but could not index (#3168).

The importers write every target, then index them one by one. The loop used to
read only ``stats.indexed_chunks``, so a file the engine refused was reported
under ``import complete``, and an ``EmbeddingPolicyChangedError`` raised when
another process restamped the policy escaped the loop before the files indexed
earlier were tagged or the session flagged. The engine's own ``current_files``
now says whether a file's stored chunks describe what it read: refusals,
including the ones that report no error, are listed as not indexed, and a
summary refresh that fails after the chunk write commits does not undo it.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Sequence
from pathlib import Path

import pytest

import memtomem.config as _cfg
from helpers import StubCtx, isolate_memtomem_env
from memtomem.config import Mem2MemConfig
from memtomem.errors import EmbeddingPolicyChangedError
from memtomem.indexing import engine as engine_mod
from memtomem.indexing.engine import PrivacyRejection
from memtomem.models import IndexingStats
from memtomem.server.component_factory import close_components, create_components
from memtomem.server.context import AppContext
from memtomem.server.tools import importers
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD

KINDS = ["notion", "obsidian"]
# Assembled at runtime so the literal never sits in the repository as a key.
_SECRET = "api_key=" + "AKIA" + "1234567890ABCDEF"
_GENERIC = "not indexed (unreadable, missing, or no longer a regular file)"


def _source(tmp_path: Path, kind: str, bodies: dict[str, str], tag: str = "1") -> Path:
    """An export (Notion) or vault (Obsidian) holding one page per *bodies* entry."""
    root = tmp_path / f"{kind}-source-{tag}"
    root.mkdir()
    for stem, body in bodies.items():
        uuid = stem.encode().hex().ljust(32, "0")[:32]
        name = f"{stem} {uuid}.md" if kind == "notion" else f"{stem}.md"
        (root / name).write_text(f"# {stem}\n\n{body}\n", encoding="utf-8")
    return root


def _rows(comp, name: str) -> list[tuple[str, str]]:
    return (
        comp.storage._get_db()
        .execute(
            "SELECT content, tags FROM chunks WHERE source_file LIKE ? ORDER BY id",
            (f"%/{name}",),
        )
        .fetchall()
    )


def _lines(result: str) -> list[str]:
    return result.split("\n")


_SESSION = "session-captured-at-start"


def _flag_spy(monkeypatch) -> list[object]:
    """Record which session the import flags; the capture answers ``_SESSION``."""
    calls: list[object] = []

    async def capture(app):
        return _SESSION

    async def spy(app, session_id):
        calls.append(session_id)

    monkeypatch.setattr(importers, "capture_session_for_untracked_write", capture)
    monkeypatch.setattr(importers, "flag_untracked_write", spy)
    return calls


def _tool(kind: str):
    return getattr(importers, f"mem_import_{kind}"), kind.capitalize()


@pytest.mark.parametrize("kind", KINDS)
async def test_a_refused_file_is_listed_and_the_import_is_not_complete(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    comp, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool, label = _tool(kind)
    flags = _flag_spy(monkeypatch)
    monkeypatch.setattr(engine_mod, "_MAX_INDEX_FILE_BYTES", 2048)
    src = _source(tmp_path, kind, {"Alpha": "alpha body", "Beta": "beta " * 1000})

    result = await tool(str(src), tags=["t1"], ctx=ctx)

    lines = _lines(result)
    assert lines[0] == f"{label} import incomplete: 1 of 2 written file(s) were not indexed."
    assert "- Written but not indexed: 1" in lines
    assert "  - Beta.md: file too large (0MB, max 0MB)" in lines
    alpha = _rows(comp, "Alpha.md")
    assert len(alpha) == 1
    assert f"- Chunks indexed: {len(alpha)}" in lines
    assert set(json.loads(alpha[0][1])) >= {"t1", kind, "imported"}
    assert _rows(comp, "Beta.md") == []
    assert flags == [_SESSION]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_summary_failure_after_the_commit_still_counts_tags_and_flags(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    comp, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool, label = _tool(kind)
    flags = _flag_spy(monkeypatch)

    async def failing_summary(*args, **kwargs):
        raise RuntimeError("summary model unavailable")

    monkeypatch.setattr("memtomem.indexing.summarizer.maybe_update_ai_summary", failing_summary)
    src = _source(tmp_path, kind, {"Alpha": "alpha body", "Beta": "beta body"})

    result = await tool(str(src), tags=["t2"], ctx=ctx)

    lines = _lines(result)
    assert lines[0] == f"{label} import complete:"
    assert "- Indexed, AI summary not refreshed: 2" in lines
    assert not any(line.startswith("- Written but not indexed") for line in lines)
    for name in ("Alpha.md", "Beta.md"):
        rows = _rows(comp, name)
        assert len(rows) == 1
        assert set(json.loads(rows[0][1])) >= {"t2", kind, "imported"}
    assert "- Chunks indexed: 2" in lines
    assert flags == [_SESSION]


@pytest.mark.parametrize("kind", KINDS)
async def test_an_unreadable_target_with_no_error_is_still_reported_not_indexed(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    """The engine holds an unreadable file's rows and returns no error."""
    comp, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool, label = _tool(kind)
    first = await tool(str(_source(tmp_path, kind, {"Alpha": "a one", "Beta": "b one"})), ctx=ctx)
    assert first.startswith(f"{label} import complete:"), first

    original_read = Path.read_text
    original_index = comp.index_engine.index_file
    unreadable: list[str] = []

    def read_text(self, *args, **kwargs):
        if self.name in unreadable:
            raise PermissionError(13, "Permission denied", str(self))
        return original_read(self, *args, **kwargs)

    async def index_file(path, *args, **kwargs):
        # Only the engine's read fails: the importer's own write already happened.
        if path.name == "Beta.md":
            unreadable.append(path.name)
        try:
            return await original_index(path, *args, **kwargs)
        finally:
            unreadable.clear()

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(comp.index_engine, "index_file", index_file)
    flags = _flag_spy(monkeypatch)

    result = await tool(
        str(_source(tmp_path, kind, {"Alpha": "a two", "Beta": "b two"}, tag="2")),
        tags=["second"],
        ctx=ctx,
    )

    lines = _lines(result)
    assert lines[0] == f"{label} import incomplete: 1 of 2 written file(s) were not indexed."
    assert f"  - Beta.md: {_GENERIC}" in lines
    beta = _rows(comp, "Beta.md")
    assert beta and all("b one" in content for content, _ in beta)
    assert all("second" not in json.loads(tags) for _, tags in beta)
    alpha = _rows(comp, "Alpha.md")
    assert alpha and all("a two" in content for content, _ in alpha)
    assert all("second" in json.loads(tags) for _, tags in alpha)
    assert flags == [_SESSION]


@pytest.mark.parametrize("kind", KINDS)
async def test_a_redaction_hold_committed_before_the_refusal_flags_the_session(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    """The importer's write guard refuses a secret before writing, so the secret
    arrives in the destination between the write and the engine's read."""
    comp, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool, label = _tool(kind)
    first = await tool(str(_source(tmp_path, kind, {"Alpha": "clean one"})), ctx=ctx)
    assert first.startswith(f"{label} import complete:"), first

    original_index = comp.index_engine.index_file
    raised: list[PrivacyRejection] = []

    async def index_file(path, *args, **kwargs):
        path.write_text(f"# Alpha\n\n{_SECRET}\n", encoding="utf-8")
        try:
            return await original_index(path, *args, **kwargs)
        except PrivacyRejection as exc:
            raised.append(exc)
            raise

    monkeypatch.setattr(comp.index_engine, "index_file", index_file)
    flags = _flag_spy(monkeypatch)

    result = await tool(str(_source(tmp_path, kind, {"Alpha": "clean two"}, tag="2")), ctx=ctx)

    assert [exc.held for exc in raised] == [True]
    assert "- Blocked (redaction): 1" in _lines(result)
    reasons = (
        comp.storage._get_db()
        .execute("SELECT reason FROM held_sources WHERE source_file LIKE ?", ("%/Alpha.md",))
        .fetchall()
    )
    assert reasons == [(REDACTION_BLOCKED_HOLD,)]
    assert flags == [_SESSION]


@pytest.mark.parametrize("kind", KINDS)
async def test_only_a_scope_hold_is_rendered_as_hidden(
    bm25_only_components, tmp_path, monkeypatch, kind
):
    """Classification of the engine's scope-hold shape (#2923), stubbed: the
    importer's output root is the user base, which no project tier can claim."""
    comp, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool, label = _tool(kind)
    original_index = comp.index_engine.index_file

    async def index_file(path, *args, **kwargs):
        if path.name == "Beta.md":
            return IndexingStats(
                0,
                0,
                0,
                0,
                0,
                0.0,
                errors=("Beta.md: no longer registered",),
                scope_held=True,
                mutated=True,
            )
        return await original_index(path, *args, **kwargs)

    monkeypatch.setattr(comp.index_engine, "index_file", index_file)
    flags = _flag_spy(monkeypatch)

    result = await tool(str(_source(tmp_path, kind, {"Beta": "b"})), ctx=ctx)

    lines = _lines(result)
    assert lines[0] == f"{label} import incomplete: 1 of 1 written file(s) were not indexed."
    assert "  - Beta.md: no longer registered — previous chunks hidden from search" in lines
    assert flags == [_SESSION]


# ---------------------------------------------------------------------------
# A policy restamp by another process during the import
# ---------------------------------------------------------------------------


class _DenseEmbedder:
    """Deterministic 1024-d vectors, so chunk writes reach the policy fence."""

    dimension = 1024
    model_name = "bge-m3"

    async def embed_texts(self, texts: Sequence[str], **_kwargs) -> list[list[float]]:
        return [[0.1] * 1024 for _ in texts]

    async def embed_query(self, query: str) -> list[float]:
        return [0.1] * 1024

    async def close(self) -> None:
        pass


@pytest.fixture
async def dense_components(tmp_path, monkeypatch):
    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / "memories"
    mem_dir.mkdir()
    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "dense.db"
    config.indexing.memory_dirs = [mem_dir]
    config.embedding.provider = "onnx"
    config.embedding.model = "bge-m3"
    config.embedding.dimension = 1024
    monkeypatch.setattr(_cfg, "load_config_overrides", lambda c: None)
    monkeypatch.setattr(_cfg, "load_config_d", lambda c: None)
    monkeypatch.setattr("memtomem.runtime.components.create_embedder", lambda c: _DenseEmbedder())
    comp = await create_components(config)
    try:
        yield comp
    finally:
        await close_components(comp)


@pytest.mark.parametrize("kind", KINDS)
async def test_a_policy_restamp_mid_import_finalizes_the_files_indexed_before_it(
    dense_components, tmp_path, monkeypatch, kind
):
    comp = dense_components
    ctx = StubCtx(AppContext.from_components(comp))
    tool, label = _tool(kind)
    assert comp.storage.embedding_mismatch is None
    db_path = comp.config.storage.sqlite_path

    original_index = comp.index_engine.index_file
    order: list[str] = []
    raised: list[type[BaseException]] = []

    async def index_file(path, *args, **kwargs):
        order.append(path.name)
        if len(order) == 2:
            # Another process adopts a different policy: a separate connection.
            with sqlite3.connect(db_path) as other:
                other.execute(
                    "UPDATE _memtomem_meta SET value='restamped-elsewhere' "
                    "WHERE key='embedding_policy_fingerprint'"
                )
        try:
            return await original_index(path, *args, **kwargs)
        except BaseException as exc:
            raised.append(type(exc))
            raise

    monkeypatch.setattr(comp.index_engine, "index_file", index_file)
    invalidations: list[None] = []
    original_invalidate = comp.search_pipeline.invalidate_cache

    def invalidate_cache():
        invalidations.append(None)
        return original_invalidate()

    monkeypatch.setattr(comp.search_pipeline, "invalidate_cache", invalidate_cache)
    flags = _flag_spy(monkeypatch)
    src = _source(tmp_path, kind, {"Alpha": "alpha", "Beta": "beta", "Gamma": "gamma"})

    result = await tool(str(src), tags=["t3"], ctx=ctx)

    assert len(order) == 3
    assert raised == [EmbeddingPolicyChangedError]
    lines = _lines(result)
    assert lines[0] == f"{label} import incomplete: 2 of 3 written file(s) were not indexed."
    first, later = order[0], order[1:]
    rows = _rows(comp, first)
    assert rows and all({"t3", kind, "imported"} <= set(json.loads(tags)) for _, tags in rows)
    for name in later:
        assert _rows(comp, name) == []
        assert any(line.startswith(f"  - {name}: ") for line in lines), name
    mismatch = comp.storage.embedding_mismatch
    assert mismatch is not None and mismatch["policy_mismatch"] is True
    assert comp.storage._get_db().in_transaction is False
    assert invalidations and flags == [_SESSION]


# ---------------------------------------------------------------------------
# The engine signal
# ---------------------------------------------------------------------------


async def test_current_files_marks_only_the_returns_whose_rows_describe_the_file(
    bm25_only_components, monkeypatch
):
    comp, mem_dir = bm25_only_components
    engine = comp.index_engine
    target = mem_dir / "note.md"

    target.write_text("# Note\n\nfirst body\n", encoding="utf-8")
    written = await engine.index_file(target)
    assert (written.current_files, written.indexed_chunks) == (1, 1)

    unchanged = await engine.index_file(target)
    assert (unchanged.current_files, unchanged.indexed_chunks) == (1, 0)

    async def failing_summary(*args, **kwargs):
        raise RuntimeError("summary model unavailable")

    with monkeypatch.context() as m:
        m.setattr("memtomem.indexing.summarizer.maybe_update_ai_summary", failing_summary)
        target.write_text("# Note\n\nsecond body\n", encoding="utf-8")
        summary_failed = await engine.index_file(target)
    assert summary_failed.current_files == 1
    assert summary_failed.errors == ("AI summary refresh failed: summary model unavailable",)

    target.write_text("", encoding="utf-8")
    emptied = await engine.index_file(target)
    assert (emptied.current_files, emptied.deleted_chunks) == (1, 1)

    with monkeypatch.context() as m:
        m.setattr(engine_mod, "_MAX_INDEX_FILE_BYTES", 4)
        target.write_text("# Note\n\nthird body\n", encoding="utf-8")
        too_large = await engine.index_file(target)
    assert too_large.current_files == 0 and too_large.errors

    original_read = Path.read_text

    def unreadable(self, *args, **kwargs):
        if self == target:
            raise PermissionError(13, "Permission denied", str(self))
        return original_read(self, *args, **kwargs)

    with monkeypatch.context() as m:
        m.setattr(Path, "read_text", unreadable)
        held = await engine.index_file(target)
    assert held.current_files == 0 and held.errors == ()
