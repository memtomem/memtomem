"""The frontmatter parser behind the per-file redaction exemption (#2076).

``declared_exemption`` decides whether a file may waive the guard's label
rules, so every "is this a declaration?" answer is a security answer. The
matrix below is the attack surface, pinned: the parser must recognise exactly
one unindented top-level ``redaction: documents-patterns`` key inside the
leading frontmatter block, and fail closed on everything else — most
importantly on a key that is *nested* inside another field's value, where
arbitrary prose (a note quoting this very documentation) would otherwise forge
a declaration for the file that contains it.

The value is never echoed: an unrecognised one could itself be a credential.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from memtomem import privacy
from memtomem.indexing.redaction_exemption import declared_exemption

_DECL = "documents-patterns"


class TestRecognisedDeclarations:
    @pytest.mark.parametrize(
        ("label", "content"),
        [
            ("bare", "---\nredaction: documents-patterns\n---\n# doc\n"),
            ("crlf", "---\r\nredaction: documents-patterns\r\n---\r\n# doc\r\n"),
            ("bom", "﻿---\nredaction: documents-patterns\n---\n# doc\n"),
            ("trailing_space", "---\nredaction: documents-patterns   \n---\n"),
            # A folded scalar ends at the dedent, so this ``redaction`` really
            # is a sibling top-level key.
            ("after_folded_scalar", "---\ndesc: >\n  x\nredaction: documents-patterns\n---\n"),
            # Real notes carry other frontmatter. Each of these puts a
            # collection at depth 1, which the depth-1 alternation has to
            # resume from correctly — getting it backwards silently misreads
            # every entry after the first ``tags: [...]``.
            (
                "after_flow_sequence",
                "---\ntags: [a, b]\nvalid_from: 2026-01-01\nredaction: documents-patterns\n---\n",
            ),
            (
                "after_block_sequence",
                "---\ntags:\n  - a\n  - b\nredaction: documents-patterns\n---\n",
            ),
            ("after_nested_mapping", "---\nmeta:\n  a: 1\nredaction: documents-patterns\n---\n"),
            ("before_block_sequence", "---\nredaction: documents-patterns\ntags:\n  - a\n---\n"),
            (
                "beside_other_keys",
                "---\nname: note\ntags: [a, b]\nredaction: documents-patterns\n---\n",
            ),
        ],
    )
    def test_declared(self, label: str, content: str) -> None:
        assert declared_exemption(Path("note.md"), content) == _DECL

    def test_markdown_suffix_variants(self) -> None:
        content = "---\nredaction: documents-patterns\n---\n"
        assert declared_exemption(Path("n.markdown"), content) == _DECL
        assert declared_exemption(Path("N.MD"), content) == _DECL


class TestFailClosed:
    @pytest.mark.parametrize(
        ("label", "content"),
        [
            # The load-bearing one: a block scalar / nested map means the key
            # belongs to *another field's value*, not to the document.
            ("block_scalar", "---\ndescription: |\n  redaction: documents-patterns\n---\n"),
            ("nested_map", "---\nmeta:\n  redaction: documents-patterns\n---\n"),
            # No space after the colon: YAML reads the whole line as one plain
            # scalar, so the document is not a mapping and declares nothing.
            ("no_space_after_colon", "---\nredaction:documents-patterns\n---\n"),
            ("tab_after_colon", "---\nredaction:\tdocuments-patterns\n---\n"),
            ("quoted", '---\nredaction: "documents-patterns"\n---\n'),
            ("single_quoted", "---\nredaction: 'documents-patterns'\n---\n"),
            ("commented", "---\n# redaction: documents-patterns\n---\n"),
            ("wrong_value", "---\nredaction: true\n---\n"),
            ("empty_value", "---\nredaction:\n---\n"),
            ("underscored", "---\nredaction: documents_patterns\n---\n"),
            ("trailing_junk", "---\nredaction: documents-patterns yes\n---\n"),
            ("no_frontmatter", "redaction: documents-patterns\n\n# doc\n"),
            ("unterminated", "---\nredaction: documents-patterns\n\n# doc\n"),
            ("not_leading", "# doc\n\n---\nredaction: documents-patterns\n---\n"),
            ("other_keys_only", "---\ntags: [a]\n---\n"),
            ("malformed_yaml", "---\nredaction: documents-patterns\n  bad: [\n---\n"),
            ("sequence_root", "---\n- redaction: documents-patterns\n---\n"),
            # Root-level to YAML, but not the shape this feature documents:
            # the contract says top-level *and unindented*, and the column
            # check is what keeps the two from drifting apart.
            ("uniformly_indented", "---\n  redaction: documents-patterns\n---\n"),
            ("flow_root_mapping", "---\n{redaction: documents-patterns}\n---\n"),
            # ``...`` is a YAML document-end marker, but this repository's
            # frontmatter contract is ``---`` (chunking/markdown.py). A second
            # grammar known only to this module would let a file declare an
            # exemption while every other reader sees no frontmatter at all.
            ("yaml_document_end_close", "---\nredaction: documents-patterns\n...\n"),
        ],
    )
    def test_not_declared(self, label: str, content: str) -> None:
        assert declared_exemption(Path("note.md"), content) is None

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            # The regression that retired the line-matching parser: a
            # multi-line **double**-quoted scalar carries its continuation
            # lines at column zero, so "unindented" did not mean "top-level".
            # There is no top-level ``redaction`` key here at all, and the
            # `password:` line below it must stay blocked.
            (
                "double_quoted_continuation",
                '---\ndescription: "first line\nredaction: documents-patterns\nlast"\n'
                "password: hunter2\n---\n\nbody\n",
            ),
            (
                "single_quoted_continuation",
                "---\ndesc: 'a\nredaction: documents-patterns\nb'\n---\n",
            ),
            (
                "flow_mapping_continuation",
                "---\nmeta: {a: 1,\nredaction: documents-patterns}\n---\n",
            ),
            # The closing delimiter must be a complete line. The chunker's
            # frontmatter regex accepts ``---suffix`` as a close (right for
            # recovering what it can; wrong for a gate), so this parser
            # anchors its own.
            (
                "suffixed_closing_delimiter",
                "---\nredaction: documents-patterns\n---not-a-delimiter\n\napi_key=xyz\n",
            ),
        ],
    )
    def test_a_key_that_is_not_a_top_level_key_declares_nothing(
        self, label: str, content: str
    ) -> None:
        assert declared_exemption(Path("note.md"), content) is None

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            # ``compose`` resolves aliases and escapes before anything can
            # look at them, so an earlier revision saw each of these as the
            # literal. The declaration has to be the shape a reviewer opening
            # the file can recognise, not any shape that evaluates to it.
            (
                "alias_as_the_key",
                "---\nkeyname: &k redaction\n*k: documents-patterns\npassword: hunter2\n---\n",
            ),
            (
                "alias_as_the_value",
                "---\nkind: &v documents-patterns\nredaction: *v\npassword: hunter2\n---\n",
            ),
            ("escaped_key", '---\n"\\x72edaction": documents-patterns\npassword: hunter2\n---\n'),
            ("quoted_key", '---\n"redaction": documents-patterns\n---\n'),
            (
                "declaration_inside_a_sequence",
                "---\ntags:\n  - redaction: documents-patterns\n---\n",
            ),
        ],
    )
    def test_only_the_written_literal_declares(self, label: str, content: str) -> None:
        assert declared_exemption(Path("note.md"), content) is None

    def test_deeply_nested_frontmatter_does_not_crash_the_indexer(self) -> None:
        # A gate must answer "no" (or, here, read the legitimate key it can
        # see) rather than propagate a parser failure and fail the whole run
        # for a file it merely could not read. The composing parser raised
        # RecursionError past ~500 levels; the event parser is iterative.
        content = "---\nredaction: documents-patterns\nx: " + "[" * 600 + "]" * 600 + "\n---\n"
        assert declared_exemption(Path("note.md"), content) == _DECL

    def test_duplicate_keys_are_ambiguous(self) -> None:
        # YAML's last-wins is not a rule worth guessing at when the answer
        # gates a secret scan; two keys mean no declaration.
        content = "---\nredaction: documents-patterns\nredaction: documents-patterns\n---\n"
        assert declared_exemption(Path("note.md"), content) is None

    def test_duplicate_with_one_valid_key_still_refuses(self) -> None:
        content = "---\nredaction: nonsense\nredaction: documents-patterns\n---\n"
        assert declared_exemption(Path("note.md"), content) is None

    @pytest.mark.parametrize("suffix", [".yaml", ".yml", ".json", ".rst", ".txt", ".py"])
    def test_non_markdown_never_declares(self, suffix: str) -> None:
        # A ``.yaml`` config whose first lines happen to look like frontmatter
        # is not a Markdown document and has no declaration surface.
        content = "---\nredaction: documents-patterns\n---\n"
        assert declared_exemption(Path(f"conf{suffix}"), content) is None


class TestNeverEchoesTheValue:
    def test_unrecognised_value_is_not_logged(self, caplog) -> None:
        secret = "hf" + "_FAKEfake0123456789FAKEfake01234567"
        content = f"---\nredaction: {secret}\n---\n"
        with caplog.at_level(logging.WARNING):
            assert declared_exemption(Path("note.md"), content) is None
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "unrecognised declaration" in logged
        assert secret not in logged
        assert f"value_chars={len(secret)}" in logged

    def test_secret_shaped_path_is_sanitized(self, caplog) -> None:
        # The path is user-controlled too, and the audit sanitizer is what
        # keeps a secret-shaped filename out of this log line.
        path = Path("api_key=AKIATESTKEY1234567890.md")
        content = "---\nredaction: nope\n---\n"
        with caplog.at_level(logging.WARNING):
            assert declared_exemption(path, content) is None
        logged = "\n".join(r.getMessage() for r in caplog.records)
        assert "AKIATESTKEY1234567890" not in logged
        assert privacy._AUDIT_REDACTED_MARKER in logged
