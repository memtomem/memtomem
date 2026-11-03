"""The public names ADR-0042 lets a package built on memtomem import.

Each test pins a promoted name to the private object it was born as, so the
two cannot drift, and pins the contract the new name is documented with.
"""

from __future__ import annotations

import base64
from pathlib import Path

import pytest

from memtomem.context import _atomic
from memtomem.integrations import langgraph as lg
from memtomem.integrations.langgraph_store import (
    _encode_segment,
    _projection,
    decode_segment,
    encode_segment,
    render_projection,
)
from memtomem.runtime import files
from memtomem.storage import sqlite_helpers


@pytest.mark.parametrize(
    "value",
    [
        "alice",
        "users",
        "a b",
        "한글 세그먼트",
        "with/slash",
        "trailing-dash-",
        "x" * 146,  # the longest ASCII value that is not hashed
        "\xff\xfe",
    ],
)
def test_segment_codec_round_trips(value: str) -> None:
    token = encode_segment(value)
    assert token.startswith("s") and token == token.lower()
    assert all(c.isascii() and c.isalnum() for c in token)
    assert decode_segment(token) == value


@pytest.mark.parametrize(
    ("value", "token"),
    [("f", "smy"), ("aaa", "smfqwc"), ("aaG", "smfquo"), ("\U0001f642", "s6cpztaq")],
)
def test_segment_codec_tokens(value: str, token: str) -> None:
    # Pairs measured, not derived. ``aaa``/``aaG`` were ``YWFh``/``YWFH`` in
    # base64url, one path on a case-folding filesystem (#3034).
    assert encode_segment(value) == token
    assert decode_segment(token) == value


def test_a_value_too_long_for_a_file_name_is_hashed_and_does_not_decode() -> None:
    token = encode_segment("x" * 147)
    assert token.startswith("h") and len(token) == 53 and token == token.lower()
    assert encode_segment("x" * 147) == token
    assert encode_segment("x" * 148) != token
    with pytest.raises(ValueError):
        decode_segment(token)


def test_encode_segment_is_the_private_encoder() -> None:
    assert _encode_segment is encode_segment
    assert _projection is render_projection


@pytest.mark.parametrize("bad", ["", ".", "..", "a\x00b"])
def test_encode_segment_refuses_unsafe_values(bad: str) -> None:
    with pytest.raises(ValueError):
        encode_segment(bad)


@pytest.mark.parametrize(
    "token",
    [
        "",  # empty
        "s",  # marker without a body; decodes to ""
        "!!!!",  # not in the alphabet
        "Zg",  # the legacy base64url token for "f"
        "SMY",  # upper case is never emitted
        "smz",  # non-canonical trailing bits; naive decoding gives "f"
        "sa",  # a base32 length no input produces
        "sfy",  # decodes to ".", which the encoder refuses
        "saa",  # decodes to NUL
        "sfyxa",  # decodes to ".."
        "s74",  # 0xFF alone: not UTF-8
        "smy=",  # padding is never emitted
        # The unhashed form of a value the encoder hashes.
        "s" + base64.b32encode(b"x" * 147).decode("ascii").rstrip("=").lower(),
    ],
)
def test_decode_segment_refuses_tokens_the_encoder_never_emits(token: str) -> None:
    with pytest.raises(ValueError):
        decode_segment(token)


def test_render_projection_matches_put_semantics() -> None:
    value = {"food": "pizza", "n": 2}
    assert render_projection(value, False) == ""
    assert render_projection(value, None) == render_projection(value, True)
    assert render_projection(value, ["food"]) == "pizza"


def test_scope_resolver_public_names_are_the_private_objects() -> None:
    assert lg.resolve_target_scope is lg._resolve_target_scope
    assert lg.UnregisteredProjectTarget is lg._UnregisteredProjectTarget
    assert lg.TierWouldNotRoundTrip is lg._TierWouldNotRoundTrip
    for name in ("resolve_target_scope", "UnregisteredProjectTarget", "TierWouldNotRoundTrip"):
        assert name in lg.__all__


def test_scope_resolver_raises_its_public_exceptions(tmp_path: Path) -> None:
    # Same shapes as the MemtomemStore tests in test_langgraph.py, through the
    # public name. A ``memories.local`` nested under a registered shared root is
    # owned as ``project_shared`` by directory but classified ``project_local``
    # by shape, so the gates and the stored rows would disagree; a project-shaped
    # path that no registered root covers cannot be gated at all.
    user_dir = tmp_path / ".memtomem" / "memories"
    shared = tmp_path / "proj" / ".memtomem" / "memories"
    nested = shared / "sub" / ".memtomem" / "memories.local" / "x.md"
    with pytest.raises(lg.TierWouldNotRoundTrip):
        lg.resolve_target_scope(nested, [user_dir], [shared])
    unregistered = tmp_path / "other" / ".memtomem" / "memories" / "x.md"
    with pytest.raises(lg.UnregisteredProjectTarget):
        lg.resolve_target_scope(unregistered, [user_dir], [shared])
    assert lg.resolve_target_scope(user_dir / "note.md", [user_dir], [shared]) == "user"


def test_scope_resolver_normalises_its_target(tmp_path: Path, monkeypatch) -> None:
    # The configured roots are resolved; the target must be too, or a symlink
    # into a registered shared directory is gated as ``user`` while the
    # indexer files its rows as ``project_shared``. In-tree callers resolve
    # before calling; the public name cannot assume that.
    user_dir = tmp_path / ".memtomem" / "memories"
    shared = tmp_path / "proj" / ".memtomem" / "memories"
    shared.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(shared, target_is_directory=True)
    direct = lg.resolve_target_scope(shared / "note.md", [user_dir], [shared])
    via_alias = lg.resolve_target_scope(alias / "note.md", [user_dir], [shared])
    assert direct == via_alias == "project_shared"
    # A relative path is resolved against the working directory.
    monkeypatch.chdir(tmp_path)
    relative = lg.resolve_target_scope(
        Path("proj/.memtomem/memories/note.md"), [user_dir], [shared]
    )
    assert relative == "project_shared"


def test_runtime_files_reexports_the_same_objects() -> None:
    assert files.async_file_lock is _atomic.async_file_lock
    assert files.atomic_write_text is _atomic.atomic_write_text
    assert files.memory_lock_path is _atomic.memory_lock_path
    assert files.CRUD_SIDECAR_LOCK_BUDGET_S == _atomic._CRUD_SIDECAR_LOCK_BUDGET_S
    assert files.norm_path is sqlite_helpers.norm_path
    assert set(files.__all__) == {
        "CRUD_SIDECAR_LOCK_BUDGET_S",
        "async_file_lock",
        "atomic_write_text",
        "memory_lock_path",
        "norm_path",
    }


def test_matches_filter_is_the_private_predicate() -> None:
    from memtomem.integrations import langgraph_store as module

    assert module.matches_filter is module._matches_filter
    value = {"n": 3, "meta": {"kind": "fruit"}}
    assert module.matches_filter(value, None)
    assert module.matches_filter(value, {})
    assert module.matches_filter(value, {"n": 3, "meta.kind": "fruit"})
    assert module.matches_filter(value, {"n": {"$gt": 2, "$lte": 3}})
    assert not module.matches_filter(value, {"n": {"$in": [1, 2]}})
    assert not module.matches_filter(value, {"meta.kind": {"$ne": "fruit"}})
    assert not module.matches_filter(value, {"missing": 1})
