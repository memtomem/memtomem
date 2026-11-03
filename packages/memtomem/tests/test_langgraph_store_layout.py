"""MemtomemBaseStore's case-safe record layout and the migration into it (#3034).

The earlier layout named files in base64url, which is case-sensitive: on a
filesystem that folds case (APFS as macOS formats it, NTFS) ``aaa`` (``YWFh``)
and ``aaG`` (``YWFH``) named one path. The issue scenarios below are real
witnesses only on such a filesystem; the single-case and migration tests build
their evidence by hand so that they witness on Linux too.
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

import pytest

pytest.importorskip("langgraph")

from memtomem.config import EmbeddingConfig
from memtomem.context._atomic import _file_lock
from memtomem.integrations import langgraph_store
from memtomem.integrations.langgraph_store import (
    MemtomemBaseStore,
    _legacy_encode_segment,
    encode_segment,
    render_projection,
)

LOGGER = "memtomem.integrations.langgraph_store"

# A file name of about 240 characters under a deep tmp_path is past the legacy
# MAX_PATH (260) on Windows, which is the fixture's limit, not the codec's. The
# hashed cases (53-character names) still run there.
_WIN_LONG_NAME = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only FIXTURE: a ~240-character file name under tmp_path exceeds "
    "Windows MAX_PATH (260); hashed names are covered on every platform.",
)


def _open(root: Path) -> MemtomemBaseStore:
    return MemtomemBaseStore(root=root, embedding=EmbeddingConfig(provider="none", dimension=0))


def _legacy(
    root: Path,
    namespace: tuple[str, ...],
    key: str,
    value: dict,
    *,
    at: tuple[str, ...] | None = None,
    generation: int | None = None,
    newline: str = "\n",
) -> Path:
    """Write a record the way the base64url layout did, optionally at another path."""
    record: dict = {
        "schema_version": 1,
        "namespace": list(namespace),
        "key": key,
        "value": value,
        "index": None,
        "projection": render_projection(value, None),
        "created_at": "2026-01-02T03:04:05.000006+00:00",
        "updated_at": "2026-01-02T03:04:05.000007+00:00",
    }
    if generation is not None:
        record["generation"] = generation
    parts = at or (
        *(_legacy_encode_segment(s) for s in namespace),
        f"{_legacy_encode_segment(key)}.json",
    )
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and p.name != ".layout.lock"
    }


def _quarantined_values(root: Path) -> list[dict]:
    directory = root / "quarantine.v2"
    files = sorted(directory.glob("*.json.conflict")) if directory.exists() else []
    return sorted((json.loads(p.read_text())["value"] for p in files), key=lambda v: v["v"])


def _folds_case(directory: Path) -> bool:
    probe = directory / "case-probe"
    probe.write_text("")
    try:
        return (directory / "CASE-PROBE").exists()
    finally:
        probe.unlink()


# -- the issue's two scenarios -------------------------------------------------


def test_keys_whose_old_names_differed_only_in_case_coexist(tmp_path):
    store = _open(tmp_path / "store")
    store.put(("ns",), "aaa", {"v": 1})
    store.put(("ns",), "aaG", {"v": 2})
    assert store.get(("ns",), "aaa").value == {"v": 1}
    assert store.get(("ns",), "aaG").value == {"v": 2}
    assert sorted(item.key for item in store.search(("ns",))) == ["aaG", "aaa"]


def test_namespaces_whose_old_names_differed_only_in_case_are_both_listed(tmp_path):
    store = _open(tmp_path / "store")
    store.put(("aaa",), "k1", {"v": "lower"})
    store.put(("aaG",), "k2", {"v": "upper"})
    assert store.list_namespaces() == [("aaG",), ("aaa",)]
    assert [item.key for item in store.search(("aaG",))] == ["k2"]
    assert [item.key for item in store.search(("aaa",))] == ["k1"]


def test_every_name_under_records_has_one_case(tmp_path):
    # The witness that does not need a case-folding filesystem: base64url
    # names fail it on Linux too.
    store = _open(tmp_path / "store")
    pairs = [("aaa", "aaG"), ("alice", "Alice"), ("YWFh", "ywfh")]
    for lower, upper in pairs:
        assert encode_segment(lower).lower() != encode_segment(upper).lower()
        store.put((lower,), upper, {"v": 1})
        store.put((upper,), lower, {"v": 2})
    names = [p.name for p in (store.root / "records.v2").rglob("*")]
    assert len(names) == 4 * len(pairs)
    assert all(name == name.lower() for name in names)


# -- the hash fallback, through real writes -----------------------------------


@pytest.mark.parametrize(
    ("value", "hashed"),
    [
        pytest.param("x" * 146, False, marks=_WIN_LONG_NAME),  # a 235-character token
        ("x" * 147, True),  # 147 bytes: 237 would not fit beside the temp-file name
        pytest.param("é" * 73, False, marks=_WIN_LONG_NAME),  # 2-byte UTF-8, 146 bytes
        ("é" * 74, True),
        pytest.param("\U0001f642" * 36 + "xx", False, marks=_WIN_LONG_NAME),  # 4-byte, 146 bytes
        ("\U0001f642" * 37, True),
    ],
)
@pytest.mark.parametrize("role", ["key", "namespace"])
def test_long_values_round_trip_as_keys_and_namespaces(tmp_path, value, hashed, role):
    store = _open(tmp_path / "store")
    if role == "key":
        namespace, key = ("ns",), value
    else:
        namespace, key = (value, "inner"), "k"
    store.put(namespace, key, {"v": value})
    assert store.get(namespace, key).value == {"v": value}
    assert [item.value for item in store.search(namespace[:1])] == [{"v": value}]
    assert store.list_namespaces() == [namespace]
    token = encode_segment(value)
    assert token.startswith("h" if hashed else "s")
    assert token in store.record_path(namespace, key).relative_to(store.root).as_posix()


# -- migration ----------------------------------------------------------------


def test_open_moves_legacy_records_and_keeps_their_bytes(tmp_path, caplog):
    root = tmp_path / "store"
    originals = {
        (("users", "alice"), "prefs"): _legacy(
            root, ("users", "alice"), "prefs", {"food": "pizza"}, generation=7
        ),
        (("users", "bob"), "prefs"): _legacy(
            root, ("users", "bob"), "prefs", {"food": "rice"}, newline="\r\n"
        ),
        ((), "top"): _legacy(root, (), "top", {"v": 0}),
    }
    raw = {identity: path.read_bytes() for identity, path in originals.items()}

    with caplog.at_level(logging.INFO, logger=LOGGER):
        store = _open(root)
    assert "Moved 3 LangGraph record(s)" in caplog.text
    assert "conflicting" not in caplog.text

    for (namespace, key), data in raw.items():
        assert store.record_path(namespace, key).read_bytes() == data  # CRLF included
    item = store.get(("users", "alice"), "prefs")
    assert item.value == {"food": "pizza"}
    assert item.created_at.isoformat() == "2026-01-02T03:04:05.000006+00:00"
    assert store.read_record(("users", "alice"), "prefs")["generation"] == 7
    assert store.list_namespaces() == [(), ("users", "alice"), ("users", "bob")]
    assert [i.key for i in store.search(("users",), query="pizza")][:1] == ["prefs"]
    assert sorted(p.name for p in root.iterdir()) == [".layout.lock", "records.v2"]

    before = _snapshot(root)
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=LOGGER):
        _open(root)
    assert [r for r in caplog.records if r.name == LOGGER] == []
    assert _snapshot(root) == before


def test_a_fresh_store_creates_nothing_until_it_writes(tmp_path):
    root = tmp_path / "store"
    store = _open(root)
    assert list(root.iterdir()) == []
    assert store.search(()) == [] and store.list_namespaces() == []


def test_a_record_under_a_case_variant_directory_moves_only_where_case_folds(tmp_path):
    # What a case-folding filesystem left behind: ("aaG",) was written into
    # the YWFh/ directory that ("aaa",) had created. The old scan skipped it.
    # There the variant spelling YWFH/ is the same file and the record comes
    # back; on a case-sensitive filesystem it is a different path, which this
    # store never wrote, and it stays put.
    root = tmp_path / "store"
    root.mkdir()
    folds = _folds_case(root)
    _legacy(root, ("aaa",), "k1", {"v": "lower"})
    variant = _legacy(root, ("aaG",), "k2", {"v": "upper"}, at=("YWFh", "azI.json"))
    store = _open(root)
    if folds:
        assert store.list_namespaces() == [("aaG",), ("aaa",)]
        assert [item.value for item in store.search(("aaG",))] == [{"v": "upper"}]
        assert not (root / "YWFh").exists()
    else:
        assert store.list_namespaces() == [("aaa",)]
        assert variant.exists()
        assert _quarantined_values(root) == []


@_WIN_LONG_NAME
def test_long_legacy_names_land_hashed(tmp_path):
    root = tmp_path / "store"
    segment, key = "n" * 180, "k" * 180  # legacy tokens of 240 characters
    _legacy(root, (segment,), key, {"v": 1})
    store = _open(root)
    assert store.get((segment,), key).value == {"v": 1}
    parts = store.record_path((segment,), key).relative_to(store.root / "records.v2").parts
    assert [p[0] for p in parts] == ["h", "h"]


def test_a_failed_write_leaves_the_legacy_record_and_the_next_open_finishes(tmp_path, monkeypatch):
    root = tmp_path / "store"
    legacy = _legacy(root, ("ns",), "k", {"v": 1})
    original = legacy.read_bytes()

    def failing_write(path, data):
        raise OSError("disk full")

    monkeypatch.setattr(langgraph_store, "atomic_write_bytes", failing_write)
    with pytest.raises(OSError, match="disk full"):
        _open(root)
    assert legacy.read_bytes() == original  # a delete-first migration would fail here
    monkeypatch.undo()

    assert _open(root).get(("ns",), "k").value == {"v": 1}
    assert not legacy.exists()


def test_a_failed_unlink_leaves_two_copies_and_the_next_open_drops_the_legacy_one(
    tmp_path, monkeypatch
):
    root = tmp_path / "store"
    legacy = _legacy(root, ("ns",), "k", {"v": 1})
    real_unlink = Path.unlink

    def failing_unlink(self, missing_ok=False):
        if self == legacy:
            raise OSError("interrupted")
        return real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", failing_unlink)
    with pytest.raises(OSError, match="interrupted"):
        _open(root)
    monkeypatch.undo()
    assert legacy.exists()

    store = _open(root)
    assert not legacy.exists()
    assert store.get(("ns",), "k").value == {"v": 1}
    assert _quarantined_values(root) == []


def test_a_conflicting_legacy_record_is_quarantined_and_cannot_come_back(tmp_path, caplog):
    root = tmp_path / "store"
    _open(root).put(("ns",), "k", {"v": "current"})
    # An older memtomem, still running, writes the old layout afterwards.
    _legacy(root, ("ns",), "k", {"v": "stale"})

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store = _open(root)
    assert "moved 1 conflicting legacy record(s)" in caplog.text
    assert store.get(("ns",), "k").value == {"v": "current"}
    assert _quarantined_values(root) == [{"v": "stale"}]

    store.delete(("ns",), "k")
    assert _open(root).get(("ns",), "k") is None, "a delete must not be undone on reopen"

    _open(root).put(("ns",), "k", {"v": "again"})
    _legacy(root, ("ns",), "k", {"v": "stale two"})
    _open(root)
    assert _quarantined_values(root) == [{"v": "stale"}, {"v": "stale two"}]


def test_a_case_variant_beside_the_genuine_record_is_left_alone(tmp_path):
    root = tmp_path / "store"
    root.mkdir()
    if _folds_case(root):
        pytest.skip("two names that differ only in case need a case-sensitive filesystem")
    _legacy(root, ("aaa",), "k1", {"v": "genuine"})
    one = _legacy(root, ("aaa",), "k1", {"v": "one"}, at=("YWFH", "azE.json"))
    two = _legacy(root, ("aaa",), "k1", {"v": "two"}, at=("ywfh", "azE.json"))
    store = _open(root)
    assert store.get(("aaa",), "k1").value == {"v": "genuine"}
    assert one.exists() and two.exists()
    assert _quarantined_values(root) == []
    # Nothing a later open could pick: the variants are never import candidates.
    store.delete(("aaa",), "k1")
    assert _open(root).get(("aaa",), "k1") is None


def test_the_filesystem_not_path_equality_decides_the_legacy_path(tmp_path, monkeypatch):
    # WindowsPath("YWFH/…") == WindowsPath("YWFh/…") even in a case-sensitive
    # directory, so path equality alone would admit a different file.
    root = tmp_path / "store"
    legacy = _legacy(root, ("ns",), "k", {"v": 1})
    monkeypatch.setattr(langgraph_store.os.path, "samefile", lambda a, b: False)
    store = _open(root)
    assert store.get(("ns",), "k") is None
    assert legacy.exists()


def test_unreadable_and_misplaced_files_stay_where_they_are(tmp_path, caplog):
    root = tmp_path / "store"
    root.mkdir()
    (root / "bad.json").write_text("{broken")
    (root / "foreign").mkdir()
    (root / "foreign" / "data.json").write_text('{"a": 1}')
    misplaced = _legacy(root, ("aaa",), "k1", {"v": 1}, at=("Zm9v", "azE.json"))
    before = _snapshot(root)

    with caplog.at_level(logging.WARNING, logger=LOGGER):
        store = _open(root)
    assert "left 3 legacy file(s) it could not import" in caplog.text
    assert _snapshot(root) == before
    assert misplaced.exists()
    assert store.get(("aaa",), "k1") is None
    assert store.list_namespaces() == []


def test_a_held_layout_lock_stops_the_migration_until_released(tmp_path, monkeypatch):
    root = tmp_path / "store"
    _legacy(root, ("ns",), "k", {"v": 1})
    before = _snapshot(root)
    monkeypatch.setattr(langgraph_store, "_LAYOUT_LOCK_TIMEOUT_S", 0.2)
    with _file_lock(root / ".layout.lock", timeout=5):
        with pytest.raises(TimeoutError):
            _open(root)
        assert _snapshot(root) == before
    assert _open(root).get(("ns",), "k").value == {"v": 1}


def test_a_refused_construction_leaves_a_legacy_tree_untouched(tmp_path):
    root = tmp_path / "store"
    _legacy(root, ("ns",), "k", {"v": 1})
    before = _snapshot(root)
    with pytest.raises(ValueError, match="confirm_project_shared"):
        MemtomemBaseStore(root=root, scope="project_shared")
    assert _snapshot(root) == before
    assert not (root / ".layout.lock").exists()


def test_a_corrupt_v2_record_is_not_replaced_by_a_legacy_copy(tmp_path):
    root = tmp_path / "store"
    store = _open(root)
    store.put(("ns",), "k", {"v": "current"})
    target = store.record_path(("ns",), "k")
    target.write_bytes(b"\xff")
    _legacy(root, ("ns",), "k", {"v": "stale"})
    _open(root)
    assert target.read_bytes() == b"\xff", "the corrupt file is left for a person"
    assert _quarantined_values(root) == [{"v": "stale"}]


def _symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError as exc:  # Windows without the symlink privilege
        pytest.skip(f"cannot create a symlink here: {exc}")


def test_a_symlinked_legacy_record_is_not_followed(tmp_path):
    root = tmp_path / "store"
    outside = _legacy(tmp_path / "outside", ("ns",), "k", {"v": "outside"})
    link = root / _legacy_encode_segment("ns") / f"{_legacy_encode_segment('k')}.json"
    link.parent.mkdir(parents=True)
    _symlink(link, outside)
    store = _open(root)
    assert store.get(("ns",), "k") is None
    assert link.is_symlink() and outside.exists()


def test_a_symlinked_destination_is_written_through_like_put(tmp_path):
    # A destination symlink is the owner's own setup and ``put`` already
    # writes through it, so the migration does too. Skipping instead would
    # leave the legacy copy behind, and a later delete of the newer record
    # would be undone once the symlink went away.
    root = tmp_path / "store"
    _open(root).put(("ns",), "k", {"v": "current"})
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _symlink(root / "quarantine.v2", elsewhere)
    _legacy(root, ("ns",), "k", {"v": "stale"})

    store = _open(root)
    assert [json.loads(p.read_text())["value"] for p in elsewhere.iterdir()] == [{"v": "stale"}]
    store.delete(("ns",), "k")
    (root / "quarantine.v2").unlink()
    assert _open(root).get(("ns",), "k") is None, "a delete must not be undone on reopen"
