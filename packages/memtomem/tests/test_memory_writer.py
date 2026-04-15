"""Tests for tools/memory_writer.py — markdown append/replace/remove helpers."""

from __future__ import annotations

import pytest

from memtomem.tools.memory_writer import (
    _validate_line_range,
    append_entry,
    remove_lines,
    replace_lines,
)


class TestAppendEntry:
    def test_creates_new_file_with_parent_dirs(self, tmp_path):
        target = tmp_path / "nested" / "dir" / "notes.md"

        append_entry(target, "hello world")

        assert target.exists()
        text = target.read_text(encoding="utf-8")
        assert "hello world" in text
        assert text.lstrip().startswith("## Entry ")  # default heading

    def test_appends_to_existing_file_without_clobbering(self, tmp_path):
        target = tmp_path / "notes.md"
        target.write_text("# Existing\n\nOld content.\n", encoding="utf-8")

        append_entry(target, "new entry", title="New")

        text = target.read_text(encoding="utf-8")
        assert "Old content." in text
        assert "## New" in text
        assert "new entry" in text
        # Original content must come first.
        assert text.index("Old content.") < text.index("new entry")

    def test_skips_heading_when_content_already_starts_with_h2(self, tmp_path):
        target = tmp_path / "notes.md"

        append_entry(target, "## Inline Heading\n\nbody text")

        text = target.read_text(encoding="utf-8")
        # Should not double up: no "## Entry " heading injected before the content.
        assert "## Entry " not in text
        assert "## Inline Heading" in text
        assert "body text" in text

    def test_tags_rendered_when_provided(self, tmp_path):
        target = tmp_path / "notes.md"

        append_entry(target, "content", title="T", tags=["alpha", "beta"])

        text = target.read_text(encoding="utf-8")
        assert "tags: ['alpha', 'beta']" in text

    def test_no_tags_line_when_tags_omitted(self, tmp_path):
        target = tmp_path / "notes.md"

        append_entry(target, "content", title="T")

        text = target.read_text(encoding="utf-8")
        assert "tags:" not in text


class TestValidateLineRange:
    def test_valid_range_raises_nothing(self):
        _validate_line_range(1, 5, 10)  # should not raise

    def test_start_below_one_raises(self):
        with pytest.raises(ValueError, match="start_line must be >= 1"):
            _validate_line_range(0, 5, 10)

    def test_start_greater_than_end_raises(self):
        with pytest.raises(ValueError, match="must be <="):
            _validate_line_range(5, 3, 10)

    def test_end_beyond_total_raises(self):
        with pytest.raises(ValueError, match="exceeds file length"):
            _validate_line_range(1, 11, 10)

    def test_single_line_range_is_valid(self):
        _validate_line_range(3, 3, 10)  # should not raise


class TestReplaceLines:
    def test_replaces_middle_lines(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\nd\n", encoding="utf-8")

        replace_lines(target, 2, 3, "X\nY")

        assert target.read_text(encoding="utf-8") == "a\nX\nY\nd\n"

    def test_replaces_beginning(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        replace_lines(target, 1, 1, "first")

        assert target.read_text(encoding="utf-8") == "first\nb\nc\n"

    def test_replaces_end(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        replace_lines(target, 3, 3, "last")

        assert target.read_text(encoding="utf-8") == "a\nb\nlast\n"

    def test_preserves_absence_of_trailing_newline(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc", encoding="utf-8")  # no trailing \n

        replace_lines(target, 2, 2, "Z")

        result = target.read_text(encoding="utf-8")
        assert result == "a\nZ\nc"
        assert not result.endswith("\n")

    def test_invalid_range_raises_and_leaves_file_intact(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\n", encoding="utf-8")

        with pytest.raises(ValueError):
            replace_lines(target, 1, 5, "X")
        # File is left unchanged on validation error.
        assert target.read_text(encoding="utf-8") == "a\nb\n"


class TestRemoveLines:
    def test_removes_middle_lines(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\nd\n", encoding="utf-8")

        remove_lines(target, 2, 3)

        assert target.read_text(encoding="utf-8") == "a\nd\n"

    def test_removes_beginning(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        remove_lines(target, 1, 1)

        assert target.read_text(encoding="utf-8") == "b\nc\n"

    def test_removes_end(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        remove_lines(target, 3, 3)

        assert target.read_text(encoding="utf-8") == "a\nb\n"

    def test_removing_all_lines_leaves_empty_file(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\n", encoding="utf-8")

        remove_lines(target, 1, 2)

        assert target.read_text(encoding="utf-8") == ""

    def test_preserves_absence_of_trailing_newline(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc", encoding="utf-8")  # no trailing \n

        remove_lines(target, 2, 2)

        result = target.read_text(encoding="utf-8")
        assert result == "a\nc"
        assert not result.endswith("\n")
