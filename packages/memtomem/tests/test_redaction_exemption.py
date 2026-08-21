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
            ("no_space", "---\nredaction:documents-patterns\n---\n"),
            ("tab", "---\nredaction:\tdocuments-patterns\n---\n"),
            ("trailing_space", "---\nredaction: documents-patterns   \n---\n"),
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
            ("indented", "---\n  redaction: documents-patterns\n---\n"),
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
        ],
    )
    def test_not_declared(self, label: str, content: str) -> None:
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
