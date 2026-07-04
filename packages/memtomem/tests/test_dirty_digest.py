"""Per-file content-digest dirty detection (#1247 id 15).

The defect: all three install sites captured ``installed_at`` from the
dest tree AFTER the copy (``max st_mtime``), so a concurrent edit landing
between a file's write and that capture sat at ``mtime <= installed_at``
— classified clean **permanently**, and the next update silently
overwrote the user's bytes. The fix records a SHA-256 per installed file,
computed from the in-memory bytes the copier wrote, and the dirty check
compares bytes instead of mtimes whenever the entry carries a valid map
(``digests`` paired to the entry's own ``installed_at`` via
``digests_installed_at``).

Test discipline (campaign convention):

- The bug-class and semantic-delta tests fail against main's src tree
  (validated via ``git restore --source origin/main --worktree -- <src>``
  before the fix commit is finalized).
- Negative pins documenting degrade paths pass on both trees where the
  shapes allow it; the load-bearing guards (digest compare, pairing
  equality, clear-on-omit) are additionally mutation-validated — see the
  PR description for the exact mutations and which pins go red.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memtomem.context import _atomic as atomic_module
from memtomem.context._atomic import DIRTY_SKIP_SUFFIXES, copy_tree_atomic
from memtomem.context.dirty import is_asset_dirty
from memtomem.context.install import (
    StaleInstallError,
    _reconcile_removed_files,
    install_skill,
    update_skill,
)
from memtomem.context.lockfile import Lockfile, digests_from_entry, manifest_from_entry
from memtomem.context.privacy_scan import PrivacyScanReadError
from memtomem.wiki.store import WikiStore

requires_posix_perms = pytest.mark.skipif(
    os.name == "nt" or (hasattr(os, "geteuid") and os.geteuid() == 0),
    reason="needs POSIX permissions and a non-root user",
)


# ── helpers ──────────────────────────────────────────────────────────────


def _initialized_wiki(wiki_root_path: Path) -> WikiStore:
    store = WikiStore.at_default()
    store.init()
    return store


def _commit_wiki(wiki_root_path: Path, message: str) -> str:
    subprocess.run(["git", "-C", str(wiki_root_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(wiki_root_path), "commit", "-m", message],
        check=True,
        capture_output=True,
    )
    return WikiStore.at_default().current_commit()


def _seed_wiki_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Add ``skills/<name>/`` to wiki + git commit. Returns the commit SHA."""
    skill_dir = wiki_root_path / "skills" / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return _commit_wiki(wiki_root_path, f"add {name}")


def _modify_wiki_skill(wiki_root_path: Path, name: str, files: dict[str, bytes]) -> str:
    """Modify wiki skill files + commit. Wiki HEAD advances. Returns new SHA."""
    skill_dir = wiki_root_path / "skills" / name
    for relpath, data in files.items():
        target = skill_dir / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return _commit_wiki(wiki_root_path, f"modify {name}")


def _drop_wiki_file(wiki_root_path: Path, name: str, rel: str) -> str:
    """Delete one file from the wiki skill + commit. Returns new SHA."""
    (wiki_root_path / "skills" / name / rel).unlink()
    return _commit_wiki(wiki_root_path, f"drop {rel} from {name}")


def _lock_path(project: Path) -> Path:
    return project / ".memtomem" / "lock.json"


def _entry(project: Path, asset_type: str = "skills", name: str = "web") -> dict:
    doc = json.loads(_lock_path(project).read_text(encoding="utf-8"))
    return doc[asset_type][name]


def _surgery(project: Path, mutate, asset_type: str = "skills", name: str = "web") -> None:
    """Rewrite one lock entry via *mutate(entry_dict)* — direct dict surgery.

    Deliberately NOT ``upsert_entry``: the §9.6 degrade fixtures simulate a
    pre-digest tool that preserves the ``digests*`` keys verbatim while
    moving ``installed_at``, which the digest-aware upsert can no longer
    produce (it clears the keys when ``digests`` is omitted).
    """
    lock_path = _lock_path(project)
    doc = json.loads(lock_path.read_text(encoding="utf-8"))
    mutate(doc[asset_type][name])
    lock_path.write_text(json.dumps(doc), encoding="utf-8")


def _epoch(installed_at: str) -> float:
    return datetime.fromisoformat(installed_at.replace("Z", "+00:00")).timestamp()


def _backdate(path: Path, installed_at: str, *, margin: float = 0.001) -> None:
    """Set *path*'s mtime strictly below the entry's installed_at epoch."""
    target = _epoch(installed_at) - margin
    os.utime(path, (target, target))


def _iso_at(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ── bug class: absorbed concurrent edits (pre-fix-fail vs main) ──────────


def test_absorbed_backdated_edit_classifies_dirty_and_refuses(
    wiki_root: Path, tmp_path: Path
) -> None:
    """§9.1 — the id 15 repro. A user edit whose mtime sits at/below
    ``installed_at`` (exactly what an edit racing the install's post-copy
    capture looks like) was permanently clean on main; the next update
    silently overwrote the user's bytes. Digests classify it dirty, the
    no-force update refuses, and the bytes survive."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "scripts/a.md": b"original\n"})
    install_skill(tmp_path, "web")

    edited = tmp_path / ".memtomem" / "skills" / "web" / "scripts" / "a.md"
    edited.write_bytes(b"user bytes the install raced past\n")
    _backdate(edited, _entry(tmp_path)["installed_at"])

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "dirty"
    assert report.dirty_files == (edited,)

    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web v2\n"})
    with pytest.raises(StaleInstallError):
        update_skill(tmp_path, "web")
    assert edited.read_bytes() == b"user bytes the install raced past\n"


def test_absorbed_backdated_edit_force_preserves_bak(wiki_root: Path, tmp_path: Path) -> None:
    """§9.1 force half: ``--force`` lands the user bytes in a ``.bak``
    sibling before the wiki bytes overwrite — on main no ``.bak`` was
    written because the file classified clean."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "scripts/a.md": b"original\n"})
    install_skill(tmp_path, "web")

    edited = tmp_path / ".memtomem" / "skills" / "web" / "scripts" / "a.md"
    edited.write_bytes(b"user bytes\n")
    _backdate(edited, _entry(tmp_path)["installed_at"])
    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web v2\n"})

    result = update_skill(tmp_path, "web", force=True)

    bak = edited.with_suffix(edited.suffix + ".bak")
    assert [p.name for p in result.bak_files_written] == ["a.md.bak"]
    assert bak.read_bytes() == b"user bytes\n"
    assert edited.read_bytes() == b"original\n"  # wiki bytes restored


def test_mid_copy_interleave_detected(
    wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§9.2 — an edit landing DURING the copy pipeline (after the file's own
    write, before the post-copy capture) leaves dest bytes ≠ the bytes the
    copier wrote. The digest is computed from the in-memory data, so the
    race is visible no matter when it lands; the scalar capture absorbed it."""
    real_write = atomic_module.atomic_write_bytes
    raced = {"done": False}

    def racing_write(path: Path, data: bytes, mode: int = 0o600) -> None:
        real_write(path, data, mode=mode)
        if path.name == "a.md" and not raced["done"]:
            raced["done"] = True
            st = path.stat()
            path.write_bytes(b"raced bytes\n")
            os.utime(path, ns=(st.st_mtime_ns, st.st_mtime_ns))  # mtime as if untouched

    monkeypatch.setattr(atomic_module, "atomic_write_bytes", racing_write)
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "a.md": b"original\n"})
    install_skill(tmp_path, "web")

    assert raced["done"], "fixture failed to interleave the racing write"
    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "dirty"
    assert [p.name for p in report.dirty_files] == ["a.md"]


def test_backdated_addition_detected(wiki_root: Path, tmp_path: Path) -> None:
    """§9.3 — a file added to dest with an old mtime was invisible to the
    mtime walk; on the digest branch any rel absent from the recorded map
    is a local addition → dirty."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n"})
    install_skill(tmp_path, "web")

    added = tmp_path / ".memtomem" / "skills" / "web" / "added.md"
    added.write_bytes(b"smuggled\n")
    _backdate(added, _entry(tmp_path)["installed_at"])

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "dirty"
    assert report.dirty_files == (added,)


def test_backdated_addition_invisible_on_legacy_entry(wiki_root: Path, tmp_path: Path) -> None:
    """§9.3 paired negative — documents the legacy gap explicitly: the same
    backdated addition on a digest-less entry stays clean (pre-digest
    behavior, bit-for-bit)."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n"})
    install_skill(tmp_path, "web")

    def strip(entry: dict) -> None:
        entry.pop("digests", None)
        entry.pop("digests_installed_at", None)

    _surgery(tmp_path, strip)
    added = tmp_path / ".memtomem" / "skills" / "web" / "added.md"
    added.write_bytes(b"smuggled\n")
    _backdate(added, _entry(tmp_path)["installed_at"])

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "clean"


def test_deletion_detected_via_digest_keys_without_manifest(
    wiki_root: Path, tmp_path: Path
) -> None:
    """§9.13 — ``missing_files`` derives from the digest map's keys on the
    digest branch, independent of the ``files`` manifest."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "scripts/a.md": b"a\n"})
    install_skill(tmp_path, "web")

    def drop_manifest(entry: dict) -> None:
        entry.pop("files", None)
        entry.pop("files_commit", None)

    _surgery(tmp_path, drop_manifest)
    gone = tmp_path / ".memtomem" / "skills" / "web" / "scripts" / "a.md"
    gone.unlink()

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "dirty"
    assert report.dirty_files == ()
    assert report.missing_files == (gone,)


# ── semantic deltas (pre-fix-fail vs main) ───────────────────────────────


def test_touch_only_edit_clean_on_digest_entry(wiki_root: Path, tmp_path: Path) -> None:
    """Delta 1 (§9.5): mtime bumped, bytes identical → clean on digest
    entries (was dirty — false refusals and manufactured ``.bak``\\ s for
    unchanged bytes). The follow-up no-force update succeeds bak-less."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n"})
    install_skill(tmp_path, "web")

    touched = tmp_path / ".memtomem" / "skills" / "web" / "SKILL.md"
    future = datetime.now(timezone.utc).timestamp() + 30
    os.utime(touched, (future, future))

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "clean"

    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web v2\n"})
    result = update_skill(tmp_path, "web")  # no force needed
    assert result.bak_files_written == ()
    assert touched.read_bytes() == b"# web v2\n"


def test_reconcile_deletes_untouched_wiki_dropped_file_with_fresh_mtime(
    wiki_root: Path, tmp_path: Path
) -> None:
    """§9.4 — legacy false-KEEP closure. A wiki-dropped file whose bytes are
    untouched but whose mtime is fresh (cross-machine checkout shape) was
    kept forever with a repeating warning (and on main the fresh mtime
    additionally classified the whole asset dirty → refuse). Digest equality
    proves it untouched: the no-force update succeeds and reconciles it away."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "dropme.md": b"old\n"})
    install_skill(tmp_path, "web")

    _drop_wiki_file(wiki_root, "web", "dropme.md")
    dropped = tmp_path / ".memtomem" / "skills" / "web" / "dropme.md"
    future = datetime.now(timezone.utc).timestamp() + 30
    os.utime(dropped, (future, future))  # fresh mtime, identical bytes

    result = update_skill(tmp_path, "web")  # no force

    assert not dropped.exists()
    assert [p.name for p in result.files_removed] == ["dropme.md"]
    assert _entry(tmp_path)["files"] == ["SKILL.md"]


def test_reconcile_digest_provenance_beats_divergent_manifest(
    wiki_root: Path, tmp_path: Path
) -> None:
    """§9.15 — when the old entry carries valid digests, they are the single
    provenance set for reconcile; a hand-edited ``files`` manifest missing
    the rel must not protect a digest-tracked, wiki-dropped file."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "r.md": b"r\n"})
    install_skill(tmp_path, "web")

    _surgery(tmp_path, lambda entry: entry.update(files=["SKILL.md"]))  # hand-drop r.md
    entry = _entry(tmp_path)
    assert manifest_from_entry(entry) == frozenset({"SKILL.md"})  # divergent but valid
    assert "r.md" in digests_from_entry(entry)  # digests still track it

    _drop_wiki_file(wiki_root, "web", "r.md")
    result = update_skill(tmp_path, "web")

    assert not (tmp_path / ".memtomem" / "skills" / "web" / "r.md").exists()
    assert [p.name for p in result.files_removed] == ["r.md"]


def test_reconcile_membership_is_written_set_not_post_copy_src_walk(
    wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Codex impl-gate Major: ``_apply_update`` used to derive reconcile
    membership by re-walking the wiki working tree AFTER the copy returned —
    a src mutation in that window made reconcile erase a file this very
    update just wrote while ``files``/``digests`` recorded it (a phantom
    lock entry). The commit-true update (#1652) closes the mutable-src
    window by construction, but the contract this test pins survives:
    membership must come from the extractor's RETURNED written set, never a
    re-read of the (mutable) wiki — so a worktree mutation landing in the
    extract→reconcile window must still be invisible."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"v1\n", "keep.md": b"same\n"})
    install_skill(tmp_path, "web")
    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"v2\n"})  # keep.md unchanged

    real_extract = WikiStore.copy_asset_at_commit

    def extract_then_mutate_worktree(
        self: WikiStore, commit: str, asset_type: str, name: str, dest: Path
    ) -> dict[str, str]:
        result = real_extract(self, commit, asset_type, name, dest)
        # Worktree mutation in the extract→reconcile window (after the
        # wiki-side dirty gate already passed).
        (self.root / asset_type / name / "keep.md").unlink()
        return result

    monkeypatch.setattr(WikiStore, "copy_asset_at_commit", extract_then_mutate_worktree)
    result = update_skill(tmp_path, "web")

    kept = tmp_path / ".memtomem" / "skills" / "web" / "keep.md"
    assert kept.read_bytes() == b"same\n"  # the file this update wrote survives
    assert result.files_removed == ()
    entry = _entry(tmp_path)
    assert entry["files"] == ["SKILL.md", "keep.md"]
    assert "keep.md" in entry["digests"]  # record and dest agree — no phantom
    assert is_asset_dirty(tmp_path, "skills", "web").reason == "clean"


@requires_posix_perms
def test_unreadable_file_classifies_dirty_with_warning(
    wiki_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """§9.16 status half — the digest branch must read bytes, so an
    unreadable file is a new failure mode: classify dirty + warn (cannot
    prove clean), never raise (one chmod'd file must not 500 a status walk
    over N projects) and never silently pass (was: clean, stat needs no
    read permission)."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "locked.md": b"x\n"})
    install_skill(tmp_path, "web")

    locked = tmp_path / ".memtomem" / "skills" / "web" / "locked.md"
    locked.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="memtomem.context.dirty"):
            report = is_asset_dirty(tmp_path, "skills", "web")
    finally:
        locked.chmod(0o644)

    assert report.reason == "dirty"
    assert report.dirty_files == (locked,)
    assert any("cannot read" in r.message for r in caplog.records)


@requires_posix_perms
def test_unreadable_subdir_classifies_dirty_not_crash(
    wiki_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """``is_asset_dirty`` must survive an unreadable SUBDIRECTORY, not just an
    unreadable file. Removing a subtree's search bit makes ``iterdir`` raise;
    pre-fix that escaped ``is_asset_dirty`` (no surrounding guard) and 500'd the
    whole ``mm context status`` table over N projects. The walk now catches the
    enumeration error and classifies DIRTY — protective ("cannot prove clean"),
    never a crash, never a silent clean."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "scripts/run.sh": b"echo hi\n"})
    install_skill(tmp_path, "web")

    scripts = tmp_path / ".memtomem" / "skills" / "web" / "scripts"
    scripts.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="memtomem.context.dirty"):
            report = is_asset_dirty(tmp_path, "skills", "web")
    finally:
        scripts.chmod(0o755)

    assert report.reason == "dirty"
    assert any("cannot enumerate" in r.message for r in caplog.records)


@requires_posix_perms
def test_unreadable_subdir_pre_manifest_legacy_classifies_dirty(
    wiki_root: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """The crash-guard must also fail-CLOSED on a PRE-MANIFEST legacy entry
    (no digests, no ``files`` manifest): there is no recorded set to surface a
    skipped subtree as ``missing``, so a walker that silently skipped would
    report CLEAN — vouching for bytes it could not read. Catching the
    enumeration error and classifying dirty closes that on both branches."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "scripts/run.sh": b"echo hi\n"})
    install_skill(tmp_path, "web")

    # Strip the entry down to a pre-#1247 legacy shape: keep only wiki_commit +
    # installed_at, drop the digests/files manifest entirely (the legacy
    # branch with manifest_from_entry() == None).
    lock = Lockfile.at(tmp_path)
    entry = lock.read_entry("skills", "web")
    assert entry is not None
    legacy_entry = {"wiki_commit": entry["wiki_commit"], "installed_at": entry["installed_at"]}

    scripts = tmp_path / ".memtomem" / "skills" / "web" / "scripts"
    scripts.chmod(0o000)
    try:
        with caplog.at_level(logging.WARNING, logger="memtomem.context.dirty"):
            report = is_asset_dirty(tmp_path, "skills", "web", lock_entry=legacy_entry)
    finally:
        scripts.chmod(0o755)

    assert report.reason == "dirty"  # not a silent clean
    assert any("cannot enumerate" in r.message for r in caplog.records)


@requires_posix_perms
def test_unreadable_file_force_update_fails_loudly_before_any_mutation(
    wiki_root: Path, tmp_path: Path
) -> None:
    """§9.16 mutation half — under ``--force`` the unreadable dirty file is
    in the ``.bak`` set, and the pipeline fails loudly BEFORE the first dest
    mutation (Gate A's read error, or OSError from any path that reaches
    ``copy2``): no ``.bak`` written, dest bytes untouched, no lockfile
    drift. The invariant is pinned, not the call site."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "locked.md": b"x\n"})
    install_skill(tmp_path, "web")
    entry_before = _entry(tmp_path)

    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web v2\n"})
    dest = tmp_path / ".memtomem" / "skills" / "web"
    locked = dest / "locked.md"
    locked.chmod(0o000)
    try:
        with pytest.raises((PrivacyScanReadError, OSError)):
            update_skill(tmp_path, "web", force=True)
    finally:
        locked.chmod(0o644)

    assert not list(dest.rglob("*.bak"))
    assert (dest / "SKILL.md").read_bytes() == b"# web\n"  # v2 never landed
    assert _entry(tmp_path) == entry_before


@requires_posix_perms
def test_unreadable_subdir_force_update_refuses_before_any_mutation(
    wiki_root: Path, tmp_path: Path
) -> None:
    """Mutation half of the subtree case: when the dest tree can't be fully
    enumerated (unreadable SUBDIRECTORY), ``is_asset_dirty`` reports
    ``walk_failed`` and ``update --force`` must REFUSE before any mutation —
    the at-risk files can't be enumerated to ``.bak``, so proceeding would
    copy/reconcile the readable files and only then fail on the subtree,
    leaving a partial update with no backups. No ``.bak``, dest bytes
    untouched, no lockfile drift."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "scripts/run.sh": b"echo hi\n"})
    install_skill(tmp_path, "web")
    entry_before = _entry(tmp_path)

    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web v2\n"})
    dest = tmp_path / ".memtomem" / "skills" / "web"
    scripts = dest / "scripts"
    scripts.chmod(0o000)
    try:
        with pytest.raises(StaleInstallError, match="can't be enumerated"):
            update_skill(tmp_path, "web", force=True)
    finally:
        scripts.chmod(0o755)

    assert not list(dest.rglob("*.bak"))
    assert (dest / "SKILL.md").read_bytes() == b"# web\n"  # v2 never landed
    assert _entry(tmp_path) == entry_before


# ── pairing degrade negative pins (§9.6) ─────────────────────────────────


def test_pairing_degrade_on_old_tool_rewrite(wiki_root: Path, tmp_path: Path) -> None:
    """§9.6 — a pre-digest tool rewriting the entry preserves the unknown
    ``digests*`` keys verbatim while refreshing ``installed_at``; the
    pairing mismatch must degrade the entry to the legacy branch (where a
    backdated edit is invisible again — honoring the stale map would
    false-dirty every file the old tool's update rewrote)."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "a.md": b"original\n"})
    install_skill(tmp_path, "web")

    edited = tmp_path / ".memtomem" / "skills" / "web" / "a.md"
    edited.write_bytes(b"changed bytes\n")
    _backdate(edited, _entry(tmp_path)["installed_at"])
    assert is_asset_dirty(tmp_path, "skills", "web").reason == "dirty"  # digest catches it

    # Old-tool rewrite simulation: installed_at moves, digests* preserved.
    new_installed_at = _iso_at(_epoch(_entry(tmp_path)["installed_at"]) + 10)
    _surgery(tmp_path, lambda entry: entry.update(installed_at=new_installed_at))

    assert digests_from_entry(_entry(tmp_path)) is None
    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "clean"  # legacy branch: backdated edit invisible


def test_pairing_degrade_survives_a_b_a_pin_roundtrip(wiki_root: Path, tmp_path: Path) -> None:
    """§9.6 A→B→A variant — the reason the pairing token is ``installed_at``
    and not ``wiki_commit``: an old tool moving the pin away and back
    re-validates the COMMIT pairing (``files_commit`` carries that
    documented hole) but must not re-validate the digests, because every
    old-tool write refreshed ``installed_at``."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "a.md": b"original\n"})
    install_skill(tmp_path, "web")
    entry0 = _entry(tmp_path)
    pin_a = entry0["wiki_commit"]
    t0 = _epoch(entry0["installed_at"])

    def roundtrip(entry: dict) -> None:
        # A→B (old tool update): pin + manifest move, installed_at refreshes.
        # B→A (old tool restore): everything back at A, installed_at fresh again.
        entry["wiki_commit"] = pin_a
        entry["files_commit"] = pin_a
        entry["installed_at"] = _iso_at(t0 + 20)

    _surgery(tmp_path, roundtrip)

    entry = _entry(tmp_path)
    assert manifest_from_entry(entry) is not None  # commit pairing re-matched (known hole)
    assert digests_from_entry(entry) is None  # installed_at pairing did not

    edited = tmp_path / ".memtomem" / "skills" / "web" / "a.md"
    edited.write_bytes(b"changed bytes\n")
    _backdate(edited, entry["installed_at"])
    assert is_asset_dirty(tmp_path, "skills", "web").reason == "clean"  # legacy, not stale map


def test_pairing_collision_consequence_is_fail_safe(wiki_root: Path, tmp_path: Path) -> None:
    """§9.6 collision variant — ``installed_at`` is mtime-derived, so a
    pre-digest rewrite CAN land on the byte-identical ISO string (documented
    residual). The guard then accepts the stale pair; the pinned consequence
    direction: byte-mismatching files classify **dirty** (refuse/.bak), and
    only byte-identical-to-stale-record files classify clean — never a
    silent clean over diverged bytes."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "a.md": b"original\n"})
    install_skill(tmp_path, "web")

    edited = tmp_path / ".memtomem" / "skills" / "web" / "a.md"
    edited.write_bytes(b"changed bytes\n")
    _backdate(edited, _entry(tmp_path)["installed_at"])

    # Old-tool rewrite whose fresh installed_at collides byte-for-byte with
    # the stale digests_installed_at: move both to the same new value.
    collided = _iso_at(_epoch(_entry(tmp_path)["installed_at"]) + 10)

    def collide(entry: dict) -> None:
        entry["installed_at"] = collided
        entry["digests_installed_at"] = collided

    _surgery(tmp_path, collide)

    assert digests_from_entry(_entry(tmp_path)) is not None  # guard accepts (residual)
    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "dirty"  # fail-safe: mismatching bytes refuse
    assert [p.name for p in report.dirty_files] == ["a.md"]  # untouched SKILL.md stays clean


# ── malformed digests degrade (§9.7) ─────────────────────────────────────

_VALID_HEX = "0" * 64


@pytest.mark.parametrize(
    "digests_value, pairing_value",
    [
        ("not-a-dict", "PAIR"),  # digests not a dict
        ({"": _VALID_HEX}, "PAIR"),  # empty rel
        ({"../escape.md": _VALID_HEX}, "PAIR"),  # traversal rel
        ({"/abs.md": _VALID_HEX}, "PAIR"),  # absolute rel
        ({"a\\b.md": _VALID_HEX}, "PAIR"),  # backslash rel
        ({"a.md": "zz" * 32}, "PAIR"),  # non-hex value
        ({"a.md": "0" * 63}, "PAIR"),  # wrong length
        ({"a.md": "A" * 64}, "PAIR"),  # uppercase hex — not lowercase
        ({"a.md": 42}, "PAIR"),  # non-string value
        ({"a.md": _VALID_HEX}, None),  # digests_installed_at missing
        ({"a.md": _VALID_HEX}, 123),  # digests_installed_at non-string
    ],
)
def test_malformed_digests_degrade_to_legacy(
    wiki_root: Path, tmp_path: Path, digests_value: object, pairing_value: object
) -> None:
    """§9.7 — lock.json is git-tracked and hand-merged: every malformed
    shape degrades to the legacy classification (backdated edit invisible),
    never crashes, never half-applies. ``"PAIR"`` means "keep the valid
    pairing value" so the shape under test is the only defect."""
    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"# web\n", "a.md": b"original\n"})
    install_skill(tmp_path, "web")

    def corrupt(entry: dict) -> None:
        entry["digests"] = digests_value
        if pairing_value is None:
            entry.pop("digests_installed_at", None)
        elif pairing_value != "PAIR":
            entry["digests_installed_at"] = pairing_value

    _surgery(tmp_path, corrupt)
    assert digests_from_entry(_entry(tmp_path)) is None

    edited = tmp_path / ".memtomem" / "skills" / "web" / "a.md"
    edited.write_bytes(b"changed bytes\n")
    _backdate(edited, _entry(tmp_path)["installed_at"])

    report = is_asset_dirty(tmp_path, "skills", "web")
    assert report.reason == "clean"  # legacy branch took over, no crash


# ── reconcile decision rules, unit level (§9.4/9.15 shapes) ──────────────


class TestReconcileDigestRules:
    """Exercise ``_reconcile_removed_files`` rules directly — the integration
    paths above can't reach every keep/delete arm deterministically (rule 3
    needs a fresh-mtime non-baked file, which the normal classify→gate flow
    refuses before reconcile; only the ``--all`` confirm race reaches it)."""

    def _tree(self, tmp_path: Path, files: dict[str, bytes]) -> Path:
        dest = tmp_path / "dest"
        for rel, data in files.items():
            target = dest / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(data)
        return dest

    def test_unrecorded_rel_kept_as_user_added(self, tmp_path: Path) -> None:
        dest = self._tree(tmp_path, {"keep.md": b"user\n"})
        removed = _reconcile_removed_files(
            dest,
            src_has=lambda rel: False,
            old_installed_at_epoch=None,
            baked=frozenset(),
            manifest=None,
            old_digests={"other.md": _sha(b"x")},
        )
        assert removed == ()
        assert (dest / "keep.md").exists()

    def test_recorded_untouched_bytes_deleted_despite_fresh_mtime(self, tmp_path: Path) -> None:
        dest = self._tree(tmp_path, {"gone.md": b"wiki\n"})
        future = datetime.now(timezone.utc).timestamp() + 30
        os.utime(dest / "gone.md", (future, future))
        removed = _reconcile_removed_files(
            dest,
            src_has=lambda rel: False,
            old_installed_at_epoch=None,
            baked=frozenset(),
            manifest=None,
            old_digests={"gone.md": _sha(b"wiki\n")},
        )
        assert [p.name for p in removed] == ["gone.md"]
        assert not (dest / "gone.md").exists()

    def test_recorded_diverged_bytes_kept_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        dest = self._tree(tmp_path, {"edited.md": b"user edit\n"})
        with caplog.at_level(logging.WARNING, logger="memtomem.context.install"):
            removed = _reconcile_removed_files(
                dest,
                src_has=lambda rel: False,
                old_installed_at_epoch=None,
                baked=frozenset(),
                manifest=None,
                old_digests={"edited.md": _sha(b"wiki\n")},
            )
        assert removed == ()
        assert (dest / "edited.md").exists()
        assert any("unproven bytes" in r.message for r in caplog.records)

    def test_recorded_diverged_but_baked_deleted(self, tmp_path: Path) -> None:
        dest = self._tree(tmp_path, {"edited.md": b"user edit\n"})
        removed = _reconcile_removed_files(
            dest,
            src_has=lambda rel: False,
            old_installed_at_epoch=None,
            baked=frozenset({dest / "edited.md"}),
            manifest=None,
            old_digests={"edited.md": _sha(b"wiki\n")},
        )
        assert [p.name for p in removed] == ["edited.md"]

    @requires_posix_perms
    def test_recorded_unreadable_kept_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Unreadable is neither provable nor baked → rule 3 keep —
        consistent with the dirty check's fail-safe direction."""
        dest = self._tree(tmp_path, {"locked.md": b"wiki\n"})
        (dest / "locked.md").chmod(0o000)
        try:
            with caplog.at_level(logging.WARNING, logger="memtomem.context.install"):
                removed = _reconcile_removed_files(
                    dest,
                    src_has=lambda rel: False,
                    old_installed_at_epoch=None,
                    baked=frozenset(),
                    manifest=None,
                    old_digests={"locked.md": _sha(b"wiki\n")},
                )
        finally:
            (dest / "locked.md").chmod(0o644)
        assert removed == ()
        assert (dest / "locked.md").exists()

    def test_digest_branch_ignores_manifest(self, tmp_path: Path) -> None:
        """§9.15 unit shape: rel recorded in digests but hand-dropped from
        the manifest — digest provenance wins, the file is deleted."""
        dest = self._tree(tmp_path, {"r.md": b"wiki\n"})
        removed = _reconcile_removed_files(
            dest,
            src_has=lambda rel: False,
            old_installed_at_epoch=None,
            baked=frozenset(),
            manifest=frozenset({"SKILL.md"}),  # r.md absent — would keep as user-added
            old_digests={"r.md": _sha(b"wiki\n")},
        )
        assert [p.name for p in removed] == ["r.md"]

    def test_legacy_fresh_mtime_kept_with_warning(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """§9.4 paired negative — today's legacy behavior, unchanged: no
        digests, manifest-recorded, fresh mtime, not baked → keep + warn
        (the false-KEEP the digest branch retires)."""
        dest = self._tree(tmp_path, {"dropme.md": b"wiki\n"})
        past_epoch = datetime.now(timezone.utc).timestamp() - 3600
        with caplog.at_level(logging.WARNING, logger="memtomem.context.install"):
            removed = _reconcile_removed_files(
                dest,
                src_has=lambda rel: False,
                old_installed_at_epoch=past_epoch,  # file mtime is now → fresh
                baked=frozenset(),
                manifest=frozenset({"dropme.md"}),
                old_digests=None,
            )
        assert removed == ()
        assert (dest / "dropme.md").exists()
        assert any("fresh mtime" in r.message for r in caplog.records)


# ── capture: copy_tree_atomic digest map (§9.9) ──────────────────────────


class TestCopyTreeAtomicDigestMap:
    def test_map_covers_written_set_with_posix_rels_and_source_hashes(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "scripts").mkdir(parents=True)
        (src / "SKILL.md").write_bytes(b"# top\n")
        (src / "scripts" / "run.py").write_bytes(b"print()\n")
        dst = tmp_path / "dst"

        digest_map = copy_tree_atomic(src, dst)

        assert digest_map == {
            "SKILL.md": _sha(b"# top\n"),
            "scripts/run.py": _sha(b"print()\n"),
        }
        written = [p for p in dst.rglob("*") if p.is_file()]
        assert len(digest_map) == len(written)

    def test_skip_rules_keep_entries_out_of_map(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        (src / "__pycache__").mkdir(parents=True)
        (src / ".git").mkdir()
        (src / "SKILL.md").write_bytes(b"x\n")
        (src / "old.md.bak").write_bytes(b"bak\n")
        (src / ".DS_Store").write_bytes(b"\x00")
        (src / "__pycache__" / "a.pyc").write_bytes(b"\x00")
        (src / ".git" / "HEAD").write_bytes(b"ref\n")
        dst = tmp_path / "dst"

        digest_map = copy_tree_atomic(src, dst, skip_suffixes=DIRTY_SKIP_SUFFIXES)

        assert sorted(digest_map) == ["SKILL.md"]
        assert [p.name for p in dst.rglob("*")] == ["SKILL.md"]

    @pytest.mark.requires_symlinks
    def test_symlinks_skipped_and_absent_from_map(self, tmp_path: Path) -> None:
        src = tmp_path / "src"
        src.mkdir()
        (src / "SKILL.md").write_bytes(b"x\n")
        (src / "link.md").symlink_to("SKILL.md")
        dst = tmp_path / "dst"

        digest_map = copy_tree_atomic(src, dst)

        assert sorted(digest_map) == ["SKILL.md"]
        assert not (dst / "link.md").exists()

    def test_skip_top_level_is_root_only(self, tmp_path: Path) -> None:
        """Root ``overrides/`` excluded, nested ``scripts/overrides/`` kept —
        the rel-prefix recursion must not widen the root-only contract."""
        src = tmp_path / "src"
        (src / "overrides").mkdir(parents=True)
        (src / "scripts" / "overrides").mkdir(parents=True)
        (src / "overrides" / "claude.md").write_bytes(b"vendor\n")
        (src / "scripts" / "overrides" / "nested.md").write_bytes(b"nested\n")
        dst = tmp_path / "dst"

        digest_map = copy_tree_atomic(src, dst, skip_top_level=frozenset({"overrides"}))

        assert sorted(digest_map) == ["scripts/overrides/nested.md"]
        assert not (dst / "overrides").exists()
        assert (dst / "scripts" / "overrides" / "nested.md").read_bytes() == b"nested\n"


# ── schema: upsert_entry digests contract (§9.12) ────────────────────────


class TestUpsertEntryDigests:
    def test_digests_stamp_pairing_from_written_installed_at(self, tmp_path: Path) -> None:
        lock = Lockfile.at(tmp_path)
        lock.upsert_entry(
            "skills",
            "web",
            wiki_commit="a" * 40,
            installed_at="2026-06-12T00:00:00.000000Z",
            files=["b.md", "a.md"],
            files_commit="a" * 40,
            digests={"b.md": _sha(b"b"), "a.md": _sha(b"a")},
        )

        entry = lock.read_entry("skills", "web")
        assert entry is not None
        assert entry["digests_installed_at"] == "2026-06-12T00:00:00.000000Z"
        assert list(entry["digests"]) == ["a.md", "b.md"]  # stored sorted
        assert digests_from_entry(entry) == {"a.md": _sha(b"a"), "b.md": _sha(b"b")}

    def test_omitting_digests_clears_prior_pair_but_keeps_unknown_keys(
        self, tmp_path: Path
    ) -> None:
        """Clear-on-omit (R2 Major 1 fold): the digest-aware writer owns the
        keys — a digest-less rewrite must not leave a stale pair that a
        later mtime collision could re-validate. Truly-unknown sibling keys
        keep round-tripping verbatim."""
        lock = Lockfile.at(tmp_path)
        lock.upsert_entry(
            "skills",
            "web",
            wiki_commit="a" * 40,
            installed_at="2026-06-12T00:00:00.000000Z",
            digests={"a.md": _sha(b"a")},
        )
        # Plant an unknown sibling key the way a future tool would.
        lock_path = tmp_path / ".memtomem" / "lock.json"
        doc = json.loads(lock_path.read_text(encoding="utf-8"))
        doc["skills"]["web"]["compat"] = "v2"
        lock_path.write_text(json.dumps(doc), encoding="utf-8")

        lock.upsert_entry(
            "skills",
            "web",
            wiki_commit="b" * 40,
            installed_at="2026-06-12T01:00:00.000000Z",
        )

        entry = lock.read_entry("skills", "web")
        assert entry is not None
        assert "digests" not in entry
        assert "digests_installed_at" not in entry
        assert entry["compat"] == "v2"  # unknown key still round-trips


# ── all three install sites record paired digests (§9.11) ────────────────


def test_install_update_and_pinned_reinstall_record_paired_digests(
    wiki_root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Site sweep: fresh install, update-with-reconcile, and the pinned
    ``install --all`` re-extraction all write ``digests`` whose pairing
    token equals the entry's ``installed_at`` and whose key set IS the
    ``files`` manifest."""
    from click.testing import CliRunner

    from memtomem.cli.context_cmd import context as context_group

    def assert_paired(entry: dict, expected: dict[str, bytes]) -> None:
        assert entry["digests"] == {rel: _sha(data) for rel, data in expected.items()}
        assert entry["digests_installed_at"] == entry["installed_at"]
        assert entry["files"] == sorted(entry["digests"])

    _initialized_wiki(wiki_root)
    _seed_wiki_skill(wiki_root, "web", {"SKILL.md": b"v1\n", "dropme.md": b"d\n"})

    # Site 1: install.
    install_skill(tmp_path, "web")
    assert_paired(_entry(tmp_path), {"SKILL.md": b"v1\n", "dropme.md": b"d\n"})

    # Site 2: update — wiki advances AND drops a file, so reconcile runs.
    _modify_wiki_skill(wiki_root, "web", {"SKILL.md": b"v2\n"})
    _drop_wiki_file(wiki_root, "web", "dropme.md")
    update_skill(tmp_path, "web")
    assert_paired(_entry(tmp_path), {"SKILL.md": b"v2\n"})

    # Site 3: pinned re-extraction (dest deleted → state="install" at pin).
    import shutil

    shutil.rmtree(tmp_path / ".memtomem" / "skills" / "web")
    monkeypatch.chdir(tmp_path)
    result = CliRunner().invoke(context_group, ["install", "--all", "--yes"])
    assert result.exit_code == 0, result.output
    assert_paired(_entry(tmp_path), {"SKILL.md": b"v2\n"})
