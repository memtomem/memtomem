"""Tests for tools/memory_writer.py — markdown append/replace/remove helpers."""

from __future__ import annotations

import pytest

import logging
import os
import shutil
from datetime import datetime

from memtomem.source_provenance import source_span_hash
from memtomem.tools.memory_writer import (
    RestoreOutcome,
    SourceRemovedError,
    SourceReplacedError,
    _validate_line_range,
    append_entry,
    format_entry_block,
    read_pre_image,
    remove_lines,
    replace_chunk_body,
    replace_lines,
    restore_pre_image_quietly,
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
        # Canonical blockquote form: explicit ``> `` prefix + JSON array.
        # Detailed pin lives in test_memory_writer_tag_format.py.
        assert '> tags: ["alpha", "beta"]' in text

    def test_no_tags_line_when_tags_omitted(self, tmp_path):
        target = tmp_path / "notes.md"

        append_entry(target, "content", title="T")

        text = target.read_text(encoding="utf-8")
        assert "tags:" not in text


class TestDefaultHeadingUniqueness:
    """Two untitled entries must never share a heading.

    ``indexing.engine._can_merge`` treats an identical ``heading_hierarchy`` as
    permission to pack two short chunks into one, so entries appended under the
    same auto-heading were folded together: the earlier entry's text was
    swallowed and the file's chunk ids were re-minted, invalidating any id
    already handed to a caller. The heading was a second-resolution timestamp,
    which two back-to-back appends share routinely.
    """

    def _headings(self, text: str) -> list[str]:
        return [line for line in text.splitlines() if line.startswith("## ")]

    def test_back_to_back_entries_get_distinct_headings(self, tmp_path):
        target = tmp_path / "notes.md"

        for i in range(5):
            append_entry(target, f"entry {i}")

        headings = self._headings(target.read_text(encoding="utf-8"))
        assert len(headings) == 5
        assert len(set(headings)) == 5

    def test_one_batch_of_blocks_gets_distinct_headings(self, tmp_path):
        # mem_batch_add composes every block in a single loop, so these are
        # written well inside one millisecond — finer resolution alone would
        # not separate them.
        blocks = [format_entry_block(f"entry {i}") for i in range(50)]
        headings = self._headings("".join(blocks))
        assert len(set(headings)) == 50

    def test_heading_suffix_keeps_the_whole_uuid(self):
        """A truncated suffix is not enough. Every block in one `mem_batch_add`
        shares a millisecond stamp, so the suffix is the only discriminator for
        up to 500 entries — at 32 bits that is roughly a 3e-5 birthday collision
        per batch, and one collision restores the merge this prevents."""
        heading = format_entry_block("body").splitlines()[1]
        suffix = heading.split()[-1]
        assert len(suffix) == 32
        int(suffix, 16)  # a full uuid4 hex, not a prefix of one

    def test_default_heading_still_carries_a_readable_timestamp(self, tmp_path):
        target = tmp_path / "notes.md"

        append_entry(target, "content")

        text = target.read_text(encoding="utf-8")
        heading = self._headings(text)[0]
        assert heading.startswith("## Entry ")
        stamp = heading.split()[2]
        assert datetime.fromisoformat(stamp).tzinfo is not None
        # the metadata line parsers read keeps its own second resolution
        created = [ln for ln in text.splitlines() if ln.startswith("> created:")][0]
        assert "." not in created.split("created:")[1]

    def test_an_explicit_title_is_still_used_verbatim(self, tmp_path):
        target = tmp_path / "notes.md"

        append_entry(target, "content", title="Cache Decision")

        assert self._headings(target.read_text(encoding="utf-8")) == ["## Cache Decision"]


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

        replace_lines(
            target,
            2,
            3,
            "X\nY",
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 2, 3
            ),
        )

        assert target.read_text(encoding="utf-8") == "a\nX\nY\nd\n"

    def test_replaces_beginning(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        replace_lines(
            target,
            1,
            1,
            "first",
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 1, 1
            ),
        )

        assert target.read_text(encoding="utf-8") == "first\nb\nc\n"

    def test_replaces_end(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        replace_lines(
            target,
            3,
            3,
            "last",
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 3, 3
            ),
        )

        assert target.read_text(encoding="utf-8") == "a\nb\nlast\n"

    def test_preserves_absence_of_trailing_newline(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc", encoding="utf-8")  # no trailing \n

        replace_lines(
            target,
            2,
            2,
            "Z",
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 2, 2
            ),
        )

        result = target.read_text(encoding="utf-8")
        assert result == "a\nZ\nc"
        assert not result.endswith("\n")

    def test_invalid_range_raises_and_leaves_file_intact(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\n", encoding="utf-8")

        with pytest.raises(ValueError):
            replace_lines(
                target,
                1,
                5,
                "X",
                expected_source_span_hash=source_span_hash(
                    target.read_text(encoding="utf-8").splitlines(), 1, 5
                ),
            )
        # File is left unchanged on validation error.
        assert target.read_text(encoding="utf-8") == "a\nb\n"


class TestRemoveLines:
    def test_removes_middle_lines(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\nd\n", encoding="utf-8")

        remove_lines(
            target,
            2,
            3,
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 2, 3
            ),
        )

        assert target.read_text(encoding="utf-8") == "a\nd\n"

    def test_removes_beginning(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        remove_lines(
            target,
            1,
            1,
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 1, 1
            ),
        )

        assert target.read_text(encoding="utf-8") == "b\nc\n"

    def test_removes_end(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc\n", encoding="utf-8")

        remove_lines(
            target,
            3,
            3,
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 3, 3
            ),
        )

        assert target.read_text(encoding="utf-8") == "a\nb\n"

    def test_removing_all_lines_leaves_empty_file(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\n", encoding="utf-8")

        remove_lines(
            target,
            1,
            2,
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 1, 2
            ),
        )

        assert target.read_text(encoding="utf-8") == ""

    def test_preserves_absence_of_trailing_newline(self, tmp_path):
        target = tmp_path / "f.md"
        target.write_text("a\nb\nc", encoding="utf-8")  # no trailing \n

        remove_lines(
            target,
            2,
            2,
            expected_source_span_hash=source_span_hash(
                target.read_text(encoding="utf-8").splitlines(), 2, 2
            ),
        )

        result = target.read_text(encoding="utf-8")
        assert result == "a\nc"
        assert not result.endswith("\n")


class TestRestorePreImage:
    """The rollback primitives: never create, never raise (#2347)."""

    def test_it_takes_bytes_and_identity_off_one_descriptor(self, tmp_path):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")

        pre = read_pre_image(src)

        info = src.stat()
        assert pre.data == b"before\n"
        assert pre.identity == (info.st_dev, info.st_ino)

    def test_it_restores_the_file_it_read(self, tmp_path):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        src.write_bytes(b"mutated, and longer than the pre-image\n")

        assert restore_pre_image_quietly(src, pre) is RestoreOutcome.restored
        # Truncate-then-write, so no tail of the longer mutation survives.
        assert src.read_bytes() == b"before\n"

    def test_it_does_not_recreate_a_removed_source(self, tmp_path):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        src.unlink()

        assert restore_pre_image_quietly(src, pre) is RestoreOutcome.source_removed
        assert not src.exists()

    def test_it_reports_a_vanished_parent_as_removed(self, tmp_path):
        # A subdirectory, not ``tmp_path`` itself: pytest still has to clean up.
        holder = tmp_path / "memories"
        holder.mkdir()
        src = holder / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        shutil.rmtree(holder)

        # Pre-#2347 this was the masking case: ``write_text`` raised ENOENT out
        # of the ``except`` arm, over the failure being rolled back.
        assert restore_pre_image_quietly(src, pre) is RestoreOutcome.source_removed
        assert not holder.exists()

    def test_it_leaves_a_replaced_source_as_found(self, tmp_path):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        replacement = tmp_path / "other.md"
        replacement.write_bytes(b"somebody else's file\n")
        os.replace(replacement, src)

        assert restore_pre_image_quietly(src, pre) is RestoreOutcome.source_replaced
        assert src.read_bytes() == b"somebody else's file\n"

    def test_it_reports_a_directory_at_the_path_as_replaced(self, tmp_path):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        src.unlink()
        src.mkdir()

        assert restore_pre_image_quietly(src, pre) is RestoreOutcome.source_replaced
        assert src.is_dir()

    def test_it_restores_on_existence_alone_when_identity_is_unanswerable(self, tmp_path):
        # st_ino == 0: the filesystem cannot answer identity. Restoring anyway
        # beats leaving the caller's half-applied mutation on disk; resurrection
        # is already ruled out by the open.
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        blind = type(pre)(data=pre.data, identity=None)
        src.write_bytes(b"mutated\n")

        assert restore_pre_image_quietly(src, blind) is RestoreOutcome.restored
        assert src.read_bytes() == b"before\n"

    def test_it_does_not_restore_over_a_removed_source_without_identity(self, tmp_path):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        blind = type(pre)(data=pre.data, identity=None)
        src.unlink()

        assert restore_pre_image_quietly(src, blind) is RestoreOutcome.source_removed
        assert not src.exists()

    @pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root writes through a read-only mode bit",
    )
    def test_it_reports_its_own_failure_instead_of_raising(self, tmp_path, caplog):
        src = tmp_path / "n.md"
        src.write_bytes(b"before\n")
        pre = read_pre_image(src)
        src.write_bytes(b"mutated\n")
        src.chmod(0o444)
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.tools.memory_writer"):
                outcome = restore_pre_image_quietly(src, pre)
        finally:
            src.chmod(0o644)

        assert outcome is RestoreOutcome.failed
        assert any(str(src) in record.getMessage() for record in caplog.records)
        assert any(record.exc_info for record in caplog.records)


def _identity_of(path):
    """The ``(st_dev, st_ino)`` a caller's pre-image would carry for *path*."""
    info = path.stat()
    return None if info.st_ino == 0 else (info.st_dev, info.st_ino)


#: The three line-range helpers, each reduced to ``call(path, *, expected_identity)``
#: so one body can drive all of them. They differ in what they compute and not at
#: all in the contract under test, which is that none of them may create.
_REWRITERS = (
    pytest.param(
        lambda path, **kw: replace_chunk_body(
            path,
            1,
            3,
            "NEW BODY",
            expected_source_span_hash=source_span_hash(_BEFORE.splitlines(), 1, 3),
            **kw,
        ),
        id="replace_chunk_body",
    ),
    pytest.param(
        lambda path, **kw: replace_lines(
            path,
            1,
            3,
            "NEW\n",
            expected_source_span_hash=source_span_hash(_BEFORE.splitlines(), 1, 3),
            **kw,
        ),
        id="replace_lines",
    ),
    pytest.param(
        lambda path, **kw: remove_lines(
            path, 1, 3, expected_source_span_hash=source_span_hash(_BEFORE.splitlines(), 1, 3), **kw
        ),
        id="remove_lines",
    ),
)

_BEFORE = "## H\n\nold body\n"


@pytest.mark.parametrize("rewrite", _REWRITERS)
class TestLineRangeHelpersNeverCreate:
    """#2367: a forward write must not resurrect (or splice) a changed source.

    ``write_text`` creates, and the span's locks bind cooperating memtomem
    writers only — so the single case these helpers could meet mid-write was
    the single case where writing was wrong. The twin of ``TestRestorePreImage``
    above, one layer earlier: there the *rollback* must not recreate, here the
    edit itself must not.
    """

    def test_it_refuses_a_source_removed_before_the_write(self, rewrite, tmp_path):
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")
        identity = _identity_of(src)
        src.unlink()

        with pytest.raises(SourceRemovedError):
            rewrite(src, expected_identity=identity)
        assert not src.exists()  # nothing was recreated

    def test_it_refuses_a_vanished_parent_as_removed(self, rewrite, tmp_path):
        holder = tmp_path / "holder"
        holder.mkdir()
        src = holder / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")
        identity = _identity_of(src)
        shutil.rmtree(holder)

        with pytest.raises(SourceRemovedError):
            rewrite(src, expected_identity=identity)
        assert not holder.exists()

    def test_it_refuses_a_directory_standing_at_the_path(self, rewrite, tmp_path):
        # POSIX answers EISDIR here and Windows EACCES; the typed error is what
        # makes this one assertion rather than a platform branch.
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")
        identity = _identity_of(src)
        src.unlink()
        src.mkdir()

        with pytest.raises(SourceReplacedError):
            rewrite(src, expected_identity=identity)
        assert src.is_dir()

    def test_it_refuses_a_replacement_and_leaves_its_bytes(self, rewrite, tmp_path):
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")
        identity = _identity_of(src)
        newcomer = tmp_path / "other.md"
        newcomer.write_text("## OTHER\n\nsomebody else's note\n", encoding="utf-8")
        os.replace(newcomer, src)

        with pytest.raises(SourceReplacedError):
            rewrite(src, expected_identity=identity)
        assert src.read_text(encoding="utf-8") == "## OTHER\n\nsomebody else's note\n"

    def test_it_edits_the_file_its_identity_names(self, rewrite, tmp_path):
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")

        rewrite(src, expected_identity=_identity_of(src))
        assert src.read_text(encoding="utf-8") != _BEFORE
        assert src.exists()

    def test_it_edits_on_existence_alone_when_identity_is_unanswerable(self, rewrite, tmp_path):
        # ``st_ino == 0`` on some FUSE/SMB mounts; the restore falls back to
        # existence there for the same reason, and refusing would leave the
        # caller unable to edit at all on those filesystems.
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")

        rewrite(src, expected_identity=None)
        assert src.read_text(encoding="utf-8") != _BEFORE

    def test_a_removed_source_is_refused_even_without_identity(self, rewrite, tmp_path):
        # The open, not the identity check, is what rules out resurrection.
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")
        src.unlink()

        with pytest.raises(SourceRemovedError):
            rewrite(src, expected_identity=None)
        assert not src.exists()

    def test_it_refuses_a_replacement_before_decoding_it(self, rewrite, tmp_path):
        # The identity check runs before the read: a replacement that is not
        # valid UTF-8 must be refused as a replacement, not raise
        # ``UnicodeDecodeError`` over the refusal.
        src = tmp_path / "n.md"
        src.write_text(_BEFORE, encoding="utf-8")
        identity = _identity_of(src)
        newcomer = tmp_path / "other.md"
        newcomer.write_bytes(b"\xff\xfe not utf-8 \xff\n")
        os.replace(newcomer, src)

        with pytest.raises(SourceReplacedError):
            rewrite(src, expected_identity=identity)
        assert src.read_bytes() == b"\xff\xfe not utf-8 \xff\n"

    def test_an_invalid_range_still_leaves_the_file_intact(self, rewrite, tmp_path):
        # The truncate happens only after the edit callback returns, so a
        # range refusal cannot empty the file it refused to edit.
        src = tmp_path / "n.md"
        src.write_text("only one line\n", encoding="utf-8")

        with pytest.raises(ValueError):
            rewrite(src, expected_identity=_identity_of(src))
        assert src.read_text(encoding="utf-8") == "only one line\n"


class TestAppendStillCreates:
    """The other half of #2367's contract: appending is *meant* to create.

    ``mem_add`` writes a note into a file that need not exist yet, so the
    refusal above is deliberately scoped to the line-range rewrites. Pinned
    here so a later sweep cannot generalise "never create" over the whole
    module.
    """

    def test_append_entry_creates_a_missing_file(self, tmp_path):
        target = tmp_path / "nested" / "d.md"

        append_entry(target, "fresh note", title="Fresh")

        assert target.exists()
        assert "fresh note" in target.read_text(encoding="utf-8")
