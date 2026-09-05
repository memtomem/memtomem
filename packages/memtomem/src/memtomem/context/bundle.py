"""ADR-0037 — one context artifact as a portable file.

The only way to hand a single skill, command, or agent to a colleague or to
another machine. :func:`export_artifact_bundle` packs one canonical artifact
into a JSON bundle; :func:`receive_artifact_bundle` validates a bundle and lands
it in a canonical store. Both are pure filesystem work with no network, no
wiki dependency, and no MCP or web surface (ADR-0037 §1, §10).

**The recognized grammar is bounded and written down.** This module reads
untrusted input, so what it accepts is an allowlist rather than "whatever did
not look wrong":

* the wire schema and path grammar of :func:`load_bundle` (ADR-0037 §2) —
  ASCII path segments, no reserved device words, no copier-reserved names, no
  case-folded duplicates or ancestor collisions;
* the resource bounds of :data:`_MAX_BUNDLE_BYTES` and friends (§7), all
  enforced before anything is decoded;
* the artifact-form rules (§8) — the manifest the kind requires is present,
  the landing name is re-validated, the frontmatter ``name:`` is rewritten;
* the version-surface rules of :func:`_validate_version_surface` (§9), run on
  both the export and the receipt side so a writer cannot emit a bundle its own
  reader refuses.

Anything outside that is refused by name, never silently dropped or normalized:
a silent rename or a skipped entry makes the landed tree disagree with the
bundle the sender listed, and the sender never learns.

**Gate ordering is the security contract.** Export reads each file once through
a no-follow descriptor and scans those in-memory bytes at
no force valve of any kind, because a file handed to someone else is as
unretractable as a pushed commit (§4). The one exemption is the artifact's own
``redaction: documents-patterns`` declaration, which waives only the two
unquoted-label rules and only when every hit in a file is one of them. Receipt decides
everything it can in memory — parse, validate, rewrite the manifest name, scan
— and only then materializes once and promotes, so the bytes scanned are
exactly the bytes that land (§6).
"""

from __future__ import annotations

import base64
import errno
import logging
import hashlib
import json
import os
import re
import secrets
import stat
from collections.abc import Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

import click

from memtomem.config import TargetScope
from memtomem.context._atomic import (
    COPY_SKIP_NAMES,
    DIRTY_SKIP_SUFFIXES,
    atomic_write_bytes,
    rename_no_replace,
    write_tree_payload,
)
from memtomem.context._canonical_txn import canonical_sidecar_lock
from memtomem.context import _skip_reasons as skip_codes
from memtomem.context._names import (
    OVERRIDE_FORMATS,
    REAPABLE_INTERNAL_ARTIFACT_KINDS,
    InvalidNameError,
    internal_artifact_owner,
    is_internal_artifact_dir,
    validate_name,
)
from memtomem.context.migrate import (
    _DIR_MANIFEST,
    SCOPE_MIGRATABLE_KINDS,
    ArtifactNotFoundError,
    _detect_source_scope,
)
from memtomem.context.agents import _AGENT_ADAPTER
from memtomem.context.commands import _COMMAND_ADAPTER
from memtomem.context.privacy_scan import (
    PrivacyBlockedError,
    raise_or_collect,
    scan_text_content,
)
from memtomem.context.scope_resolver import ArtifactKind, canonical_artifact_dir
from memtomem.context.skills import SwapRecoveryError, run_swap_prelude, swap_failure_text
from memtomem.context.versioning import (
    SCHEMA_VERSION,
    VersionError,
    _validate_label_name,
    _validate_tag,
)

# The one ``context -> indexing`` import in this package, and deliberately so:
# ``redaction_exemption`` is the single reader of the ADR-0006 Axis E.5
# declaration, and a second copy of that YAML-frontmatter parsing is exactly how
# the two would drift on what counts as a declaration. It is a leaf (it imports
# only ``privacy``), ``indexing/__init__`` is empty, and nothing here reaches
# the indexing engine — so this does not close a cycle or pull in that weight.
from memtomem.indexing.redaction_exemption import declared_exemption, indexer_text
from memtomem.privacy import DECLARED_EXEMPTION_DOCUMENTS_PATTERNS, exemption_covers
from memtomem.context.transfer import (
    TransferCollisionError,
    TransferRecoveryError,
    _classify_provenance_carry,
    _remove_staging,
    _sync_followup,
    rewrite_manifest_name_bytes,
)

__all__ = [
    "ArtifactBundle",
    "BUNDLE_FORMAT",
    "BUNDLE_VERSION",
    "BundleExportResult",
    "BundleFormatError",
    "BundleIntegrityError",
    "BundlePrivacyError",
    "BundleReceiveResult",
    "BundleSourceError",
    "export_artifact_bundle",
    "load_bundle",
    "receive_artifact_bundle",
]

logger = logging.getLogger(__name__)

BUNDLE_FORMAT = "memtomem-context-artifact-bundle"
BUNDLE_VERSION = 1

#: ADR-0037 §7. Every bound is part of the format, not an implementation
#: detail — two readers that disagree about which bundles are valid would make
#: "a bundle exports here and imports there" false.
_MAX_BUNDLE_BYTES = 100 * 1024 * 1024
_MAX_FILE_ENTRIES = 4096
_MAX_DIR_ENTRIES = 4096
_MAX_DECODED_BYTES = 64 * 1024 * 1024
_MAX_ENTRY_DECODED_BYTES = 16 * 1024 * 1024
_MAX_SEGMENT_CHARS = 64
_MAX_PATH_CHARS = 255
_MAX_PATH_SEGMENTS = 16
_MAX_JSON_DEPTH = 8

#: Scope the egress scan runs at. NOT ``project_shared``: that tier refuses a
#: per-file ``redaction: documents-patterns`` declaration outright, and real
#: artifacts are full of documented credential shapes — every skill on the
#: author's machine was refused on ``api_key: str`` annotations alone. The
#: promise §4 makes is "no force valve on export", which this keeps: nothing
#: here ever passes ``force_unsafe``, and the declaration waives only the two
#: unquoted-label rules, all-or-nothing, from a marker that travels with the
#: artifact so the receiver sees the same claim.
_EGRESS_SCAN_SCOPE: TargetScope = "user"

#: Manifest schema versions the store can read. DERIVED from the store's own
#: ceiling rather than written out: ``versioning.resolve_schema_version``
#: accepts anything at or below :data:`versioning.SCHEMA_VERSION`, so a literal
#: set here becomes a second, quietly stricter rule the moment that constant is
#: bumped — export would then refuse, with a message claiming the store cannot
#: read it, artifacts the store reads fine. The surrounding code already
#: borrows ``_validate_tag`` and ``_validate_label_name`` from that module for
#: exactly this reason; the schema gate was the one rule that was copied.
_KNOWN_VERSION_SCHEMAS: frozenset[int] = frozenset(range(1, SCHEMA_VERSION + 1))

#: The store's own parsers, so a snapshot's name is read the way the sync
#: fan-out reads it rather than by a second matcher that can disagree.
_SYNC_ADAPTERS = {"agents": _AGENT_ADAPTER, "commands": _COMMAND_ADAPTER}

# Every one of these is consumed with ``fullmatch``: ``match`` plus a ``$``
# anchor accepts a TRAILING NEWLINE, which would have admitted "evil\n" and
# ".git\n" as path segments straight past the grammar below.
_SEGMENT_RE = re.compile(r"[A-Za-z0-9._-]{1,%d}" % _MAX_SEGMENT_CHARS)
_HEX64_RE = re.compile(r"[0-9a-f]{64}")
_HEX40_RE = re.compile(r"[0-9a-f]{40}")
_ISO_Z_RE = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z")

#: Windows resolves these as devices whatever the extension, so a bundle
#: carrying one cannot be materialized consistently. A character allowlist
#: cannot express a reserved *word*, hence the separate set.
_RESERVED_DEVICE_WORDS = frozenset(
    {"con", "prn", "aux", "nul"}
    | {f"com{i}" for i in range(1, 10)}
    | {f"lpt{i}" for i in range(1, 10)}
)

#: Names a conforming exporter never emits, refused on receipt too (§2). A
#: crafted bundle could otherwise land paths the install and dirty walkers
#: ignore — a place to stash bytes no later integrity check looks at.
_FORBIDDEN_COMPONENTS_FOLDED = frozenset(name.lower() for name in COPY_SKIP_NAMES)


class BundleFormatError(Exception):
    """The file is not a bundle this version can read.

    Surfaced as a plain ``Exception`` so each surface renders it natively (the
    :class:`~memtomem.context.privacy_scan.PrivacyScanError` precedent); the
    CLI translates it to a ``click.ClickException``.
    """


class BundleIntegrityError(BundleFormatError):
    """A digest did not match what the bundle claims.

    Transport corruption or tampering. Never a reason to trust the bundle
    *less* selectively: the whole file is refused.
    """


class BundleSourceError(Exception):
    """The source artifact cannot be packed as it stands.

    A symlink at a carried path, an unreadable entry, a mid-swap tree, or an
    unhealthy version surface. Distinct from a privacy block so the CLI can
    say what the user has to fix.
    """


class BundlePrivacyError(Exception):
    """Receipt Gate A blocked a ``user`` / ``project_local`` landing.

    Carries the neutral condition — which entry, which tier — plus the
    ``reason_code`` its surface maps to a remediation clause (#1869).
    ``project_shared`` never reaches here: it raises
    :class:`~memtomem.context.privacy_scan.PrivacyBlockedError`, which has no
    valve at all (ADR-0011 §5).
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        #: ``reason_code`` the surface maps to its own remediation vocabulary.
        self.code = code


@dataclass(frozen=True)
class ArtifactBundle:
    """A validated bundle: everything decided before any disk is touched."""

    kind: ArtifactKind
    name: str
    source_tier: TargetScope | None
    source_wiki_commit: str | None
    exported_at: str
    versions_included: bool
    #: Files whose credential-shaped matches the sender's artifact-wide
    #: ``redaction: documents-patterns`` declaration waived. Disclosure only —
    #: receipt re-scans every entry regardless.
    redaction_exempted: list[str]
    #: ``(posix relpath, bytes, exec)``, sorted by relpath.
    payload: list[tuple[str, bytes, bool]]
    #: Directories with no files beneath them, sorted.
    dirs: list[str]


@dataclass(frozen=True)
class BundleExportResult:
    kind: ArtifactKind
    name: str
    from_scope: TargetScope
    src_path: Path
    out_path: Path
    file_count: int
    versions_included: bool
    source_wiki_commit: str | None
    redaction_exempted: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BundleReceiveResult:
    kind: ArtifactKind
    name: str
    dst_name: str
    to_scope: TargetScope
    dst_project_root: Path | None
    dst_path: Path
    bundle_path: Path
    file_count: int
    received: bool
    needs_sync: bool
    sync_command: str | None
    versions_included: bool
    redaction_exempted: list[str]
    source_tier: TargetScope | None
    source_wiki_commit: str | None
    adopt_hint: str | None = None
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# Path grammar (ADR-0037 §2)
# --------------------------------------------------------------------------


def _validate_component(segment: str, *, where: str) -> None:
    """Refuse one path segment that is outside the portable allowlist.

    An allowlist rather than a denylist because the refusal list has no end —
    Windows device names, the ``? * " < > |`` class, control characters, colon
    segments, trailing spaces, Unicode forms that normalize together — and a
    missed entry lands a file the sender never described. The cost is that a
    non-ASCII filename cannot be bundled, which is refused loudly here rather
    than transliterated (ADR-0037 §"Consequences" and its TRACKER row).
    """
    if not _SEGMENT_RE.fullmatch(segment):
        raise BundleFormatError(
            f"{where}: path segment {segment!r} is outside the portable "
            f"allowlist (ASCII letters, digits, '.', '_', '-'; "
            f"at most {_MAX_SEGMENT_CHARS} characters)"
        )
    if segment.startswith("-"):
        raise BundleFormatError(f"{where}: path segment {segment!r} starts with a hyphen")
    if segment.endswith("."):
        raise BundleFormatError(f"{where}: path segment {segment!r} ends with a dot")
    folded = segment.lower()
    if folded.split(".", 1)[0] in _RESERVED_DEVICE_WORDS:
        raise BundleFormatError(
            f"{where}: path segment {segment!r} is a reserved device name on Windows"
        )
    if folded in _FORBIDDEN_COMPONENTS_FOLDED or folded.endswith(".bak"):
        raise BundleFormatError(
            f"{where}: path segment {segment!r} is a name a conforming export never emits"
        )


def _validate_relpath(rel: object, *, where: str) -> str:
    """Validate one bundle relpath and return it unchanged."""
    if not isinstance(rel, str) or not rel:
        raise BundleFormatError(f"{where}: path must be a non-empty string")
    if len(rel) > _MAX_PATH_CHARS:
        raise BundleFormatError(f"{where}: path exceeds {_MAX_PATH_CHARS} characters")
    if "\\" in rel or "\0" in rel:
        raise BundleFormatError(f"{where}: path contains a backslash or NUL")
    pure = PurePosixPath(rel)
    if pure.is_absolute() or pure.drive or pure.root or pure.as_posix() != rel:
        raise BundleFormatError(f"{where}: path is not a canonical relative POSIX path")
    parts = pure.parts
    if not parts or len(parts) > _MAX_PATH_SEGMENTS:
        raise BundleFormatError(
            f"{where}: path must have between 1 and {_MAX_PATH_SEGMENTS} segments"
        )
    for segment in parts:
        _validate_component(segment, where=where)
    return rel


def _validate_topology(files: list[str], dirs: list[str]) -> None:
    """Refuse duplicate and ancestor collisions over the union of both arrays.

    Folded component-wise first: file ``A`` and file ``a/b`` are distinct as
    written, yet on a case-insensitive filesystem they demand that ``A`` be a
    file and a directory at once. Only directory-to-directory ancestry is
    allowed, so a ``files`` entry may not prefix anything and a ``dirs`` entry
    may not prefix a file.

    IMPLICIT parents are folded too. Checking only the listed paths compared
    ``Docs/a.md`` against ``docs/b.md`` and found no collision, because neither
    ``Docs`` nor ``docs`` is itself an entry — yet materializing creates the
    parents, so on a case-insensitive filesystem both files land in ONE
    directory and the receiver is told the whole listed tree arrived. A Linux
    sender produces that bundle from two genuinely different directories, so it
    is a real bundle, not only a crafted one.
    """
    folded: dict[tuple[str, ...], str] = {}
    parents: dict[tuple[str, ...], str] = {}
    for rel in [*files, *dirs]:
        parts = PurePosixPath(rel).parts
        key = tuple(part.lower() for part in parts)
        if key in folded:
            raise BundleFormatError(f"paths {folded[key]!r} and {rel!r} collide under case folding")
        folded[key] = rel
        for cut in range(1, len(parts)):
            prefix_key = key[:cut]
            spelling = "/".join(parts[:cut])
            seen = parents.setdefault(prefix_key, spelling)
            if seen != spelling:
                raise BundleFormatError(
                    f"directories {seen!r} and {spelling!r} collide under case folding; "
                    f"they would materialize as one directory and merge the files under them"
                )
    dir_keys = {tuple(part.lower() for part in PurePosixPath(rel).parts) for rel in dirs}
    for key, rel in folded.items():
        for cut in range(1, len(key)):
            ancestor = key[:cut]
            if ancestor not in folded:
                continue
            if not (ancestor in dir_keys and key in dir_keys):
                raise BundleFormatError(
                    f"path {folded[ancestor]!r} is an ancestor of {rel!r}; only a "
                    f"directory may contain a directory"
                )


# --------------------------------------------------------------------------
# Structure digest (ADR-0037 §2)
# --------------------------------------------------------------------------


def _structure_digest(
    kind: str,
    name: str,
    entries: list[tuple[str, bool, str]],
    dirs: list[str],
) -> str:
    """Digest binding identity, every path, its content digest, and ``exec``.

    Deliberately not ``skill_payload.payload_digest``, which is the ADR-0030
    content-only tree digest: it would not notice a reordered entry, a changed
    directory set, a flipped executable bit, or a swapped ``kind`` / ``name``,
    and all four change what lands. The newline framing is unambiguous without
    a length prefix because the path grammar forbids a newline in a segment.
    """
    h = hashlib.sha256()
    h.update(f"{BUNDLE_FORMAT}/v{BUNDLE_VERSION}\n".encode())
    h.update(f"i\n{kind}\n{name}\n".encode())
    for rel, is_exec, digest in entries:
        h.update(f"f\n{rel}\n{1 if is_exec else 0}\n{digest}\n".encode())
    for rel in dirs:
        h.update(f"d\n{rel}\n".encode())
    return h.hexdigest()


# --------------------------------------------------------------------------
# Reading a bundle (ADR-0037 §2, §7, §8)
# --------------------------------------------------------------------------


def _decoded_length(blob: str) -> int:
    """Exact decoded byte count from a base64 string, padding included.

    ``len * 3 // 4`` overcounts a padded entry, which would refuse a payload
    that is legally under the cap. Computed BEFORE decoding so an inflated
    bundle is never allocated.
    """
    if len(blob) % 4:
        raise BundleFormatError("content_b64 length is not a multiple of 4")
    return (len(blob) // 4) * 3 - (blob.count("=", -2) if blob.endswith("=") else 0)


def _no_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    seen: set[str] = set()
    for key, _ in pairs:
        if key in seen:
            raise BundleFormatError(f"duplicate JSON key {key!r}")
        seen.add(key)
    return dict(pairs)


def _check_raw_depth(raw: bytes, *, where: str) -> None:
    """Bound container nesting on the RAW text, before ``json.loads`` runs.

    ``json.loads`` recurses per nesting level, so a 200 KB file of ``[`` — far
    under every byte and entry cap — raised ``RecursionError`` from inside the
    parser. That is a ``RuntimeError``: no CLI translator lists it, so it
    escaped as a 41-line traceback, and §7's promise that the bounds are
    "enforced before anything is decoded" was false for this one bound.

    A byte scan is enough to decide it. Only structural brackets count, so the
    scanner tracks string state and escapes; it deliberately does not validate
    anything else, because a malformed document is still the parser's to
    report — this only refuses to hand the parser something that would take it
    past its own limit.
    """
    depth = 0
    in_string = False
    escaped = False
    for byte in raw:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:  # backslash
                escaped = True
            elif byte == 0x22:  # closing quote
                in_string = False
            continue
        if byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):  # [ {
            depth += 1
            if depth > _MAX_JSON_DEPTH:
                raise BundleFormatError(f"{where}: JSON nesting exceeds {_MAX_JSON_DEPTH} levels")
        elif byte in (0x5D, 0x7D):  # ] }
            depth -= 1


def _check_depth(node: object, depth: int = 1) -> None:
    """Container nesting only — a scalar leaf adds nothing (§7).

    Counting scalars would refuse a legal depth-8 object purely for holding a
    string, which is every real bundle's `files` entries.
    """
    if not isinstance(node, (dict, list)):
        return
    if depth > _MAX_JSON_DEPTH:
        raise BundleFormatError(f"JSON nesting exceeds {_MAX_JSON_DEPTH} levels")
    children = node.values() if isinstance(node, dict) else node
    for value in children:
        _check_depth(value, depth + 1)


def _read_capped(path: Path) -> bytes:
    """Read at most the cap plus one byte from a single verified descriptor.

    A ``stat``-then-read pair can be defeated by a file that grows or is
    swapped between the two, and a FIFO reports a small size and then blocks
    forever. ``O_NONBLOCK`` and ``O_NOFOLLOW`` are POSIX-only — on Windows they
    are zero and a reparse point is still followed, the same asymmetry the
    swap-marker reader documents — so the ``fstat`` type check below is what
    runs everywhere.

    ``O_BINARY`` runs the other way: it is a no-op on POSIX and load-bearing on
    Windows, where ``os.open`` defaults to TEXT mode and the C runtime silently
    rewrites ``\\r\\n`` to ``\\n`` on the way in. Reading a bundle through a
    translating descriptor would corrupt every byte the format promises to carry
    verbatim.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise BundleFormatError(f"{path} is a symlink; refusing to read it") from exc
        raise BundleFormatError(f"cannot open {path}: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BundleFormatError(f"{path} is not a regular file")
        chunks: list[bytes] = []
        remaining = _MAX_BUNDLE_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    finally:
        os.close(fd)
    data = b"".join(chunks)
    if len(data) > _MAX_BUNDLE_BYTES:
        raise BundleFormatError(f"{path} exceeds the {_MAX_BUNDLE_BYTES} byte bundle cap")
    return data


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise BundleFormatError(message)


def _snapshot_declared_name(
    kind: ArtifactKind, data: bytes, *, artifact_name: str, where: str
) -> str:
    """The name a frozen snapshot would fan out under, via the REAL parser.

    An earlier revision matched the first literal ``name:`` line here. That is
    the mistake this project has recorded before: a hand-rolled matcher of a
    specified format loses to the format. It read ``name : victim`` as no name
    at all, kept the FIRST of two ``name:`` lines where the parser keeps the
    last, and never stripped quotes — each one a way to declare a name the
    runtime resolves and this check does not see, which is exactly the delayed
    fan-out injection the check exists to stop.

    Parsed as the WORKING MANIFEST it would become, not as ``versions/vN.md``:
    a manifest may legally omit ``name:`` and the parser falls back to the path
    it was handed, so parsing a snapshot by its own path would resolve to
    ``v1`` — nobody's identity. Handing it the artifact's manifest path makes an
    omitted name resolve to the artifact, which is correct: such a snapshot
    carries no name to inject.
    """
    adapter = _SYNC_ADAPTERS[kind]
    try:
        item = adapter.parse_canonical_text(
            data.decode("utf-8", errors="replace"),
            # The path the snapshot WOULD live at once restored, so the
            # omitted-name fallback (the parent directory for a dir layout)
            # resolves to the artifact rather than to an empty string.
            source=Path(artifact_name) / _DIR_MANIFEST[kind],
            layout="dir",
        )
    except adapter.parse_error_type as exc:
        raise BundleSourceError(
            f"{where}: this snapshot does not parse as a {kind[:-1]} manifest ({exc}), so "
            f"what a labeled sync would fan out under cannot be determined."
        ) from exc
    return str(adapter.name_of(item))


def _validate_version_surface(
    kind: ArtifactKind,
    name: str,
    entries: dict[str, bytes],
    dirs: list[str],
    *,
    where: str,
) -> None:
    """Refuse a version surface a receiver could not resolve (ADR-0037 §9).

    Run on BOTH sides — against the source tree on export and against the
    decoded payload on receipt — so a writer can never emit a bundle its own
    reader refuses. This goes beyond what :func:`load_manifest` checks: that
    validates syntax, schema, tag shape and label names, but never that the
    manifest agrees with what is on disk.
    """
    # ``dirs`` counts: an empty ``versions/`` carried as a directory entry is
    # still a version surface, and an orphan empty ``versions/v2/`` is still an
    # orphan. Looking only at files made both invisible.
    #
    # But the two arrays are kept APART. Merging them into one list of names
    # threw away the one fact that distinguishes a snapshot from a directory
    # that merely spells one: a ``dirs`` entry ``versions/v1.md`` then satisfied
    # a file-layout record, and the receiver got a manifest whose snapshot
    # ``resolve_version`` cannot read. A path's type is part of what is being
    # validated here, so it travels with the path.
    manifest_bytes = entries.get("versions.json")
    version_paths: list[tuple[str, bool]] = [
        (rel, True) for rel in entries if rel.split("/", 1)[0] == "versions"
    ] + [(rel, False) for rel in dirs if rel.split("/", 1)[0] == "versions"]

    # Co-presence. Either both halves travel or neither does; one alone is a
    # state no receiver can interpret.
    if manifest_bytes is None:
        if version_paths:
            raise BundleSourceError(
                f"{where}: versions/ is present without versions.json. That is the "
                f"crash state a create leaves behind; inspect it and repair the "
                f"manifest, or export with --no-versions."
            )
        return
    if not version_paths:
        raise BundleSourceError(
            f"{where}: versions.json is present without any versions/ snapshot."
        )

    _check_raw_depth(manifest_bytes, where=f"{where}: versions.json")
    try:
        payload = json.loads(manifest_bytes.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise BundleFormatError(f"{where}: versions.json is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"{where}: versions.json is not valid JSON ({exc.msg})") from exc
    _check_depth(payload)
    if not isinstance(payload, dict):
        raise BundleFormatError(f"{where}: versions.json must be a JSON object")

    schema_version = payload.get("schema_version", 1)
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version not in _KNOWN_VERSION_SCHEMAS
    ):
        raise BundleSourceError(
            f"{where}: versions.json declares schema_version {schema_version!r}, which this "
            f"version of the store cannot read"
        )
    versions = payload.get("versions")
    labels = payload.get("labels", {})
    if not isinstance(versions, dict) or not isinstance(labels, dict):
        raise BundleFormatError(f"{where}: versions.json 'versions'/'labels' must be objects")

    # ``expected`` is typed: a file record demands versions/<tag>.md and a tree
    # record demands versions/<tag>/ with at least one file under it. Comparing
    # only names would accept a regular file where a tree belongs.
    expected_files: set[str] = set()
    expected_trees: set[str] = set()
    for tag, meta in versions.items():
        if not isinstance(meta, dict):
            raise BundleFormatError(
                f"{where}: version {tag!r} metadata is not an object; a scalar would be "
                f"coerced into an empty record"
            )
        # The version store's OWN validators, not a second copy of the rules.
        try:
            _validate_tag(str(tag))
        except VersionError as exc:
            raise BundleFormatError(f"{where}: {exc}") from exc
        layout = meta.get("layout", "file")
        if layout not in ("file", "tree"):
            raise BundleSourceError(f"{where}: version {tag!r} declares unknown layout {layout!r}")
        if layout == "tree":
            if schema_version < 2:
                raise BundleSourceError(
                    f"{where}: version {tag!r} is a tree snapshot but versions.json declares "
                    f"schema_version {schema_version}; the store's own reader refuses that "
                    f"combination"
                )
            if kind != "skills":
                raise BundleSourceError(
                    f"{where}: version {tag!r} is a tree snapshot, which only skills can "
                    f"resolve; this artifact is {kind}."
                )
            expected_trees.add(str(tag))
        else:
            expected_files.add(str(tag))

    actual_files: set[str] = set()
    actual_trees: set[str] = set()
    for rel, is_file in version_paths:
        parts = rel.split("/")
        if len(parts) == 1:
            # A bare ``versions`` entry. As a directory it is the empty version
            # surface the co-presence check above already accounted for; as a
            # FILE it is a regular file standing where the snapshot directory
            # belongs, which no reader can descend.
            if is_file:
                raise BundleSourceError(
                    f"{where}: versions is a file, but the version surface is a directory"
                )
            continue
        if len(parts) == 2 and parts[1].endswith(".md"):
            if not is_file:
                raise BundleSourceError(
                    f"{where}: {rel} is a directory, but a file-layout snapshot must be a "
                    f"regular file; resolve_version cannot read a directory here"
                )
            actual_files.add(parts[1][: -len(".md")])
        elif len(parts) >= 3:
            actual_trees.add(parts[1])
        elif not is_file:
            # ``versions/<tag>/`` carried as an empty directory entry: an
            # orphan tree snapshot, reported as such by the expected/actual
            # comparison below rather than as an unrecognized path.
            actual_trees.add(parts[1])
        else:
            raise BundleSourceError(f"{where}: {rel} is not a recognized snapshot path")
    if actual_files & actual_trees:
        raise BundleSourceError(
            f"{where}: version {sorted(actual_files & actual_trees)[0]!r} has both a file "
            f"and a tree form"
        )
    for label, kinds in (
        ("file", (expected_files, actual_files)),
        ("tree", (expected_trees, actual_trees)),
    ):
        expected, actual = kinds
        missing = sorted(expected - actual)
        if missing:
            raise BundleSourceError(
                f"{where}: versions.json records {label} snapshot(s) "
                f"{', '.join(f'versions/{tag}.md' if label == 'file' else f'versions/{tag}/' for tag in missing)} "
                f"but they are absent"
            )
        orphan = sorted(actual - expected)
        if orphan:
            # Name the path the user has to open, not the tag: for a file
            # record that is ``versions/v2.md``, and telling them ``v2`` makes
            # them work out the spelling before they can act.
            shown = f"versions/{orphan[0]}.md" if label == "file" else f"versions/{orphan[0]}/"
            raise BundleSourceError(
                f"{where}: {shown} is not recorded in versions.json. The version "
                f"store keeps an unreferenced snapshot because it may be the only copy of that "
                f"history — inspect it and repair the manifest, or export with --no-versions."
            )

    for label, tag in labels.items():
        try:
            _validate_label_name(str(label))
        except VersionError as exc:
            raise BundleFormatError(f"{where}: {exc}") from exc
        if str(tag) not in versions:
            raise BundleSourceError(
                f"{where}: label {label!r} targets version {tag!r}, which the manifest does "
                f"not record"
            )
        if str(versions[str(tag)].get("layout", "file")) == "tree":
            # ``promote_label`` refuses to create this and ``resolve_label``
            # refuses to follow it, so carrying one would land a pointer the
            # store considers dead.
            raise BundleSourceError(
                f"{where}: label {label!r} targets tree snapshot {tag!r}, which the label "
                f"path cannot resolve"
            )

    # §8: a frozen agent/command snapshot steers a labeled fan-out through the
    # name inside IT, which the working-manifest rewrite never touches.
    if kind in ("agents", "commands"):
        for rel, data in entries.items():
            if not rel.startswith("versions/") or not rel.endswith(".md"):
                continue
            declared = _snapshot_declared_name(
                kind, data, artifact_name=name, where=f"{where}: {rel}"
            )
            if declared != name:
                raise BundleSourceError(
                    f"{where}: {rel} declares name {declared!r} but the artifact is "
                    f"{name!r}. A labeled sync fans out under the name inside the "
                    f"snapshot, so this would write {declared!r}'s runtime target. Fix the "
                    f"snapshot, or export with --no-versions."
                )


def _validate_artifact_form(
    kind: ArtifactKind,
    name: str,
    entries: dict[str, bytes],
    dirs: list[str],
    *,
    where: str,
) -> None:
    """Refuse a payload the store itself would never have written (§8, §12)."""
    manifest = _DIR_MANIFEST[kind]
    if manifest not in entries:
        raise BundleFormatError(
            f"{where}: no {manifest} at the top level. A {kind} bundle must carry the "
            f"manifest its kind requires — a mismatched 'kind' would promote a tree no "
            f"adapter can parse."
        )
    try:
        entries[manifest].decode("utf-8")
    except UnicodeDecodeError as exc:
        # Refused HERE, which is on both the export and the receipt path, so a
        # non-UTF-8 manifest never becomes a bundle in the first place. It used
        # to export cleanly — the scan reads with ``errors="replace"`` and the
        # bytes travel verbatim — and then die on the receiver with a raw
        # ``UnicodeDecodeError`` from the name rewrite: a codec message with no
        # bundle vocabulary, naming a file the receiver cannot fix.
        raise BundleFormatError(
            f"{where}: {manifest} is not valid UTF-8. The manifest is parsed on arrival "
            f"(its 'name:' is rewritten for the landing), so it must be text — re-save it "
            f"as UTF-8 in the source artifact and export again."
        ) from exc
    other_manifests = {m for k, m in _DIR_MANIFEST.items() if k != kind}
    for wrong in sorted(other_manifests & entries.keys()):
        raise BundleFormatError(f"{where}: carries {wrong} but declares kind {kind!r}")
    allowed_overrides = {
        f"{vendor}.{ext}" for (k, vendor), (_, ext) in OVERRIDE_FORMATS.items() if k == kind
    }
    # Files and directories are classified separately for the same reason
    # :func:`_validate_version_surface` keeps them apart: merged, a DIRECTORY
    # named ``overrides/claude.md`` passed the override-name check and landed
    # where a file belongs, while a legitimately empty ``overrides/`` directory
    # was refused as "not a known override" because it has one path segment.
    for rel, is_file in [(r, True) for r in entries] + [(r, False) for r in dirs]:
        parts = rel.split("/")
        top = parts[0]
        if top == "overrides":
            if len(parts) == 1 and not is_file:
                # The empty overrides directory. Carrying it is how an artifact
                # that has the directory but no vendor file round-trips.
                continue
            if len(parts) != 2 or parts[1] not in allowed_overrides:
                raise BundleFormatError(
                    f"{where}: {rel!r} is not a known override for {kind}; expected one of "
                    f"{sorted(allowed_overrides)}"
                )
            if not is_file:
                raise BundleFormatError(
                    f"{where}: {rel!r} is a directory, but an override is a regular file"
                )
        elif top == "versions.json":
            if rel != "versions.json":
                raise BundleFormatError(
                    f"{where}: {rel!r} uses the reserved versions.json name as a directory"
                )
        elif top == "versions":
            continue
        elif is_internal_artifact_dir(top):
            raise BundleFormatError(f"{where}: {rel!r} has the shape of an internal staging dir")
        elif kind != "skills" and rel != manifest:
            raise BundleFormatError(
                f"{where}: {rel!r} is not part of a {kind} artifact; only {manifest}, "
                f"overrides/ and versions/ travel"
            )
    _ = name


def load_bundle(path: Path) -> ArtifactBundle:
    """Parse and fully validate a bundle file. Touches no store.

    Order is load-bearing (ADR-0037 §6 step 1-3): size, parse, schema, paths,
    topology, decode, digests, artifact form, version surface. Everything is
    decided before any caller can materialize a byte, so a refusal leaves
    nothing behind by construction rather than by cleanup.
    """
    raw = _read_capped(path)
    _check_raw_depth(raw, where=path.name)
    try:
        doc = json.loads(raw.decode("utf-8"), object_pairs_hook=_no_duplicate_keys)
    except UnicodeDecodeError as exc:
        raise BundleFormatError(f"{path.name} is not valid UTF-8") from exc
    except json.JSONDecodeError as exc:
        raise BundleFormatError(f"{path.name} is not valid JSON ({exc.msg})") from exc
    _check_depth(doc)
    if not isinstance(doc, dict):
        raise BundleFormatError(f"{path.name}: top level must be a JSON object")

    fmt = doc.get("format")
    if fmt != BUNDLE_FORMAT:
        if isinstance(doc.get("chunks"), list) or doc.get("total_chunks") is not None:
            raise BundleFormatError(
                f"{path.name} is a memtomem MEMORY export bundle, not a context artifact "
                f"bundle. Import it with the mem_import tool instead."
            )
        raise BundleFormatError(f"{path.name}: 'format' is not {BUNDLE_FORMAT!r}")
    version = doc.get("version")
    if not isinstance(version, int) or isinstance(version, bool) or version != BUNDLE_VERSION:
        raise BundleFormatError(
            f"{path.name}: bundle version {version!r} is not readable by this "
            f"version, which reads {BUNDLE_VERSION}"
        )
    if "provenance" not in doc or doc["provenance"] is not None:
        # Reserved, and reserved means exactly null in v1: accepting a marker
        # shaped like something would let a later reader be handed one this
        # reader never validated (§3).
        raise BundleFormatError(f"{path.name}: 'provenance' must be present and null in v1")

    kind = doc.get("kind")
    if kind not in SCOPE_MIGRATABLE_KINDS:
        raise BundleFormatError(f"{path.name}: 'kind' must be one of {SCOPE_MIGRATABLE_KINDS}")
    try:
        name = validate_name(doc.get("name"), kind=f"{kind[:-1]} name")
    except InvalidNameError as exc:
        raise BundleFormatError(f"{path.name}: {exc}") from exc
    _validate_landing_name(name, where=path.name)

    exported_at = doc.get("exported_at")
    if not isinstance(exported_at, str) or not _ISO_Z_RE.fullmatch(exported_at):
        raise BundleFormatError(f"{path.name}: 'exported_at' must be an RFC 3339 UTC timestamp")
    try:
        datetime.strptime(exported_at.rstrip("Z").split(".")[0], "%Y-%m-%dT%H:%M:%S")
    except ValueError as exc:
        # The shape regex accepts 2026-99-99T99:99:99Z; only a real parse
        # rejects it.
        raise BundleFormatError(f"{path.name}: 'exported_at' is not a real instant") from exc
    versions_included = doc.get("versions_included")
    if not isinstance(versions_included, bool):
        raise BundleFormatError(f"{path.name}: 'versions_included' must be a boolean")

    source = doc.get("source")
    if not isinstance(source, dict) or set(source) != {"tier", "wiki_commit"}:
        raise BundleFormatError(
            f"{path.name}: 'source' must be an object with exactly 'tier' and 'wiki_commit'"
        )
    source_tier = source["tier"]
    if source_tier not in ("user", "project_shared", "project_local"):
        raise BundleFormatError(f"{path.name}: 'source.tier' is not a known tier")
    wiki_commit = source["wiki_commit"]
    if wiki_commit is not None and not (
        isinstance(wiki_commit, str) and _HEX40_RE.fullmatch(wiki_commit)
    ):
        # Informational, so a malformed value is refused rather than normalized
        # away: silently blanking it would hide that the sender wrote garbage.
        raise BundleFormatError(
            f"{path.name}: 'source.wiki_commit' must be 40 lowercase hex characters or null"
        )

    raw_files = doc.get("files")
    raw_dirs = doc.get("dirs")
    if not isinstance(raw_files, list) or not isinstance(raw_dirs, list):
        raise BundleFormatError(f"{path.name}: 'files' and 'dirs' must be arrays")
    if len(raw_files) > _MAX_FILE_ENTRIES or len(raw_dirs) > _MAX_DIR_ENTRIES:
        raise BundleFormatError(
            f"{path.name}: too many entries (cap {_MAX_FILE_ENTRIES} files, "
            f"{_MAX_DIR_ENTRIES} dirs)"
        )

    dirs: list[str] = [
        _validate_relpath(rel, where=f"{path.name} dirs[{i}]") for i, rel in enumerate(raw_dirs)
    ]
    if dirs != sorted(dirs):
        raise BundleFormatError(f"{path.name}: 'dirs' is not sorted")

    file_paths: list[str] = []
    encoded: list[tuple[str, bool, str, str]] = []
    total_estimate = 0
    for i, entry in enumerate(raw_files):
        where = f"{path.name} files[{i}]"
        if not isinstance(entry, dict):
            raise BundleFormatError(f"{where}: entry must be an object")
        unknown = set(entry) - {"path", "exec", "sha256", "content_b64"}
        if unknown:
            raise BundleFormatError(
                f"{where}: unknown key(s) {sorted(unknown)}. An entry is the "
                f"security-relevant unit, so an unrecognized field is refused rather than "
                f"ignored."
            )
        rel = _validate_relpath(entry.get("path"), where=where)
        is_exec = entry.get("exec")
        if not isinstance(is_exec, bool):
            raise BundleFormatError(f"{where}: 'exec' must be a boolean")
        digest = entry.get("sha256")
        if not isinstance(digest, str) or not _HEX64_RE.fullmatch(digest):
            raise BundleFormatError(f"{where}: 'sha256' must be 64 lowercase hex characters")
        blob = entry.get("content_b64")
        if not isinstance(blob, str):
            raise BundleFormatError(f"{where}: 'content_b64' must be a string")
        entry_bytes = _decoded_length(blob)
        if entry_bytes > _MAX_ENTRY_DECODED_BYTES:
            raise BundleFormatError(
                f"{where}: entry exceeds the {_MAX_ENTRY_DECODED_BYTES} byte cap"
            )
        total_estimate += entry_bytes
        if total_estimate > _MAX_DECODED_BYTES:
            raise BundleFormatError(
                f"{path.name}: decoded payload exceeds {_MAX_DECODED_BYTES} bytes"
            )
        file_paths.append(rel)
        encoded.append((rel, is_exec, digest, blob))
    if file_paths != sorted(file_paths):
        raise BundleFormatError(f"{path.name}: 'files' is not sorted by path")
    _validate_topology(file_paths, dirs)

    payload: list[tuple[str, bytes, bool]] = []
    entries: dict[str, bytes] = {}
    for rel, is_exec, digest, blob in encoded:
        try:
            data = base64.b64decode(blob, validate=True)
        except (ValueError, TypeError) as exc:
            raise BundleFormatError(f"{path.name}: {rel!r} content is not valid base64") from exc
        if hashlib.sha256(data).hexdigest() != digest:
            raise BundleIntegrityError(f"{path.name}: {rel!r} does not match its recorded digest")
        payload.append((rel, data, is_exec))
        entries[rel] = data

    claimed = doc.get("payload_sha256")
    actual = _structure_digest(kind, name, [(r, e, d) for r, e, d, _ in encoded], dirs)
    if not isinstance(claimed, str) or claimed != actual:
        raise BundleIntegrityError(
            f"{path.name}: payload_sha256 does not match the bundle's own structure"
        )

    _validate_artifact_form(kind, name, entries, dirs, where=path.name)
    has_versions = any(rel.split("/", 1)[0] == "versions" for rel in entries) or (
        "versions.json" in entries
    )
    if has_versions != versions_included:
        raise BundleFormatError(
            f"{path.name}: 'versions_included' is {versions_included} but the payload "
            f"{'carries' if has_versions else 'carries no'} version data"
        )
    _validate_version_surface(kind, name, entries, dirs, where=path.name)
    raw_exempted = doc.get("redaction_exempted", [])
    if not isinstance(raw_exempted, list) or not all(isinstance(r, str) for r in raw_exempted):
        raise BundleFormatError(f"{path.name}: 'redaction_exempted' must be an array of strings")
    return ArtifactBundle(
        kind=kind,
        name=name,
        redaction_exempted=sorted(raw_exempted),
        source_tier=source_tier,
        source_wiki_commit=wiki_commit,
        exported_at=exported_at,
        versions_included=versions_included,
        payload=payload,
        dirs=dirs,
    )


def _validate_landing_name(name: str, *, where: str) -> None:
    """Portability and internal-shape rules for an artifact or ``--as`` name.

    The name becomes a directory in the store, so it needs the same rules a
    path segment does; and an internal-shaped name would land an artifact every
    discovery walk skips — invisible to status and unusable as a skill — which
    the name validator accepts today.
    """
    if is_internal_artifact_dir(name):
        raise BundleFormatError(
            f"{where}: {name!r} has the shape of an internal staging or move-aside "
            f"directory, which every discovery walk skips"
        )
    _validate_component(name, where=where)


# --------------------------------------------------------------------------
# Export (ADR-0037 §4)
# --------------------------------------------------------------------------


def _read_source_file(
    path: Path | str,
    rel: str,
    *,
    budget: int,
    dir_fd: int | None = None,
) -> tuple[bytes, bool]:
    """Read one source file through a verified descriptor.

    Walking with ``lstat`` and then reading by path would let an external
    writer swap a vetted regular file for a symlink or a FIFO between the two —
    escaping the artifact or hanging the export while it holds the canonical
    lock — and would let ``exec`` come from a different inode than the content.
    Both come from this descriptor.

    *budget* is the largest payload this file may still contribute. Reading is
    stopped at ``budget + 1`` bytes, which is enough for the caller to refuse
    and no more: enforcing the cap only after the whole file is in memory made
    the promised refusal reachable solely by first allocating the thing the cap
    exists to prevent.

    When *dir_fd* is given, *path* is a single component resolved relative to
    it. That is what closes the directory half of the swap window: the walk's
    type check and this open then name the same inode by construction, rather
    than agreeing only because nothing moved in between.

    ``O_BINARY`` is a no-op on POSIX and required on Windows, where ``os.open``
    defaults to TEXT mode: without it the C runtime strips the ``\\r`` from every
    ``\\r\\n`` on the way in, so a Windows export packed bytes that were not the
    bytes on disk. That silently broke the byte-identical round trip for any
    CRLF text file and would have mangled a binary asset containing ``0D 0A``,
    while the digests still verified — they were computed over the already
    translated bytes.
    """
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NONBLOCK", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        fd = os.open(path, flags) if dir_fd is None else os.open(path, flags, dir_fd=dir_fd)
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.EMLINK):
            raise BundleSourceError(f"{rel} is a symlink; refusing to read it") from exc
        raise BundleSourceError(f"cannot read {rel}: {exc.strerror}") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise BundleSourceError(f"{rel} is not a regular file")
        chunks: list[bytes] = []
        remaining = budget + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 1 << 20))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        raise BundleSourceError(f"cannot read {rel}: {exc.strerror}") from exc
    finally:
        os.close(fd)
    is_exec = bool(st.st_mode & 0o111) and os.name != "nt"
    return b"".join(chunks), is_exec


#: Whether this platform can enumerate and open relative to a directory
#: descriptor. POSIX can; Windows exposes neither, and there the walk falls
#: back to path-based traversal with the same rules but without the inode
#: guarantee — the same asymmetry ``_read_capped`` documents for
#: ``O_NOFOLLOW``.
_HAS_DIR_FD = os.scandir in os.supports_fd and os.open in os.supports_dir_fd

_DIR_OPEN_FLAGS = (
    os.O_RDONLY
    | getattr(os, "O_DIRECTORY", 0)
    | getattr(os, "O_NOFOLLOW", 0)
    | getattr(os, "O_NONBLOCK", 0)
)


def _classify_entry(entry: os.DirEntry[str] | Path) -> tuple[bool, bool, bool]:
    """``(is_symlink, is_dir, is_file)`` for either walk flavour.

    The two enumerations answer the same three questions with different
    spellings — ``os.DirEntry`` needs ``follow_symlinks=False`` where ``Path``
    has no such parameter at all — so asking them through one function keeps
    the walk body free of a platform branch, and keeps a ``Path`` from ever
    being handed a keyword that would raise at runtime.
    """
    if isinstance(entry, Path):
        return entry.is_symlink(), entry.is_dir(), entry.is_file()
    return (
        entry.is_symlink(),
        entry.is_dir(follow_symlinks=False),
        entry.is_file(follow_symlinks=False),
    )


def _open_dir_at(name: str, rel: str, *, dir_fd: int | None) -> int:
    """Open one directory component, refusing a symlink at open time.

    ``O_DIRECTORY`` and ``O_NOFOLLOW`` together are what make this a check that
    cannot be raced: the kernel resolves the name and enforces both in the same
    operation, where an ``is_dir()`` test followed by a separate open leaves a
    window in which the name can be repointed.

    The two flags report the same refusal differently across platforms — Linux
    raises ``ELOOP`` for a symlinked directory, macOS raises ``ENOTDIR`` — so
    the *reason* is recovered from an ``lstat`` of the same name rather than
    from the errno alone. A caller told "not a directory" about a symlink would
    go looking for the wrong problem.
    """
    try:
        if dir_fd is None:
            return os.open(name, _DIR_OPEN_FLAGS)
        return os.open(name, _DIR_OPEN_FLAGS, dir_fd=dir_fd)
    except OSError as exc:
        symlink = False
        if exc.errno in (errno.ELOOP, errno.EMLINK, errno.ENOTDIR):
            try:
                probe = os.lstat(name) if dir_fd is None else os.lstat(name, dir_fd=dir_fd)
                symlink = stat.S_ISLNK(probe.st_mode)
            except OSError:
                symlink = exc.errno in (errno.ELOOP, errno.EMLINK)
        if symlink:
            raise BundleSourceError(
                f"{rel or 'the artifact root'} is a symlink. A bundle carries the "
                f"canonical tree itself, not whatever a link points at."
            ) from exc
        if exc.errno == errno.ENOTDIR:
            raise BundleSourceError(f"{rel or 'the artifact root'} is not a directory") from exc
        raise BundleSourceError(f"cannot enumerate {rel or '.'}: {exc.strerror}") from exc


class _SourceReader:
    """Reads artifact files through descriptors anchored at the artifact root.

    The export walk checks each entry's type and then, in a second pass, reads
    the files it accepted. Doing that second pass by path is what left the
    directory half of the swap window open: ``O_NOFOLLOW`` on the final
    component cannot see that an ancestor turned into a symlink after the walk
    vetted it, so the bytes read could come from outside the artifact. This
    re-descends from the root descriptor instead, opening every component with
    ``O_NOFOLLOW`` so any component that changed type is refused rather than
    followed.

    On a platform without ``dir_fd`` support this degrades to path-based reads,
    which is what the module did everywhere before.
    """

    def __init__(self, root: Path) -> None:
        self._root = root
        self._fd: int | None = None

    def __enter__(self) -> _SourceReader:
        if _HAS_DIR_FD:
            self._fd = _open_dir_at(str(self._root), "", dir_fd=None)
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def read(self, rel: str, *, budget: int) -> tuple[bytes, bool]:
        if self._fd is None:
            return _read_source_file(self._root / rel, rel, budget=budget)
        parts = rel.split("/")
        opened: list[int] = []
        try:
            cursor = self._fd
            for i, part in enumerate(parts[:-1]):
                cursor = _open_dir_at(part, "/".join(parts[: i + 1]), dir_fd=cursor)
                opened.append(cursor)
            return _read_source_file(parts[-1], rel, budget=budget, dir_fd=cursor)
        finally:
            for fd in reversed(opened):
                os.close(fd)


def _walk_source(root: Path, *, include_versions: bool) -> tuple[list[str], list[str], list[str]]:
    """Enumerate the artifact strictly. Returns ``(relpaths, empty_dirs, notes)``.

    Fail-closed: an unreadable entry aborts rather than shrinking the payload.
    Exclusion is decided BEFORE the type check, so an excluded name that
    happens to be a symlink is skipped as excluded rather than refused; only a
    link at a path the bundle would otherwise carry is an error.

    Returns relative paths rather than :class:`Path` objects on purpose. A
    resolved path invites the caller to read it later by name, which is the
    race :class:`_SourceReader` exists to close; the relpath is the only thing
    a caller needs, and it can only be used through the reader.
    """
    files: list[str] = []
    empty_dirs: list[str] = []
    notes: list[str] = []

    def walk(dir_fd: int | None, current: Path, prefix: str) -> bool:
        """Returns True when this directory contributed at least one file."""
        if dir_fd is None and current.is_symlink():
            # Path-based fallback only; the descriptor walk refuses a symlinked
            # component at open time. A symlinked root reads an out-of-store
            # tree, so the bundle would carry bytes the canonical store never
            # held.
            raise BundleSourceError(
                f"{prefix or 'the artifact root'} is a symlink. A bundle carries the "
                f"canonical tree itself, not whatever a link points at."
            )
        names: list[tuple[str, os.DirEntry[str] | Path]] = []
        try:
            if dir_fd is None:
                names = [(p.name, p) for p in sorted(current.iterdir(), key=lambda p: p.name)]
            else:
                with os.scandir(dir_fd) as it:
                    names = sorted(((e.name, e) for e in it), key=lambda item: item[0])
        except OSError as exc:
            raise BundleSourceError(f"cannot enumerate {prefix or '.'}: {exc.strerror}") from exc
        contributed = False
        for name, entry in names:
            rel = f"{prefix}{name}"
            if name in COPY_SKIP_NAMES or PurePosixPath(name).suffix in DIRTY_SKIP_SUFFIXES:
                notes.append(f"excluded {rel} (not carried by a bundle)")
                continue
            if is_internal_artifact_dir(name):
                notes.append(f"excluded {rel} (crash leftover)")
                continue
            if not include_versions and prefix == "" and name in ("versions", "versions.json"):
                continue
            is_symlink, is_dir, is_file = _classify_entry(entry)
            if is_symlink:
                raise BundleSourceError(
                    f"{rel} is a symlink. A bundle carries regular files only, and skipping "
                    f"it silently would hand the receiver a tree you believe is complete — "
                    f"replace it with its contents or remove it."
                )
            if is_dir:
                if dir_fd is None:
                    child_fd = None
                    child_path = current / name
                else:
                    child_fd = _open_dir_at(name, rel, dir_fd=dir_fd)
                    child_path = current / name
                try:
                    contributed_here = walk(child_fd, child_path, f"{rel}/")
                finally:
                    if child_fd is not None:
                        os.close(child_fd)
                if contributed_here:
                    contributed = True
                else:
                    empty_dirs.append(rel)
            elif is_file:
                files.append(rel)
                contributed = True
            else:
                raise BundleSourceError(f"{rel} is not a regular file or directory")
        return contributed

    if _HAS_DIR_FD:
        root_fd = _open_dir_at(str(root), "", dir_fd=None)
        try:
            walk(root_fd, root, "")
        finally:
            os.close(root_fd)
    else:
        walk(None, root, "")
    return files, empty_dirs, notes


def _artifact_exemption(kind: ArtifactKind, captured: dict[str, bytes]) -> str | None:
    """The artifact's ``redaction: documents-patterns`` declaration, if any.

    Derived from the manifest bytes ALREADY CAPTURED for this export, never a
    second read: a declaration present on a first read and gone by the time the
    content is captured would export bytes that no longer carry the claim the
    receiver is told about.

    Read once from the manifest and applied to every file in the artifact,
    rather than per file as the indexing path does. An artifact is one unit the
    author writes and the receiver reads as a whole, and the per-file reader
    only recognizes the declaration in Markdown frontmatter — so a skill whose
    ``SKILL.md`` documents credential shapes could declare it while the
    ``scripts/*.py`` showing the same shapes could not, which refuses the
    artifact for a distinction its author cannot act on.

    The captured bytes are verbatim — the reads keep ``O_BINARY``, so what is
    packed is what was read, and receipt writes those bytes through unchanged
    except for the ``name:`` line that ``--as`` rewrites — so they are decoded
    through ``indexer_text`` for the declaration read, giving the reader the
    same newline-translated view the indexer has of that file on disk. Without
    it a CRLF-authored manifest
    declares nothing here while declaring fine through ``mm index``, and export
    refuses the artifact while advising the declaration it already carries
    (#2310). Receipt decodes the same way, so the two ends cannot disagree.
    """
    data = captured.get(_DIR_MANIFEST[kind])
    if data is None:
        return None
    return declared_exemption(Path(_DIR_MANIFEST[kind]), indexer_text(data, errors="replace"))


def export_artifact_bundle(
    kind: ArtifactKind,
    name: str,
    *,
    src_project_root: Path | None,
    from_scope: TargetScope | None,
    out_path: Path,
    include_versions: bool = True,
    surface: str = "cli_context_export",
) -> BundleExportResult:
    """Pack one canonical artifact into a bundle file at *out_path*.

    Gate A runs from EVERY source tier and has no force valve (ADR-0037 §4): a
    file handed to someone else is as unretractable as a pushed commit, so this
    follows the wiki-promote precedent rather than the transfer one. The scan
    itself runs at :data:`_EGRESS_SCAN_SCOPE`, whose docstring explains why that
    is ``user`` and not ``project_shared`` — the no-valve promise is kept by
    never passing ``force_unsafe``, not by the scope label. Only secret-bearing
    artifacts are refused; a clean artifact exports from any tier.
    """
    if kind not in SCOPE_MIGRATABLE_KINDS:
        raise click.ClickException(f"unsupported artifact kind: {kind}")
    validate_name(name, kind=f"{kind[:-1]} name")
    # The receiver's own rules, applied here: a bundle this exporter can write
    # but its own reader refuses is a defect the sender discovers on someone
    # else's machine.
    _validate_landing_name(name, where=f"{kind}/{name}")
    src_scope, src_path, layout = _detect_source_scope(kind, name, src_project_root, from_scope)
    src_store = canonical_artifact_dir(kind, src_scope, src_project_root)

    out_path = out_path.expanduser()
    resolved_out = out_path.resolve() if out_path.parent.exists() else out_path.absolute()
    src_resolved = src_path.resolve()
    if resolved_out == src_resolved or src_resolved in resolved_out.parents:
        raise BundleSourceError(
            f"--out {out_path} is inside the artifact being exported; the bundle would "
            f"overwrite the bytes it just captured"
        )

    notes: list[str] = []
    with canonical_sidecar_lock(src_store, name):
        try:
            run_swap_prelude(src_store, name, kind=kind)
        except SwapRecoveryError as exc:
            raise TransferRecoveryError(swap_failure_text(exc)) from exc
        if layout == "dir":
            if not (src_path / _DIR_MANIFEST[kind]).is_file():
                raise ArtifactNotFoundError(
                    f"{kind}/{name} is no longer a complete artifact "
                    "(it disappeared, or an interrupted transaction left it incomplete)."
                )
            walked, empty_dirs, walk_notes = _walk_source(
                src_path, include_versions=include_versions
            )
            notes.extend(walk_notes)
        else:
            if not src_path.is_file():
                raise ArtifactNotFoundError(f"{kind}/{name} is no longer present.")
            walked, empty_dirs = [_DIR_MANIFEST[kind]], []
            notes.append(
                f"flat-layout source packed as {_DIR_MANIFEST[kind]} (bundles are always "
                f"directory layout)"
            )

        if not walked:
            raise BundleSourceError(f"{kind}/{name} has no regular files to export")

        # Counts first, before a single byte is read: discovering the cap
        # after loading 4096 files into memory is how an exporter publishes a
        # bundle its own reader refuses, having done all the work twice.
        if len(walked) > _MAX_FILE_ENTRIES:
            raise BundleSourceError(
                f"{kind}/{name} has {len(walked)} files; the format caps a bundle at "
                f"{_MAX_FILE_ENTRIES}"
            )
        if len(empty_dirs) > _MAX_DIR_ENTRIES:
            raise BundleSourceError(
                f"{kind}/{name} has {len(empty_dirs)} empty directories; the format caps a "
                f"bundle at {_MAX_DIR_ENTRIES}"
            )

        # Capture every file first, so the declaration and the scan and the
        # encoded payload all read ONE set of bytes (the promote_asset rule).
        # Each read is capped at what the caps still allow PLUS ONE byte, which
        # is exactly enough to detect the overrun: a file that would blow the
        # budget is refused without ever being held in memory whole.
        captured: dict[str, tuple[bytes, bool]] = {}
        running = 0
        with ExitStack() as stack:
            # A flat source is a single file with no directory to descend, so
            # it is read directly; only the directory layout needs the anchored
            # reader.
            reader = stack.enter_context(_SourceReader(src_path)) if layout == "dir" else None
            for rel in sorted(walked):
                _validate_relpath(rel, where=f"{kind}/{name}")
                budget = min(_MAX_ENTRY_DECODED_BYTES, _MAX_DECODED_BYTES - running)
                if reader is not None:
                    data, is_exec = reader.read(rel, budget=budget)
                else:
                    data, is_exec = _read_source_file(src_path, rel, budget=budget)
                running += len(data)
                if len(data) > _MAX_ENTRY_DECODED_BYTES or running > _MAX_DECODED_BYTES:
                    raise BundleSourceError(
                        f"{kind}/{name} exceeds the bundle payload caps "
                        f"({_MAX_ENTRY_DECODED_BYTES} bytes per file, "
                        f"{_MAX_DECODED_BYTES} total)"
                    )
                captured[rel] = (data, is_exec)

        entries: list[tuple[str, bool, str, str]] = []
        decoded: dict[str, bytes] = {}
        exempted_files: list[str] = []
        artifact_exemption = _artifact_exemption(
            kind, {rel: data for rel, (data, _) in captured.items()}
        )
        for rel, (data, is_exec) in captured.items():
            # Display only — the bytes were already read through a verified
            # descriptor, and nothing re-opens this path.
            file_path = src_path / rel if layout == "dir" else src_path
            # errors="replace" so non-UTF8 bytes cannot mask an embedded ASCII
            # secret from the scanner (the wiki-promote rule).
            text = data.decode("utf-8", errors="replace")
            scan = scan_text_content(
                text,
                source_path=file_path,
                surface=surface,
                scope=_EGRESS_SCAN_SCOPE,
                project_root=src_project_root,
                declared_exemption=artifact_exemption,
            )
            if scan.decision == "exempted":
                # Disclosure, because the declaration is artifact-wide: it is
                # written in the manifest but waives hits in files the author
                # may not have re-read, and the waived class includes a plain
                # ``password=<value>``. Naming every file here — and in the
                # bundle's own notes, so the RECEIVER sees the same list —
                # turns a silent widening into one both sides can audit.
                exempted_files.append(rel)
            if scan.decision in ("blocked", "blocked_project_shared"):
                # Export raises its OWN refusal rather than reusing
                # ``raise_or_collect``: that helper only raises for a
                # ``project_shared`` scope, and telling it "project_shared"
                # here would print a git-history rejection that did not happen
                # (the scan runs at ``_EGRESS_SCAN_SCOPE``). The condition and
                # the ceiling differ from every in-machine write, so the
                # sentence does too.
                raise PrivacyBlockedError(
                    f"Gate A: {rel} contains {scan.hits_count} privacy pattern hit(s); "
                    f"packing it into a bundle is refused. A bundle leaves this machine "
                    f"and cannot be retracted, so export has no force valve. Remove the "
                    f"secret, or — if this file documents credential shapes rather than "
                    f"carrying one — declare `redaction: documents-patterns` in its "
                    f"frontmatter."
                    + (
                        " Frozen history can also be dropped with --no-versions."
                        if rel.startswith("versions/")
                        else ""
                    ),
                    blocked=scan,
                    scope=_EGRESS_SCAN_SCOPE,
                    kind=kind[:-1],
                    artifact_name=name,
                )
            entries.append((rel, is_exec, hashlib.sha256(data).hexdigest(), ""))
            decoded[rel] = data

        empty_dirs = sorted(_validate_relpath(d, where=f"{kind}/{name}") for d in empty_dirs)
        _validate_topology([rel for rel, _, _, _ in entries], empty_dirs)
        _validate_artifact_form(kind, name, decoded, empty_dirs, where=f"{kind}/{name}")
        _validate_version_surface(kind, name, decoded, empty_dirs, where=f"{kind}/{name}")

        wiki_commit: str | None = None
        if src_scope == "project_shared" and src_project_root is not None:
            plan = _classify_provenance_carry(kind, name, src_project_root, renamed=False)
            if plan is not None and plan.carry:
                wiki_commit = plan.wiki_commit

    digest_entries = [(rel, is_exec, digest) for rel, is_exec, digest, _ in entries]
    document = {
        "format": BUNDLE_FORMAT,
        "version": BUNDLE_VERSION,
        "exported_at": _utcnow_z(),
        "kind": kind,
        "name": name,
        "source": {"tier": src_scope, "wiki_commit": wiki_commit},
        "versions_included": any(
            rel == "versions.json" or rel.startswith("versions/") for rel in decoded
        ),
        "payload_sha256": _structure_digest(kind, name, digest_entries, empty_dirs),
        "provenance": None,
        "dirs": empty_dirs,
        "files": [
            {
                "path": rel,
                "exec": is_exec,
                "sha256": digest,
                "content_b64": base64.b64encode(decoded[rel]).decode("ascii"),
            }
            for rel, is_exec, digest in digest_entries
        ],
    }
    if exempted_files:
        # The field is the carrier — it travels to the receiver and each surface
        # renders it in its own voice. A ``notes`` entry saying the same thing
        # would print twice on the exporting terminal.
        document["redaction_exempted"] = sorted(exempted_files)
    _publish_bundle(out_path, json.dumps(document, indent=2, sort_keys=True).encode("utf-8"))
    return BundleExportResult(
        kind=kind,
        name=name,
        from_scope=src_scope,
        src_path=src_path,
        out_path=out_path,
        file_count=len(digest_entries),
        versions_included=bool(document["versions_included"]),
        source_wiki_commit=wiki_commit,
        redaction_exempted=sorted(exempted_files),
        notes=notes,
    )


def _utcnow_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _publish_bundle(out_path: Path, data: bytes) -> None:
    """Write to a sibling temp and publish with a no-replace rename.

    Creating the destination directly would leave a partial, retry-blocking
    file after ``ENOSPC`` or an interrupt, and would need a separate exclusive
    -create dance to avoid clobbering. This gets both at once: export never
    overwrites an existing destination and never follows a symlink to one.
    """
    parent = out_path.parent
    if not parent.is_dir():
        raise BundleSourceError(f"--out directory does not exist: {parent}")
    tmp = parent / f".{out_path.name}.{os.getpid()}-{secrets.token_hex(4)}.tmp"
    atomic_write_bytes(tmp, data, mode=0o600)
    try:
        rename_no_replace(tmp, out_path)
    except BaseException as exc:
        # BaseException, not OSError: an interrupt between the write and the
        # rename would otherwise leave a sibling temp file that blocks nothing
        # but confuses the next reader of that directory. Cleanup is
        # best-effort and must never replace the exception that got us here —
        # a failed unlink reported instead of the original KeyboardInterrupt
        # would hide why the export stopped.
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            logger.warning("could not remove temporary bundle file %s", tmp)
        if not isinstance(exc, OSError):
            raise
        if exc.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR):
            raise BundleSourceError(
                f"{out_path} already exists; export never overwrites (choose another --out)"
            ) from exc
        raise BundleSourceError(f"cannot write {out_path}: {exc.strerror}") from exc


# --------------------------------------------------------------------------
# Receipt (ADR-0037 §5, §6, §9)
# --------------------------------------------------------------------------


def _staging_path(dst_store: Path, dst_name: str) -> Path:
    """A staging directory discovery skips, sharing the destination's parent.

    The name matches the central internal-artifact predicate, so every
    canonical lister, runtime scan and status walk skips it. It must share the
    destination's parent: the native no-replace rename refuses a cross-parent
    promote with ``EXDEV`` by design.

    Being skipped by discovery is only half a contract — something has to
    delete these. The skills reaper does it for skills and *only* for skills
    (``run_swap_prelude`` returns immediately for any other kind), while this
    transport stages for every migratable kind, so an agents or commands
    leftover was hidden from every lister and reaped by nothing: the
    "invisible to discovery and immortal on disk" state ``_names`` warns
    about. :func:`_reap_own_staging` is the other half, and it runs for every
    kind under the destination's own lock.
    """
    return dst_store / f".staging-{dst_name}-{os.getpid()}-{secrets.token_hex(3)}.tmp"


def _reap_own_staging(dst_store: Path, dst_name: str) -> None:
    """Delete leftover staging trees belonging to *dst_name*, under its lock.

    Two independent conditions, and both are load-bearing.

    **The kind must be reapable.** Classified and deletable are different
    questions (#2304): the predicate hides every internal transient, but
    ``.migrate-*`` is deliberately absent from
    :data:`REAPABLE_INTERNAL_ARTIFACT_KINDS` because ``migrate._stage_move``
    renames the source into staging on the same filesystem, so between that
    rename and the promote that tree is the ONLY copy of the artifact.
    Reaping by classification would turn another engine's recoverable crash
    into data loss. Receipt only ever creates ``.staging-*`` itself, so
    restricting to the reapable subset costs it nothing.

    **The owner must match**, decided by :func:`internal_artifact_owner` and
    never by a ``.staging-<name>-*`` glob: that prefix match would make a
    reaper holding only ``foo``'s lock delete ``foo-bar``'s in-flight tree, and
    hyphenated artifact names are the norm.

    Best-effort by design. A leftover that cannot be removed must not turn a
    working import into a failure — the caller stages under a fresh random
    name regardless, so the worst case is the disk residue we already had.
    """
    if not dst_store.is_dir():
        return
    try:
        entries = list(dst_store.iterdir())
    except OSError as exc:
        logger.warning("could not scan %s for staging leftovers: %s", dst_store, exc)
        return
    reapable = tuple(f".{kind}-" for kind in REAPABLE_INTERNAL_ARTIFACT_KINDS)
    for entry in entries:
        if not entry.name.startswith(reapable):
            continue
        if internal_artifact_owner(entry.name) != dst_name:
            continue
        logger.info("removing staging leftover %s", entry)
        _remove_staging(entry)


def receive_artifact_bundle(
    bundle_path: Path,
    *,
    dst_project_root: Path | None,
    to_scope: TargetScope,
    apply_: bool,
    surface: str = "cli_context_import",
    new_name: str | None = None,
    force_unsafe: bool = False,
    lock_timeout: float | None = None,
    pre_materialize: Callable[[], None] | None = None,
) -> BundleReceiveResult:
    """Validate a bundle and land it in a canonical store.

    A bundle is foreign by definition, so Gate A runs for EVERY destination
    tier (ADR-0037 §5): ``project_shared`` hard-refuses with no valve, while
    ``user`` / ``project_local`` honour *force_unsafe* — the
    ``mm context init --force-unsafe-import`` valve. Everything decidable is
    decided in memory first, so the bytes scanned are the bytes that land and a
    refusal leaves zero residue under the destination store.

    *pre_materialize* runs once every in-memory gate has passed and this call
    is definitely going to write, just before the destination lock is taken. It
    is the seam for destination-side preparation that must not happen for a
    refused import — establishing the ``project_local`` gitignore marker, in
    the CLI's case. Doing that work before the gates meant a malformed,
    privacy-blocked or colliding bundle still modified the destination project;
    doing it after the write would leave a window where the bytes are present
    and unignored. It may raise, and its exception propagates unchanged: a
    destination that cannot be prepared is not a destination.
    """
    bundle = load_bundle(bundle_path)
    kind = bundle.kind
    dst_name = bundle.name
    if new_name is not None:
        dst_name = validate_name(new_name, kind=f"{kind[:-1]} name")
        _validate_landing_name(dst_name, where="--as")
    # Every rename consequence below keys on the name actually DIFFERING, not
    # on the flag being present. ``--as <the-name-it-already-has>`` is a no-op a
    # scripted import passes routinely, and keying on the flag made it refuse a
    # rename it was not performing, print an overrides warning about a rewrite
    # that never happened, and suppress the adopt hint. The CLI already reports
    # "renamed from" on this same predicate.
    renamed = dst_name != bundle.name
    if renamed and bundle.versions_included and kind in ("agents", "commands"):
        raise BundleFormatError(
            f"cannot rename a {kind[:-1]} bundle that carries version history: a labeled "
            f"sync resolves a frozen snapshot and fans out under the name inside it, so "
            f"the renamed copy would write {bundle.name!r}'s runtime target. Ask the "
            f"sender to re-export with --no-versions."
        )

    dst_store = canonical_artifact_dir(kind, to_scope, dst_project_root)
    dst_path = dst_store / dst_name
    needs_sync, sync_command = _sync_followup(to_scope, dst_project_root)
    notes: list[str] = []
    if any(rel.startswith("overrides/") for rel, _, _ in bundle.payload) and renamed:
        notes.append(
            "overrides/ travel verbatim and were not rewritten for the new name — review them"
        )

    adopt_hint: str | None = None
    if to_scope == "project_shared" and not renamed and bundle.source_wiki_commit:
        adopt_hint = f"mm context adopt {kind[:-1]} {dst_name}"

    # Rewrite the manifest name in memory BEFORE the scan, so the scan sees the
    # final bytes and no later step changes one (ADR-0037 §6 step 4).
    #
    # Unconditional, INCLUDING when nothing is being renamed: the fan-out keys
    # on the parsed ``name:``, not on the directory, so landing a manifest that
    # disagrees with its own path would write another artifact's runtime target
    # — the same hazard the version-history refusal above describes. The rewrite
    # is a no-op when the manifest already agrees, which is the common case.
    manifest_rel = _DIR_MANIFEST[kind]
    payload: list[tuple[str, bytes, bool]] = []
    for rel, data, is_exec in bundle.payload:
        if rel == manifest_rel:
            data = rewrite_manifest_name_bytes(data, dst_name, manifest_label=manifest_rel)
        payload.append((rel, data, is_exec))

    exempted = _scan_payload(
        payload,
        kind=kind,
        artifact_name=dst_name,
        to_scope=to_scope,
        dst_project_root=dst_project_root,
        surface=surface,
        force_unsafe=force_unsafe,
        bundle_name=bundle_path.name,
    )
    if sorted(bundle.redaction_exempted) != exempted:
        raise BundleFormatError(
            f"{bundle_path.name}: 'redaction_exempted' lists "
            f"{sorted(bundle.redaction_exempted) or 'nothing'} but this scan waived "
            f"{exempted or 'nothing'}. The disclosure must match what the declaration "
            f"actually covers."
        )

    def _result(*, received: bool) -> BundleReceiveResult:
        """The dry-run and the applied result differ in exactly one field.

        Built through a typed closure rather than a ``dict`` splatted into the
        constructor: the dict widened every field to one union, so the checker
        could no longer tell ``kind`` from ``file_count`` and reported the whole
        call as twenty type errors. Naming the fields once here keeps the two
        return paths identical without giving that up.
        """
        return BundleReceiveResult(
            received=received,
            kind=kind,
            name=bundle.name,
            dst_name=dst_name,
            to_scope=to_scope,
            dst_project_root=dst_project_root,
            dst_path=dst_path,
            bundle_path=bundle_path,
            file_count=len(payload),
            needs_sync=needs_sync,
            sync_command=sync_command,
            versions_included=bundle.versions_included,
            redaction_exempted=exempted,
            source_tier=bundle.source_tier,
            source_wiki_commit=bundle.source_wiki_commit,
            adopt_hint=adopt_hint,
            notes=notes,
        )

    if _collides(dst_store, dst_name):
        raise TransferCollisionError(
            f"{kind}/{dst_name} already exists at scope={to_scope} ({dst_path}). "
            f"Import does not overwrite; use --as <new-name> to land it alongside."
        )
    if not apply_:
        return _result(received=False)

    if pre_materialize is not None:
        pre_materialize()

    with canonical_sidecar_lock(dst_store, dst_name, timeout=lock_timeout):
        try:
            run_swap_prelude(dst_store, dst_name, kind=kind)
        except SwapRecoveryError as exc:
            raise TransferRecoveryError(swap_failure_text(exc)) from exc
        if _collides(dst_store, dst_name):
            raise TransferCollisionError(f"destination appeared during lock acquire: {dst_path}.")
        dst_store.mkdir(parents=True, exist_ok=True)
        # Under the destination's lock, so a leftover removed here is provably
        # ours and not another process's in-flight staging.
        _reap_own_staging(dst_store, dst_name)
        staging = _staging_path(dst_store, dst_name)
        try:
            staging.mkdir(parents=False, exist_ok=False)
        except FileExistsError:
            staging = _staging_path(dst_store, dst_name)
            staging.mkdir(parents=False, exist_ok=False)
        try:
            write_tree_payload(staging, [(rel, data) for rel, data, _ in payload])
            for rel in bundle.dirs:
                (staging / rel).mkdir(parents=True, exist_ok=True)
            if os.name != "nt":
                for rel, _, is_exec in payload:
                    if is_exec:
                        (staging / rel).chmod(0o755)
            try:
                rename_no_replace(staging, dst_path)
            except OSError as exc:
                if exc.errno in (errno.EEXIST, errno.ENOTEMPTY, errno.EISDIR, errno.ENOTDIR):
                    raise TransferCollisionError(
                        f"destination appeared during promote: {dst_path}."
                    ) from exc
                raise
        except BaseException:
            _remove_staging(staging)
            raise
    return _result(received=True)


def _collides(dst_store: Path, dst_name: str) -> bool:
    """True when either spelling of the landing identity already exists.

    Normalizing every bundle to directory layout does not remove the store's
    second collision identity: a legacy flat ``<name>.md`` would be silently
    shadowed by a directory landing next to it, since the canonical lister
    gives the directory layout precedence. ``lstat`` so a dangling symlink or
    an entry of the wrong type counts too.

    Only "the entry is not there" counts as absent. Treating every ``OSError``
    as absent made a transient ``EACCES``/``EIO`` while probing the legacy
    ``<name>.md`` identity read as "nothing in the way", which is precisely the
    case this collision check exists to catch — the import would then land a
    directory that silently shadows the existing flat artifact. A probe that
    cannot answer must propagate, not answer "no".
    """
    for candidate in (dst_store / dst_name, dst_store / f"{dst_name}.md"):
        try:
            candidate.lstat()
        except (FileNotFoundError, NotADirectoryError):
            # NotADirectoryError: an ancestor is a file, so this path cannot
            # exist — a genuine absence, not a failure to look.
            continue
        return True
    return False


def _scan_payload(
    payload: list[tuple[str, bytes, bool]],
    *,
    kind: ArtifactKind,
    artifact_name: str,
    to_scope: TargetScope,
    dst_project_root: Path | None,
    surface: str,
    force_unsafe: bool,
    bundle_name: str,
) -> list[str]:
    """Gate A over the in-memory payload, fail-fast; returns what was exempted.

    The return value is the disclosure the receiver is shown, RE-DERIVED here
    rather than read off the wire: ``redaction_exempted`` is not covered by
    ``payload_sha256``, so a stripped field would tell the receiver nothing was
    waived while the bytes still landed.

    The artifact's own ``redaction: documents-patterns`` declaration is honored
    here exactly as it is on export, read from the manifest bytes that are about
    to land. That symmetry is the point: an artifact whose author declared it
    documents credential shapes must not export cleanly and then refuse on
    arrival, and the receiver is reading the same declaration in the same file.
    That includes the decode: both ends read the manifest through
    ``indexer_text`` (#2310), so a CRLF manifest cannot be exempt on one side
    and undeclared on the other.

    The ceiling is untouched — a ``project_shared`` landing refuses a
    declaration exactly as it refuses ``force_unsafe`` (ADR-0011 §5), and
    ``exemption_covers`` still waives only label-class hits, all-or-nothing.
    """
    manifest_rel = _DIR_MANIFEST[kind]
    artifact_exemption: str | None = None
    for rel, data, _ in payload:
        if rel == manifest_rel:
            artifact_exemption = declared_exemption(Path(rel), indexer_text(data, errors="replace"))
            break
    exempted: list[str] = []
    declared = artifact_exemption == DECLARED_EXEMPTION_DOCUMENTS_PATTERNS
    for rel, data, _ in payload:
        scan = scan_text_content(
            data.decode("utf-8", errors="replace"),
            source_path=Path(rel),
            surface=surface,
            scope=to_scope,
            project_root=dst_project_root,
            force_unsafe=force_unsafe,
            declared_exemption=artifact_exemption,
        )
        if scan.decision == "exempted" or (
            # ``force_unsafe`` wins over the declaration inside the guard and
            # returns "bypassed" before the declaration is ever consulted, by
            # design — its audit line is the one that describes what happened.
            # But the disclosure this function returns answers a DIFFERENT
            # question ("what did the declaration cover?"), and reading it off
            # the admission decision made the two collapse: with the valve set,
            # every declared file re-derived as waiving nothing, and the
            # equality check against the wire then refused the sender's honest
            # disclosure as malformed. So coverage is computed from the hits the
            # same scan already returned, independently of what was admitted.
            scan.decision == "bypassed"
            and declared
            and bool(scan.hits)
            and exemption_covers(list(scan.hits))
        ):
            exempted.append(rel)
        if scan.decision not in ("blocked", "blocked_project_shared"):
            continue
        if to_scope == "project_shared":
            raise_or_collect(
                scan,
                scope=to_scope,
                kind=kind[:-1],
                artifact_name=artifact_name,
                remediation_hint=(
                    f"Offending bundle entry: {rel}. Ask the sender to remove the secret "
                    f"and re-export, or import to --to project_local instead."
                ),
            )
        # The engine states the CONDITION only; each surface appends its own
        # remediation vocabulary via ``remediation.append_hint`` (#1869). An
        # engine that spelled a CLI flag here would hand an MCP or web caller a
        # flag it cannot pass.
        raise BundlePrivacyError(
            f"privacy block: {bundle_name} entry {rel} contains a secret-shaped value "
            f"and was refused for a {to_scope} landing. Review the bundle first.",
            code=skip_codes.PRIVACY_BLOCKED,
        )
    return sorted(exempted)
