"""Tests for deterministic CI pytest sharding."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "run_pytest_shard.py"
    spec = importlib.util.spec_from_file_location("run_pytest_shard", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


sharder = _load_tool()


def test_shards_are_disjoint_and_exhaustive() -> None:
    files = [Path(f"test_{index}.py") for index in range(11)]
    shards = [sharder.shard_files(files, index=index, count=3) for index in range(3)]

    assert set().union(*map(set, shards)) == set(files)
    assert all(set(left).isdisjoint(right) for left, right in zip(shards, shards[1:]))


@pytest.mark.parametrize(("index", "count"), [(-1, 2), (2, 2), (0, 0)])
def test_invalid_shard_coordinates_fail(index: int, count: int) -> None:
    with pytest.raises(ValueError):
        sharder.shard_files([Path("test_one.py")], index=index, count=count)


def test_repository_discovery_excludes_golden_path() -> None:
    files = sharder.test_files(_ROOT)

    assert files
    assert all(path.name != "test_golden_path.py" for path in files)
    assert files == sorted(files)
