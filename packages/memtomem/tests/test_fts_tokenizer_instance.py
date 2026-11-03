"""Each storage backend tokenizes its own database its own way (#3018).

The FTS tokenizer used to be a process global that ``create_components`` set
from the config it was given, so a second component stack in one process
changed how the first one tokenized from then on — and a stack created with
the default never set it back. ADR-0042 gives the planned ``langchain-memtomem``
store its own stack beside whatever else the application runs, which is why
the choice now lives on the backend.

A stub ``kiwipiepy`` keeps these tests independent of the optional extra. It
reverses every word, so a kiwipiepy row or query is told apart from a
unicode61 one by its spelling alone.
"""

from __future__ import annotations

import ast
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from helpers import make_chunk

from memtomem.config import Mem2MemConfig, StorageConfig
from memtomem.runtime.components import close_components, create_components
from memtomem.storage import fts_tokenizer
from memtomem.storage import sqlite_backend as sqlite_backend_mod
from memtomem.storage.sqlite_backend import SqliteBackend

_SRC = Path(__file__).resolve().parents[1] / "src" / "memtomem"
_TOOLS = Path(__file__).resolve().parents[3] / "tools"


class _Token:
    def __init__(self, form: str) -> None:
        self.form = form


class _StubKiwi:
    calls: list[str] = []

    def tokenize(self, text: str) -> list[_Token]:
        _StubKiwi.calls.append(text)
        return [_Token(word[::-1]) for word in text.split()]


@pytest.fixture
def stub_kiwi(monkeypatch):
    """Install the stub as ``kiwipiepy`` and start from a process that never loaded it."""
    module = types.ModuleType("kiwipiepy")
    module.Kiwi = _StubKiwi  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "kiwipiepy", module)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_instance", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", False)
    _StubKiwi.calls = []
    return _StubKiwi


@pytest.fixture
def blocked_kiwi(monkeypatch):
    """A process where ``kiwipiepy`` cannot be imported and nothing has tried yet."""
    monkeypatch.setitem(sys.modules, "kiwipiepy", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_instance", None)
    monkeypatch.setattr(fts_tokenizer, "_kiwi_unavailable", False)


def _config(tmp_path: Path, name: str, tokenizer: str) -> Mem2MemConfig:
    root = tmp_path / name
    (root / "memories").mkdir(parents=True)
    config = Mem2MemConfig()
    config.storage.sqlite_path = root / "store.db"
    config.indexing.memory_dirs = [root / "memories"]
    config.search.tokenizer = tokenizer
    return config


def _fts_content(db_path: Path) -> list[str]:
    with sqlite3.connect(db_path) as db:
        return [row[0] for row in db.execute("SELECT content FROM chunks_fts")]


class TestTwoStacksInOneProcess:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("kiwi_first", [False, True], ids=["unicode61-first", "kiwi-first"])
    async def test_each_stack_tokenizes_its_own_rows_and_queries(
        self, stub_kiwi, tmp_path, kiwi_first
    ):
        order = ["kiwipiepy", "unicode61"] if kiwi_first else ["unicode61", "kiwipiepy"]
        stacks = {}
        try:
            for tokenizer in order:
                stacks[tokenizer] = await create_components(
                    _config(tmp_path, tokenizer, tokenizer),
                    load_ambient_config=False,
                    entity_backfill=False,
                )
            plain, kiwi = stacks["unicode61"], stacks["kiwipiepy"]
            assert plain.storage.fts_tokenizer == "unicode61"
            assert kiwi.storage.fts_tokenizer == "kiwipiepy"

            for stack in (plain, kiwi):
                await stack.storage.upsert_chunks([make_chunk("wombat burrow", source="a.md")])

            # Index time: the rows each stack wrote are in its own form.
            assert _fts_content(plain.config.storage.sqlite_path) == ["wombat burrow"]
            assert _fts_content(kiwi.config.storage.sqlite_path) == ["tabmow worrub"]

            # Query time: the unicode61 stack never consults Kiwi, and both
            # find their row, which each would miss under the other's form.
            before = len(stub_kiwi.calls)
            assert len(await plain.storage.bm25_search("wombat", top_k=5)) == 1
            assert len(stub_kiwi.calls) == before, "the unicode61 stack tokenized with Kiwi"
            assert len(await kiwi.storage.bm25_search("wombat", top_k=5)) == 1
            assert "wombat" in stub_kiwi.calls[before:]
        finally:
            for stack in stacks.values():
                await close_components(stack)


class TestSharedDatabaseContract:
    def test_a_switch_moves_only_the_backend_it_is_called_on(self, tmp_path):
        """Stacks sharing one database must be configured alike; a switch does not follow."""
        config = StorageConfig(sqlite_path=tmp_path / "shared.db")
        first = SqliteBackend(config, fts_tokenizer="unicode61")
        second = SqliteBackend(config, fts_tokenizer="unicode61")

        first.set_fts_tokenizer("kiwipiepy")

        assert first.fts_tokenizer == "kiwipiepy"
        assert second.fts_tokenizer == "unicode61"

    def test_an_unknown_tokenizer_is_refused(self, tmp_path):
        config = StorageConfig(sqlite_path=tmp_path / "x.db")
        with pytest.raises(ValueError, match="Unknown tokenizer"):
            SqliteBackend(config, fts_tokenizer="icu")
        backend = SqliteBackend(config, fts_tokenizer="unicode61")
        with pytest.raises(ValueError, match="Unknown tokenizer"):
            backend.set_fts_tokenizer("icu")
        assert backend.fts_tokenizer == "unicode61"


class TestFallbackIsAProcessFact:
    @pytest.mark.asyncio
    async def test_a_failed_import_changes_what_kiwi_backends_run_not_what_they_are(
        self, blocked_kiwi, tmp_path
    ):
        stacks = []
        try:
            for name, tokenizer in (("k", "kiwipiepy"), ("u", "unicode61")):
                stacks.append(
                    await create_components(
                        _config(tmp_path, name, tokenizer),
                        load_ambient_config=False,
                        entity_backfill=False,
                    )
                )
            kiwi, plain = (s.storage for s in stacks)

            # Pending: nothing has tried the import yet.
            assert kiwi.fts_tokenizer_effective == "kiwipiepy"

            await kiwi.upsert_chunks([make_chunk("wombat burrow", source="a.md")])

            assert kiwi.fts_tokenizer == "kiwipiepy"
            assert kiwi.fts_tokenizer_effective == "unicode61"
            assert plain.fts_tokenizer_effective == "unicode61"
            # The fallback wrote unicode61 rows, and queries find them.
            assert _fts_content(stacks[0].config.storage.sqlite_path) == ["wombat burrow"]
            assert len(await kiwi.bm25_search("wombat", top_k=5)) == 1
        finally:
            for stack in stacks:
                await close_components(stack)


class TestOneTokenizerPerOperation:
    @pytest.mark.asyncio
    async def test_a_switch_during_rebuild_does_not_split_its_rows(
        self, stub_kiwi, tmp_path, monkeypatch
    ):
        """The rebuild worker reads the tokenizer once, not per batch or per row."""
        stack = await create_components(
            _config(tmp_path, "s", "kiwipiepy"),
            load_ambient_config=False,
            entity_backfill=False,
        )
        try:
            storage = stack.storage
            await storage.upsert_chunks(
                [make_chunk(f"wombat number{i}", source=f"n{i}.md") for i in range(5)]
            )
            monkeypatch.setattr(sqlite_backend_mod, "_REBUILD_FTS_BATCH_SIZE", 2)
            seen: list[str] = []
            real = fts_tokenizer.tokenize_for_fts

            def switching(text, **kwargs):
                seen.append(kwargs["tokenizer"])
                storage.set_fts_tokenizer("unicode61")
                return real(text, **kwargs)

            monkeypatch.setattr(sqlite_backend_mod._fts, "tokenize_for_fts", switching)

            assert await storage.rebuild_fts() == 5

            assert seen == ["kiwipiepy"] * 5
        finally:
            await close_components(stack)


# ---------------------------------------------------------------------------
# Guards: nothing selects a tokenizer behind a backend's back.

_FORBIDDEN = frozenset({"set_tokenizer", "get_tokenizer", "_active_tokenizer"})


def _names(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


# Names whose calls the guard checks. Importing one under another name is
# itself a violation: a call through ``tok`` would need scope analysis to tie
# back to ``tokenize_for_fts``, and nothing in the tree needs the rename.
_POLICED = frozenset({"tokenize_for_fts", "SqliteBackend"})


def _calls(tree: ast.AST):
    """Yield ``(call, callee name)`` for every call in *tree*."""
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            yield node, _names(node.func)


def _violations(source: str, path: str) -> list[str]:
    """Return every place *source* bypasses the per-backend tokenizer."""
    tree = ast.parse(source)
    found: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.alias):
            name = node.name.rsplit(".", 1)[-1]
            if name in _FORBIDDEN:
                found.append(f"{path}:{node.lineno} imports {name}")
            elif name in _POLICED and node.asname not in (None, name):
                found.append(f"{path}:{node.lineno} imports {name} as {node.asname}")
        elif isinstance(node, (ast.Name, ast.Attribute)) and _names(node) in _FORBIDDEN:
            found.append(f"{path}:{node.lineno} uses {_names(node)}")
    for node, callee in _calls(tree):
        keywords = {k.arg for k in node.keywords}
        if callee == "tokenize_for_fts" and "tokenizer" not in keywords:
            found.append(f"{path}:{node.lineno} calls tokenize_for_fts without tokenizer=")
        if callee == "SqliteBackend" and "fts_tokenizer" not in keywords:
            found.append(f"{path}:{node.lineno} builds SqliteBackend without fts_tokenizer=")
    return found


def _sources() -> list[Path]:
    return sorted(_SRC.rglob("*.py")) + sorted(_TOOLS.rglob("*.py"))


def test_no_module_selects_a_tokenizer_behind_the_backend():
    violations = [
        v for path in _sources() for v in _violations(path.read_text(encoding="utf-8"), str(path))
    ]
    assert violations == []


def test_the_guard_sees_the_call_sites_it_polices():
    """A guard that matched nothing would pass vacuously."""
    calls: dict[str, int] = {"tokenize_for_fts": 0, "SqliteBackend": 0}
    for path in _sources():
        for _node, callee in _calls(ast.parse(path.read_text(encoding="utf-8"))):
            if callee in calls:
                calls[callee] += 1
    # sqlite_backend (3), sqlite_search_ops (3), hybrid_store (1);
    # factory, components, embedding_cmd, reset_cmd, init_cmd.
    assert calls == {"tokenize_for_fts": 7, "SqliteBackend": 5}


@pytest.mark.parametrize(
    "source",
    [
        "from memtomem.storage.fts_tokenizer import set_tokenizer\n",
        "from memtomem.storage import fts_tokenizer as f\nf.get_tokenizer()\n",
        "import memtomem.storage.fts_tokenizer as f\nf._active_tokenizer\n",
        "from memtomem.storage import fts_tokenizer as _fts\n_fts.tokenize_for_fts('q')\n",
        "from memtomem.storage.fts_tokenizer import tokenize_for_fts\ntokenize_for_fts('q')\n",
        "SqliteBackend(cfg, dimension=3)\n",
        "from memtomem.storage import sqlite_backend as sb\nsb.SqliteBackend(cfg)\n",
        "from memtomem.storage.fts_tokenizer import tokenize_for_fts as tok\ntok('q')\n",
        "from memtomem.storage.sqlite_backend import SqliteBackend as Backend\nBackend(cfg)\n",
        "from memtomem.storage.fts_tokenizer import set_tokenizer as s\n",
    ],
    ids=[
        "import-set",
        "alias-get",
        "alias-global",
        "alias-call",
        "direct-call",
        "ctor",
        "ctor-attr",
        "renamed-call",
        "renamed-ctor",
        "renamed-set",
    ],
)
def test_the_guard_catches_each_bypass_shape(source):
    assert _violations(source, "<witness>") != []


@pytest.mark.parametrize(
    "source",
    [
        # An unrelated import that happens to reuse a name is not a call to ours.
        "from json import dumps as tok\ndef f(tok):\n    return tok('q')\n",
        "from memtomem.storage.fts_tokenizer import tokenize_for_fts\n"
        "tokenize_for_fts('q', tokenizer='unicode61')\n",
        "from memtomem.storage.sqlite_backend import SqliteBackend as SqliteBackend\n"
        "SqliteBackend(cfg, fts_tokenizer='unicode61')\n",
    ],
    ids=["unrelated-alias", "explicit-call", "same-name-alias"],
)
def test_the_guard_passes_what_it_should(source):
    assert _violations(source, "<witness>") == []
