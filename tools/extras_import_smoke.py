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
import sys
import tomllib
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


def _declared_extras(repo_root: Path) -> set[str]:
    pyproject = repo_root / "packages" / "memtomem" / "pyproject.toml"
    with pyproject.open("rb") as handle:
        return set(tomllib.load(handle)["project"]["optional-dependencies"])


def check_coverage(repo_root: Path) -> int:
    """Fail when an extra exists with no import probe (or vice versa)."""
    declared = _declared_extras(repo_root) - META_EXTRAS
    probed = set(EXTRA_MODULES)
    if declared == probed:
        print(f"extras import coverage OK ({len(probed)} extras probed)")
        return 0
    if missing := sorted(declared - probed):
        print(f"ERROR: extras declared with no import probe: {missing}", file=sys.stderr)
    if unknown := sorted(probed - declared):
        print(f"ERROR: import probes for undeclared extras: {unknown}", file=sys.stderr)
    print(f"Add or remove entries in {Path(__file__).name}:EXTRA_MODULES.", file=sys.stderr)
    return 1


def run_imports() -> int:
    """Import every probed module in the current interpreter."""
    failures: list[str] = []
    for extra, modules in sorted(EXTRA_MODULES.items()):
        for module in modules:
            try:
                importlib.import_module(module)
            except Exception as exc:  # noqa: BLE001 - report every failure, not the first
                failures.append(f"{extra}: import {module} failed: {exc!r}")
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
