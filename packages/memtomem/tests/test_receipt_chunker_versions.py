"""Third-party chunker versions are part of the source receipt's code identity (#2774).

The chunkers import their parsers lazily and fall back when one is absent, so
installing ``tree-sitter-python`` changed the chunks of an unchanged ``.py``
source without changing memtomem's own source. The receipt kept the
parser-less chunks: ``mm index <file>`` answered ``receipt_hit`` while
``mm memory doctor`` re-chunked with the parser and reported ``stale_index``.
"""

import ast
import os
import re
import subprocess
import sys
from importlib import metadata
from pathlib import Path
from unittest.mock import Mock

import pytest

import memtomem
from memtomem.indexing import source_receipt

CHUNKING = Path(memtomem.__file__).parent / "chunking"


def _is_type_checking_block(node: ast.AST) -> bool:
    if not isinstance(node, ast.If):
        return False
    test = node.test
    return (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING") or (
        isinstance(test, ast.Attribute)
        and test.attr == "TYPE_CHECKING"
        and isinstance(test.value, ast.Name)
        and test.value.id == "typing"
    )


def _direct_third_party_imports(tree: ast.AST) -> set[str]:
    found: set[str] = set()

    def visit(node: ast.AST) -> None:
        if _is_type_checking_block(node):
            # Typing-only imports never run, so they cannot shape a chunk.
            for child in node.orelse:  # type: ignore[attr-defined]
                visit(child)
            return
        if isinstance(node, ast.Import):
            found.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
        for child in ast.iter_child_nodes(node):
            visit(child)

    visit(tree)
    return {
        name
        for name in found
        if name not in sys.stdlib_module_names and name not in {"__future__", "memtomem"}
    }


def test_registry_lists_every_third_party_module_imported_under_chunking():
    """A direct-import guard: a new parser import must join the identity.

    Imports at any depth count, since the parsers are imported inside
    functions. A dependency reached only through a ``memtomem`` helper outside
    ``chunking/``, or through a dynamic import, is not seen here and has to be
    added to the registry by hand.
    """
    imported: set[str] = set()
    files = sorted(CHUNKING.rglob("*.py"))
    assert files
    for path in files:
        imported |= _direct_third_party_imports(ast.parse(path.read_text(encoding="utf-8")))
    assert imported == set(source_receipt.CHUNKER_DISTRIBUTIONS)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        pytest.param(
            "def f():\n    import tree_sitter_python\n", {"tree_sitter_python"}, id="lazy"
        ),
        pytest.param("from yaml.loader import SafeLoader\n", {"yaml"}, id="from-submodule"),
        pytest.param("from . import base\nimport json\n", set(), id="relative-and-stdlib"),
        pytest.param("from memtomem.models import Chunk\n", set(), id="own-package"),
        pytest.param(
            "from typing import TYPE_CHECKING\nif TYPE_CHECKING:\n    import tokenizers\n",
            set(),
            id="typing-only",
        ),
        pytest.param(
            "import typing\nif typing.TYPE_CHECKING:\n    pass\nelse:\n    import tokenizers\n",
            {"tokenizers"},
            id="type-checking-else-runs",
        ),
        pytest.param(
            "if flags.TYPE_CHECKING:\n    import tokenizers\n",
            {"tokenizers"},
            id="other-type-checking-attribute-runs",
        ),
    ],
)
def test_guard_classifies_import_shapes(source, expected):
    assert _direct_third_party_imports(ast.parse(source)) == expected


def _canonical(name: str) -> str:
    """Distribution name as metadata lookups compare it: case and ``-_.`` runs folded."""
    return re.sub(r"[-_.]+", "-", name).lower()


def test_registry_names_the_distribution_that_provides_each_installed_module():
    provided = metadata.packages_distributions()
    checked = 0
    for module, dist in source_receipt.CHUNKER_DISTRIBUTIONS.items():
        if module not in provided:
            continue
        checked += 1
        assert [_canonical(name) for name in provided[module]] == [_canonical(dist)], module
    # ``yaml`` is a base dependency, so at least one entry is always checked.
    assert checked


def test_chunker_versions_reports_one_entry_per_distribution(monkeypatch):
    real_version = metadata.version
    monkeypatch.setattr(
        source_receipt.metadata,
        "version",
        lambda dist: "1.2.3" if dist == "tree-sitter" else real_version(dist),
    )
    versions = source_receipt.chunker_versions()
    assert list(versions) == sorted(set(source_receipt.CHUNKER_DISTRIBUTIONS.values()))
    assert versions["tree-sitter"] == "1.2.3"


def test_chunker_versions_records_an_absent_distribution_as_none(monkeypatch):
    def version(dist):
        if dist == "tree-sitter-python":
            raise metadata.PackageNotFoundError(dist)
        return "1.0"

    monkeypatch.setattr(source_receipt.metadata, "version", version)
    versions = source_receipt.chunker_versions()
    assert versions["tree-sitter-python"] is None
    assert versions["tree-sitter"] == "1.0"


def test_chunker_versions_never_raises(monkeypatch):
    def version(dist):
        if dist == "PyYAML":
            raise ValueError("broken metadata")
        return "1.0"

    monkeypatch.setattr(source_receipt.metadata, "version", version)
    assert source_receipt.chunker_versions()["PyYAML"] == "unavailable"


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"tree-sitter-python": "0.26.0"}, id="upgraded"),
        pytest.param({"tree-sitter-python": None}, id="removed"),
        pytest.param({"tree-sitter-python": "unavailable"}, id="unreadable"),
    ],
)
def test_a_version_change_changes_the_policy_digest(change):
    before = {"tree-sitter": "0.25.0", "tree-sitter-python": "0.25.0"}
    assert source_receipt.digest({"chunkers": before}) != source_receipt.digest(
        {"chunkers": {**before, **change}}
    )


_SNAPSHOT_PROBE = """
from memtomem.indexing import source_receipt
source_receipt.chunker_versions = lambda: {"tree-sitter": "at-import"}
import memtomem.indexing.engine as engine
source_receipt.chunker_versions = lambda: {"tree-sitter": "after-import"}
print(engine._CHUNKER_VERSIONS["tree-sitter"])
"""


def test_engine_takes_the_chunker_versions_once_at_import():
    """A fresh interpreter, because this process has already imported the engine."""
    src = str(Path(memtomem.__file__).parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_PROBE],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )
    assert result.stdout.strip().splitlines()[-1] == "at-import"


@pytest.mark.anyio
async def test_a_chunker_version_change_rechunks_an_unchanged_source(
    bm25_only_components, monkeypatch
):
    comp, directory = bm25_only_components
    source = directory / "parser.py"
    source.write_text("def first():\n    return 1\n\n\ndef second():\n    return 2\n")
    # Run 1 records the receipt.
    assert not (await comp.index_engine.index_file(source)).errors

    chunker = Mock(wraps=comp.index_engine.chunk_content)
    monkeypatch.setattr(comp.index_engine, "chunk_content", chunker)

    # Run 2, nothing changed: the receipt is reused and nothing is chunked.
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_not_called()

    # Run 3, a parser was installed or upgraded. ``raising=False`` so that an
    # engine without the attribute fails on behaviour below, not in setup.
    import memtomem.indexing.engine as engine

    current = dict(getattr(engine, "_CHUNKER_VERSIONS", {}))
    monkeypatch.setattr(
        engine, "_CHUNKER_VERSIONS", {**current, "tree-sitter-python": "9.9.9"}, raising=False
    )
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_called_once()

    # Run 4, same parser versions as run 3: the replacement receipt was
    # recorded and is honoured.
    chunker.reset_mock()
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_not_called()
