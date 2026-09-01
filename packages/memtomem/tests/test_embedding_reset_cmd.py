"""Tests for ``mm embedding-reset --yes`` (issue #2065).

``--mode apply-current`` is destructive and was the last confirmation
prompt with no non-interactive escape, so cron/CI had to reach for
``yes | mm …`` — which dies with SIGPIPE under ``set -o pipefail``.  The
contract here is the flag's *shape*, not the wipe itself
(``reset_embedding_meta`` is covered in the storage tests):

* ``--mode apply-current --yes`` never reads stdin — proven by running it
  with no input at all, which makes ``click.confirm`` abort (exit 1) if
  the prompt is still reached;
* without ``--yes`` the prompt survives, and answering ``n`` cancels;
* ``--yes`` outside ``apply-current`` is a ``UsageError``, mirroring
  ``mm gc``'s "``--yes`` requires ``--apply``".
"""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from memtomem.cli import cli

from .helpers import set_home


@pytest.fixture
def home(tmp_path, monkeypatch):
    """Tmp HOME + stripped ``MEMTOMEM_*`` env, with the module-bound
    ``_bootstrap._CONFIG_PATH`` repointed (a bare ``HOME`` override leaves
    the real ``~/.memtomem/config.json`` in play — #2103)."""
    from memtomem.cli import _bootstrap

    for var in [k for k in os.environ if k.startswith("MEMTOMEM_")]:
        monkeypatch.delenv(var, raising=False)

    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.chdir(h)
    set_home(monkeypatch, h)
    monkeypatch.setattr(_bootstrap, "_CONFIG_PATH", h / ".memtomem" / "config.json")
    return h


@pytest.fixture
def runner() -> CliRunner:
    return CliRunner()


def _install(home, runner: CliRunner) -> None:
    mem_dir = home / "memories"
    mem_dir.mkdir(exist_ok=True)
    (mem_dir / "note.md").write_text("# memo\n\nhello embedding reset\n", encoding="utf-8")
    result = runner.invoke(
        cli,
        ["init", "-y", "--provider", "none", "--memory-dir", str(mem_dir), "--mcp", "skip"],
    )
    assert result.exit_code == 0, f"init failed: {result.output}"


def test_apply_current_with_yes_does_not_prompt(home, runner: CliRunner) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", "apply-current", "--yes"], input="")

    # Empty stdin is the discriminator: a surviving ``click.confirm`` gets
    # EOF and aborts with exit 1, so exit 0 can only mean it was skipped.
    assert result.exit_code == 0, result.output
    assert "DB reset to" in result.output
    assert "Continue?" not in result.output


def test_short_flag_is_accepted(home, runner: CliRunner) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", "apply-current", "-y"], input="")

    assert result.exit_code == 0, result.output
    assert "DB reset to" in result.output


def test_apply_current_without_yes_still_prompts(home, runner: CliRunner) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", "apply-current"], input="n\n")

    assert result.exit_code == 0, result.output
    assert "Continue?" in result.output
    assert "Cancelled." in result.output
    assert "DB reset to" not in result.output


@pytest.mark.parametrize("mode", ["status", "revert-to-stored"])
def test_yes_outside_apply_current_is_a_usage_error(home, runner: CliRunner, mode: str) -> None:
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--mode", mode, "--yes"], input="")

    assert result.exit_code == 2, result.output
    assert "--yes requires --mode apply-current" in result.stderr


def test_bare_yes_does_not_silently_run_status(home, runner: CliRunner) -> None:
    """``--mode`` defaults to ``status``, so a bare ``--yes`` must refuse
    rather than print status — the exact "reset without asking" misread the
    flag exists to remove."""
    _install(home, runner)

    result = runner.invoke(cli, ["embedding-reset", "--yes"], input="")

    assert result.exit_code == 2, result.output
    assert "Embedding Status" not in result.output


def test_help_documents_the_non_interactive_form(runner: CliRunner) -> None:
    result = runner.invoke(cli, ["embedding-reset", "--help"])

    assert result.exit_code == 0
    assert "--yes" in result.output
    assert "-y" in result.output
