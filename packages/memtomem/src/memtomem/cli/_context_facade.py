"""Late-bound handle on ``memtomem.cli.context_cmd`` for the modules split out of it.

``context_cmd`` imports the ``context_*_cmd`` modules at its end, to register
their commands and re-export their names. Those modules therefore cannot import
``context_cmd`` at their own top: imported first, one would hand ``context_cmd``
a half-initialised module to import from. Resolving each attribute when it is
used keeps every ``_ctx.<name>`` call reading ``context_cmd`` at call time,
which is also where tests monkeypatch it.
"""

from __future__ import annotations

import importlib
from typing import Any


class _LateModule:
    def __init__(self, name: str) -> None:
        self._name = name

    def __getattr__(self, attr: str) -> Any:
        return getattr(importlib.import_module(self._name), attr)


context_cmd: Any = _LateModule("memtomem.cli.context_cmd")
