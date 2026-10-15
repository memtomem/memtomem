"""Tests for Notion and Obsidian importers."""

import zipfile

import pytest
from memtomem.indexing import importers
from memtomem.indexing.importers import (
    UnsafeArchiveError,
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
