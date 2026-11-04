#!/usr/bin/env python3
"""Normalize and validate the CycloneDX SBOMs attached to GitHub Releases.

Two shapes: memtomem's core and all-extras pair (the flat invocation), and
langchain-memtomem's single SBOM (``release_sbom.py langchain ...``).
"""

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
    "tree-sitter",
    "urllib3",
}
_LANGCHAIN_ANCHORS = {"langchain-core", "langgraph", "memtomem"}
# How uv marks a workspace member that has no registry source.
_WORKSPACE_PATH = {"name": "uv:workspace:path", "value": "packages/memtomem"}


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


def _root_row(payload: dict[str, Any], *, label: str, name: str, version: str) -> dict[str, Any]:
    """The dependency row of the root component, after checking the format and the root."""
    if payload.get("bomFormat") != "CycloneDX" or payload.get("specVersion") != "1.5":
        raise SbomError(f"{label} SBOM must be CycloneDX 1.5")
    metadata = payload.get("metadata")
    component = metadata.get("component") if isinstance(metadata, dict) else None
    expected_root = {"name": name, "version": version, "type": "library"}
    if not isinstance(component, dict) or any(
        component.get(k) != v for k, v in expected_root.items()
    ):
        raise SbomError(f"{label} SBOM root component must be {name} {version} library")
    dependencies = payload.get("dependencies")
    if not isinstance(dependencies, list):
        raise SbomError(f"{label} SBOM dependencies must be a list")
    root_ref = component.get("bom-ref")
    rows = [row for row in dependencies if isinstance(row, dict) and row.get("ref") == root_ref]
    if not isinstance(root_ref, str) or not rows:
        raise SbomError(f"{label} SBOM has no dependency row for its root component")
    return rows[0]


def _validate_one(
    payload: dict[str, Any], *, label: str, version: str
) -> set[tuple[str, str, str]]:
    _root_row(payload, label=label, name="memtomem", version=version)
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


def validate_langchain(payload: dict[str, Any], *, version: str, core_version: str) -> None:
    """langchain-memtomem *version*, with memtomem as the workspace member at *core_version*.

    The lock resolves memtomem from the workspace, so uv writes it without a
    purl and with its workspace path. A memtomem from a registry at the same
    version would describe another artifact, so version equality alone is not
    enough: the workspace marker is required and a purl refused.
    """
    label = "langchain-memtomem"
    root = _root_row(payload, label=label, name="langchain-memtomem", version=version)
    names = {name for name, _, _ in _components(payload, label)}
    if missing := _LANGCHAIN_ANCHORS - names:
        raise SbomError(f"{label} SBOM misses required components: {sorted(missing)}")
    # Rows, not the de-duplicated set: two identical memtomem rows must not pass as one.
    rows = [row for row in payload["components"] if row["name"] == "memtomem"]
    if len(rows) != 1:
        raise SbomError(f"{label} SBOM must list memtomem once, found {len(rows)}")
    core = rows[0]
    if core["version"] != core_version:
        raise SbomError(
            f"{label} SBOM names memtomem {core['version']}, the checkout {core_version}"
        )
    if "purl" in core or core.get("properties") != [_WORKSPACE_PATH]:
        raise SbomError(
            f"{label} SBOM memtomem must be the workspace member: no purl and "
            f"properties {[_WORKSPACE_PATH]}"
        )
    direct = root.get("dependsOn")
    expected = {
        row.get("bom-ref")
        for row in payload["components"]
        if row["name"] in {"memtomem", "langgraph"}
    }
    if not isinstance(direct, list) or None in expected or not expected <= set(direct):
        raise SbomError(f"{label} SBOM root must depend on memtomem and langgraph")


def normalize_validate_langchain(*, raw: Path, out: Path, version: str, core_version: str) -> None:
    payload = normalize(_load(raw))
    validate_langchain(payload, version=version, core_version=core_version)
    out.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _main_langchain(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="release_sbom.py langchain",
        description="Normalize and validate langchain-memtomem's SBOM.",
        allow_abbrev=False,
    )
    parser.add_argument("--raw", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--core-version", required=True)
    args = parser.parse_args(argv)
    try:
        normalize_validate_langchain(
            raw=args.raw, out=args.out, version=args.version, core_version=args.core_version
        )
    except (OSError, UnicodeError, SbomError) as exc:
        print(f"SBOM validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["langchain"]:
        return _main_langchain(argv[1:])
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
