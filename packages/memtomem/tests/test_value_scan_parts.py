"""Record guards scan the strings inside a stored value raw, not only JSON-escaped (#3300)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memtomem import privacy

# Each is blocked when scanned raw and passes once JSON-escaped inside a value.
RAW_SECRETS = ['db config: {"password": "synthetic-example"}', "BEGIN\nRSA\nPRIVATE\nKEY"]

# (value, expected parts), written out rather than derived. langchain-memtomem's
# test_lcmm_record_guard.py holds its private copy to the same table.
SCAN_PARTS_CASES = [
    ({}, []),
    ({"text": "apple"}, ["text", "apple"]),
    (
        {"b": "x", "a": ["y", {"c": "z"}], "n": 1, "t": True, "nul": None, "f": 1.5},
        ["a", "y", "c", "z", "b", "x", "f", "n", "nul", "t"],
    ),
    ({"k": [[], {}, "", ["deep", ["deeper"]]]}, ["k", "", "deep", "deeper"]),
    ({"줄 ": 'say "hi"\n\t\\'}, ["줄 ", 'say "hi"\n\t\\']),
]


@pytest.mark.parametrize(("value", "expected"), SCAN_PARTS_CASES)
def test_value_scan_parts_fixed_order(value, expected):
    assert privacy.value_scan_parts(value) == expected


def test_value_scan_parts_is_not_recursive():
    value: list = []
    for _ in range(50_000):
        value = [value, "leaf"]
    assert privacy.value_scan_parts({"v": value}) == ["v"] + ["leaf"] * 50_000


@pytest.mark.parametrize("secret", RAW_SECRETS)
def test_fixtures_are_hidden_by_json_escaping(secret):
    assert privacy.scan(secret)
    assert not privacy.scan(json.dumps({"text": secret}, ensure_ascii=False))
    assert privacy.scan("\n---\n".join(privacy.value_scan_parts({"text": secret})))


def _open(root: Path, **kwargs):
    pytest.importorskip("langgraph")
    from memtomem.config import EmbeddingConfig
    from memtomem.integrations.langgraph_store import MemtomemBaseStore

    return MemtomemBaseStore(
        root=root, embedding=EmbeddingConfig(provider="none", dimension=0), **kwargs
    )


def _persisted(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "value",
    [
        lambda s: {"text": s},
        lambda s: {s: "apple"},
        lambda s: {"notes": [{"body": ["apple", s]}]},
    ],
    ids=["string", "dict-key", "nested-list"],
)
@pytest.mark.parametrize("secret", RAW_SECRETS)
def test_basestore_value_strings_scan_raw(tmp_path, caplog, value, secret):
    root = tmp_path / "store"
    store = _open(root)
    try:
        with pytest.raises(ValueError, match="privacy pattern") as error:
            store.put(("docs",), "key", value(secret))
        assert secret not in str(error.value)
        assert store.get(("docs",), "key") is None
    finally:
        store.close()
    assert secret not in caplog.text
    persisted = _persisted(root)
    escaped = json.dumps(secret, ensure_ascii=False)[1:-1]
    assert secret.encode() not in persisted
    assert escaped.encode() not in persisted


@pytest.mark.parametrize("secret", RAW_SECRETS)
def test_basestore_value_strings_force_unsafe(tmp_path, secret):
    store = _open(tmp_path / "store", force_unsafe=True)
    try:
        store.put(("docs",), "key", {"text": secret})
        assert store.get(("docs",), "key").value == {"text": secret}
    finally:
        store.close()


@pytest.mark.parametrize(
    "words", [["password", "= synthetic-example"], ["= synthetic-example", "password"]]
)
def test_basestore_does_not_join_adjacent_value_strings(tmp_path, words):
    assert privacy.scan("\n".join(["password", "= synthetic-example"]))
    store = _open(tmp_path / "store")
    try:
        store.put(("docs",), "key", {"t": words})
        assert store.get(("docs",), "key").value == {"t": words}
    finally:
        store.close()
