"""Maintenance, entity and summary reads screen a held source themselves (#2783).

Dedup, consolidation discovery, export, the consolidation policy, the entity
scan and summary regeneration each listed sources and then read every
source's chunks. The hold used to be checked in a separate call beside that
read, so a hold landing between the two let held content through. Each read
now judges the hold in its own statement and matches the stored path as
stored. Here the hold is committed just before that read and released just
after it, so a check made before or after the read sees no hold and only the
read itself can keep the held rows out.

The summary key tests cover the other half of #2783: a summary is filed under
the key taken before the LLM call, not one resolved again afterwards.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path

pytestmark = pytest.mark.asyncio


async def _index(comp, path: Path, body: str):
    path.write_text(body, encoding="utf-8")
    assert not (await comp.index_engine.index_file(path)).errors
    return await comp.storage.list_chunks_by_source(path.resolve(), limit=None)


def _hold_during_read(monkeypatch, comp, path: Path, reason: str = "scan_missing"):
    """Hold ``path`` for exactly the duration of each read of its chunks."""
    real = comp.storage.list_chunks_by_source
    key = norm_path(path)

    async def held_while_read(source, *args, **kwargs):
        if str(source) != key:
            return await real(source, *args, **kwargs)
        await comp.storage.hold_source(path, reason)
        try:
            return await real(source, *args, **kwargs)
        finally:
            db = comp.storage._get_db()
            db.execute("DELETE FROM held_sources WHERE source_file=?", (key,))
            db.commit()

    monkeypatch.setattr(comp.storage, "list_chunks_by_source", held_while_read)


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


class TestDiscovery:
    async def test_dedup_scan_reads_no_held_rows(self, bm25_only_components, monkeypatch):
        from memtomem.search.dedup import DedupScanner

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        await _index(comp, held, "# D\n\nduplicateword shared body.\n")
        await _index(comp, mem_dir / "visible.md", "# D\n\nduplicateword shared body.\n")
        _hold_during_read(monkeypatch, comp, held, REDACTION_BLOCKED_HOLD)

        chunks = await DedupScanner(comp.storage)._get_all_chunks(100)

        assert {str(c.metadata.source_file) for c in chunks} == {norm_path(mem_dir / "visible.md")}

    async def test_consolidation_candidates_preview_no_held_rows(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.server.tools.consolidation import mem_consolidate

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        body = "".join(f"## S{i}\n\nheldpreviewword part {i}.\n\n" for i in range(4))
        await _index(comp, held, body)
        _hold_during_read(monkeypatch, comp, held)

        report = await mem_consolidate(
            min_group_size=2, ctx=StubCtx(AppContext.from_components(comp))
        )

        assert "heldpreviewword" not in report
        assert "No consolidation candidates found" in report

    async def test_policy_consolidation_reads_no_held_group(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.tools.policy_engine import execute_auto_consolidate

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        await _index(comp, held, "".join(f"## S{i}\n\nheldgroupword {i}.\n\n" for i in range(4)))
        _hold_during_read(monkeypatch, comp, held)

        result = await execute_auto_consolidate(
            comp.storage, {"min_group_size": 2}, None, dry_run=True
        )

        assert result.affected_count == 0

    async def test_entity_scan_sends_no_held_text(self, bm25_only_components, monkeypatch):
        from memtomem.server.tools.entity import mem_entity_scan
        from memtomem.tools import entity_extraction

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        await _index(comp, held, "# E\n\nheldentityword met Alice.\n")
        await _index(comp, mem_dir / "visible.md", "# E\n\nvisibleentityword met Bob.\n")
        _hold_during_read(monkeypatch, comp, held, REDACTION_BLOCKED_HOLD)
        seen: list[str] = []

        async def record(text, *args, **kwargs):
            seen.append(text)
            return []

        monkeypatch.setattr(entity_extraction, "extract_entities_with_llm", record)

        await mem_entity_scan(ctx=StubCtx(AppContext.from_components(comp)))

        assert any("visibleentityword" in text for text in seen)
        assert not any("heldentityword" in text for text in seen)


class TestExport:
    async def test_a_hold_during_the_read_keeps_rows_out(self, bm25_only_components, monkeypatch):
        from contextlib import asynccontextmanager

        from memtomem.tools.export_import import export_chunks

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        await _index(comp, held, "# X\n\nheldexportword.\n")
        await _index(comp, mem_dir / "visible.md", "# X\n\nvisibleexportword.\n")
        # Export now reads all sources in one leased snapshot. Commit the hold
        # just before that read, and release it only after the snapshot closes.
        real = comp.storage._lease_txn_read_db

        @asynccontextmanager
        async def held_snapshot():
            await comp.storage.hold_source(held, REDACTION_BLOCKED_HOLD)
            try:
                async with real() as db:
                    yield db
            finally:
                writer = comp.storage._get_db()
                writer.execute("DELETE FROM held_sources WHERE source_file=?", (norm_path(held),))
                writer.commit()

        monkeypatch.setattr(comp.storage, "_lease_txn_read_db", held_snapshot)

        bundle = await export_chunks(comp.storage, stamp_provenance=False)

        assert any("visibleexportword" in c["content"] for c in bundle.chunks)
        assert not any("heldexportword" in c["content"] for c in bundle.chunks)

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege")
    async def test_a_symlink_at_a_held_path_does_not_export_its_target_twice(
        self, bm25_only_components
    ):
        from memtomem.tools.export_import import export_chunks

        comp, mem_dir = bm25_only_components
        held, visible = mem_dir / "held.md", mem_dir / "visible.md"
        await _index(comp, held, "# X\n\nheldexportword.\n")
        await _index(comp, visible, "# X\n\nvisibleexportword.\n")
        await comp.storage.hold_source(held, "scan_missing")
        held.unlink()
        _symlink_or_skip(held, visible)

        bundle = await export_chunks(comp.storage, stamp_provenance=False)

        assert [c["content"].count("visibleexportword") for c in bundle.chunks] == [1]
        assert bundle.omitted_held_sources == 1

    async def test_a_hidden_source_matching_no_filter_is_not_counted(self, bm25_only_components):
        from memtomem.tools.export_import import export_chunks

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        await _index(comp, held, "# X\n\nheldexportword.\n")
        await comp.storage.hold_source(held, "scan_missing")

        assert (await export_chunks(comp.storage, stamp_provenance=False)).omitted_held_sources == 1
        filtered = await export_chunks(
            comp.storage, tag_filter="no-such-tag", stamp_provenance=False
        )
        assert filtered.omitted_held_sources == 0
        assert filtered.chunks == []


class _RecordingLLM:
    def __init__(self, on_call=None):
        self.bodies: list[str] = []
        self._on_call = on_call

    async def generate(self, user, *, system=None, max_tokens=None):
        self.bodies.append(user)
        if self._on_call is not None:
            self._on_call()
        return "A short summary."


def _summary_config(comp):
    cfg = comp.config.indexing
    cfg.auto_summarize = True
    return cfg


class TestSummaries:
    async def test_regeneration_sends_no_held_text(self, bm25_only_components):
        from memtomem.web.routes.sources import _run_summary_regen

        comp, mem_dir = bm25_only_components
        held = mem_dir / "held.md"
        await _index(comp, held, "# R\n\nheldregenword.\n")
        await comp.storage.set_ai_summary(held, "old prose", "sig", "xx")
        await comp.storage.hold_source(held, REDACTION_BLOCKED_HOLD)
        llm = _RecordingLLM()
        state = SimpleNamespace(summary_regen={"running": True, "done": 0, "failed": 0})

        await _run_summary_regen(
            state, comp.storage, llm, [Path(norm_path(held))], _summary_config(comp)
        )

        assert llm.bodies == []
        # Skipped, not deleted: the summary stays (hidden by the read screen).
        assert (await comp.storage.get_ai_summary(held))["summary"] == "old prose"

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege")
    async def test_regeneration_reads_and_writes_the_stored_key(self, bm25_only_components):
        from memtomem.web.routes.sources import _run_summary_regen

        comp, mem_dir = bm25_only_components
        a, b = mem_dir / "a.md", mem_dir / "b.md"
        await _index(comp, a, "# A\n\nalphaword body.\n")
        await _index(comp, b, "# B\n\nbravoword body.\n")
        stored_a = Path(norm_path(a))
        a.unlink()
        _symlink_or_skip(a, b)
        llm = _RecordingLLM()
        state = SimpleNamespace(summary_regen={"running": True, "done": 0, "failed": 0})

        await _run_summary_regen(state, comp.storage, llm, [stored_a], _summary_config(comp))

        assert len(llm.bodies) == 1
        assert "alphaword" in llm.bodies[0] and "bravoword" not in llm.bodies[0]
        assert await comp.storage.get_ai_summary(stored_a, as_stored=True) is not None
        assert await comp.storage.get_ai_summary(Path(norm_path(b)), as_stored=True) is None

    @pytest.mark.skipif(sys.platform == "win32", reason="symlinks need privilege")
    async def test_a_symlink_placed_during_the_llm_call_does_not_move_the_summary(
        self, bm25_only_components
    ):
        from memtomem.indexing.summarizer import maybe_update_ai_summary

        comp, mem_dir = bm25_only_components
        a, b = mem_dir / "a.md", mem_dir / "b.md"
        chunks = await _index(comp, a, "# A\n\nalphaword body.\n")
        await _index(comp, b, "# B\n\nbravoword body.\n")
        stored_a, stored_b = Path(norm_path(a)), Path(norm_path(b))
        await comp.storage.delete_ai_summary(stored_a, as_stored=True)
        await comp.storage.delete_ai_summary(stored_b, as_stored=True)

        def swap() -> None:
            a.unlink()
            _symlink_or_skip(a, b)

        await maybe_update_ai_summary(
            comp.storage, _RecordingLLM(on_call=swap), a, chunks, _summary_config(comp)
        )

        assert await comp.storage.get_ai_summary(stored_a, as_stored=True) is not None
        assert await comp.storage.get_ai_summary(stored_b, as_stored=True) is None
