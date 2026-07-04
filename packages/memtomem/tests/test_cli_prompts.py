"""Tests for ``cli/_prompts.confirm`` (#1640).

click 8.4's ``_readline_prompt`` redirects the prompt function's stdout to
stderr on POSIX when ``err=True`` but not on Windows, where the prompt tail
(and, under ``CliRunner``, the echoed reply) leaks into stdout.
``confirm(err=True)`` bypasses click's prompt machinery so ``--json`` stdout
stays a single JSON document on every platform. The end-to-end pins for the
two production call sites live next to their suites
(``test_reset_cmd.py`` / ``test_cli_add_json.py``).
"""

from __future__ import annotations

import click
import pytest
from click.testing import CliRunner

from memtomem.cli._prompts import confirm


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

    def test_stdout_stays_clean_under_simulated_windows(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The #1640 trigger — forcing click's WIN prompt branch must not
        # matter because the helper never enters click's prompt machinery.
        import click.termui

        monkeypatch.setattr(click.termui, "WIN", True)

        result = CliRunner().invoke(_cmd, [], input="n\n")

        assert result.exit_code == 0, result.output
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
