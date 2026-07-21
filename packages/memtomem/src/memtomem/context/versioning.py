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
    └── versions.json       ← {"schema_version", "versions", "labels"} — only mutable state

Two storage shapes share one store (ADR-0030 §10). Agents and commands are
single files, so a version is ``versions/vN.md``. Skills are directory trees,
so a version is ``versions/vN/`` — a *tree snapshot*, marked ``layout: "tree"``
on its manifest entry and requiring ``schema_version`` 2. Tag allocation
reconciles across BOTH shapes and both are write-once; the difference is only
what a tag names on disk.

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
import os
import re
import secrets
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from memtomem.context._atomic import (
    _file_lock,
    _lock_path_for,
    atomic_write_bytes,
    fsync_dir,
    rename_no_replace,
    write_tree_payload,
)
from memtomem.context._names import Layout, is_internal_artifact_dir

__all__ = [
    "RESERVED_LABELS",
    "SCHEMA_VERSION",
    "VersionLayout",
    "VersionRecord",
    "VersionsManifest",
    "VersionError",
    "VersionNotFoundError",
    "LabelNotFoundError",
    "ReservedLabelError",
    "InvalidLabelError",
    "InvalidTagError",
    "TreeVersionError",
    "UnsupportedSchemaVersionError",
    "VersionsDirMissingError",
    "versions_dir",
    "versions_json_path",
    "load_manifest",
    "next_version_tag",
    "create_version",
    "create_tree_version",
    "promote_label",
    "delete_label",
    "resolve_label",
    "resolve_version",
    "resolve_version_tree",
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

#: The manifest schema this build UNDERSTANDS — readers refuse anything higher
#: (:class:`UnsupportedSchemaVersionError`). Absent on disk means 1 (the
#: original ``{"versions", "labels"}`` shape). Writers do NOT emit this value;
#: they emit :func:`_required_schema_version` — see there for why.
SCHEMA_VERSION = 2

#: Minimum schema a reader needs per feature. ``layout: "tree"`` entries
#: (ADR-0030 §10) are the only schema-2 feature; unknown top-level/per-entry
#: keys ride through ``extra`` and need no bump.
_SCHEMA_FILE_ENTRIES = 1
_SCHEMA_TREE_ENTRIES = 2

#: Per-version storage shape. ``"file"`` is ``versions/<tag>.md`` (agents,
#: commands, and every pre-G3 entry — the key is OMITTED from the JSON for
#: these, so existing manifests stay byte-shape identical). ``"tree"`` is a
#: ``versions/<tag>/`` directory snapshot (skills, ADR-0030 §10).
VersionLayout = Literal["file", "tree"]
_VALID_LAYOUTS: frozenset[str] = frozenset({"file", "tree"})

#: Top-level and per-entry keys this build owns. Anything else is preserved
#: verbatim through a load→mutate→save cycle via the ``extra`` fields.
_KNOWN_TOP_KEYS = frozenset({"schema_version", "versions", "labels"})
_KNOWN_ENTRY_KEYS = frozenset({"created_at", "note", "layout"})


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


class UnsupportedSchemaVersionError(VersionError):
    """``versions.json`` declares a ``schema_version`` newer than this build.

    Distinct from the malformed-value case (a plain :class:`VersionError`)
    because it is *server-side state*, not a bad request: the remedy is to
    upgrade memtomem, so surfaces translate it to a conflict rather than a
    validation failure."""


class VersionsDirMissingError(VersionError):
    """Versioning was attempted on an artifact with no per-artifact directory
    (flat layout). Run ``mm context migrate`` first."""


class TreeVersionError(VersionError):
    """A tree-layout version was addressed through a single-file API (or vice versa).

    :func:`resolve_version` / :func:`resolve_label` /
    :func:`make_label_resolver` all promise a path whose ``read_bytes()`` IS the
    artifact's content — a ``versions/<tag>/`` directory has no such bytes.
    Handing one back would surface as an ``IsADirectoryError`` deep inside
    fan-out or, worse, fail an ``is_file()`` check and produce a lying
    "recorded but missing" :class:`VersionNotFoundError` pointing at a path that
    plainly exists. Refusing with a named type keeps the message honest and
    gives the sync engine something specific to isolate. Labeled fan-out of tree
    versions is deferred (ADR-0030 §10)."""


@dataclass
class VersionRecord:
    """Metadata for one immutable version snapshot."""

    tag: str  # "v1", "v2", … (validated against _VALID_TAG_RE)
    created_at: str  # ISO-8601 UTC, e.g. "2026-06-03T09:00:00Z"
    note: str = ""
    #: Storage shape of this version — see :data:`VersionLayout`. ``kw_only``
    #: so inserting it here cannot silently re-bind an existing positional
    #: construction (``VersionRecord(tag, created_at, note)``).
    layout: VersionLayout = field(default="file", kw_only=True)
    #: Per-entry keys this build does not own, preserved verbatim so an old
    #: mutator cannot strip a future writer's fields.
    extra: dict[str, object] = field(default_factory=dict)


@dataclass
class VersionsManifest:
    """In-memory view of ``versions.json``. Mutated by callers under lock, then
    written back via :func:`_save_manifest`."""

    versions: dict[str, VersionRecord] = field(default_factory=dict)
    labels: dict[str, str] = field(default_factory=dict)  # label_name → tag
    #: Schema declared ON DISK (absent means 1). This is a READ-ONLY
    #: observation, NOT the value the next save emits — writers emit
    #: :func:`_required_schema_version`, which is derived from the manifest's
    #: actual content. Keeping the two separate is what stops an unrelated
    #: mutation from advertising a schema the manifest does not use.
    schema_version: int = _SCHEMA_FILE_ENTRIES
    #: Top-level keys this build does not own, preserved verbatim.
    extra: dict[str, object] = field(default_factory=dict)


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


def _validate_schema_version(raw: dict[str, object], path: Path) -> int:
    """Return the manifest's declared ``schema_version``; absent means 1.

    Fails LOUD rather than coercing: a manifest written by a newer build may use
    a layout this one would misread (or silently strip), so refusing is the only
    safe read. ``bool`` is rejected explicitly because ``isinstance(True, int)``
    is ``True`` in Python and ``{"schema_version": true}`` must not read as 1.

    Absent reads as 1 (the original shape), not as this build's
    :data:`SCHEMA_VERSION` — the return value describes what is ON DISK, and
    claiming a legacy file already declared the newest schema would make the
    observation useless the moment the constant advances.
    """
    value = raw.get("schema_version")
    if value is None:
        return _SCHEMA_FILE_ENTRIES
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise VersionError(
            f"malformed versions manifest at {path}: 'schema_version' must be a "
            f"positive integer, got {value!r}"
        )
    if value > SCHEMA_VERSION:
        raise UnsupportedSchemaVersionError(
            f"unsupported versions manifest at {path}: 'schema_version' {value} is "
            f"newer than this build understands ({SCHEMA_VERSION}) — upgrade memtomem"
        )
    return value


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
    # Refuse a newer schema BEFORE parsing anything else: a future layout could
    # make the fields below mean something different.
    schema_version = _validate_schema_version(raw, path)
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
        # A non-object entry is coerced to an empty one, which the next save
        # then writes back as `{}` — a silent strip of whatever it held. That
        # is only safe because a future writer using a different entry SHAPE
        # must also bump ``schema_version``, which the gate above refuses
        # before we get here. Keep those two facts together: if entries ever
        # gain a non-object form under the CURRENT schema, this must become a
        # hard error instead.
        meta = meta if isinstance(meta, dict) else {}
        raw_layout = meta.get("layout", "file")
        # ``isinstance`` FIRST: a hand-edited manifest can hold any JSON value,
        # and an unhashable one (``["tree"]``) would raise TypeError out of the
        # membership test — this module's contract is that every malformed
        # manifest surfaces a clean VersionError.
        if not isinstance(raw_layout, str) or raw_layout not in _VALID_LAYOUTS:
            # A newer writer using a new storage shape MUST also bump
            # ``schema_version``, which the gate above already refused — so an
            # unrecognized value down here is corruption, not forward-compat.
            # Fail closed: treating an unknown tree-ish shape as a readable
            # file is how a resolver ends up handing a directory to
            # ``read_bytes()``.
            raise VersionError(
                f"malformed versions manifest at {path}: version {tag!r} has unknown "
                f"layout {raw_layout!r} (expected one of {sorted(_VALID_LAYOUTS)})"
            )
        # Re-stated as a literal so the type narrows without a ``cast`` — a cast
        # here would assert the very property the guard above exists to check.
        layout: VersionLayout = "tree" if raw_layout == "tree" else "file"
        versions[tag] = VersionRecord(
            tag=tag,
            created_at=str(meta.get("created_at", "")),
            note=str(meta.get("note", "")),
            layout=layout,
            extra={k: v for k, v in meta.items() if k not in _KNOWN_ENTRY_KEYS},
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

    manifest = VersionsManifest(
        versions=versions,
        labels=labels,
        schema_version=schema_version,
        extra={k: v for k, v in raw.items() if k not in _KNOWN_TOP_KEYS},
    )
    # The declared schema must cover what the entries actually use. A
    # ``schema_version: 1`` manifest carrying a ``layout: "tree"`` entry is
    # self-contradictory — no build that wrote it could have meant it, and
    # accepting it would let a hand-edited file smuggle tree state past the
    # version gate and out through the read-only surfaces.
    required = _required_schema_version(manifest)
    if schema_version < required:
        raise VersionError(
            f"malformed versions manifest at {path}: declares schema_version "
            f"{schema_version} but its entries require {required}"
        )
    return manifest


def _save_manifest(artifact_dir: Path, manifest: VersionsManifest) -> None:
    """Atomically write *manifest* to ``versions.json``.

    PRIVATE. The caller MUST already hold ``_file_lock`` on the sidecar — there
    is no public single-call path, because ``_file_lock`` is non-reentrant and
    every mutation runs inside the larger ``create_version`` / ``promote_label``
    / ``delete_label`` transaction.
    """
    payload: dict[str, object] = {
        "schema_version": _required_schema_version(manifest),
        "versions": {
            tag: _entry_payload(rec)
            for tag, rec in sorted(manifest.versions.items(), key=lambda kv: _tag_num(kv[0]))
        },
        "labels": {label: manifest.labels[label] for label in sorted(manifest.labels)},
    }
    # Round-trip top-level keys this build does not own so a future writer's
    # fields survive an old mutator's load→mutate→save cycle. Known keys win;
    # sorted for a deterministic file.
    for key in sorted(manifest.extra):
        if key not in _KNOWN_TOP_KEYS:
            payload[key] = manifest.extra[key]
    data = (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write_bytes(versions_json_path(artifact_dir), data)


def _required_schema_version(manifest: VersionsManifest) -> int:
    """Lowest ``schema_version`` a reader must understand for *manifest*.

    Writers emit THIS, never :data:`SCHEMA_VERSION`. Tree-layout entries
    (ADR-0030 §10) are the only schema-2 feature; unknown top-level/per-entry
    keys ride through ``extra`` and need no bump.

    The distinction is load-bearing. ``_save_manifest`` rewrites the whole file
    on every ``promote_label`` / ``delete_label``, so emitting this build's
    maximum would silently stamp ``schema_version: 2`` onto every flat
    agents/commands store the first time anyone moved a label — and an older
    memtomem would then refuse a manifest containing nothing it cannot read,
    spending the forward-compat valve PR-G1 just built. Emitting the minimum
    keeps the flat fleet on schema 1 for as long as it stays flat.

    Consequence, deliberate and pinned by test: a hand-written
    ``schema_version: 2`` manifest with only file entries is DOWNGRADED to 1 on
    the next mutation. That is correct — every reader can read it.
    """
    if any(rec.layout == "tree" for rec in manifest.versions.values()):
        return _SCHEMA_TREE_ENTRIES
    return _SCHEMA_FILE_ENTRIES


def _entry_payload(rec: VersionRecord) -> dict[str, object]:
    """One ``versions.json`` entry, with unrecognized per-entry keys preserved.

    ``layout`` is emitted only when non-default, so file-layout entries stay
    byte-shape identical to what every pre-G3 build wrote and reads.
    """
    entry: dict[str, object] = {"created_at": rec.created_at, "note": rec.note}
    if rec.layout != "file":
        entry["layout"] = rec.layout
    for key in sorted(rec.extra):
        if key not in _KNOWN_ENTRY_KEYS:
            entry[key] = rec.extra[key]
    return entry


def _tag_num(tag: str) -> int:
    """Numeric suffix of a validated tag (``"v3"`` → ``3``)."""
    return int(tag[1:])


def next_version_tag(manifest: VersionsManifest) -> str:
    """Return ``"v1"`` if no versions exist, else ``"v<max+1>"``. Pure (no I/O)."""
    if not manifest.versions:
        return "v1"
    return f"v{max(_tag_num(t) for t in manifest.versions) + 1}"


def create_version(
    artifact_dir: Path,
    working_file: Path,
    note: str = "",
    *,
    source_bytes: bytes | None = None,
    lock_timeout: float | None = None,
) -> VersionRecord:
    """Snapshot *working_file* into ``versions/<tag>.md`` and record it.

    Holds a single ``_file_lock`` on the ``versions.json`` sidecar across the
    whole transaction (``load → allocate tag → write vN.md → save manifest``),
    so two concurrent callers cannot both allocate the same tag. The version
    file is write-once: if ``versions/<tag>.md`` already exists the call raises
    :class:`InvalidTagError` rather than overwriting.

    ``lock_timeout``: bound (seconds) on the sidecar-lock wait; ``None``
    blocks indefinitely (CLI default — Ctrl-C-able). Callers that must not
    block forever (an async web handler offloading to a worker thread) pass a
    budget; on expiry :func:`memtomem.context._atomic._file_lock` raises the
    builtin ``TimeoutError`` having acquired nothing (#1145 shape).

    ``source_bytes``: pass the bytes the caller already read (and privacy-
    scanned) to snapshot *exactly* those, closing the scan→write TOCTOU window —
    otherwise re-reading ``working_file`` here could capture a concurrently
    edited (unsafe) file the caller never scanned. ``None`` (default) reads
    ``working_file`` for callers with no pre-scan obligation.

    Raises :class:`VersionsDirMissingError` if *artifact_dir* does not exist
    (flat-layout artifact has no per-artifact directory).
    """
    if not artifact_dir.is_dir():
        raise VersionsDirMissingError(
            f"{artifact_dir} is not a directory — versioning requires directory layout "
            f"(run `mm context migrate` first)"
        )
    if source_bytes is None:
        try:
            source_bytes = working_file.read_bytes()
        except OSError as exc:
            raise VersionError(f"cannot read working canonical {working_file}: {exc}") from exc

    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock, timeout=lock_timeout):
        manifest = load_manifest(artifact_dir)
        # Allocate against BOTH the manifest and any on-disk ``vN.md`` files.
        # A crash between the version-file write and the manifest save (below)
        # leaves an orphan ``vN.md`` absent from the manifest; allocating off
        # the manifest alone would recompute the same tag, hit the
        # write-once guard, and wedge every future create. Reconciling against
        # disk skips past the orphan instead (the orphan stays unreferenced and
        # harmless — it is not in the manifest, so it is never listed/resolved).
        tag = _next_version_tag_reconciled(artifact_dir, manifest)
        vfile = versions_dir(artifact_dir) / f"{tag}.md"
        # By construction ``tag`` is free on disk; keep the no-overwrite check
        # as a defensive assertion against an unexpected race.
        if vfile.exists():
            raise InvalidTagError(f"version file already exists: {vfile}")
        atomic_write_bytes(vfile, source_bytes)
        record = VersionRecord(tag=tag, created_at=_now_iso(), note=note)
        manifest.versions[tag] = record
        _save_manifest(artifact_dir, manifest)
    return record


def _next_version_tag_reconciled(artifact_dir: Path, manifest: VersionsManifest) -> str:
    """Next tag considering the manifest, on-disk ``vN.md`` files AND ``vN/`` dirs.

    Crash-safe variant of :func:`next_version_tag`: an orphan snapshot (written
    before its manifest entry was saved) bumps the allocation forward instead
    of colliding. Caller must hold the sidecar lock.

    Orphans of BOTH layouts are PRESERVED, never reaped (ADR-0030 §10): an
    orphan is real snapshot bytes whose row we merely failed to record, and
    deleting it would destroy the only copy. It stays unreferenced and harmless
    (never listed, never resolved) while its tag is skipped.

    Reconciliation is deliberately cross-layout — a tree ``v2/`` must stop a
    flat :func:`create_version` from minting ``v2.md``, or one tag would name
    two different snapshots. ``.staging-*.tmp`` transients never match
    ``^v[1-9]\\d*$``, so they are ignored without a special case.
    """
    nums = {_tag_num(t) for t in manifest.versions}
    vdir = versions_dir(artifact_dir)
    if vdir.is_dir():
        for entry in vdir.iterdir():
            stem = entry.name[:-3] if entry.name.endswith(".md") else entry.name
            if _VALID_TAG_RE.fullmatch(stem):
                nums.add(_tag_num(stem))
    return f"v{max(nums) + 1}" if nums else "v1"


def _validate_tree_payload(payload: Sequence[tuple[str, bytes]]) -> None:
    """Anti-recursion guard on a tree-snapshot payload.

    Structural traversal safety (absolute paths, ``..``, empty segments,
    duplicates) is enforced by :func:`~memtomem.context._atomic.write_tree_payload`
    at the write primitive. This adds the one rule THIS module owns: a snapshot
    may never contain the version store. Without it ``v2`` would contain ``v1``,
    every snapshot would double the store, and fan-out would push version
    history into runtimes.

    Expressed against this module's own constants. The payload-SCOPE rules
    (``overrides/``, the manifest's lock/tmp sidecars) belong to
    :mod:`memtomem.context.skill_payload`, which imports this module — so the
    reverse import would be a cycle and the caller supplies an already-filtered
    payload.
    """
    if not payload:
        raise VersionError("cannot snapshot an empty payload")
    for rel, _ in payload:
        head = rel.split("/", 1)[0]
        if head in (_VERSIONS_DIRNAME, _MANIFEST_FILENAME):
            raise VersionError(
                f"payload entry {rel!r} is version-store internal — a snapshot cannot "
                f"contain the version store"
            )


def _refuse_case_colliding_store(artifact_dir: Path) -> None:
    """Refuse a store whose reserved names are aliased by a case variant.

    On a case-INSENSITIVE filesystem (macOS default, Windows) a pre-existing
    ``Versions/`` IS ``versions/`` on disk, but the payload iterator's exclusion
    set is case-sensitive and reads ``Versions/`` as ordinary skill content. The
    two disagree, and the snapshot lands inside a directory the next payload
    read will happily include — so ``v2`` ends up containing ``v1`` and fan-out
    ships version history, which is precisely the recursion hazard ADR-0030 §10
    exists to prevent.

    Refuse loudly instead of guessing. Silently folding case in the payload
    iterator would be the other option, but it would change what counts as skill
    *content* on case-sensitive filesystems too, where ``Versions/`` is a
    legitimate, distinct user directory.

    Which is exactly why the test is ALIASING, not spelling: ``samefile`` asks
    the filesystem whether the two names reach the same inode. On ext4 they do
    not, so a user's ``Versions/`` is left alone; on APFS/NTFS they do, and we
    refuse. A name-only check would have banned legitimate content on Linux to
    fix a bug that only exists on macOS and Windows.
    """
    reserved = {_VERSIONS_DIRNAME, _MANIFEST_FILENAME}
    try:
        entries = list(artifact_dir.iterdir())
    except OSError as exc:
        raise VersionError(f"cannot read artifact directory {artifact_dir}: {exc}") from exc
    for entry in entries:
        lowered = entry.name.lower()
        if lowered in reserved and entry.name != lowered:
            canonical = artifact_dir / lowered
            try:
                aliases = canonical.exists() and entry.samefile(canonical)
            except OSError:
                # Cannot prove they are distinct → treat as a collision. The
                # cost of a false refusal is a rename; the cost of a false pass
                # is version history leaking into every runtime.
                aliases = True
            if aliases:
                raise VersionError(
                    f"{artifact_dir / entry.name} is the same directory entry as the version "
                    f"store's {lowered!r} on this case-insensitive filesystem — rename it "
                    f"before versioning this artifact"
                )


def _reap_version_staging(vdir: Path) -> None:
    """Remove crash-leftover ``versions/.staging-*`` trees.

    Caller holds the sidecar lock, so no live staging tree for this store can
    exist concurrently.

    Deliberately narrow, and it must STAY narrow: orphan ``vN.md`` / ``vN/`` are
    load-bearing history (see :func:`_next_version_tag_reconciled`). The kill
    decision is delegated to
    :func:`~memtomem.context._names.is_internal_artifact_dir` — the same
    predicate the extract/reap paths use — so a name that merely looks
    staging-ish is never deleted.
    """
    if not vdir.is_dir():
        return
    for stale in vdir.glob(".staging-v*.tmp"):
        if is_internal_artifact_dir(stale.name):
            shutil.rmtree(stale, ignore_errors=True)


def create_tree_version(
    artifact_dir: Path,
    payload: Sequence[tuple[str, bytes]],
    note: str = "",
    *,
    lock_timeout: float | None = None,
) -> VersionRecord:
    """Snapshot a captured *payload* into ``versions/<tag>/`` and record it.

    The tree twin of :func:`create_version` (ADR-0030 §10). There is no
    ``working_file``: a skill's "working canonical" is its whole payload tree,
    so the caller passes the ``(posix_relpath, bytes)`` pre-image it already
    read and privacy-scanned — see
    :func:`memtomem.context.skill_payload.iter_skill_payload_files`, which is
    also what defines which files are payload at all.

    Bytes are copied into NEW inodes from that pre-image. It never hardlinks
    live payload: an editor, or a crash mid-swap, could then mutate history
    through the shared inode, silently rewriting a snapshot that exists
    precisely so it cannot change.

    Atomicity and durability: stage into
    ``versions/.staging-<tag>-<pid>-<rand>.tmp`` (same directory, hence same
    filesystem, hence the promote is a rename), fsync each file and staged
    directory (``F_FULLFSYNC`` on macOS), then
    :func:`~memtomem.context._atomic.rename_no_replace` into
    ``versions/<tag>/`` — write-once, the destination is never replaced — then
    fsync ``versions/``. Directory fsync is BEST-EFFORT: Windows and some
    network/tmpfs mounts reject it, and there the guarantee degrades to
    process-crash consistency, matching the existing single-file write. The
    snapshot lands BEFORE the manifest row, so a crash leaves either an orphan
    ``vN/`` (preserved, bumps the next tag) or a complete entry — never a row
    pointing at nothing.

    LOCKING — read this before adding a caller. Takes ONLY the ``versions.json``
    sidecar lock (C1), exactly like :func:`create_version`. ``_file_lock`` is
    NON-REENTRANT, so a caller already inside the canonical name lock (C0) —
    ``pull_apply._commit_skills``, which is what PR-G4 wires up — must call this
    DIRECTLY with its remaining budget. Routing it through
    :func:`memtomem.context._canonical_txn.versioning_op_locked` would re-acquire
    C0 and self-deadlock. Callers OUTSIDE a canonical transaction use
    ``versioning_op_locked`` to get the ADR-0030 §6 order C0 → C1.

    Raises :class:`VersionsDirMissingError` (no artifact directory),
    :class:`VersionError` (empty payload or a version-store-internal relpath),
    ``ValueError`` (malformed relpath, from the write primitive),
    :class:`InvalidTagError` (defensive write-once assertion), ``TimeoutError``
    (sidecar budget) or ``OSError`` (staging/promote failure). On every failure
    the staging tree is removed and the manifest is left untouched.
    """
    if not artifact_dir.is_dir():
        raise VersionsDirMissingError(
            f"{artifact_dir} is not a directory — versioning requires directory layout "
            f"(run `mm context migrate` first)"
        )
    # Validate before taking the lock: a bad payload is a caller bug, and there
    # is no reason to make a concurrent writer wait for it.
    _validate_tree_payload(payload)

    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock, timeout=lock_timeout):
        _refuse_case_colliding_store(artifact_dir)
        vdir = versions_dir(artifact_dir)
        vdir.mkdir(parents=True, exist_ok=True)
        # Re-check AFTER the mkdir. The sidecar lock serializes memtomem's own
        # writers, but nothing stops an out-of-band ``mkdir Versions/`` landing
        # between the check and here — on a case-insensitive filesystem
        # ``exist_ok=True`` would then silently adopt that alias and stage the
        # snapshot into what the payload iterator still reads as user content.
        _refuse_case_colliding_store(artifact_dir)
        # Make ``versions/``'s own entry durable BEFORE anything is promoted
        # into it, or a power cut could leave a saved manifest row naming a
        # directory whose entry never reached stable storage — the one state
        # the snapshot-before-row ordering exists to rule out.
        #
        # Unconditional, deliberately. "It already existed" is NOT proof the
        # entry was ever fsynced: the flat ``create_version`` creates
        # ``versions/`` as a side effect of ``atomic_write_bytes``' parent
        # mkdir and never syncs the parent, so a flat-then-tree sequence would
        # skip the barrier on exactly the shape that needs it. A redundant
        # fsync of an already-durable directory is cheap; reasoning about who
        # synced it first is not.
        fsync_dir(artifact_dir)
        _reap_version_staging(vdir)
        manifest = load_manifest(artifact_dir)
        tag = _next_version_tag_reconciled(artifact_dir, manifest)
        target = vdir / tag
        # By construction ``tag`` is free on disk; keep the check as a
        # defensive assertion (the rename below is exclusive regardless).
        if target.exists():
            raise InvalidTagError(f"version snapshot already exists: {target}")

        staging = vdir / f".staging-{tag}-{os.getpid()}-{secrets.token_hex(3)}.tmp"
        try:
            write_tree_payload(staging, payload, durable=True)
            rename_no_replace(staging, target)
        except BaseException:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        # Make the promote itself durable before we advertise it in the
        # manifest — the reverse order could survive a power cut as a row
        # naming a directory whose entry never reached stable storage.
        fsync_dir(vdir)

        record = VersionRecord(tag=tag, created_at=_now_iso(), note=note, layout="tree")
        manifest.versions[tag] = record
        _save_manifest(artifact_dir, manifest)
        _verify_manifest_spelling(artifact_dir)
        fsync_dir(artifact_dir)
    return record


def _verify_manifest_spelling(artifact_dir: Path) -> None:
    """Ensure the manifest ended up under its canonical name, repairing if not.

    The last gap in the case-alias story. Both collision checks run before the
    manifest exists, so an out-of-band ``Versions.JSON`` created later in the
    transaction is still adopted — and the two case-insensitive filesystems
    disagree about what happens next:

    - **APFS** keeps the EXISTING entry's spelling through ``os.replace``, so
      our bytes land under ``Versions.JSON``. ``is_payload_top_name`` is
      case-sensitive, so the manifest would then read as ordinary skill content
      and fan version metadata out to every runtime — the one outcome §10
      exists to prevent.
    - **NTFS** adopts the source spelling, so the entry is already canonical.

    Repair rather than merely refuse. By the time we get here ``os.replace``
    has already overwritten whatever was in that entry with OUR manifest, so a
    same-file rename to the canonical spelling destroys no user data — it
    finishes our own write. Refusing instead would leave the store functional
    but permanently mis-spelled, which is the leak we are trying to close.

    Still fails closed if the rename cannot be made to stick: a loud error with
    the promoted ``vN/`` preserved as an orphan (:func:`_next_version_tag_reconciled`
    skips its tag) beats a silent metadata leak.
    """

    def _entries() -> set[str]:
        try:
            return {entry.name for entry in artifact_dir.iterdir()}
        except OSError as exc:
            raise VersionError(f"cannot read artifact directory {artifact_dir}: {exc}") from exc

    if _MANIFEST_FILENAME in _entries():
        return
    canonical = versions_json_path(artifact_dir)
    for name in sorted(_entries()):
        if name.lower() != _MANIFEST_FILENAME:
            continue
        try:
            os.replace(artifact_dir / name, canonical)
        except OSError as exc:
            raise VersionError(
                f"{canonical} did not land under its canonical name and could not be "
                f"repaired ({exc}) — a case-variant of {_MANIFEST_FILENAME!r} claimed the "
                f"directory entry. Rename it manually; the snapshot is preserved."
            ) from exc
        break
    if _MANIFEST_FILENAME not in _entries():
        raise VersionError(
            f"{canonical} did not land under its canonical name — a case-variant of "
            f"{_MANIFEST_FILENAME!r} claimed the directory entry. Rename it manually; "
            f"the snapshot is preserved."
        )


def promote_label(
    artifact_dir: Path, label: str, version: str, *, lock_timeout: float | None = None
) -> None:
    """Point *label* at *version* (create-or-move). Rollout == rollback.

    Raises :class:`ReservedLabelError` for ``latest``, :class:`InvalidLabelError`
    for a version-shaped label name, :class:`InvalidTagError` for a malformed
    tag, and :class:`VersionNotFoundError` if the tag is not in the manifest.
    Holds ``_file_lock`` across ``load → validate → mutate → save``;
    ``lock_timeout`` bounds the wait as in :func:`create_version`.
    """
    _validate_label_name(label)
    _validate_tag(version)
    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock, timeout=lock_timeout):
        manifest = load_manifest(artifact_dir)
        if version not in manifest.versions:
            raise VersionNotFoundError(f"version {version!r} does not exist")
        if manifest.versions[version].layout == "tree":
            # Same discipline as ``_validate_label_name``: never store a pointer
            # no resolver could follow. Labeled fan-out of tree snapshots is
            # deferred (ADR-0030 §10), so this label would be dead on arrival.
            raise TreeVersionError(
                f"cannot point label {label!r} at {version!r}: labeled fan-out of tree "
                f"snapshots is deferred (ADR-0030 §10) — the pointer could never be honored"
            )
        manifest.labels[label] = version
        _save_manifest(artifact_dir, manifest)


def delete_label(artifact_dir: Path, label: str, *, lock_timeout: float | None = None) -> None:
    """Remove *label* from the manifest. No-op if absent. Raises
    :class:`ReservedLabelError` for ``latest``. Holds ``_file_lock``;
    ``lock_timeout`` bounds the wait as in :func:`create_version`."""
    if label in RESERVED_LABELS:
        raise ReservedLabelError(f"{label!r} is a reserved label and cannot be deleted")
    lock = _lock_path_for(versions_json_path(artifact_dir))
    with _file_lock(lock, timeout=lock_timeout):
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
    if manifest.versions[tag].layout == "tree":
        raise TreeVersionError(
            f"version {tag!r} is a tree snapshot — it has no single file to read "
            f"(use resolve_version_tree); labeled fan-out of tree versions is not supported"
        )
    vfile = versions_dir(artifact_dir) / f"{tag}.md"
    if not vfile.is_file():
        raise VersionNotFoundError(f"version {tag!r} is recorded but {vfile} is missing")
    return vfile


def resolve_version_tree(artifact_dir: Path, tag: str) -> Path:
    """Resolve a tree-layout *tag* to its ``versions/<tag>/`` directory.

    READ-ONLY. The mirror of :func:`resolve_version`, so neither API can
    silently serve the other's shape: a file-layout entry is refused here with
    :class:`TreeVersionError` just as a tree entry is refused there. Raises
    :class:`InvalidTagError` for a malformed tag and :class:`VersionNotFoundError`
    when the tag is absent from the manifest or its directory is missing.
    """
    _validate_tag(tag)
    manifest = load_manifest(artifact_dir)
    if tag not in manifest.versions:
        raise VersionNotFoundError(f"version {tag!r} does not exist")
    if manifest.versions[tag].layout != "tree":
        raise TreeVersionError(f"version {tag!r} is a file snapshot — use resolve_version")
    vdir = versions_dir(artifact_dir) / tag
    if not vdir.is_dir():
        raise VersionNotFoundError(f"version {tag!r} is recorded but {vdir} is missing")
    return vdir


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


def make_label_resolver(label: str) -> Callable[[Path, Layout], tuple[bytes, Path]]:
    """Build a ``(item_path, layout) -> (bytes, source_path)`` resolver.

    Plugged into ``AtomicSyncAdapter.resolve_canonical_bytes`` (ADR-0022) so a
    labeled ``mm context sync`` fans out a frozen version's bytes instead of
    the working file. The caller must NOT pass ``label`` of ``None`` or
    ``latest`` here — those use the unmodified adapter (working-file path).

    Returns the resolved bytes **and the version file they came from**, so the
    engine can attribute its Gate A privacy scan to the actual
    ``versions/vN.md`` (not the clean working ``agent.md``) — otherwise a secret
    living only in a frozen version would point remediation at the wrong file.

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

    def _resolve(item_path: Path, layout: Layout) -> tuple[bytes, Path]:
        if layout != "dir":
            raise VersionsDirMissingError(
                f"{item_path.name}: versioning requires directory layout "
                f"(run `mm context migrate` first)"
            )
        artifact_dir = item_path.parent
        if _VALID_TAG_RE.fullmatch(label):
            vfile = resolve_version(artifact_dir, label)
        else:
            vfile = resolve_label(artifact_dir, label)
        return vfile.read_bytes(), vfile

    return _resolve
