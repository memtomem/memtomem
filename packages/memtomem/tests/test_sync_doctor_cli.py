"""Tests for ``mm sync-doctor`` (Phase 2 of multi-device sync RFC)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.sync_doctor_cmd import (
    check_claude_slug,
    check_cloud_mount,
    check_config_d_present,
    check_config_json_absent,
    check_memory_dirs_under_home,
    check_no_db_staged,
    cloud_mount_prefix,
)


def _git_init(repo: Path) -> None:
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)


def _git_add(repo: Path, *paths: str) -> None:
    subprocess.run(["git", "add", *paths], cwd=repo, check=True)


def _set_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> None:
    """Point ``Path.home()`` at ``home`` on both POSIX and Windows.

    ``os.path.expanduser`` consults ``USERPROFILE`` on Windows; setting only
    ``HOME`` leaves Windows CI looking at the real user profile (reviewer
    note from PR #838).
    """
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))


def _claude_slug_for(cwd: Path) -> str:
    """Encode ``cwd`` into Claude Code's hyphenated slug for test fixtures.

    Delegates to the production encoder so fixtures always match the real rule
    (every character outside ASCII ``[A-Za-z0-9]`` → ``-``, incl. ``_`` and the
    Windows drive ``:``). POSIX ``/Users/foo/bar`` → ``-Users-foo-bar``; Windows
    ``C:\\Users\\foo\\bar`` → ``C--Users-foo-bar``.
    """
    from memtomem.context.projects import _encode_claude_project_path

    return _encode_claude_project_path(cwd.resolve())


# ---- cloud_mount_prefix helper ---------------------------------------------


class TestCloudMountPrefix:
    def test_cloudstorage(self, tmp_path: Path) -> None:
        p = tmp_path / "Library" / "CloudStorage" / "GoogleDrive-foo" / "memories"
        assert cloud_mount_prefix(p, home=tmp_path) == "~/Library/CloudStorage/"

    def test_icloud(self, tmp_path: Path) -> None:
        p = tmp_path / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "memories"
        assert (
            cloud_mount_prefix(p, home=tmp_path)
            == "~/Library/Mobile Documents/com~apple~CloudDocs/"
        )

    def test_dropbox(self, tmp_path: Path) -> None:
        p = tmp_path / "Dropbox" / "memories"
        assert cloud_mount_prefix(p, home=tmp_path) == "~/Dropbox/"

    @pytest.mark.parametrize(
        "name",
        ["OneDrive", "OneDrive-Personal", "OneDrive - Acme"],
    )
    def test_onedrive_variants(self, tmp_path: Path, name: str) -> None:
        p = tmp_path / name / "memories"
        assert cloud_mount_prefix(p, home=tmp_path) == "~/OneDrive*/"

    def test_no_match_under_home(self, tmp_path: Path) -> None:
        p = tmp_path / ".memtomem" / "memories"
        assert cloud_mount_prefix(p, home=tmp_path) is None

    def test_onedrive_substring_no_false_positive(self, tmp_path: Path) -> None:
        # ``~/OneDriveStuff`` is not a OneDrive mount — must not match.
        p = tmp_path / "OneDriveStuff" / "x"
        assert cloud_mount_prefix(p, home=tmp_path) is None


# ---- check_no_db_staged ----------------------------------------------------


class TestCheckNoDbStaged:
    def test_clean(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        r = check_no_db_staged(tmp_path)
        assert r.status == "pass"

    @pytest.mark.parametrize("name", ["snapshot.db", "notes.db-wal", "notes.db-shm"])
    def test_db_family_staged_fails(self, tmp_path: Path, name: str) -> None:
        _git_init(tmp_path)
        (tmp_path / name).write_bytes(b"")
        _git_add(tmp_path, name)
        r = check_no_db_staged(tmp_path)
        assert r.status == "fail"

    def test_not_a_git_repo_warns(self, tmp_path: Path) -> None:
        r = check_no_db_staged(tmp_path)
        assert r.status == "warn"

    def test_subdir_invocation_sees_root_files(self, tmp_path: Path) -> None:
        # When invoked from a nested subdir, the check must still see
        # tracked *.db at the repo root (reviewer note from PR #838).
        _git_init(tmp_path)
        (tmp_path / "snapshot.db").write_bytes(b"")
        _git_add(tmp_path, "snapshot.db")
        nested = tmp_path / "deep" / "nested"
        nested.mkdir(parents=True)
        r = check_no_db_staged(nested)
        assert r.status == "fail"
        assert "snapshot.db" in (r.detail or "")


# ---- check_config_json_absent ----------------------------------------------


class TestCheckConfigJsonAbsent:
    def test_clean(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        r = check_config_json_absent(tmp_path)
        assert r.status == "pass"

    def test_staged_at_root(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        (tmp_path / "config.json").write_text("{}")
        _git_add(tmp_path, "config.json")
        r = check_config_json_absent(tmp_path)
        assert r.status == "fail"

    def test_staged_at_subpath(self, tmp_path: Path) -> None:
        _git_init(tmp_path)
        sub = tmp_path / "sub"
        sub.mkdir()
        (sub / "config.json").write_text("{}")
        _git_add(tmp_path, "sub/config.json")
        r = check_config_json_absent(tmp_path)
        assert r.status == "fail"


# ---- check_config_d_present ------------------------------------------------


class TestCheckConfigDPresent:
    def test_present_with_fragments(self, tmp_path: Path) -> None:
        d = tmp_path / "config.d"
        d.mkdir()
        (d / "10-rules.json").write_text("{}")
        (d / "20-other.json").write_text("{}")
        r = check_config_d_present(d)
        assert r.status == "pass"
        assert "2 files" in r.message

    def test_dir_present_but_empty(self, tmp_path: Path) -> None:
        d = tmp_path / "config.d"
        d.mkdir()
        r = check_config_d_present(d)
        assert r.status == "warn"

    def test_dir_missing(self, tmp_path: Path) -> None:
        r = check_config_d_present(tmp_path / "absent")
        assert r.status == "warn"

    def test_symlinked_fragment_is_counted(self, tmp_path: Path) -> None:
        # The multi-device sync guide recommends bridging synced fragments
        # into ``~/.memtomem/config.d/`` via symlink so edits flow back to
        # the synced repo automatically. ``Path.glob("*.json")`` follows
        # symlinks today; pinning so a refactor to a non-following
        # iteration (e.g. Python 3.13's ``glob(..., follow_symlinks=False)``
        # or a ``set(d.iterdir())`` walk that stat-filters them out) trips
        # the test.
        synced_dir = tmp_path / "synced-repo" / "config.d"
        synced_dir.mkdir(parents=True)
        target = synced_dir / "10-namespace-rules.json"
        target.write_text("{}")

        canonical_dir = tmp_path / ".memtomem" / "config.d"
        canonical_dir.mkdir(parents=True)
        try:
            (canonical_dir / "10-namespace-rules.json").symlink_to(target)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symlink creation unavailable in this environment: {exc}")

        r = check_config_d_present(canonical_dir)
        assert r.status == "pass"
        assert "1 files" in r.message


# ---- check_memory_dirs_under_home ------------------------------------------


class TestCheckMemoryDirsUnderHome:
    def test_under_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_home(monkeypatch, tmp_path)
        d = tmp_path / "memories"
        d.mkdir()
        r = check_memory_dirs_under_home([d])
        assert r.status == "pass"

    def test_outside_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        outside_home = tmp_path / "home"
        outside_home.mkdir()
        _set_home(monkeypatch, outside_home)
        # An entry under tmp_path but not under tmp_path/home.
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        r = check_memory_dirs_under_home([outside])
        assert r.status == "warn"


# ---- check_cloud_mount -----------------------------------------------------


class TestCheckCloudMount:
    def test_no_cloud(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_home(monkeypatch, tmp_path)
        r = check_cloud_mount([tmp_path / "memories"])
        assert r.status == "pass"

    def test_dropbox_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_home(monkeypatch, tmp_path)
        r = check_cloud_mount([tmp_path / "Dropbox" / "memories"])
        assert r.status == "warn"
        assert "Dropbox" in r.message

    def test_outside_synced_worktree_still_warns(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Pin the broader-scope decision from PR #838 review: the cloud-mount
        # check is a watcher-reliability check, not a worktree-hygiene check,
        # so it scans every entry in ``memory_dirs[]`` — including entries
        # outside the synced repo's worktree. Narrowing to "only entries
        # inside the synced worktree" was the alternative that didn't land;
        # this test makes a future flip a deliberate fixture edit.
        _set_home(monkeypatch, tmp_path)
        synced_repo = tmp_path / "synced-repo"
        inside_worktree = synced_repo / "memories"  # not on a cloud mount
        outside_worktree = tmp_path / "Dropbox" / "agent-memory"
        r = check_cloud_mount([inside_worktree, outside_worktree])
        assert r.status == "warn"
        assert "Dropbox" in r.message


# ---- check_claude_slug -----------------------------------------------------


class TestCheckClaudeSlug:
    def test_projects_dir_absent_is_info(self, tmp_path: Path) -> None:
        # No ~/.claude/projects → skip the check entirely.
        r = check_claude_slug(tmp_path, home=tmp_path / "home")
        assert r.status == "info"

    def test_slug_matches(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        cwd = home / "work" / "repo"
        cwd.mkdir(parents=True)
        projects = home / ".claude" / "projects"
        projects.mkdir(parents=True)
        (projects / _claude_slug_for(cwd)).mkdir()
        r = check_claude_slug(cwd, home=home)
        assert r.status == "pass"

    def test_slug_mismatch_fails(self, tmp_path: Path) -> None:
        home = tmp_path / "home"
        home.mkdir()
        cwd = home / "work" / "repo"
        cwd.mkdir(parents=True)
        projects = home / ".claude" / "projects"
        projects.mkdir(parents=True)
        (projects / "-Users-someone-else-repo").mkdir()
        r = check_claude_slug(cwd, home=home)
        assert r.status == "fail"

    def test_dotted_cwd_slug_matches(self, tmp_path: Path) -> None:
        # Claude Code collapses '.' to '-' too; the old '/'-only encode + blind
        # '-'->'/' decode falsely failed a dotted cwd the UI can now resolve
        # (#1151 re-review). The shared encoder/decoder must make it pass.
        from memtomem.context import projects as proj_mod

        home = tmp_path / "home"
        home.mkdir()
        cwd = home / "work" / ".config-dir"
        cwd.mkdir(parents=True)
        projects = home / ".claude" / "projects"
        projects.mkdir(parents=True)
        (projects / proj_mod._encode_claude_project_path(cwd.resolve())).mkdir()
        r = check_claude_slug(cwd, home=home)
        assert r.status == "pass"


class TestEncodeClaudeProjectPath:
    """Pin Claude Code's slug rule (anthropics/claude-code#19972): every char
    outside ASCII ``[A-Za-z0-9]`` becomes a single ``-``. Expected values are
    literals checked against the production encoder — never recomputed with
    ``re.sub`` (that would be tautological and could not catch a revert)."""

    def test_posix_collapses_slash_dot_and_underscore(self) -> None:
        # The old ``replace("/","-").replace(".","-")`` left ``_`` intact (and
        # only ran on POSIX); this literal fails on that old body.
        from memtomem.context.projects import _encode_claude_project_path

        assert _encode_claude_project_path(Path("/a/b_c.d")) == "-a-b-c-d"

    def test_windows_drive_and_backslash(self) -> None:
        # PureWindowsPath renders backslashes + the drive colon even on a POSIX
        # CI host, so this exercises the real ``C--`` slug without a Windows box.
        # The drive colon is not special-cased: ``C:\`` → ``C--``.
        from pathlib import PureWindowsPath

        from memtomem.context.projects import _encode_claude_project_path

        assert _encode_claude_project_path(PureWindowsPath(r"C:\Users\foo")) == "C--Users-foo"

    def test_non_ascii_becomes_single_dash(self) -> None:
        # Korean/CJK/accented chars are replaced, not preserved (#19972) — one
        # dash per char. ``가`` is the Hangul syllable 가 (single codepoint,
        # so this does not depend on the source file's NFC/NFD normalization).
        from memtomem.context.projects import _encode_claude_project_path

        assert _encode_claude_project_path(Path("/a/b가c")) == "-a-b-c"


# ---- CLI end-to-end --------------------------------------------------------


class TestSyncDoctorCli:
    def test_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor", "--help"])
        assert result.exit_code == 0
        assert "Validate" in result.output

    def test_exits_nonzero_when_db_staged(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _set_home(monkeypatch, tmp_path)
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        (tmp_path / "snapshot.db").write_bytes(b"")
        _git_add(tmp_path, "snapshot.db")

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor"])
        assert result.exit_code == 1
        assert "*.db" in result.output

    def test_pass_path_clean_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_home(monkeypatch, tmp_path)
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor"])
        # Clean repo with a fresh fake HOME has no fragments and no claude
        # projects dir; that's "warn" + "info", not "fail" — exit 0.
        assert result.exit_code == 0
        assert "no *.db files staged" in result.output
        assert "config.json absent from worktree" in result.output

    def test_does_not_rewrite_legacy_config_json(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Reviewer note from PR #838: legacy installs (auto_discover=True
        # + existing config.json) trigger _migrate_auto_discover_once via
        # load_config_overrides, which rewrites config.json on disk. The
        # doctor must be read-only (RFC §Non-goals).
        import json

        _set_home(monkeypatch, tmp_path)
        memtomem_dir = tmp_path / ".memtomem"
        memtomem_dir.mkdir()
        config_json = memtomem_dir / "config.json"
        config_json.write_text(
            json.dumps({"indexing": {"auto_discover": True, "memory_dirs": ["~/notes"]}})
        )
        before = config_json.read_bytes()

        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        runner = CliRunner()
        runner.invoke(cli, ["sync-doctor"])

        assert config_json.read_bytes() == before, "sync-doctor must not rewrite config.json"

    def test_subdir_invocation_finds_root_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # End-to-end variant of the subdir-invocation fix: running
        # ``mm sync-doctor`` from a nested subdir must still flag a *.db
        # tracked at the repo root.
        _set_home(monkeypatch, tmp_path)
        _git_init(tmp_path)
        (tmp_path / "leaked.db").write_bytes(b"")
        _git_add(tmp_path, "leaked.db")
        nested = tmp_path / "memories" / "shared"
        nested.mkdir(parents=True)
        monkeypatch.chdir(nested)

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor"])
        assert result.exit_code == 1
        assert "leaked.db" in result.output

    def test_subdir_invocation_anchors_slug_at_repo_root(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The slug check should use the repo top-level, not the subdir cwd —
        # otherwise a subdir invocation would see ``slug differs`` even when
        # Claude Code has the repo root indexed correctly.
        _set_home(monkeypatch, tmp_path)
        _git_init(tmp_path)
        nested = tmp_path / "memories" / "shared"
        nested.mkdir(parents=True)

        # Create a Claude project entry for the repo root (not the subdir).
        projects = tmp_path / ".claude" / "projects"
        projects.mkdir(parents=True)
        (projects / _claude_slug_for(tmp_path)).mkdir()

        monkeypatch.chdir(nested)
        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor"])
        assert "slug matches synced layout" in result.output
        assert "slug differs" not in result.output
