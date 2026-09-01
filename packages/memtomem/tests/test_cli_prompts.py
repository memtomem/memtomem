"""Tests for ``cli/_prompts.confirm`` (#1640).

Click 8.1-8.4's ``_readline_prompt`` redirects the prompt function's stdout to
stderr on POSIX when ``err=True`` but not on Windows, where the prompt tail
(and, under ``CliRunner``, the echoed reply) leaks into stdout. Click 8.5
dropped that Windows fork, but ``click>=8.1`` still admits the versions that
have it. ``confirm(err=True)`` bypasses click's prompt machinery entirely, so
``--json`` stdout stays a single JSON document on every platform and every
supported click. That bypass is what ``helpers.poison_click_prompts`` pins.
The end-to-end pins for the three production call sites live next to their
suites (``test_reset_cmd.py`` / ``test_cli_add_json.py`` /
``test_upgrade_cmd.py``).
"""

from __future__ import annotations

import click
import click.termui
import pytest
from click.testing import CliRunner

from memtomem.cli._prompts import confirm

from helpers import CLICK_PROMPT_SENTINEL, poison_click_prompts


@click.command()
@click.option("--default", "default_", is_flag=True)
def _cmd(default_: bool) -> None:
    click.echo(f"answer={confirm('Continue?', default=default_, err=True)}")


class TestConfirmErrTrue:
    @pytest.mark.parametrize(
        ("reply", "expected"),
        [
            ("n\n", "False"),
            ("no\n", "False"),
            ("y\n", "True"),
            ("YES\n", "True"),
            ("\n", "False"),
        ],
    )
    def test_reply_parsing_and_stdout_purity(self, reply: str, expected: str) -> None:
        result = CliRunner().invoke(_cmd, [], input=reply)

        assert result.exit_code == 0, result.output
        # Exact-equality pin: nothing but the command's own stdout output —
        # no prompt tail, no reply echo.
        assert result.stdout == f"answer={expected}\n"
        assert "Continue? [y/N]: " in result.stderr

    def test_empty_reply_returns_default_true(self) -> None:
        result = CliRunner().invoke(_cmd, ["--default"], input="\n")

        assert result.exit_code == 0, result.output
        assert result.stdout == "answer=True\n"
        assert "[Y/n]" in result.stderr

    def test_invalid_reply_reprompts_on_stderr(self) -> None:
        result = CliRunner().invoke(_cmd, [], input="maybe\ny\n")

        assert result.exit_code == 0, result.output
        assert result.stdout == "answer=True\n"
        assert "Error: invalid input" in result.stderr
        assert result.stderr.count("Continue?") == 2

    def test_eof_aborts(self) -> None:
        result = CliRunner().invoke(_cmd, [], input="")

        assert result.exit_code == 1
        assert "answer=" not in result.stdout

    def test_stdout_stays_clean_without_click_prompt_machinery(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The #1640 invariant: the helper never enters click's prompt
        # machinery, so the stdout leak that machinery can produce (click's
        # Windows prompt fork, pre-8.5) is unreachable by construction.
        calls = poison_click_prompts(monkeypatch)

        result = CliRunner().invoke(_cmd, [], input="n\n")

        assert result.exit_code == 0, result.output
        assert calls == []
        assert result.stdout == "answer=False\n"


class TestConfirmErrFalse:
    def test_defers_to_click_confirm(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """err=False keeps click's interactive UX (readline editing etc.)."""
        calls: dict[str, tuple] = {}

        def fake_confirm(text: str, default: bool = False) -> bool:
            calls["args"] = (text, default)
            return True

        monkeypatch.setattr(click, "confirm", fake_confirm)

        assert confirm("Go?", default=True, err=False) is True
        assert calls["args"] == ("Go?", True)


class TestPoisonClickPromptsIsArmed:
    """The guard has to survive the environment it guards in.

    ``poison_click_prompts`` is only evidence if it still fires from inside a
    ``CliRunner.invoke``. An earlier draft patched
    ``click.termui.visible_prompt_func``, which ``CliRunner.isolation()``
    reassigns during ``invoke`` — the poison looked installed and never fired,
    so ``calls == []`` would have passed for a command that prompted freely.
    These cases fail if that ever becomes true of a patched name, and they
    cover every name the helper patches — an unexercised arm is a claim, not
    evidence.
    """

    @pytest.mark.parametrize("alias", ["confirm", "prompt"])
    def test_package_alias_is_poisoned_under_cli_runner(
        self, monkeypatch: pytest.MonkeyPatch, alias: str
    ) -> None:
        @click.command()
        def _prompting() -> None:
            getattr(click, alias)("Continue?")

        calls = poison_click_prompts(monkeypatch)

        result = CliRunner().invoke(_prompting, [], input="n\n")

        assert result.exit_code == 0, result.output
        assert calls == [f"click.{alias}"]
        assert CLICK_PROMPT_SENTINEL in result.stdout

    def test_termui_path_is_poisoned_under_cli_runner(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bypasses the ``click.confirm`` alias, the way click's own option
        # prompting does, and lands on the ``_readline_prompt`` chokepoint.
        @click.command()
        def _prompting() -> None:
            click.termui.confirm("Continue?")

        calls = poison_click_prompts(monkeypatch)

        result = CliRunner().invoke(_prompting, [], input="n\n")

        assert result.exit_code == 0, result.output
        assert calls == ["click.termui._readline_prompt"]
        assert CLICK_PROMPT_SENTINEL in result.stdout
