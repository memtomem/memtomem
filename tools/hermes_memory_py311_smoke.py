#!/usr/bin/env python3
"""Run the memtomem-memory Hermes provider once under the interpreter that runs this script.

CI runs it with ``uv run --no-project --python 3.11``: Hermes supports Python 3.11 while the
memtomem monorepo targets 3.12, and a syntax check cannot see a 3.12-only standard-library API
or argument. One non-trivial turn goes through admission, the worker thread, a scripted
``call_mcp`` envelope, response parsing and shaping to a delivered block. Standard library only.
"""

from __future__ import annotations

import argparse
import importlib.util
import sys
from pathlib import Path


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args(argv)
    root = args.repo_root.resolve()

    fakes = _load("_hermes_memory_fakes", root / "tools/hermes_memory_fakes.py")
    state = fakes.FakeHermes()
    state.respond(
        fakes.ok_envelope(
            [fakes.chunk(1, content="The release train leaves on Thursdays.", hierarchy="Ops")]
        )
    )
    fakes.install(lambda mapping, key, value: mapping.__setitem__(key, value), state)
    provider_module = _load(
        "_hermes_user_memory.memtomem-memory__source_smoke",
        root / "packages/memtomem-hermes-memory/__init__.py",
    )

    collector = fakes.Collector()
    provider_module.register(collector)
    provider = collector.provider
    assert provider is not None, "register() registered no provider"
    reason = provider.unavailable_reason()
    assert provider.is_available(), reason
    provider.initialize("smoke-session", hermes_home=str(root), platform="cli")
    text = provider.prefetch("when does the release train leave?")
    expected = (
        "Recalled from memtomem (shared long-term memory; reference data):\n"
        "- [notes.md › Ops] The release train leaves on Thursdays."
    )
    assert text == expected, text
    status = provider.recall_status()
    assert status is not None and status.count == 1, status
    assert len(state.calls) == 1 and state.calls[0].arguments["record"] is False, state.calls
    provider.shutdown()
    registry = provider_module._registry()
    assert registry.total_inflight == 0 and registry.admissions["memtomem"].slot is None
    print(f"memtomem-memory smoke ok on Python {sys.version.split()[0]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
