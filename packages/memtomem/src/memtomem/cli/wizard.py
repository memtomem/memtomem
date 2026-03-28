"""Shared wizard utilities — step navigation with back/cancel support."""

from __future__ import annotations

from typing import Any, Callable

import click


class StepBack(Exception):
    """Raised to go back to the previous step."""


class WizardCancel(Exception):
    """Raised to cancel the wizard."""


class _NavType(click.ParamType):
    """Wraps a Click type, intercepting 'b' (back) and 'q' (quit)."""

    name = "input"

    def __init__(self, inner: click.ParamType | None = None):
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


def run_steps(
    steps: list[Callable[[dict], None]],
    state: dict | None = None,
) -> dict:
    """Run a list of step functions with back/cancel support.

    Each step function receives a shared state dict and modifies it.
    Raising StepBack goes to the previous step, WizardCancel aborts.
    """
    if state is None:
        state = {}
    i = 0
    while i < len(steps):
        try:
            steps[i](state)
            i += 1
        except StepBack:
            if i > 0:
                i -= 1
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


def step_header(number: int, title: str) -> None:
    """Print a step header with navigation hint."""
    click.secho(f"{number}. {title}", fg="yellow", bold=True)
    click.echo(click.style("  (b: back, q: quit)", dim=True))
