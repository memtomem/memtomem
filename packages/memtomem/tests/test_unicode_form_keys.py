"""Path keys fold Unicode normalisation forms only where the filesystem does (#2544).

On macOS an NFC and an NFD spelling name one directory, so the key folds them
(#235). On ext4 or NTFS they are two directories, and folding made the key the
address of the wrong one: a file under an NFD directory was keyed by an NFC
path that does not exist, so the orphan scan confirmed it missing, and an NFC
and an NFD sibling shared one key, so each index replaced the other's chunks.

The real-filesystem tests run with the real platform flag and skip by asking
the filesystem, so the Linux and Windows jobs witness one arm and the macOS job
the other.
"""

from __future__ import annotations

import unicodedata
from pathlib import Path

import pytest

import memtomem.storage.sqlite_helpers as helpers_mod
from memtomem.storage.orphan_detect import scan_orphans
from memtomem.storage.sqlite_helpers import fold_path_form, norm_dir_prefix, norm_path

from helpers import (
    CAFE_NFC,
    CAFE_NFD,
    filesystem_folds_unicode_forms,
    filesystem_keeps_unicode_forms_apart,
)


def test_the_two_spellings_really_differ() -> None:
    assert CAFE_NFC != CAFE_NFD
    assert unicodedata.normalize("NFC", CAFE_NFD) == CAFE_NFC


class TestWithoutFolding:
    """The keying on a platform whose filesystems keep the forms apart."""

    @pytest.fixture(autouse=True)
    def _no_fold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(helpers_mod, "FOLDS_UNICODE_FORMS", False)

    def test_fold_path_form_returns_the_spelling_unchanged(self) -> None:
        assert fold_path_form(f"/notes/{CAFE_NFD}") == f"/notes/{CAFE_NFD}"

    def test_norm_path_keeps_the_nfd_bytes(self, tmp_path: Path) -> None:
        assert norm_path(tmp_path / CAFE_NFD).endswith(CAFE_NFD)
        assert norm_path(tmp_path / CAFE_NFD) != norm_path(tmp_path / CAFE_NFC)

    def test_norm_dir_prefix_keeps_the_nfd_bytes(self, tmp_path: Path) -> None:
        assert norm_dir_prefix(tmp_path / CAFE_NFD) != norm_dir_prefix(tmp_path / CAFE_NFC)

    def test_the_resolve_fallback_is_not_folded_either(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom(self: Path, strict: bool = False) -> Path:
            raise OSError("boom")

        monkeypatch.setattr(Path, "resolve", _boom)
        assert Path(norm_path(Path(f"/tmp/{CAFE_NFD}"))).name == CAFE_NFD


def _require_distinct_forms(tmp_path: Path) -> None:
    if not filesystem_keeps_unicode_forms_apart(tmp_path):
        pytest.skip("this filesystem treats NFC and NFD names as one")


async def _source_keys(storage) -> set[str]:
    return {str(p) for p in await storage.get_all_source_files()}


async def _hits(comp, query: str) -> int:
    results, _stats = await comp.search_pipeline.search(query, top_k=5)
    return len(results)


class TestFormsKeptApart:
    """ext4 / NTFS: an NFD directory is its own path (runs on Linux and Windows CI)."""

    async def test_a_file_under_an_nfd_dir_is_not_an_orphan(self, bm25_only_components, tmp_path):
        """The measured failure: with the fold, the key named an NFC path that
        does not exist, and ``scan_orphans`` confirmed the live file missing."""
        _require_distinct_forms(tmp_path)
        comp, mem_dir = bm25_only_components
        note = mem_dir / CAFE_NFD / "note.md"
        note.parent.mkdir()
        note.write_text("# Lone\n\nquokka lives under the decomposed directory\n")

        await comp.index_engine.index_file(note)

        assert await _source_keys(comp.storage) == {str(note.resolve())}
        result = await scan_orphans(comp.storage, recheck_delay_seconds=0)
        assert result.confirmed_orphans == []

    async def test_nfc_and_nfd_siblings_keep_their_own_chunks(self, bm25_only_components, tmp_path):
        """The other measured failure: one shared key, so the second index
        replaced the first file's chunks and its content stopped matching."""
        _require_distinct_forms(tmp_path)
        comp, mem_dir = bm25_only_components
        composed = mem_dir / CAFE_NFC / "note.md"
        decomposed = mem_dir / CAFE_NFD / "note.md"
        composed.parent.mkdir()
        decomposed.parent.mkdir()
        composed.write_text("# A\n\nwombat note in the composed directory\n")
        decomposed.write_text("# B\n\nplatypus note in the decomposed directory\n")

        await comp.index_engine.index_file(composed)
        await comp.index_engine.index_file(decomposed)

        assert await _source_keys(comp.storage) == {
            str(composed.resolve()),
            str(decomposed.resolve()),
        }
        assert await _hits(comp, "wombat") == 1
        assert await _hits(comp, "platypus") == 1

    async def test_a_row_keyed_by_the_old_fold_becomes_the_only_orphan(
        self, bm25_only_components, tmp_path, monkeypatch: pytest.MonkeyPatch
    ):
        """Upgrade path for a database written before #2544: the folded row names
        a path that does not exist, so it is exactly what orphan cleanup removes,
        and a re-index keys the file correctly."""
        _require_distinct_forms(tmp_path)
        comp, mem_dir = bm25_only_components
        note = mem_dir / CAFE_NFD / "note.md"
        note.parent.mkdir()
        note.write_text("# Legacy\n\nnumbat indexed before the fix\n")
        legacy_key = unicodedata.normalize("NFC", str(note.resolve()))

        monkeypatch.setattr(helpers_mod, "FOLDS_UNICODE_FORMS", True)
        await comp.index_engine.index_file(note)
        assert await _source_keys(comp.storage) == {legacy_key}

        monkeypatch.setattr(helpers_mod, "FOLDS_UNICODE_FORMS", False)
        await comp.index_engine.index_file(note)

        assert await _source_keys(comp.storage) == {legacy_key, str(note.resolve())}
        result = await scan_orphans(comp.storage, recheck_delay_seconds=0)
        assert [str(p) for p in result.confirmed_orphans] == [legacy_key]


class TestFormsFolded:
    """APFS / HFS+: the two spellings are one file (runs on macOS CI)."""

    async def test_both_spellings_of_one_file_share_one_key(self, bm25_only_components, tmp_path):
        """#235 end to end with the real platform flag, not a forced one."""
        if not filesystem_folds_unicode_forms(tmp_path):
            pytest.skip("this filesystem keeps NFC and NFD names apart")
        comp, mem_dir = bm25_only_components
        (mem_dir / CAFE_NFD).mkdir()
        (mem_dir / CAFE_NFD / "note.md").write_text("# One\n\nbilby note reached two ways\n")

        await comp.index_engine.index_file(mem_dir / CAFE_NFC / "note.md")
        await comp.index_engine.index_file(mem_dir / CAFE_NFD / "note.md")

        assert len(await _source_keys(comp.storage)) == 1
        assert await _hits(comp, "bilby") == 1
