"""Tests for the ``mm wiki remote`` / ``push`` / ``pull`` CLI surface (#1416)."""

from __future__ import annotations

import subprocess
from pathlib import Path

from click.testing import CliRunner

from memtomem.cli.wiki_cmd import wiki
from memtomem.wiki.store import WikiStore


def _bare_origin(path: Path) -> Path:
    subprocess.run(
        ["git", "init", "--bare", "-b", "main", str(path)],
        check=True,
        capture_output=True,
    )
    return path


def _commit_file(root: Path, rel: str, content: str, msg: str) -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    subprocess.run(["git", "-C", str(root), "add", rel], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(root), "commit", "-m", msg], check=True, capture_output=True)


class TestRemoteCmd:
    def test_show_when_unset(self, wiki_root: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["remote"])
        assert result.exit_code == 0, result.output
        assert "No wiki remote configured" in result.output
        assert "mm wiki remote" in result.output

    def test_set_added(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        origin = _bare_origin(tmp_path / "origin.git")
        result = runner.invoke(wiki, ["remote", str(origin)])
        assert result.exit_code == 0, result.output
        assert "Set wiki remote 'origin'" in result.output
        assert "(added)" in result.output

    def test_show_after_set(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        origin = _bare_origin(tmp_path / "origin.git")
        runner.invoke(wiki, ["remote", str(origin)])
        result = runner.invoke(wiki, ["remote"])
        assert result.exit_code == 0, result.output
        assert str(origin) in result.output

    def test_set_redacts_credentials(self, wiki_root: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["remote", "https://user:secret@github.com/o/r.git"])
        assert result.exit_code == 0, result.output
        assert "secret" not in result.output
        assert "github.com/o/r.git" in result.output

    def test_show_redacts_credentials(self, wiki_root: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        runner.invoke(wiki, ["remote", "https://user:secret@github.com/o/r.git"])
        result = runner.invoke(wiki, ["remote"])
        assert result.exit_code == 0, result.output
        assert "secret" not in result.output
        assert "github.com/o/r.git" in result.output

    def test_requires_wiki(self, wiki_root: Path) -> None:
        runner = CliRunner()  # no init
        result = runner.invoke(wiki, ["remote"])
        assert result.exit_code != 0
        assert "wiki not found" in result.output


class TestPushCmd:
    def test_push_happy(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(wiki, ["init"]).exit_code == 0
        origin = _bare_origin(tmp_path / "origin.git")
        assert runner.invoke(wiki, ["remote", str(origin)]).exit_code == 0
        result = runner.invoke(wiki, ["push"])
        assert result.exit_code == 0, result.output
        assert "Pushed." in result.output
        # The push actually landed: origin's main matches the local wiki HEAD.
        remote_head = subprocess.run(
            ["git", "ls-remote", str(origin), "main"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.split()[0]
        local_head = WikiStore.at(wiki_root).current_commit()
        assert remote_head == local_head

    def test_push_detached_head_surfaces_error(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        origin = _bare_origin(tmp_path / "origin.git")
        runner.invoke(wiki, ["remote", str(origin)])
        head = WikiStore.at(wiki_root).current_commit()
        subprocess.run(
            ["git", "-C", str(wiki_root), "checkout", head], check=True, capture_output=True
        )
        result = runner.invoke(wiki, ["push"])
        assert result.exit_code != 0
        assert "detached HEAD" in result.output

    def test_push_no_remote_is_friendly(self, wiki_root: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["push"])
        assert result.exit_code != 0
        assert "mm wiki remote" in result.output

    def test_push_non_fast_forward_shows_git_message(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(wiki, ["init"]).exit_code == 0
        origin = _bare_origin(tmp_path / "origin.git")
        assert runner.invoke(wiki, ["remote", str(origin)]).exit_code == 0
        assert runner.invoke(wiki, ["push"]).exit_code == 0

        # A second clone advances origin; the local wiki then diverges.
        other = WikiStore.at(tmp_path / "other")
        other.init_from_url(str(origin))
        _commit_file(other.root, "README.md", "from other\n", "other edit")
        other.push()
        _commit_file(wiki_root, "README.md", "local\n", "local edit")

        result = runner.invoke(wiki, ["push"])
        assert result.exit_code != 0
        lowered = result.output.lower()
        assert "rejected" in lowered or "fast-forward" in lowered or "fast forward" in lowered

    def test_push_requires_wiki(self, wiki_root: Path) -> None:
        runner = CliRunner()  # no init
        result = runner.invoke(wiki, ["push"])
        assert result.exit_code != 0
        assert "wiki not found" in result.output


class TestPullCmd:
    def test_pull_happy(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        assert runner.invoke(wiki, ["init"]).exit_code == 0
        origin = _bare_origin(tmp_path / "origin.git")
        assert runner.invoke(wiki, ["remote", str(origin)]).exit_code == 0
        assert runner.invoke(wiki, ["push"]).exit_code == 0

        # A second clone pushes a new asset; the local wiki pulls it.
        other = WikiStore.at(tmp_path / "other")
        other.init_from_url(str(origin))
        _commit_file(other.root, "skills/x/SKILL.md", "# x\n", "add x")
        other.push()

        result = runner.invoke(wiki, ["pull"])
        assert result.exit_code == 0, result.output
        assert "Pulled." in result.output
        assert (wiki_root / "skills" / "x" / "SKILL.md").read_text() == "# x\n"

    def test_pull_no_remote_is_friendly(self, wiki_root: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        result = runner.invoke(wiki, ["pull"])
        assert result.exit_code != 0
        assert "mm wiki remote" in result.output

    def test_pull_detached_head_surfaces_error(self, wiki_root: Path, tmp_path: Path) -> None:
        runner = CliRunner()
        runner.invoke(wiki, ["init"])
        origin = _bare_origin(tmp_path / "origin.git")
        runner.invoke(wiki, ["remote", str(origin)])
        head = WikiStore.at(wiki_root).current_commit()
        subprocess.run(
            ["git", "-C", str(wiki_root), "checkout", head], check=True, capture_output=True
        )
        result = runner.invoke(wiki, ["pull"])
        assert result.exit_code != 0
        assert "detached HEAD" in result.output

    def test_pull_requires_wiki(self, wiki_root: Path) -> None:
        runner = CliRunner()  # no init
        result = runner.invoke(wiki, ["pull"])
        assert result.exit_code != 0
        assert "wiki not found" in result.output
