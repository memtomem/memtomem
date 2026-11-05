#!/usr/bin/env python3
"""Normalize and validate the CycloneDX SBOMs attached to GitHub Releases.

Two shapes: memtomem's core and all-extras pair (the flat invocation), and
langchain-memtomem's single SBOM (``release_sbom.py langchain ...``).

Each SBOM's dependency edges are checked against the uv.lock it was exported
from (``--lock``). uv writes an edge that a package declares only under an
extra nothing in the graph activates, whenever the target is installed for
another reason; those edges are dropped and reported. An edge the lock does
not declare at all is refused rather than rewritten.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from collections.abc import Iterable
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


def load_lock(path: Path) -> dict[str, Any]:
    try:
        lock = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as exc:
        raise SbomError(f"cannot read a valid uv.lock from {path}: {exc}") from exc
    if not isinstance(lock.get("package"), list):
        raise SbomError(f"{path} has no [[package]] entries")
    return lock


def _canonical(name: str) -> str:
    """PEP 503 name normalization, the form uv.lock records."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _entry_names(entries: Any, where: str) -> list[tuple[str, list[str]]]:
    """(canonical target, requested extras) for a lock dependency list."""
    if not isinstance(entries, list):
        raise SbomError(f"uv.lock {where} must be a list")
    result = []
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        extras = entry.get("extra", []) if isinstance(entry, dict) else None
        if not isinstance(name, str) or not isinstance(extras, list):
            raise SbomError(f"uv.lock {where} has a malformed entry: {entry!r}")
        result.append((_canonical(name), [_canonical(e) for e in extras]))
    return result


def reconcile_edges(
    payload: dict[str, Any], lock: dict[str, Any], *, all_root_extras: bool
) -> list[tuple[str, str]]:
    """Drop edges declared only by inactive extras; return them as sorted (source, target).

    Every component is matched to its lock package by canonical name and version.
    An extra is active when the root is exported with ``--all-extras`` or when a
    package in the graph requests it, through a base dependency or an active
    extra's dependency. Markers are ignored, so activation is a superset of what
    an installer would pick: a false edge may survive, a declared one is never
    dropped. Each edge ``A -> T`` is kept when the lock declares T among A's base
    or active-extra dependencies, dropped when T appears only under A's inactive
    extras, and refused otherwise. The lock must hold one package per name and no
    dependency group but ``dev`` on a package in the graph, so that names identify
    packages and ``--no-dev`` excluded every group.
    """
    by_name: dict[str, dict[str, Any]] = {}
    for package in lock["package"]:
        name = package.get("name") if isinstance(package, dict) else None
        if not isinstance(name, str):
            raise SbomError(f"uv.lock has a package without a name: {package!r}")
        key = _canonical(name)
        if key in by_name:
            raise SbomError(f"uv.lock holds more than one package named {key}")
        by_name[key] = package

    metadata = payload.get("metadata")
    root = metadata.get("component") if isinstance(metadata, dict) else None
    components = payload.get("components")
    dependencies = payload.get("dependencies")
    if not isinstance(root, dict) or not isinstance(components, list):
        raise SbomError("SBOM needs a root component and a component list")
    if not isinstance(dependencies, list):
        raise SbomError("SBOM dependencies must be a list")
    names: dict[str, str] = {}  # bom-ref -> canonical name
    for row in [root, *components]:
        ref, name, version = row.get("bom-ref"), row.get("name"), row.get("version")
        if not isinstance(ref, str) or not isinstance(name, str) or not isinstance(version, str):
            raise SbomError(f"SBOM component misses bom-ref/name/version: {row!r}")
        key = _canonical(name)
        package = by_name.get(key)
        if package is None:
            raise SbomError(f"SBOM component {name} {version} is not in uv.lock")
        if package.get("version") != version:
            raise SbomError(
                f"SBOM component {name} {version} does not match uv.lock "
                f"version {package.get('version')}"
            )
        groups = set(package.get("dev-dependencies", {})) - {"dev"}
        if groups:
            raise SbomError(f"uv.lock package {key} declares dependency groups {sorted(groups)}")
        if ref in names:
            raise SbomError(f"SBOM bom-ref {ref} is used twice")
        names[ref] = key
    root_ref = root["bom-ref"]
    present = set(names.values())

    def base(key: str) -> list[tuple[str, list[str]]]:
        return _entry_names(by_name[key].get("dependencies", []), f"{key} dependencies")

    def optional(key: str) -> dict[str, list[tuple[str, list[str]]]]:
        table = by_name[key].get("optional-dependencies", {})
        if not isinstance(table, dict):
            raise SbomError(f"uv.lock {key} optional-dependencies must be a table")
        return {
            _canonical(extra): _entry_names(entries, f"{key}[{extra}]")
            for extra, entries in table.items()
        }

    active: dict[str, set[str]] = {key: set() for key in present}
    if all_root_extras:
        active[names[root_ref]] |= set(optional(names[root_ref]))
    changed = True
    while changed:
        changed = False
        for key in present:
            extras = optional(key)
            entries = base(key) + [e for x in active[key] for e in extras.get(x, [])]
            for target, requested in entries:
                if target in active and not set(requested) <= active[target]:
                    active[target] |= set(requested)
                    changed = True

    def declared(key: str, extras: Iterable[str]) -> set[str]:
        table = optional(key)
        return {target for x in extras for target, _ in table.get(x, [])}

    dropped: list[tuple[str, str]] = []
    rows_seen: set[str] = set()
    for row in dependencies:
        if not isinstance(row, dict) or row.get("ref") not in names:
            raise SbomError(f"SBOM dependency row names no component: {row!r}")
        # One row per component: the root check reads the first, a reader may take any.
        if row["ref"] in rows_seen:
            raise SbomError(f"SBOM has more than one dependency row for {row['ref']}")
        rows_seen.add(row["ref"])
        source = names[row["ref"]]
        targets = row.get("dependsOn", [])
        if not isinstance(targets, list):
            raise SbomError(f"SBOM dependsOn of {source} must be a list")
        allowed = {t for t, _ in base(source)} | declared(source, active[source])
        inactive = declared(source, set(optional(source)) - active[source])
        kept = []
        for ref in targets:
            target = names.get(ref)
            if target is None:
                raise SbomError(f"SBOM edge {source} -> {ref} names no component")
            if target in allowed:
                kept.append(ref)
            elif target in inactive:
                dropped.append((source, target))
            else:
                raise SbomError(f"SBOM edge {source} -> {target} is not declared in uv.lock")
        if kept != targets:
            row["dependsOn"] = kept

    graph = {row["ref"]: row.get("dependsOn", []) for row in dependencies}
    seen, stack = {root_ref}, [root_ref]
    while stack:
        for ref in graph.get(stack.pop(), []):
            if ref not in seen:
                seen.add(ref)
                stack.append(ref)
    if unreachable := sorted(names[ref] for ref in names.keys() - seen):
        raise SbomError(f"SBOM components unreachable from the root: {unreachable}")
    return sorted(dropped)


def _report(label: str, dropped: list[tuple[str, str]]) -> None:
    for source, target in dropped:
        print(
            f"{label}: dropped an edge only an inactive extra declares: {source} -> {target}",
            file=sys.stderr,
        )


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
    *, core_raw: Path, all_raw: Path, core_out: Path, all_out: Path, version: str, lock: Path
) -> None:
    core = normalize(_load(core_raw))
    all_extras = normalize(_load(all_raw))
    core_components = _validate_one(core, label="core", version=version)
    all_components = _validate_one(all_extras, label="all", version=version)
    lock_data = load_lock(lock)
    _report("core", reconcile_edges(core, lock_data, all_root_extras=False))
    _report("all", reconcile_edges(all_extras, lock_data, all_root_extras=True))
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


def normalize_validate_langchain(
    *, raw: Path, out: Path, version: str, core_version: str, lock: Path
) -> None:
    payload = normalize(_load(raw))
    validate_langchain(payload, version=version, core_version=core_version)
    _report(
        "langchain-memtomem",
        reconcile_edges(payload, load_lock(lock), all_root_extras=False),
    )
    # Again on what is written: the root's required edges must survive the drop.
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
    parser.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        normalize_validate_langchain(
            raw=args.raw,
            out=args.out,
            version=args.version,
            core_version=args.core_version,
            lock=args.lock,
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
    parser.add_argument("--lock", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        normalize_validate_pair(
            core_raw=args.core_raw,
            all_raw=args.all_raw,
            core_out=args.core_out,
            all_out=args.all_out,
            version=args.version,
            lock=args.lock,
        )
    except (OSError, UnicodeError, SbomError) as exc:
        print(f"SBOM validation failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
