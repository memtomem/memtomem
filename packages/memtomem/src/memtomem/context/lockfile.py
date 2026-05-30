"""Project-level wiki install lockfile (``<project>/.memtomem/lock.json``).

Records which wiki commit each installed asset was snapshotted from, so a
later ``mm context update`` (PR-D) can detect drift between the on-disk
canonical tree and the wiki source. Schema and invariants are pinned in
``docs/adr/0008-wiki-layer.md`` (sections "Lockfile schema" and "PR
breakdown").

The store is dict-based on purpose: ADR-0008 mandates that reads MUST
preserve unknown top-level and per-entry fields so future schema additions
(``compat``, ``mode``, ``skill_version``) round-trip through older client
versions unchanged. A strict dataclass would silently strip those keys.

Concurrency uses the sidecar-lockfile pattern from
:mod:`memtomem.context._atomic` (``_file_lock`` + ``_lock_path_for``),
shared with ``KnownProjectsStore``. The lock window is intentionally narrow
— only the ``load → mutate dict → atomic_write_bytes`` triple — so the slow
``copy_tree_atomic`` step in :func:`memtomem.context.install.install_skill`
runs unlocked.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_bytes

logger = logging.getLogger(__name__)

__all__ = [
    "LOCKFILE_NAME",
    "LOCKFILE_VERSION",
    "Lockfile",
    "LockfileVersionError",
    "utcnow_iso8601_z",
]


LOCKFILE_NAME = "lock.json"
LOCKFILE_VERSION = 1


class LockfileVersionError(RuntimeError):
    """The lockfile carries a ``version`` this build does not understand.

    Raised by :meth:`Lockfile.load` with ``strict=True`` (the default for
    write paths). Diagnostic surfaces (e.g. a future ``mm context status``)
    can pass ``strict=False`` to recover the raw dict for inspection.
    """


def utcnow_iso8601_z() -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Microsecond precision keeps concurrency tests deterministic — two
    writers that land in the same second still produce distinct
    ``installed_at`` values for ordering.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


class Lockfile:
    """Read / mutate ``<project>/.memtomem/lock.json``.

    Mutations hold an exclusive sidecar lock and write atomically via
    ``atomic_write_bytes``. Two writers on different ``(asset_type, name)``
    keys both survive (no key collision). Two writers on the same key are
    last-write-wins on the entry.
    """

    def __init__(self, path: Path | str) -> None:
        self._path = Path(path).expanduser()

    @classmethod
    def at(cls, project_root: Path | str) -> Lockfile:
        """Return a :class:`Lockfile` rooted at ``<project_root>/.memtomem/lock.json``."""
        return cls(Path(project_root).expanduser() / ".memtomem" / LOCKFILE_NAME)

    @property
    def path(self) -> Path:
        return self._path

    def load(self, *, strict: bool = True) -> dict[str, Any]:
        """Return the lockfile dict.

        - Missing file → ``{"version": LOCKFILE_VERSION}`` (write-safe default).
        - Invalid JSON → log warning, return ``{"version": LOCKFILE_VERSION}``.
        - ``version`` ≠ ``LOCKFILE_VERSION`` and ``strict=True`` → raise
          :class:`LockfileVersionError` (canonical record; silent reset
          would clobber a forward-compatible lockfile written by a newer
          tool).
        - ``version`` ≠ ``LOCKFILE_VERSION`` and ``strict=False`` → return
          the raw dict so diagnostic surfaces can render a useful message.
        """
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {"version": LOCKFILE_VERSION}
        except OSError as exc:
            logger.warning("lockfile: read failed at %s: %s", self._path, exc)
            return {"version": LOCKFILE_VERSION}

        try:
            doc = json.loads(raw)
        except json.JSONDecodeError as exc:
            logger.warning("lockfile: invalid JSON at %s, ignoring file: %s", self._path, exc)
            return {"version": LOCKFILE_VERSION}

        if not isinstance(doc, dict):
            logger.warning("lockfile: top-level not an object at %s, ignoring", self._path)
            return {"version": LOCKFILE_VERSION}

        version = doc.get("version")
        if version != LOCKFILE_VERSION:
            if strict:
                raise LockfileVersionError(
                    f"lockfile at {self._path} has version {version!r}; "
                    f"this build supports version {LOCKFILE_VERSION}"
                )
            return doc

        return doc

    def read_entry(self, asset_type: str, name: str) -> dict[str, Any] | None:
        """Return the entry under ``doc[asset_type][name]`` or ``None``."""
        doc = self.load()
        section = doc.get(asset_type)
        if not isinstance(section, dict):
            return None
        entry = section.get(name)
        if not isinstance(entry, dict):
            return None
        return entry

    def iter_entries(self) -> Iterator[tuple[str, str, dict[str, Any]]]:
        """Yield ``(asset_type, name, entry)`` triples in deterministic order.

        Ordering contract: alphabetical by ``(asset_type, name)``. With the
        current asset matrix this means ``agents`` → ``commands`` →
        ``skills``, and within each section the names sort alphabetically.

        The iteration is deliberately schema-flexible: any top-level key
        whose value is a ``dict[str, dict[str, Any]]`` (asset section
        shape) is yielded — a future asset_type works without code
        changes here. Top-level scalars like ``version``, and unknown
        per-entry shapes, are skipped silently so this remains
        round-trip-safe per ADR-0008.

        Caller surfaces that want a different display order (e.g. ``mm
        context status`` may prefer a functional order with skills
        first) should re-sort the output. This method's contract is
        *deterministic*, not *display-optimal*.
        """
        doc = self.load()
        for asset_type in sorted(doc):
            section = doc.get(asset_type)
            if not isinstance(section, dict):
                continue
            for name in sorted(section):
                entry = section[name]
                if not isinstance(entry, dict):
                    continue
                yield asset_type, name, entry

    def upsert_entry(
        self,
        asset_type: str,
        name: str,
        *,
        wiki_commit: str,
        installed_at: str,
    ) -> None:
        """Insert or replace the ``(asset_type, name)`` entry.

        Holds the sidecar lock for the load + mutate + write triple.
        Preserves all unknown sibling and per-entry keys verbatim — only
        the two mandated fields are written, anything else under
        ``doc[asset_type][name]`` survives.
        """
        with _file_lock(_lock_path_for(self._path)):
            doc = self.load()
            section = doc.get(asset_type)
            if not isinstance(section, dict):
                section = {}
                doc[asset_type] = section

            existing = section.get(name)
            if isinstance(existing, dict):
                merged = dict(existing)
            else:
                merged = {}
            merged["wiki_commit"] = wiki_commit
            merged["installed_at"] = installed_at
            section[name] = merged

            atomic_write_bytes(
                self._path,
                json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"),
            )

    def remove_entry(self, asset_type: str, name: str) -> bool:
        """Delete the ``(asset_type, name)`` entry if present.

        Returns ``True`` when an entry was removed, ``False`` when there
        was nothing to remove (no such section, or no such name) — in
        which case the file is left untouched: no atomic write happens, so
        ``mtime`` is unchanged and a concurrent reader sees no spurious
        churn.

        Holds the sidecar lock for the load → mutate → write triple,
        mirroring :meth:`upsert_entry`. Only the targeted entry is
        deleted; sibling entries and unknown top-level / per-entry fields
        round-trip verbatim per ADR-0008. The (possibly now-empty) section
        dict is left in place rather than pruned, so a section a newer
        tool populated out-of-band is never dropped as a side effect of
        removing one entry.
        """
        with _file_lock(_lock_path_for(self._path)):
            doc = self.load()
            section = doc.get(asset_type)
            if not isinstance(section, dict) or name not in section:
                return False
            del section[name]
            atomic_write_bytes(
                self._path,
                json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"),
            )
            return True
