"""The source receipt tells two builds of one release apart (#2773).

A ``main`` checkout and the release it follows report the same version. With
the version as the receipt's only code identity, the checkout reused receipts
the release wrote and never re-chunked with its own chunker, while
``mm memory doctor`` re-chunked with it and reported ``stale_index`` that
``mm index <file>`` could not clear.
"""

import hashlib
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

import memtomem
from memtomem.indexing import source_receipt


def _tree(root: Path, files: dict[str, str]) -> Path:
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return root


def test_tree_fingerprint_is_stable_for_identical_trees(tmp_path):
    files = {"a.py": "x = 1\n", "pkg/b.py": "y = 2\n"}
    first = _tree(tmp_path / "one", files)
    second = _tree(tmp_path / "two", files)
    assert source_receipt._tree_fingerprint(first) == source_receipt._tree_fingerprint(second)


@pytest.mark.parametrize(
    "change",
    [
        pytest.param({"pkg/b.py": "y = 3\n"}, id="edited"),
        pytest.param({"pkg/b.py": "y = 2\n", "c.py": ""}, id="added"),
        pytest.param({"pkg/c.py": "y = 2\n"}, id="renamed"),
    ],
)
def test_tree_fingerprint_moves_with_the_source(tmp_path, change):
    base = {"a.py": "x = 1\n"}
    before = _tree(tmp_path / "before", {**base, "pkg/b.py": "y = 2\n"})
    after = _tree(tmp_path / "after", {**base, **change})
    assert source_receipt._tree_fingerprint(before) != source_receipt._tree_fingerprint(after)


def test_tree_fingerprint_ignores_files_that_are_not_python(tmp_path):
    root = _tree(tmp_path / "root", {"a.py": "x = 1\n"})
    before = source_receipt._tree_fingerprint(root)
    (root / "style.css").write_text("body {}\n", encoding="utf-8")
    assert source_receipt._tree_fingerprint(root) == before


def test_tree_fingerprint_orders_files_by_posix_relative_path(tmp_path):
    # ``Path`` ordering compares parts, which puts ``a/b.py`` before ``a.b.py``;
    # string ordering on the POSIX form puts ``a.b.py`` first ('.' < '/').
    root = _tree(tmp_path / "root", {"a/b.py": "1\n", "a.b.py": "2\n"})
    expected = hashlib.sha256()
    for rel in ("a.b.py", "a/b.py"):
        expected.update(rel.encode() + b"\0" + (root / rel).read_bytes() + b"\0")
    assert source_receipt._tree_fingerprint(root) == expected.hexdigest()


def test_code_fingerprint_falls_back_when_the_source_cannot_be_read(monkeypatch):
    def unreadable(root):
        raise PermissionError(root)

    monkeypatch.setattr(source_receipt, "_tree_fingerprint", unreadable)
    assert source_receipt.code_fingerprint() == "unavailable"


_SNAPSHOT_PROBE = """
from memtomem.indexing import source_receipt

def unreadable(root):
    raise OSError(root)

source_receipt._tree_fingerprint = {at_import}
import memtomem.indexing.engine as engine
source_receipt._tree_fingerprint = lambda root: "after-import"
print(engine._CODE_FINGERPRINT)
"""


@pytest.mark.parametrize(
    ("at_import", "expected"),
    [
        pytest.param('lambda root: "at-import"', "at-import", id="readable"),
        pytest.param("unreadable", "unavailable", id="unreadable"),
    ],
)
def test_engine_takes_the_fingerprint_once_at_import(at_import, expected):
    """Files edited after the engine loaded must not relabel its receipts.

    A fresh interpreter, because this process has already imported the engine.
    """
    src = str(Path(memtomem.__file__).parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_PROBE.format(at_import=at_import)],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=120,
    )
    assert result.stdout.strip().splitlines()[-1] == expected


@pytest.mark.anyio
async def test_a_build_with_other_source_and_the_same_version_rechunks(
    bm25_only_components, monkeypatch
):
    comp, directory = bm25_only_components
    source = directory / "same-version.md"
    source.write_text("# Same version\n\nAn ordinary nonsecret memory.\n")
    assert not (await comp.index_engine.index_file(source)).errors

    chunker = Mock(wraps=comp.index_engine.chunk_content)
    monkeypatch.setattr(comp.index_engine, "chunk_content", chunker)
    # Control: same build, same content — the receipt is reused.
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_not_called()

    # Another build that reports the same version.
    monkeypatch.setattr("memtomem.indexing.engine._CODE_FINGERPRINT", "another-build")
    assert not (await comp.index_engine.index_file(source)).errors
    chunker.assert_called_once()
