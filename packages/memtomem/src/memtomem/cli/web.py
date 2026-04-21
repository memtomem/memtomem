"""memtomem web — launch the Web UI server."""

from __future__ import annotations

import click


_WEB_MODE_CHOICES = ("prod", "dev")


def _missing_web_deps() -> str | None:
    """Return the name of the first missing web-UI dependency, or None if all
    required packages are importable. Kept private so the wizard can reuse it."""
    for mod in ("fastapi", "uvicorn"):
        try:
            __import__(mod)
        except ImportError:
            return mod
    return None


def _web_install_hint() -> str:
    """Return the recommended install command for the `[web]` extra. Used by
    both `mm web` errors and the `mm init` wizard's Next Steps section."""
    return 'uv tool install --reinstall "memtomem[web]"'


@click.command("web")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to bind to")
@click.option("--open", "open_browser", is_flag=True, help="Run with opening the browser")
@click.option(
    "--timeout", default=30, type=int, help="Timeout for web opening (seconds). Zero is no timeout."
)
@click.option(
    "--mode",
    type=click.Choice(_WEB_MODE_CHOICES, case_sensitive=False),
    default=None,
    help="UI surface to expose. 'prod' (default) shows the polished page set; "
    "'dev' adds opt-in maintainer pages. Overrides MEMTOMEM_WEB__MODE.",
)
@click.option(
    "--dev",
    "dev_flag",
    is_flag=True,
    help="Shortcut for --mode dev. Mutually exclusive with --mode.",
)
def web(
    host: str,
    port: int,
    open_browser: bool,
    timeout: int,
    mode: str | None,
    dev_flag: bool,
) -> None:
    """Launch the memtomem Web UI (FastAPI + SPA)."""
    missing = _missing_web_deps()
    if missing is not None:
        click.secho(
            f"Error: Web UI requires the [web] extra (missing: {missing}).",
            fg="red",
        )
        click.echo(
            "The base install does not include web dependencies."
            " To add them, reinstall with the [web] extra:"
        )
        click.echo(f"  {_web_install_hint()}")
        click.echo('  Or, if using pip: pip install "memtomem[web]"')
        raise SystemExit(1)

    if mode is not None and dev_flag:
        raise click.UsageError("--mode and --dev are mutually exclusive")

    from memtomem.web.app import resolve_web_mode_from_env

    if dev_flag:
        resolved_mode: str = "dev"
    elif mode is not None:
        resolved_mode = mode.lower()
    else:
        try:
            resolved_mode = resolve_web_mode_from_env(strict=True)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="MEMTOMEM_WEB__MODE") from exc

    import uvicorn

    from memtomem.web.app import _lifespan, create_app

    import asyncio

    click.echo(f"Starting memtomem Web UI at http://{host}:{port} (mode={resolved_mode})")

    async def after_started(server: uvicorn.Server, timeout: float) -> None:
        if not open_browser:
            return
        import time

        if timeout == 0:
            click.secho(
                "Warning: No timeout for Web opening (timeout is set to 0).",
                fg="yellow",
            )
            deadline = float("inf")
        else:
            deadline = time.monotonic() + timeout
        while not server.started:
            if time.monotonic() >= deadline:
                click.secho(
                    "Warning: Web server did not start within the timeout period; not opening browser.",
                    fg="yellow",
                )
                return
            await asyncio.sleep(0.1)
        import webbrowser

        webbrowser.open(f"http://{host}:{port}")

    async def start_server() -> None:
        web_config = uvicorn.Config(
            create_app(lifespan=_lifespan, mode=resolved_mode),
            host=host,
            port=port,
        )
        web_server = uvicorn.Server(web_config)

        await asyncio.gather(
            web_server.serve(),
            after_started(web_server, timeout=float(timeout)),
        )

    asyncio.run(start_server())
