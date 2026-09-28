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


# --- Uppercase FTS5 keywords as query words (#2576) -------------------------
#
# FTS5 reads a bare uppercase AND, OR or NOT as an operator. With the prefix
# wildcard appended (``NOT*``) the whole MATCH was a syntax error, so the BM25
# leg dropped out and search ran dense-only. Only the uppercase spelling is an
# operator; ``and``/``Not`` and ``NEAR`` without parentheses are plain terms.

_KEYWORD_QUERIES = ["alpha NOT beta", "alpha AND beta", "alpha OR beta", "NOT", "x AND y OR z"]


@pytest.mark.parametrize(
    "query, use_or, expected",
    [
        ("alpha NOT beta", False, 'alpha* "NOT"* beta*'),
        ("alpha NOT beta", True, 'alpha* OR "NOT"* OR beta*'),
        ("NOT", False, '"NOT"*'),
        ("x AND y OR z", False, 'x* "AND"* y* "OR"* z*'),
        # Not operators: left as ordinary prefix terms.
        ("and Or Not NEAR", False, "and* Or* Not* NEAR*"),
    ],
)
def test_uppercase_keywords_are_quoted_terms(query: str, use_or: bool, expected: str) -> None:
    assert tokenize_for_fts(query, for_query=True, use_or=use_or, tokenizer="unicode61") == expected


def _fts_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:")
    try:
        db.execute("CREATE VIRTUAL TABLE t USING fts5(content, source_file, tokenize='unicode61')")
    except sqlite3.OperationalError as exc:
        pytest.skip(f"sqlite FTS5 unavailable: {exc}")
    return db


def _count(db: sqlite3.Connection, body: str) -> int:
    # Same column-filter shape the backend builds (``_restrict_to_column``).
    return db.execute(
        "SELECT count(*) FROM t WHERE t MATCH ?", ("{content} : (" + body + ")",)
    ).fetchone()[0]


@pytest.mark.parametrize("tokenizer", ["unicode61", "kiwipiepy"])
@pytest.mark.parametrize("use_or", [False, True])
@pytest.mark.parametrize("query", _KEYWORD_QUERIES)
def test_uppercase_keyword_queries_match_like_lowercase(
    tokenizer: str, use_or: bool, query: str
) -> None:
    if tokenizer == "kiwipiepy":
        pytest.importorskip("kiwipiepy")
    db = _fts_db()
    db.execute("INSERT INTO t VALUES ('alpha not beta and gamma or x y z', 'a.md')")
    db.execute("INSERT INTO t VALUES ('unrelated words only', 'b.md')")

    body = tokenize_for_fts(query, for_query=True, use_or=use_or, tokenizer=tokenizer)
    lower = tokenize_for_fts(query.lower(), for_query=True, use_or=use_or, tokenizer=tokenizer)

    assert _count(db, body) == _count(db, lower) == 1


def test_uppercase_keyword_keeps_the_prefix_match() -> None:
    db = _fts_db()
    db.execute("INSERT INTO t VALUES ('nothing to see', 'a.md')")

    assert _count(db, tokenize_for_fts("NOT", for_query=True)) == 1
    assert _count(db, tokenize_for_fts("not", for_query=True)) == 1


@pytest.mark.asyncio
async def test_bm25_search_finds_a_query_with_an_uppercase_keyword(storage) -> None:
    from helpers import make_chunk

    await storage.upsert_chunks([make_chunk("git rebase does NOT rewrite merged history")])

    for query in ("rebase NOT history", "rebase OR merged", "NOT"):
        results = await storage.bm25_search(query, top_k=5)
        assert [r.chunk.content for r in results] == [
            "git rebase does NOT rewrite merged history"
        ], query
