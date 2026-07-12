#!/usr/bin/env python3
"""Run a stable file-level shard of the memtomem pytest suite."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


def test_files(repo_root: Path) -> list[Path]:
    """Return the regular pytest files covered by the cross-platform suite."""
    tests_root = repo_root / "packages" / "memtomem" / "tests"
    return [
        path for path in sorted(tests_root.rglob("test_*.py")) if path.name != "test_golden_path.py"
    ]


def shard_files(files: list[Path], *, index: int, count: int) -> list[Path]:
    """Partition sorted test files deterministically without overlap."""
    if count < 1:
        raise ValueError("shard count must be positive")
    if index < 0 or index >= count:
        raise ValueError(f"shard index must be in [0, {count}), got {index}")
    return files[index::count]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--shard-count", type=int, required=True)
    args, pytest_args = parser.parse_known_args(argv)
    if pytest_args[:1] == ["--"]:
        pytest_args = pytest_args[1:]

    repo_root = Path(__file__).resolve().parents[1]
    selected = shard_files(test_files(repo_root), index=args.shard_index, count=args.shard_count)
    if not selected:
        parser.error("selected shard contains no test files")
    command = [sys.executable, "-m", "pytest", *(str(path) for path in selected), *pytest_args]
    return subprocess.call(command, cwd=repo_root)  # noqa: S603


if __name__ == "__main__":
    raise SystemExit(main())
