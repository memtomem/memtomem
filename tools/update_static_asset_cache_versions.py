#!/usr/bin/env python3
"""Check or append the Web UI's content-aware cache-version bindings."""

from __future__ import annotations

import argparse
import asyncio
import sys

from memtomem.web.static_cache_manifest import (
    MANIFEST_PATH,
    StaticCacheManifestError,
    canonical_manifest_error,
    collect_runtime_references,
    contract_errors,
    load_manifest,
    render_manifest,
    updated_manifest,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Validate Web UI asset bytes against their public ?v=N cache keys."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="validate only (the default)",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="append bindings after public versions have already been incremented",
    )
    return parser


async def _run(*, write: bool) -> int:
    references = await collect_runtime_references()
    manifest = load_manifest()
    if write:
        manifest = updated_manifest(manifest, references)
        rendered = render_manifest(manifest)
        if MANIFEST_PATH.read_text(encoding="utf-8") != rendered:
            MANIFEST_PATH.write_text(rendered, encoding="utf-8")

    errors = contract_errors(manifest, references)
    canonical_error = canonical_manifest_error(manifest)
    if canonical_error is not None:
        errors.append(canonical_error)
    if errors:
        for error in errors:
            print(f"error: {error}", file=sys.stderr)
        return 1
    print(f"static cache contract OK ({len(manifest.assets)} assets)")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        return asyncio.run(_run(write=args.write))
    except (OSError, StaticCacheManifestError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
