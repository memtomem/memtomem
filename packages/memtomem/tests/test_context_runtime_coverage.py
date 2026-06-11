"""Unit tests for the shared runtime_coverage calculation module."""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context.runtime_coverage import compute_runtime_coverage

from .helpers import set_home


@pytest.fixture()
def isolated_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Empty fake home so machine-level runtime dirs can't leak into probes.

    The settings-availability probe (ADR-0009 §1) reads ``Path.home()``
    directly — without this, a dev machine's real ``~/.claude`` would flip
    every ``available`` assertion below.
    """
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    set_home(monkeypatch, fake_home)
    return fake_home


def test_compute_runtime_coverage_empty_dir(tmp_path: Path, isolated_home: Path) -> None:
    """An empty directory (and empty home) has no available runtimes."""
    project = tmp_path / "proj"
    project.mkdir()
    coverage = compute_runtime_coverage(project)
    assert len(coverage) == 4
    names = {c["name"] for c in coverage}
    assert names == {"claude", "gemini", "codex", "kimi"}
    for item in coverage:
        assert item["available"] is False


def test_compute_runtime_coverage_with_markers(tmp_path: Path, isolated_home: Path) -> None:
    """Markers trigger available=True for targeted runtimes."""
    project = tmp_path / "proj"
    # Claude marker directory
    (project / ".claude" / "skills").mkdir(parents=True)
    # Kimi config file marker
    (project / ".kimi").mkdir()
    (project / ".kimi" / "config.toml").touch()

    coverage = compute_runtime_coverage(project)
    coverage_dict = {c["name"]: c for c in coverage}

    assert coverage_dict["claude"]["available"] is True
    assert coverage_dict["kimi"]["available"] is True
    assert coverage_dict["gemini"]["available"] is False
    assert coverage_dict["codex"]["available"] is False


def test_settings_home_dir_counts_as_available(tmp_path: Path, isolated_home: Path) -> None:
    """ADR-0009 §1: the settings surface joins the availability OR.

    A machine-level runtime home (``~/.codex``) with a completely untouched
    project must light the chip — the settings generators' ``is_available``
    is home-OR-project per ADR-0010 §3.
    """
    (isolated_home / ".codex").mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    coverage = {c["name"]: c for c in compute_runtime_coverage(project)}

    assert coverage["codex"]["available"] is True
    assert coverage["claude"]["available"] is False
    assert coverage["gemini"]["available"] is False
    assert coverage["kimi"]["available"] is False


def test_settings_project_dir_counts_as_available(tmp_path: Path, isolated_home: Path) -> None:
    """Project-side runtime dir without any marker file is enough.

    ``<proj>/.kimi/`` (no ``config.toml``) is invisible to the
    RUNTIME_MARKER_FILES probe but satisfies ``KimiSettingsGenerator.
    is_available`` — pins that the settings probe is a distinct OR leg,
    not a restatement of the marker-file probe.
    """
    project = tmp_path / "proj"
    (project / ".kimi").mkdir(parents=True)

    coverage = {c["name"]: c for c in compute_runtime_coverage(project)}

    assert coverage["kimi"]["available"] is True
    assert coverage["claude"]["available"] is False


def test_home_isolation_negative_pin(tmp_path: Path, isolated_home: Path) -> None:
    """Empty home + empty project stays all-False even with home dirs nearby.

    Negative pin for the settings probe: creating an unrelated dir in the
    fake home must not light any chip (guards against an over-broad probe
    that keys on home existence rather than the runtime dir).
    """
    (isolated_home / ".not-a-runtime").mkdir()
    project = tmp_path / "proj"
    project.mkdir()

    coverage = {c["name"]: c for c in compute_runtime_coverage(project)}
    assert all(c["available"] is False for c in coverage.values())
