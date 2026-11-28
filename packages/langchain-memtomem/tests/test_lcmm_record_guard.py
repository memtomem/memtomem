"""The record guard scans the strings inside a value raw, not only JSON-escaped (#3300)."""

from __future__ import annotations

import json

import pytest
from memtomem import privacy

from langchain_memtomem._records import RecordStore, _value_scan_parts

# Each is blocked when scanned raw and passes once JSON-escaped inside a value.
RAW_SECRETS = ['db config: {"password": "synthetic-example"}', "BEGIN\nRSA\nPRIVATE\nKEY"]

# Written out rather than derived, and asserted against both this package's
# copy and memtomem's helper: two identically wrong walkers cannot pass.
SCAN_PARTS_CASES = [
    ({}, []),
    ({"text": "apple"}, ["text", "apple"]),
    (
        {"b": "x", "a": ["y", {"c": "z"}], "n": 1, "t": True, "nul": None, "f": 1.5},
        ["a", "y", "c", "z", "b", "x", "f", "n", "nul", "t"],
    ),
    ({"k": [[], {}, "", ["deep", ["deeper"]]]}, ["k", "", "deep", "deeper"]),
    ({"줄\u2028": 'say "hi"\n\t\\'}, ["줄\u2028", 'say "hi"\n\t\\']),
]


@pytest.mark.parametrize(("value", "expected"), SCAN_PARTS_CASES)
def test_copy_matches_memtomem_helper(value, expected):
    assert _value_scan_parts(value) == expected
    assert privacy.value_scan_parts(value) == expected


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
@pytest.mark.parametrize("scope", ["user", "project_shared"])
def test_record_guard_scans_value_strings_raw(tmp_path, caplog, value, secret, scope):
    records = RecordStore(tmp_path / "records", scope=scope, force_unsafe=False)
    with pytest.raises(ValueError, match="privacy pattern") as error:
        records.prepare(("docs",), "key", value(secret), None)
    assert secret not in str(error.value)
    assert secret not in caplog.text
    assert not (tmp_path / "records").exists() or not any((tmp_path / "records").rglob("*"))


@pytest.mark.parametrize("secret", RAW_SECRETS)
def test_force_unsafe_bypasses_outside_project_shared(tmp_path, secret):
    records = RecordStore(tmp_path / "records", scope="user", force_unsafe=True)
    assert records.prepare(("docs",), "key", {"text": secret}, None).value == {"text": secret}
    shared = RecordStore(tmp_path / "shared", scope="project_shared", force_unsafe=True)
    with pytest.raises(ValueError, match="privacy pattern"):
        shared.prepare(("docs",), "key", {"text": secret}, None)


@pytest.mark.parametrize(
    "words", [["password", "= synthetic-example"], ["= synthetic-example", "password"]]
)
def test_record_guard_does_not_join_adjacent_value_strings(tmp_path, words):
    assert privacy.scan("\n".join(["password", "= synthetic-example"]))
    records = RecordStore(tmp_path / "records", scope="user", force_unsafe=False)
    assert records.prepare(("docs",), "key", {"t": words}, None).value == {"t": words}


def test_fixtures_are_hidden_by_json_escaping():
    for secret in RAW_SECRETS:
        assert privacy.scan(secret)
        assert not privacy.scan(json.dumps({"text": secret}, ensure_ascii=False))


@pytest.mark.parametrize("index", [None, ["other"], False])
@pytest.mark.parametrize("secret", RAW_SECRETS)
async def test_put_refuses_value_strings_the_projection_does_not_show(store, index, secret):
    # The projection guard sees a selected string field raw, so it already
    # refused index=["text"]; these shapes reached the record unscanned.
    with pytest.raises(ValueError, match="store write blocked"):
        await store.aput(("docs",), "key", {"text": secret, "other": "x"}, index=index)
    assert await store.aget(("docs",), "key") is None
    assert not any(store.records_dir.rglob("*.json"))
