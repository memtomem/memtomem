"""Pins for ``_home_guard``: the real-home write detector (#1892).

Everything here runs against a **synthetic** home under ``tmp_path``. Nothing in
this file may read or write the developer's real ``$HOME`` — a guard that damages
the thing it protects while testing itself would be its own best argument.

The negative pins matter more than the positive ones: a fingerprinting guard
that silently stops detecting looks exactly like a clean run, so each detection
path is asserted to actually fire, and each known false-positive shape is
asserted not to.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from . import _home_guard as hg


@pytest.fixture
def fake_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    (home / ".memtomem").mkdir(parents=True)
    return home


# -- the off switch ---------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        ({}, True),
        ({hg.DISABLE_ENV: "off"}, False),
        ({hg.DISABLE_ENV: "OFF"}, False),
        ({hg.DISABLE_ENV: "0"}, False),
        ({hg.DISABLE_ENV: "false"}, False),
        ({hg.DISABLE_ENV: " no "}, False),
        ({hg.DISABLE_ENV: "on"}, True),
        ({hg.DISABLE_ENV: ""}, True),
    ],
)
def test_guard_enabled_parses_the_off_switch(value: dict[str, str], expected: bool) -> None:
    """Pure parsing, so the default can be pinned without touching the real env.

    Asserting ``guard_enabled(os.environ)`` here would fail under the documented
    ``MEMTOMEM_TEST_HOME_GUARD=off`` invocation — the switch would break the test
    that exists to describe it.
    """
    assert hg.guard_enabled(value) is expected


def test_as_home_restores_the_previous_environment(fake_home: Path) -> None:
    before = (os.environ.get("HOME"), os.environ.get("USERPROFILE"))
    with hg.as_home(fake_home):
        assert Path.home() == fake_home
        assert os.environ["USERPROFILE"] == str(fake_home)
    assert (os.environ.get("HOME"), os.environ.get("USERPROFILE")) == before


# -- derivation -------------------------------------------------------------


def test_derivation_is_non_empty_and_home_contained(fake_home: Path) -> None:
    protected = hg.derive_protected(fake_home)
    assert protected.files or protected.roots
    for path in protected.files + protected.roots:
        assert path.is_absolute()
        assert path.is_relative_to(fake_home), f"{path} escapes the synthetic home"


def test_every_fanout_target_is_covered(fake_home: Path) -> None:
    """EVERY non-None (artifact, runtime) target, not one per runtime.

    "At least one of skills/agents/commands per runtime" was the first version of
    this pin and it was too weak by exactly the margin that matters: dropping
    ``commands`` from the derivation left it green. A new runtime — or a new
    artifact kind — must be protected the day it lands, and a hand-edit replacing
    the derivation with a literal list must fail here.
    """
    from memtomem.context._runtime_targets import KNOWN_RUNTIMES, runtime_fanout_root

    protected = hg.derive_protected(fake_home)
    everything = set(protected.files) | set(protected.roots)

    missing: list[str] = []
    checked = 0
    for artifact in ("skills", "agents", "commands"):
        for runtime in KNOWN_RUNTIMES:
            with hg.as_home(fake_home):
                target = runtime_fanout_root(artifact, runtime, "user", None)
            if target is None:
                continue  # no fan-out by design
            checked += 1
            if not any(p == Path(target) or Path(target).is_relative_to(p) for p in everything):
                missing.append(f"{artifact}/{runtime} -> {target}")

    assert checked >= len(KNOWN_RUNTIMES), "fan-out sweep found almost nothing — it is broken"
    assert not missing, "unprotected fan-out targets:\n  " + "\n  ".join(missing)


def test_every_settings_target_is_covered(fake_home: Path) -> None:
    """Each generator's user-scope target must actually be in the watched set."""
    from memtomem.context.settings import SETTINGS_GENERATORS

    protected = hg.derive_protected(fake_home)
    watched = set(protected.files) | set(protected.roots)

    missing: list[str] = []
    for name, generator in SETTINGS_GENERATORS.items():
        with hg.as_home(fake_home):
            target = generator.target_file(fake_home / "nope", "user")
        if target is None:
            continue
        if not any(Path(target) == p or Path(target).is_relative_to(p) for p in watched):
            missing.append(f"{name} -> {target}")
    assert SETTINGS_GENERATORS, "no settings generators registered — the sweep is broken"
    assert not missing, "unprotected settings targets:\n  " + "\n  ".join(missing)


def test_user_scope_targets_ignore_project_root(fake_home: Path) -> None:
    """The derivation passes a nonexistent project root; pin that it cannot matter."""
    from memtomem.context.settings import SETTINGS_GENERATORS

    with hg.as_home(fake_home):
        for generator in SETTINGS_GENERATORS.values():
            first = generator.target_file(fake_home / "project-a", "user")
            second = generator.target_file(fake_home / "project-b", "user")
            assert first == second, f"{generator.name}: user scope depends on project_root"


def test_fastembed_cache_is_excluded(fake_home: Path) -> None:
    """The model cache is legitimately written by the golden-path run."""
    protected = hg.derive_protected(fake_home)
    cache = fake_home / ".memtomem" / "cache"
    assert cache in protected.excluded
    assert not any(p.is_relative_to(cache) for p in protected.files + protected.roots)


def test_wiki_root_comes_from_the_call_time_resolver(fake_home: Path) -> None:
    """Not ``DEFAULT_WIKI_PATH`` — that constant is frozen to the real home.

    Using it would put a real-home path in a synthetic-home derivation, where the
    ``is_relative_to(home)`` filter then silently drops it and the wiki ends up
    unguarded in production too.
    """
    protected = hg.derive_protected(fake_home)
    assert (fake_home / ".memtomem-wiki") in set(protected.roots)


def test_effective_configured_db_path_is_picked_up(fake_home: Path) -> None:
    """A developer who moved their DB elsewhere under $HOME is still covered."""
    moved = fake_home / "elsewhere" / "custom.db"
    (fake_home / ".memtomem" / "config.json").write_text(
        json.dumps({"storage": {"sqlite_path": str(moved)}}), encoding="utf-8"
    )
    protected = hg.derive_protected(fake_home)
    assert moved in set(protected.roots)


def test_indexed_memory_dirs_are_not_protected(fake_home: Path) -> None:
    """Inputs the suite reads are not outputs it owns.

    ``indexing.memory_dirs`` resolves on a real machine to things like
    ``~/.claude/projects/<slug>/memory`` and ``~/.claude/plans`` — directories the
    developer's own coding agent rewrites continuously, concurrently with any
    test run. Guarding them would turn someone else's ordinary write into a red
    build, and a guard that cries wolf gets switched off. Pinned because the
    symmetry with ``sqlite_path`` above makes "just add memory_dirs too" look
    like an obvious completion.
    """
    indexed = fake_home / "notes"
    (fake_home / ".memtomem" / "config.json").write_text(
        json.dumps({"indexing": {"memory_dirs": [str(indexed)]}}), encoding="utf-8"
    )
    protected = hg.derive_protected(fake_home)
    assert indexed not in set(protected.roots)


def test_derivation_refuses_to_arm_on_an_empty_set(fake_home: Path, monkeypatch) -> None:
    """An empty protected set looks exactly like a clean run — fail loudly instead.

    Simulates every production registry going away at once (renamed, moved, or an
    import that silently started returning nothing), which is the shape in which
    this guard would rot into a no-op.
    """
    from memtomem.context import runtime_registry, scope_resolver, settings
    from memtomem.wiki import store as wiki_store

    monkeypatch.setattr(settings, "SETTINGS_GENERATORS", {})
    monkeypatch.setattr(runtime_registry, "registry_location_paths", lambda *a, **k: {})
    monkeypatch.setattr(hg, "_ARTIFACTS", ())
    monkeypatch.setattr(hg, "_effective_config_paths", lambda home: [])
    # The two remaining roots are absolute constants — point them outside the
    # home so the ``is_relative_to(home)`` filter drops them.
    monkeypatch.setattr(scope_resolver, "DEFAULT_USER_ARTIFACT_BASE", Path("/dev/null/nope"))
    monkeypatch.setattr(wiki_store, "_wiki_path_from_env", lambda: Path("/dev/null/nope"))

    with pytest.raises(hg.HomeGuardError, match="derivation produced nothing"):
        hg.derive_protected(fake_home)


# -- per-test file tier -----------------------------------------------------


@pytest.fixture
def watched(fake_home: Path) -> Path:
    target = fake_home / ".claude" / "settings.json"
    target.write_text('{"model": "opus"}', encoding="utf-8")
    return target


def _cycle(paths: tuple[Path, ...], mutate) -> list[hg.Violation]:
    before = hg.snapshot_files(paths)
    digests = hg.snapshot_file_digests(paths)
    mutate()
    return hg.diff_files(before, hg.snapshot_files(paths), digests=digests)


def test_content_change_is_a_violation(watched: Path) -> None:
    violations = _cycle((watched,), lambda: watched.write_text("{}", encoding="utf-8"))
    assert [v.kind for v in violations] == ["modified"]
    assert "sha256" in violations[0].detail


def test_byte_identical_rewrite_is_not_a_violation(watched: Path) -> None:
    """Observed in the wild: an editor rewrote settings.json with identical bytes.

    Only the mtime moved. A metadata-only comparison would have blamed whichever
    test happened to be running.
    """
    original = watched.read_text(encoding="utf-8")

    def rewrite() -> None:
        watched.write_text(original, encoding="utf-8")
        st = watched.stat()
        os.utime(watched, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))

    assert _cycle((watched,), rewrite) == []


def test_deletion_is_a_violation(watched: Path) -> None:
    violations = _cycle((watched,), watched.unlink)
    assert [v.kind for v in violations] == ["deleted"]


def test_creation_of_an_absent_file_is_a_violation(fake_home: Path) -> None:
    """The CI shape of #1892: the runner has no ~/.claude/settings.json at all.

    A guard that only compared existing files would have stayed green on CI while
    the suite created one.
    """
    absent = fake_home / ".claude" / "settings.json"
    violations = _cycle((absent,), lambda: absent.write_text("{}", encoding="utf-8"))
    assert [v.kind for v in violations] == ["created"]


def test_untouched_file_is_clean(watched: Path) -> None:
    assert _cycle((watched,), lambda: None) == []


# -- session tree tier ------------------------------------------------------


def test_new_entry_in_a_protected_tree_is_a_violation(fake_home: Path) -> None:
    root = fake_home / ".claude" / "skills"
    (root / "existing").mkdir(parents=True)
    (root / "existing" / "SKILL.md").write_text("a", encoding="utf-8")

    before = hg.tree_manifest((root,))
    (root / "sneaky").mkdir()
    (root / "sneaky" / "SKILL.md").write_text("b", encoding="utf-8")
    violations = hg.diff_trees(before, hg.tree_manifest((root,)))

    # Two: the directory and the file. Directories are entries in their own
    # right so that creating or removing an EMPTY one is visible — a file-only
    # manifest cannot see it at all.
    assert [v.kind for v in violations] == ["created", "created"]
    assert any(v.path.endswith("SKILL.md") for v in violations)


def test_empty_directory_creation_is_a_violation(fake_home: Path) -> None:
    """The gap a file-only manifest leaves."""
    root = fake_home / ".claude" / "skills"
    root.mkdir(parents=True)
    before = hg.tree_manifest((root,))
    (root / "empty").mkdir()
    assert [v.kind for v in hg.diff_trees(before, hg.tree_manifest((root,)))] == ["created"]


def test_nested_content_change_is_a_violation(fake_home: Path) -> None:
    """The gap a shallow directory stamp would leave: parent mtime does not move."""
    root = fake_home / ".claude" / "skills"
    nested = root / "thing" / "SKILL.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("before", encoding="utf-8")

    before = hg.tree_manifest((root,))
    nested.write_text("after!", encoding="utf-8")  # same length, different bytes
    violations = hg.diff_trees(before, hg.tree_manifest((root,)))

    assert [v.kind for v in violations] == ["modified"]


def test_tree_byte_identical_rewrite_is_not_a_violation(fake_home: Path) -> None:
    root = fake_home / ".claude" / "skills"
    nested = root / "thing" / "SKILL.md"
    nested.parent.mkdir(parents=True)
    nested.write_text("same", encoding="utf-8")

    before = hg.tree_manifest((root,))
    nested.write_text("same", encoding="utf-8")
    st = nested.stat()
    os.utime(nested, ns=(st.st_atime_ns + 10**9, st.st_mtime_ns + 10**9))

    assert hg.diff_trees(before, hg.tree_manifest((root,))) == []


def test_oversized_file_degrades_to_metadata_and_says_so(fake_home: Path, monkeypatch) -> None:
    """The ~99 MB SQLite DB is why this path exists — never imply content coverage."""
    monkeypatch.setattr(hg, "MAX_DIGEST_BYTES", 4)
    root = fake_home / ".memtomem"
    big = root / "memtomem.db"
    big.write_text("0123456789", encoding="utf-8")

    before = hg.tree_manifest((root,))
    assert str(big) in before.metadata_only

    os.utime(big, ns=(0, 0))
    violations = hg.diff_trees(before, hg.tree_manifest((root,)))
    assert len(violations) == 1
    assert "content is not covered" in violations[0].detail


def test_entry_cap_aborts_rather_than_warning(fake_home: Path) -> None:
    """A partially watched root looks identical to a clean one."""
    root = fake_home / ".memtomem" / "memories"
    root.mkdir(parents=True)
    for i in range(5):
        (root / f"{i}.md").write_text("x", encoding="utf-8")

    with pytest.raises(hg.HomeGuardError, match="Refusing to arm"):
        hg.tree_manifest((root,), max_entries=3)


# -- shapes Codex found on review of #1903 ----------------------------------


def test_walker_honours_exclusions_not_just_derivation(fake_home: Path) -> None:
    """The cache sits INSIDE ~/.memtomem, which is itself a protected root.

    Pinning only that derivation drops the cache is a self-certifying
    half-measure: the walk descends into it anyway, and every fastembed model
    download becomes a violation for whichever test is running.
    """
    protected = hg.derive_protected(fake_home)
    root = fake_home / ".memtomem"
    before = hg.tree_manifest((root,), excluded=protected.excluded)
    cache_file = root / "cache" / "fastembed" / "model.onnx"
    cache_file.parent.mkdir(parents=True)
    cache_file.write_text("weights", encoding="utf-8")
    assert hg.diff_trees(before, hg.tree_manifest((root,), excluded=protected.excluded)) == []


def test_same_size_restored_mtime_content_change_is_caught(fake_home: Path) -> None:
    """A stat fast path is blind to this, and the repo writes preserved-mtime fixtures."""
    target = fake_home / ".claude" / "settings.json"
    target.write_text("AAAA", encoding="utf-8")
    st = target.stat()

    def rewrite() -> None:
        target.write_text("BBBB", encoding="utf-8")  # same length
        os.utime(target, ns=(st.st_atime_ns, st.st_mtime_ns))  # mtime restored

    assert [v.kind for v in _cycle((target,), rewrite)] == ["modified"]


def test_symlink_is_recorded_not_followed(fake_home: Path, tmp_path: Path) -> None:
    """Following a link lets it bypass the digest cap: lstat sizes the link,
    the read chases the target — which may be huge, or a blocking device."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (hg.MAX_DIGEST_BYTES + 10))
    root = fake_home / ".claude" / "skills"
    root.mkdir(parents=True)
    (root / "link").symlink_to(big)

    manifest = hg.tree_manifest((root,))
    entry = manifest.entries[str(root / "link")]
    assert entry.startswith("symlink:"), entry
    assert "sha256" not in entry


def test_sqlite_sidecars_are_covered(fake_home: Path) -> None:
    """A WAL-mode write can land entirely in -wal and leave the .db bytes alone."""
    moved = fake_home / "db" / "custom.db"
    (fake_home / ".memtomem" / "config.json").write_text(
        json.dumps({"storage": {"sqlite_path": str(moved)}}), encoding="utf-8"
    )
    roots = set(hg.derive_protected(fake_home).roots)
    assert moved in roots
    assert moved.with_name("custom.db-wal") in roots
    assert moved.with_suffix(".provenance_key") in roots


def test_detection_only_registry_paths_are_not_watched(fake_home: Path) -> None:
    """registry_location_paths lists what memtomem PROBES, not what it writes.

    The Antigravity configs are read and shown as manual paste targets. Watching
    them blames Antigravity's own churn on whichever test is running.
    """
    watched = set(hg.derive_protected(fake_home).files)
    assert (fake_home / ".gemini" / "antigravity" / "mcp_config.json") not in watched
    assert (fake_home / ".claude.json") not in watched


# -- shapes Codex reproduced on re-gate of #1903 -----------------------------


def test_empty_root_creation_is_a_violation(fake_home: Path) -> None:
    """A watched root appearing at all is the event; it need not have contents.

    Recording only a root's CHILDREN made this invisible: absent root and
    freshly-created empty root both manifest as {}.
    """
    root = fake_home / ".claude" / "skills"
    assert not root.exists()
    before = hg.tree_manifest((root,))
    root.mkdir(parents=True)
    assert [v.kind for v in hg.diff_trees(before, hg.tree_manifest((root,)))] == ["created"]


def test_directory_symlink_retarget_is_a_violation(fake_home: Path, tmp_path: Path) -> None:
    """``followlinks=False`` stops the walk but still lists the link in dirnames.

    Recording it as a plain "dir" made retargeting it — pointing a watched skills
    directory at somewhere else entirely — produce identical manifests.
    """
    root = fake_home / ".claude" / "skills"
    root.mkdir(parents=True)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    link = root / "linked"
    link.symlink_to(tmp_path / "a")

    before = hg.tree_manifest((root,))
    link.unlink()
    link.symlink_to(tmp_path / "b")

    assert [v.kind for v in hg.diff_trees(before, hg.tree_manifest((root,)))] == ["modified"]


def test_per_test_tier_does_not_follow_symlinks(fake_home: Path, tmp_path: Path) -> None:
    """The tier is hashed on EVERY test — following a link would let one huge or
    blocking target be read 10,300 times."""
    big = tmp_path / "big.bin"
    big.write_bytes(b"x" * (hg.MAX_DIGEST_BYTES + 10))
    link = fake_home / ".claude" / "settings.json"
    link.symlink_to(big)

    prints = hg.snapshot_file_digests((link,))
    assert prints[str(link)].startswith("symlink:")


def test_unreadable_files_are_not_all_equal(fake_home: Path) -> None:
    """Two different unreadable files both hashed to None and compared equal."""
    a = fake_home / "a.bin"
    a.write_bytes(b"aaaa")
    b = fake_home / "b.bin"
    b.write_bytes(b"bbbbbbbb")
    a.chmod(0o000)
    b.chmod(0o000)
    try:
        pa, pb = hg.fingerprint(a), hg.fingerprint(b)
        if pa is not None and pa.startswith("unreadable:"):
            assert pa != pb, "distinct unreadable files must not fingerprint alike"
    finally:
        a.chmod(0o600)
        b.chmod(0o600)


# -- fail-closed edge cases (round-3 review) ---------------------------------


def test_unreadable_directory_aborts_rather_than_reading_as_empty(fake_home: Path) -> None:
    """``os.walk`` swallows traversal errors by default and yields nothing.

    A change inside an unreadable subtree then produces an identical manifest —
    the guard reports "clean" about a directory it could not even open.
    """
    root = fake_home / ".memtomem"
    locked = root / "locked"
    locked.mkdir(parents=True)
    (locked / "secret.md").write_text("x", encoding="utf-8")
    locked.chmod(0o000)
    try:
        with pytest.raises(hg.HomeGuardError, match="cannot traverse"):
            hg.tree_manifest((root,))
    finally:
        locked.chmod(0o700)


def test_directory_symlink_subtree_is_reported_as_uncovered(
    fake_home: Path, tmp_path: Path
) -> None:
    """Retargeting is detected, but writes THROUGH the link are not.

    That gap is surfaced instead of skipped silently: an uncovered subtree
    nobody mentions is the same failure shape as a guard that is not armed.
    """
    root = fake_home / ".claude" / "skills"
    root.mkdir(parents=True)
    (tmp_path / "elsewhere").mkdir()
    (root / "linked").symlink_to(tmp_path / "elsewhere")

    manifest = hg.tree_manifest((root,))
    assert manifest.uncovered == (str(root / "linked"),)


def test_dangling_root_symlink_is_still_fingerprinted(fake_home: Path, tmp_path: Path) -> None:
    """``exists()`` is False for a broken link, so an exists-first check skips it."""
    root = fake_home / ".claude" / "skills"
    root.parent.mkdir(parents=True, exist_ok=True)
    root.symlink_to(tmp_path / "gone")
    assert not root.exists()

    manifest = hg.tree_manifest((root,))
    assert manifest.entries[str(root)].startswith("symlink:")


def test_oversized_file_is_never_hashed_even_if_it_grows(fake_home: Path, monkeypatch) -> None:
    """The cap is enforced on the read, not only on the pre-open stat.

    Checking size before opening is a race: the file can grow past the cap in
    between, and the old code streamed the whole thing.
    """
    monkeypatch.setattr(hg, "MAX_DIGEST_BYTES", 16)
    big = fake_home / "big.bin"
    big.write_bytes(b"x" * 64)
    result = hg.fingerprint(big)
    assert result is not None and result.startswith("meta:"), result
