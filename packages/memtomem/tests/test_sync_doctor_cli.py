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


# ---- check_memory_dirs_under_home ------------------------------------------


class TestCheckMemoryDirsUnderHome:
    def test_under_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        d = tmp_path / "memories"
        d.mkdir()
        r = check_memory_dirs_under_home([d])
        assert r.status == "pass"

    def test_outside_home(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        outside_home = tmp_path / "home"
        outside_home.mkdir()
        monkeypatch.setenv("HOME", str(outside_home))
        # An entry under tmp_path but not under tmp_path/home.
        outside = tmp_path / "elsewhere"
        outside.mkdir()
        r = check_memory_dirs_under_home([outside])
        assert r.status == "warn"


# ---- check_cloud_mount -----------------------------------------------------


class TestCheckCloudMount:
    def test_no_cloud(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        r = check_cloud_mount([tmp_path / "memories"])
        assert r.status == "pass"

    def test_dropbox_warns(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        r = check_cloud_mount([tmp_path / "Dropbox" / "memories"])
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
        slug = str(cwd.resolve()).replace("/", "-")
        (projects / slug).mkdir()
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
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)
        (tmp_path / "snapshot.db").write_bytes(b"")
        _git_add(tmp_path, "snapshot.db")

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor"])
        assert result.exit_code == 1
        assert "*.db" in result.output

    def test_pass_path_clean_repo(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.chdir(tmp_path)
        _git_init(tmp_path)

        runner = CliRunner()
        result = runner.invoke(cli, ["sync-doctor"])
        # Clean repo with a fresh fake HOME has no fragments and no claude
        # projects dir; that's "warn" + "info", not "fail" — exit 0.
        assert result.exit_code == 0
        assert "no *.db files staged" in result.output
        assert "config.json absent from worktree" in result.output
