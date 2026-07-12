#!/usr/bin/env python3
"""Normalize and validate memtomem's core and all-extras CycloneDX SBOMs."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


_CORE_ANCHORS = {"cryptography", "idna", "pyjwt", "python-multipart", "starlette"}
_ALL_ANCHORS = {
    "fastapi",
    "fastembed",
    "kiwipiepy",
    "langfuse",
    "ollama",
    "openai",
    "tree-sitter",
    "urllib3",
}


class SbomError(RuntimeError):
    """An SBOM contract was not met."""


def _load(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SbomError(f"cannot read valid JSON from {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SbomError(f"SBOM root in {path} must be an object")
    return payload


def normalize(payload: dict[str, Any]) -> dict[str, Any]:
    """Remove optional time/random fields while preserving the dependency graph."""
    normalized = json.loads(json.dumps(payload))
    normalized.pop("serialNumber", None)
    metadata = normalized.get("metadata")
    if isinstance(metadata, dict):
        metadata.pop("timestamp", None)
    return normalized


def _components(payload: dict[str, Any], label: str) -> set[tuple[str, str, str]]:
    rows = payload.get("components")
    if not isinstance(rows, list):
        raise SbomError(f"{label} SBOM components must be a list")
    result: set[tuple[str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            raise SbomError(f"{label} SBOM contains a non-object component")
        name, version, purl = row.get("name"), row.get("version"), row.get("purl", "")
        if not isinstance(name, str) or not isinstance(version, str) or not isinstance(purl, str):
            raise SbomError(f"{label} SBOM component misses name/version/purl")
        result.add((name, version, purl))
    return result


def _validate_one(
    payload: dict[str, Any], *, label: str, version: str
) -> set[tuple[str, str, str]]:
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise SbomError(f"{label} SBOM must be CycloneDX 1.5")
    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    expected_root = {"name": "memtomem", "version": version, "type": "library"}
    if not isinstance(component, dict) or any(
        component.get(k) != v for k, v in expected_root.items()
    ):
        raise SbomError(f"{label} SBOM root component must be memtomem {version} library")
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise SbomError(f"{label} SBOM dependencies must be a list")
    root_ref = component.get("bom-ref")
    if not isinstance(root_ref, str) or not any(
        isinstance(row, dict) and row.get("ref") == root_ref for row in dependencies
    ):
        raise SbomError(f"{label} SBOM has no dependency row for its root component")
    return _components(payload, label)


def normalize_validate_pair(
    *, core_raw: Path, all_raw: Path, core_out: Path, all_out: Path, version: str
) -> None:
    core = normalize(_load(core_raw))
    all_extras = normalize(_load(all_raw))
    core_components = _validate_one(core, label="core", version=version)
    all_components = _validate_one(all_extras, label="all", version=version)
    if not core_components <= all_components:
        missing = sorted(core_components - all_components)
        raise SbomError(f"all SBOM is not a superset of core: {missing[:5]}")
    core_names = {name for name, _, _ in core_components}
    all_names = {name for name, _, _ in all_components}
    if missing := _CORE_ANCHORS - core_names:
        raise SbomError(f"core SBOM misses required components: {sorted(missing)}")
    if missing := _ALL_ANCHORS - all_names:
        raise SbomError(f"all SBOM misses extras components: {sorted(missing)}")
    for path, payload in ((core_out, core), (all_out, all_extras)):
        path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--core-raw", type=Path, required=True)
    parser.add_argument("--all-raw", type=Path, required=True)
    parser.add_argument("--core-out", type=Path, required=True)
    parser.add_argument("--all-out", type=Path, required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        normalize_validate_pair(
            core_raw=args.core_raw,
            all_raw=args.all_raw,
            core_out=args.core_out,
            all_out=args.all_out,
            version=args.version,
        )
    except (OSError, UnicodeError, SbomError) as exc:
        print(f"SBOM validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
