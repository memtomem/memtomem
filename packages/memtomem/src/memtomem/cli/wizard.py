"""Shared wizard utilities — step navigation with back/cancel support."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any, Callable

import click


class StepBack(Exception):
    """Raised to go back to the previous step."""


class StepRetry(Exception):
    """Raised to re-invoke the current step.

    Steps wrap a recoverable action in ``while True: try: ... except
    StepRetry: continue`` to retry only that action without re-prompting
    inputs collected earlier in the step. If a step does not catch
    ``StepRetry`` itself, ``run_steps`` re-invokes the entire step from
    the top.
    """


class WizardCancel(Exception):
    """Raised to cancel the wizard."""


class _NavType(click.ParamType):
    """Wraps a Click type, intercepting 'b' (back) and 'q' (quit)."""

    name = "input"

    def __init__(self, inner: click.ParamType | type | None = None):
        # Accept Python types (int, float, str) and convert to Click types
        if inner is not None and not isinstance(inner, click.ParamType):
            inner = click.types.convert_type(inner)
        self.inner = inner

    def convert(self, value: Any, param: Any, ctx: Any) -> Any:
        if isinstance(value, str):
            v = value.strip().lower()
            if v in ("b", "back"):
                raise StepBack()
            if v in ("q", "quit"):
                raise WizardCancel()
        if self.inner:
            return self.inner.convert(value, param, ctx)
        return value


def nav_prompt(text: str, **kwargs: Any) -> Any:
    """Like click.prompt but intercepts 'b' (back) and 'q' (quit)."""
    inner_type = kwargs.pop("type", None)
    kwargs["type"] = _NavType(inner_type)
    return click.prompt(text, **kwargs)


def nav_confirm(text: str, default: bool = False) -> bool:
    """Like click.confirm but intercepts 'b' (back) and 'q' (quit)."""
    suffix = " [Y/n]" if default else " [y/N]"
    while True:
        val = click.prompt(
            text + suffix,
            default="y" if default else "n",
            show_default=False,
            type=_NavType(),
        )
        v = val.strip().lower()
        if v in ("y", "yes"):
            return True
        if v in ("n", "no"):
            return False
        click.echo("  Please answer y or n.")


def fail_step(message: str, *, retryable: bool = True) -> None:
    """Surface a step failure and force the user to choose recovery.

    Replaces the silent ``click.echo("error..."); continue`` pattern that
    let the wizard advance after a failed action (#626). Always raises —
    never returns. Caller pattern for retryable actions::

        while True:
            try:
                do_action()
                break
            except ActionFailed as exc:
                fail_step(f"Action failed: {exc}", retryable=True)

    ``StepRetry`` from ``fail_step`` is caught by the local ``try`` so
    only the inner action retries; ``StepBack`` / ``WizardCancel`` bubble
    up to ``run_steps``.

    ``retryable=False`` is for failures the user can't fix in place
    (e.g. invalid input that needs re-entry on the previous step) —
    only ``[b]ack`` / ``[q]uit`` are offered.
    """
    click.secho(f"  ✗ {message}", fg="red")
    if retryable:
        text = "  Retry, back, or quit? [R/b/q]"
        default = "r"
    else:
        text = "  Back or quit? [B/q]"
        default = "b"
    while True:
        val = click.prompt(text, default=default, show_default=False)
        v = val.strip().lower()
        if retryable and v in ("r", "retry"):
            raise StepRetry()
        if v in ("b", "back"):
            raise StepBack()
        if v in ("q", "quit"):
            raise WizardCancel()
        click.echo("  Please answer r, b, or q." if retryable else "  Please answer b or q.")


def silent_step(fn: Callable[[dict], None]) -> Callable[[dict], None]:
    """Mark a step as silent (no user prompt — only side effects like banners).

    Silent steps are skipped during back-navigation so ``b`` lands on the
    previous *interactive* step instead of re-running the banner and falling
    straight back to where the user came from.
    """
    fn._silent_in_back_nav = True  # type: ignore[attr-defined]
    return fn


def _is_silent(step: Callable[[dict], None]) -> bool:
    return getattr(step, "_silent_in_back_nav", False)


def run_steps(
    steps: Sequence[Callable[[dict], None]],
    state: dict | None = None,
) -> dict:
    """Run a list of step functions with back/cancel support.

    Each step function receives a shared state dict and modifies it.
    Raising StepBack goes to the previous interactive step (skipping any
    ``@silent_step`` in between). StepRetry re-invokes the same step
    from the top (steps that want partial retry should catch StepRetry
    locally — see :func:`fail_step`). WizardCancel aborts.

    Before each invocation, ``state["_wizard_position"] = (i + 1, len(steps))``
    is seeded so :func:`step_header` (and any other display helper) can render
    a number reflecting the step's actual position in *this* flow — not a
    value hardcoded at the step definition. Without the seed the number was
    only correct for the 10-step ``--advanced`` flow; preset flows showed
    e.g. ``3. Memory Directory`` when memory-dir was actually the first step
    the user saw. (#420)
    """
    if state is None:
        state = {}
    i = 0
    while i < len(steps):
        state["_wizard_position"] = (i + 1, len(steps))
        try:
            steps[i](state)
            i += 1
        except StepRetry:
            click.secho("  (retrying...)", dim=True)
        except StepBack:
            target = i - 1
            while target >= 0 and _is_silent(steps[target]):
                target -= 1
            if target >= 0:
                i = target
                click.echo()
            else:
                click.echo("  (already at first step)")
        except WizardCancel:
            click.echo()
            click.secho("  Wizard cancelled.", fg="yellow")
            raise SystemExit(0)
        except click.Abort:
            click.echo()
            click.secho("  Wizard cancelled.", fg="yellow")
            raise SystemExit(0)
    return state


def step_header(state: dict, title: str) -> None:
    """Print a step header with navigation hint.

    The number is read from ``state["_wizard_position"]`` as seeded by
    :func:`run_steps`. If the key is absent (standalone / test use), the
    header is rendered without a number. (#420)
    """
    position = state.get("_wizard_position")
    if position is not None:
        current, _total = position
        click.secho(f"{current}. {title}", fg="yellow", bold=True)
    else:
        click.secho(title, fg="yellow", bold=True)
    click.echo(click.style("  (b: back, q: quit)", dim=True))
