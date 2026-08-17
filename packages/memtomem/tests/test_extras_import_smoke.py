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
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
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
        monkeypatch.setattr(
            smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}, "removed": {"gone": "gone"}}
        )
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1

    def test_empty_probe_string_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A blank probe must not be a way to satisfy the table."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": ""}})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1
        assert "empty probes" in capsys.readouterr().err

    def test_extra_with_no_probes_at_all_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {}})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1
        assert "no probe" in capsys.readouterr().err

    def test_extra_missing_from_all_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """``all`` omitting an extra means its probe would never run."""
        monkeypatch.setattr(
            smoke,
            "EXTRA_PROBES",
            {"web": {"fastapi": "fastapi"}, "korean": {"kiwipiepy": "kiwipiepy"}},
        )
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
        monkeypatch.setattr(
            smoke,
            "EXTRA_PROBES",
            {"web": {"fastapi": "fastapi"}, "korean": {"kiwipiepy": "kiwipiepy"}},
        )
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
        monkeypatch.setattr(
            smoke, "EXTRA_PROBES", {"web": {"fastapi": "definitely_not_installed_xyz"}}
        )
        assert smoke.run_imports() == 1

    def test_reports_every_failure_not_just_the_first(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Installed distributions whose probed modules do not exist."""
        monkeypatch.setattr(
            smoke,
            "EXTRA_PROBES",
            {
                "a": {"pytest": "no_such_module_one"},
                "b": {"packaging": "no_such_module_two"},
            },
        )
        assert smoke.run_imports() == 1
        err = capsys.readouterr().err
        assert "no_such_module_one" in err
        assert "no_such_module_two" in err

    def test_distribution_probe_resolves_without_import(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A probe passes when the module really belongs to its distribution."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"meta": {"pytest": "pytest"}})
        assert smoke.run_imports() == 0

    def test_dist_only_probe_checks_installation_alone(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DIST_ONLY covers a distribution with no importable module."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"meta": {"pytest": smoke.DIST_ONLY}})
        assert smoke.run_imports() == 0

    def test_module_not_owned_by_its_distribution_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The substitution this check exists to catch (Codex demonstrated it).

        ``import sys`` succeeds in any interpreter, so binding it to
        ``fastapi`` would make the probe prove nothing.
        """
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"pytest": "sys"}})
        assert smoke.run_imports() == 1
        assert "proves nothing" in capsys.readouterr().err

    def test_module_owned_by_another_distribution_fails(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A sibling extra's module cannot stand in for this one."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"pytest": "packaging"}})
        assert smoke.run_imports() == 1
        assert "proves nothing" in capsys.readouterr().err

    def test_missing_distribution_probe_fails(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            smoke,
            "EXTRA_PROBES",
            {"meta": {"no-such-distribution-xyz": smoke.DIST_ONLY}},
        )
        assert smoke.run_imports() == 1


class TestUmbrellaParsing:
    """Only a bare ``memtomem[...]`` self-reference may count as coverage."""

    def test_foreign_bracket_is_not_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
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
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(
            tmp_path,
            {"web": ["fastapi>=0.115"], "all": ['memtomem[web]; python_version < "3.12"']},
        )
        assert smoke.check_coverage(root) == 1

    def test_missing_umbrella_extra_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Deleting ``all`` must not skip the membership check."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"]})
        assert smoke.check_coverage(root) == 1
        assert "all" in capsys.readouterr().err


class TestDistProbeBinding:
    """A ``dist:`` probe must name a distribution its own extra declares."""

    def test_unrelated_installed_distribution_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"httpx": "httpx"}})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 1
        assert "does not" in capsys.readouterr().err

    def test_own_distribution_is_accepted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]"]})
        assert smoke.check_coverage(root) == 0

    def test_name_normalization_matches_pep503(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``tree_sitter_python`` and ``tree-sitter-python`` are one name."""
        monkeypatch.setattr(
            smoke,
            "EXTRA_PROBES",
            {"code": {"tree_sitter_python": "tree_sitter_python"}},
        )
        root = _fixture(tmp_path, {"code": ["tree-sitter-python>=0.23"], "all": ["memtomem[code]"]})
        assert smoke.check_coverage(root) == 0


class TestDistributionLevelCoverage:
    """Probes are per-distribution: an extra that gains a dep gains a probe."""

    def test_added_dependency_without_probe_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The extra name is still covered; the new distribution is not."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(
            tmp_path,
            {"web": ["fastapi>=0.115", "uvicorn>=0.49"], "all": ["memtomem[web]"]},
        )
        assert smoke.check_coverage(root) == 1
        assert "uvicorn" in capsys.readouterr().err

    def test_probe_supplied_by_another_extra_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`web` may not lean on a distribution only `korean` declares.

        In the combined `[all]` environment the import would succeed either
        way, which is exactly the masking this check exists to prevent.
        """
        monkeypatch.setattr(
            smoke,
            "EXTRA_PROBES",
            {
                "web": {"fastapi": "fastapi", "kiwipiepy": "kiwipiepy"},
                "korean": {"kiwipiepy": "kiwipiepy"},
            },
        )
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115"],
                "korean": ["kiwipiepy>=0.18"],
                "all": ["memtomem[web,korean]"],
            },
        )
        assert smoke.check_coverage(root) == 1
        assert "does not" in capsys.readouterr().err

    def test_any_marker_in_an_extra_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Markers are refused, not interpreted.

        Coverage runs in the repo interpreter while the probes run in a
        separate ``[all]`` venv, so any marker logic here could validate a
        set the smoke never installs. Refusing keeps the two honest; an
        extra that genuinely needs a marker must extend this guard.
        """
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115", 'kiwipiepy>=0.18; python_version < "3.12"'],
                "all": ["memtomem[web]"],
            },
        )
        assert smoke.check_coverage(root) == 1
        assert "marker" in capsys.readouterr().err

    def test_extra_marker_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """`extra != "web"` needs the selected extra to evaluate at all."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(
            tmp_path,
            {"web": ['fastapi>=0.115; extra != "web"'], "all": ["memtomem[web]"]},
        )
        assert smoke.check_coverage(root) == 1
        assert "marker" in capsys.readouterr().err

    def test_direct_url_requirement_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(
            tmp_path,
            {"web": ["fastapi @ https://example.invalid/fastapi.whl"], "all": ["memtomem[web]"]},
        )
        assert smoke.check_coverage(root) == 1
        assert "URL" in capsys.readouterr().err

    def test_self_reference_to_unknown_extra_is_rejected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """An empty extra pointing at a nonexistent extra must not pass."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}, "bundle": {}})
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115"],
                "bundle": ["memtomem[does-not-exist]"],
                "all": ["memtomem[web,bundle]"],
            },
        )
        assert smoke.check_coverage(root) == 1
        assert "not a declared extra" in capsys.readouterr().err

    def test_self_reference_with_specifier_is_coverage(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`memtomem[web]>=0.4` is a legal unconditional self-reference."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}})
        root = _fixture(tmp_path, {"web": ["fastapi>=0.115"], "all": ["memtomem[web]>=0.4"]})
        assert smoke.check_coverage(root) == 0

    def test_self_reference_is_not_probed_as_a_distribution(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An extra referencing a sibling extra needs no probe for memtomem."""
        monkeypatch.setattr(smoke, "EXTRA_PROBES", {"web": {"fastapi": "fastapi"}, "bundle": {}})
        root = _fixture(
            tmp_path,
            {
                "web": ["fastapi>=0.115"],
                "bundle": ["memtomem[web]"],
                "all": ["memtomem[web,bundle]"],
            },
        )
        assert smoke.check_coverage(root) == 0
