#!/usr/bin/env python3
"""Smoke-test an *installed* langchain-memtomem against the memtomem beside it.

``install`` builds a fresh virtual environment with ``uv``, installs exactly
the given core requirement (a version spec or a wheel path) and the given
langchain-memtomem artifact into it, runs ``uv pip check``, then runs
``check`` with that environment's interpreter. It needs only the standard
library and ``uv`` on PATH, so ``uv run --no-project`` can start it.

``check`` refuses to run against a source checkout. It writes three items,
checks that a query search returns the right one with its value, closes,
reopens, searches again, deletes, and checks the deleted item is gone.
Then it opens a ``retrieval={"mode": "bm25"}`` store, checks it ranks, and
checks that ``{"mode": "dense"}`` without an embedder is refused. HOME
points at a scratch directory and every ``MEMTOMEM_*`` variable is dropped
before memtomem is imported, so the developer's own memtomem is never read.
``--expect-version`` and ``--expect-core`` make the installed distribution
versions part of the check: an install that resolved another memtomem than
the one under test fails here.

Both artifacts are passed to one install on purpose: with ``--find-links``,
uv prefers an indexed memtomem newer than a local wheel, so the core under
test would not be the one named.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ITEMS = {
    "seat": {"text": "prefers a window seat on long flights", "kind": "pref"},
    "food": {"text": "allergic to peanuts and tree nuts", "kind": "health"},
    "lang": {"text": "writes code reviews in Korean", "kind": "pref"},
}
NAMESPACE = ("users", "alice")


def _fail(message: str) -> None:
    raise SystemExit(f"langchain smoke failed: {message}")


def _check_installed(expect_version: str | None, expect_core: str | None) -> None:
    import langchain_memtomem
    import memtomem

    prefix = Path(sys.prefix).resolve()
    for module in (langchain_memtomem, memtomem):
        location = Path(module.__file__ or "").resolve()
        if prefix not in location.parents:
            _fail(f"{module.__name__} imported from {location}, outside {prefix}")
    for name, expected in (("langchain-memtomem", expect_version), ("memtomem", expect_core)):
        installed = importlib.metadata.version(name)
        if expected is not None and installed != expected:
            _fail(f"{name} {installed} is installed, expected {expected}")
        print(f"{name} {installed}")
    if langchain_memtomem.__version__ != importlib.metadata.version("langchain-memtomem"):
        _fail("langchain_memtomem.__version__ differs from the installed metadata")


async def _top_hit(store: object, query: str) -> tuple[str, dict]:
    hits = await store.asearch(("users",), query=query, limit=3)  # type: ignore[attr-defined]
    if not hits:
        _fail(f"no hit for {query!r}")
    return hits[0].key, hits[0].value


async def _no_hit(store: object, query: str, when: str) -> None:
    """``query`` is a word of every projection path and of no value (#3205)."""
    hits = await store.asearch(("users",), query=query, limit=3)  # type: ignore[attr-defined]
    if hits:
        _fail(f"{when}, the path word {query!r} returned {[hit.key for hit in hits]}")


async def _exercise(root: Path) -> None:
    from langchain_memtomem import MemtomemPipelineStore

    store = await MemtomemPipelineStore.open(root)
    try:
        for key, value in ITEMS.items():
            await store.aput(NAMESPACE, key, value)
        key, value = await _top_hit(store, "window seat")
        if (key, value) != ("seat", ITEMS["seat"]):
            _fail(f"query 'window seat' ranked {key!r} {value!r} first")
        await _no_hit(store, "projections", "before reopen")
    finally:
        await store.aclose()

    store = await MemtomemPipelineStore.open(root)
    try:
        key, value = await _top_hit(store, "peanuts")
        if (key, value) != ("food", ITEMS["food"]):
            _fail(f"after reopen, query 'peanuts' ranked {key!r} {value!r} first")
        await _no_hit(store, "projections", "after reopen")
        item = await store.aget(NAMESPACE, "lang")
        if item is None or item.value != ITEMS["lang"]:
            _fail(f"get after reopen returned {item!r}")
        await store.adelete(NAMESPACE, "food")
        hits = await store.asearch(("users",), query="peanuts", limit=3)
        if any(hit.key == "food" for hit in hits):
            _fail("a deleted item is still returned by a query search")
    finally:
        await store.aclose()
    print("put, search, reopen, get and delete behave")


async def _exercise_retrieval(root: Path) -> None:
    from langchain_memtomem import MemtomemPipelineStore

    store = await MemtomemPipelineStore.open(root / "bm25", retrieval={"mode": "bm25"})
    try:
        for key, value in ITEMS.items():
            await store.aput(NAMESPACE, key, value)
        key, value = await _top_hit(store, "window seat")
        if (key, value) != ("seat", ITEMS["seat"]):
            _fail(f"a bm25 store ranked {key!r} {value!r} first for 'window seat'")
    finally:
        await store.aclose()
    try:
        store = await MemtomemPipelineStore.open(root / "dense", retrieval={"mode": "dense"})
    except ValueError as exc:
        if "dense mode requires embeddings" not in str(exc):
            _fail(f"dense without an embedder was refused for another reason: {exc}")
    else:
        await store.aclose()
        _fail("a dense store opened without an embedder")
    print("retrieval: bm25 ranks, dense without an embedder is refused")


def _check(args: argparse.Namespace) -> int:
    with tempfile.TemporaryDirectory(prefix="lcmm-smoke-") as scratch:
        home = Path(scratch) / "home"
        home.mkdir()
        os.environ["HOME"] = str(home)
        os.environ["USERPROFILE"] = str(home)
        for name in [name for name in os.environ if name.startswith("MEMTOMEM_")]:
            del os.environ[name]
        _check_installed(args.expect_version, args.expect_core)
        asyncio.run(_exercise(Path(scratch) / "store"))
        asyncio.run(_exercise_retrieval(Path(scratch) / "retrieval"))
    return 0


def _install(args: argparse.Namespace) -> int:
    venv = Path(args.venv)
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")

    def run(*command: str) -> None:
        print("+", " ".join(command), flush=True)
        subprocess.run(command, check=True)

    run("uv", "venv", "--python", args.python, str(venv))
    run("uv", "pip", "install", "--python", str(python), args.core, args.artifact)
    run("uv", "pip", "check", "--python", str(python))
    expectations = []
    for flag, value in (
        ("--expect-version", args.expect_version),
        ("--expect-core", args.expect_core),
    ):
        if value is not None:
            expectations += [flag, value]
    run(str(python), str(Path(__file__).resolve()), "check", *expectations)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__, allow_abbrev=False)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("check", "install"):
        sub = commands.add_parser(name, allow_abbrev=False)
        sub.add_argument("--expect-version")
        sub.add_argument("--expect-core")
        if name == "install":
            sub.add_argument("--artifact", required=True)
            sub.add_argument("--core", required=True)
            sub.add_argument("--venv", required=True)
            sub.add_argument("--python", default="3.12")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _install(args) if args.command == "install" else _check(args)


if __name__ == "__main__":
    sys.exit(main())
