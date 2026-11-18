"""Shared subprocess environment helpers for tests (#3062).

When a test spawns a child Python process (e.g. via ``subprocess.run``) to run
CLI commands or isolated interpreter probes, the child process must import the
exact ``memtomem`` package that the running pytest session imported, rather than
falling back to an ambient installation or another checkout's editable install.

Relative ``PYTHONPATH`` entries inherited from a caller also resolve against the
child's working directory rather than the repository root (#3052, #3058).

This module provides helpers to construct child environments with the imported
package's source directory prepended to ``PYTHONPATH``.
"""

from __future__ import annotations

import os
from collections.abc import Mapping, Sequence
from pathlib import Path

import memtomem


def package_src_dir() -> Path:
    """Return the source root of the ``memtomem`` package imported by pytest.

    Typically ``.../packages/memtomem/src``.
    """
    return Path(memtomem.__file__).resolve().parents[1]


def subprocess_env(
    base: Mapping[str, str] | None = None,
    *,
    extra_paths: Sequence[str | Path] = (),
    **overrides: str,
) -> dict[str, str]:
    """Return an environment mapping with ``package_src_dir()`` first on ``PYTHONPATH``.

    Parameters:
        base: The base environment dictionary (defaults to ``os.environ``).
        extra_paths: Optional additional paths to prepend after ``package_src_dir()``
            and before any inherited ``PYTHONPATH`` entries.
        **overrides: Additional environment variable overrides to apply.

    Any empty components in inherited ``PYTHONPATH`` are dropped to prevent
    unintended current-working-directory fallback.
    """
    env = dict(os.environ if base is None else base)
    src_dir = str(package_src_dir())

    parts = [src_dir, *(str(p) for p in extra_paths if str(p))]
    inherited = env.get("PYTHONPATH")
    if inherited:
        parts.extend(filter(None, inherited.split(os.pathsep)))

    env["PYTHONPATH"] = os.pathsep.join(parts)
    env.update(overrides)
    return env
