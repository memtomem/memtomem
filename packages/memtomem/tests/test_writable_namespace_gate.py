"""Writes refuse a namespace the read side cannot address by exact name (#2933).

``NamespaceFilter.parse`` reads a comma as a list, a ``*`` as a glob and
``!system`` as the every-non-system-namespace selector, so rows stored under
such a name could only be reached through some other spelling. Each write
surface now passes its ``namespace=`` through ``writable_namespace_arg``
before anything is written. Every refusal test below also asserts that
nothing landed (no file, no row, no fetch, no queue entry), because a refusal
raised after the markdown append would leave an unindexed entry behind.

The gate is deliberately narrower than ``validate_namespace``: namespace rules
store names such as ``claude:-Users-...`` that the strict gate refuses, and a
write into a namespace the store already uses has to keep working.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from helpers import StubCtx, make_chunk
from httpx import ASGITransport, AsyncClient

from memtomem.constants import (
    InvalidNameError,
    validate_writable_namespace,
    writable_namespace_arg,
)
from memtomem.tools.bundle_format import BundleValidationError

REFUSED = ["a,b", "x*y", "!system", "x*y,z", " ", "\t", "agent-runtime:foo:bar"]
REFUSED_MORE = ["agent-runtime:-x", "agent-runtime", 123, ["a"]]
ACCEPTED = [
    "claude:-Users-x",
    "ns@org",
    "a b",
    "!other",
    " !system ",
    "archive:summary",
    "agent-runtime:planner",
    "Agent-Runtime:x",
    "agent/id",
]
LEGACY = "claude:-Users-pdstudio-proj"


class TestHelper:
    @pytest.mark.parametrize("value", REFUSED + REFUSED_MORE)
    def test_refused(self, value):
        with pytest.raises(InvalidNameError, match="invalid namespace"):
            validate_writable_namespace(value)

    @pytest.mark.parametrize("value", ACCEPTED)
    def test_accepted_unchanged(self, value):
        assert validate_writable_namespace(value) == value

    def test_empty_is_refused_by_the_gate(self):
        with pytest.raises(InvalidNameError, match="empty"):
            validate_writable_namespace("")

    @pytest.mark.parametrize("value", [None, ""])
    def test_arg_reads_none_and_empty_as_absent(self, value):
        assert writable_namespace_arg(value) is None

    @pytest.mark.parametrize("value", [0, False, []])
    def test_arg_refuses_other_falsy_values(self, value):
        with pytest.raises(InvalidNameError, match="expected str"):
            writable_namespace_arg(value)

    @pytest.mark.parametrize(
        "value, reading",
        [
            ("a,b", "comma-separated list"),
            ("x*y", "glob pattern"),
            ("!system", "every non-system namespace"),
        ],
    )
    def test_message_says_how_a_reader_would_read_it(self, value, reading):
        with pytest.raises(InvalidNameError, match=reading):
            validate_writable_namespace(value)


# ---------------------------------------------------------------------------
# MCP tools against a real BM25 stack
# ---------------------------------------------------------------------------


def _ctx(comp):
    from memtomem.server.context import AppContext

    return StubCtx(AppContext.from_components(comp))


async def _namespaces(comp) -> set[str]:
    return {ns for ns, _ in await comp.storage.list_namespaces()}


def _files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file())


@pytest.fixture
async def stack(bm25_only_components):
    comp, mem_dir = bm25_only_components
    return comp, mem_dir, _ctx(comp)


class TestMcpWrites:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", REFUSED)
    async def test_mem_add_refuses_and_writes_nothing(self, stack, value):
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir, ctx = stack
        out = await mem_add(content="zephyr fact", namespace=value, ctx=ctx)
        assert out.startswith("Error") and "invalid namespace" in out
        assert _files(mem_dir) == []
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    async def test_mem_batch_add_refuses_and_writes_nothing(self, stack):
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, mem_dir, ctx = stack
        out = await mem_batch_add(entries=[{"key": "k", "value": "v"}], namespace="a,b", ctx=ctx)
        assert out.startswith("Error") and "invalid namespace" in out
        assert _files(mem_dir) == []
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    async def test_mem_add_accepts_a_rule_shaped_legacy_name(self, stack):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _, ctx = stack
        out = await mem_add(content="zephyr fact", namespace=LEGACY, ctx=ctx)
        assert not out.startswith("Error"), out
        assert await _namespaces(comp) == {LEGACY}

    @pytest.mark.asyncio
    async def test_mem_add_reads_empty_as_no_namespace(self, stack):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _, ctx = stack
        out = await mem_add(content="zephyr fact", namespace="", ctx=ctx)
        assert not out.startswith("Error"), out
        assert await _namespaces(comp) == {comp.config.namespace.default_namespace}

    @pytest.mark.asyncio
    async def test_mem_procedure_save_is_gated_through_mem_add(self, stack):
        from memtomem.server.tools.procedure import mem_procedure_save

        comp, mem_dir, ctx = stack
        out = await mem_procedure_save(name="p", steps="1. go", namespace="x*y", ctx=ctx)
        assert out.startswith("Error") and "invalid namespace" in out
        assert _files(mem_dir) == []

    @pytest.mark.asyncio
    async def test_mem_fetch_refuses_before_fetching(self, stack, monkeypatch):
        import memtomem.indexing.url_fetcher as fetcher
        from memtomem.server.tools.url_index import mem_fetch

        def _no_fetch(*_a, **_k):
            raise AssertionError("fetched despite a refused namespace")

        monkeypatch.setattr(fetcher, "fetch_url_with_text", _no_fetch)
        comp, mem_dir, ctx = stack
        out = await mem_fetch(url="https://example.com/x", namespace="!system", ctx=ctx)
        assert out.startswith("Error") and "invalid namespace" in out
        assert _files(mem_dir) == []

    @pytest.mark.asyncio
    async def test_mem_index_refuses_and_indexes_nothing(self, stack):
        from memtomem.server.tools.indexing import mem_index

        comp, mem_dir, ctx = stack
        note = mem_dir / "note.md"
        note.write_text("# Note\n\nzephyr fact\n", encoding="utf-8")
        out = await mem_index(path=str(note), namespace="a,b", ctx=ctx)
        assert out.startswith("Error") and "invalid namespace" in out
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "tool, path_arg", [("mem_import_notion", "path"), ("mem_import_obsidian", "vault_path")]
    )
    async def test_importers_refuse_before_reading(self, stack, tmp_path, tool, path_arg):
        import memtomem.server.tools.importers as importers

        comp, mem_dir, ctx = stack
        kwargs = {path_arg: str(tmp_path / "does-not-exist"), "namespace": "a,b", "ctx": ctx}
        out = await getattr(importers, tool)(**kwargs)
        assert out.startswith("Error") and "invalid namespace" in out
        assert _files(mem_dir) == []


# ---------------------------------------------------------------------------
# Bundle import
# ---------------------------------------------------------------------------


def _record(chunk, *, namespace: str | None) -> dict:
    record = {
        "id": str(chunk.id),
        "content": chunk.content,
        "content_hash": chunk.content_hash,
        "source_file": str(chunk.metadata.source_file),
        "tags": list(chunk.metadata.tags),
        "chunk_type": chunk.metadata.chunk_type.value,
        "start_line": chunk.metadata.start_line,
        "end_line": chunk.metadata.end_line,
        "heading_hierarchy": list(chunk.metadata.heading_hierarchy),
        "language": chunk.metadata.language,
    }
    if namespace is not None:
        record["namespace"] = namespace
    return record


def _bundle(tmp_path: Path, records: list[dict]) -> Path:
    path = tmp_path / "bundle.json"
    path.write_text(json.dumps({"chunks": records}), encoding="utf-8")
    return path


def _spy_embed_and_upsert(monkeypatch, comp) -> list[str]:
    """Record every embedding and chunk write the import makes."""
    calls: list[str] = []
    embed, upsert = comp.embedder.embed_texts, comp.storage.upsert_chunks

    async def _embed(*a, **k):
        calls.append("embed")
        return await embed(*a, **k)

    async def _upsert(*a, **k):
        calls.append("upsert")
        return await upsert(*a, **k)

    monkeypatch.setattr(comp.embedder, "embed_texts", _embed)
    monkeypatch.setattr(comp.storage, "upsert_chunks", _upsert)
    return calls


async def _seed_hashed(comp, content: str, namespace: str):
    from memtomem.indexing.hasher import content_hash

    stored = make_chunk(content, namespace=namespace, source="s.md")
    # The real hash: the import matches existing rows by content hash.
    stored.content_hash = content_hash(stored.content)
    await comp.storage.upsert_chunks([stored])
    return stored


class TestBundleImport:
    @pytest.mark.asyncio
    async def test_one_bad_record_rejects_the_whole_bundle(
        self, bm25_only_components, tmp_path, monkeypatch
    ):
        from memtomem.tools.export_import import import_chunks

        comp, _ = bm25_only_components
        calls = _spy_embed_and_upsert(monkeypatch, comp)
        good = make_chunk("good fact one\n", source="g.md")
        bad = make_chunk("bad fact two\n", source="b.md")
        path = _bundle(tmp_path, [_record(good, namespace="work"), _record(bad, namespace="a,b")])
        with pytest.raises(BundleValidationError) as info:
            await import_chunks(comp.storage, comp.embedder, path)
        assert info.value.field == "namespace"
        assert calls == []
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    async def test_a_valid_override_imports_it(self, bm25_only_components, tmp_path):
        from memtomem.tools.export_import import import_chunks

        comp, _ = bm25_only_components
        bad = make_chunk("bad fact two\n", source="b.md")
        path = _bundle(tmp_path, [_record(bad, namespace="a,b")])
        stats = await import_chunks(comp.storage, comp.embedder, path, namespace="rescued")
        assert stats.imported_chunks == 1
        assert await _namespaces(comp) == {"rescued"}

    @pytest.mark.asyncio
    async def test_an_invalid_override_is_refused(self, bm25_only_components, tmp_path):
        from memtomem.tools.export_import import import_chunks

        comp, _ = bm25_only_components
        good = make_chunk("good fact one\n", source="g.md")
        path = _bundle(tmp_path, [_record(good, namespace="work")])
        with pytest.raises(BundleValidationError) as info:
            await import_chunks(comp.storage, comp.embedder, path, namespace="x*y")
        assert info.value.field == "namespace"
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    async def test_a_legacy_record_inheriting_a_stored_bad_name_is_refused(
        self, bm25_only_components, tmp_path, monkeypatch
    ):
        """No namespace on the record: ``on_conflict="update"`` carries the stored one over.

        The check runs on the post-carryover value, so the inherited ``a,b``
        is caught before anything is embedded or written.
        """
        from memtomem.tools.export_import import import_chunks

        comp, _ = bm25_only_components
        stored = await _seed_hashed(comp, "stored fact\n", "a,b")
        calls = _spy_embed_and_upsert(monkeypatch, comp)
        path = _bundle(tmp_path, [_record(stored, namespace=None)])
        with pytest.raises(BundleValidationError) as info:
            await import_chunks(comp.storage, comp.embedder, path, on_conflict="update")
        assert info.value.field == "namespace"
        assert calls == []

    @pytest.mark.asyncio
    async def test_control_the_same_update_under_a_valid_name_embeds_and_writes(
        self, bm25_only_components, tmp_path, monkeypatch
    ):
        """The spies see an update that is allowed, so their silence above means something."""
        from memtomem.tools.export_import import import_chunks

        comp, _ = bm25_only_components
        stored = await _seed_hashed(comp, "stored fact\n", "work")
        calls = _spy_embed_and_upsert(monkeypatch, comp)
        path = _bundle(tmp_path, [_record(stored, namespace=None)])
        stats = await import_chunks(comp.storage, comp.embedder, path, on_conflict="update")
        assert stats.updated_chunks == 1
        assert calls == ["embed", "upsert"]
        assert await _namespaces(comp) == {"work"}


# ---------------------------------------------------------------------------
# Web routes
# ---------------------------------------------------------------------------


def _web_app(comp):
    from memtomem.web.app import create_app
    from memtomem.web.deps import require_configured

    app = create_app(lifespan=None, mode="prod")
    app.state.csrf_enforce = False
    for name in ("storage", "index_engine", "search_pipeline", "config"):
        setattr(app.state, name, getattr(comp, name))
    app.state.project_root = None
    app.dependency_overrides[require_configured] = lambda: None
    return app


async def _post(app, url: str, body: dict):
    transport = ASGITransport(app=app, raise_app_exceptions=False)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post(url, json=body)


class TestWeb:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("value", ["a,b", "!system", " "])
    async def test_add_refuses_with_400_and_writes_nothing(self, stack, value):
        comp, mem_dir, _ = stack
        resp = await _post(_web_app(comp), "/api/add", {"content": "zephyr", "namespace": value})
        assert resp.status_code == 400, resp.text
        assert "invalid namespace" in resp.json()["detail"]
        assert _files(mem_dir) == []
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    @pytest.mark.parametrize("route", ["/api/index", "/api/index/stream"])
    async def test_index_refuses_with_400_before_indexing(self, stack, route):
        comp, mem_dir, _ = stack
        (mem_dir / "note.md").write_text("# Note\n\nzephyr fact\n", encoding="utf-8")
        resp = await _post(_web_app(comp), route, {"path": str(mem_dir), "namespace": "x*y"})
        assert resp.status_code == 400, resp.text
        assert "invalid namespace" in resp.json()["detail"]
        assert await _namespaces(comp) == set()

    @pytest.mark.asyncio
    async def test_index_reads_empty_as_no_namespace(self, stack):
        """Before #2933 the indexer stored ``""`` as an explicit namespace."""
        comp, mem_dir, _ = stack
        (mem_dir / "note.md").write_text("# Note\n\nzephyr fact\n", encoding="utf-8")
        resp = await _post(_web_app(comp), "/api/index", {"path": str(mem_dir), "namespace": ""})
        assert resp.status_code == 200, resp.text
        stored = await _namespaces(comp)
        assert stored and "" not in stored

    @pytest.mark.asyncio
    async def test_add_accepts_a_rule_shaped_legacy_name(self, stack):
        comp, _, _ = stack
        resp = await _post(_web_app(comp), "/api/add", {"content": "zephyr", "namespace": LEGACY})
        assert resp.status_code == 200, resp.text
        assert await _namespaces(comp) == {LEGACY}


# ---------------------------------------------------------------------------
# CLI and the LangGraph adapter
# ---------------------------------------------------------------------------


class TestCli:
    @pytest.mark.parametrize("extra", [[], ["--debounce-window", "5"]])
    def test_mm_index_refuses_before_either_branch(self, monkeypatch, tmp_path, extra):
        import memtomem.cli.indexing as cli_indexing

        def _boom(*_a, **_k):
            raise AssertionError("reached indexing despite a refused namespace")

        monkeypatch.setattr(cli_indexing, "_run_debounce", _boom)
        monkeypatch.setattr(cli_indexing, "_index", _boom)
        result = CliRunner().invoke(
            cli_indexing.index, [str(tmp_path), "--namespace", "a,b", *extra]
        )
        assert result.exit_code == 2, result.output
        assert "invalid namespace" in result.output


class TestLangGraphAdapter:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("method", ["add", "index"])
    async def test_refuses_before_initialising(self, method, monkeypatch):
        from memtomem.integrations.langgraph import MemtomemStore

        store = MemtomemStore()

        async def _no_init(*_a, **_k):
            raise AssertionError("initialised despite a refused namespace")

        monkeypatch.setattr(store, "_ensure_init", _no_init)
        kwargs = {"content": "zephyr"} if method == "add" else {}
        with pytest.raises(InvalidNameError, match="invalid namespace"):
            await getattr(store, method)(namespace="a,b", **kwargs)
