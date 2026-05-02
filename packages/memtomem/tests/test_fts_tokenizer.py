"""Regression tests for FTS5 query sanitization.

Covers tokens containing characters that FTS5 treats as operators or
token separators (`.`, `/`, `\\`, `<`, `>`, `~`, etc.).  Without the
full ``_FTS5_SPECIAL_RE`` character set these tokens leak into the
query parser unquoted and trigger ``fts5: syntax error``.
"""

from __future__ import annotations

import pytest

from memtomem.storage.fts_tokenizer import (
    _FTS5_SPECIAL_RE,
    _apply_prefix_wildcard,
    tokenize_for_fts,
)


# ---------------------------------------------------------------------------
# _FTS5_SPECIAL_RE: character-class coverage
# ---------------------------------------------------------------------------

class TestFTS5SpecialRegex:
    """Every FTS5 special character must be caught by the regex."""

    @pytest.mark.parametrize(
        "char",
        list('-*"()+^:\\./<>~`!@#%&=\'{}[];?'),
        ids=lambda c: f"char_{repr(c)}",
    )
    def test_special_char_detected(self, char: str) -> None:
        assert _FTS5_SPECIAL_RE.search(char), f"Character {char!r} not matched"

    def test_plain_word_not_matched(self) -> None:
        assert _FTS5_SPECIAL_RE.search("normal_word") is None

    def test_alphanumeric_not_matched(self) -> None:
        assert _FTS5_SPECIAL_RE.search("hello123") is None


# ---------------------------------------------------------------------------
# _apply_prefix_wildcard: quoting vs prefix-wildcard
# ---------------------------------------------------------------------------

class TestApplyPrefixWildcard:
    """Words with special chars must be quoted, not wildcarded."""

    def test_url_is_quoted(self) -> None:
        result = _apply_prefix_wildcard("https://example.com")
        assert result == '"https://example.com"', f"URL not quoted: {result!r}"

    def test_filesystem_path_is_quoted(self) -> None:
        result = _apply_prefix_wildcard("a/b/c")
        assert result == '"a/b/c"', f"Path not quoted: {result!r}"

    def test_dotted_filename_is_quoted(self) -> None:
        result = _apply_prefix_wildcard("file.name.ext")
        assert result == '"file.name.ext"', f"Dotted name not quoted: {result!r}"

    def test_yaml_frontmatter_is_quoted(self) -> None:
        # "key: value" splits on space: "key:" has colon (special) -> quoted,
        # "value" is plain -> wildcarded
        result = _apply_prefix_wildcard("key: value")
        assert result == '"key:" value*', f"YAML not quoted: {result!r}"

    def test_code_span_fragment_is_quoted(self) -> None:
        result = _apply_prefix_wildcard("foo->bar")
        assert result == '"foo->bar"', f"Code span not quoted: {result!r}"

    def test_tilde_proximity_is_quoted(self) -> None:
        result = _apply_prefix_wildcard("hello~world")
        assert result == '"hello~world"', f"Tilde not quoted: {result!r}"

    def test_plain_word_gets_wildcard(self) -> None:
        result = _apply_prefix_wildcard("search")
        assert result == "search*", f"Plain word not wildcarded: {result!r}"

    def test_mixed_query(self) -> None:
        result = _apply_prefix_wildcard("normal https://example.com test")
        assert result == 'normal* "https://example.com" test*'

    def test_or_joiner(self) -> None:
        result = _apply_prefix_wildcard("a/b normal", use_or=True)
        assert result == '"a/b" OR normal*'

    def test_double_quotes_escaped(self) -> None:
        # "say" is plain -> wildcarded.  "hello" has quotes -> doubled then wrapped.
        result = _apply_prefix_wildcard('say "hello"')
        assert result == 'say* """hello"""' 


# ---------------------------------------------------------------------------
# tokenize_for_fts: end-to-end unicode61 path
# ---------------------------------------------------------------------------

class TestTokenizeForFTS:
    """Integration tests for the unicode61 query path."""

    def test_url_query(self) -> None:
        result = tokenize_for_fts("https://example.com/path", for_query=True)
        assert result == '"https://example.com/path"'

    def test_path_query(self) -> None:
        result = tokenize_for_fts("src/storage/fts.py", for_query=True)
        assert result == '"src/storage/fts.py"'

    def test_plain_query(self) -> None:
        result = tokenize_for_fts("hello world", for_query=True)
        assert result == "hello* world*"

    def test_insertion_passthrough(self) -> None:
        """Non-query tokenization returns text unchanged for unicode61."""
        result = tokenize_for_fts("https://example.com", for_query=False)
        assert result == "https://example.com"

    def test_empty_text(self) -> None:
        assert tokenize_for_fts("") == ""
        assert tokenize_for_fts("", for_query=True) == ""
