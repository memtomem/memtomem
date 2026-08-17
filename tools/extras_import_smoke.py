#!/usr/bin/env python3
"""Probe every distribution an optional extra installs, from an installed wheel.

Run this against a venv that has ``memtomem[all]`` installed. It is the
shared body of two checks that used to exist in only one place:

* ``release.yml`` preflight, which ran an inline
  ``python -c 'import fastapi, fastembed, ...'`` at tag time;
* nothing at all on pull requests, so a dependency bump that broke an
  extra's install or import first surfaced while cutting a release
  (found reviewing the openai 2.53.0 -> 3.0.0 bump, #2071).

Keeping the probe table here rather than inline in each workflow means the
two callers cannot drift apart, and ``--check-coverage`` fails closed when
``pyproject.toml`` and this table disagree.

The guard is deliberately per-*distribution*, not per-extra: an extra that
gains a dependency must gain a probe for it, or coverage fails. Requirements
are parsed with :mod:`packaging` (a base dependency) so environment markers,
extras, and version specifiers are read the way pip reads them rather than
approximated with regexes.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path

from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

# How to prove each distribution an extra installs actually arrived, keyed by
# distribution name. The usual probe is the top-level module it provides; a
# distribution that installs no importable module is probed as
# ``dist:`` instead (verified through installed metadata). Every *active*
# direct requirement of an extra must appear here — see ``--check-coverage``.
EXTRA_PROBES: dict[str, dict[str, str]] = {
    "onnx": {"fastembed": "fastembed", "urllib3": "urllib3"},
    "ollama": {"ollama": "ollama"},
    "openai": {"openai": "openai"},
    "korean": {"kiwipiepy": "kiwipiepy"},
    "code": {
        "tree-sitter": "tree_sitter",
        "tree-sitter-python": "tree_sitter_python",
        "tree-sitter-javascript": "tree_sitter_javascript",
        "tree-sitter-typescript": "tree_sitter_typescript",
    },
    "web": {"fastapi": "fastapi", "uvicorn": "uvicorn"},
    "langfuse": {"langfuse": "langfuse", "urllib3": "urllib3"},
    "langgraph": {"langgraph": "langgraph"},
}

# The distribution whose extras these are; only its own self-reference can
# count as umbrella coverage.
PROJECT_NAME = "memtomem"

# The extra whose job is to pull in every other extra. ``--check-coverage``
# verifies it actually does, because an extra missing from it would never be
# installed by the smoke and its probes would silently never run.
UMBRELLA_EXTRA = "all"

# ``all`` installs the extras above rather than distributions of its own.
META_EXTRAS = frozenset({UMBRELLA_EXTRA})

# Probe prefix for a distribution that installs no importable top-level
# module. Spelled explicitly rather than left blank: "nothing to import" and
# "nobody wrote the probe" must not look alike to the guard.
DIST_PREFIX = "dist:"


def _optional_dependencies(repo_root: Path) -> dict[str, list[str]]:
    pyproject = repo_root / "packages" / "memtomem" / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


def _is_active(requirement: Requirement) -> bool:
    """True when this requirement installs something in *this* environment.

    A marker-excluded requirement installs nothing here, so binding a probe
    to it would let an unrelated copy of the same distribution — pulled in by
    another extra or by the base dependencies — satisfy the probe.
    """
    return requirement.marker is None or requirement.marker.evaluate()


def _active_requirements(requirements: list[str]) -> dict[str, Requirement]:
    """Canonical distribution name -> requirement, for active requirements."""
    active: dict[str, Requirement] = {}
    for text in requirements:
        parsed = Requirement(text)
        if canonicalize_name(parsed.name) == canonicalize_name(PROJECT_NAME):
            continue  # self-reference: an umbrella edge, not a distribution
        if _is_active(parsed):
            active[canonicalize_name(parsed.name)] = parsed
    return active


def _inactive_names(requirements: list[str]) -> set[str]:
    return {
        canonicalize_name(parsed.name)
        for parsed in map(Requirement, requirements)
        if not _is_active(parsed)
    }


def _umbrella_members(requirements: list[str]) -> set[str]:
    """Extras named in an unconditional ``memtomem[...]`` self-reference.

    A marker makes the reference conditional, so it installs nothing on the
    interpreters the marker excludes and cannot count as coverage. Version
    specifiers are irrelevant and allowed (``memtomem[web]>=0.4``).
    """
    members: set[str] = set()
    for text in requirements:
        parsed = Requirement(text)
        if canonicalize_name(parsed.name) != canonicalize_name(PROJECT_NAME):
            continue
        if parsed.marker is not None:
            continue
        members |= set(parsed.extras)
    return members


def check_coverage(repo_root: Path) -> int:
    """Fail when the probe table and pyproject disagree in any direction."""
    optional = _optional_dependencies(repo_root)
    declared = set(optional) - META_EXTRAS
    probed = set(EXTRA_PROBES)
    errors: list[str] = []

    if missing := sorted(declared - probed):
        errors.append(f"extras declared with no probes: {missing}")
    if unknown := sorted(probed - declared):
        errors.append(f"probes for undeclared extras: {unknown}")

    if UMBRELLA_EXTRA not in optional:
        errors.append(
            f"no '{UMBRELLA_EXTRA}' extra declared — the smoke installs it, so "
            f"without it nothing would be probed at all"
        )
    elif outside := sorted(declared - _umbrella_members(optional[UMBRELLA_EXTRA])):
        errors.append(
            f"extras missing from '{UMBRELLA_EXTRA}': {outside} — the smoke "
            f"installs '{UMBRELLA_EXTRA}', so their probes would never run "
            f"(only an unconditional '{PROJECT_NAME}[...]' reference counts)"
        )

    for extra in sorted(probed & declared):
        # Canonicalize probe keys too, so the table may spell a distribution
        # the way its module does (``tree_sitter_python``) without the guard
        # reading that as a different distribution.
        probes = {canonicalize_name(name): probe for name, probe in EXTRA_PROBES[extra].items()}
        active = _active_requirements(optional[extra])
        if unprobed := sorted(set(active) - set(probes)):
            errors.append(
                f"extra '{extra}' installs {unprobed} with no probe — every "
                f"distribution an extra adds must be proved to arrive"
            )
        if stale := sorted(set(probes) - set(active)):
            inactive = _inactive_names(optional[extra])
            for name in stale:
                if name in inactive:
                    errors.append(
                        f"extra '{extra}' probes '{name}', which its marker "
                        f"excludes in this environment — the probe would be "
                        f"satisfied by some other extra's copy"
                    )
                else:
                    errors.append(
                        f"extra '{extra}' probes '{name}', which it does not "
                        f"declare (declares: {sorted(active)})"
                    )
        if empty := sorted(name for name, probe in probes.items() if not probe):
            errors.append(f"extra '{extra}' has empty probes for {empty}")

    if not errors:
        total = sum(len(probes) for probes in EXTRA_PROBES.values())
        print(f"extras coverage OK ({len(probed)} extras, {total} distributions probed)")
        return 0
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"Add or remove entries in {Path(__file__).name}:EXTRA_PROBES.", file=sys.stderr)
    return 1


def run_imports() -> int:
    """Run every probe in the current interpreter."""
    failures: list[str] = []
    for extra, probes in sorted(EXTRA_PROBES.items()):
        for dist_name, probe in sorted(probes.items()):
            try:
                if probe.startswith(DIST_PREFIX):
                    distribution(probe[len(DIST_PREFIX) :])
                else:
                    importlib.import_module(probe)
            except Exception as exc:  # noqa: BLE001 - report every failure, not the first
                failures.append(f"{extra}: probe {probe} for {dist_name} failed: {exc!r}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    total = sum(len(probes) for probes in EXTRA_PROBES.values())
    print(f"all-extras probe smoke OK ({total} distributions, {len(EXTRA_PROBES)} extras)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-coverage",
        action="store_true",
        help="Only verify EXTRA_PROBES matches pyproject; do not import anything.",
    )
    parser.add_argument(
        "--repo-root",
        type=Path,
        default=Path("."),
        help="Repository root holding packages/memtomem/pyproject.toml.",
    )
    args = parser.parse_args(argv)
    if args.check_coverage:
        return check_coverage(args.repo_root)
    return run_imports()


if __name__ == "__main__":
    raise SystemExit(main())
