"""Regression tests for FTS5 query tokenization."""

from __future__ import annotations

import sqlite3

import pytest

from memtomem.storage.fts_tokenizer import _FTS5_SPECIAL_RE, tokenize_for_fts


@pytest.mark.parametrize(
    "query, expected",
    [
        ("https://example.com/path", '"https://example.com/path"'),
        ("file.name.ext", '"file.name.ext"'),
        ("a/b/c", '"a/b/c"'),
        (r"dir\\file.md", r'"dir\\file.md"'),
        ("<tag>", '"<tag>"'),
        ("word~3", '"word~3"'),
        ("`foo.bar()`", '"`foo.bar()`"'),
        ("---\nkey: value", '"---" "key:" value*'),
    ],
)
def test_query_tokens_with_punctuation_are_quoted(query: str, expected: str) -> None:
    assert tokenize_for_fts(query, for_query=True) == expected


def test_plain_words_still_get_prefix_wildcards() -> None:
    assert tokenize_for_fts("hello world", for_query=True) == "hello* world*"


def test_or_queries_preserve_safe_quoting() -> None:
    assert (
        tokenize_for_fts("file.name.ext a/b/c", for_query=True, use_or=True)
        == '"file.name.ext" OR "a/b/c"'
    )


@pytest.mark.parametrize("char", list('."()/\\<>~`[]{}!,;?@#$%&=|'))
def test_ascii_punctuation_is_not_treated_as_bareword(char: str) -> None:
    assert _FTS5_SPECIAL_RE.search(f"a{char}b")


def test_punctuation_queries_do_not_raise_fts5_syntax_errors() -> None:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE chunks_fts USING fts5(content)")
    except sqlite3.OperationalError as exc:
        pytest.skip(f"sqlite FTS5 unavailable: {exc}")

    db.execute(
        "INSERT INTO chunks_fts(content) VALUES (?)",
        (
            "---\n"
            "key: value\n"
            "https://example.com/path file.name.ext a/b/c dir/file.md "
            "`foo.bar()` <tag> word~3",
        ),
    )

    queries = [
        "---\nkey: value",
        "https://example.com/path",
        "file.name.ext",
        "a/b/c",
        "dir/file.md",
        "`foo.bar()`",
        "<tag>",
        "word~3",
    ]
    for query in queries:
        fts_query = tokenize_for_fts(query, for_query=True)
        rows = db.execute(
            "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?",
            (fts_query,),
        ).fetchall()
        assert rows == [(1,)]
