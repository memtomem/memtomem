"""Tests for ``memtomem.context.lockfile`` — project install lockfile.

Covers ADR-0008 lockfile schema invariants: dict round-trip preserves
unknown fields, sidecar lock survives concurrent writers, recovery posture
on missing/invalid/unknown-version files.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from memtomem.context.lockfile import (
    LOCKFILE_VERSION,
    Lockfile,
    LockfileVersionError,
)


# ── load() recovery posture ──────────────────────────────────────────────


def test_load_missing_returns_default_v1(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    doc = lock.load()
    assert doc == {"version": LOCKFILE_VERSION}


def test_load_invalid_json_recovers_to_default(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_text("not valid json {{", encoding="utf-8")
    lock = Lockfile.at(project)
    assert lock.load() == {"version": LOCKFILE_VERSION}


def test_load_top_level_not_object_recovers(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    lock = Lockfile.at(project)
    assert lock.load() == {"version": LOCKFILE_VERSION}


def test_load_unknown_version_raises_when_strict(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_text(
        json.dumps({"version": 99, "skills": {"foo": {}}}), encoding="utf-8"
    )
    lock = Lockfile.at(project)
    with pytest.raises(LockfileVersionError, match="version 99"):
        lock.load()


def test_load_unknown_version_returns_dict_when_not_strict(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    payload = {"version": 99, "skills": {"foo": {"compat": "future"}}}
    (project / ".memtomem" / "lock.json").write_text(json.dumps(payload), encoding="utf-8")
    lock = Lockfile.at(project)
    doc = lock.load(strict=False)
    assert doc == payload


# ── upsert / round-trip ──────────────────────────────────────────────────


def test_upsert_creates_file_with_entry(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills",
        "foo",
        wiki_commit="a" * 40,
        installed_at="2026-04-30T12:34:56.123456Z",
    )
    doc = lock.load()
    assert doc["version"] == LOCKFILE_VERSION
    assert doc["skills"]["foo"]["wiki_commit"] == "a" * 40
    assert doc["skills"]["foo"]["installed_at"] == "2026-04-30T12:34:56.123456Z"


def test_upsert_preserves_unknown_top_level_fields(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    seed = {
        "version": LOCKFILE_VERSION,
        "future_root": "preserved",
        "skills": {
            "alpha": {
                "wiki_commit": "b" * 40,
                "installed_at": "2026-01-01T00:00:00.000000Z",
                "compat": "v2",
            }
        },
    }
    (project / ".memtomem" / "lock.json").write_text(json.dumps(seed), encoding="utf-8")

    lock = Lockfile.at(project)
    lock.upsert_entry(
        "skills",
        "beta",
        wiki_commit="c" * 40,
        installed_at="2026-04-30T00:00:00.000000Z",
    )

    doc = lock.load()
    assert doc["future_root"] == "preserved"
    assert doc["skills"]["alpha"]["compat"] == "v2"
    assert doc["skills"]["alpha"]["wiki_commit"] == "b" * 40
    assert doc["skills"]["beta"]["wiki_commit"] == "c" * 40


def test_upsert_replaces_existing_entry_keeping_extras(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    seed = {
        "version": LOCKFILE_VERSION,
        "skills": {
            "foo": {
                "wiki_commit": "old" + "0" * 37,
                "installed_at": "2026-01-01T00:00:00.000000Z",
                "compat": "v2",
            }
        },
    }
    (project / ".memtomem" / "lock.json").write_text(json.dumps(seed), encoding="utf-8")

    lock = Lockfile.at(project)
    lock.upsert_entry(
        "skills",
        "foo",
        wiki_commit="new" + "0" * 37,
        installed_at="2026-04-30T00:00:00.000000Z",
    )

    entry = lock.read_entry("skills", "foo")
    assert entry is not None
    assert entry["wiki_commit"] == "new" + "0" * 37
    assert entry["installed_at"] == "2026-04-30T00:00:00.000000Z"
    assert entry["compat"] == "v2"  # extra preserved through replace


def test_read_entry_returns_none_for_missing(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    assert lock.read_entry("skills", "nonexistent") is None


def test_read_entry_returns_none_for_unknown_section(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills",
        "foo",
        wiki_commit="a" * 40,
        installed_at="2026-04-30T00:00:00.000000Z",
    )
    assert lock.read_entry("agents", "foo") is None


# ── concurrency (real OS-level) ──────────────────────────────────────────


def _upsert_worker(project_str: str, asset_type: str, name: str) -> None:
    """Subprocess body — one upsert per worker, distinct (asset_type, name)."""
    lock = Lockfile.at(Path(project_str))
    lock.upsert_entry(
        asset_type,
        name,
        wiki_commit=f"{name:0<40}"[:40],
        installed_at=f"2026-04-30T00:00:0{name[-1]}.000000Z",
    )


def test_concurrent_upserts_keep_file_valid(tmp_path: Path) -> None:
    """Eight processes upsert distinct keys; all entries must survive
    (sidecar lock + key-disjoint = no loss). ADR-0008 lockfile invariant."""
    project = tmp_path
    (project / ".memtomem").mkdir()

    ctx = mp.get_context("spawn")
    procs = [
        ctx.Process(target=_upsert_worker, args=(str(project), "skills", f"skill{i}"))
        for i in range(8)
    ]
    for p in procs:
        p.start()
    for p in procs:
        p.join(timeout=30)
    for i, p in enumerate(procs):
        assert p.exitcode == 0, f"worker {i} crashed"

    raw = (project / ".memtomem" / "lock.json").read_text()
    doc = json.loads(raw)
    assert doc["version"] == LOCKFILE_VERSION
    skills = doc.get("skills", {})
    for i in range(8):
        name = f"skill{i}"
        assert name in skills, f"missing entry for {name}; got {sorted(skills)}"
        assert "wiki_commit" in skills[name]
        assert "installed_at" in skills[name]
