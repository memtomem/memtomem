"""Tests for ``memtomem.context.projects`` — multi-project discovery.

PR2 minimum-bar from the RFC (`multi-project-context-ui-rfc.md` §Test
obligations): scope_id stability + collision sanity, atomic-write race
through real OS-level concurrency, symlink dedup, both
``experimental_claude_projects_scan`` defaults.
"""

from __future__ import annotations

import json
import multiprocessing as mp
import sys
from pathlib import Path

import pytest

from memtomem.context.projects import (
    KnownProjectsStore,
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


def test_discover_experimental_scan_disabled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When the flag is False, ``~/.claude/projects/`` is never inspected."""
    claude_projects = tmp_path / "fake_home" / ".claude" / "projects"
    claude_projects.mkdir(parents=True)
    cwd = tmp_path / "work"
    cwd.mkdir()

    from memtomem.context import projects as proj_mod

    monkeypatch.setattr(proj_mod, "_CLAUDE_PROJECTS_DIR", claude_projects)

    # ``-tmp`` decodes to ``/tmp`` which exists on every Unix host — would be
    # picked up if the flag were True. With it False the scan never runs.
    (claude_projects / "-tmp").mkdir()

    scopes = proj_mod.discover_project_scopes(
        cwd, tmp_path / "kp.json", experimental_claude_projects_scan=False
    )
    assert len(scopes) == 1
    assert "claude-projects" not in scopes[0].sources


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


# ── runtime marker helper (POST validation warning) ─────────────────────


def test_has_runtime_marker_true(tmp_path: Path) -> None:
    (tmp_path / ".claude").mkdir()
    assert has_runtime_marker(tmp_path) is True


def test_has_runtime_marker_false(tmp_path: Path) -> None:
    assert has_runtime_marker(tmp_path) is False


def test_has_runtime_marker_file_doesnt_count(tmp_path: Path) -> None:
    """A *file* named ``.claude`` should not satisfy the marker — only directories."""
    (tmp_path / ".claude").write_text("")
    assert has_runtime_marker(tmp_path) is False
