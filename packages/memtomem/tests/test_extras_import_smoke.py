"""Tests for the shared all-extras import smoke helper.

The guard's whole value is failing closed, so every negative case below
pins a way the coverage check could silently pass and let an extra ship
unprobed (Codex review of the PR that introduced it).
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "extras_import_smoke.py"
    spec = importlib.util.spec_from_file_location("extras_import_smoke", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


smoke = _load_tool()


def _write_pyproject(root: Path, optional: dict[str, list[str]]) -> Path:
    package = root / "packages" / "memtomem"
    package.mkdir(parents=True)
    body = [
        "[project]",
        'name = "memtomem"',
        'version = "0.0.0"',
        "",
        "[project.optional-dependencies]",
    ]
    for extra, requirements in optional.items():
        # json.dumps, not an f-string: markers embed double quotes.
        rendered = ", ".join(json.dumps(item) for item in requirements)
        body.append(f"{extra} = [{rendered}]")
    (package / "pyproject.toml").write_text("\n".join(body) + "\n")
    return root


def _fixture(tmp_path: Path, optional: dict[str, list[str]]) -> Path:
    return _write_pyproject(tmp_path, optional)


class TestCheckCoverage:
    def test_real_repository_passes(self) -> None:
        """The shipped table must match the shipped pyproject."""
        assert smoke.check_coverage(_ROOT) == 0

    def test_declared_extra_without_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",)})
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115"],
                "brandnew": ["something>=1"],
                "all": ["memtomem[web,brandnew]"],
            },
        )
        assert smoke.check_coverage(root) == 1

    def test_probe_for_undeclared_extra_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",), "removed": ("gone",)})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1

    def test_empty_probe_tuple_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty probe must not be a way to satisfy the name check."""
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ()})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1
        assert "empty probe" in capsys.readouterr().err

    def test_extra_missing_from_all_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``all`` omitting an extra means its probe would never run."""
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",), "korean": ("kiwipiepy",)})
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115"],
                "korean": ["kiwipiepy>=0.18"],
                "all": ["memtomem[web]"],
            },
        )
        assert smoke.check_coverage(root) == 1
        assert "korean" in capsys.readouterr().err

    def test_fully_covered_fixture_passes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The positive control: same shape, nothing missing."""
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",), "korean": ("kiwipiepy",)})
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115"],
                "korean": ["kiwipiepy>=0.18"],
                "all": ["memtomem[web,korean]"],
            },
        )
        assert smoke.check_coverage(root) == 0


class TestRunImports:
    def test_missing_module_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("definitely_not_installed_xyz",)})
        assert smoke.run_imports() == 1

    def test_reports_every_failure_not_just_the_first(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(
            smoke,
            "EXTRA_MODULES",
            {"a": ("no_such_module_one",), "b": ("no_such_module_two",)},
        )
        assert smoke.run_imports() == 1
        err = capsys.readouterr().err
        assert "no_such_module_one" in err
        assert "no_such_module_two" in err

    def test_distribution_probe_resolves_without_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``dist:`` probes cover extras that install no importable module.

        ``pytest`` stands in for such a distribution only as a *runtime*
        target here; whether an extra may legitimately probe it is the
        coverage check's business, pinned in ``TestDistProbeBinding``.
        """
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"meta": (f"{smoke.DIST_PREFIX}pytest",)})
        assert smoke.run_imports() == 0

    def test_missing_distribution_probe_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            smoke, "EXTRA_MODULES", {"meta": (f"{smoke.DIST_PREFIX}no-such-distribution-xyz",)}
        )
        assert smoke.run_imports() == 1


class TestUmbrellaParsing:
    """Only a bare ``memtomem[...]`` self-reference may count as coverage."""

    def test_foreign_bracket_is_not_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",)})
        root = _fixture(
            tmp_path,
            {"web": ["fastapi>=0.115"], "all": ["unrelated[web]"]},
        )
        assert smoke.check_coverage(root) == 1
        assert "web" in capsys.readouterr().err

    def test_marked_self_reference_is_not_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A marker means the extra installs nothing on excluded interpreters."""
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",)})
        root = _fixture(
            tmp_path,
            {"web": ["fastapi>=0.115"], "all": ['memtomem[web]; python_version < "3.12"']},
        )
        assert smoke.check_coverage(root) == 1

    def test_missing_umbrella_extra_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Deleting ``all`` must not skip the membership check."""
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": ("fastapi",)})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"]})
        assert smoke.check_coverage(root) == 1
        assert "all" in capsys.readouterr().err


class TestDistProbeBinding:
    """A ``dist:`` probe must name a distribution its own extra declares."""

    def test_unrelated_installed_distribution_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": (f"{smoke.DIST_PREFIX}httpx",)})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1
        assert "does not" in capsys.readouterr().err

    def test_own_distribution_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_MODULES", {"web": (f"{smoke.DIST_PREFIX}fastapi",)})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 0

    def test_name_normalization_matches_pep503(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``tree_sitter_python`` and ``tree-sitter-python`` are one name."""
        monkeypatch.setattr(
            smoke, "EXTRA_MODULES", {"code": (f"{smoke.DIST_PREFIX}tree_sitter_python",)}
        )
        root = _fixture(tmp_path, {"code": ["tree-sitter-python>=0.23"], "all": ["memtomem[code]"]})
        assert smoke.check_coverage(root) == 0
