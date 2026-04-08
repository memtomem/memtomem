"""memtomem web — launch the Web UI server."""

from __future__ import annotations

import click


@click.command("web")
@click.option("--host", default="127.0.0.1", help="Host to bind to")
@click.option("--port", default=8080, type=int, help="Port to bind to")
def web(host: str, port: int) -> None:
    """Launch the memtomem Web UI (FastAPI + SPA)."""
    try:
        import uvicorn
    except ImportError:
        click.secho("Error: Web UI requires extra dependencies.", fg="red")
        click.echo("Install with: uv pip install 'memtomem[web]'")
        raise SystemExit(1)

    from memtomem.web.app import create_app, _lifespan

    click.echo(f"Starting memtomem Web UI at http://{host}:{port}")
    uvicorn.run(create_app(lifespan=_lifespan), host=host, port=port)
