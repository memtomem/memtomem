"""Tests for release SBOM normalization and structural validation."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType

import pytest


_ROOT = Path(__file__).resolve().parents[3]
_CORE = {"cryptography", "idna", "pyjwt", "python-multipart", "starlette"}
_EXTRAS = {
    "fastapi",
    "fastembed",
    "kiwipiepy",
    "langfuse",
    "ollama",
    "openai",
    "tree-sitter",
    "urllib3",
}


def _load_tool() -> ModuleType:
    path = _ROOT / "tools" / "release_sbom.py"
    spec = importlib.util.spec_from_file_location("release_sbom", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rs = _load_tool()


def _payload(names: set[str], *, version: str = "0.3.6", serial: str = "one") -> dict:
    root_ref = f"memtomem@{version}"
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.5",
        "serialNumber": f"urn:uuid:{serial}",
        "metadata": {
            "timestamp": "2026-07-12T00:00:00Z",
            "component": {
                "type": "library",
                "bom-ref": root_ref,
                "name": "memtomem",
                "version": version,
            },
        },
        "components": [
            {
                "type": "library",
                "bom-ref": f"{name}@1",
                "name": name,
                "version": "1",
                "purl": f"pkg:pypi/{name}@1",
            }
            for name in sorted(names)
        ],
        "dependencies": [{"ref": root_ref, "dependsOn": [f"{name}@1" for name in sorted(names)]}],
    }


def _write(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_normalize_removes_only_optional_nondeterminism() -> None:
    payload = _payload(_CORE)
    normalized = rs.normalize(payload)
    assert "serialNumber" not in normalized
    assert "timestamp" not in normalized["metadata"]
    assert normalized["components"] == payload["components"]


def test_pair_is_validated_and_written_reproducibly(tmp_path: Path) -> None:
    core_a, all_a = tmp_path / "core-a.json", tmp_path / "all-a.json"
    core_b, all_b = tmp_path / "core-b.json", tmp_path / "all-b.json"
    _write(core_a, _payload(_CORE, serial="one"))
    _write(all_a, _payload(_CORE | _EXTRAS, serial="two"))
    _write(core_b, _payload(_CORE, serial="three"))
    _write(all_b, _payload(_CORE | _EXTRAS, serial="four"))
    first_core, first_all = tmp_path / "first-core.json", tmp_path / "first-all.json"
    second_core, second_all = tmp_path / "second-core.json", tmp_path / "second-all.json"

    rs.normalize_validate_pair(
        core_raw=core_a,
        all_raw=all_a,
        core_out=first_core,
        all_out=first_all,
        version="0.3.6",
    )
    rs.normalize_validate_pair(
        core_raw=core_b,
        all_raw=all_b,
        core_out=second_core,
        all_out=second_all,
        version="0.3.6",
    )

    assert first_core.read_bytes() == second_core.read_bytes()
    assert first_all.read_bytes() == second_all.read_bytes()


def test_pair_rejects_all_graph_that_is_not_core_superset(tmp_path: Path) -> None:
    core, all_raw = tmp_path / "core.json", tmp_path / "all.json"
    _write(core, _payload(_CORE))
    _write(all_raw, _payload((_CORE - {"starlette"}) | _EXTRAS))
    with pytest.raises(rs.SbomError, match="not a superset"):
        rs.normalize_validate_pair(
            core_raw=core,
            all_raw=all_raw,
            core_out=tmp_path / "out-core.json",
            all_out=tmp_path / "out-all.json",
            version="0.3.6",
        )


def test_pair_rejects_wrong_root_version(tmp_path: Path) -> None:
    core, all_raw = tmp_path / "core.json", tmp_path / "all.json"
    _write(core, _payload(_CORE, version="0.3.5"))
    _write(all_raw, _payload(_CORE | _EXTRAS))
    with pytest.raises(rs.SbomError, match="root component"):
        rs.normalize_validate_pair(
            core_raw=core,
            all_raw=all_raw,
            core_out=tmp_path / "out-core.json",
            all_out=tmp_path / "out-all.json",
            version="0.3.6",
        )
