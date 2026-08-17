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


def _umbrella_members(requirements: list[str]) -> set[str]:
    """Extras named inside a ``memtomem[a,b,c]`` self-reference."""
    members: set[str] = set()
    for requirement in requirements:
        if match := re.search(r"\[([^\]]+)\]", requirement):
            members |= {part.strip() for part in match.group(1).split(",") if part.strip()}
    return members


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
    if UMBRELLA_EXTRA in optional:
        if outside := sorted(declared - _umbrella_members(optional[UMBRELLA_EXTRA])):
            errors.append(
                f"extras missing from '{UMBRELLA_EXTRA}': {outside} — the smoke "
                f"installs '{UMBRELLA_EXTRA}', so their probes would never run"
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
