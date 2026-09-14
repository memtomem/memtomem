"""Exercise the extracted first-user distribution, not just repository paths."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import re
import sys
from unittest.mock import Mock
from urllib.parse import unquote, urlparse
import zipfile

import pytest


ROOT = Path(__file__).resolve().parents[3]


def load(path: Path):
    spec = importlib.util.spec_from_file_location(path.stem, path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def bundle(tmp_path):
    builder = load(ROOT / "tools/build_slateharbor_bundle.py")
    first, second = tmp_path / "first.zip", tmp_path / "second.zip"
    assert builder.build(first) == builder.build(second)
    assert first.read_bytes() == second.read_bytes()
    target = tmp_path / "실습 폴더 (사본)"
    with zipfile.ZipFile(first) as archive:
        archive.extractall(target)
    return target


@pytest.mark.parametrize("default_encoding", ["utf-8", "cp1252"])
def test_extracted_entrypoints_and_tools_are_complete(bundle, monkeypatch, default_encoding):
    original_read_text = Path.read_text

    def locale_read_text(path, encoding=None, errors=None, **kwargs):
        return original_read_text(
            path, encoding=encoding or default_encoding, errors=errors, **kwargs
        )

    # Exercise non-UTF-8 host defaults even on UTF-8 development machines.
    monkeypatch.setattr(Path, "read_text", locale_read_text)
    assert (bundle / "START_HERE.md").is_file()
    assert (bundle / "LICENSE").is_file()
    assert (bundle / "tools/check_beginner_notebooks.py").is_file()
    assert (bundle / "tools/build_slateharbor_bundle.py").is_file()
    # Follow every local link in the two distribution entrypoints.
    for relative in ("START_HERE.md", "examples/notebooks/README.md"):
        page = bundle / relative
        for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", page.read_text(encoding="utf-8")):
            parsed = urlparse(target)
            if not parsed.scheme:
                assert (page.parent / unquote(parsed.path)).exists(), target
    notebook = json.loads(
        (bundle / "examples/notebooks/00_start_here.ipynb").read_text(encoding="utf-8")
    )
    for cell in notebook["cells"]:
        if cell["cell_type"] == "code":
            assert cell["outputs"] == []
            assert cell["execution_count"] is None
    sample = bundle / "examples/onboarding/slateharbor"
    lab = load(sample / "lab.py")
    assert len(lab.verify_sources(sample)["files"]) == 150


@pytest.mark.parametrize("missing", ["manifest.json", "project/auth/src/policy.py"])
def test_incomplete_bundle_explains_recovery_before_creating_state(bundle, missing, monkeypatch):
    sample = bundle / "examples/onboarding/slateharbor"
    lab = load(sample / "lab.py")
    (sample / missing).unlink()
    allocate = Mock(side_effect=AssertionError("Incomplete bundle allocated temporary state"))
    monkeypatch.setattr(lab.tempfile, "TemporaryDirectory", allocate)
    with pytest.raises(FileNotFoundError, match="ZIP"):
        lab.Lab(sample)
    allocate.assert_not_called()


def test_unsupported_python_is_reported_before_dependency_import(monkeypatch):
    notebook = load(ROOT / "examples/onboarding/slateharbor/build_notebook.py").notebook()
    first_code = next(c["source"] for c in notebook["cells"] if c["cell_type"] == "code")
    with monkeypatch.context() as patch:
        patch.setattr(sys, "version_info", (3, 11, 0))
        patch.setitem(sys.modules, "memtomem", None)
        with pytest.raises(RuntimeError, match="Python 3.12"):
            exec(first_code, {})
