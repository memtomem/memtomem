"""Pure helpers of the store: namespaces, paths, the projection header, the repair plan.

Everything here is a function of its arguments. The store (``store.py``) owns
the locks, the files and the memtomem components; this module decides what a
given on-disk state means, so the decision can be tested without a store. The
contract these helpers implement is written in ``docs/protocol.md``.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from memtomem.constants import validate_namespace

#: Prefix of every encoded segment, on disk and in a memtomem namespace. It
#: keeps an encoded segment from ever equalling a raw one (``("a b",)`` and
#: ``("meqge",)`` stay apart), and no Windows reserved device name (``CON``,
#: ``COM2``, ``LPT3``, …) starts with ``s``.
SEGMENT_MARKER = "s"

#: Longest encoded segment. The longest name the store writes is the
#: temporary file of an atomic marker write, ``.<token>.json.deleting.<8>.tmp``
#: (``tempfile.mkstemp`` inside ``atomic_write_text``): the token plus 28
#: characters. Common filesystems cap a name at 255 bytes; 200 leaves room.
SEGMENT_MAX_CHARS = 200
_TOKEN_RE = re.compile(r"s[a-z2-7]+")

#: ``validate_namespace``'s total-length cap.
NAMESPACE_MAX_CHARS = 256

#: Marker and record file suffixes beside the item files.
INDEXED_SUFFIX = ".indexed"
DELETING_SUFFIX = ".deleting"
#: Tombstone a delete leaves beside where the record was: the generation the
#: item reached. A later put of the same key continues from it, so a new
#: record always outranks an old deletion marker and a row cached before the
#: delete never names the new record's generation.
LAST_SUFFIX = ".last"


# --- the segment codec -------------------------------------------------------


def encode_segment(value: str) -> str:
    """``s`` + lowercase RFC 4648 base32 of ``value``'s UTF-8, without padding.

    The alphabet is ``a-z2-7``: one case only, so two different segments never
    share a path on a filesystem that folds case. base64url (the codec
    ``MemtomemBaseStore`` uses) is case-sensitive and does (``aaa`` →
    ``YWFh``, ``aaG`` → ``YWFH``).
    """
    if not isinstance(value, str) or not value or "\x00" in value:
        raise ValueError("a namespace segment or key is a non-empty string without NUL")
    body = base64.b32encode(value.encode("utf-8")).decode("ascii").rstrip("=").lower()
    token = SEGMENT_MARKER + body
    if len(token) > SEGMENT_MAX_CHARS:
        raise ValueError(
            f"{value[:20]!r}… encodes to {len(token)} characters; a segment or key "
            f"may encode to at most {SEGMENT_MAX_CHARS}"
        )
    return token


def decode_segment(token: str) -> str:
    """Inverse of :func:`encode_segment`; ``ValueError`` for a token it would not emit."""
    if not isinstance(token, str) or _TOKEN_RE.fullmatch(token) is None:
        raise ValueError(f"{token!r} is not an encoded segment")
    body = token[len(SEGMENT_MARKER) :].upper()
    try:
        value = base64.b32decode(body + "=" * (-len(body) % 8)).decode("utf-8")
    except (binascii.Error, ValueError) as exc:
        raise ValueError(f"{token!r} is not an encoded segment") from exc
    if encode_segment(value) != token:
        raise ValueError(f"{token!r} is not the canonical encoding of its value")
    return value


# --- namespaces --------------------------------------------------------------


def to_memtomem_namespace(namespace: tuple[str, ...]) -> str:
    """The one memtomem namespace a LangGraph namespace tuple maps to.

    Every segment is encoded, always, and joined with ``:``. The mapping is
    injective and the result passes :func:`memtomem.constants.validate_namespace`
    or this raises ``ValueError`` naming the cap and the encoded length.
    """
    if not namespace:
        raise ValueError("a LangGraph namespace has at least one segment")
    joined = ":".join(encode_segment(segment) for segment in namespace)
    if len(joined) > NAMESPACE_MAX_CHARS:
        raise ValueError(
            f"namespace {namespace!r} encodes to {len(joined)} characters; "
            f"memtomem caps a namespace at {NAMESPACE_MAX_CHARS}"
        )
    validate_namespace(joined)
    return joined


def from_memtomem_namespace(value: str) -> tuple[str, ...]:
    """Inverse of :func:`to_memtomem_namespace`; ``ValueError`` for anything else.

    A segment without the marker, or whose token is not the canonical encoding
    of some string, was not written by this store.
    """
    return tuple(decode_segment(token) for token in value.split(":"))


# --- paths -------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ItemPaths:
    """Every file one item may own. ``record`` is the source of truth."""

    record: Path
    deleting: Path
    last: Path
    projection: Path
    indexed: Path


def record_path(records_dir: Path, namespace: tuple[str, ...], key: str) -> Path:
    """One directory per encoded segment, the encoded key as the file name."""
    parts = [encode_segment(segment) for segment in namespace]
    return records_dir.joinpath(*parts, f"{encode_segment(str(key))}.json")


def item_paths(
    records_dir: Path, projections_dir: Path, namespace: tuple[str, ...], key: str
) -> ItemPaths:
    """Where an item's files live: the record tree and the projection tree mirror each other."""
    parts = [encode_segment(segment) for segment in namespace]
    name = encode_segment(str(key))
    record = records_dir.joinpath(*parts, f"{name}.json")
    projection = projections_dir.joinpath(*parts, f"{name}.md")
    return ItemPaths(
        record=record,
        deleting=record.with_name(record.name + DELETING_SUFFIX),
        last=record.with_name(record.name + LAST_SUFFIX),
        projection=projection,
        indexed=projection.with_name(projection.name + INDEXED_SUFFIX),
    )


def item_from_path(root: Path, path: Path, suffix: str) -> tuple[tuple[str, ...], str]:
    """``(namespace, key)`` of a record or projection path under ``root``.

    ``suffix`` is ``".json"`` for a record and ``".md"`` for a projection.
    ``ValueError`` when the path is not under ``root``, has the wrong suffix,
    or a component is not a canonical encoded segment: such a file was not
    written by this store.
    """
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise ValueError(f"{path} is not under {root}") from exc
    parts = relative.parts
    if not parts or not parts[-1].endswith(suffix) or len(parts) < 2:
        raise ValueError(f"{path} is not an item {suffix} file")
    namespace = tuple(decode_segment(part) for part in parts[:-1])
    key = decode_segment(parts[-1][: -len(suffix)])
    return namespace, key


# --- the projection file -----------------------------------------------------


def render_projection_file(body: str) -> str:
    """The text of a projection: one empty line, then the rendered value.

    Nothing of the store's own bookkeeping is in the file, so nothing of it is
    searchable. The leading empty line keeps memtomem's chunker from reading a
    value that starts with ``---`` as frontmatter (measured: a value opening
    with a frontmatter block that sets ``valid_to`` in the past was otherwise
    hidden from search).
    """
    return "\n" + body + ("" if body.endswith("\n") else "\n")


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


#: One row's provenance: its memtomem chunk id and the SHA-256 of the text
#: retrieval ranked it by (``Chunk.retrieval_content``: the heading path or
#: retrieval context, then the body).
ChunkStamp = tuple[str, str]


@dataclass(frozen=True, slots=True)
class IndexedMarker:
    """What the last clean index of a projection produced.

    ``generation`` is the record generation it was indexed from, ``digest`` the
    SHA-256 of the projection text, ``chunks`` the stamp of every row the index
    holds for the file afterwards. A hit is served only when its stamp is in
    ``chunks``. The id alone is not enough: memtomem keeps a chunk's id across
    a re-index when its body hash matches (ADR-0005), and the body hash leaves
    out the heading path that retrieval ranks by, so a row whose heading
    changed keeps its id (measured by review: a cached hit for the old heading
    was served with the new value).
    """

    generation: int
    digest: str
    chunks: frozenset[ChunkStamp]


def render_indexed_marker(marker: IndexedMarker) -> str:
    return (
        json.dumps(
            {
                "generation": marker.generation,
                "digest": marker.digest,
                "chunks": dict(sorted(marker.chunks)),
            },
            sort_keys=True,
        )
        + "\n"
    )


def read_indexed_marker(path: Path) -> IndexedMarker | None:
    """The marker at ``path``; ``None`` when absent or not one this store wrote."""
    try:
        payload = json.loads(path.read_bytes().decode("utf-8"))
        generation = payload["generation"]
        digest = payload["digest"]
        chunks = payload["chunks"]
        if (
            type(generation) is not int
            or generation < 0
            or not isinstance(digest, str)
            or not isinstance(chunks, dict)
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in chunks.items())
        ):
            return None
        return IndexedMarker(generation, digest, frozenset(chunks.items()))
    except (OSError, ValueError, TypeError, KeyError, RecursionError):
        return None


# --- markers -----------------------------------------------------------------


def read_marker(path: Path) -> int | None:
    """The generation a marker file holds, ``None`` when absent or unreadable."""
    try:
        text = path.read_bytes().decode("ascii").strip()
    except (OSError, UnicodeError):
        return None
    if re.fullmatch(r"0|[1-9][0-9]*", text) is None:
        return None
    return int(text)


# --- repair ------------------------------------------------------------------


class Action(Enum):
    """What repair does with one item, from its on-disk state."""

    NOTHING = "nothing"
    FINISH_DELETE = "finish_delete"
    PURGE_PROJECTION = "purge_projection"
    RENDER_AND_INDEX = "render_and_index"
    INDEX = "index"


@dataclass(frozen=True, slots=True)
class ItemState:
    """What one item looks like on disk and in the index, read under its lock.

    ``record_gen`` is ``None`` with no readable record. ``index_off`` is a
    record that has nothing to rank: ``index=False``, or selectors that render
    to empty text. ``projection_current`` is whether the projection file holds
    exactly the text the record renders to; ``indexed_current`` whether the
    ``.indexed`` marker names the record's generation and that text's digest;
    ``indexed_chunks`` the row stamps that marker lists. ``rows_match`` is
    whether the rows the index holds now carry exactly those stamps; it is
    only computed for repair and is ``True`` when not checked. ``deleting_gen``
    is the deletion marker's generation; ``rows`` the index rows stored for the
    projection path.
    """

    record_gen: int | None
    index_off: bool
    projection_present: bool
    projection_current: bool
    marker_present: bool
    indexed_current: bool
    indexed_chunks: frozenset[ChunkStamp]
    deleting_gen: int | None
    rows: int
    rows_match: bool = True

    @property
    def has_derived(self) -> bool:
        return self.projection_present or self.marker_present or self.rows > 0

    @property
    def agrees(self) -> bool:
        """Record, projection, marker and rows agree: the item is serveable."""
        return (
            self.record_gen is not None
            and not self.index_off
            and self.deleting_gen is None
            and self.projection_current
            and self.indexed_current
            and self.rows > 0
            and self.rows_match
        )


def classify(state: ItemState) -> Action:
    """The repair action for ``state``. See ``docs/protocol.md`` §Repair.

    A deletion marker wins over a record of the same or an older generation;
    a newer record superseded the marker (the executor drops the marker
    whenever the action is not ``FINISH_DELETE``).
    """
    if state.deleting_gen is not None and (
        state.record_gen is None or state.record_gen <= state.deleting_gen
    ):
        return Action.FINISH_DELETE
    if state.record_gen is None or state.index_off:
        return Action.PURGE_PROJECTION if state.has_derived else Action.NOTHING
    if not state.projection_current:
        return Action.RENDER_AND_INDEX
    if not state.indexed_current or state.rows == 0 or not state.rows_match:
        return Action.INDEX
    return Action.NOTHING
