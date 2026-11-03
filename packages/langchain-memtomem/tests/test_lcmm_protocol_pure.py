"""The pure helpers: namespaces, paths, the projection file, the repair table."""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_memtomem._protocol import (
    GENERATION_TAG_PREFIX,
    NAMESPACE_MAX_CHARS,
    SEGMENT_MARKER,
    SEGMENT_MAX_CHARS,
    Action,
    ItemState,
    classify,
    decode_segment,
    encode_segment,
    from_memtomem_namespace,
    generation_from_tags,
    item_from_path,
    item_paths,
    parse_projection_header,
    read_marker,
    render_projection_file,
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


@pytest.mark.parametrize("value", ["a", "a b", "한글", "-x", "a:b", ".", "..", "x" * 140])
def test_segment_codec_round_trips_canonically(value: str) -> None:
    token = encode_segment(value)
    assert decode_segment(token) == value
    assert token.startswith(SEGMENT_MARKER) and len(token) <= SEGMENT_MAX_CHARS


@pytest.mark.parametrize("token", ["", "s", "sMFQWC", "mfqwc", "s!!", "sb", "sme======"])
def test_decode_refuses_tokens_the_encoder_never_emits(token: str) -> None:
    with pytest.raises(ValueError):
        decode_segment(token)


@pytest.mark.parametrize("value", ["", "a\x00b", "x" * 200])
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


# --- the projection file -----------------------------------------------------


NS_ENC = to_memtomem_namespace(("users", "alice"))
KEY_ENC = encode_segment("note")


def test_projection_file_round_trips_its_header() -> None:
    text = render_projection_file(NS_ENC, KEY_ENC, 7, "body line\nsecond")
    assert text.startswith(
        f"---\ntags: [{GENERATION_TAG_PREFIX}7]\n---\n<!-- langchain-memtomem v1 "
    )
    assert text.endswith("second\n")
    assert parse_projection_header(text) == (NS_ENC, KEY_ENC, 7)
    assert parse_projection_header(render_projection_file(NS_ENC, KEY_ENC, 1, "")) == (
        NS_ENC,
        KEY_ENC,
        1,
    )


def test_a_valid_header_is_what_the_rejections_below_differ_from() -> None:
    # Witness for the parametrized rejections: each case changes one thing.
    assert parse_projection_header(
        f"---\ntags: [lcmm-gen-2]\n---\n<!-- langchain-memtomem v1 ns={NS_ENC} key={KEY_ENC} gen=2 -->\n"
    ) == (NS_ENC, KEY_ENC, 2)


@pytest.mark.parametrize(
    "text",
    [
        "",
        "plain markdown\n",
        # tag and comment name different generations
        f"---\ntags: [lcmm-gen-2]\n---\n<!-- langchain-memtomem v1 ns={NS_ENC} key={KEY_ENC} gen=3 -->\n",
        # another header version
        f"---\ntags: [lcmm-gen-2]\n---\n<!-- langchain-memtomem v2 ns={NS_ENC} key={KEY_ENC} gen=2 -->\n",
        # no frontmatter
        f"<!-- langchain-memtomem v1 ns={NS_ENC} key={KEY_ENC} gen=2 -->\n",
        # a token outside the case-safe alphabet
        "---\ntags: [lcmm-gen-2]\n---\n<!-- langchain-memtomem v1 ns=sYQ key=a2V5 gen=2 -->\n",
    ],
)
def test_projection_header_rejects_torn_or_foreign_files(text: str) -> None:
    assert parse_projection_header(text) is None


def test_generation_from_tags() -> None:
    assert generation_from_tags(("x", "lcmm-gen-4")) == 4
    assert generation_from_tags(()) is None
    assert generation_from_tags(("lcmm-gen-4", "lcmm-gen-5")) is None
    assert generation_from_tags(("lcmm-gen-x",)) is None


# --- markers -----------------------------------------------------------------


def test_read_marker(tmp_path: Path) -> None:
    assert read_marker(tmp_path / "missing") is None
    (tmp_path / "m").write_text("12\n")
    assert read_marker(tmp_path / "m") == 12
    (tmp_path / "bad").write_text("twelve")
    assert read_marker(tmp_path / "bad") is None


# --- repair table ------------------------------------------------------------


def _state(**overrides: object) -> ItemState:
    base = dict(
        record_gen=2,
        index_off=False,
        projection_present=True,
        projection_gen=2,
        marker_gen=2,
        deleting_gen=None,
        rows=3,
    )
    base.update(overrides)
    return ItemState(**base)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("state", "action"),
    [
        # whole item
        (_state(), Action.NOTHING),
        # put interrupted after the record, before the projection (gen 2 vs 1)
        (_state(projection_gen=1, marker_gen=1), Action.RENDER_AND_INDEX),
        # put interrupted after the projection, before the index (stale rows)
        (_state(marker_gen=1), Action.INDEX),
        # put interrupted after the index, before the marker
        (_state(marker_gen=None), Action.INDEX),
        # index lost (index.db deleted) while the files agree
        (_state(rows=0), Action.INDEX),
        # projection unparsable for a whole record
        (_state(projection_gen=None), Action.RENDER_AND_INDEX),
        # first put interrupted after the record: nothing derived yet
        (
            _state(
                record_gen=1, projection_present=False, projection_gen=None, marker_gen=None, rows=0
            ),
            Action.RENDER_AND_INDEX,
        ),
        # index=False put interrupted after the record: projection of gen 1 remains
        (_state(index_off=True, projection_gen=1, marker_gen=1), Action.PURGE_PROJECTION),
        # index=False put interrupted after the projection unlink, before the marker unlink
        (
            _state(
                index_off=True, projection_present=False, projection_gen=None, marker_gen=1, rows=0
            ),
            Action.PURGE_PROJECTION,
        ),
        # index=False item, whole
        (
            _state(
                index_off=True,
                projection_present=False,
                projection_gen=None,
                marker_gen=None,
                rows=0,
            ),
            Action.NOTHING,
        ),
        # projection without a record
        (_state(record_gen=None), Action.PURGE_PROJECTION),
        # nothing at all
        (
            _state(
                record_gen=None,
                projection_present=False,
                projection_gen=None,
                marker_gen=None,
                rows=0,
            ),
            Action.NOTHING,
        ),
        # delete interrupted after the marker
        (_state(deleting_gen=2), Action.FINISH_DELETE),
        # delete interrupted after the record unlink, before the marker unlink
        (
            _state(
                record_gen=None,
                deleting_gen=2,
                projection_present=False,
                projection_gen=None,
                marker_gen=None,
                rows=0,
            ),
            Action.FINISH_DELETE,
        ),
        # delete marker superseded by a later put (record gen 3 > marker gen 2)
        (_state(record_gen=3, projection_gen=3, marker_gen=3, deleting_gen=2), Action.NOTHING),
        # superseded marker, and the later put was itself interrupted
        (_state(record_gen=3, deleting_gen=2), Action.RENDER_AND_INDEX),
    ],
)
def test_classify(state: ItemState, action: Action) -> None:
    assert classify(state) is action


def test_agrees_needs_every_leg() -> None:
    assert _state().agrees
    assert not _state(rows=0).agrees
    assert not _state(marker_gen=1).agrees
    assert not _state(deleting_gen=2).agrees
    assert not _state(index_off=True).agrees
