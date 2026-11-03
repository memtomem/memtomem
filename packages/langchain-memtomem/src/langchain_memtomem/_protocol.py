"""Pure helpers of the store: namespaces, paths, the projection header, the repair plan.

Everything here is a function of its arguments. The store (``store.py``) owns
the locks, the files and the memtomem components; this module decides what a
given on-disk state means, so the decision can be tested without a store. The
contract these helpers implement is written in ``docs/protocol.md``.
"""

from __future__ import annotations

import base64
import binascii
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

#: Longest encoded segment. A record file is ``<token>.json.deleting``, and
#: common filesystems cap a name at 255 bytes.
SEGMENT_MAX_CHARS = 240
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

#: Frontmatter tag every chunk of a projection carries: ``lcmm-gen-<n>``. The
#: chunker copies a file's frontmatter tags onto each of its rows and refreshes
#: them on re-index even for chunks whose text did not change (measured on
#: memtomem ``bb0e63a9``), so a search hit names the generation its row was
#: indexed from.
GENERATION_TAG_PREFIX = "lcmm-gen-"

_HEADER_VERSION = "langchain-memtomem v1"
_HEADER_RE = re.compile(
    r"^<!-- langchain-memtomem v1 ns=(?P<ns>s[a-z2-7]+(?::s[a-z2-7]+)*) "
    r"key=(?P<key>s[a-z2-7]+) gen=(?P<gen>\d+) -->$"
)
#: The header comment is on this line (1-based) of a projection file.
_HEADER_LINE = 4


def render_projection_file(namespace_enc: str, key_enc: str, generation: int, body: str) -> str:
    """The text of a projection.

    Three frontmatter lines carrying the generation tag, one header comment
    naming the item and its generation, then the rendered value.
    """
    head = (
        f"---\ntags: [{GENERATION_TAG_PREFIX}{generation}]\n---\n"
        f"<!-- {_HEADER_VERSION} ns={namespace_enc} key={key_enc} gen={generation} -->\n"
    )
    if not body:
        return head
    return head + body + ("" if body.endswith("\n") else "\n")


def parse_projection_header(text: str) -> tuple[str, str, int] | None:
    """``(namespace_enc, key_enc, generation)`` from a projection's header, or ``None``.

    The frontmatter tag must name the same generation; a file whose two
    generations disagree was not written whole and parses as ``None``.
    """
    lines = text.split("\n", _HEADER_LINE)
    if len(lines) < _HEADER_LINE:
        return None
    match = _HEADER_RE.match(lines[_HEADER_LINE - 1])
    if match is None:
        return None
    generation = int(match.group("gen"))
    if lines[1].strip() != f"tags: [{GENERATION_TAG_PREFIX}{generation}]":
        return None
    return match.group("ns"), match.group("key"), generation


def generation_from_tags(tags: tuple[str, ...]) -> int | None:
    """The generation a row's tags name, ``None`` when absent or ambiguous."""
    found = {
        int(tag[len(GENERATION_TAG_PREFIX) :])
        for tag in tags
        if tag.startswith(GENERATION_TAG_PREFIX) and tag[len(GENERATION_TAG_PREFIX) :].isdigit()
    }
    if len(found) != 1:
        return None
    return found.pop()


# --- markers -----------------------------------------------------------------


def read_marker(path: Path) -> int | None:
    """The generation a marker file holds, ``None`` when absent or unreadable."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return None
    except OSError:
        return None
    text = text.strip()
    if not text.isdigit():
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

    ``record_gen`` is ``None`` with no readable record; ``projection_gen`` is
    ``None`` when the projection is missing or its header does not name this
    item; ``projection_present`` is whether the file exists at all;
    ``marker_gen`` and ``deleting_gen`` are the marker files' contents; ``rows``
    is the number of index rows stored for the projection path.
    """

    record_gen: int | None
    index_off: bool
    projection_present: bool
    projection_gen: int | None
    marker_gen: int | None
    deleting_gen: int | None
    rows: int

    @property
    def has_derived(self) -> bool:
        return self.projection_present or self.marker_gen is not None or self.rows > 0

    @property
    def agrees(self) -> bool:
        """The three generations agree and rows exist: the item is serveable."""
        return (
            self.record_gen is not None
            and not self.index_off
            and self.deleting_gen is None
            and self.projection_gen == self.record_gen
            and self.marker_gen == self.record_gen
            and self.rows > 0
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
    if state.projection_gen != state.record_gen:
        return Action.RENDER_AND_INDEX
    if state.marker_gen != state.record_gen or state.rows == 0:
        return Action.INDEX
    return Action.NOTHING
