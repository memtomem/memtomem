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
    "LockfileCorruptError",
    "LockfileError",
    "LockfileVersionError",
    "digests_from_entry",
    "manifest_from_entry",
    "utcnow_iso8601_z",
]


LOCKFILE_NAME = "lock.json"
LOCKFILE_VERSION = 1


class LockfileError(RuntimeError):
    """Base for lockfile read failures raised by :meth:`Lockfile.load`.

    Catch this to handle :class:`LockfileVersionError` and
    :class:`LockfileCorruptError` in one clause — surfaces that degrade
    (``mm context status``) or message-and-exit (CLI) treat both the same.
    """


class LockfileVersionError(LockfileError):
    """The lockfile carries a ``version`` this build does not understand.

    Raised by :meth:`Lockfile.load` with ``strict=True`` (the default for
    write paths). Diagnostic surfaces (e.g. a future ``mm context status``)
    can pass ``strict=False`` to recover the raw dict for inspection.
    """


class LockfileCorruptError(LockfileError):
    """The lockfile exists but cannot be read as a JSON object.

    Raised by :meth:`Lockfile.load` with ``strict=True`` (the default, and
    what every write path uses) when the file is unreadable (``OSError``
    other than missing), not valid JSON, or its top level is not an object.
    Refusing here is load-bearing: ``upsert_entry`` loads inside the sidecar
    lock and writes the doc back, so a tolerant reset would be *persisted*
    with only the upserted entry, wiping every sibling asset's install
    record (#1247 id 16). Only ``strict=False`` diagnostic reads keep the
    tolerant empty-doc fallback.
    """


def utcnow_iso8601_z() -> str:
    """``YYYY-MM-DDTHH:MM:SS.ffffffZ``.

    Microsecond precision keeps concurrency tests deterministic — two
    writers that land in the same second still produce distinct
    ``installed_at`` values for ordering.
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def manifest_from_entry(entry: dict[str, Any]) -> frozenset[str] | None:
    """Return the entry's validated file manifest, or ``None``.

    The manifest (``files`` + ``files_commit``, written by install/update
    since #1247) is honored only when it provably describes the entry's
    current pin AND is well-formed:

    - ``files_commit`` is a ``str`` equal to ``entry["wiki_commit"]`` —
      ``upsert_entry`` preserves unknown keys, so an entry rewritten by an
      *older* tool keeps a stale ``files`` list while the pin moves; the
      commit pairing detects that.
    - ``files`` is a list of non-empty ``str`` POSIX relpaths — no leading
      ``/``, no ``..`` segment, no ``\\``. ``lock.json`` can be git-tracked
      and hand-merged, so malformed shapes are an ordinary event and must
      degrade to "no manifest", never crash or mis-answer membership.
    """
    files = entry.get("files")
    files_commit = entry.get("files_commit")
    wiki_commit = entry.get("wiki_commit")
    if not isinstance(files_commit, str) or not isinstance(wiki_commit, str):
        return None
    if files_commit != wiki_commit:
        return None
    if not isinstance(files, list):
        return None
    out: set[str] = set()
    for item in files:
        if not _is_valid_relpath(item):
            return None
        out.add(item)
    return frozenset(out)


def _is_valid_relpath(item: object) -> bool:
    """Shared relpath shape rule for ``files`` members and ``digests`` keys.

    Non-empty ``str`` POSIX relpath — no leading ``/``, no ``..`` segment,
    no ``\\``. ``lock.json`` can be git-tracked and hand-merged, so callers
    treat a violation as "degrade to no manifest/digests", never crash.
    """
    if not isinstance(item, str) or not item:
        return False
    if item.startswith("/") or "\\" in item:
        return False
    return ".." not in item.split("/")


_HEX_DIGITS = frozenset("0123456789abcdef")


def digests_from_entry(entry: dict[str, Any]) -> dict[str, str] | None:
    """Return the entry's validated per-file digest map, or ``None``.

    ``digests`` / ``digests_installed_at`` (#1247 id 15) record the SHA-256
    of **the bytes the writing operation put into dest** — content identity
    of the install, not commit provenance. The map is honored only when:

    - ``digests_installed_at`` is a ``str`` equal to the entry's
      ``installed_at``. Every entry-writing operation rewrites
      ``installed_at`` and :meth:`Lockfile.upsert_entry` stamps
      ``digests_installed_at`` from the same value, so the pairing proves
      "these digests were written by the same upsert that last (re)wrote
      this entry". A pre-digest tool preserves the unknown ``digests*``
      keys verbatim while moving ``installed_at`` → mismatch → degrade.
      Unlike the commit pairing of ``files_commit``, this also degrades an
      old tool moving the pin A→B→A: each old-tool write refreshes
      ``installed_at``. Residual limitation (documented, fail-safe): a
      pre-digest rewrite landing on the byte-identical µs ISO string would
      falsely re-validate the stale map — stale digests vs newer bytes
      still classify **dirty**; a silent clean additionally requires the
      current bytes to equal what was once installed. String equality only
      — no ISO parsing; a malformed ``installed_at`` already degrades the
      whole entry to ``never_installed`` before any digest consumer runs.
    - ``digests`` is a dict of relpath → digest where every key passes the
      manifest relpath shape rules (:func:`_is_valid_relpath`) and every
      value is a 64-char lowercase-hex string (algorithm fixed: SHA-256;
      an algorithm change is a new key name, not a value prefix).

    Any violation → ``None`` (degrade to the legacy mtime behavior), never
    crash or mis-answer — lock.json is git-tracked and hand-merged, so
    malformed shapes are an ordinary event.
    """
    digests = entry.get("digests")
    digests_installed_at = entry.get("digests_installed_at")
    installed_at = entry.get("installed_at")
    if not isinstance(digests_installed_at, str) or not isinstance(installed_at, str):
        return None
    if digests_installed_at != installed_at:
        return None
    if not isinstance(digests, dict):
        return None
    out: dict[str, str] = {}
    for rel, digest in digests.items():
        if not _is_valid_relpath(rel):
            return None
        if not isinstance(digest, str) or len(digest) != 64 or not set(digest) <= _HEX_DIGITS:
            return None
        out[rel] = digest
    return out


def installed_at_epoch_from_entry(entry: dict[str, Any]) -> float | None:
    """Parse the entry's ``installed_at`` ISO-8601 string to an epoch, or ``None``.

    Tolerant on purpose: consumers use the epoch as an mtime guard and must
    degrade to their fail-safe branch — "keep, never delete" in install's
    reconcile, ``never_installed`` in the dirty probe — when the timestamp is
    missing, non-string, or malformed. ``lock.json`` can be git-tracked and
    hand-merged, so malformed shapes are an ordinary event.

    ``migrate._is_flat_file_dirty`` deliberately does NOT use this helper: there
    a malformed timestamp must crash, because degrading to "clean" would approve
    overwriting user edits (#1247 id 1) — its callers pre-validate with
    ``_installed_at_parseable`` instead.
    """
    raw = entry.get("installed_at")
    if not isinstance(raw, str):
        return None
    try:
        return datetime.fromisoformat(raw).timestamp()
    except ValueError:
        return None


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

        - Missing file → ``{"version": LOCKFILE_VERSION}`` (write-safe default,
          both modes — an absent lockfile is the normal pre-install state).
        - Unreadable / invalid JSON / non-object top level and ``strict=True``
          → raise :class:`LockfileCorruptError`. Write paths load-then-write,
          so a tolerant reset here would be persisted, destroying every
          sibling entry (#1247 id 16).
        - Same corrupt cases with ``strict=False`` → log warning, return
          ``{"version": LOCKFILE_VERSION}`` (diagnostic surfaces degrade).
        - ``version`` ≠ ``LOCKFILE_VERSION`` and ``strict=True`` → raise
          :class:`LockfileVersionError` (canonical record; silent reset
          would clobber a forward-compatible lockfile written by a newer
          tool).
        - ``version`` ≠ ``LOCKFILE_VERSION`` and ``strict=False`` → return
          the raw dict so diagnostic surfaces can render a useful message.
        """
        hint = "fix or remove it (e.g. restore it from version control), then retry"
        try:
            raw = self._path.read_bytes()
        except FileNotFoundError:
            return {"version": LOCKFILE_VERSION}
        except OSError as exc:
            if strict:
                raise LockfileCorruptError(
                    f"lockfile at {self._path} is unreadable ({exc}); {hint}"
                ) from exc
            logger.warning("lockfile: read failed at %s: %s", self._path, exc)
            return {"version": LOCKFILE_VERSION}

        try:
            doc = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            # UnicodeDecodeError: json.loads(bytes) decodes before parsing,
            # so invalid UTF-8 raises it instead of JSONDecodeError — same
            # corrupt-file class, same handling (Codex design review).
            if strict:
                raise LockfileCorruptError(
                    f"lockfile at {self._path} is not valid JSON ({exc}); {hint}"
                ) from exc
            logger.warning("lockfile: invalid JSON at %s, ignoring file: %s", self._path, exc)
            return {"version": LOCKFILE_VERSION}

        if not isinstance(doc, dict):
            if strict:
                raise LockfileCorruptError(
                    f"lockfile at {self._path} top level is not a JSON object; {hint}"
                )
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
        files: list[str] | None = None,
        files_commit: str | None = None,
        digests: dict[str, str] | None = None,
        lock_timeout: float | None = None,
    ) -> None:
        """Insert or replace the ``(asset_type, name)`` entry.

        Holds the sidecar lock for the load + mutate + write triple.
        ``lock_timeout`` (seconds) bounds the sidecar-lock acquisition: ``None``
        (default) blocks indefinitely, matching the narrow CLI write window;
        an async web handler offloading this to a worker thread MUST pass a
        bound below its own request timeout so the thread self-aborts in-window
        rather than orphaning a late write after the handler returned (#1145,
        the ``_file_lock`` docstring). On expiry ``_file_lock`` raises
        ``TimeoutError`` having written nothing.
        Preserves all unknown sibling and per-entry keys verbatim — only
        the mandated fields are written, anything else under
        ``doc[asset_type][name]`` survives.

        ``files`` / ``files_commit`` (#1247): the installed file manifest,
        stored sorted. Both must be passed together. Omitting them leaves
        any previously recorded manifest untouched (same unknown-key
        preservation contract as the rest of the entry) — consumers detect
        the resulting staleness via the ``files_commit`` pairing, see
        :func:`manifest_from_entry`.

        ``digests`` (#1247 id 15): per-file SHA-256 of the bytes the
        operation wrote into dest, stored with sorted keys. When passed,
        ``digests_installed_at`` is stamped from the same value written to
        ``installed_at`` — there is no second kwarg, so the freshness
        pairing cannot be mis-assembled by a caller. When **omitted**, any
        existing ``digests`` / ``digests_installed_at`` keys are DELETED,
        not preserved: this method owns those keys, and a digest-less write
        that kept them would leave a stale-but-potentially-re-matching pair
        behind (``installed_at`` is mtime-derived, not a nonce). The
        unknown-field round-trip contract is untouched — it binds tools the
        keys are *unknown to*; pre-digest clients preserve them verbatim,
        which is exactly what the :func:`digests_from_entry` pairing
        degrade catches.
        """
        if (files is None) != (files_commit is None):
            raise ValueError("files and files_commit must be passed together")
        with _file_lock(_lock_path_for(self._path), timeout=lock_timeout):
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
            if files is not None:
                merged["files"] = sorted(files)
                merged["files_commit"] = files_commit
            if digests is not None:
                merged["digests"] = {rel: digests[rel] for rel in sorted(digests)}
                merged["digests_installed_at"] = installed_at
            else:
                merged.pop("digests", None)
                merged.pop("digests_installed_at", None)
            section[name] = merged

            atomic_write_bytes(
                self._path,
                json.dumps(doc, indent=2, ensure_ascii=False).encode("utf-8"),
            )

    def remove_entry(
        self, asset_type: str, name: str, *, lock_timeout: float | None = None
    ) -> bool:
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

        ``lock_timeout`` bounds the sidecar-lock wait (``None`` blocks — the
        historical default), matching :meth:`upsert_entry`; a caller inside a
        bounded budget (transfer's canonical-lock span, ADR-0030 §6) forwards
        its remaining deadline so a stuck holder can't outlive the request.
        """
        with _file_lock(_lock_path_for(self._path), timeout=lock_timeout):
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
