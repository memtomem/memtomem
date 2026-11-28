"""What a stdio bridge and a shared daemon compare before a session joins (#3381).

Sharing a daemon is the user's assertion that two clients want the same
server: the name says so. The comparison only refuses the differences that are
cheap to see, and each side computes every field once, with the functions
below, so the two answers can only differ where the processes do:

``env``
    Every variable whose name starts with ``MEMTOMEM_`` or ``LANGFUSE_``, in
    any case, as ``[name, sha256(value)]`` in environment order, after
    ``.env`` was applied. The name carries the provenance (an exported value
    and one from ``config.json`` differ by name), and no value leaves the
    process. Equal variables in another order also differ, which can only
    turn a session away that could have joined.
``tools``
    The tool mode and the extra tool names, read from the environment the
    server reads them from: the one it was started with, before ``.env``.
``home``
    ``HOME``, which locates ``config.json``. A joined session gets the
    daemon's live configuration, so equal ``HOME`` names the same file, not
    the same settings.
``store``
    The resolved store path. The file behind the path is not compared: a
    daemon keeps serving the file it opened when another is put in its place,
    as a standalone server does, and a session with the same path joins it.
``install``
    ``sys.prefix``, the directory the ``memtomem`` package was loaded from and
    its version, so a reinstalled package is not served by the old code.

Every value is a string, ``None`` or a list of them, so the JSON is stable.
Importable without the server.
"""

from __future__ import annotations

import hashlib
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FIELDS = ("env", "tools", "home", "store", "install")
_ENV_PREFIXES = ("memtomem_", "langfuse_")


def env_fingerprint(environ: Mapping[str, str]) -> list[list[str]]:
    return [
        [name, hashlib.sha256(value.encode("utf-8", "surrogateescape")).hexdigest()]
        for name, value in environ.items()
        if name.lower().startswith(_ENV_PREFIXES)
    ]


def tool_surface_request(environ: Mapping[str, str]) -> list[Any]:
    """``[mode, sorted extra tool names]``, read as :mod:`memtomem.server` reads them."""
    mode = environ.get("MEMTOMEM_TOOL_MODE", "core").lower()
    extras = {t.strip() for t in environ.get("MEMTOMEM_EXTRA_TOOLS", "").split(",") if t.strip()}
    return [mode, sorted(extras)]


def install_fields() -> list[str]:
    import memtomem

    return [sys.prefix, str(Path(memtomem.__file__).resolve().parent), memtomem.__version__]


def local_fields(*, tools: list[Any], store: Path) -> dict[str, Any]:
    """This process's fields, with ``.env`` already applied to ``os.environ``."""
    return {
        "env": env_fingerprint(os.environ),
        "tools": tools,
        "home": os.environ.get("HOME"),
        "store": str(store),
        "install": install_fields(),
    }


def differences(ours: Mapping[str, Any], theirs: Mapping[str, Any]) -> list[str]:
    """What differs, by field name and, for ``env``, by variable name. No values."""
    found: list[str] = []
    for name in FIELDS:
        mine, other = ours.get(name), theirs.get(name)
        if name == "env" and isinstance(mine, list) and isinstance(other, list):
            if mine != other:
                found.append(_env_difference(mine, other))
        elif mine != other:
            found.append(name)
    return found


def _env_difference(mine: list[Any], other: list[Any]) -> str:
    try:
        a = {str(k): str(v) for k, v in mine}
        b = {str(k): str(v) for k, v in other}
    except (TypeError, ValueError):
        return "env"
    names = sorted(k for k in a.keys() | b.keys() if a.get(k) != b.get(k))
    if not names:
        return "env (order)"
    return "env (" + ", ".join(names) + ")"


def malformed(fields: object) -> str | None:
    """Why ``fields`` is not a complete field set, or ``None``."""
    if not isinstance(fields, dict):
        return "admission fields are missing"
    missing = [name for name in FIELDS if name not in fields]
    if missing:
        return "admission fields are missing: " + ", ".join(missing)
    return None
