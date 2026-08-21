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
            # Boundary parity with ``chunking/markdown.py``, which requires a
            # bare ``---`` line at offset 0 and normalises nothing. In all
            # three of these the chunker sees *no frontmatter at all*, so
            # honouring a declaration would be honouring body text — the same
            # defect as the mid-file boundary case, at the other end.
            ("opening_with_trailing_spaces", "---   \nredaction: documents-patterns\n---\n"),
            ("byte_order_mark", "\ufeff---\nredaction: documents-patterns\n---\n"),
            ("crlf_line_endings", "---\r\nredaction: documents-patterns\r\n---\r\n"),
            # A collection value is never the literal, but it is still a
            # second ``redaction`` key: dropping it made the pair look
            # unambiguous, and in this order YAML's effective value is ``{}``.
            (
                "duplicate_with_collection_first",
                "---\nredaction: {}\nredaction: documents-patterns\n---\n",
            ),
            (
                "duplicate_with_collection_second",
                "---\nredaction: documents-patterns\nredaction: {}\n---\n",
            ),
            (
                "duplicate_with_sequence_first",
                "---\nredaction: [a]\nredaction: documents-patterns\n---\n",
            ),
            ("collection_value", "---\nredaction: {}\n---\n"),
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

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            # ``!!str redaction`` and friends resolve to the same string, and
            # an anchored scalar can be aliased into the declaration position
            # from elsewhere in the block. Plain style alone does not rule
            # either out — the event carries ``tag`` and ``anchor`` separately.
            ("tagged_key", "---\n!!str redaction: documents-patterns\n---\n"),
            ("custom_tagged_key", "---\n!custom redaction: documents-patterns\n---\n"),
            ("anchored_key", "---\n&a redaction: documents-patterns\n---\n"),
            ("tagged_value", "---\nredaction: !!str documents-patterns\n---\n"),
            ("anchored_value", "---\nredaction: &x documents-patterns\n---\n"),
        ],
    )
    def test_tags_and_anchors_do_not_declare(self, label: str, content: str) -> None:
        assert declared_exemption(Path("note.md"), content) is None

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            # ``yaml.parse`` checks syntax only, so each of these yields a
            # clean event stream while no loader could construct the document.
            # The contract says malformed frontmatter declares nothing.
            ("alias_with_no_anchor", "---\nmeta: *missing\nredaction: documents-patterns\n---\n"),
            ("merge_key_with_no_anchor", "---\n<<: *missing\nredaction: documents-patterns\n---\n"),
            ("second_document", "---\nname: n\n---\nredaction: documents-patterns\n---\n"),
        ],
    )
    def test_unloadable_frontmatter_declares_nothing(self, label: str, content: str) -> None:
        assert declared_exemption(Path("note.md"), content) is None

    def test_the_block_ends_where_the_chunker_says_it_ends(self) -> None:
        """A declaration below the chunker's boundary is body text.

        ``chunking/markdown.py`` closes the block at the first line beginning
        with ``---``, so ``---body: value`` ends it there. Scanning on to the
        next *exact* ``---`` would read the file's body as frontmatter and let
        it declare — while every other reader in the codebase sees the
        declaration as prose.
        """
        from memtomem.chunking.markdown import _FRONT_MATTER_RE

        content = (
            "---\nname: note\n---body: value\nredaction: documents-patterns\n"
            "password: hunter2\n---\n\nreal body\n"
        )
        # Pin the premise: the chunker really does stop at the short line.
        assert _FRONT_MATTER_RE.match(content).group(1) == "name: note"
        assert declared_exemption(Path("note.md"), content) is None

    @pytest.mark.parametrize(
        ("label", "content"),
        [
            ("comment_line", "---\n# a note about redaction\nredaction: documents-patterns\n---\n"),
            (
                "anchors_used_elsewhere",
                "---\nbase: &b 1\nother: *b\nredaction: documents-patterns\n---\n",
            ),
            ("empty_sibling_mapping", "---\nmeta: {}\nredaction: documents-patterns\n---\n"),
            ("empty_sibling_sequence", "---\ntags: []\nredaction: documents-patterns\n---\n"),
            ("null_sibling_value", "---\nmeta:\nredaction: documents-patterns\n---\n"),
        ],
    )
    def test_ordinary_frontmatter_around_it_still_declares(self, label: str, content: str) -> None:
        # The refusals above must not have cost the common case: these are
        # shapes real notes carry, and each puts something at depth 1 that the
        # key/value alternation has to step over correctly.
        assert declared_exemption(Path("note.md"), content) == _DECL

    def test_boundary_parity_with_the_chunker_at_both_ends(self) -> None:
        """Whatever this module reads as frontmatter, the chunker must too."""
        from memtomem.chunking.markdown import _FRONT_MATTER_RE

        divergent = [
            "---   \nredaction: documents-patterns\n---\n",
            "\ufeff---\nredaction: documents-patterns\n---\n",
            "---\r\nredaction: documents-patterns\r\n---\r\n",
        ]
        for content in divergent:
            # Pin the premise rather than trusting the comment: the chunker
            # really does see no frontmatter in these.
            assert _FRONT_MATTER_RE.match(content) is None
            assert declared_exemption(Path("note.md"), content) is None

    def test_composition_stack_overflow_fails_closed(self, monkeypatch) -> None:
        # The validation half is the recursive one, so its guard gets its own
        # pin — the event walker's handler below covers the other half.
        from memtomem.indexing import redaction_exemption as mod

        def _boom(*args, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(mod.yaml, "compose_all", _boom)
        content = "---\nredaction: documents-patterns\n---\n"
        assert declared_exemption(Path("note.md"), content) is None

    def test_a_parser_stack_overflow_fails_closed(self, monkeypatch, caplog) -> None:
        # The composing parser blew the stack past ~500 nesting levels. The
        # event parser is iterative, so this is unreachable through input
        # today — but a gate that propagates a parser failure fails the whole
        # indexing run for a file it merely could not read, so the handler is
        # pinned directly.
        import yaml

        from memtomem.indexing import redaction_exemption as mod

        def _boom(*args, **kwargs):
            raise RecursionError("maximum recursion depth exceeded")

        monkeypatch.setattr(mod.yaml, "parse", _boom)
        content = "---\nredaction: documents-patterns\n---\n"
        with caplog.at_level(logging.WARNING):
            assert declared_exemption(Path("note.md"), content) is None
        assert "too deeply nested" in caplog.text
        assert isinstance(yaml.YAMLError, type)  # import kept meaningful

    def test_deeply_nested_frontmatter_does_not_crash_the_indexer(self) -> None:
        # Composition is the recursive half (the event walk is iterative), and
        # it gives up past a few hundred levels of nesting. A gate must answer
        # "no" rather than propagate that and fail the whole indexing run for
        # a file it merely could not read — so an unreadable block declares
        # nothing, like any other malformed frontmatter.
        content = "---\nredaction: documents-patterns\nx: " + "[" * 600 + "]" * 600 + "\n---\n"
        assert declared_exemption(Path("note.md"), content) is None

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
