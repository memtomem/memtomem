"""Single source of truth for Kimi Code's on-disk home.

Kimi Code v0.1.0 moved the user-level data, MCP, and skill home from the
legacy ``~/.kimi`` (``$KIMI_SHARE_DIR``) layout to ``~/.kimi-code``,
overridable with ``$KIMI_CODE_HOME``. Two independent surfaces need that
answer and must never disagree:

* :mod:`memtomem.context.runtime_registry` — decides whether Kimi has a
  memtomem MCP registration (reads ``<home>/mcp.json``).
* :mod:`memtomem.context._runtime_targets` — decides where user-scope
  Context Gateway fan-out and import read/write skills and agents.

When those two drift, a relocated install reports "registered" while the
gateway pushes artifacts to a directory Kimi never reads, which is silent
and looks like a Kimi bug. Both call in here instead.

The legacy directory is retained for *discovery only*: it marks the client
as installed (so upgrades stay visible) and it is inventoried by
``mm uninstall``, but it is never an active registration and never a
fan-out target — modern Kimi Code does not read it.
"""

from __future__ import annotations

import os
from pathlib import Path

#: Directory name of the current Kimi Code home under ``$HOME``.
KIMI_CODE_DIRNAME = ".kimi-code"
#: Directory name of the pre-v0.1.0 Kimi home under ``$HOME``.
KIMI_LEGACY_DIRNAME = ".kimi"


def kimi_code_home(home: Path | None = None) -> Path:
    """Return Kimi Code's current data/MCP/skill home.

    ``$KIMI_CODE_HOME`` wins when set; otherwise ``<home>/.kimi-code``.
    ``home`` is injectable so the registry can probe a test HOME without
    relying on ``expanduser`` (which is a no-op for a monkeypatched
    ``$HOME`` on Windows).
    """
    override = os.environ.get("KIMI_CODE_HOME")
    if override:
        return Path(override).expanduser()
    base = home if home is not None else Path.home()
    return base / KIMI_CODE_DIRNAME


def kimi_legacy_home(home: Path | None = None) -> Path:
    """Return the legacy Kimi share directory (discovery/cleanup only).

    ``$KIMI_SHARE_DIR`` wins when set; otherwise ``<home>/.kimi``. Never
    use this as a write target — see the module docstring.
    """
    override = os.environ.get("KIMI_SHARE_DIR")
    if override:
        return Path(override).expanduser()
    base = home if home is not None else Path.home()
    return base / KIMI_LEGACY_DIRNAME
