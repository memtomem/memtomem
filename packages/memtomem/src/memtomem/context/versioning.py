"""Per-artifact version snapshots + label pointers (ADR-0022).

Langfuse-style versioning for canonical context artifacts. A *version* is an
immutable snapshot of one artifact's working canonical file; a *label* is a
movable pointer (``production`` → ``v2``) over those versions. This lets
editing a canonical and *deploying* it to runtimes become two separate acts
with instant rollback (move the pointer).

This module is **pure filesystem** — it has no awareness of the sync engine,
CLI, MCP, or web. It owns one artifact's version store:

::

    .memtomem/agents/<name>/
    ├── agent.md            ← working canonical (label "latest"; NOT touched here)
    ├── versions/
    │   ├── v1.md           ← immutable snapshot (write-once)
    │   └── v2.md
    └── versions.json       ← {"versions": {...}, "labels": {...}} — only mutable state

The unit that owns a store is ``(scope, type, name)`` (ADR-0022 Decision (b)):
the directory passed as ``artifact_dir`` is already scope-specific because the
caller resolves it from the scoped canonical root. There is no global or
cross-tier label lookup.

Invariants (ADR-0022):

- ``latest`` is reserved and NOT handled here — the caller branches on it and
  reads the working file directly (it knows the real ``agent.md`` /
  ``command.md`` path).
- Version ``.md`` files are write-once; ``create_version`` refuses to
  overwrite an existing ``vN.md``.
- Tags match ``^v[1-9]\\d*$`` (``v0`` is invalid). Validated on create / load /
  resolve / promote so a hand-edited ``versions.json`` cannot point a label at
  a path-like tag (traversal guard).
- ``create_version`` / ``promote_label`` / ``delete_label`` each hold a single
  non-reentrant ``_file_lock`` on the ``versions.json`` sidecar across their
  entire ``load → mutate → write`` transaction (the ``lockfile.py`` pattern),
  so two racing ``create_version`` calls cannot both allocate the same tag.
- Versions snapshot the base canonical only; per-vendor overrides stay live.

Directory layout is required: a flat-layout artifact (``agents/<name>.md``) has
no per-artifact directory, so it cannot carry a version store —
``create_version`` raises :class:`VersionsDirMissingError`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from memtomem.context._atomic import _file_lock, _lock_path_for, atomic_write_bytes
from memtomem.context._names import Layout

__all__ = [
    "RESERVED_LABELS",
    "VersionRecord",
    "VersionsManifest",
    "VersionError",
    "VersionNotFoundError",
    "LabelNotFoundError",
    "ReservedLabelError",
    "InvalidLabelError",
    "InvalidTagError",
    "VersionsDirMissingError",
    "versions_dir",
    "versions_json_path",
    "load_manifest",
    "next_version_tag",
    "create_version",
    "promote_label",
    "delete_label",
    "resolve_label",
    "resolve_version",
    "make_label_resolver",
]

# Tag grammar: ``v`` + a positive integer starting at 1. ``v0`` is invalid
# (ADR-0022 invariant 5). Anchored so a manifest cannot smuggle a path-like
# tag (``v1/../../etc``) past validation.
_VALID_TAG_RE: re.Pattern[str] = re.compile(r"^v[1-9]\d*$")

#: Label names that are reserved and never stored in ``versions.json``.
#: ``latest`` always means the working file and is resolved by the caller,
#: never by :func:`resolve_label`.
RESERVED_LABELS: frozenset[str] = frozenset({"latest"})

_VERSIONS_DIRNAME = "versions"
_MANIFEST_FILENAME = "versions.json"


class VersionError(ValueError):
    """Base class for all versioning errors (a ``ValueError`` subclass so the
    CLI/MCP boundary can catch the family and translate to ``ClickException`` /
    a tool error)."""


class VersionNotFoundError(VersionError):
    """A version tag is absent from the manifest or its ``vN.md`` is missing."""


class LabelNotFoundError(VersionError):
    """A label name is absent from the manifest's label map."""


class ReservedLabelError(VersionError):
    """A reserved label (``latest``) was used as a writable label target."""


class InvalidLabelError(VersionError):
    """A label name is not allowed — e.g. it looks like a version tag
    (``^v[1-9]\\d*$``), which the sync resolver always treats as a direct
    version, so the label pointer could never be honored."""


class InvalidTagError(VersionError):
    """A tag string does not match ``^v[1-9]\\d*$``."""


class VersionsDirMissingError(VersionError):
    """Versioning was attempted on an artifact with no per-artifact directory
    (flat layout). Run ``mm context migrate`` first."""


@dataclass
class VersionRecord:
    """Metadata for one immutable version snapshot."""

    tag: str  # "v1", "v2", … (validated against _VALID_TAG_RE)
    created_at: str  # ISO-8601 UTC, e.g. "2026-06-03T09:00:00Z"
    note: str = ""


@dataclass
class VersionsManifest:
    """In-memory view of ``versions.json``. Mutated by callers under lock, then
    written back via :func:`_save_manifest`."""

    versions: dict[str, VersionRecord] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # label_name → tag


def versions_dir(artifact_dir: Path) -> Path:
    """Return the ``versions/`` subdirectory under *artifact_dir*."""
    return artifact_dir / _VERSIONS_DIRNAME


def versions_json_path(artifact_dir: Path) -> Path:
    """Return the ``versions.json`` sidecar path under *artifact_dir*."""
    return artifact_dir / _MANIFEST_FILENAME


def _validate_tag(tag: str) -> str:
    if not _VALID_TAG_RE.fullmatch(tag):
        raise InvalidTagError(f"invalid version tag {tag!r} (expected ^v[1-9]\\d*$)")
    return tag


def _validate_label_name(label: str) -> str:
    """Reject label names that cannot be honored by the sync resolver.

    ``--label`` shares one namespace with version tags: a ``^v[1-9]\\d*$`` value
    always resolves as a direct version (``make_label_resolver``), so a label
    *named* ``v1`` would be permanently shadowed by version ``v1``. Reject such
    names (and the reserved ``latest``) at write time so they can never be
    created, instead of storing an unreachable, misleading pointer.
    """
    if label in RESERVED_LABELS:
        raise ReservedLabelError(f"{label!r} is a reserved label name")
    if _VALID_TAG_RE.fullmatch(label):
        raise InvalidLabelError(
            f"label name {label!r} looks like a version tag — these are reserved for "
            f"direct version addressing (`--label {label}` already deploys that version)"
        )
    return label


def _now_iso() -> str:
    # Whole-second UTC with a trailing ``Z`` — matches the ADR's example shape
    # and avoids microsecond noise in the manifest.
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_manifest(artifact_dir: Path) -> VersionsManifest:
    """Read ``versions.json`` for *artifact_dir*.

    READ-ONLY and UNSYNCHRONIZED (no lock held), mirroring
    ``lockfile.Lockfile.load()`` — only the mutating helpers take the lock. A
    missing file returns an empty manifest (no error). Every tag found (both
    in ``versions`` and as a label target) is validated against
    ``_VALID_TAG_RE``; a malformed manifest raises :class:`InvalidTagError`.
    """
    path = versions_json_path(artifact_dir)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return VersionsManifest()
    except (OSError, json.JSONDecodeError) as exc:
        raise VersionError(f"unreadable versions manifest at {path}: {exc}") from exc

    # A hand-edited manifest may be the wrong JSON shape (e.g. ``[]`` or a
    # string). Validate every container is a mapping before iterating so a
    # malformed file surfaces a clean VersionError, not an AttributeError.
    if not isinstance(raw, dict):
        raise VersionError(f"malformed versions manifest at {path}: expected an object")
    # ``None`` / absent → empty; any other non-dict shape (e.g. ``[]``) is
    # malformed and must error rather than be coerced to empty (so a wrong-type
    # ``"versions": []`` surfaces a clean VersionError, not a silent drop).
    raw_versions = raw.get("versions") if raw.get("versions") is not None else {}
    raw_labels = raw.get("labels") if raw.get("labels") is not None else {}
    if not isinstance(raw_versions, dict) or not isinstance(raw_labels, dict):
        raise VersionError(
            f"malformed versions manifest at {path}: 'versions' and 'labels' must be objects"
        )

    versions: dict[str, VersionRecord] = {}
    for tag, meta in raw_versions.items():
        _validate_tag(tag)
        meta = meta if isinstance(meta, dict) else {}
        versions[tag] = VersionRecord(
            tag=tag,
            created_at=str(meta.get("created_at", "")),
            note=str(meta.get("note", "")),
        )

    labels: dict[str, str] = {}
    for label, tag in raw_labels.items():
        # Refuse to load a label the write APIs would never create — a reserved
        # ``latest`` or a version-shaped name (``v1``) that the sync resolver
        # would permanently shadow with the same-named version. Fail loud on a
        # tampered manifest rather than surface an impossible/unreachable state.
        try:
            _validate_label_name(str(label))
        except VersionError as exc:
            raise type(exc)(f"malformed versions manifest at {path}: {exc}") from exc
        _validate_tag(str(tag))
        labels[str(label)] = str(tag)

    return VersionsManifest(versions=versions, labels=labels)


def _save_manifest(artifact_dir: Path, manifest: VersionsManifest) -> None:
    """Atomically write *manifest* to ``versions.json``.

    PRIVATE. The caller MUST already hold ``_file_lock`` on the sidecar — there
    is no public single-call path, because ``_file_lock`` is non-reentrant and
    every mutation runs inside the larger ``create_version`` / ``promote_label``
    / ``delete_label`` transaction.
    """
    payload = {
        "versions": {
            tag: {"created_at": rec.created_at, "note": rec.note}
            for tag, rec in sorted(manifest.versions.items(), key=lambda kv: _tag_num(kv[0]))
        },
        "labels": {label: manifest.labels[label] for label in sorted(manifest.labels)},
    }
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(versions_json_path(artifact_dir), data)


def _tag_num(tag: str) -> int:
    """Numeric suffix of a validated tag (``"v3"`` → ``3``)."""
    return int(tag[1:])


def next_version_tag(manifest: VersionsManifest) -> str:
    """Return ``"v1"`` if no versions exist, else ``"v<max+1>"``. Pure (no I/O)."""
    if not manifest.versions:
        return "v1"
    return f"v{max(_tag_num(t) for t in manifest.versions) + 1}"


def create_version(artifact_dir: Path, working_file: Path, note: str = "") -> VersionRecord:
    """Snapshot *working_file* into ``versions/<tag>.md`` and record it.

    Holds a single ``_file_lock`` on the ``versions.json`` sidecar across the
    whole transaction (``load → allocate tag → write vN.md → save manifest``),
    so two concurrent callers cannot both allocate the same tag. The version
    file is write-once: if ``versions/<tag>.md`` already exists the call raises
    :class:`InvalidTagError` rather than overwriting.

    Raises :class:`VersionsDirMissingError` if *artifact_dir* does not exist
    (flat-layout artifact has no per-artifact directory).
    """
    if not artifact_dir.is_dir():
        raise VersionsDirMissingError(
            f"{artifact_dir} is not a directory — versioning requires directory layout "
            f"(run `mm context migrate` first)"
        )
    try:
        source_bytes = working_file.read_bytes()
    except OSError as exc:
        raise VersionError(f"cannot read working canonical {working_file}: {exc}") from exc

    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock):
        manifest = load_manifest(artifact_dir)
        tag = next_version_tag(manifest)
        vfile = versions_dir(artifact_dir) / f"{tag}.md"
        # Write-once. next_version_tag() is monotonic over the manifest, but a
        # stray on-disk vN.md not in the manifest would otherwise be silently
        # clobbered by atomic_write_bytes' os.replace — refuse loudly instead.
        if vfile.exists():
            raise InvalidTagError(f"version file already exists: {vfile}")
        atomic_write_bytes(vfile, source_bytes)
        record = VersionRecord(tag=tag, created_at=_now_iso(), note=note)
        manifest.versions[tag] = record
        _save_manifest(artifact_dir, manifest)
    return record


def promote_label(artifact_dir: Path, label: str, version: str) -> None:
    """Point *label* at *version* (create-or-move). Rollout == rollback.

    Raises :class:`ReservedLabelError` for ``latest``, :class:`InvalidLabelError`
    for a version-shaped label name, :class:`InvalidTagError` for a malformed
    tag, and :class:`VersionNotFoundError` if the tag is not in the manifest.
    Holds ``_file_lock`` across ``load → validate → mutate → save``.
    """
    _validate_label_name(label)
    _validate_tag(version)
    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock):
        manifest = load_manifest(artifact_dir)
        if version not in manifest.versions:
            raise VersionNotFoundError(f"version {version!r} does not exist")
        manifest.labels[label] = version
        _save_manifest(artifact_dir, manifest)


def delete_label(artifact_dir: Path, label: str) -> None:
    """Remove *label* from the manifest. No-op if absent. Raises
    :class:`ReservedLabelError` for ``latest``. Holds ``_file_lock``."""
    if label in RESERVED_LABELS:
        raise ReservedLabelError(f"{label!r} is a reserved label and cannot be deleted")
    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock):
        manifest = load_manifest(artifact_dir)
        if label in manifest.labels:
            del manifest.labels[label]
            _save_manifest(artifact_dir, manifest)


def resolve_version(artifact_dir: Path, tag: str) -> Path:
    """Resolve a bare version *tag* to its ``versions/<tag>.md`` path.

    READ-ONLY. Raises :class:`InvalidTagError` for a malformed tag and
    :class:`VersionNotFoundError` if the tag is not in the manifest or its file
    is missing.
    """
    _validate_tag(tag)
    manifest = load_manifest(artifact_dir)
    if tag not in manifest.versions:
        raise VersionNotFoundError(f"version {tag!r} does not exist")
    vfile = versions_dir(artifact_dir) / f"{tag}.md"
    if not vfile.is_file():
        raise VersionNotFoundError(f"version {tag!r} is recorded but {vfile} is missing")
    return vfile


def resolve_label(artifact_dir: Path, label: str) -> Path:
    """Resolve a named *label* to the ``versions/<tag>.md`` it points at.

    READ-ONLY. Does **not** handle ``latest`` — the caller must branch on it
    and read the working file directly (``latest`` is artifact-name-aware;
    this module is not). Raises :class:`LabelNotFoundError` if the label is
    absent and :class:`VersionNotFoundError` if it points at a missing version.
    """
    if label in RESERVED_LABELS:
        raise ReservedLabelError(
            f"{label!r} is reserved — resolve it to the working file at the call site"
        )
    manifest = load_manifest(artifact_dir)
    tag = manifest.labels.get(label)
    if tag is None:
        raise LabelNotFoundError(f"label {label!r} is not defined")
    return resolve_version(artifact_dir, tag)


def make_label_resolver(label: str) -> Callable[[Path, Layout], bytes]:
    """Build a ``(item_path, layout) -> bytes`` resolver for the sync engine.

    Plugged into ``AtomicSyncAdapter.resolve_canonical_bytes`` (ADR-0022) so a
    labeled ``mm context sync`` fans out a frozen version's bytes instead of
    the working file. The caller must NOT pass ``label`` of ``None`` or
    ``latest`` here — those use the unmodified adapter (working-file path).

    Layout handling (the flat-layout ``item_path.parent`` trap): only the
    directory layout has a per-artifact directory, so ``item_path.parent`` is
    the artifact root (``agents/<name>/``) there. A flat-layout artifact
    (``agents/<name>.md``) has no version store — resolving raises
    :class:`VersionsDirMissingError`, which the engine isolates as a skip.

    A value matching ``^v[1-9]\\d*$`` is treated as a **direct version tag**
    (``resolve_version``); any other string is a **named label**
    (``resolve_label``). This precedence is unambiguous because
    :func:`_validate_label_name` forbids creating a label whose name is
    version-shaped, so a ``vN`` here can only ever mean the version ``vN``.
    """

    def _resolve(item_path: Path, layout: Layout) -> bytes:
        if layout != "dir":
            raise VersionsDirMissingError(
                f"{item_path.name}: versioning requires directory layout "
                f"(run `mm context migrate` first)"
            )
        artifact_dir = item_path.parent
        if _VALID_TAG_RE.fullmatch(label):
            return resolve_version(artifact_dir, label).read_bytes()
        return resolve_label(artifact_dir, label).read_bytes()

    return _resolve
