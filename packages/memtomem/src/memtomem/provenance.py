"""Local-provenance marker for export/import bundles (ADR-0006 Axis F.3).

A bundle this install exported carries an HMAC-SHA256 marker over its
canonical chunk payload, keyed by a per-install secret stored as a sidecar
next to the SQLite DB (``<db-stem>.provenance_key``). On import, a valid
marker means the bundle is a *self-export* and round-trips unchanged (the
redaction re-scan is skipped); an absent or invalid marker means the bundle is
foreign and runs the per-record redaction gate
(``privacy.enforce_write_guard``).

What the marker proves — and does not
-------------------------------------
It proves **local provenance** (this install's storage produced the bundle),
NOT that every chunk passed redaction. Legacy pre-guard rows, prior
``force_unsafe`` writes, and content from the still-unguarded folder-index path
can all appear in a self-export. F.3 is therefore an explicit
*local-provenance round-trip exemption* (ADR-0006 Axis F): we re-import our own
export as-is, trusting the local user's earlier storage decisions, rather than
re-proving redaction on data that already lives in this install. The stronger
per-chunk-redaction-provenance alternative is deferred.

Threat model
------------
* **Forgery.** A foreign author cannot compute a valid marker without the
  per-install key, so key secrecy is load-bearing. The key file is created and
  read with symlink-safe, owner-private, exclusive semantics
  (``O_CREAT|O_EXCL|O_NOFOLLOW`` + a post-open ``fstat`` that requires a regular
  file owned by the current user with no group/other permission bits) so a file
  or symlink pre-planted in a misconfigured storage directory (e.g. a shared
  ``MEMTOMEM_STORAGE__SQLITE_PATH``) cannot leak or fix the key. Verification
  fails *safe* — any key-file anomaly is treated as "no key" → bundle foreign →
  re-scanned, never an exception. Signing fails *closed* — a suspicious
  existing key (symlink / non-regular / wrong owner / loose mode) raises rather
  than signing with an attacker-influenced secret.
* **Modification / injection.** The HMAC binds the entire canonical ``chunks``
  list, so editing, adding, or removing any record invalidates the marker.
* **Replay / lift (accepted property).** An *unchanged* self-export re-imports
  as self — that is the intended round-trip. A third party who holds your old
  export already holds its plaintext; re-importing it only restores data this
  install itself produced. The revocation lever is to delete or rotate the
  sidecar key (``<db-stem>.provenance_key``): every prior export then verifies
  as foreign and is re-scanned.

``_canonical_chunks`` is deterministic for the *current* bundle-chunk schema
(ints + strings + lists, no floats); it is not RFC-8785 canonical JSON. Any
future change that makes import trust or behavior depend on a non-``chunks``
bundle field (``version`` / ``exported_at`` / ``total_chunks``) must bump
``SCHEME`` or fold that field into the signed payload.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import stat
from pathlib import Path

logger = logging.getLogger(__name__)

SCHEME = "memtomem-bundle-provenance-v1"
ALGO = "HMAC-SHA256"
_KEY_BYTES = 32
# A 32-byte key is 64 hex chars; cap the read so a giant file planted at the
# key path can't be slurped into memory.
_KEY_READ_CAP = 4096


class ProvenanceKeyError(Exception):
    """Raised when the provenance key file is unsafe to sign with."""


def key_path_for_db(db_path: Path | str) -> Path:
    """Sidecar key path for a SQLite DB: ``<db-stem>.provenance_key``.

    ``~/.memtomem/memtomem.db`` → ``~/.memtomem/memtomem.provenance_key``. Tying
    the key to the DB path means the existing storage-isolation knob
    (``MEMTOMEM_STORAGE__SQLITE_PATH``, worktree / HOME overrides) isolates the
    key for free, and a DB moved without its sidecar degrades safely (its old
    exports verify as foreign and are re-scanned).
    """
    return Path(db_path).expanduser().with_suffix(".provenance_key")


def _decode_key(raw: bytes) -> bytes | None:
    try:
        key = bytes.fromhex(raw.decode("ascii").strip())
    except (ValueError, UnicodeDecodeError):
        return None
    return key if len(key) == _KEY_BYTES else None


def _fd_is_safe(fd: int) -> tuple[bool, str]:
    """Validate an *opened* key fd: regular file, owner-only, no group/other.

    Validating ``os.fstat(fd)`` (the inode actually opened) rather than only a
    pre-open ``lstat`` closes the TOCTOU window — the bytes we read came from
    the inode we vetted. Owner / mode checks are POSIX-only; Windows lacks the
    same permission model, so there a regular file is accepted.
    """
    st = os.fstat(fd)
    if not stat.S_ISREG(st.st_mode):
        return False, "not a regular file"
    if os.name == "posix":
        if hasattr(os, "getuid") and st.st_uid != os.getuid():
            return False, "not owned by the current user"
        if st.st_mode & 0o077:
            return False, "group/other-accessible (must be 0o600)"
    return True, ""


def _read_existing_key(path: Path, *, strict: bool) -> bytes | None:
    """Read a key file with symlink-safe, owner-private validation.

    Returns the 32-byte key, or ``None`` if the file is absent / malformed /
    unsafe. "Unsafe" = a symlink, a non-regular file, a file not owned by the
    current user, or one with group/other permission bits — any of which would
    let a foreign party in a shared/misconfigured storage dir plant or read the
    HMAC key and forge a "self" marker. On an unsafe key: ``strict=True``
    (export/sign) raises ``ProvenanceKeyError`` (fail closed — never sign with
    an attacker-influenced key); ``strict=False`` (verify) returns ``None`` so
    the bundle falls through to the foreign re-scan path (fail safe).
    """
    try:
        lst = os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError as exc:
        if strict:
            raise ProvenanceKeyError(f"cannot stat provenance key {path}: {exc}") from exc
        return None

    # Reject symlinks before opening; ``O_NOFOLLOW`` + the post-open ``fstat``
    # below are the authoritative checks, this is just an early, clear reject.
    if stat.S_ISLNK(lst.st_mode):
        msg = f"provenance key {path} is a symlink; refusing to follow"
        if strict:
            raise ProvenanceKeyError(msg)
        logger.warning("%s — treating bundle as foreign", msg)
        return None

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        if strict:
            raise ProvenanceKeyError(f"cannot open provenance key {path}: {exc}") from exc
        return None
    try:
        ok, reason = _fd_is_safe(fd)
        if not ok:
            msg = f"provenance key {path} is unsafe ({reason}); refusing to trust it"
            if strict:
                raise ProvenanceKeyError(msg)
            logger.warning("%s — treating bundle as foreign", msg)
            return None
        raw = os.read(fd, _KEY_READ_CAP)
    finally:
        os.close(fd)
    return _decode_key(raw)


def load_key_for_verify(key_path: Path) -> bytes | None:
    """Return the install key for verification, or ``None`` if absent/unsafe.

    Never creates a key and never raises: a missing or anomalous key file means
    "cannot prove provenance" → the caller treats the bundle as foreign and
    re-scans it.
    """
    return _read_existing_key(Path(key_path), strict=False)


def load_or_create_key_for_export(key_path: Path) -> bytes:
    """Return the install key for signing, creating it on first export.

    Creation is symlink-safe and exclusive (``O_CREAT|O_EXCL|O_NOFOLLOW``,
    ``0o600``). A pre-existing but unsafe key (symlink / non-regular) raises
    ``ProvenanceKeyError`` rather than signing with an attacker-influenced key.
    """
    path = Path(key_path)
    existing = _read_existing_key(path, strict=True)
    if existing is not None:
        return existing

    key = secrets.token_bytes(_KEY_BYTES)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(path, flags, 0o600)
    except FileExistsError:
        # A concurrent export created the key first — read it back (strict).
        existing = _read_existing_key(path, strict=True)
        if existing is None:
            raise ProvenanceKeyError(
                f"provenance key {path} appeared during creation but is unreadable"
            ) from None
        return existing
    try:
        if hasattr(os, "fchmod"):
            os.fchmod(fd, 0o600)
        os.write(fd, key.hex().encode("ascii"))
    finally:
        os.close(fd)
    return key


def _canonical_chunks(chunks: list[dict]) -> bytes:
    # Deterministic for the current chunk schema (no floats); ``sort_keys``
    # makes it insensitive to dict key order, and signing the parsed objects
    # (not the pretty-printed bundle text) makes it insensitive to indentation.
    return json.dumps(chunks, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )


def sign_chunks(chunks: list[dict], key: bytes) -> str:
    """HMAC-SHA256 hex digest over ``SCHEME + "\\n" + canonical(chunks)``."""
    payload = SCHEME.encode("ascii") + b"\n" + _canonical_chunks(chunks)
    return hmac.new(key, payload, hashlib.sha256).hexdigest()


def make_marker(chunks: list[dict], key: bytes) -> dict[str, str]:
    """Build the bundle ``provenance`` marker for ``chunks``."""
    return {"scheme": SCHEME, "algo": ALGO, "signature": sign_chunks(chunks, key)}


def verify_marker(chunks: list[dict], marker: object, key: bytes | None) -> bool:
    """True iff ``marker`` is a valid local-provenance HMAC over ``chunks``.

    Returns ``False`` (foreign) on a missing key, a malformed/absent marker, a
    scheme/algo mismatch, or a signature mismatch — every non-self path is the
    safe one (the caller then runs the redaction gate).
    """
    if key is None or not isinstance(marker, dict):
        return False
    if marker.get("scheme") != SCHEME or marker.get("algo") != ALGO:
        return False
    sig = marker.get("signature")
    if not isinstance(sig, str):
        return False
    try:
        return hmac.compare_digest(sign_chunks(chunks, key), sig)
    except TypeError:
        # ``hmac.compare_digest`` raises on a non-ASCII ``str`` — our digest is
        # always lowercase hex, so any non-ASCII signature is foreign, not ours.
        return False
