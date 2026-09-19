"""A bare ``mm index`` says which directory it is about to walk.

``PATH`` defaults to ``.``. Someone following a setup guide inside a code
repository gets the repository indexed, the redaction gate blocking its
fixtures, and exit 1 — a refusal that reads as a broken install rather than a
mis-aimed command. The notice is stderr-only so no stdout contract moves, and
it must not fire on the queue modes, which never index ``.`` at all.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import patch

import click
import pytest
from click.testing import CliRunner

from memtomem.cli import cli, indexing

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


class TestTheDeclaredClickFloorStillRuns:
    """``pyproject.toml`` declares ``click>=8.1``; the lockfile installs 8.5.

    Reaching for ``ParameterSource`` through the ``click`` package raises
    ``AttributeError`` on most of that declared range: measured absent on
    8.1.8, 8.2.1, 8.3.0, 8.3.1 and 8.3.2, and present from 8.3.3. An earlier
    version of this docstring said 8.4, which came of sampling 8.3.0 and 8.4.0
    and interpolating between them.

    Which release introduced it is not the point, though — that a supported
    one lacks it is. The access sits in the comparison, ahead of the DEFAULT
    branch, so the failure is not confined to the bare form: it takes down
    every direct ``mm index``. Nothing in the suite can see it, because the
    installed Click has the alias.
    """

    def test_the_notice_does_not_reach_through_the_click_package(
        self, indexed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Behavioural half: with the 8.4 alias gone, the command still runs."""
        monkeypatch.delattr(click, "ParameterSource", raising=False)
        runner = CliRunner()

        with runner.isolated_filesystem(temp_dir=tmp_path):
            result = runner.invoke(cli, ["index"])

        assert result.exit_code == 0, result.exception
        assert _NOTICE in result.stderr

    def test_an_explicit_path_survives_the_floor_too(
        self, indexed, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The lookup precedes the branch, so the explicit form is not a refuge."""
        monkeypatch.delattr(click, "ParameterSource", raising=False)
        target = tmp_path / "notes"
        target.mkdir()

        result = CliRunner().invoke(cli, ["index", str(target)])

        assert result.exit_code == 0, result.exception
        assert _NOTICE not in result.stderr

    def test_the_module_still_imports_without_the_top_level_alias(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Import half: execute the module's source with the alias gone.

        ``monkeypatch.delattr`` cannot reach a *module-level* alias such as
        ``ParameterSource = click.ParameterSource``, which is evaluated once at
        import time and would reintroduce the identical AttributeError. An
        earlier version of this test scanned the AST for that one spelling;
        review pointed out that ``getattr(click, "ParameterSource")``, an
        aliased package import, or a from-import out of ``click`` all walk
        straight past such a matcher. Executing the module tests the property
        instead of enumerating its spellings.

        Under a throwaway name, and never ``importlib.reload``. Reloading the
        real module rebinds the classes defined in it, so every module that
        had already imported ``_IndexingStatsError`` by name keeps the old
        object and stops recognising the new one — measured: four unrelated
        tests in ``test_indexing_cli.py`` failed their ``pytest.raises``, and
        only in a full-suite run, because running this file alone never
        reaches them.
        """
        monkeypatch.delattr(click, "ParameterSource", raising=False)

        spec = importlib.util.spec_from_file_location("_click_floor_probe", indexing.__file__)
        assert spec and spec.loader
        probe = importlib.util.module_from_spec(spec)

        # Not registered in ``sys.modules``: nothing else can pick it up, and
        # the canonical module keeps its identity.
        spec.loader.exec_module(probe)

        assert probe.ParameterSource is not None
