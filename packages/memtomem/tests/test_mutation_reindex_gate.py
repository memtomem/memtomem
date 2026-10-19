"""A memory-tool write must not index content that nobody let through the guard.

The write tools scan only the text they write. They used to re-index the whole
file with the redaction gate switched off, so a secret already sitting elsewhere
in the file (written by hand, by another program) was chunked, stored and
embedded on the next ``mem_add`` / ``mem_edit`` / ``mem_delete``. Now the tools
ask the engine about the file as it stands before they write, and the engine
judges every byte it stores.
"""

from __future__ import annotations

import pytest

from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import mem_add, mem_delete, mem_edit

from helpers import StubCtx

# AWS's documented example key, assembled at runtime so push protection does not
# see a literal credential in the repository.
_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
_CLEAN = "## A\n\nalpha entry about deploys\n\n## B\n\nbeta entry about reviews\n"
_SECRET_SECTION = f"\n## K\n\naws {_KEY}\n"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


async def _indexed_then_tampered(comp, mem_dir):
    """An indexed clean file that then gains a secret no tool ever scanned."""
    target = mem_dir / "notes.md"
    target.write_text(_CLEAN, encoding="utf-8")
    await comp.index_engine.index_file(target)
    with target.open("a", encoding="utf-8") as fh:
        fh.write(_SECRET_SECTION)
    return target


async def _chunk_id(comp, target, needle: str) -> str:
    chunks = await comp.storage.list_chunks_by_source(target, limit=None)
    return next(str(c.id) for c in chunks if needle in c.content)


async def _stored_text(comp, target) -> str:
    chunks = await comp.storage.list_chunks_by_source(target, limit=None)
    return "\n".join(c.content for c in chunks)


async def _assert_not_stored(comp, target):
    assert _KEY not in await _stored_text(comp, target)
    hits = await comp.storage.bm25_search("aws", top_k=10)
    assert not [h for h in hits if _KEY in h.chunk.content]


class TestToolsRefuseAFileThatHoldsUnscannedContent:
    @pytest.mark.asyncio
    async def test_mem_delete(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        before = target.read_bytes()
        chunk_id = await _chunk_id(comp, target, "alpha")

        result = await mem_delete(chunk_id=chunk_id, ctx=StubCtx(AppContext.from_components(comp)))  # type: ignore[arg-type]

        assert "refused" in result
        assert target.read_bytes() == before, "nothing may be written"
        await _assert_not_stored(comp, target)
        assert await comp.storage.is_source_held(target)

    @pytest.mark.asyncio
    async def test_mem_edit(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        before = target.read_bytes()
        chunk_id = await _chunk_id(comp, target, "alpha")

        result = await mem_edit(  # type: ignore[arg-type]
            chunk_id=chunk_id,
            new_content="alpha entry, revised",
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        assert target.read_bytes() == before
        await _assert_not_stored(comp, target)
        assert await comp.storage.is_source_held(target)

    @pytest.mark.asyncio
    async def test_mem_add(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        before = target.read_bytes()

        result = await mem_add(  # type: ignore[arg-type]
            content="gamma entry about planning",
            file=str(target),
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        assert target.read_bytes() == before, "the entry must not be appended"
        await _assert_not_stored(comp, target)
        assert await comp.storage.is_source_held(target)

    @pytest.mark.asyncio
    async def test_a_held_file_stays_held(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        from memtomem.indexing.engine import PrivacyRejection

        with pytest.raises(PrivacyRejection):
            await comp.index_engine.index_file(target)
        assert await comp.storage.is_source_held(target)
        before = target.read_bytes()

        # Id access is already withheld from a held source; a write that names
        # the file must not release the hold by re-indexing it either.
        result = await mem_add(  # type: ignore[arg-type]
            content="gamma entry about planning",
            file=str(target),
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        assert target.read_bytes() == before
        await _assert_not_stored(comp, target)
        assert await comp.storage.is_source_held(target)


class TestTheValveCoversOnlyTheCallersOwnWrite:
    @pytest.mark.asyncio
    async def test_forced_add_into_a_clean_file_is_stored(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = mem_dir / "clean.md"
        target.write_text(_CLEAN, encoding="utf-8")
        await comp.index_engine.index_file(target)

        result = await mem_add(  # type: ignore[arg-type]
            content=f"aws {_KEY}",
            file=str(target),
            force_unsafe=True,
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "Memory added" in result
        assert _KEY in await _stored_text(comp, target)

    @pytest.mark.asyncio
    async def test_unscanned_title_is_refused_after_the_append(self, bm25_only_components):
        """A post-write refusal: the title is not part of the ingress scan."""
        comp, mem_dir = bm25_only_components
        target = mem_dir / "titled.md"
        target.write_text(_CLEAN, encoding="utf-8")
        await comp.index_engine.index_file(target)

        result = await mem_add(  # type: ignore[arg-type]
            content="delta entry",
            title="password: hunter2-title-value",
            file=str(target),
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        assert "appended" in result
        assert "hunter2-title-value" not in await _stored_text(comp, target)
        assert await comp.storage.is_source_held(target)


_DECLARED = (
    "---\nredaction: documents-patterns\n---\n"
    "# Notes\n\nThe guard matches `password:` on the keyword alone.\n"
)


class TestPreflight:
    def test_a_new_file_is_clean(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        pf = comp.index_engine.preflight_mutation(mem_dir / "new.md", b"")
        assert (pf.rejection, pf.masking_locked, pf.valve_ok) == (None, False, True)

    def test_a_clean_file_lets_the_valve_through(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        pf = comp.index_engine.preflight_mutation(mem_dir / "a.md", _CLEAN.encode())
        assert (pf.rejection, pf.masking_locked, pf.valve_ok) == (None, False, True)

    def test_unscanned_content_is_refused_and_nothing_is_recorded(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        before = privacy.snapshot()

        pf = comp.index_engine.preflight_mutation(
            mem_dir / "a.md", (_CLEAN + _SECRET_SECTION).encode()
        )

        assert pf.rejection is not None and pf.rejection.decision == "blocked"
        assert pf.rejection.held is None
        assert not pf.valve_ok
        assert privacy.snapshot() == before

    def test_a_declared_label_passes_but_keeps_the_valve_off(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        pf = comp.index_engine.preflight_mutation(mem_dir / "d.md", _DECLARED.encode())
        assert pf.rejection is None
        assert pf.valve_ok is False

    def test_crlf_and_invalid_utf8_are_read_as_the_engine_reads_them(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        crlf = _CLEAN.replace("\n", "\r\n").encode()
        assert comp.index_engine.preflight_mutation(mem_dir / "c.md", crlf).valve_ok
        broken = b"## A\n\nbad byte \xff here\n" + _SECRET_SECTION.encode()
        assert comp.index_engine.preflight_mutation(mem_dir / "b.md", broken).rejection

    @pytest.mark.asyncio
    async def test_a_reviewed_masking_manifest_locks_the_file(self, bm25_only_components, tmp_path):
        import hashlib
        import json

        comp, mem_dir = bm25_only_components
        target = mem_dir / "masked.md"
        text = "# Document\n\npassword: first\n  continued-value\n\nPublic text.\n"
        target.write_text(text, encoding="utf-8")
        manifest = tmp_path / "mask.json"
        manifest.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sources": {
                        str(target.resolve()): {
                            "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                            "spans": [[3, 4]],
                        }
                    },
                }
            )
        )
        comp.index_engine._config.index_masking_manifest_path = str(manifest)

        pf = comp.index_engine.preflight_mutation(target, text.encode())
        assert pf.rejection is None
        assert pf.masking_locked
        result = await mem_add(  # type: ignore[arg-type]
            content="another public line",
            file=str(target),
            ctx=StubCtx(AppContext.from_components(comp)),
        )
        assert "reviewed masking manifest" in result
        assert target.read_text(encoding="utf-8") == text


class TestRefuseMutation:
    @pytest.mark.asyncio
    async def test_records_once_and_holds(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        pf = comp.index_engine.preflight_mutation(target, target.read_bytes())

        rejection = await comp.index_engine.refuse_mutation(target, pf)

        assert rejection.held is True
        assert await comp.storage.is_source_held(target)
        assert privacy.snapshot()["by_tool"]["index"]["blocked"] == 1

    @pytest.mark.asyncio
    async def test_emits_the_guards_own_audit_line(self, bm25_only_components, caplog):
        import logging

        comp, mem_dir = bm25_only_components
        target = mem_dir / "declared.md"
        target.write_text(_DECLARED + f"\naws {_KEY}\n", encoding="utf-8")
        pf = comp.index_engine.preflight_mutation(target, target.read_bytes())
        assert pf.rejection is not None

        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            await comp.index_engine.refuse_mutation(target, pf)

        refused = [
            r
            for r in caplog.records
            if "exemption_refused=" in r.getMessage() and "uncovered_pattern" in r.getMessage()
        ]
        assert len(refused) == 1


class TestValveBinding:
    @pytest.mark.asyncio
    async def test_a_second_forced_add_meets_the_first_secret(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = mem_dir / "forced.md"
        ctx = StubCtx(AppContext.from_components(comp))
        first = await mem_add(content=f"aws {_KEY}", file=str(target), force_unsafe=True, ctx=ctx)  # type: ignore[arg-type]
        assert "Memory added" in first
        before = target.read_bytes()

        second = await mem_add(content="hf note", file=str(target), force_unsafe=True, ctx=ctx)  # type: ignore[arg-type]

        assert "refused" in second
        assert target.read_bytes() == before
        assert await comp.storage.is_source_held(target)

    @pytest.mark.asyncio
    async def test_a_forced_add_into_a_declared_file_is_refused(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = mem_dir / "declared.md"
        target.write_text(_DECLARED, encoding="utf-8")
        await comp.index_engine.index_file(target)
        before = target.read_bytes()

        result = await mem_add(  # type: ignore[arg-type]
            content=f"aws {_KEY}",
            file=str(target),
            force_unsafe=True,
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "force_unsafe covers only" in result
        assert target.read_bytes() == before
        assert _KEY not in await _stored_text(comp, target)

    @pytest.mark.asyncio
    async def test_valve_text_must_match_the_file(self, bm25_only_components):
        from memtomem.indexing.engine import PrivacyRejection

        comp, mem_dir = bm25_only_components
        target = mem_dir / "replaced.md"
        guarded = "# Page\n\nfetched body\n"
        target.write_text(guarded + f"\naws {_KEY}\n", encoding="utf-8")

        with pytest.raises(PrivacyRejection):
            await comp.index_engine.index_file(
                target, ingress_guarded=True, force_unsafe=True, valve_text=guarded
            )

        # Bytes, not write_text: text mode on Windows would turn each "\r\n"
        # into "\r\r\n", which reads back as two newlines.
        crlf = guarded.replace("\n", "\r\n") + f"aws {_KEY}\r\n"
        target.write_bytes(crlf.encode("utf-8"))
        stats = await comp.index_engine.index_file(
            target, ingress_guarded=True, force_unsafe=True, valve_text=crlf
        )
        assert stats.indexed_chunks > 0

    @pytest.mark.asyncio
    async def test_force_unsafe_index_releases_a_preflight_hold(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        result = await mem_add(  # type: ignore[arg-type]
            content="gamma entry",
            file=str(target),
            ctx=StubCtx(AppContext.from_components(comp)),
        )
        assert "refused" in result and await comp.storage.is_source_held(target)

        await comp.index_engine.index_file(target, force_unsafe=True)

        assert not await comp.storage.is_source_held(target)


class TestBatchAdd:
    @pytest.mark.asyncio
    async def test_refused_like_mem_add(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, mem_dir = bm25_only_components
        target = await _indexed_then_tampered(comp, mem_dir)
        before = target.read_bytes()

        result = await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": "k1", "value": "first note"}, {"key": "k2", "value": "second"}],
            file=str(target),
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        assert target.read_bytes() == before
        await _assert_not_stored(comp, target)


class TestRefusalAfterTheWriteReportsTheRestore:
    """The outcome a refused edit reports is what the restore actually did."""

    @pytest.mark.parametrize(
        ("restore", "outcome", "written"),
        [
            ("restored", "rolled_back", False),
            ("failed", "not_rolled_back", True),
            ("source_removed", "superseded", False),
            ("source_replaced", "superseded", False),
        ],
    )
    @pytest.mark.asyncio
    async def test_web_helper(self, tmp_path, monkeypatch, restore, outcome, written):
        from unittest.mock import AsyncMock, MagicMock

        from helpers import clean_preflight
        from memtomem.indexing.engine import PrivacyRejection
        from memtomem.tools import memory_mutation
        from memtomem.tools.memory_writer import RestoreOutcome
        from memtomem.web.routes._errors import privacy_index_blocked_http

        src = tmp_path / "n.md"
        src.write_text("orig\n", encoding="utf-8")
        refusal = PrivacyRejection(path=src, hit_count=1, scope="user", decision="blocked")
        engine = AsyncMock()
        engine.is_excluded = MagicMock(return_value=False)
        engine.is_read_only_source = MagicMock(return_value=False)
        engine.preflight_mutation = MagicMock(side_effect=clean_preflight)
        engine.index_file = AsyncMock(side_effect=refusal)
        monkeypatch.setattr(
            memory_mutation, "restore_pre_image_quietly", lambda *_: RestoreOutcome[restore]
        )

        with pytest.raises(PrivacyRejection) as ei:
            await memory_mutation.mutate_source_and_reindex(
                engine, src, lambda _pre: src.write_text("mutated\n", encoding="utf-8")
            )

        assert ei.value.mutation_outcome == outcome
        # No rollback re-index: it would lift the hold the refusal placed.
        assert engine.index_file.await_count == 1
        detail = privacy_index_blocked_http(ei.value, "edit").detail
        assert detail["written"] is written
        assert detail["file"] == "n.md"


class TestAppendedRefusalsSpeakAtEveryPublicTool:
    @pytest.mark.asyncio
    async def test_reflection_save_names_the_unindexed_append(self, bm25_only_components):
        from memtomem.server.tools.reflection import mem_reflect_save

        comp, _ = bm25_only_components
        result = await mem_reflect_save(  # type: ignore[arg-type]
            insight="deploys go smoother on tuesdays",
            tags=["password: tag-value-xyz"],
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        assert "appended" in result
        assert "internal error" not in result


_OTHER_SECRET = "hf" + "_OTHERfake0123456789OTHERfake01234"


class TestAForcedWriteCoversOnlyItsOwnBytes:
    """A program outside memtomem writes into the file between preflight and index."""

    @pytest.mark.asyncio
    async def test_append(self, bm25_only_components, monkeypatch):
        import memtomem.tools.memory_writer as memory_writer
        from memtomem.tools.memory_writer import append_entry as real_append

        comp, mem_dir = bm25_only_components
        target = mem_dir / "clean.md"
        target.write_text(_CLEAN, encoding="utf-8")
        await comp.index_engine.index_file(target)

        def append_then_intruder(path, *args, **kwargs):
            appended = real_append(path, *args, **kwargs)
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n## Intruder\n\ntoken {_OTHER_SECRET}\n")
            return appended

        monkeypatch.setattr(memory_writer, "append_entry", append_then_intruder)
        result = await mem_add(  # type: ignore[arg-type]
            content=f"aws {_KEY}",
            file=str(target),
            force_unsafe=True,
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result and "appended" in result
        stored = await _stored_text(comp, target)
        assert _OTHER_SECRET not in stored
        assert _KEY not in stored

    @pytest.mark.asyncio
    async def test_rewrite(self, bm25_only_components, monkeypatch):
        import memtomem.tools.memory_writer as memory_writer
        from memtomem.tools.memory_writer import replace_chunk_body as real_replace

        comp, mem_dir = bm25_only_components
        target = mem_dir / "clean.md"
        target.write_text(_CLEAN, encoding="utf-8")
        await comp.index_engine.index_file(target)
        chunk_id = await _chunk_id(comp, target, "alpha")

        def intruder_then_replace(path, *args, **kwargs):
            with open(path, "a", encoding="utf-8") as fh:
                fh.write(f"\n## Intruder\n\ntoken {_OTHER_SECRET}\n")
            return real_replace(path, *args, **kwargs)

        monkeypatch.setattr(memory_writer, "replace_chunk_body", intruder_then_replace)
        result = await mem_edit(  # type: ignore[arg-type]
            chunk_id=chunk_id,
            new_content=f"alpha now holds aws {_KEY}",
            force_unsafe=True,
            ctx=StubCtx(AppContext.from_components(comp)),
        )

        assert "refused" in result
        stored = await _stored_text(comp, target)
        assert _OTHER_SECRET not in stored
        assert _KEY not in stored

    @pytest.mark.asyncio
    async def test_engine_drops_an_unbound_valve(self, bm25_only_components):
        from memtomem.indexing.engine import PrivacyRejection

        comp, mem_dir = bm25_only_components
        target = mem_dir / "unbound.md"
        target.write_text(f"## A\n\naws {_KEY}\n", encoding="utf-8")

        with pytest.raises(PrivacyRejection):
            await comp.index_engine.index_file(target, ingress_guarded=True, force_unsafe=True)

    def test_appended_text_matches_the_engines_read(self, tmp_path):
        from memtomem.tools.memory_mutation import appended_valve_text
        from memtomem.tools.memory_writer import append_entry

        target = tmp_path / "n.md"
        target.write_bytes(b"# Notes\r\n\r\nfirst line\r\n")
        pre = target.read_bytes()
        appended = append_entry(target, "second entry", title="T")

        assert appended_valve_text(pre, appended) == target.read_text(encoding="utf-8")

    def test_a_rewrite_that_read_something_else_cannot_be_bound(self):
        from memtomem.tools.memory_mutation import rewrite_valve_text
        from memtomem.tools.memory_writer import Rewrite

        judged = b"## A\n\nalpha\n"
        assert rewrite_valve_text(judged, Rewrite("## A\n\nalpha\n", "x")) == "x"
        assert rewrite_valve_text(judged, Rewrite("## A\n\nalpha\nmore\n", "x")) is None
        assert rewrite_valve_text(judged, None) is None


class TestScratchPromotionIsNotRepeatable:
    @pytest.mark.asyncio
    async def test_mcp(self, bm25_only_components):
        from memtomem.server.tools.scratch import mem_scratch_promote

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        await comp.storage.scratch_set("note", "scratch-body-marker")

        first = await mem_scratch_promote(  # type: ignore[arg-type]
            key="note", title="password: hunter2-title-value", ctx=ctx
        )
        assert "appended" in first
        retried = await mem_scratch_promote(key="note", title="clean", ctx=ctx)  # type: ignore[arg-type]

        assert "already promoted" in retried
        on_disk = sum(p.read_text().count("scratch-body-marker") for p in mem_dir.glob("*.md"))
        assert on_disk == 1

    @pytest.mark.asyncio
    async def test_mcp_marks_only_the_value_it_wrote(self, bm25_only_components, monkeypatch):
        import memtomem.server.tools.memory_crud as memory_crud
        from memtomem.server.tools.scratch import mem_scratch_promote

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        await comp.storage.scratch_set("note", "original-value-marker")
        real_core = memory_crud._mem_add_core

        async def add_then_replace(*args, **kwargs):
            result = await real_core(*args, **kwargs)
            await comp.storage.scratch_set("note", "replacement-value-marker")
            return result

        monkeypatch.setattr(memory_crud, "_mem_add_core", add_then_replace)
        await mem_scratch_promote(key="note", ctx=ctx)  # type: ignore[arg-type]

        entry = await comp.storage.scratch_get("note")
        assert entry["value"] == "replacement-value-marker"
        assert entry["promoted"] is False, "the replacement was never written"

    @pytest.mark.asyncio
    async def test_storage_marks_only_the_expected_value(self, bm25_only_components):
        comp, _ = bm25_only_components
        await comp.storage.scratch_set("note", "new")

        assert not await comp.storage.scratch_promote("note", expected_value="old")
        assert (await comp.storage.scratch_get("note"))["promoted"] is False
        assert await comp.storage.scratch_promote("note", expected_value="new")
        assert (await comp.storage.scratch_get("note"))["promoted"] is True
