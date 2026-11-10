"""``chunks_fts.source_file`` holds the file's name, not its path (#3208).

The column is BM25's fallback when no chunk's content matches (#2224). With the
whole path in it, a word of a memory dir's root, or the extension, was a term
of every chunk under that dir, and a query naming it returned all of them.

Four things are pinned here: what ``fts_source_text`` returns, that every
writer of the column goes through it, that an existing database is brought to
the same form once, and that every statement naming the table is accounted for,
so a new writer cannot appear unreviewed.
"""

from __future__ import annotations

import ast
import re
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from memtomem.config import StorageConfig
from memtomem.models import Chunk, ChunkMetadata
from memtomem.storage import sqlite_schema
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_helpers import fts_source_text

_PACKAGE_SRC = Path(sqlite_schema.__file__).parent.parent


class TestFtsSourceText:
    @pytest.mark.parametrize(
        ("stored", "expected"),
        [
            ("/home/me/plane-notes/octopus.md", "octopus"),
            # Both separators split on every host, so a store made on Windows
            # gives the same terms when opened on POSIX.
            ("C:\\Users\\me\\plane-notes\\octopus.md", "octopus"),
            ("\\\\server\\share\\plane-notes\\octopus.md", "octopus"),
            ("/a/mixed\\sep/octopus.md", "octopus"),
            # Only the last suffix goes.
            ("/a/archive.tar.md", "archive.tar"),
            ("/a/rule.md.consolidated.md", "rule.md.consolidated"),
            # A leading dot is not a suffix, and a name without one is kept.
            ("/a/.bashrc", ".bashrc"),
            ("/a/README", "README"),
            ("octopus.md", "octopus"),
        ],
    )
    def test_the_name_without_its_last_suffix(self, stored, expected):
        assert fts_source_text(stored) == expected


def _chunk(source: Path, content: str = "body text") -> Chunk:
    return Chunk(content=content, metadata=ChunkMetadata(source_file=source), embedding=[0.1, 0.2])


def _fts_rows(db: sqlite3.Connection) -> list[tuple[str, str]]:
    """``(chunks.source_file, chunks_fts.source_file)`` per chunk, joined by rowid."""
    return db.execute(
        "SELECT c.source_file, f.source_file FROM chunks c "
        "JOIN chunks_fts f ON f.rowid = c.rowid ORDER BY c.rowid"
    ).fetchall()


def _assert_derived(db: sqlite3.Connection, expected_count: int) -> None:
    rows = _fts_rows(db)
    assert len(rows) == expected_count
    for source, fts_source in rows:
        assert fts_source == fts_source_text(source), (source, fts_source)


@pytest.fixture
async def store(tmp_path):
    backend = SqliteBackend(StorageConfig(sqlite_path=tmp_path / "fts.db"), dimension=2)
    await backend.initialize()
    try:
        yield backend
    finally:
        await backend.close()


class TestEveryWriterStoresTheName:
    """One test per writer; dropping the helper from that writer fails it."""

    @pytest.mark.asyncio
    async def test_upsert_insert(self, store, tmp_path):
        await store.upsert_chunks([_chunk(tmp_path / "plane-notes" / "a.md")])
        _assert_derived(store._get_db(), 1)

    @pytest.mark.asyncio
    async def test_upsert_update(self, store, tmp_path):
        chunk = _chunk(tmp_path / "plane-notes" / "a.md")
        await store.upsert_chunks([chunk])
        chunk.content = "changed body"
        await store.upsert_chunks([chunk])
        rows = _fts_rows(store._get_db())
        assert [fts for _, fts in rows] == ["a"]

    @pytest.mark.asyncio
    async def test_source_move(self, store, tmp_path):
        old = tmp_path / "plane-notes" / "a.md"
        new = tmp_path / "plane-notes" / "moved-here.md"
        await store.upsert_chunks([_chunk(old)])
        assert await store.update_chunks_scope_for_source(old, new, "user", None) == 1
        rows = _fts_rows(store._get_db())
        assert [fts for _, fts in rows] == ["moved-here"]

    # The policy-summary move is pinned where that move is set up:
    # ``test_context_memory_migrate.py::
    # test_memory_migrate_moves_the_policy_summary_with_its_source``.

    @pytest.mark.asyncio
    async def test_rebuild_fts(self, store, tmp_path):
        await store.upsert_chunks(
            [_chunk(tmp_path / "plane-notes" / n) for n in ("a.md", "archive.tar.md")]
        )
        assert await store.rebuild_fts() == 2
        _assert_derived(store._get_db(), 2)
        assert sorted(fts for _, fts in _fts_rows(store._get_db())) == ["a", "archive.tar"]


#: A mention of the table itself: bare, quoted or schema-qualified
#: (``main.chunks_fts``), but not a shadow table (``chunks_fts_private``) or
#: another name it is a prefix of. The census does not try to tell a write
#: from a read; see ``_mentions_of_the_table``.
_TABLE_MENTION = re.compile(r"(?<![\w-])chunks_fts(?![\w-])", re.IGNORECASE)


def _docstring_nodes(tree: ast.AST) -> set[int]:
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    return {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, owners)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }


def _mentions_of_the_table(root: Path) -> Counter[tuple[str, str]]:
    """``(module, innermost function)`` -> mentions of ``chunks_fts`` in code literals.

    Observed, not classified. An earlier version matched only SQL that *writes*
    the column, and each review round found another valid statement it missed
    (``REPLACE INTO``, schema-qualified names, comments, several statements in
    one script). Counting every mention outside docstrings has no such gap:
    any new statement touching the table, read or write, changes the count and
    fails the census, and whoever adds it decides whether it writes
    ``source_file``. Mentions are counted, not literals, so a second statement
    added to an existing script is seen too.

    What it does not see is a change that keeps the count, such as a DELETE
    rewritten into an UPDATE of the same table in the same function. The
    census guards against new writers appearing; the writers there are now are
    pinned by behaviour, in ``TestEveryWriterStoresTheName``.
    """
    found: Counter[tuple[str, str]] = Counter()
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        module = path.relative_to(root).as_posix()

        def visit(node: ast.AST, func: str) -> None:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                func = node.name
            if isinstance(node, ast.JoinedStr):
                text = "".join(v.value if isinstance(v, ast.Constant) else "?" for v in node.values)
                # The interpolated expressions are code too, and may hold a
                # literal of their own (``f"UPDATE {'chunks_fts'} ..."``).
                for part in node.values:
                    if isinstance(part, ast.FormattedValue):
                        visit(part.value, func)
            elif (
                isinstance(node, ast.Constant)
                and isinstance(node.value, str)
                and id(node) not in docstrings
            ):
                text = node.value
            else:
                for child in ast.iter_child_nodes(node):
                    visit(child, func)
                return
            if n := len(_TABLE_MENTION.findall(text)):
                found[(module, func)] += n

        visit(tree, "<module>")
    return found


def test_the_census_counts_what_it_claims(tmp_path):
    """The counter itself, on a module written for it."""
    (tmp_path / "m.py").write_text(
        '''"""Module docstring naming chunks_fts."""


def reader():
    """UPDATE chunks_fts SET source_file=? in a docstring is not code."""
    return "SELECT 1 FROM main.chunks_fts"


def script(t):
    return f"UPDATE chunks_fts SET source_file=?; REPLACE INTO \\"chunks_fts\\" VALUES ({t})"


def other():
    return "INSERT INTO chunks_fts_private VALUES (1); UPDATE \\"chunks_fts-backup\\" SET x=1"


def upper():
    return "UPDATE CHUNKS_FTS SET source_file=?"


def nested():
    return f"UPDATE {'chunks_fts'} SET source_file=?"
''',
        encoding="utf-8",
    )
    assert _mentions_of_the_table(tmp_path) == Counter(
        {("m.py", "reader"): 1, ("m.py", "script"): 2, ("m.py", "upper"): 1, ("m.py", "nested"): 1}
    )


#: Every function whose code names ``chunks_fts``, and how often. The writers of
#: ``source_file`` among them (all through ``fts_source_text``, each pinned by a
#: test above): ``upsert_chunks`` (UPDATE and INSERT),
#: ``update_chunks_scope_for_source``, ``_carry_policy_summary``, ``_run`` (the
#: worker of ``rebuild_fts``) and ``_rewrite_fts_source_text``. The rest read
#: the table, delete from it, or create it.
_KNOWN_MENTIONS = Counter(
    {
        ("indexing/source_receipt.py", "state"): 1,
        ("storage/mixins/eval_cases.py", "read_fts_fingerprint_rows"): 1,
        ("storage/orphan_gc.py", "sweep_orphan_project_root"): 1,
        ("storage/sqlite_backend.py", "_carry_policy_summary"): 1,
        ("storage/sqlite_backend.py", "_delete_policy_summaries_for_source"): 1,
        ("storage/sqlite_backend.py", "_run"): 2,
        ("storage/sqlite_backend.py", "delete_by_source"): 1,
        ("storage/sqlite_backend.py", "delete_chunks"): 1,
        ("storage/sqlite_backend.py", "reset_all"): 6,
        ("storage/sqlite_backend.py", "update_chunks_scope_for_source"): 1,
        ("storage/sqlite_backend.py", "upsert_chunks"): 3,
        ("storage/sqlite_namespace.py", "_delete_chunk_rows"): 1,
        ("storage/sqlite_schema.py", "_migrate_chunks_uniqueness"): 1,
        ("storage/sqlite_schema.py", "_rewrite_fts_source_text"): 2,
        ("storage/sqlite_schema.py", "create_tables"): 1,
        ("storage/sqlite_search_ops.py", "_query"): 2,
        ("storage/sqlite_search_ops.py", "_restrict_to_column"): 1,
    }
)


def test_every_statement_touching_chunks_fts_is_accounted_for():
    """A new statement on the table fails this until it is looked at.

    If it writes ``source_file``, route the value through ``fts_source_text``
    and add a test to ``TestEveryWriterStoresTheName``. Either way, update
    ``_KNOWN_MENTIONS``.
    """
    assert _mentions_of_the_table(_PACKAGE_SRC) == _KNOWN_MENTIONS


async def _reopen(path: Path) -> SqliteBackend:
    backend = SqliteBackend(StorageConfig(sqlite_path=path), dimension=2)
    await backend.initialize()
    return backend


def _make_pre_3208(path: Path) -> None:
    """Put every FTS row back to the full path and forget the rewrite."""
    with sqlite3.connect(path) as db:
        db.execute(
            "UPDATE chunks_fts SET source_file = "
            "(SELECT c.source_file FROM chunks c WHERE c.rowid = chunks_fts.rowid)"
        )
        db.execute(
            "DELETE FROM _memtomem_meta WHERE key = ?", (sqlite_schema._FTS_SOURCE_TEXT_KEY,)
        )


class TestAnExistingDatabaseIsRewrittenOnce:
    @pytest.mark.asyncio
    async def test_full_paths_become_names_on_open(self, tmp_path):
        path = tmp_path / "old.db"
        mem = tmp_path / "plane-notes"
        backend = await _reopen(path)
        try:
            await backend.upsert_chunks(
                [_chunk(mem / "a.md", "apples"), _chunk(mem / "b.md", "the dog sleeps")]
            )
        finally:
            await backend.close()
        _make_pre_3208(path)
        with sqlite3.connect(path) as db:
            assert all(fts == source for source, fts in _fts_rows(db))

        backend = await _reopen(path)
        try:
            db = backend._get_db()
            _assert_derived(db, 2)
            assert (
                db.execute(
                    "SELECT value FROM _memtomem_meta WHERE key = ?",
                    (sqlite_schema._FTS_SOURCE_TEXT_KEY,),
                ).fetchone()[0]
                == "done"
            )
            assert await backend.bm25_search("plane", 10) == []
            assert [r.chunk.metadata.source_file.name for r in await backend.bm25_search("b", 10)]
        finally:
            await backend.close()

    @pytest.mark.asyncio
    async def test_a_rerun_derives_from_the_chunk_not_from_its_own_output(self, tmp_path):
        """``archive.tar.md`` stays ``archive.tar``; it does not lose a suffix per run."""
        path = tmp_path / "multi.db"
        backend = await _reopen(path)
        try:
            await backend.upsert_chunks([_chunk(tmp_path / "archive.tar.md")])
        finally:
            await backend.close()
        for _ in range(2):
            with sqlite3.connect(path) as db:
                db.execute(
                    "DELETE FROM _memtomem_meta WHERE key = ?",
                    (sqlite_schema._FTS_SOURCE_TEXT_KEY,),
                )
            backend = await _reopen(path)
            try:
                assert [fts for _, fts in _fts_rows(backend._get_db())] == ["archive.tar"]
            finally:
                await backend.close()

    @pytest.mark.asyncio
    async def test_the_marker_stops_a_second_pass(self, tmp_path):
        path = tmp_path / "marked.db"
        backend = await _reopen(path)
        try:
            await backend.upsert_chunks([_chunk(tmp_path / "a.md")])
        finally:
            await backend.close()
        with sqlite3.connect(path) as db:
            db.execute("UPDATE chunks_fts SET source_file = 'left-alone'")
        backend = await _reopen(path)
        try:
            assert [fts for _, fts in _fts_rows(backend._get_db())] == ["left-alone"]
        finally:
            await backend.close()
