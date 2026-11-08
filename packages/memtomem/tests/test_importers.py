"""Tests for Notion and Obsidian importers."""

import unicodedata
import zipfile
from pathlib import Path

import pytest
from memtomem.indexing import importers
from memtomem.indexing.importers import (
    NotionImportCollisionError,
    UnsafeArchiveError,
    _build_notion_output_mapping,
    _clean_notion_filename,
    _clean_notion_markdown,
    _convert_obsidian_syntax,
    import_notion,
    import_obsidian,
    safe_extract_zip,
)


class TestNotionFilename:
    def test_strips_uuid(self):
        assert _clean_notion_filename("My Page abc123def4567890123456789abcdef0") == "My Page"

    def test_no_uuid(self):
        assert _clean_notion_filename("Simple Name") == "Simple Name"

    def test_short_hex_not_stripped(self):
        assert _clean_notion_filename("Page abc123") == "Page abc123"

    def test_empty_after_strip(self):
        # 32-char hex only
        result = _clean_notion_filename("abcdef01234567890123456789012345")
        assert result == "abcdef01234567890123456789012345"

    @pytest.mark.parametrize(
        "stem",
        ["Report ABCDEF0123456789ABCDEF0123456789", "Report AbCdEf0123456789abcdef0123456789"],
    )
    def test_upper_or_mixed_case_hex_is_part_of_the_title(self, stem):
        # Notion writes page UUIDs in lowercase; anything else is the title (#3140).
        assert _clean_notion_filename(stem) == stem


class TestNotionLinkTail:
    """#3140 — the fragment, query and external-URL handling #3119 added to ``_fix_link``."""

    @pytest.mark.asyncio
    async def test_fragment_and_query_survive_on_a_mapped_sibling_link(self, tmp_path):
        export = tmp_path / "export"
        export.mkdir()
        ua, ub = "a1b2c3d4e5f60718293a4b5c6d7e8f90", "f0e1d2c3b4a5968778695a4b3c2d1e0f"
        (export / f"Meeting Notes {ua}.md").write_text("# one", encoding="utf-8")
        (export / f"Meeting Notes {ub}.md").write_text("# two", encoding="utf-8")
        (export / "Index 0123456789abcdef0123456789abcdef.md").write_text(
            f"[s](Meeting%20Notes%20{ua}.md#section) [q](Meeting%20Notes%20{ub}.md?v=1#top)",
            encoding="utf-8",
        )
        output = tmp_path / "out"

        await import_notion(export, output)

        index = (output / "Index.md").read_text(encoding="utf-8")
        assert "[s](Meeting Notes (a1b2c3d4).md#section)" in index
        assert "[q](Meeting Notes (f0e1d2c3).md?v=1#top)" in index

    @pytest.mark.parametrize(
        "href",
        [
            "https://example.com/Page%20a1b2c3d4e5f60718293a4b5c6d7e8f90.md",
            "//example.com/Page%20a1b2c3d4e5f60718293a4b5c6d7e8f90.md",
            "mailto:Page%20a1b2c3d4e5f60718293a4b5c6d7e8f90.md",
        ],
    )
    def test_external_md_link_keeps_its_uuid(self, href):
        from urllib.parse import unquote

        cleaned = _clean_notion_markdown(f"[x]({href})")
        assert cleaned == f"[x]({unquote(href)})"


class TestNotionMarkdown:
    def test_removes_property_table(self):
        content = "| Property | Value |\n| --- | --- |\n| Status | Done |\n\n# Real Content"
        cleaned = _clean_notion_markdown(content)
        assert "Real Content" in cleaned
        assert "Property" not in cleaned

    def test_removes_empty_toggles(self):
        content = "Before\n<details>\n<summary></summary>\n</details>\nAfter"
        cleaned = _clean_notion_markdown(content)
        assert "<details>" not in cleaned

    def test_preserves_normal_content(self):
        content = "## Heading\n\nSome paragraph text."
        cleaned = _clean_notion_markdown(content)
        assert "Heading" in cleaned
        assert "paragraph" in cleaned


class TestObsidianSyntax:
    def test_wikilink(self):
        result = _convert_obsidian_syntax("See [[My Note]] for details")
        assert "[My Note](My%20Note.md)" in result

    def test_wikilink_with_alias(self):
        result = _convert_obsidian_syntax("See [[My Note|the note]]")
        assert "[the note](My%20Note.md)" in result

    def test_embed(self):
        result = _convert_obsidian_syntax("![[Document]]")
        assert "[Document](Document.md)" in result

    def test_callout(self):
        result = _convert_obsidian_syntax("> [!note] Important thing")
        assert "**Note**: Important thing" in result

    def test_callout_warning(self):
        result = _convert_obsidian_syntax("> [!warning] Be careful")
        assert "**Warning**: Be careful" in result

    def test_preserves_normal_links(self):
        result = _convert_obsidian_syntax("[text](https://example.com)")
        assert "[text](https://example.com)" in result

    def test_preserves_tags(self):
        result = _convert_obsidian_syntax("Some text #tag1 #tag2")
        assert "#tag1" in result
        assert "#tag2" in result


class TestImportNotion:
    @pytest.mark.asyncio
    async def test_import_directory(self, tmp_path):
        # Create fake Notion export
        notion_dir = tmp_path / "notion_export"
        notion_dir.mkdir()
        (notion_dir / "My Page abc123def4567890123456789abcdef0.md").write_text(
            "# My Page\n\nSome content here.",
            encoding="utf-8",
        )
        (notion_dir / "Sub").mkdir()
        (notion_dir / "Sub" / "Child def4567890123456789012345abcdef0.md").write_text(
            "# Child Page\n\nNested content.",
            encoding="utf-8",
        )

        output = tmp_path / "output"
        imported = await import_notion(notion_dir, output)

        assert len(imported) == 2
        assert all(f.exists() for f in imported)
        # UUID should be stripped from filename
        names = {f.name for f in imported}
        assert "My Page.md" in names
        assert "Child.md" in names
        # Content should have import metadata
        content = imported[0].read_text(encoding="utf-8")
        assert "imported_from: notion" in content

    @pytest.mark.asyncio
    async def test_import_zip(self, tmp_path):
        import zipfile

        notion_dir = tmp_path / "notion_src"
        notion_dir.mkdir()
        (notion_dir / "Note abc123def4567890123456789abcdef0.md").write_text(
            "# Note\n\nContent.",
            encoding="utf-8",
        )

        zip_path = tmp_path / "notion.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(
                notion_dir / "Note abc123def4567890123456789abcdef0.md",
                "Note abc123def4567890123456789abcdef0.md",
            )

        output = tmp_path / "output"
        imported = await import_notion(zip_path, output)
        assert len(imported) == 1

    @pytest.mark.asyncio
    async def test_notion_sibling_pages_collision_directory(self, tmp_path):
        """Notion sibling pages sharing a title disambiguate with UUID prefixes (#3113)."""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        (export_dir / "Meeting Notes a1b2c3d4e5f60718293a4b5c6d7e8f90.md").write_text(
            "# Meeting Notes 1\nContent 1",
            encoding="utf-8",
        )
        (export_dir / "Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md").write_text(
            "# Meeting Notes 2\nContent 2",
            encoding="utf-8",
        )

        output = tmp_path / "out"
        imported = await import_notion(export_dir, output)

        assert len(imported) == 2
        assert len(set(imported)) == 2
        assert all(p.exists() for p in imported)

        f1 = output / "Meeting Notes (a1b2c3d4).md"
        f2 = output / "Meeting Notes (f0e1d2c3).md"
        plain = output / "Meeting Notes.md"
        assert f1.exists()
        assert f2.exists()
        assert not plain.exists()
        assert "Content 1" in f1.read_text(encoding="utf-8")
        assert "Content 2" in f2.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_notion_sibling_pages_collision_zip(self, tmp_path):
        """Notion sibling pages in a ZIP archive disambiguate with UUID prefixes (#3113)."""
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Meeting Notes a1b2c3d4e5f60718293a4b5c6d7e8f90.md", "# First")
            zf.writestr("Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md", "# Second")

        output = tmp_path / "out"
        imported = await import_notion(zip_path, output)

        assert len(imported) == 2
        assert len(set(imported)) == 2
        f1 = output / "Meeting Notes (a1b2c3d4).md"
        f2 = output / "Meeting Notes (f0e1d2c3).md"
        plain = output / "Meeting Notes.md"
        assert f1.exists()
        assert f2.exists()
        assert not plain.exists()
        assert "# First" in f1.read_text(encoding="utf-8")
        assert "# Second" in f2.read_text(encoding="utf-8")

    @pytest.mark.asyncio
    async def test_notion_sibling_pages_unique_page_plain_name(self, tmp_path):
        """A non-colliding page next to colliding siblings keeps its plain name (#3113)."""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        (export_dir / "Meeting Notes a1b2c3d4e5f60718293a4b5c6d7e8f90.md").write_text(
            "# Meeting Notes 1", encoding="utf-8"
        )
        (export_dir / "Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md").write_text(
            "# Meeting Notes 2", encoding="utf-8"
        )
        (export_dir / "Project Plan 11223344556677889900aabbccddeeff.md").write_text(
            "# Project Plan", encoding="utf-8"
        )

        output = tmp_path / "out"
        imported = await import_notion(export_dir, output)

        assert len(imported) == 3
        assert (output / "Meeting Notes (a1b2c3d4).md").exists()
        assert (output / "Meeting Notes (f0e1d2c3).md").exists()
        assert (output / "Project Plan.md").exists()
        assert not (output / "Project Plan (11223344).md").exists()

    @pytest.mark.asyncio
    async def test_notion_reimport_updates_in_place_no_duplicate(self, tmp_path):
        """Re-importing the same export updates existing files in place (#3113)."""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        file1 = export_dir / "Meeting Notes a1b2c3d4e5f60718293a4b5c6d7e8f90.md"
        file2 = export_dir / "Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md"
        file1.write_text("# Initial 1", encoding="utf-8")
        file2.write_text("# Initial 2", encoding="utf-8")

        output = tmp_path / "out"
        imported_run1 = await import_notion(export_dir, output)
        assert len(imported_run1) == 2

        # Edit content in export
        file1.write_text("# Updated 1", encoding="utf-8")

        imported_run2 = await import_notion(export_dir, output)
        assert len(imported_run2) == 2
        assert set(imported_run1) == set(imported_run2)

        f1 = output / "Meeting Notes (a1b2c3d4).md"
        assert "Updated 1" in f1.read_text(encoding="utf-8")
        assert len(list(output.glob("*.md"))) == 2

    @pytest.mark.asyncio
    async def test_notion_links_resolve_colliding_siblings_and_nested(self, tmp_path):
        """Relative links to colliding siblings, plain pages, and nested folders resolve correctly (#3113)."""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        sub_dir = export_dir / "Subfolder"
        sub_dir.mkdir()

        (export_dir / "Meeting Notes a1b2c3d4e5f60718293a4b5c6d7e8f90.md").write_text(
            "# Sibling 1", encoding="utf-8"
        )
        (export_dir / "Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md").write_text(
            "# Sibling 2", encoding="utf-8"
        )
        (export_dir / "Project Plan 11223344556677889900aabbccddeeff.md").write_text(
            "# Plan", encoding="utf-8"
        )
        (sub_dir / "Nested Note 22334455667788990011223344556677.md").write_text(
            "# Nested 1", encoding="utf-8"
        )
        (sub_dir / "Nested Note 8899aabbccddeeff0011223344556677.md").write_text(
            "# Nested 2", encoding="utf-8"
        )

        index_content = (
            "# Index\n\n"
            "- [First](Meeting%20Notes%20a1b2c3d4e5f60718293a4b5c6d7e8f90.md)\n"
            "- [Second](Meeting%20Notes%20f0e1d2c3b4a5968778695a4b3c2d1e0f.md)\n"
            "- [Plan](Project%20Plan%2011223344556677889900aabbccddeeff.md)\n"
            "- [Nested](Subfolder/Nested%20Note%2022334455667788990011223344556677.md)\n"
        )
        (export_dir / "Index 99999999999999999999999999999999.md").write_text(
            index_content, encoding="utf-8"
        )

        output = tmp_path / "out"
        await import_notion(export_dir, output)

        index_text = (output / "Index.md").read_text(encoding="utf-8")
        assert "[First](Meeting Notes (a1b2c3d4).md)" in index_text
        assert "[Second](Meeting Notes (f0e1d2c3).md)" in index_text
        assert "[Plan](Project Plan.md)" in index_text
        assert "[Nested](Subfolder/Nested Note (22334455).md)" in index_text

    @pytest.mark.asyncio
    async def test_notion_add_third_sibling_preserves_existing_names(self, tmp_path):
        """Adding a third sibling later does not rename existing disambiguated siblings (#3113)."""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        (export_dir / "Meeting Notes a1b2c3d4e5f60718293a4b5c6d7e8f90.md").write_text(
            "# Sibling 1", encoding="utf-8"
        )
        (export_dir / "Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md").write_text(
            "# Sibling 2", encoding="utf-8"
        )

        output = tmp_path / "out"
        await import_notion(export_dir, output)

        assert (output / "Meeting Notes (a1b2c3d4).md").exists()
        assert (output / "Meeting Notes (f0e1d2c3).md").exists()

        # Add third sibling
        (export_dir / "Meeting Notes 1234567890abcdef1234567890abcdef.md").write_text(
            "# Sibling 3", encoding="utf-8"
        )

        await import_notion(export_dir, output)

        assert (output / "Meeting Notes (a1b2c3d4).md").exists()
        assert (output / "Meeting Notes (f0e1d2c3).md").exists()
        assert (output / "Meeting Notes (12345678).md").exists()

    @pytest.mark.asyncio
    async def test_notion_siblings_prefix_collision_uses_full_uuid(self, tmp_path):
        """Siblings sharing the same 8-hex prefix fall back to full 32-hex UUIDs (#3113)."""
        export_dir = tmp_path / "export"
        export_dir.mkdir()
        (export_dir / "Meeting Notes a1b2c3d4111111111111111111111111.md").write_text(
            "# Sibling A", encoding="utf-8"
        )
        (export_dir / "Meeting Notes a1b2c3d4222222222222222222222222.md").write_text(
            "# Sibling B", encoding="utf-8"
        )
        (export_dir / "Meeting Notes f0e1d2c3b4a5968778695a4b3c2d1e0f.md").write_text(
            "# Sibling C", encoding="utf-8"
        )

        output = tmp_path / "out"
        imported = await import_notion(export_dir, output)

        assert len(imported) == 3
        assert (output / "Meeting Notes (a1b2c3d4111111111111111111111111).md").exists()
        assert (output / "Meeting Notes (a1b2c3d4222222222222222222222222).md").exists()
        assert (output / "Meeting Notes (f0e1d2c3).md").exists()


_UA = "a1b2c3d4e5f60718293a4b5c6d7e8f90"
_UB = "f0e1d2c3b4a5968778695a4b3c2d1e0f"
_UC = "0123456789abcdef0123456789abcdef"


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _sources(*names: str) -> list:
    """Mapping-input tuples for files in the export root, or under 'dir/'."""
    out = []
    for name in names:
        rel = Path(name)
        out.append((rel, rel.name, ""))
    return out


class TestNotionCollisionKey:
    """#3141 — titles that differ only by case or normalization collide too.

    These call the mapping directly, so they hold on every platform regardless
    of whether the test machine's filesystem folds case.
    """

    def test_case_only_siblings_get_prefixes_in_their_own_spelling(self):
        mapping = _build_notion_output_mapping(
            _sources(f"Meeting notes {_UA}.md", f"Meeting Notes {_UB}.md")
        )
        assert mapping[(Path("."), f"Meeting notes {_UA}.md")] == "Meeting notes (a1b2c3d4).md"
        assert mapping[(Path("."), f"Meeting Notes {_UB}.md")] == "Meeting Notes (f0e1d2c3).md"

    def test_nfc_and_nfd_titles_collide(self):
        nfc = unicodedata.normalize("NFC", "Café notes")
        nfd = unicodedata.normalize("NFD", "Café notes")
        assert nfc != nfd
        mapping = _build_notion_output_mapping(_sources(f"{nfc} {_UA}.md", f"{nfd} {_UB}.md"))
        names = set(mapping.values())
        assert names == {f"{nfc} (a1b2c3d4).md", f"{nfd} (f0e1d2c3).md"}

    def test_uuidless_case_only_pair_gets_stable_hash_suffixes(self):
        sources = _sources("notes.md", "Notes.md")
        first = _build_notion_output_mapping(sources)
        again = _build_notion_output_mapping(list(reversed(sources)))
        assert first == again
        lower, upper = first[(Path("."), "notes.md")], first[(Path("."), "Notes.md")]
        assert lower.startswith("notes (") and upper.startswith("Notes (")
        assert _fold(lower) != _fold(upper)

    def test_single_uuidless_page_in_a_colliding_group_keeps_plain_name(self):
        mapping = _build_notion_output_mapping(_sources("notes.md", f"Notes {_UA}.md"))
        assert mapping[(Path("."), "notes.md")] == "notes.md"
        assert mapping[(Path("."), f"Notes {_UA}.md")] == "Notes (a1b2c3d4).md"

    def test_case_only_pair_sharing_a_prefix_gets_full_uuids(self):
        ua, ub = "a1b2c3d4" + "1" * 24, "a1b2c3d4" + "2" * 24
        mapping = _build_notion_output_mapping(_sources(f"X {ua}.md", f"x {ub}.md"))
        assert set(mapping.values()) == {f"X ({ua}).md", f"x ({ub}).md"}

    def test_folders_differing_only_by_case_group_together(self):
        # Only a crafted ZIP has two folders with one UUID; a genuine export's
        # folder UUIDs are its page UUIDs.
        mapping = _build_notion_output_mapping(
            _sources(f"Folder {_UC}/X {_UA}.md", f"folder {_UC}/x {_UB}.md")
        )
        assert mapping[(Path(f"Folder {_UC}"), f"X {_UA}.md")] == "X (a1b2c3d4).md"
        assert mapping[(Path(f"folder {_UC}"), f"x {_UB}.md")] == "x (f0e1d2c3).md"

    def test_same_folded_name_in_different_folders_is_not_a_collision(self):
        mapping = _build_notion_output_mapping(_sources(f"one/Page {_UA}.md", f"two/page {_UB}.md"))
        assert set(mapping.values()) == {"Page.md", "page.md"}

    def test_title_spelled_like_a_generated_name_is_refused(self):
        sources = _sources(f"X {_UA}.md", f"x {_UB}.md", f"X (a1b2c3d4) {_UC}.md")
        with pytest.raises(NotionImportCollisionError, match=r"X \(a1b2c3d4\)\.md"):
            _build_notion_output_mapping(sources)

    def test_duplicated_source_is_refused_not_collapsed(self):
        # A ZIP may carry one member name twice. Both entries share a mapping
        # key, so the check must see each source before any dict collapses them.
        with pytest.raises(NotionImportCollisionError):
            _build_notion_output_mapping(_sources(f"X {_UA}.md", f"X {_UA}.md"))

    def test_same_uuid_folders_differing_by_case_are_refused(self):
        # On Windows these parents are one Path key; on every platform they
        # are one folder to a case-insensitive filesystem.
        with pytest.raises(NotionImportCollisionError):
            _build_notion_output_mapping(_sources(f"Folder/X {_UA}.md", f"folder/X {_UA}.md"))

    def test_exact_title_siblings_are_named_as_before(self):
        mapping = _build_notion_output_mapping(
            _sources(f"Meeting Notes {_UA}.md", f"Meeting Notes {_UB}.md", f"Plan {_UC}.md")
        )
        assert set(mapping.values()) == {
            "Meeting Notes (a1b2c3d4).md",
            "Meeting Notes (f0e1d2c3).md",
            "Plan.md",
        }


class TestImportNotionCaseCollision:
    """#3141 through ``import_notion``: one file per page, each with its own text."""

    @staticmethod
    def _assert_one_file_per_page(imported, output):
        folded = {_fold(p.relative_to(output).as_posix()) for p in imported}
        assert len(folded) == len(imported)

    @pytest.mark.asyncio
    async def test_directory_export(self, tmp_path):
        export = tmp_path / "export"
        export.mkdir()
        (export / f"Meeting notes {_UA}.md").write_text("# lower body", encoding="utf-8")
        (export / f"Meeting Notes {_UB}.md").write_text("# Upper body", encoding="utf-8")
        (export / f"Index {_UC}.md").write_text(
            f"[a](Meeting%20notes%20{_UA}.md) [b](Meeting%20Notes%20{_UB}.md)",
            encoding="utf-8",
        )
        output = tmp_path / "out"

        imported = await import_notion(export, output)

        assert len(imported) == 3
        self._assert_one_file_per_page(imported, output)
        lower = output / "Meeting notes (a1b2c3d4).md"
        upper = output / "Meeting Notes (f0e1d2c3).md"
        assert "lower body" in lower.read_text(encoding="utf-8")
        assert "Upper body" in upper.read_text(encoding="utf-8")
        index = (output / "Index.md").read_text(encoding="utf-8")
        assert "[a](Meeting notes (a1b2c3d4).md)" in index
        assert "[b](Meeting Notes (f0e1d2c3).md)" in index

    @pytest.mark.asyncio
    async def test_zip_export(self, tmp_path):
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr(f"Meeting notes {_UA}.md", "# lower body")
            zf.writestr(f"Meeting Notes {_UB}.md", "# Upper body")
            zf.writestr("notes.md", "# uuidless lower")
            zf.writestr("Notes.md", "# uuidless upper")
        output = tmp_path / "out"

        imported = await import_notion(zip_path, output)

        assert len(imported) == 4
        self._assert_one_file_per_page(imported, output)
        texts = sorted(p.read_text(encoding="utf-8").splitlines()[-1] for p in imported)
        assert texts == ["# Upper body", "# lower body", "# uuidless lower", "# uuidless upper"]

    @pytest.mark.asyncio
    async def test_refused_export_writes_nothing(self, tmp_path):
        export = tmp_path / "export"
        export.mkdir()
        for name in (f"X {_UA}.md", f"x {_UB}.md", f"X (a1b2c3d4) {_UC}.md"):
            (export / name).write_text(f"# {name}", encoding="utf-8")
        output = tmp_path / "out"

        with pytest.raises(NotionImportCollisionError):
            await import_notion(export, output)

        assert not output.exists()

    @pytest.mark.asyncio
    async def test_tool_indexes_each_page_once(self, bm25_only_components, tmp_path):
        """Measured on main before the fix: one file on disk, two source keys."""
        from helpers import StubCtx
        from memtomem.server.context import AppContext
        from memtomem.server.tools import importers as importer_tools

        comp, _ = bm25_only_components
        export = tmp_path / "export"
        export.mkdir()
        (export / f"Meeting notes {_UA}.md").write_text(
            "# lower\n\nlower page body text\n", encoding="utf-8"
        )
        (export / f"Meeting Notes {_UB}.md").write_text(
            "# Upper\n\nupper page body text\n", encoding="utf-8"
        )
        ctx = StubCtx(AppContext.from_components(comp))

        out = await importer_tools.mem_import_notion(str(export), ctx=ctx)

        assert "Files imported: 2" in out, out
        sources = await comp.storage.get_all_source_files()
        assert len(sources) == 2
        assert len({_fold(p.name) for p in sources}) == 2
        bodies = sorted(p.read_text(encoding="utf-8").splitlines()[-1] for p in sources)
        assert bodies == ["lower page body text", "upper page body text"]


class TestImportObsidian:
    @pytest.mark.asyncio
    async def test_import_vault(self, tmp_path):
        vault = tmp_path / "my_vault"
        vault.mkdir()
        (vault / "Daily Note.md").write_text(
            "# Daily Note\n\nSee [[Project Plan]] for details.\n> [!note] Remember this",
            encoding="utf-8",
        )
        (vault / "Project Plan.md").write_text(
            "# Project Plan\n\n![[Architecture Diagram]]",
            encoding="utf-8",
        )
        # Create .obsidian config (should be skipped)
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian" / "config.json").write_text("{}", encoding="utf-8")

        output = tmp_path / "output"
        imported = await import_obsidian(vault, output)

        assert len(imported) == 2  # .obsidian skipped
        content = (output / "Daily Note.md").read_text(encoding="utf-8")
        assert "imported_from: obsidian" in content
        assert "[[Project Plan]]" not in content  # converted
        assert "[Project Plan]" in content

    @pytest.mark.asyncio
    async def test_skip_obsidian_config(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian" / "settings.md").write_text("config", encoding="utf-8")
        (vault / "real-note.md").write_text("# Real\n\nContent", encoding="utf-8")

        output = tmp_path / "out"
        imported = await import_obsidian(vault, output)
        assert len(imported) == 1
        assert imported[0].name == "real-note.md"


class TestSafeExtractZip:
    """Resource + traversal caps on ZIP extraction (decompression-bomb defense)."""

    @staticmethod
    def _make_zip(path, entries):
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zf:
            for name, data in entries:
                zf.writestr(name, data)
        return path

    def test_extracts_normal_zip(self, tmp_path):
        z = self._make_zip(tmp_path / "ok.zip", [("a.md", "hi"), ("sub/b.md", "yo")])
        dest = tmp_path / "out"
        dest.mkdir()
        safe_extract_zip(z, dest)
        assert (dest / "a.md").read_text() == "hi"
        assert (dest / "sub" / "b.md").read_text() == "yo"

    def test_rejects_parent_traversal_member(self, tmp_path):
        z = self._make_zip(tmp_path / "trav.zip", [("../escape.md", "x")])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(z, dest)
        assert not (tmp_path / "escape.md").exists()

    def test_rejects_absolute_member(self, tmp_path):
        z = self._make_zip(tmp_path / "abs.zip", [("/abs_escape.md", "x")])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(z, dest)

    def test_rejects_too_many_entries(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importers, "_ZIP_MAX_ENTRIES", 1)
        z = self._make_zip(tmp_path / "many.zip", [("a.md", "x"), ("b.md", "y")])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(z, dest)

    def test_rejects_oversize_aggregate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importers, "_ZIP_MAX_TOTAL_BYTES", 10)
        z = self._make_zip(tmp_path / "big.zip", [("a.md", "x" * 100)])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(z, dest)

    def test_rejects_oversize_member(self, tmp_path, monkeypatch):
        monkeypatch.setattr(importers, "_ZIP_MAX_MEMBER_BYTES", 10)
        z = self._make_zip(tmp_path / "bigm.zip", [("a.md", "x" * 100)])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(z, dest)

    def test_rejects_compression_bomb_ratio(self, tmp_path, monkeypatch):
        # Floor to 0 so the small fixture is ratio-checked; cap at 5:1. A
        # highly repetitive payload deflates far past that.
        monkeypatch.setattr(importers, "_ZIP_RATIO_FLOOR_BYTES", 0)
        monkeypatch.setattr(importers, "_ZIP_MAX_RATIO", 5)
        z = self._make_zip(tmp_path / "bomb.zip", [("a.md", "A" * 100_000)])
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(z, dest)

    def test_member_filter_skips_unmatched_members(self, tmp_path, monkeypatch):
        # A would-be-oversized attachment is neither size-checked nor written
        # when the filter excludes it; the matched markdown still extracts.
        # (This is how import_notion ignores large Notion attachments instead
        # of extracting them or rejecting the whole export.)
        monkeypatch.setattr(importers, "_ZIP_MAX_MEMBER_BYTES", 50)
        z = self._make_zip(
            tmp_path / "mixed.zip",
            [("note.md", "hello"), ("big.bin", "x" * 1000)],
        )
        dest = tmp_path / "out"
        dest.mkdir()
        safe_extract_zip(z, dest, member_filter=lambda n: n.lower().endswith(".md"))
        assert (dest / "note.md").read_text() == "hello"
        assert not (dest / "big.bin").exists()  # attachment skipped, not extracted

    def test_rejects_root_entry_member(self, tmp_path):
        # A file member naming the extraction root itself (e.g. ".") is rejected
        # as UnsafeArchiveError, not a late zipfile error.
        zpath = tmp_path / "root.zip"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr(zipfile.ZipInfo("."), "x")
        dest = tmp_path / "out"
        dest.mkdir()
        with pytest.raises(UnsafeArchiveError):
            safe_extract_zip(zpath, dest)


def _chunk(path):
    from memtomem.chunking.markdown import MarkdownChunker

    return MarkdownChunker().chunk_file(path, path.read_text(encoding="utf-8"))


class TestImportProvenanceHeader:
    """The provenance keys join a note's own frontmatter instead of preceding it (#2716)."""

    NOTE = "---\ntags: [alpha, beta]\n---\n\n## Notes\n\nSee [[api-redesign|the API project]].\n"

    @pytest.mark.asyncio
    async def test_obsidian_note_keeps_its_frontmatter_tags(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text(self.NOTE, encoding="utf-8")

        await import_obsidian(vault, tmp_path / "out")

        written = tmp_path / "out" / "note.md"
        assert written.read_text(encoding="utf-8") == (
            "---\nimported_from: obsidian\noriginal_file: note.md\ntags: [alpha, beta]\n---\n\n"
            "## Notes\n\nSee [the API project](api-redesign.md).\n"
        )
        chunks = _chunk(written)
        assert chunks
        assert [c.metadata.tags for c in chunks] == [("alpha", "beta")] * len(chunks)

    @pytest.mark.asyncio
    async def test_obsidian_note_keeps_its_validity_window(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "note.md").write_text(
            "---\nvalid_to: 2020-01-01\n---\n\n## Notes\n\nbody\n", encoding="utf-8"
        )

        await import_obsidian(vault, tmp_path / "out")

        chunks = _chunk(tmp_path / "out" / "note.md")
        assert chunks
        assert all(c.metadata.valid_to_unix is not None for c in chunks)

    @pytest.mark.asyncio
    async def test_notion_directory_note_keeps_its_frontmatter_tags(self, tmp_path):
        export = tmp_path / "export"
        export.mkdir()
        (export / "Page abc123def4567890123456789abcdef0.md").write_text(
            "---\ntags: [alpha]\n---\n\n# Page\n\nbody\n", encoding="utf-8"
        )

        await import_notion(export, tmp_path / "out")

        written = tmp_path / "out" / "Page.md"
        assert written.read_text(encoding="utf-8").startswith(
            "---\nimported_from: notion\noriginal_file: Page abc123def4567890123456789abcdef0.md\n"
            "tags: [alpha]\n---\n"
        )
        assert {c.metadata.tags for c in _chunk(written)} == {("alpha",)}

    @pytest.mark.asyncio
    async def test_notion_zip_crlf_frontmatter_is_merged(self, tmp_path):
        zip_path = tmp_path / "export.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.writestr("Page.md", "---\r\ntags: [alpha]\r\n---\r\n\r\n# Page\r\n\r\nbody\r\n")

        await import_notion(zip_path, tmp_path / "out")

        written = tmp_path / "out" / "Page.md"
        assert written.read_text(encoding="utf-8").startswith(
            "---\nimported_from: notion\noriginal_file: Page.md\ntags: [alpha]\n---\n"
        )
        assert {c.metadata.tags for c in _chunk(written)} == {("alpha",)}

    @pytest.mark.asyncio
    async def test_note_without_frontmatter_gets_the_old_header(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "Daily Note.md").write_text("# Daily\n\nbody\n", encoding="utf-8")

        await import_obsidian(vault, tmp_path / "out")

        assert (tmp_path / "out" / "Daily Note.md").read_text(encoding="utf-8") == (
            "---\nimported_from: obsidian\noriginal_file: Daily Note.md\n---\n\n# Daily\n\nbody\n"
        )


class TestWithImportHeader:
    """Defined outcomes of ``_with_import_header`` for frontmatter shapes (#2716)."""

    @staticmethod
    def _frontmatter(text):
        import yaml
        from memtomem.chunking.markdown import _FRONT_MATTER_RE

        return yaml.safe_load(_FRONT_MATTER_RE.match(text).group(1))

    @pytest.mark.parametrize(
        "name",
        [
            "a: b.md",
            "#x.md",
            "yes",
            "- x.md",
            "한글 노트.md",
            "[x].md",
            "x\u2028y.md",
            "😀.md",
            "x\n😀.md",
            "x\u2028😀 한글.md",
            "a\x85b\r\nc\x0bd\x1ce.md",
        ],
    )
    def test_original_file_round_trips(self, name):
        for content in ("body\n", "---\ntags: [a]\n---\nbody\n"):
            meta = self._frontmatter(importers._with_import_header(content, "obsidian", name))
            assert meta["original_file"] == name
            assert meta["imported_from"] == "obsidian"

    def test_line_break_in_name_cannot_add_frontmatter_lines(self):
        from memtomem.chunking.markdown import MarkdownChunker
        from memtomem.indexing.redaction_exemption import declared_exemption
        from pathlib import Path

        name = "x\nredaction: documents-patterns\ntags: [forged]\ny.md"
        for content in ("body\n", "---\ntitle: t\n---\nbody\n"):
            out = importers._with_import_header(content, "notion", name)
            assert self._frontmatter(out)["original_file"] == name
            assert declared_exemption(Path("n.md"), out) is None
            assert MarkdownChunker._extract_frontmatter_tags(out) == []

    @pytest.mark.parametrize(
        "content",
        [
            # The chunker reads this block as "---\ntags...": an inserted line
            # in front would close the block early.
            "---\n---\ntags: [alpha]\nvalid_to: 2020-01-01\n---\nbody\n",
            # Same overlap with an empty mapping, so the merged key list
            # still matches and only the chunker's view differs: the note's
            # "---" and "{}" lines would become body text.
            "---\n---\n{}\n---\nbody\n",
            # Valid mappings that column-0 lines in front would break.
            "---\n{original: x, tags: [alpha]}\n---\nbody\n",
            "---\n  tags: [alpha]\n  x: 1\n---\nbody\n",
            "---\n{\nredaction: documents-patterns\n}\n---\nbody\n",
            # Not a mapping, or not readable as YAML.
            "---\n- a\n- b\n---\nbody\n",
            "---\ntags: [alpha\n---\nbody\n",
            "---\n<<: {tags: [a]}\n---\nbody\n",
            "---\n? [a]\n: 1\n---\nbody\n",
            # Composing raises a bare ValueError, not a YAMLError.
            '---\nv: "\\U00110000"\n---\nbody\n',
            "---\n%YAML " + "1" * 4301 + ".1\n---\nbody\n",
            "---\n" + "[" * 5000 + "]" * 5000 + "\n---\nbody\n",
        ],
    )
    def test_unverifiable_block_is_kept_unchanged(self, content):
        assert importers._with_import_header(content, "notion", "n.md") == content

    @pytest.mark.parametrize("entry", ['d: !!int ""', "d: !!bool nope", "d: !!timestamp nope"])
    def test_tag_that_fails_to_construct_is_merged_without_constructing(self, entry):
        content = f"---\n{entry}\n---\nbody\n"
        out = importers._with_import_header(content, "notion", "n.md")
        assert out == f"---\nimported_from: notion\noriginal_file: n.md\n{entry}\n---\nbody\n"

    def test_merge_chain_is_not_expanded(self):
        lines = ["a0: &a0 {x: 1}"] + [
            f"a{i}: &a{i} {{<<: [*a{i - 1}, *a{i - 1}]}}" for i in range(1, 60)
        ]
        content = "---\n" + "\n".join(lines) + "\n---\nbody\n"
        out = importers._with_import_header(content, "notion", "n.md")
        assert out.startswith("---\nimported_from: notion\noriginal_file: n.md\na0:")

    def test_redaction_declaration_still_accepted(self):
        from memtomem.indexing.redaction_exemption import declared_exemption
        from pathlib import Path

        content = "---\nredaction: documents-patterns\n---\nbody\n"
        out = importers._with_import_header(content, "obsidian", "n.md")
        assert out != content
        assert declared_exemption(Path("n.md"), out) == declared_exemption(Path("n.md"), content)
        assert declared_exemption(Path("n.md"), out) is not None

    @pytest.mark.parametrize(
        "existing",
        [
            "imported_from: notion",
            '"imported_from": notion',
            "!!str imported_from: notion",
            "? imported_from\n: notion",
        ],
    )
    def test_existing_key_is_not_duplicated(self, existing):
        content = f"---\n{existing}\n---\nbody\n"
        out = importers._with_import_header(content, "obsidian", "n.md")
        assert out == f"---\noriginal_file: n.md\n{existing}\n---\nbody\n"

    def test_both_keys_present_is_unchanged(self):
        content = "---\nimported_from: notion\noriginal_file: old.md\n---\nbody\n"
        assert importers._with_import_header(content, "obsidian", "n.md") == content

    def test_key_lookalike_inside_a_scalar_is_not_a_key(self):
        content = '---\nnote: "x\noriginal_file: fake.md"\n---\nbody\n'
        out = importers._with_import_header(content, "obsidian", "n.md")
        assert out == "---\nimported_from: obsidian\noriginal_file: n.md\n" + content[4:]

    @pytest.mark.parametrize("content", ["body\n", "---\ntags: [a]\n---\nbody\n"])
    @pytest.mark.parametrize(
        "serialise",
        [
            lambda fields: "".join(f"{k}: {v}x\n" for k, v in fields.items()),
            lambda fields: "".join(f"{k}_x: {v}\n" for k, v in fields.items()),
        ],
        ids=["wrong-value", "wrong-key"],
    )
    def test_keys_that_do_not_read_back_are_omitted(self, content, serialise, monkeypatch):
        monkeypatch.setattr(importers, "_provenance_lines", serialise)
        assert importers._with_import_header(content, "obsidian", "n.md") == content

    def test_unterminated_opener_gets_the_old_header(self):
        content = "---\ntags: [a]\nbody\n"
        out = importers._with_import_header(content, "obsidian", "n.md")
        assert out == "---\nimported_from: obsidian\noriginal_file: n.md\n---\n\n" + content

    def test_empty_block_receives_the_keys(self):
        content = "---\n\n---\nbody\n"
        out = importers._with_import_header(content, "obsidian", "n.md")
        assert self._frontmatter(out) == {"imported_from": "obsidian", "original_file": "n.md"}

    @pytest.mark.asyncio
    async def test_unverifiable_note_does_not_stop_the_batch(self, tmp_path):
        vault = tmp_path / "vault"
        vault.mkdir()
        (vault / "a.md").write_text('---\nv: "\\U00110000"\n---\nbody\n', encoding="utf-8")
        (vault / "b.md").write_text("---\ntags: [b]\n---\nbody\n", encoding="utf-8")

        imported = await import_obsidian(vault, tmp_path / "out")

        assert sorted(p.name for p in imported) == ["a.md", "b.md"]
        assert (tmp_path / "out" / "a.md").read_text(encoding="utf-8") == (
            '---\nv: "\\U00110000"\n---\nbody\n'
        )
