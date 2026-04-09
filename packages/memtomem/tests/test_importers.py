"""Tests for Notion and Obsidian importers."""

import pytest
from memtomem.indexing.importers import (
    _clean_notion_filename,
    _clean_notion_markdown,
    _convert_obsidian_syntax,
    import_notion,
    import_obsidian,
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
            "# My Page\n\nSome content here.", encoding="utf-8",
        )
        (notion_dir / "Sub").mkdir()
        (notion_dir / "Sub" / "Child def4567890123456789012345abcdef0.md").write_text(
            "# Child Page\n\nNested content.", encoding="utf-8",
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
        content = imported[0].read_text()
        assert "imported_from: notion" in content

    @pytest.mark.asyncio
    async def test_import_zip(self, tmp_path):
        import zipfile

        notion_dir = tmp_path / "notion_src"
        notion_dir.mkdir()
        (notion_dir / "Note abc123def4567890123456789abcdef0.md").write_text(
            "# Note\n\nContent.", encoding="utf-8",
        )

        zip_path = tmp_path / "notion.zip"
        with zipfile.ZipFile(zip_path, "w") as zf:
            zf.write(notion_dir / "Note abc123def4567890123456789abcdef0.md",
                     "Note abc123def4567890123456789abcdef0.md")

        output = tmp_path / "output"
        imported = await import_notion(zip_path, output)
        assert len(imported) == 1


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
            "# Project Plan\n\n![[Architecture Diagram]]", encoding="utf-8",
        )
        # Create .obsidian config (should be skipped)
        (vault / ".obsidian").mkdir()
        (vault / ".obsidian" / "config.json").write_text("{}", encoding="utf-8")

        output = tmp_path / "output"
        imported = await import_obsidian(vault, output)

        assert len(imported) == 2  # .obsidian skipped
        content = (output / "Daily Note.md").read_text()
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
