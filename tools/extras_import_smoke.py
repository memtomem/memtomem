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

Two deliberate limits keep the guard small enough to trust:

* It models only plain, unconditional requirements. A requirement carrying
  an environment marker or a direct URL is **rejected**, not interpreted —
  the coverage check and the smoke run in different interpreters, so any
  marker logic here could validate a set the smoke never installs.
* Coverage is over each extra's *direct* requirements. Transitive breakage
  is left to ``uv pip check`` and the release preflight.

``--check-coverage`` needs ``packaging`` and is only ever run from the repo
checkout; ``run_imports`` deliberately imports nothing beyond the standard
library, because it also runs inside the isolated ``[all]`` release venv,
where ``packaging`` is not a declared dependency.
"""

from __future__ import annotations

import argparse
import importlib
import sys
import tomllib
from importlib.metadata import distribution, packages_distributions
from pathlib import Path

# How to prove each distribution an extra installs actually arrived, keyed by
# distribution name. The value is the top-level module that distribution
# provides — verified to *belong* to it, not merely to be importable — or
# ``DIST_ONLY`` for a distribution that installs no importable module.
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

# Probe value for a distribution that installs no importable top-level
# module. Spelled explicitly rather than left blank: "nothing to import" and
# "nobody wrote the probe" must not look alike to the guard.
DIST_ONLY = "<dist-only>"


def _canonicalize(name: str) -> str:
    """PEP 503 name normalization, without importing ``packaging``."""
    out = []
    previous_dash = False
    for char in name.strip().lower():
        if char in "-_.":
            if not previous_dash:
                out.append("-")
            previous_dash = True
        else:
            out.append(char)
            previous_dash = False
    return "".join(out)


def _optional_dependencies(repo_root: Path) -> dict[str, list[str]]:
    pyproject = repo_root / "packages" / "memtomem" / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


def check_coverage(repo_root: Path) -> int:
    """Fail when the probe table and pyproject disagree in any direction."""
    from packaging.requirements import Requirement  # noqa: PLC0415 - see module docstring

    optional = _optional_dependencies(repo_root)
    declared = set(optional) - META_EXTRAS
    probed = set(EXTRA_PROBES)
    errors: list[str] = []

    if missing := sorted(declared - probed):
        errors.append(f"extras declared with no probes: {missing}")
    if unknown := sorted(probed - declared):
        errors.append(f"probes for undeclared extras: {unknown}")

    def parse(extra: str, text: str) -> Requirement | None:
        """Parse a requirement, rejecting forms this guard does not model."""
        parsed = Requirement(text)
        if parsed.marker is not None:
            errors.append(
                f"extra '{extra}' declares '{text}' with an environment "
                f"marker — not modeled, because coverage and the smoke run in "
                f"different interpreters; handle it explicitly instead"
            )
            return None
        if parsed.url is not None:
            errors.append(f"extra '{extra}' declares '{text}' as a direct URL — not modeled")
            return None
        return parsed

    # Self-references are umbrella edges, not distributions: they need no
    # probe, but the extras they name must exist.
    self_referenced: dict[str, set[str]] = {}
    for extra in sorted(set(optional)):
        members: set[str] = set()
        for text in optional[extra]:
            parsed = parse(extra, text)
            if parsed is None:
                continue
            if _canonicalize(parsed.name) == _canonicalize(PROJECT_NAME):
                members |= set(parsed.extras)
        if unknown_members := sorted(members - set(optional)):
            errors.append(
                f"extra '{extra}' references '{PROJECT_NAME}{sorted(unknown_members)}', "
                f"which is not a declared extra"
            )
        self_referenced[extra] = members

    if UMBRELLA_EXTRA not in optional:
        errors.append(
            f"no '{UMBRELLA_EXTRA}' extra declared — the smoke installs it, so "
            f"without it nothing would be probed at all"
        )
    elif outside := sorted(declared - self_referenced.get(UMBRELLA_EXTRA, set())):
        errors.append(
            f"extras missing from '{UMBRELLA_EXTRA}': {outside} — the smoke "
            f"installs '{UMBRELLA_EXTRA}', so their probes would never run"
        )

    for extra in sorted(probed & declared):
        probes = {_canonicalize(name): probe for name, probe in EXTRA_PROBES[extra].items()}
        required = {
            _canonicalize(parsed.name)
            for text in optional[extra]
            if (parsed := parse(extra, text)) is not None
            and _canonicalize(parsed.name) != _canonicalize(PROJECT_NAME)
        }
        if unprobed := sorted(required - set(probes)):
            errors.append(
                f"extra '{extra}' installs {unprobed} with no probe — every "
                f"distribution an extra adds must be proved to arrive"
            )
        if foreign := sorted(set(probes) - required):
            errors.append(
                f"extra '{extra}' probes {foreign}, which it does not declare "
                f"(declares: {sorted(required)})"
            )
        if empty := sorted(name for name, probe in probes.items() if not probe):
            errors.append(f"extra '{extra}' has empty probes for {empty}")

    if not errors:
        total = sum(len(probes) for probes in EXTRA_PROBES.values())
        print(f"extras coverage OK ({len(probed)} extras, {total} distributions probed)")
        return 0
    for error in dict.fromkeys(errors):  # stable order, no duplicates
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"Add or remove entries in {Path(__file__).name}:EXTRA_PROBES.", file=sys.stderr)
    return 1


def run_imports() -> int:
    """Run every probe in the current interpreter.

    Each probe proves two things about its own distribution: that the
    distribution is installed, and that the module named is one the
    distribution actually provides. Checking only that "some module imports"
    would let an unrelated module — or a copy another extra installed —
    stand in for it.
    """
    failures: list[str] = []
    owners = packages_distributions()
    for extra, probes in sorted(EXTRA_PROBES.items()):
        for dist_name, probe in sorted(probes.items()):
            label = f"{extra}: {dist_name}"
            try:
                distribution(dist_name)
            except Exception as exc:  # noqa: BLE001 - report every failure, not the first
                failures.append(f"{label} is not installed: {exc!r}")
                continue
            if probe == DIST_ONLY:
                continue
            try:
                importlib.import_module(probe)
            except Exception as exc:  # noqa: BLE001 - report every failure, not the first
                failures.append(f"{label}: import {probe} failed: {exc!r}")
                continue
            providers = {_canonicalize(name) for name in owners.get(probe, [])}
            if _canonicalize(dist_name) not in providers:
                # No fallback when ``providers`` is empty: a module no
                # distribution claims (a stdlib name, say) is precisely the
                # substitution this check exists to catch.
                failures.append(
                    f"{label}: module '{probe}' is provided by "
                    f"{sorted(providers) or 'no installed distribution'}, not by "
                    f"'{dist_name}' — the probe proves nothing about this extra"
                )
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
