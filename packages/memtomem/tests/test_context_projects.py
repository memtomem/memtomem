"""Tests for ``memtomem.context.projects`` — multi-project discovery.

PR2 minimum-bar from the RFC (`multi-project-context-ui-rfc.md` §Test
obligations): scope_id stability + collision sanity, atomic-write race
through real OS-level concurrency, symlink dedup, both
``experimental_claude_projects_scan`` defaults.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
from pathlib import Path

import pytest

from memtomem.context.projects import (
    KnownProjectsStore,
    ProjectHealth,
    ProjectScope,
    annotate_project_health,
    compute_scope_id,
    discover_project_scopes,
    has_runtime_marker,
)


# ── scope_id stability ───────────────────────────────────────────────────


def test_scope_id_same_path_is_stable(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    assert compute_scope_id(project) == compute_scope_id(project)


def test_scope_id_distinct_paths_are_distinct(tmp_path: Path) -> None:
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()
    assert compute_scope_id(a) != compute_scope_id(b)


def test_scope_id_trailing_slash_invariant(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    with_slash = Path(str(project) + "/")
    assert compute_scope_id(project) == compute_scope_id(with_slash)


@pytest.mark.skipif(
    sys.platform != "darwin",
    reason="case-insensitive FS only assumed on macOS HFS+/APFS in CI",
)
def test_scope_id_case_insensitive_on_macos(tmp_path: Path) -> None:
    project = tmp_path / "Alpha"
    project.mkdir()
    upper = Path(str(project))
    lower = Path(str(project).lower())
    # Case-folded path should resolve to the same inode → same scope_id.
    assert compute_scope_id(upper) == compute_scope_id(lower)


def test_scope_id_format(tmp_path: Path) -> None:
    sid = compute_scope_id(tmp_path)
    # ``p-`` prefix + 12 hex chars (48 bits — RFC §Decision 4).
    assert sid.startswith("p-")
    assert len(sid) == 14
    assert all(c in "0123456789abcdef" for c in sid[2:])


# ── known_projects store ─────────────────────────────────────────────────


def test_store_load_missing_returns_empty(tmp_path: Path) -> None:
    store = KnownProjectsStore(tmp_path / "nope.json")
    assert store.load() == []


def test_store_add_then_load(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    entries = store.load()
    assert len(entries) == 1
    assert entries[0].root == project


def test_store_add_is_idempotent(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    store.add(project)  # second add should not duplicate
    assert len(store.load()) == 1


def test_store_remove_by_scope_id(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    sid = compute_scope_id(project)
    assert store.remove_by_scope_id(sid) is True
    assert store.load() == []


def test_store_remove_nonexistent_returns_false(tmp_path: Path) -> None:
    store = KnownProjectsStore(tmp_path / "kp.json")
    assert store.remove_by_scope_id("p-deadbeefcafe") is False


def test_store_remove_stale_entry(tmp_path: Path) -> None:
    """Stale entries (root deleted post-registration) must still be removable."""
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    sid = compute_scope_id(project)
    project.rmdir()
    assert store.remove_by_scope_id(sid) is True


def test_store_update_label_by_scope_id(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    sid = compute_scope_id(project)

    updated = store.update_label_by_scope_id(sid, "Alpha Prod")
    assert updated is not None
    assert updated.label == "Alpha Prod"
    assert updated.root == project
    # Reload from disk to confirm the write persisted.
    reloaded = store.load()
    assert len(reloaded) == 1
    assert reloaded[0].label == "Alpha Prod"


def test_store_update_label_preserves_added_at(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    added_at = store.add(project).added_at
    sid = compute_scope_id(project)

    updated = store.update_label_by_scope_id(sid, "renamed")
    assert updated is not None
    assert updated.added_at == added_at  # rename never touches the registration time


def test_store_update_label_clear_to_none(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project, label="Custom")
    sid = compute_scope_id(project)

    updated = store.update_label_by_scope_id(sid, None)
    assert updated is not None
    assert updated.label is None
    assert store.load()[0].label is None


def test_store_update_label_unknown_returns_none(tmp_path: Path) -> None:
    store = KnownProjectsStore(tmp_path / "kp.json")
    assert store.update_label_by_scope_id("p-deadbeefcafe", "x") is None


def test_store_update_label_updates_all_duplicate_rows(tmp_path: Path) -> None:
    """A manually corrupted file with duplicate scope_id rows must not leave a
    stale label behind a success (mirrors remove_by_scope_id's all-matching
    semantics). ``add`` dedups by path, so the API never produces this."""
    project = tmp_path / "alpha"
    project.mkdir()
    kp = tmp_path / "kp.json"
    kp.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {"root": str(project), "added_at": "2026-01-01T00:00:00Z", "label": "old1"},
                    {"root": str(project), "added_at": "2026-01-02T00:00:00Z", "label": "old2"},
                ],
            }
        ),
        encoding="utf-8",
    )
    store = KnownProjectsStore(kp)
    sid = compute_scope_id(project)

    updated = store.update_label_by_scope_id(sid, "new")
    assert updated is not None
    entries = store.load()
    assert len(entries) == 2
    assert all(e.label == "new" for e in entries)


# ── enabled (sync enrollment) ────────────────────────────────────────────


def test_store_add_enabled_default_true(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    assert store.load()[0].enabled is True


def test_store_set_enabled_round_trip(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    sid = compute_scope_id(project)

    paused = store.set_enabled_by_scope_id(sid, False)
    assert paused is not None and paused.enabled is False
    assert store.load()[0].enabled is False  # persisted

    resumed = store.set_enabled_by_scope_id(sid, True)
    assert resumed is not None and resumed.enabled is True
    assert store.load()[0].enabled is True


def test_store_legacy_entry_without_enabled_defaults_true(tmp_path: Path) -> None:
    """A pre-``enabled`` schema row (no key) reads back as enabled — old and new
    readers must agree without a version bump."""
    project = tmp_path / "alpha"
    project.mkdir()
    kp = tmp_path / "kp.json"
    kp.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {"root": str(project), "added_at": "2026-01-01T00:00:00Z", "label": None}
                ],
            }
        ),
        encoding="utf-8",
    )
    assert KnownProjectsStore(kp).load()[0].enabled is True


def test_store_set_enabled_unknown_returns_none(tmp_path: Path) -> None:
    store = KnownProjectsStore(tmp_path / "kp.json")
    assert store.set_enabled_by_scope_id("p-deadbeefcafe", False) is None


def test_store_set_enabled_preserves_label_and_added_at(tmp_path: Path) -> None:
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    added_at = store.add(project, label="Alpha").added_at
    sid = compute_scope_id(project)

    updated = store.set_enabled_by_scope_id(sid, False)
    assert updated is not None
    assert updated.label == "Alpha"  # toggling sync never touches the label
    assert updated.added_at == added_at


def test_store_update_label_preserves_enabled(tmp_path: Path) -> None:
    """Regression: a rename must NOT silently resume a paused project (the
    default-True dataclass field would otherwise reset ``enabled``)."""
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project)
    sid = compute_scope_id(project)
    store.set_enabled_by_scope_id(sid, False)

    renamed = store.update_label_by_scope_id(sid, "Renamed")
    assert renamed is not None
    assert renamed.enabled is False  # rename preserved the paused state
    assert store.load()[0].enabled is False


def test_store_update_entry_both_fields_atomic(tmp_path: Path) -> None:
    """``update_entry_by_scope_id`` applies label + enabled in one write."""
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project, label="Old")
    sid = compute_scope_id(project)

    updated = store.update_entry_by_scope_id(
        sid, label="New", set_label=True, enabled=False, set_enabled=True
    )
    assert updated is not None
    assert updated.label == "New"
    assert updated.enabled is False
    reloaded = store.load()[0]
    assert reloaded.label == "New"
    assert reloaded.enabled is False


def test_store_update_entry_unset_fields_preserved(tmp_path: Path) -> None:
    """Fields without their ``set_*`` flag are left untouched."""
    project = tmp_path / "alpha"
    project.mkdir()
    store = KnownProjectsStore(tmp_path / "kp.json")
    store.add(project, label="Keep")
    sid = compute_scope_id(project)
    store.set_enabled_by_scope_id(sid, False)

    # Touch neither field → no-op update that still matches.
    updated = store.update_entry_by_scope_id(sid)
    assert updated is not None
    assert updated.label == "Keep"
    assert updated.enabled is False


def test_store_corrupt_json_recovers_to_empty(tmp_path: Path) -> None:
    kp = tmp_path / "kp.json"
    kp.write_text("not valid {json")
    store = KnownProjectsStore(kp)
    assert store.load() == []


def test_store_unknown_version_recovers_to_empty(tmp_path: Path) -> None:
    kp = tmp_path / "kp.json"
    kp.write_text(json.dumps({"version": 999, "projects": []}))
    store = KnownProjectsStore(kp)
    assert store.load() == []


# ── atomic-write race (real OS-level concurrency, RFC bar) ───────────────


def _race_worker(kp_path_str: str, project_dir_str: str) -> None:
    """Subprocess body for the multiprocess race test.

    Each worker registers a single project root. The test then asserts the
    resulting file is valid JSON and contains *at least* one of the two
    registrations — last-write-wins is acceptable, but the file must never
    be invalid or truncated.
    """
    store = KnownProjectsStore(Path(kp_path_str))
    store.add(Path(project_dir_str))


def test_concurrent_adds_keep_file_valid(tmp_path: Path) -> None:
    kp = tmp_path / "kp.json"
    a = tmp_path / "alpha"
    b = tmp_path / "beta"
    a.mkdir()
    b.mkdir()

    ctx = mp.get_context("spawn")
    p1 = ctx.Process(target=_race_worker, args=(str(kp), str(a)))
    p2 = ctx.Process(target=_race_worker, args=(str(kp), str(b)))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)
    assert p1.exitcode == 0, "worker 1 crashed"
    assert p2.exitcode == 0, "worker 2 crashed"

    raw = kp.read_text()
    doc = json.loads(raw)  # must parse
    assert doc["version"] == 1
    roots = {p["root"] for p in doc["projects"]}
    # Last-write-wins: at least one is present, both possible.
    assert roots & {str(a), str(b)}


# ── discover_project_scopes ─────────────────────────────────────────────


def test_discover_cwd_only(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    kp = tmp_path / "kp.json"
    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    assert len(scopes) == 1
    assert scopes[0].label == "Server CWD"
    assert scopes[0].sources == ("server-cwd",)
    assert scopes[0].experimental is False
    assert scopes[0].missing is False


def test_discover_cwd_plus_known(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    other = tmp_path / "inflearn"
    other.mkdir()
    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(other)

    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    assert len(scopes) == 2
    assert scopes[0].label == "Server CWD"
    assert scopes[1].label == "inflearn"
    assert scopes[1].sources == ("known-projects",)


def test_discover_dedup_cwd_overlap_with_known(tmp_path: Path) -> None:
    """When cwd is also a known-projects entry, sources union; only one scope."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(cwd)

    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    assert len(scopes) == 1
    assert set(scopes[0].sources) == {"server-cwd", "known-projects"}
    # The cwd source wins for the label.
    assert scopes[0].label == "Server CWD"


@pytest.mark.requires_symlinks
def test_discover_symlink_dedup(tmp_path: Path) -> None:
    real = tmp_path / "real_project"
    real.mkdir()
    link = tmp_path / "link_project"
    link.symlink_to(real)

    kp = tmp_path / "kp.json"
    # Register the symlink path; cwd is the resolved path → must collapse.
    KnownProjectsStore(kp).add(link)

    scopes = discover_project_scopes(real, kp, experimental_claude_projects_scan=False)
    assert len(scopes) == 1, f"expected dedup via resolve, got {scopes}"


def test_discover_missing_known_project(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    gone = tmp_path / "ghost"
    gone.mkdir()
    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(gone)
    gone.rmdir()

    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    ghost_scopes = [s for s in scopes if s.label == "ghost"]
    assert len(ghost_scopes) == 1
    assert ghost_scopes[0].missing is True
    # A missing root is never also stale — the two flags are exclusive.
    assert ghost_scopes[0].stale is False


def test_discover_uses_stored_label(tmp_path: Path) -> None:
    """A known-projects entry registered with a label surfaces that label,
    not the directory basename."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    other = tmp_path / "inflearn"
    other.mkdir()
    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(other, label="Inflearn Prod")

    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    labeled = next(s for s in scopes if "known-projects" in s.sources)
    assert labeled.label == "Inflearn Prod"


def test_discover_stored_label_overrides_server_cwd(tmp_path: Path) -> None:
    """An explicit label wins even when the cwd is also a registered project —
    the user's deliberate name beats the 'Server CWD' auto-label."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(cwd, label="My Main Tree")

    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    assert len(scopes) == 1
    assert set(scopes[0].sources) == {"server-cwd", "known-projects"}
    assert scopes[0].label == "My Main Tree"


def test_discover_stale_without_memtomem(tmp_path: Path) -> None:
    """A present root with no ``.memtomem/`` store is reported stale."""
    cwd = tmp_path / "work"
    cwd.mkdir()  # no .memtomem
    kp = tmp_path / "kp.json"
    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    assert scopes[0].missing is False
    assert scopes[0].stale is True


def test_discover_not_stale_with_memtomem(tmp_path: Path) -> None:
    cwd = tmp_path / "work"
    cwd.mkdir()
    (cwd / ".memtomem").mkdir()
    kp = tmp_path / "kp.json"
    scopes = discover_project_scopes(cwd, kp, experimental_claude_projects_scan=False)
    assert scopes[0].stale is False


# ── annotate_project_health ──────────────────────────────────────────────


def _scope(root: Path | None) -> ProjectScope:
    return ProjectScope(
        scope_id="p-000000000000",
        label="x",
        root=root,
        tier="project",
        sources=("server-cwd",),
    )


def test_annotate_health_missing_when_root_none() -> None:
    health = annotate_project_health(_scope(None))
    assert health == ProjectHealth(missing=True, stale=False)


def test_annotate_health_missing_when_root_absent(tmp_path: Path) -> None:
    health = annotate_project_health(_scope(tmp_path / "does_not_exist"))
    assert health == ProjectHealth(missing=True, stale=False)


def test_annotate_health_stale_without_memtomem(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    root.mkdir()
    assert annotate_project_health(_scope(root)) == ProjectHealth(missing=False, stale=True)


def test_annotate_health_healthy_with_memtomem(tmp_path: Path) -> None:
    root = tmp_path / "proj"
    (root / ".memtomem").mkdir(parents=True)
    assert annotate_project_health(_scope(root)) == ProjectHealth(missing=False, stale=False)


def test_annotate_health_file_root_is_missing_not_stale(tmp_path: Path) -> None:
    """A root that exists but is a *file* (not a directory) is missing, not stale."""
    f = tmp_path / "afile"
    f.write_text("x")
    assert annotate_project_health(_scope(f)) == ProjectHealth(missing=True, stale=False)


def test_discover_experimental_scan_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When BOTH scan gates are off, ``~/.claude/projects/`` is never inspected."""
    claude_projects = tmp_path / "fake_home" / ".claude" / "projects"
    claude_projects.mkdir(parents=True)
    cwd = tmp_path / "work"
    cwd.mkdir()

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_CLAUDE_PROJECTS_DIR", claude_projects)

    # ``-tmp`` decodes to ``/tmp`` which exists on every Unix host — would be
    # picked up if a scan gate were open. With both off the scan never runs.
    (claude_projects / "-tmp").mkdir()

    scopes = proj_mod.discover_project_scopes(
        cwd,
        tmp_path / "kp.json",
        experimental_claude_projects_scan=False,
        auto_display_configured_projects=False,
    )
    assert len(scopes) == 1
    assert "claude-projects" not in scopes[0].sources


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only fixture: the `-tmp` slug decodes to `/tmp`, which exists "
    "as a real directory on Unix hosts but not on Windows (the decoder itself is "
    "cross-platform since #1157 — this skip is about the `/tmp` fixture, not the "
    "encoding).",
)
def test_discover_experimental_scan_enabled_filters_misdecoded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag on, only encoded entries that resolve to a real dir survive."""
    claude_projects = tmp_path / "fake_home" / ".claude" / "projects"
    claude_projects.mkdir(parents=True)
    cwd = tmp_path / "work"
    cwd.mkdir()

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_CLAUDE_PROJECTS_DIR", claude_projects)

    # Valid encoded entry — decodes to /tmp, which exists.
    (claude_projects / "-tmp").mkdir()
    # Garbage encoded entry — decodes nowhere.
    (claude_projects / "-Users-no-such-place-here").mkdir()

    scopes = proj_mod.discover_project_scopes(
        cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
    )
    # /tmp scope is discovered via claude-projects only.
    tmp_scopes = [
        s
        for s in scopes
        if s.root is not None and "claude-projects" in s.sources and "server-cwd" not in s.sources
    ]
    assert len(tmp_scopes) == 1, f"expected exactly one claude-only scope, got {scopes}"
    assert tmp_scopes[0].experimental is True
    # The garbage entry must have been filtered by ``Path.is_dir()``.
    assert all("no/such/place" not in str(s.root or "") for s in scopes)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only fixture: uses cwd=Path('/tmp') and the `-tmp` → `/tmp` "
    "slug; `/tmp` does not exist on Windows (the decoder is cross-platform since "
    "#1157 — this skip is about the `/tmp` fixture, not the encoding).",
)
def test_discover_experimental_dedup_with_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If cwd resolves to the same path a claude-projects entry decodes to,
    the two coalesce and ``experimental`` clears (cwd is the trusted source).
    """
    claude_projects = tmp_path / "fake_home" / ".claude" / "projects"
    claude_projects.mkdir(parents=True)

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_CLAUDE_PROJECTS_DIR", claude_projects)

    # ``-tmp`` decodes to ``/tmp`` (resolves to ``/private/tmp`` on macOS,
    # ``/tmp`` on Linux). Pick a cwd that resolves to the same place.
    cwd = Path("/tmp")
    (claude_projects / "-tmp").mkdir()

    scopes = proj_mod.discover_project_scopes(
        cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
    )
    cwd_scope = next(s for s in scopes if "server-cwd" in s.sources)
    assert "claude-projects" in cwd_scope.sources
    # Union with cwd → experimental clears, the trusted source label wins.
    assert cwd_scope.experimental is False
    assert cwd_scope.label == "Server CWD"


# ── auto-display filter + enabled / sync_eligible ───────────────────────


def test_discover_auto_display_filters_scan_by_marker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """auto_display on, experimental off: scan candidates are admitted only when
    their root carries a runtime marker."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    configured = tmp_path / "cfg"
    configured.mkdir()
    (configured / ".codex").mkdir()  # codex-only project (exercises the .codex marker)
    plain = tmp_path / "plain"
    plain.mkdir()  # no runtime marker

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(
        proj_mod, "_discover_claude_projects", lambda anchors=(): [configured, plain]
    )
    scopes = proj_mod.discover_project_scopes(
        cwd,
        tmp_path / "kp.json",
        experimental_claude_projects_scan=False,
        auto_display_configured_projects=True,
    )
    scan_roots = {s.root for s in scopes if "claude-projects" in s.sources}
    assert configured.resolve() in scan_roots  # marker present → admitted
    assert plain.resolve() not in scan_roots  # no marker → filtered out


def test_discover_experimental_bypasses_marker_filter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The experimental flag is the unfiltered escape hatch — an unmarked scan
    candidate still surfaces."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()  # no marker

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_discover_claude_projects", lambda anchors=(): [plain])
    scopes = proj_mod.discover_project_scopes(
        cwd,
        tmp_path / "kp.json",
        experimental_claude_projects_scan=True,
        auto_display_configured_projects=False,
    )
    assert any(s.root == plain.resolve() and "claude-projects" in s.sources for s in scopes)


def test_discover_filter_never_drops_known_or_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The marker filter applies only to scan rows — an unmarked known project
    (and the cwd) are always shown."""
    cwd = tmp_path / "work"
    cwd.mkdir()  # no marker
    known = tmp_path / "known"
    known.mkdir()  # no marker
    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(known)

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_discover_claude_projects", lambda anchors=(): [])
    scopes = proj_mod.discover_project_scopes(
        cwd, kp, experimental_claude_projects_scan=False, auto_display_configured_projects=True
    )
    roots = {s.root for s in scopes}
    assert cwd.resolve() in roots
    assert known.resolve() in roots


def test_discover_sync_eligible_derivation(tmp_path: Path) -> None:
    """sync_eligible = server-cwd OR (enrolled AND enabled)."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    enabled_proj = tmp_path / "en"
    enabled_proj.mkdir()
    disabled_proj = tmp_path / "dis"
    disabled_proj.mkdir()
    kp = tmp_path / "kp.json"
    store = KnownProjectsStore(kp)
    store.add(enabled_proj)
    store.add(disabled_proj)
    store.set_enabled_by_scope_id(compute_scope_id(disabled_proj), False)

    scopes = discover_project_scopes(
        cwd, kp, experimental_claude_projects_scan=False, auto_display_configured_projects=False
    )
    by_root = {s.root: s for s in scopes}
    cwd_scope = next(s for s in scopes if "server-cwd" in s.sources)
    assert cwd_scope.enabled is True and cwd_scope.sync_eligible is True
    assert by_root[enabled_proj.resolve()].enabled is True
    assert by_root[enabled_proj.resolve()].sync_eligible is True
    assert by_root[disabled_proj.resolve()].enabled is False
    assert by_root[disabled_proj.resolve()].sync_eligible is False


def test_discover_paused_known_not_reenabled_by_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression (Blocker 2): a paused known project the scan also surfaces must
    stay sync-ineligible — scan / cwd sources never contribute enablement."""
    project = tmp_path / "proj"
    project.mkdir()
    (project / ".codex").mkdir()
    kp = tmp_path / "kp.json"
    store = KnownProjectsStore(kp)
    store.add(project)
    store.set_enabled_by_scope_id(compute_scope_id(project), False)
    cwd = tmp_path / "work"
    cwd.mkdir()

    from memtomem.context import projects as proj_mod

    # Scan surfaces the SAME root → a naive OR-merge would flip enabled back True.
    monkeypatch.setattr(proj_mod, "_discover_claude_projects", lambda anchors=(): [project])
    scopes = proj_mod.discover_project_scopes(
        cwd, kp, experimental_claude_projects_scan=False, auto_display_configured_projects=True
    )
    scope = next(s for s in scopes if s.root == project.resolve())
    assert "known-projects" in scope.sources
    assert "claude-projects" in scope.sources  # both sources coalesced onto one row
    assert scope.enabled is False
    assert scope.sync_eligible is False  # the scan did NOT re-enable it


def test_discover_auto_displayed_configured_scan_not_experimental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker-bearing scan row admitted by the default filtered auto-display
    path is NOT badged experimental — it is a normal configured project, not the
    opt-in/experimental scan."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    configured = tmp_path / "cfg"
    configured.mkdir()
    (configured / ".claude").mkdir()

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_discover_claude_projects", lambda anchors=(): [configured])
    scopes = proj_mod.discover_project_scopes(
        cwd,
        tmp_path / "kp.json",
        experimental_claude_projects_scan=False,
        auto_display_configured_projects=True,
    )
    scope = next(s for s in scopes if s.root == configured.resolve())
    assert "claude-projects" in scope.sources
    assert scope.experimental is False


def test_discover_unconfigured_scan_row_is_experimental(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unmarked scan row — present only because the unfiltered experimental
    gate is open — IS experimental."""
    cwd = tmp_path / "work"
    cwd.mkdir()
    plain = tmp_path / "plain"
    plain.mkdir()  # no runtime marker

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_discover_claude_projects", lambda anchors=(): [plain])
    scopes = proj_mod.discover_project_scopes(
        cwd,
        tmp_path / "kp.json",
        experimental_claude_projects_scan=True,
        auto_display_configured_projects=False,
    )
    scope = next(s for s in scopes if s.root == plain.resolve())
    assert scope.experimental is True


# ── runtime marker helper (POST validation warning) ─────────────────────


def test_has_runtime_marker_true(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    assert has_runtime_marker(tmp_path) is True


def test_has_runtime_marker_codex(tmp_path: Path) -> None:
    """Codex-configured projects (``.codex``) count as a runtime marker — the
    user's explicit codex/agy/kimi coverage requirement."""
    (tmp_path / ".codex").mkdir()
    assert has_runtime_marker(tmp_path) is True


def test_has_runtime_marker_false(tmp_path: Path) -> None:
    assert has_runtime_marker(tmp_path) is False


def test_has_runtime_marker_file_doesnt_count(tmp_path: Path) -> None:
    """A *file* named ``.claude`` should not satisfy the marker — only directories."""
    (tmp_path / ".claude").write_text("")
    assert has_runtime_marker(tmp_path) is False


# ── FS-guided kebab-case decoder (#1147 B7-1, Windows #1157) ──────────────
#
# The encoder collapses *every* non-ASCII-alphanumeric char to ``-`` (lossy /
# many-to-one); these tests pin the FS-guided reconstruction, which reverses each
# ``-`` to the real on-disk char (``/``, ``.``, ``_``, literal ``-``, …). Note
# pytest's ``tmp_path`` leaf carries ``_`` from the test name, so these fixtures
# exercise the underscore round-trip too. Hermetic: ``_CLAUDE_PROJECTS_DIR``
# (bound at import via ``expanduser()``) is monkeypatched, and target dirs live
# under a real ``tmp_path`` so the reconstruction's absolute ``is_dir()`` probes
# hit fixtures.
#
# These ``@_WIN_SKIP`` cases stay POSIX-only because of the FIXTURE, not the
# decoder: each creates ``~/.claude/projects/<full-encoded-absolute-path>`` — a
# directory whose *name* re-encodes the deep ``tmp_path`` — which on Windows risks
# bumping the legacy ``MAX_PATH`` (260) limit and makes the ``C:\``→target walk
# environment-sensitive (sibling ambiguity), neither of which is decoder
# correctness. The decoder itself IS cross-platform since #1157; its Windows path
# is covered directly by ``test_decode_seed_recognizes_posix_and_windows_roots``
# (string logic, every host) and ``test_decode_windows_drive_root_walk`` (the real
# drive-root walk, Windows-only, no long-named fixture dir).

_WIN_SKIP = pytest.mark.skipif(
    sys.platform == "win32",
    reason="POSIX-only FIXTURE: builds `~/.claude/projects/<full-encoded-abspath>` "
    "from a deep tmp_path, which risks MAX_PATH (260) on Windows and an "
    "environment-sensitive C:\\-rooted walk. The decoder is cross-platform since "
    "#1157 — Windows coverage is in test_decode_seed_* and "
    "test_decode_windows_drive_root_walk.",
)


def _claude_projects_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    from memtomem.context import projects as proj_mod

    cp = tmp_path / "fake_home" / ".claude" / "projects"
    cp.mkdir(parents=True)
    monkeypatch.setattr(proj_mod, "_CLAUDE_PROJECTS_DIR", cp)
    return cp


@_WIN_SKIP
def test_decode_kebab_case_resolves_single_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A literal-dash component (``agent-harness``) resolves — the blind
    ``replace('-', '/')`` decoder would have mis-decoded it to ``agent/harness``."""
    from memtomem.context import projects as proj_mod

    target = tmp_path / "work" / "agent-harness"
    target.mkdir(parents=True)
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / proj_mod._encode_claude_project_path(target)).mkdir()

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    scopes = proj_mod.discover_project_scopes(
        cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
    )
    claude = [s for s in scopes if "claude-projects" in s.sources and "server-cwd" not in s.sources]
    assert len(claude) == 1
    assert claude[0].root == target.resolve()


@_WIN_SKIP
def test_decode_underscore_and_non_ascii_component(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``_`` and non-ASCII chars both encode to ``-`` (Claude Code's real rule,
    anthropics/claude-code#19972). The FS-guided decoder reverses each ``-`` to
    the actual on-disk char, so an underscored / non-Latin path still
    reconstructs — the old hardcoded (``.``, ``-``) branch could not."""
    from memtomem.context import projects as proj_mod

    target = tmp_path / "my_proj" / "데이터"  # underscore + Hangul component
    target.mkdir(parents=True)
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    encoded = proj_mod._encode_claude_project_path(target)
    assert "_" not in encoded and "데이터" not in encoded  # both collapsed to "-"
    (cp / encoded).mkdir()

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    scopes = proj_mod.discover_project_scopes(
        cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
    )
    claude = [s for s in scopes if "claude-projects" in s.sources and "server-cwd" not in s.sources]
    assert len(claude) == 1
    assert claude[0].root == target.resolve()


def test_decode_windows_slug_resolves_via_anchor() -> None:
    """A Windows ``C--…`` slug is resolved by a registered anchor (cwd /
    known_projects) that re-encodes to the same slug — the authoritative,
    filesystem-free resolution path that works on every host. (The FS walk also
    reconstructs an *unregistered* drive dir, but only on a real Windows drive,
    #1157; here we pass anchors and no filesystem, so the anchor returns first.)
    Regression: the ``startswith("-")`` gate used to run BEFORE the anchor loop
    and silently dropped Windows roots (Codex review). Cross-platform
    (PureWindowsPath + anchors only, no filesystem), so it runs on every job."""
    from pathlib import PureWindowsPath

    from memtomem.context import projects as proj_mod

    anchor = PureWindowsPath(r"C:\Users\foo")
    name = proj_mod._encode_claude_project_path(anchor)
    assert name == "C--Users-foo" and not name.startswith("-")  # no leading "-"
    assert proj_mod._decode_claude_project_dirname(name, anchors=(anchor,)) == [anchor]


def test_decode_seed_recognizes_posix_and_windows_roots() -> None:
    """``_decode_seed`` maps an absolute slug to its ``(root, body)`` walk seed.
    Pure string logic (no filesystem), so the Windows drive-root detection added
    in #1157 is pinned on every platform — not just the windows job."""
    from memtomem.context import projects as proj_mod

    # POSIX absolute root: leading "-" is "/" — identical on every host.
    assert proj_mod._decode_seed("-home-foo") == (Path("/"), "home-foo")
    # A UNC slug ("\\host\share" → "--host-share") leads with "-", so it falls
    # through to the POSIX branch (out of scope for the drive-root walk).
    assert proj_mod._decode_seed("--host-share") == (Path("/"), "-host-share")
    # Slugs that are no absolute root at all → None on every host (fail closed):
    assert proj_mod._decode_seed("C-foo") is None  # drive-relative "C:foo", not "C:\"
    assert proj_mod._decode_seed("Ca--b") is None  # two leading letters ≠ a drive
    assert proj_mod._decode_seed("foo-bar") is None  # neither root encoding

    # The drive branch only yields a seed where "C:\" is a genuine absolute root,
    # i.e. on Windows. On POSIX "C:\" is a relative PosixPath, so the slug fails
    # closed — proving the contract instead of relying on cwd contents (#1157
    # review). The drive prefix (and its ":" + "\") is consumed from the body.
    if os.name == "nt":
        assert proj_mod._decode_seed("C--Users-foo") == (Path("C:\\"), "Users-foo")
        assert proj_mod._decode_seed("d--data") == (Path("d:\\"), "data")  # lowercase
        assert proj_mod._decode_seed("C--") == (Path("C:\\"), "")  # bare drive root
    else:
        assert proj_mod._decode_seed("C--Users-foo") is None
        assert proj_mod._decode_seed("C--") is None

    # End-to-end: a drive-rooted slug with no anchors and no matching dir resolves
    # to [] on every host — on POSIX via the None seed, on Windows via a walk that
    # finds nothing. (Cross-platform fail-closed guarantee.)
    assert proj_mod._decode_claude_project_dirname("C--Zzqq-nope-xyzplace") == []


@pytest.mark.skipif(
    os.name != "nt",
    reason="Windows-only: exercises the real C:\\-rooted FS walk for an "
    "unregistered drive slug (#1157). Passes the slug straight to the decoder — no "
    "~/.claude/projects/<long-name> fixture dir — so it avoids the MAX_PATH limit "
    "that keeps the @_WIN_SKIP reconstruction cases POSIX-only.",
)
def test_decode_windows_drive_root_walk(tmp_path: Path) -> None:
    """On Windows, an *unregistered* ``C--…`` slug reconstructs via the drive-root
    FS walk (the gap #1157 closes). No anchors, no ``~/.claude/projects`` fixture:
    the encoded slug goes straight to the decoder, so the only on-disk artifact is
    the short real ``tmp_path`` target — no long-named directory, no MAX_PATH."""
    from memtomem.context import projects as proj_mod

    target = tmp_path / "agent-harness"  # literal-dash leaf, like a real repo
    target.mkdir()
    # Canonicalize BEFORE encoding: GitHub's hosted Windows runners can expose the
    # temp dir via an 8.3 short alias (e.g. ``C:\Users\RUNNER~1\...``) while
    # ``os.scandir`` reports the long name (``runneradmin``). The decoder matches
    # entries with a case-sensitive ``startswith``, so the slug must be spelled the
    # way the filesystem lists it — ``resolve()`` gives that canonical long form.
    target = target.resolve(strict=True)
    slug = proj_mod._encode_claude_project_path(target)
    assert not slug.startswith("-")  # drive-rooted: no leading "-" to strip

    result = proj_mod._decode_claude_project_dirname(slug)
    # Membership, not equality: a deep tmp_path walk could surface a
    # sibling-ambiguous candidate, but the real target must always be among the
    # FS-confirmed results — proving the drive-root walk reaches it.
    assert target in {r.resolve() for r in result}


@_WIN_SKIP
def test_decode_dot_hidden_dir_component(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A hidden, dashed component (``.config-dir``, encoded ``--config-dir``)
    reconstructs — the 3-way branch must consider ``.`` as well as separator
    and literal dash (Codex blocker)."""
    from memtomem.context import projects as proj_mod

    target = tmp_path / "proj" / ".config-dir"
    target.mkdir(parents=True)
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    encoded = proj_mod._encode_claude_project_path(target)
    assert "--config-dir" in encoded  # the `/.` collapsed to `--`
    (cp / encoded).mkdir()

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    scopes = proj_mod.discover_project_scopes(
        cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
    )
    claude = [s for s in scopes if "claude-projects" in s.sources and "server-cwd" not in s.sources]
    assert len(claude) == 1
    assert claude[0].root == target.resolve()


@_WIN_SKIP
def test_decode_no_match_skips_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from memtomem.context import projects as proj_mod

    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / "-this-does-not-exist-anywhere-xyz").mkdir()

    cwd = tmp_path / "work"
    cwd.mkdir()
    with caplog.at_level("WARNING"):
        scopes = proj_mod.discover_project_scopes(
            cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
        )
    assert not [s for s in scopes if "claude-projects" in s.sources]
    assert any("no matching directory" in r.message for r in caplog.records)


@_WIN_SKIP
def test_decode_ambiguous_two_real_dirs_skips_and_warns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``base/a-b/c`` and ``base/a/b-c`` encode to the *same* name; with both on
    disk the decode is genuinely ambiguous → skip + warn (never guess)."""
    from memtomem.context import projects as proj_mod

    base = tmp_path / "base"
    (base / "a-b" / "c").mkdir(parents=True)
    (base / "a" / "b-c").mkdir(parents=True)
    encoded = proj_mod._encode_claude_project_path(base / "a-b" / "c")
    assert encoded == proj_mod._encode_claude_project_path(base / "a" / "b-c")
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / encoded).mkdir()

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    with caplog.at_level("WARNING"):
        scopes = proj_mod.discover_project_scopes(
            cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
        )
    assert not [s for s in scopes if "claude-projects" in s.sources]
    assert any("ambiguous decode" in r.message for r in caplog.records)


@_WIN_SKIP
def test_decode_known_root_anchor_resolves_ambiguity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registered known-project root is an authoritative anchor: it resolves
    a name the pure-FS walk would flag ambiguous."""
    from memtomem.context import projects as proj_mod

    base = tmp_path / "base"
    chosen = base / "a-b" / "c"
    chosen.mkdir(parents=True)
    (base / "a" / "b-c").mkdir(parents=True)  # the rival decode
    encoded = proj_mod._encode_claude_project_path(chosen)
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / encoded).mkdir()

    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(chosen)

    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    scopes = proj_mod.discover_project_scopes(cwd, kp, experimental_claude_projects_scan=True)
    resolved = [s for s in scopes if "claude-projects" in s.sources]
    assert len(resolved) == 1
    assert resolved[0].root == chosen.resolve()


@_WIN_SKIP
def test_decode_anchor_dedup_cwd_equals_known_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """cwd that is ALSO a registered known-project root appears twice in the
    anchor list; dedup by resolved path must not flag it ambiguous (#1151
    review Minor)."""
    from memtomem.context import projects as proj_mod

    target = tmp_path / "work" / "agent-harness"
    target.mkdir(parents=True)
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / proj_mod._encode_claude_project_path(target)).mkdir()

    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(target)  # known-projects entry for cwd

    # cwd == target → anchors = (target, target). Without dedup this looks like
    # two ambiguous candidates and the entry is dropped.
    scopes = proj_mod.discover_project_scopes(target, kp, experimental_claude_projects_scan=True)
    claude = [s for s in scopes if "claude-projects" in s.sources]
    assert len(claude) == 1
    assert claude[0].root == target.resolve()


@_WIN_SKIP
def test_decode_budget_overflow_raises_distinct_from_no_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A frontier overflow must raise ``_DecodeBudgetError`` (distinct from a
    no-match []) so the caller reports it as 'exceeded decode budget', not
    'no matching directory' (#1151 review Major)."""
    from memtomem.context import projects as proj_mod

    base = tmp_path / "base"
    (base / "a").mkdir(parents=True)  # forces a second branch at the 'a-' dash
    (base / "a-b" / "c").mkdir(parents=True)
    target = base / "a-b" / "c"
    encoded = proj_mod._encode_claude_project_path(target)

    # Tiny budget so the genuine multi-branch reconstruction overflows.
    monkeypatch.setattr(proj_mod, "_MAX_DECODE_CANDIDATES", 1)

    # Direct: overflow raises the distinct error rather than returning [].
    with pytest.raises(proj_mod._DecodeBudgetError):
        proj_mod._decode_claude_project_dirname(encoded)

    # Discovery: the warning names the budget, NOT "no matching directory".
    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / encoded).mkdir()
    cwd = tmp_path / "elsewhere"
    cwd.mkdir()
    with caplog.at_level("WARNING"):
        scopes = proj_mod.discover_project_scopes(
            cwd, tmp_path / "kp.json", experimental_claude_projects_scan=True
        )
    assert not [s for s in scopes if "claude-projects" in s.sources]
    msgs = " ".join(r.message for r in caplog.records)
    assert "exceeded decode budget" in msgs
    assert "no matching directory" not in msgs


@_WIN_SKIP
def test_decode_stale_colliding_anchor_does_not_drop_live_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A stale known-project root whose lossy encoding collides with the live
    cwd must NOT make the live match look ambiguous (#1151 re-review): stale
    candidates are filtered by is_dir() before the accept-one decision."""
    from memtomem.context import projects as proj_mod

    live = tmp_path / "work" / "agent-harness"
    live.mkdir(parents=True)
    stale = tmp_path / "work" / "agent" / "harness"  # same encoding, never created
    assert proj_mod._encode_claude_project_path(live) == proj_mod._encode_claude_project_path(stale)

    cp = _claude_projects_dir(tmp_path, monkeypatch)
    (cp / proj_mod._encode_claude_project_path(live)).mkdir()

    kp = tmp_path / "kp.json"
    KnownProjectsStore(kp).add(stale)  # stale anchor (does not exist on disk)

    # cwd == live, so the live anchor + the stale anchor both encode to the slug.
    scopes = proj_mod.discover_project_scopes(live, kp, experimental_claude_projects_scan=True)
    claude = [s for s in scopes if "claude-projects" in s.sources]
    assert len(claude) == 1
    assert claude[0].root == live.resolve()
