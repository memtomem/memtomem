"""Tests for ``memtomem.context.lockfile`` — project install lockfile.

Covers ADR-0008 lockfile schema invariants: dict round-trip preserves
unknown fields, sidecar lock survives concurrent writers, recovery posture
on missing/invalid/unknown-version files — strict (default) reads refuse a
corrupt file so write paths can never persist a silent reset (#1247 id 16);
only ``strict=False`` diagnostic reads degrade to the empty default.
"""

from __future__ import annotations

import json
import multiprocessing as mp
from pathlib import Path

import pytest

from memtomem.context.lockfile import (
    LOCKFILE_VERSION,
    Lockfile,
    LockfileCorruptError,
    LockfileError,
    LockfileVersionError,
)


# ── load() recovery posture ──────────────────────────────────────────────


def test_load_missing_returns_default_v1(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    doc = lock.load()
    assert doc == {"version": LOCKFILE_VERSION}


def test_load_invalid_json_raises_when_strict(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_text("not valid json {{", encoding="utf-8")
    lock = Lockfile.at(project)
    with pytest.raises(LockfileCorruptError, match="not valid JSON"):
        lock.load()


def test_load_invalid_utf8_raises_when_strict(tmp_path: Path) -> None:
    """``json.loads(bytes)`` decodes before parsing — invalid UTF-8 raises
    ``UnicodeDecodeError``, not ``JSONDecodeError``; both are the same
    corrupt-file class (Codex design review)."""
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_bytes(b"\xff")
    lock = Lockfile.at(project)
    with pytest.raises(LockfileCorruptError, match="not valid JSON"):
        lock.load()
    assert lock.load(strict=False) == {"version": LOCKFILE_VERSION}


def test_load_top_level_not_object_raises_when_strict(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    lock = Lockfile.at(project)
    with pytest.raises(LockfileCorruptError, match="not a JSON object"):
        lock.load()


def test_load_corrupt_recovers_to_default_when_not_strict(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    lock_json = project / ".memtomem" / "lock.json"
    lock = Lockfile.at(project)

    lock_json.write_text("not valid json {{", encoding="utf-8")
    assert lock.load(strict=False) == {"version": LOCKFILE_VERSION}

    lock_json.write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert lock.load(strict=False) == {"version": LOCKFILE_VERSION}


def test_corrupt_and_version_errors_share_lockfile_error_base() -> None:
    """Degrading surfaces (status, CLI) catch the base in one clause."""
    assert issubclass(LockfileCorruptError, LockfileError)
    assert issubclass(LockfileVersionError, LockfileError)


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


# ── remove_entry (#1123 B4-1) ─────────────────────────────────────────────


def test_remove_entry_deletes_and_returns_true(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills", "foo", wiki_commit="a" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    assert lock.read_entry("skills", "foo") is not None

    assert lock.remove_entry("skills", "foo") is True
    assert lock.read_entry("skills", "foo") is None


def test_remove_entry_absent_returns_false_and_leaves_file_untouched(tmp_path: Path) -> None:
    lock = Lockfile.at(tmp_path)
    # No lockfile yet: removing is a no-op and must NOT create the file.
    assert lock.remove_entry("skills", "ghost") is False
    assert not lock.path.exists()

    lock.upsert_entry(
        "agents", "keep", wiki_commit="b" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    before = lock.path.read_bytes()
    # Existing file, but neither the section nor the name matches → no rewrite.
    assert lock.remove_entry("commands", "ghost") is False
    assert lock.remove_entry("agents", "ghost") is False
    assert lock.path.read_bytes() == before  # byte-identical: mtime/content untouched


def test_remove_entry_preserves_siblings_and_unknown_fields(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    seed = {
        "version": LOCKFILE_VERSION,
        "future_root": "preserved",
        "skills": {
            "alpha": {"wiki_commit": "a" * 40, "installed_at": "2026-01-01T00:00:00.000000Z"},
            "beta": {
                "wiki_commit": "b" * 40,
                "installed_at": "2026-01-02T00:00:00.000000Z",
                "compat": "v2",
            },
        },
    }
    (project / ".memtomem" / "lock.json").write_text(json.dumps(seed), encoding="utf-8")

    lock = Lockfile.at(project)
    assert lock.remove_entry("skills", "alpha") is True

    doc = lock.load()
    assert "alpha" not in doc["skills"]
    assert doc["skills"]["beta"]["wiki_commit"] == "b" * 40
    assert doc["skills"]["beta"]["compat"] == "v2"  # unknown per-entry field kept
    assert doc["future_root"] == "preserved"  # unknown top-level field kept


# ── B1: file manifest fields (#1247) ─────────────────────────────────────


def test_upsert_records_manifest_keys(tmp_path: Path) -> None:
    """``files`` is stored sorted (POSIX relpaths) with its pairing
    ``files_commit`` so consumers can detect stale manifests."""
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills",
        "web",
        wiki_commit="a" * 40,
        installed_at="2026-06-11T00:00:00.000000Z",
        files=["scripts/run.py", "SKILL.md"],
        files_commit="a" * 40,
    )

    doc = json.loads((tmp_path / ".memtomem" / "lock.json").read_text(encoding="utf-8"))
    entry = doc["skills"]["web"]
    assert entry["files"] == ["SKILL.md", "scripts/run.py"]
    assert entry["files_commit"] == "a" * 40


def test_upsert_without_manifest_preserves_existing_manifest_keys(tmp_path: Path) -> None:
    """Omitting ``files`` on a later upsert must not strip a previously
    recorded manifest — same unknown-key preservation contract as the rest
    of the entry; staleness is handled by the ``files_commit`` guard."""
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills",
        "web",
        wiki_commit="a" * 40,
        installed_at="2026-06-11T00:00:00.000000Z",
        files=["SKILL.md"],
        files_commit="a" * 40,
    )
    lock.upsert_entry(
        "skills",
        "web",
        wiki_commit="b" * 40,
        installed_at="2026-06-12T00:00:00.000000Z",
    )

    doc = json.loads((tmp_path / ".memtomem" / "lock.json").read_text(encoding="utf-8"))
    entry = doc["skills"]["web"]
    assert entry["files"] == ["SKILL.md"]
    assert entry["files_commit"] == "a" * 40  # now stale — guard ignores it
    assert entry["wiki_commit"] == "b" * 40


# ── corrupt-file write refusal (#1247 id 16) ─────────────────────────────


def test_upsert_over_corrupt_file_refuses_and_preserves_bytes(tmp_path: Path) -> None:
    """A corrupt lockfile (e.g. git merge-conflict markers in a tracked
    ``.memtomem/``) must refuse the upsert — pre-fix, the tolerant load
    reset the doc and the write persisted it with ONLY the new entry,
    wiping every sibling asset's install record."""
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills", "alpha", wiki_commit="a" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    lock.upsert_entry(
        "agents", "beta", wiki_commit="b" * 40, installed_at="2026-01-02T00:00:00.000000Z"
    )

    corrupt = b'<<<<<<< HEAD\n{"version": 1}\n=======\n'
    lock.path.write_bytes(corrupt)

    with pytest.raises(LockfileCorruptError, match="not valid JSON"):
        lock.upsert_entry(
            "skills", "gamma", wiki_commit="c" * 40, installed_at="2026-01-03T00:00:00.000000Z"
        )
    assert lock.path.read_bytes() == corrupt  # refusal left the file byte-identical


def test_upsert_transient_oserror_refuses_and_siblings_survive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One transient read failure (AV scanner / EACCES) during the in-lock
    load must refuse the upsert — pre-fix it silently re-baselined and the
    written file contained only the new entry."""
    lock = Lockfile.at(tmp_path)
    lock.upsert_entry(
        "skills", "alpha", wiki_commit="a" * 40, installed_at="2026-01-01T00:00:00.000000Z"
    )
    lock.upsert_entry(
        "skills", "beta", wiki_commit="b" * 40, installed_at="2026-01-02T00:00:00.000000Z"
    )

    real_read_bytes = Path.read_bytes
    tripped = {"done": False}

    def flaky_read_bytes(self: Path) -> bytes:
        if self == lock.path and not tripped["done"]:
            tripped["done"] = True
            raise PermissionError(13, "transient access denied", str(self))
        return real_read_bytes(self)

    monkeypatch.setattr(Path, "read_bytes", flaky_read_bytes)
    with pytest.raises(LockfileCorruptError, match="unreadable"):
        lock.upsert_entry(
            "skills", "gamma", wiki_commit="c" * 40, installed_at="2026-01-03T00:00:00.000000Z"
        )

    doc = lock.load()
    assert set(doc["skills"]) == {"alpha", "beta"}  # siblings survived the refusal


def test_remove_entry_over_corrupt_file_refuses(tmp_path: Path) -> None:
    project = tmp_path
    (project / ".memtomem").mkdir()
    corrupt = b"not valid json {{"
    lock_json = project / ".memtomem" / "lock.json"
    lock_json.write_bytes(corrupt)
    lock = Lockfile.at(project)

    with pytest.raises(LockfileCorruptError):
        lock.remove_entry("skills", "anything")
    assert lock_json.read_bytes() == corrupt


def test_read_paths_raise_over_corrupt_file(tmp_path: Path) -> None:
    """``read_entry`` feeds install's already-installed check and update's
    not-installed check; a tolerant ``None`` there produced the
    AlreadyInstalled/NotInstalled wedge where each command points at the
    other. Raising names the real problem."""
    project = tmp_path
    (project / ".memtomem").mkdir()
    (project / ".memtomem" / "lock.json").write_text("not valid json {{", encoding="utf-8")
    lock = Lockfile.at(project)

    with pytest.raises(LockfileCorruptError):
        lock.read_entry("skills", "foo")
    with pytest.raises(LockfileCorruptError):
        list(lock.iter_entries())
