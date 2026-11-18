"""The ``MEMTOMEM_*`` subset of the nearest ``.env``, importable without the server.

Lives outside :mod:`memtomem.server` because importing any module of that
package registers every MCP tool; a process that only has to resolve its
store (the stdio bridge of #3381) must be able to load the same file the
server loads without paying for that.
"""

from __future__ import annotations

import os

# python-dotenv's own truthy spellings for ``PYTHON_DOTENV_DISABLED``
# (``dotenv.main._load_dotenv_disabled``), copied because that helper is private.
DOTENV_DISABLED_VALUES = frozenset({"1", "true", "t", "yes", "y"})


def load_dotenv() -> list[str]:
    """Apply the ``MEMTOMEM_*`` keys of the nearest ``.env``; return the names left out.

    Only the prefixed keys reach ``os.environ`` (#2824): the file found is
    usually ``~/.env``, and its other keys — API keys for unrelated tools —
    would otherwise enter this process and every subprocess it starts,
    whatever environment filtering the MCP client applied.

    Everything else matches ``load_dotenv()``. ``find_dotenv()`` is called
    with no argument, from here: the search starts at this file's directory,
    the installed ``memtomem`` package, rather than the CWD — except where
    python-dotenv itself falls back to the CWD (an interactive ``__main__``,
    a trace function, a frozen interpreter). Values are resolved by
    python-dotenv's own ``DotEnv`` with ``load_dotenv()``'s arguments spelled
    out: ``override=False`` keeps an exported value ahead of the file inside
    ``${VAR}`` (``dotenv_values()`` would invert that), and the class's own
    defaults are a locale encoding and ``override=True``. Resolving writes
    nothing, so an unrelated key never enters the environment, even briefly.
    The prefix match is case-insensitive like the settings binding, an
    exported value wins, and a key with no value is not set.
    """
    from dotenv import find_dotenv
    from dotenv.main import DotEnv

    if os.environ.get("PYTHON_DOTENV_DISABLED", "").casefold() in DOTENV_DISABLED_VALUES:
        return []
    path = find_dotenv()
    if not path:
        return []
    values = DotEnv(path, encoding="utf-8", interpolate=True, override=False).dict()
    left_out: list[str] = []
    for key, value in values.items():
        if not key.upper().startswith("MEMTOMEM_"):
            left_out.append(key)
            continue
        if value is None or key in os.environ:
            continue
        try:
            os.environ[key] = value
        except ValueError:
            # A quoted key containing ``=`` is not a legal variable name.
            # ``load_dotenv()`` raised here, out of server startup.
            left_out.append(key)
    return left_out
