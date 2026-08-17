#!/usr/bin/env python3
"""Import every module an optional extra installs, from an installed wheel.

Run this against a venv that has ``memtomem[all]`` installed. It is the
shared body of two checks that used to exist in only one place:

* ``release.yml`` preflight, which ran an inline
  ``python -c 'import fastapi, fastembed, ...'`` at tag time;
* nothing at all on pull requests, so a dependency bump that broke an
  extra's install or import first surfaced while cutting a release
  (found reviewing the openai 2.53.0 -> 3.0.0 bump, #2071).

Keeping the module list here rather than inline in each workflow means the
two callers cannot drift apart, and ``--check-coverage`` fails closed when a
new extra is declared in ``pyproject.toml`` without a probe added below.
"""

from __future__ import annotations

import argparse
import importlib
import re
import sys
import tomllib
from importlib.metadata import distribution
from pathlib import Path

# Top-level modules each extra is expected to make importable. Keys must
# cover every entry in ``[project.optional-dependencies]`` except the ``all``
# meta-extra, which only re-exports the others (see ``--check-coverage``).
EXTRA_MODULES: dict[str, tuple[str, ...]] = {
    "onnx": ("fastembed", "urllib3"),
    "ollama": ("ollama",),
    "openai": ("openai",),
    "korean": ("kiwipiepy",),
    "code": (
        "tree_sitter",
        "tree_sitter_python",
        "tree_sitter_javascript",
        "tree_sitter_typescript",
    ),
    "web": ("fastapi", "uvicorn"),
    "langfuse": ("langfuse", "urllib3"),
    "langgraph": ("langgraph",),
}

# ``all`` installs the extras above rather than distributions of its own.
META_EXTRAS = frozenset({"all"})

# The extra whose job is to pull in every other extra. ``--check-coverage``
# verifies it actually does, because an extra missing from it would never be
# installed by the smoke and its probe would silently never run.
UMBRELLA_EXTRA = "all"

# Probe prefix for a distribution that installs no importable top-level
# module. Use ``"dist:name"`` rather than an empty probe tuple, which is
# rejected: "nothing to import" and "nobody wrote the probe" must not look
# alike to the guard.
DIST_PREFIX = "dist:"


def _optional_dependencies(repo_root: Path) -> dict[str, list[str]]:
    pyproject = repo_root / "packages" / "memtomem" / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return tomllib.load(handle)["project"]["optional-dependencies"]


PROJECT_NAME = "memtomem"

# Only a bare, unconditional self-reference counts as umbrella coverage:
# ``memtomem[a,b]`` and nothing else on the line. A requirement carrying an
# environment marker installs nothing on the interpreters the marker excludes,
# and a bracket belonging to some *other* distribution says nothing about our
# extras — both used to be read as coverage. Anything this does not match
# fails closed rather than being interpreted.
_SELF_REFERENCE = re.compile(rf"^{PROJECT_NAME}\[([^\]\[;]+)\]$")

# PEP 503 name normalization, so ``tree-sitter``/``tree_sitter.python`` and
# friends compare equal to whatever spelling a ``dist:`` probe uses.
_NAME_SEPARATORS = re.compile(r"[-_.]+")

# Leading distribution name of a PEP 508 requirement, before any extras,
# version specifier, marker, or URL.
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def _normalize(name: str) -> str:
    return _NAME_SEPARATORS.sub("-", name).lower()


def _umbrella_members(requirements: list[str]) -> set[str]:
    """Extras named in an unconditional ``memtomem[a,b,c]`` self-reference."""
    members: set[str] = set()
    for requirement in requirements:
        if match := _SELF_REFERENCE.match(requirement.strip()):
            members |= {part.strip() for part in match.group(1).split(",") if part.strip()}
    return members


def _requirement_names(requirements: list[str]) -> set[str]:
    """Normalized distribution names an extra declares directly."""
    names: set[str] = set()
    for requirement in requirements:
        if match := _REQUIREMENT_NAME.match(requirement):
            names.add(_normalize(match.group(1)))
    return names


def check_coverage(repo_root: Path) -> int:
    """Fail when an extra is unprobed, probed emptily, or outside ``all``."""
    optional = _optional_dependencies(repo_root)
    declared = set(optional) - META_EXTRAS
    probed = set(EXTRA_MODULES)
    errors: list[str] = []

    if missing := sorted(declared - probed):
        errors.append(f"extras declared with no import probe: {missing}")
    if unknown := sorted(probed - declared):
        errors.append(f"import probes for undeclared extras: {unknown}")
    if empty := sorted(name for name, modules in EXTRA_MODULES.items() if not modules):
        errors.append(
            f"extras with an empty probe: {empty} — an extra that installs no "
            f"importable module must be probed as '{DIST_PREFIX}<distribution>'"
        )
    if UMBRELLA_EXTRA not in optional:
        errors.append(
            f"no '{UMBRELLA_EXTRA}' extra declared — the smoke installs it, so "
            f"without it nothing would be probed at all"
        )
    elif outside := sorted(declared - _umbrella_members(optional[UMBRELLA_EXTRA])):
        errors.append(
            f"extras missing from '{UMBRELLA_EXTRA}': {outside} — the smoke "
            f"installs '{UMBRELLA_EXTRA}', so their probes would never run "
            f"(coverage counts only a bare '{PROJECT_NAME}[...]' self-reference)"
        )

    # A ``dist:`` probe that names a distribution the extra does not declare
    # proves nothing: base and transitive packages resolve in the all-extras
    # environment whether or not the extra installed anything.
    for extra in sorted(probed & declared):
        declared_names = _requirement_names(optional[extra])
        for module in EXTRA_MODULES[extra]:
            if not module.startswith(DIST_PREFIX):
                continue
            target = _normalize(module[len(DIST_PREFIX) :])
            if target not in declared_names:
                errors.append(
                    f"extra '{extra}' probes '{module}', which it does not "
                    f"declare (declares: {sorted(declared_names)}) — a "
                    f"'{DIST_PREFIX}' probe must name one of the extra's own "
                    f"distributions"
                )

    if not errors:
        print(f"extras import coverage OK ({len(probed)} extras probed)")
        return 0
    for error in errors:
        print(f"ERROR: {error}", file=sys.stderr)
    print(f"Add or remove entries in {Path(__file__).name}:EXTRA_MODULES.", file=sys.stderr)
    return 1


def run_imports() -> int:
    """Import every probed module in the current interpreter."""
    failures: list[str] = []
    for extra, modules in sorted(EXTRA_MODULES.items()):
        for module in modules:
            try:
                if module.startswith(DIST_PREFIX):
                    distribution(module[len(DIST_PREFIX) :])
                else:
                    importlib.import_module(module)
            except Exception as exc:  # noqa: BLE001 - report every failure, not the first
                failures.append(f"{extra}: probe {module} failed: {exc!r}")
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    total = sum(len(modules) for modules in EXTRA_MODULES.values())
    print(f"all-extras import smoke OK ({total} modules, {len(EXTRA_MODULES)} extras)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check-coverage",
        action="store_true",
        help="Only verify EXTRA_MODULES matches pyproject; do not import anything.",
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
