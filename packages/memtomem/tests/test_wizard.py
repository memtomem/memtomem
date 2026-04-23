"""Unit tests for ``memtomem.cli.wizard`` — ``run_steps``, ``silent_step``,
back/cancel navigation.

Integration tests for the full ``mm init`` wizard live in ``test_init_cmd.py``;
this file exercises ``run_steps`` directly with synthetic step functions so
the navigation mechanics are pinned independent of wiring.
"""

from __future__ import annotations

from typing import Callable

import click
from click.testing import CliRunner

from memtomem.cli.wizard import StepBack, run_steps, silent_step


def _make_step(
    name: str,
    log: list[str],
    raise_back_on_call: int | None = None,
) -> Callable[[dict], None]:
    """Build an interactive step. Counts its own invocations in ``log`` and
    optionally raises ``StepBack`` on the Nth invocation (1-indexed)."""
    calls = {"n": 0}

    def step(state: dict) -> None:
        calls["n"] += 1
        log.append(name)
        if raise_back_on_call is not None and calls["n"] == raise_back_on_call:
            raise StepBack()

    step.__name__ = name
    return step


def _make_silent(name: str, log: list[str]) -> Callable[[dict], None]:
    """Build a silent step — no prompt, only records its invocation."""

    @silent_step
    def step(state: dict) -> None:
        log.append(name)

    step.__name__ = name
    return step


class TestSilentStepMarker:
    """``silent_step`` marks the function in a way ``run_steps`` can detect."""

    def test_decorator_sets_marker_attribute(self) -> None:
        @silent_step
        def step(state: dict) -> None:
            pass

        assert getattr(step, "_silent_in_back_nav", False) is True

    def test_undecorated_step_is_not_silent(self) -> None:
        def step(state: dict) -> None:
            pass

        assert getattr(step, "_silent_in_back_nav", False) is False


class TestRunStepsBackNav:
    """Pin the back-navigation skip behavior for silent steps. (#421)"""

    def test_back_through_silent_lands_on_prev_interactive(self) -> None:
        """``b`` at step 2 (interactive) should skip over silent step 1 and
        land on interactive step 0 — not re-run the silent banner."""
        log: list[str] = []
        step0 = _make_step("s0", log)
        step1 = _make_silent("silent", log)
        # step2 raises back on first call, then completes on second.
        step2 = _make_step("s2", log, raise_back_on_call=1)

        run_steps([step0, step1, step2])

        assert log == [
            "s0",  # forward
            "silent",  # forward (banner prints once)
            "s2",  # forward — raises back
            "s0",  # back skipped silent, landed on interactive
            "silent",  # forward again
            "s2",  # forward completes
        ]

    def test_back_through_multiple_silent_steps(self) -> None:
        """Two consecutive silent steps: back-nav skips both."""
        log: list[str] = []
        step0 = _make_step("s0", log)
        silent_a = _make_silent("silent_a", log)
        silent_b = _make_silent("silent_b", log)
        step3 = _make_step("s3", log, raise_back_on_call=1)

        run_steps([step0, silent_a, silent_b, step3])

        assert log == [
            "s0",
            "silent_a",
            "silent_b",
            "s3",
            "s0",  # back skipped both silents
            "silent_a",
            "silent_b",
            "s3",
        ]

    def test_back_from_interactive_adjacent_to_interactive_unchanged(self) -> None:
        """Sanity: when the previous step is already interactive, behavior is
        the same as before (no silent skipping needed)."""
        log: list[str] = []
        step0 = _make_step("s0", log)
        step1 = _make_step("s1", log, raise_back_on_call=1)

        run_steps([step0, step1])

        assert log == ["s0", "s1", "s0", "s1"]

    def test_back_at_first_step_echoes_message(self) -> None:
        """``b`` at index 0 with no prior steps keeps the existing
        ``(already at first step)`` message."""
        log: list[str] = []
        step0 = _make_step("s0", log, raise_back_on_call=1)

        runner = CliRunner()

        # Wrap in a click command so CliRunner can capture stdout.
        @click.command()
        def cmd() -> None:
            run_steps([step0])

        result = runner.invoke(cmd, [])
        assert result.exit_code == 0
        assert "(already at first step)" in result.output
        # Step re-ran after the message.
        assert log == ["s0", "s0"]

    def test_silent_at_position_zero_prevents_back_to_nowhere(self) -> None:
        """If only a silent step precedes the current one, treat it as
        ``(already at first step)`` — there is no interactive step to return
        to, so the silent banner must NOT re-fire."""
        log: list[str] = []
        silent0 = _make_silent("silent", log)
        step1 = _make_step("s1", log, raise_back_on_call=1)

        runner = CliRunner()

        @click.command()
        def cmd() -> None:
            run_steps([silent0, step1])

        result = runner.invoke(cmd, [])
        assert result.exit_code == 0
        assert "(already at first step)" in result.output
        # Silent step fires once (forward), s1 fires twice (back + forward
        # complete). Silent banner does NOT re-fire.
        assert log == ["silent", "s1", "s1"]


class TestRunStepsRegressions:
    """Pin existing behavior that must not regress under the silent-skip fix."""

    def test_forward_only_flow_unchanged(self) -> None:
        """No back-nav: each step (silent or interactive) runs exactly once."""
        log: list[str] = []
        steps = [
            _make_step("a", log),
            _make_silent("b", log),
            _make_step("c", log),
        ]

        run_steps(steps)

        assert log == ["a", "b", "c"]

    def test_state_mutations_persist_across_back_nav(self) -> None:
        """State dict shared between steps survives back-nav — this is the
        whole point of ``run_steps``."""
        log: list[str] = []

        def step_a(state: dict) -> None:
            log.append("a")
            state["a_ran"] = True

        calls_b = {"n": 0}

        def step_b(state: dict) -> None:
            calls_b["n"] += 1
            log.append(f"b{calls_b['n']}")
            if calls_b["n"] == 1:
                # Confirm state from step_a is visible.
                assert state.get("a_ran") is True
                raise StepBack()

        final = run_steps([step_a, step_b])

        assert log == ["a", "b1", "a", "b2"]
        assert final.get("a_ran") is True
