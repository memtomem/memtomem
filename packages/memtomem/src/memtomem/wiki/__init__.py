"""memtomem wiki — shared canonical artifact store at ``~/.memtomem-wiki/``.

The wiki is a normal git repository holding skills, agents, and commands
in vendor-neutral form. ``mm context install`` snapshots wiki artifacts
into ``<project>/.memtomem/``; ``mm wiki ...`` edits the wiki directly.

See :file:`docs/adr/0008-wiki-layer.md` for invariants.
"""

from __future__ import annotations

from memtomem.wiki.store import (
    DEFAULT_WIKI_PATH,
    WIKI_ASSET_TYPES,
    WikiAlreadyExistsError,
    WikiAsset,
    WikiNotFoundError,
    WikiStore,
)

__all__ = [
    "DEFAULT_WIKI_PATH",
    "WIKI_ASSET_TYPES",
    "WikiAlreadyExistsError",
    "WikiAsset",
    "WikiNotFoundError",
    "WikiStore",
]
