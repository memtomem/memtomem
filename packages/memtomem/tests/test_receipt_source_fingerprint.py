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


def test_tree_fingerprint_refuses_an_empty_walk(tmp_path):
    # A zipped install's package path is not a directory, and a tree with no
    # Python files is not the package: neither may hash to a stable value.
    (tmp_path / "empty").mkdir()
    (tmp_path / "pkg.zip").write_bytes(b"PK")
    for root in (tmp_path / "empty", tmp_path / "pkg.zip" / "memtomem"):
        with pytest.raises(OSError):
            source_receipt._tree_fingerprint(root)


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_tree_fingerprint_refuses_a_directory_it_cannot_list(tmp_path):
    root = _tree(tmp_path / "root", {"a.py": "x = 1\n", "hidden/b.py": "y = 2\n"})
    (root / "hidden").chmod(0)
    try:
        with pytest.raises(OSError):
            source_receipt._tree_fingerprint(root)
    finally:
        (root / "hidden").chmod(0o755)


def test_tree_fingerprint_hashes_a_name_that_is_not_utf8(tmp_path, monkeypatch):
    # POSIX hands an undecodable name back surrogate-escaped. Simulated, since
    # some file systems (APFS) refuse to create one.
    name = os.fsdecode(b"\xff.py") if os.name == "posix" else "\udcff.py"
    monkeypatch.setattr(source_receipt.os, "walk", lambda root, onerror: [(str(root), [], [name])])
    monkeypatch.setattr(Path, "read_bytes", lambda self: b"x = 1\n")
    assert len(source_receipt._tree_fingerprint(tmp_path)) == 64


def test_code_fingerprint_falls_back_on_an_unencodable_name(monkeypatch):
    def unencodable(root):
        raise UnicodeEncodeError("utf-8", "\udcff", 0, 1, "surrogates not allowed")

    monkeypatch.setattr(source_receipt, "_tree_fingerprint", unencodable)
    assert source_receipt.code_fingerprint() == "unavailable"


def test_code_fingerprint_hashes_the_whole_package(tmp_path, monkeypatch):
    package = _tree(
        tmp_path / "memtomem",
        {"__init__.py": "", "chunking/markdown.py": "a = 1\n", "privacy.py": "b = 1\n"},
    )
    monkeypatch.setattr(memtomem, "__file__", str(package / "__init__.py"))
    before = source_receipt.code_fingerprint()
    assert before == source_receipt._tree_fingerprint(package)
    # A module outside ``chunking/`` that chunk output depends on.
    (package / "privacy.py").write_text("b = 2\n", encoding="utf-8")
    assert source_receipt.code_fingerprint() != before


def test_code_fingerprint_falls_back_when_the_source_cannot_be_read(monkeypatch):
    def unreadable(root):
        raise PermissionError(root)

    monkeypatch.setattr(source_receipt, "_tree_fingerprint", unreadable)
    assert source_receipt.code_fingerprint() == "unavailable"


def test_code_fingerprint_falls_back_without_a_package_file(monkeypatch):
    monkeypatch.delattr(memtomem, "__file__")
    assert source_receipt.code_fingerprint() == "unavailable"


_SNAPSHOT_PROBE = """
import memtomem
from memtomem.indexing import source_receipt

def unreadable(root):
    raise OSError(root)

{setup}
import memtomem.indexing.engine as engine
source_receipt._tree_fingerprint = lambda root: "after-import"
print(engine._CODE_FINGERPRINT)
"""


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        pytest.param(
            'source_receipt._tree_fingerprint = lambda root: "at-import"',
            "at-import",
            id="readable",
        ),
        pytest.param(
            "source_receipt._tree_fingerprint = unreadable", "unavailable", id="unreadable"
        ),
        pytest.param("del memtomem.__file__", "unavailable", id="no-package-file"),
    ],
)
def test_engine_takes_the_fingerprint_once_at_import(setup, expected):
    """Files edited after the engine loaded must not relabel its receipts.

    A fresh interpreter, because this process has already imported the engine.
    """
    src = str(Path(memtomem.__file__).parents[1])
    env = {**os.environ, "PYTHONPATH": os.pathsep.join([src, os.environ.get("PYTHONPATH", "")])}
    result = subprocess.run(
        [sys.executable, "-c", _SNAPSHOT_PROBE.format(setup=setup)],
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
