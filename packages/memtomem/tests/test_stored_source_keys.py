"""A delete or hold by a stored source key acts on that exact key (#2835).

Stored ``chunks.source_file`` keys are :func:`norm_path` forms when written. A
caller holding one read back from the store must not resolve it again: if the
path has since become a symlink, the second resolution names the target, and
the delete or hold lands on another source's rows.

Every test here relinks a stored path to another one, so they run against a
real ``SqliteBackend`` and real symlinks.
"""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
import unicodedata
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.config import IndexingConfig
from memtomem.indexing.watcher import FileWatcher
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud
from memtomem.storage import orphan_detect, sqlite_helpers
from memtomem.storage.orphan_detect import RetainRule, group_orphans, hold_orphans, scan_orphans
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path, stored_key_for, stored_key_moved

BODY = "## Note\n\nstoredkeyword lives here\n"


@pytest.fixture
async def env(bm25_only_components, monkeypatch):
    comp, mem_dir = bm25_only_components
    monkeypatch.setattr(orphan_detect, "ORPHAN_RECHECK_DELAY_SECONDS", 0.0)
    return comp, Path(norm_path(mem_dir))


async def _indexed(comp, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(BODY, encoding="utf-8")
    stats = await comp.index_engine.index_file(path)
    assert stats.indexed_chunks == 1, stats
    return path


async def _rows(comp, key: Path) -> int:
    return len(await comp.storage.list_chunks_by_source(key, as_stored=True))


async def _relinked(comp, root: Path, *, target_present: bool) -> tuple[Path, Path]:
    """Index ``work/a.md`` and ``plans/b.md``, then make ``a.md`` a link to ``b.md``."""
    a = await _indexed(comp, root / "work" / "a.md")
    b = await _indexed(comp, root / "plans" / "b.md")
    if not target_present:
        b.unlink()
    a.unlink()
    a.symlink_to(b)
    assert await _rows(comp, a) == 1 and await _rows(comp, b) == 1
    return a, b


def _patch_bootstrap(monkeypatch, comp) -> None:
    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)


@pytest.mark.requires_symlinks
class TestStorage:
    async def test_dangling_relink_deletes_each_source_own_rows(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=False)

        assert await comp.storage.delete_by_source(b, as_stored=True) == 1
        assert await comp.storage.delete_by_source(a, as_stored=True) == 1

        assert await _rows(comp, a) == 0 and await _rows(comp, b) == 0

    async def test_live_relink_deletes_the_link_rows_not_the_target(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)

        assert await comp.storage.delete_by_source(a, as_stored=True) == 1

        assert await _rows(comp, a) == 0
        assert await _rows(comp, b) == 1

    async def test_without_as_stored_a_path_is_still_normalized(self, env, tmp_path):
        comp, root = env
        await _indexed(comp, root / "plans" / "b.md")
        alias = tmp_path / "alias"
        alias.symlink_to(root / "plans", target_is_directory=True)

        assert await comp.storage.delete_by_source(alias / "b.md") == 1

    async def test_delete_clears_the_stored_key_side_rows_only(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        storage = comp.storage
        for key in (a, b):
            await storage.set_ai_summary(key, "summary", "sig", "en", as_stored=True)
            assert await storage.hold_source(key, "scan_missing", as_stored=True)
        receipts = "SELECT COUNT(*) FROM source_index_receipts WHERE source_file=?"
        db = storage._get_db()
        assert db.execute(receipts, (str(a),)).fetchone()[0] == 1

        await storage.delete_by_source(a, as_stored=True)

        assert await storage.get_ai_summary(a, as_stored=True) is None
        assert await storage.get_ai_summary(b, as_stored=True) is not None
        assert not await storage.is_source_held(a)
        assert await storage.is_source_held(b)
        assert db.execute(receipts, (str(a),)).fetchone()[0] == 0
        assert db.execute(receipts, (str(b),)).fetchone()[0] == 1

    async def test_hold_holds_the_link_key_not_its_target(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)

        assert await comp.storage.hold_source(a, "scan_missing", as_stored=True)

        assert await comp.storage.is_source_held(a)
        assert not await comp.storage.is_source_held(b)

    async def test_release_leaves_the_target_redaction_hold(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        storage = comp.storage
        assert await storage.hold_source(a, "scan_missing", as_stored=True)
        assert await storage.hold_source(b, REDACTION_BLOCKED_HOLD, as_stored=True)
        generation = await storage.source_hold_generation(b)

        assert await storage.release_source_hold(a, as_stored=True)

        assert not await storage.is_source_held(a)
        assert await storage.is_source_held(b)
        assert await storage.source_hold_generation(b) == generation

    async def test_count_counts_each_key_its_own_rows(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)

        counts = await comp.storage.count_chunks_by_sources([a, b], as_stored=True)

        assert counts == {a: 1, b: 1}

    async def test_scope_probe_reads_the_link_key(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        db = comp.storage._get_db()
        db.execute("UPDATE chunks SET scope='project_local' WHERE source_file=?", (str(b),))
        db.commit()
        assert await comp.storage.list_scopes_by_source(b, as_stored=True) == {"project_local"}

        assert await comp.storage.list_scopes_by_source(a, as_stored=True) == {"user"}


class TestStoredKeyMoved:
    def test_a_canonical_key_has_not_moved(self, tmp_path):
        present = tmp_path / "present.md"
        present.write_text("x", encoding="utf-8")

        assert not stored_key_moved(norm_path(present))
        assert not stored_key_moved(norm_path(tmp_path / "absent.md"))

    @pytest.mark.requires_symlinks
    def test_a_relinked_key_has_moved(self, tmp_path):
        target = tmp_path / "target.md"
        target.write_text("x", encoding="utf-8")
        link = Path(norm_path(tmp_path)) / "link.md"
        key = str(link)
        link.symlink_to(target)

        assert stored_key_moved(key)

    def test_unicode_form_alone_is_not_a_move(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
        key = unicodedata.normalize("NFD", str(Path(norm_path(tmp_path)) / "café.md"))
        assert key != unicodedata.normalize("NFC", key)

        assert not stored_key_moved(key)


class TestStoredKeyFor:
    """Matching is by string only: nothing about the request is resolved."""

    @pytest.mark.requires_symlinks
    def test_a_relinked_key_matches_itself_and_an_alias_matches_nothing(self, tmp_path):
        real = Path(norm_path(tmp_path)) / "real"
        real.mkdir()
        a, b = real / "a.md", real / "b.md"
        b.write_text("x", encoding="utf-8")
        a.symlink_to(b)
        alias = tmp_path / "alias"
        alias.symlink_to(real, target_is_directory=True)

        assert stored_key_for(str(a), [a, b]) == a
        assert stored_key_for(str(a), [b]) is None
        assert stored_key_for(str(alias / "b.md"), [a, b]) is None

    def test_a_folded_spelling_finds_the_one_stored_key(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
        key = Path(norm_path(tmp_path)) / unicodedata.normalize("NFC", "café.md")
        nfd = unicodedata.normalize("NFD", str(key))

        assert stored_key_for(nfd, [key]) == key

    def test_two_keys_folding_alike_match_only_their_own_spelling(self, tmp_path, monkeypatch):
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
        base = str(Path(norm_path(tmp_path)))
        nfc = Path(base) / unicodedata.normalize("NFC", "Å.md")
        nfd = Path(unicodedata.normalize("NFD", str(nfc)))
        third = str(Path(base) / "\u212b.md")  # ANGSTROM SIGN, NFC-folds to Å

        assert stored_key_for(str(nfc), [nfc, nfd]) == nfc
        assert stored_key_for(str(nfd), [nfc, nfd]) == nfd
        assert stored_key_for(third, [nfc, nfd]) is None

    def test_a_case_variant_matches_nothing(self, tmp_path):
        key = Path(norm_path(tmp_path)) / "A.md"

        assert stored_key_for(str(key.with_name("a.md")), [key]) is None

    @pytest.mark.skipif(
        sys.platform == "win32", reason="POSIX path spelling; Path writes it with backslashes"
    )
    def test_the_tmp_spelling_mem_list_prints_finds_a_private_tmp_key(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        key = Path("/private/tmp/notes/a.md")

        assert stored_key_for("/tmp/notes/a.md", [key]) == key

    async def test_the_store_lookup_matches_as_the_helper_does(self, env, monkeypatch):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        storage = comp.storage

        assert await storage.find_source_key(str(a)) == a
        assert await storage.find_source_key(str(root / "work" / "." / "a.md")) == a
        assert await storage.find_source_key(str(root / "work" / "missing.md")) is None
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", False)
        assert await storage.find_source_key(str(a).upper()) is None


class TestLegacyUnicodeKeys:
    """A key stored before keys were folded (#235) names the same file as its folded form.

    The stored-key judgments compare strings against folded prefixes, so they fold
    the key too. Folding is forced on so every platform runs these.
    """

    @pytest.fixture(autouse=True)
    def _fold(self, monkeypatch):
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)

    @staticmethod
    def _cafe(tmp_path: Path) -> tuple[Path, Path]:
        root = Path(norm_path(tmp_path)) / "root"
        cafe = root / unicodedata.normalize("NFC", "café")
        cafe.mkdir(parents=True)
        key = Path(unicodedata.normalize("NFD", str(cafe / "a.md")))
        assert str(key) != str(cafe / "a.md")
        return cafe, key

    def test_retain_keeps_an_nfd_key_under_an_nfc_retain_dir(self, tmp_path):
        cafe, key = self._cafe(tmp_path)
        rule = RetainRule.prepare(
            IndexingConfig(memory_dirs=[cafe.parent], retain_missing_dirs=[cafe])
        )

        assert rule.split([key]) == ([key], [])

    def test_orphan_groups_and_under_take_an_nfd_key_with_its_root(self, tmp_path):
        cafe, key = self._cafe(tmp_path)

        groups, excluded = group_orphans([key], [cafe], under=[cafe])

        assert excluded == 0
        assert [(g.root, g.sources) for g in groups] == [(cafe, [key])]


@pytest.mark.requires_symlinks
class TestCallers:
    async def test_orphan_scan_holds_a_relinked_source_under_its_own_key(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md")
        b.unlink()
        a.unlink()
        a.symlink_to(root / "plans" / "never-indexed.md")

        result = await scan_orphans(comp.storage)
        await hold_orphans(comp.storage, result, comp.config.indexing)

        assert set(await comp.storage.get_held_sources()) == {a, b}

    async def test_gc_orphan_sources_deletes_both_sides_of_a_dangling_relink(
        self, env, monkeypatch
    ):
        from memtomem.cli import gc_cmd

        comp, root = env
        a, b = await _relinked(comp, root, target_present=False)
        _patch_bootstrap(monkeypatch, comp)

        await gc_cmd._run_orphan_sources(apply_=True, assume_yes=True)

        assert await _rows(comp, a) == 0 and await _rows(comp, b) == 0

    async def test_purge_skips_a_relinked_key_and_deletes_matches_by_their_key(
        self, env, tmp_path, monkeypatch, capsys
    ):
        """A relinked key is reported, not judged by the file it now reaches.

        ``work/MEMORY.md`` now points into a claude-memory root, whose
        convention excludes ``MEMORY.md``. Judged through the link, the stored
        key matched and the delete removed ``healthy.md``'s rows.
        """
        from memtomem.cli import purge_cmd

        comp, root = env
        claude = Path(norm_path(tmp_path)) / ".claude" / "projects" / "demo" / "memory"
        work = root / "work"
        comp.config.indexing.memory_dirs = [work, claude]
        memory_md = await _indexed(comp, work / "MEMORY.md")
        healthy = await _indexed(comp, claude / "healthy.md")
        draft = await _indexed(comp, work / "drafts" / "x.md")
        memory_md.unlink()
        memory_md.symlink_to(healthy)
        comp.config.indexing.exclude_patterns = ["drafts/**"]
        _patch_bootstrap(monkeypatch, comp)

        await purge_cmd._run_matching_excluded(apply_=True, sample_size=5, as_json=True)

        payload = json.loads(capsys.readouterr().out)
        assert payload["files"] == 1 and payload["chunks"] == 1, payload
        assert payload["moved"] == 1 and payload["moved_sample"] == [str(memory_md)]
        assert await _rows(comp, draft) == 0
        assert await _rows(comp, healthy) == 1
        assert await _rows(comp, memory_md) == 1

    async def test_mem_delete_refuses_a_stored_key_that_now_reaches_another_source(self, env):
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file=str(a), ctx=ctx)

        assert out.startswith("Error:") and "resolves to another file" in out, out
        assert await _rows(comp, a) == 1
        assert await _rows(comp, b) == 1

    async def test_mem_delete_refusal_creates_no_sidecar_beside_an_outside_target(
        self, env, tmp_path
    ):
        comp, root = env
        a = await _indexed(comp, root / "a.md")
        outside = tmp_path / "outside"
        outside.mkdir()
        target = outside / "t.md"
        target.write_text("x", encoding="utf-8")
        a.unlink()
        a.symlink_to(target)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file=str(a), ctx=ctx)

        assert "resolves to another file" in out, out
        assert sorted(p.name for p in outside.iterdir()) == ["t.md"]
        assert await _rows(comp, a) == 1

    async def test_watcher_settles_a_relinked_pending_key_under_its_own_key(self, env):
        comp, root = env
        a = await _indexed(comp, root / "work" / "a.md")
        b = await _indexed(comp, root / "plans" / "b.md")
        assert comp.storage.queue_source_check_sync(a)
        b.unlink()
        a.unlink()
        a.symlink_to(b)
        assert a in await comp.storage.get_pending_source_checks()

        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        task = asyncio.create_task(watcher._recheck_held_loop())
        try:
            for _ in range(100):
                if a in await comp.storage.get_held_sources():
                    break
                await asyncio.sleep(0.01)
            assert a in await comp.storage.get_held_sources()
            assert b not in await comp.storage.get_held_sources()
            assert a not in await comp.storage.get_pending_source_checks()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_watcher_shows_an_outside_root_relinked_key_under_its_own_key(self, env):
        """A held source outside every root is shown again once present (#2835).

        Its link and its target are both outside the remaining root, so the
        recheck takes the release branch rather than reindexing.
        """
        comp, root = env
        a = await _indexed(comp, root / "old" / "a.md")
        b = await _indexed(comp, root / "elsewhere" / "b.md")
        a.unlink()
        a.symlink_to(b)
        assert await comp.storage.hold_source(a, "watch_root_removed", as_stored=True)
        (root / "plans").mkdir()
        comp.config.indexing.memory_dirs = [root / "plans"]

        watcher = FileWatcher(comp.index_engine, comp.config.indexing)
        task = asyncio.create_task(watcher._recheck_held_loop())
        try:
            for _ in range(100):
                if a not in await comp.storage.get_held_sources():
                    break
                await asyncio.sleep(0.01)
            assert a not in await comp.storage.get_held_sources()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_mem_delete_refuses_an_outside_root_key_that_links_into_a_root(self, env):
        """``work/a.md`` left the configured roots and now links to ``plans/b.md``
        inside one. ``_validate_path`` would accept the resolved path and delete b."""
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        comp.config.indexing.memory_dirs = [root / "plans"]
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file=str(a), ctx=ctx)

        assert "resolves to another file" in out, out
        assert await _rows(comp, a) == 1 and await _rows(comp, b) == 1

    async def test_mem_delete_removes_a_legacy_nfd_key_by_that_key(self, env, monkeypatch):
        """A key stored before keys were folded (#235) is deleted by its own spelling,
        not by the folded one, which holds no rows."""
        comp, root = env
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", False)
        nfd = await _indexed(comp, root / unicodedata.normalize("NFD", "café.md"))
        assert await _rows(comp, nfd) == 1
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file=str(nfd), ctx=ctx)

        assert "Removed 1 chunks" in out, out
        assert await _rows(comp, nfd) == 0

    async def test_mem_delete_refuses_a_relative_name_for_a_relinked_key(self, env):
        """A relative ``source_file`` is the entry under the first memory dir, not the
        file it now links to."""
        comp, root = env
        a, b = await _relinked(comp, root, target_present=True)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file="work/a.md", ctx=ctx)

        assert "resolves to another file" in out, out
        assert await _rows(comp, a) == 1 and await _rows(comp, b) == 1

    async def test_mem_delete_refuses_another_unicode_spelling_of_a_relinked_key(
        self, env, monkeypatch
    ):
        comp, root = env
        monkeypatch.setattr(sqlite_helpers, "FOLDS_UNICODE_FORMS", True)
        cafe = await _indexed(comp, root / "work" / unicodedata.normalize("NFC", "café.md"))
        b = await _indexed(comp, root / "plans" / "b.md")
        cafe.unlink()
        cafe.symlink_to(b)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(
            source_file=unicodedata.normalize("NFD", str(cafe)), ctx=ctx
        )

        assert "resolves to another file" in out, out
        assert await _rows(comp, cafe) == 1 and await _rows(comp, b) == 1

    async def test_mem_delete_refuses_a_case_variant_of_a_relinked_key(self, env):
        """On a case-insensitive volume ``a.md`` opens ``A.md``, which now links to
        ``b.md``; resolving the request deleted b's rows."""
        comp, root = env
        upper = await _indexed(comp, root / "work" / "A.md")
        b = await _indexed(comp, root / "plans" / "b.md")
        upper.unlink()
        upper.symlink_to(b)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file=str(root / "work" / "a.md"), ctx=ctx)

        assert "is not an indexed source file" in out, out
        assert await _rows(comp, upper) == 1 and await _rows(comp, b) == 1

    async def test_mem_delete_of_an_unindexed_path_is_an_error(self, env):
        comp, root = env
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file=str(root / "never.md"), ctx=ctx)

        assert out.startswith("Error:") and "is not an indexed source file" in out, out

    async def test_mem_delete_refuses_a_relative_name_under_a_root_that_became_a_link(
        self, env, tmp_path
    ):
        """Memory dirs ``[A, B]`` each hold ``n.md``; then ``A`` becomes a link to
        ``B``. ``n.md`` names A's key, which has moved; resolving A first matched
        B's key and deleted it."""
        comp, root = env
        a_dir, b_dir = root / "A", root / "B"
        a_dir.mkdir()
        b_dir.mkdir()
        comp.config.indexing.memory_dirs = [a_dir, b_dir]
        a_note = await _indexed(comp, a_dir / "n.md")
        b_note = await _indexed(comp, b_dir / "n.md")
        shutil.rmtree(a_dir)
        a_dir.symlink_to(b_dir, target_is_directory=True)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await memory_crud.mem_delete(source_file="n.md", ctx=ctx)

        assert "resolves to another file" in out, out
        assert await _rows(comp, a_note) == 1 and await _rows(comp, b_note) == 1
