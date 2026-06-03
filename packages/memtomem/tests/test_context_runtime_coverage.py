"""Unit tests for the shared runtime_coverage calculation module."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.runtime_coverage import compute_runtime_coverage


def test_compute_runtime_coverage_empty_dir(tmp_path: Path) -> None:
    """An empty directory has no available runtimes."""
    coverage = compute_runtime_coverage(tmp_path)
    assert len(coverage) == 4
    names = {c["name"] for c in coverage}
    assert names == {"claude", "gemini", "codex", "kimi"}
    for item in coverage:
        assert item["available"] is False


def test_compute_runtime_coverage_with_markers(tmp_path: Path) -> None:
    """Markers trigger available=True for targeted runtimes."""
    # Claude marker directory
    (tmp_path / ".claude" / "skills").mkdir(parents=True)
    # Kimi config file marker
    (tmp_path / ".kimi").mkdir()
    (tmp_path / ".kimi" / "config.toml").touch()

    coverage = compute_runtime_coverage(tmp_path)
    coverage_dict = {c["name"]: c for c in coverage}

    assert coverage_dict["claude"]["available"] is True
    assert coverage_dict["kimi"]["available"] is True
    assert coverage_dict["gemini"]["available"] is False
    assert coverage_dict["codex"]["available"] is False
