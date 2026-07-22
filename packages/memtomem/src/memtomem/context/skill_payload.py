"""ADR-0030 §10 — the skill *payload* surface and its tree digest.

Skills are directory artifacts whose version store lives **inside** the
artifact directory (``<canonical>/<name>/versions/`` + ``versions.json``,
ADR-0022). That creates a recursion hazard the moment versions become trees:
a naive snapshot would contain the version store, ``v2`` would contain ``v1``,
and fan-out would push internal metadata into runtimes. §10 resolves it with
**two precisely-scoped surfaces**, both defined here:

* :func:`read_skill_tree` — the **WIDE copier surface**: every byte a copy
  would move, Store-owned metadata included. The ingress Gate-A privacy scan
  uses this one, because a secret hiding under ``overrides/`` would still be
  copied into the transaction and must still be caught.
* :func:`iter_skill_payload_files` — the **NARROW payload surface**: the
  artifact *content*, excluding the Store-owned top-level ``overrides/`` /
  ``versions/`` / ``versions.json`` (plus that manifest's lock/temp sidecars)
  and our own ``.staging-*`` / ``.old-*`` crash leftovers. It drives the
  snapshot content, the tree digest, the Store↔candidate comparison, the
  fan-out staging surface, and the sync diff.

Keeping both in one module is deliberate: the invariant is the *relation*
between them (narrow ⊂ wide), and a split-brain exclusion set is exactly how
version history would leak into a runtime or a snapshot would come to contain
a snapshot. Widening the gate is a safety decision, so the wide surface is
never derived from the narrow one — the narrow one filters the wide one.

Exclusion is **top-level only**: a nested ``scripts/versions.json`` or
``docs/overrides/`` is ordinary user content and stays payload.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from memtomem.context._names import is_internal_artifact_dir
from memtomem.context.versioning import _MANIFEST_FILENAME, _VERSIONS_DIRNAME

# Store-owned internal metadata excluded from the *payload* surface. The
# runtime side never carries these; counting them in the Store comparison
# would make every versioned / override-carrying skill read as ``differs``,
# and fanning them out would push version history into runtimes. NOT excluded
# from the copier/gate surface (a secret under them would still be copied by a
# Pull). Names are pulled from the version store's own constants so the
# exclusion set cannot drift from the writer.
_OVERRIDES_DIRNAME = "overrides"
_PAYLOAD_EXCLUDED_TOP_DIRS = frozenset({_OVERRIDES_DIRNAME, _VERSIONS_DIRNAME})
# ``atomic_write_bytes`` leaves ``.{name}.*.tmp`` siblings; ``_lock_path_for``
# writes ``.{name}.lock`` — both next to ``versions.json`` at the skill root.
_MANIFEST_LOCK_NAME = f".{_MANIFEST_FILENAME}.lock"
_MANIFEST_TMP_PREFIX = f".{_MANIFEST_FILENAME}."


def _is_store_internal_top_file(name: str) -> bool:
    """Top-level version-store metadata files (manifest + its lock/temp sidecars)."""
    if name == _MANIFEST_FILENAME or name == _MANIFEST_LOCK_NAME:
        return True
    return name.startswith(_MANIFEST_TMP_PREFIX) and name.endswith(".tmp")


def is_payload_top_name(name: str) -> bool:
    """Whether a **top-level** entry of a skill dir is payload (skill content).

    The single definition of the exclusion set. Callers that walk relative
    paths use :func:`is_payload_relpath`; callers that filter a directory
    listing (the fan-out copier's root-only skip) use this one, so the two can
    never drift.

    ``False`` for the Store-owned ``overrides/`` / ``versions/`` directories,
    the ``versions.json`` manifest and its ``.lock`` / ``.tmp`` sidecars, and
    our own ``.staging-*`` / ``.old-*`` crash-leftover trees
    (:func:`~memtomem.context._names.is_internal_artifact_dir` — the same
    predicate the extract/reap paths use, so "hidden" and "excluded" can't
    drift apart).
    """
    if name in _PAYLOAD_EXCLUDED_TOP_DIRS or is_internal_artifact_dir(name):
        return False
    return not _is_store_internal_top_file(name)


def is_payload_relpath(rel: str) -> bool:
    """Whether a posix relpath (relative to the skill root) is payload.

    Only the FIRST segment is judged — the Store owns the top level — so a
    nested ``scripts/versions.json`` or ``docs/overrides/x`` is user content
    while an excluded top-level entry takes its whole subtree with it.

    Delegates to :func:`is_payload_top_name` rather than re-deriving the
    exclusion set: judging a *file*-shaped name only at ``len(parts) == 1``
    would make a directory named ``versions.json`` (or a sidecar-shaped
    ``.versions.json.<rand>.tmp/`` left by a crash) payload here while fan-out
    and the diff — which filter a directory listing — dropped it, which is
    exactly the digest-vs-fan-out disagreement this module exists to prevent.
    """
    return is_payload_top_name(rel.split("/")[0])


def read_skill_tree(root: Path) -> list[tuple[str, bytes]]:
    """Full copier surface of a skill dir as sorted ``(posix_relpath, bytes)``.

    The WIDE surface (see the module docstring): uses the copier-surface
    iterator so gate scanning sees every byte a Pull would copy into the
    transaction (§5 grouping uses the narrow payload surface instead — what a
    Pull actually lands). Raises ``OSError`` (fail closed).
    """
    # Imported lazily to avoid import-order coupling with the large skills
    # module (which imports the gate/override leaves this module also uses).
    from memtomem.context.skills import _iter_scannable_skill_files

    files: list[tuple[str, bytes]] = []
    for path in _iter_scannable_skill_files(root):
        rel = path.relative_to(root).as_posix()
        files.append((rel, path.read_bytes()))
    files.sort()
    return files


def iter_skill_payload_files(root: Path) -> list[tuple[str, bytes]]:
    """The skill *payload* as sorted ``(posix_relpath, bytes)`` (ADR-0030 §10).

    The NARROW surface: :func:`read_skill_tree` (which already drops
    ``COPY_SKIP_NAMES`` and symlinks and fails CLOSED on ``OSError``) filtered
    by :func:`is_payload_relpath`.

    Propagates ``OSError`` (unreadable subtree or file) so callers fail closed.
    """
    return [(rel, data) for rel, data in read_skill_tree(root) if is_payload_relpath(rel)]


def payload_digest(payload: list[tuple[str, bytes]]) -> str:
    """Canonical ADR-0030 §10 tree digest over a ``(relpath, bytes)`` payload.

    SHA-256 with **length-prefixed framing** (8-byte big-endian length before
    each member) so no ``(rel, data)`` pair can be confused with a different
    split of the same bytes, over the payload **sorted** — order-independent,
    so two callers that walked the tree differently agree.

    Two properties the framing deliberately leaves out, both required for
    reproducibility:

    * **file-only** — empty directories are not tracked (the payload iterator
      yields files, and the copier does not preserve empty dirs);
    * **mode-independent** — the executable bit is NOT part of the digest,
      because ``copy_tree_atomic`` normalizes modes to ``0o644`` and
      preserving a bit the copier drops would make digests unreproducible.

    One digest, several consumers: the pull-apply Store-state precondition
    today, the tree snapshot / version identity next (§10), and campaign 2's
    snapshot CAS after that. Changing the framing changes every stored digest —
    treat it as a wire format (``test_context_skill_payload.py`` pins a
    stability vector).
    """
    h = hashlib.sha256()
    for rel, data in sorted(payload):
        rel_b = rel.encode("utf-8")
        h.update(len(rel_b).to_bytes(8, "big"))
        h.update(rel_b)
        h.update(len(data).to_bytes(8, "big"))
        h.update(data)
    return h.hexdigest()
