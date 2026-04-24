"""Pin ``append_entry`` output to the canonical blockquote tag format.

A regression that reverts to the legacy lazy-continuation form
(``tags: [...]`` without ``> `` prefix) or to Python ``repr()``
(single-quoted) breaks the chunker contract documented in
``planning/mem-add-tags-blockquote-promote-rfc.md``. This test makes
either drift fail loudly.
"""

import json
from pathlib import Path

from memtomem.tools.memory_writer import append_entry


class TestAppendEntryTagFormat:
    def test_tag_line_uses_explicit_blockquote_prefix(self, tmp_path: Path):
        """``> tags:`` line carries an explicit ``> `` prefix."""
        f = tmp_path / "out.md"
        append_entry(f, "Body text.", title="Heading", tags=["a", "b"])
        text = f.read_text(encoding="utf-8")
        # Must contain the canonical line verbatim.
        assert '> tags: ["a", "b"]' in text
        # Legacy lazy-continuation form must NOT appear.
        assert "\ntags: [" not in text
        assert "\ntags: '" not in text

    def test_tags_serialized_as_json(self, tmp_path: Path):
        """JSON (double-quoted) form, not Python ``repr()`` (single-quoted)."""
        f = tmp_path / "out.md"
        append_entry(f, "Body.", title="H", tags=["cache", "shared-from=abc"])
        text = f.read_text(encoding="utf-8")
        assert '> tags: ["cache", "shared-from=abc"]' in text
        # Python repr would produce single quotes; reject.
        assert "'cache'" not in text

    def test_tags_round_trip_via_json(self, tmp_path: Path):
        """The emitted tag list parses cleanly as JSON."""
        f = tmp_path / "out.md"
        tags_in = ["alpha", "beta", "shared-from=xyz", "with space"]
        append_entry(f, "Body.", title="H", tags=tags_in)
        text = f.read_text(encoding="utf-8")
        # Locate the tag line and parse the bracketed value.
        line = next(line for line in text.splitlines() if line.startswith("> tags: "))
        value = line.split("> tags: ", 1)[1]
        assert json.loads(value) == tags_in

    def test_no_tag_line_when_tags_empty(self, tmp_path: Path):
        """Empty / None tags omit the tag line entirely (today's behavior)."""
        f = tmp_path / "out.md"
        append_entry(f, "Body.", title="H", tags=None)
        assert "tags:" not in f.read_text(encoding="utf-8")

        f2 = tmp_path / "out2.md"
        append_entry(f2, "Body.", title="H", tags=[])
        assert "tags:" not in f2.read_text(encoding="utf-8")

    def test_created_line_carries_blockquote_prefix(self, tmp_path: Path):
        """``> created:`` keeps its prefix unchanged (no regression there)."""
        f = tmp_path / "out.md"
        append_entry(f, "Body.", title="H", tags=["x"])
        text = f.read_text(encoding="utf-8")
        assert "\n> created: " in text
