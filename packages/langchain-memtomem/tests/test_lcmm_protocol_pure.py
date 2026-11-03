"""The pure helpers: namespaces, paths, the projection file, the repair table."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_memtomem._protocol import (
    NAMESPACE_MAX_CHARS,
    SEGMENT_MARKER,
    SEGMENT_MAX_CHARS,
    Action,
    IndexedMarker,
    ItemState,
    classify,
    decode_segment,
    encode_segment,
    from_memtomem_namespace,
    item_from_path,
    item_paths,
    read_indexed_marker,
    read_marker,
    render_indexed_marker,
    render_projection_file,
    text_digest,
    to_memtomem_namespace,
)

# --- namespaces --------------------------------------------------------------


@pytest.mark.parametrize(
    "namespace",
    [("a",), ("users", "alice"), ("a b", "YSBi"), ("한글", "x.y"), ("-dash",), ("a:b", "c,d")],
)
def test_namespace_round_trips_and_validates(namespace: tuple[str, ...]) -> None:
    encoded = to_memtomem_namespace(namespace)
    assert from_memtomem_namespace(encoded) == namespace
    for segment in encoded.split(":"):
        assert segment.startswith(SEGMENT_MARKER)
        assert not segment.startswith("-")


def test_raw_and_encoded_segments_never_collide() -> None:
    # ``("a b",)`` encodes to ``smeqge``; a raw ``("smeqge",)`` encodes again.
    assert to_memtomem_namespace(("a b",)) != to_memtomem_namespace((encode_segment("a b"),))


@pytest.mark.parametrize(
    ("left", "right"),
    [("aaa", "aaG"), ("users", "USERS"), ("Ab", "aB"), ("x" * 50, "x" * 49 + "X")],
)
def test_tokens_differ_under_case_folding(left: str, right: str) -> None:
    """The codec's point: a filesystem that folds case keeps two segments apart."""
    a, b = encode_segment(left), encode_segment(right)
    assert a == a.lower() and b == b.lower()
    assert a.lower() != b.lower()


@pytest.mark.parametrize("value", ["a", "a b", "한글", "-x", "a:b", ".", "..", "x" * 124])
def test_segment_codec_round_trips_canonically(value: str) -> None:
    token = encode_segment(value)
    assert decode_segment(token) == value
    assert token.startswith(SEGMENT_MARKER) and len(token) <= SEGMENT_MAX_CHARS


@pytest.mark.parametrize("token", ["", "s", "sMFQWC", "mfqwc", "s!!", "sb", "sme======"])
def test_decode_refuses_tokens_the_encoder_never_emits(token: str) -> None:
    with pytest.raises(ValueError):
        decode_segment(token)


@pytest.mark.parametrize("value", ["", "a\x00b", "x" * 125])
def test_encode_refuses_empty_nul_and_overlong(value: str) -> None:
    with pytest.raises(ValueError):
        encode_segment(value)


def test_namespace_over_the_cap_is_refused_with_the_numbers() -> None:
    many = tuple(f"seg{n:05d}" for n in range(20))
    with pytest.raises(ValueError) as excinfo:
        to_memtomem_namespace(many)
    assert str(NAMESPACE_MAX_CHARS) in str(excinfo.value)
    assert "encodes to" in str(excinfo.value)


def test_empty_namespace_is_refused() -> None:
    with pytest.raises(ValueError):
        to_memtomem_namespace(())


@pytest.mark.parametrize("value", ["YQ", "s", "sYQ", "smfqwc:mfqwc", "smfqwc:s!!", "s=="])
def test_foreign_namespaces_do_not_decode(value: str) -> None:
    with pytest.raises(ValueError):
        from_memtomem_namespace(value)


# --- paths -------------------------------------------------------------------


def test_item_paths_mirror_the_record_layout(tmp_path: Path) -> None:
    paths = item_paths(tmp_path / "records", tmp_path / "projections", ("users", "alice"), "k 1")
    enc = encode_segment
    assert paths.record == tmp_path / "records" / enc("users") / enc("alice") / f"{enc('k 1')}.json"
    assert (
        paths.projection
        == tmp_path / "projections" / enc("users") / enc("alice") / f"{enc('k 1')}.md"
    )
    assert paths.indexed == paths.projection.with_name(paths.projection.name + ".indexed")
    assert paths.deleting == paths.record.with_name(paths.record.name + ".deleting")
    assert item_from_path(tmp_path / "records", paths.record, ".json") == (
        ("users", "alice"),
        "k 1",
    )
    assert item_from_path(tmp_path / "projections", paths.projection, ".md") == (
        ("users", "alice"),
        "k 1",
    )


@pytest.mark.parametrize(
    "relative",
    ["stray.md", "smfqwc/stray.txt", "not-ours!/smfqwc.md", "smfqwc/sYQ.md", "smfqwc/sb.md"],
)
def test_foreign_paths_are_refused(tmp_path: Path, relative: str) -> None:
    with pytest.raises(ValueError):
        item_from_path(tmp_path, tmp_path / relative, ".md")


def test_path_outside_root_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        item_from_path(tmp_path / "a", tmp_path / "b" / "YQ" / "YQ.md", ".md")


# --- the projection file and the markers ------------------------------------


def test_segment_cap_is_exactly_the_documented_byte_length() -> None:
    assert len(encode_segment("x" * 124)) <= SEGMENT_MAX_CHARS
    with pytest.raises(ValueError):
        encode_segment("x" * 125)


def test_projection_file_is_the_value_text_behind_one_empty_line() -> None:
    assert render_projection_file("body") == "\nbody\n"
    assert render_projection_file("body\n") == "\nbody\n"
    # nothing of the store's bookkeeping is in the text
    text = render_projection_file("apples")
    for word in ("langchain", "memtomem", "gen", "lcmm"):
        assert word not in text


def test_indexed_marker_round_trips() -> None:
    marker = IndexedMarker(3, text_digest("\nx\n"), frozenset({("b", "d2"), ("a", "d1")}))
    text = render_indexed_marker(marker)
    assert '"chunks": {"a": "d1", "b": "d2"}' in text


def test_indexed_marker_reads_back(tmp_path: Path) -> None:
    marker = IndexedMarker(3, text_digest("\nx\n"), frozenset({("id-1", "d1"), ("id-2", "d2")}))
    (tmp_path / "m").write_text(render_indexed_marker(marker))
    assert read_indexed_marker(tmp_path / "m") == marker


@pytest.mark.parametrize(
    "content",
    [
        b"",
        b"\xff",
        b"3\n",
        b'{"generation": true, "digest": "d", "chunks": []}',
        b'{"generation": -1, "digest": "d", "chunks": []}',
        b'{"generation": 1, "digest": 5, "chunks": []}',
        b'{"generation": 1, "digest": "d", "chunks": ["id"]}',
        b'{"generation": 1, "digest": "d", "chunks": {"id": 1}}',
        b'{"generation": 1, "digest": "d"}',
    ],
)
def test_indexed_marker_refuses_what_it_did_not_write(tmp_path: Path, content: bytes) -> None:
    (tmp_path / "m").write_bytes(content)
    assert read_indexed_marker(tmp_path / "m") is None


def test_read_marker(tmp_path: Path) -> None:
    assert read_marker(tmp_path / "missing") is None
    for content, expected in (
        (b"12\n", 12),
        (b"0", 0),
        (b"twelve", None),
        (b"\xff", None),
        ("\u00b2".encode(), None),
        (b"012", None),
    ):
        (tmp_path / "m").write_bytes(content)
        assert read_marker(tmp_path / "m") == expected, content


# --- repair table ------------------------------------------------------------


def _state(**overrides: object) -> ItemState:
    base = dict(
        record_gen=2,
        index_off=False,
        projection_present=True,
        projection_current=True,
        marker_present=True,
        indexed_current=True,
        indexed_chunks=frozenset({("c1", "d1")}),
        deleting_gen=None,
        rows=3,
    )
    base.update(overrides)
    return ItemState(**base)  # type: ignore[arg-type]


_NOTHING_DERIVED = dict(
    projection_present=False,
    projection_current=False,
    marker_present=False,
    indexed_current=False,
    indexed_chunks=frozenset(),
    rows=0,
)


@pytest.mark.parametrize(
    ("state", "action"),
    [
        # whole item
        (_state(), Action.NOTHING),
        # put interrupted after the record, before the projection
        (_state(projection_current=False, indexed_current=False), Action.RENDER_AND_INDEX),
        # put interrupted after the projection, before the index (stale rows)
        (_state(indexed_current=False), Action.INDEX),
        # put interrupted after the index, before the marker
        (_state(indexed_current=False, marker_present=True), Action.INDEX),
        # index lost (index.db deleted) while the files agree
        (_state(rows=0), Action.INDEX),
        # rows the marker does not describe (an older index.db restored, or a
        # repair interrupted between its index and its marker)
        (_state(rows_match=False), Action.INDEX),
        # projection edited by hand for a whole record
        (_state(projection_current=False), Action.RENDER_AND_INDEX),
        # first put interrupted after the record: nothing derived yet
        (_state(record_gen=1, **_NOTHING_DERIVED), Action.RENDER_AND_INDEX),
        # index=False put interrupted after the record: the old projection remains
        (
            _state(index_off=True, projection_current=False, indexed_current=False),
            Action.PURGE_PROJECTION,
        ),
        # index=False put interrupted between the two unlinks
        (
            _state(index_off=True, **{**_NOTHING_DERIVED, "marker_present": True}),
            Action.PURGE_PROJECTION,
        ),
        # index=False item, whole
        (_state(index_off=True, **_NOTHING_DERIVED), Action.NOTHING),
        # projection without a record
        (
            _state(record_gen=None, projection_current=False, indexed_current=False),
            Action.PURGE_PROJECTION,
        ),
        # nothing at all
        (_state(record_gen=None, **_NOTHING_DERIVED), Action.NOTHING),
        # delete interrupted after the marker
        (_state(deleting_gen=2), Action.FINISH_DELETE),
        # delete interrupted after the record unlink, before the marker unlink
        (_state(record_gen=None, deleting_gen=2, **_NOTHING_DERIVED), Action.FINISH_DELETE),
        # delete marker superseded by a later put (record gen 3 > marker gen 2)
        (_state(record_gen=3, deleting_gen=2), Action.NOTHING),
        # superseded marker, and the later put was itself interrupted
        (
            _state(record_gen=3, deleting_gen=2, projection_current=False, indexed_current=False),
            Action.RENDER_AND_INDEX,
        ),
    ],
)
def test_classify(state: ItemState, action: Action) -> None:
    assert classify(state) is action


def test_agrees_needs_every_leg() -> None:
    assert _state().agrees
    assert not _state(rows=0).agrees
    assert not _state(indexed_current=False).agrees
    assert not _state(projection_current=False).agrees
    assert not _state(deleting_gen=2).agrees
    assert not _state(index_off=True).agrees
    assert not _state(rows_match=False).agrees
