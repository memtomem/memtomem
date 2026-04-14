"""memtomem web — launch the Web UI server."""

from __future__ import annotations

import click


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
@click.option("--headless", is_flag=True, help="Run without opening the browser")
def web(host: str, port: int, headless: bool) -> None:
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

    import uvicorn

    from memtomem.web.app import _lifespan, create_app

    import asyncio

    click.echo(f"Starting memtomem Web UI at http://{host}:{port}")

    async def after_started(server: uvicorn.Server, headless: bool) -> None:
        if headless:
            return
        while not server.started:
            await asyncio.sleep(0.1)
        import webbrowser

        webbrowser.open(f"http://{host}:{port}")

    async def start_server() -> None:
        web_config = uvicorn.Config(create_app(lifespan=_lifespan), host=host, port=port)
        web_server = uvicorn.Server(web_config)

        await asyncio.gather(
            web_server.serve(),
            after_started(web_server, headless),
        )

    asyncio.run(start_server())
