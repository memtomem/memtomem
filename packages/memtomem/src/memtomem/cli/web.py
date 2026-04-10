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
def web(host: str, port: int) -> None:
    """Launch the memtomem Web UI (FastAPI + SPA)."""
    missing = _missing_web_deps()
    if missing is not None:
        click.secho(
            f"Error: Web UI requires extra dependencies (missing: {missing}).",
            fg="red",
        )
        click.echo(f"Install with: {_web_install_hint()}")
        click.echo('Or, if using pip: pip install "memtomem[web]"')
        raise SystemExit(1)

    import uvicorn

    from memtomem.web.app import _lifespan, create_app

    click.echo(f"Starting memtomem Web UI at http://{host}:{port}")
    uvicorn.run(create_app(lifespan=_lifespan), host=host, port=port)
