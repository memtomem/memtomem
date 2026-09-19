"""A bare ``mm index`` says which directory it is about to walk.

``PATH`` defaults to ``.``. Someone following a setup guide inside a code
repository gets the repository indexed, the redaction gate blocking its
fixtures, and exit 1 — a refusal that reads as a broken install rather than a
mis-aimed command. The notice is stderr-only so no stdout contract moves, and
it must not fire on the queue modes, which never index ``.`` at all.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

_NOTICE = "no PATH given"


@pytest.fixture
def indexed() -> list[str]:
    """Swallow the real index run; record the path it was asked for."""
    seen: list[str] = []

    async def fake_index(path: str, *args: object, **kwargs: object) -> None:
        seen.append(path)

    with patch("memtomem.cli.indexing._index", fake_index):
        yield seen


class TestImplicitPathNotice:
    def test_bare_index_names_the_directory_it_will_walk(self, indexed, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path) as cwd:
            result = runner.invoke(cli, ["index"])

            assert result.exit_code == 0
            assert _NOTICE in result.stderr
            # The resolved directory, not the bare "." the user did not type.
            assert str(Path(cwd).resolve()) in result.stderr

    def test_the_notice_points_at_the_explicit_form(self, indexed, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["index"])

            assert "mm index <path>" in result.stderr
            assert "mm status" in result.stderr

    def test_an_explicit_path_is_not_second_guessed(self, indexed, tmp_path: Path) -> None:
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["index", "."])

            assert result.exit_code == 0
            assert _NOTICE not in result.stderr
            assert indexed == ["."]

    def test_an_explicit_other_path_is_not_second_guessed(self, indexed, tmp_path: Path) -> None:
        target = tmp_path / "notes"
        target.mkdir()

        result = CliRunner().invoke(cli, ["index", str(target)])

        assert result.exit_code == 0
        assert _NOTICE not in result.stderr

    def test_stdout_carries_none_of_it(self, indexed, tmp_path: Path) -> None:
        """Machine consumers read stdout; the notice is advisory prose."""
        runner = CliRunner()
        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["index"])

            assert result.exit_code == 0
            assert indexed == ["."], "the index call itself must have happened"
            assert _NOTICE not in result.stdout


class TestQueueModesAreUntouched:
    """``--status`` / ``--flush`` return before indexing; the hook uses them."""

    def test_flush_does_not_warn(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with (
            patch("memtomem.cli.indexing._run_flush") as flush,
            runner.isolated_filesystem(temp_dir=tmp_path),
        ):
            result = runner.invoke(cli, ["index", "--flush"])

            assert result.exit_code == 0
            assert flush.called
            assert _NOTICE not in result.stderr

    def test_status_does_not_warn(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with (
            patch("memtomem.cli.indexing._print_status") as status,
            runner.isolated_filesystem(temp_dir=tmp_path),
        ):
            result = runner.invoke(cli, ["index", "--status"])

            assert result.exit_code == 0
            assert status.called
            assert _NOTICE not in result.stderr

    def test_pathless_debounce_does_not_warn(self, tmp_path: Path) -> None:
        """Pins placement: an explicit path would suppress the notice anyway, so
        only the pathless case can catch the call drifting above the queue
        dispatch — where it would fire for a mode that never indexes ``.``."""
        runner = CliRunner()
        with (
            patch("memtomem.cli.indexing._run_debounce") as debounce,
            runner.isolated_filesystem(temp_dir=tmp_path),
        ):
            result = runner.invoke(cli, ["index", "--debounce-window", "5"])

            assert result.exit_code == 0
            assert debounce.called
            assert _NOTICE not in result.stderr

    def test_a_default_map_value_is_not_treated_as_omitted(self, tmp_path: Path) -> None:
        """``DEFAULT_MAP`` is a distinct ParameterSource: a configured path was
        chosen by the user, so it must not draw the notice either."""
        target = tmp_path / "configured"
        target.mkdir()
        seen: list[str] = []

        async def fake_index(path: str, *args: object, **kwargs: object) -> None:
            seen.append(path)

        with patch("memtomem.cli.indexing._index", fake_index):
            result = CliRunner().invoke(
                cli, ["index"], default_map={"index": {"path": str(target)}}
            )

        assert result.exit_code == 0
        assert seen == [str(target)]
        assert _NOTICE not in result.stderr

    def test_debounce_with_an_explicit_path_does_not_warn(self, tmp_path: Path) -> None:
        runner = CliRunner()
        with (
            patch("memtomem.cli.indexing._run_debounce") as debounce,
            runner.isolated_filesystem(temp_dir=tmp_path),
        ):
            result = runner.invoke(cli, ["index", "--debounce-window", "5", str(tmp_path / "a.md")])

            assert result.exit_code == 0
            assert debounce.called
            assert _NOTICE not in result.stderr
