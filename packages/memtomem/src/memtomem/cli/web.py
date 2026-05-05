"""memtomem web — launch the Web UI server."""

from __future__ import annotations

import click


_WEB_MODE_CHOICES = ("prod", "dev")


def _missing_web_deps() -> str | None:
    """Return the name of the first missing web-UI dependency, or None if all
    required packages are importable. Kept private so the wizard can reuse it.

    Uses ``importlib.util.find_spec`` so the probe is cheap (no module init
    side-effects) and matches the semantic the wizard's
    ``_collect_missing_extras`` uses — both sites now answer the
    "is the package installed" question the same way (#363 Phase 3,
    eliminates the historical ``__import__`` vs ``find_spec`` split)."""
    from importlib.util import find_spec

    for mod in ("fastapi", "uvicorn"):
        try:
            present = find_spec(mod) is not None
        except (ImportError, ValueError):
            present = False
        if not present:
            return mod
    return None


def _web_install_hint() -> str:
    """Return the recommended install command for the `[web]` extra. Used by
    both `mm web` errors and the `mm init` wizard's Next Steps section."""
    return 'uv tool install --reinstall "memtomem[web]"'


_LOOPBACK_BINDS = {"127.0.0.1", "::1", "localhost"}


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
@click.option(
    "--allow-remote-ui",
    is_flag=True,
    help="Acknowledge that --host is exposing the Web UI off-loopback. RFC #787 "
    "stage 1 (this release) only logs the observation; stage 2 will refuse to "
    "start without this flag when --host is non-loopback. Pair with "
    "--trusted-origin / --trusted-host so the eventual enforcement layer has "
    "an explicit allow-list to read.",
)
@click.option(
    "--trusted-origin",
    "trusted_origins",
    multiple=True,
    metavar="HOST",
    help="Add a hostname to the CSRF Origin/Referer allow-list. Loopback "
    "(127.0.0.1, ::1, localhost) is always trusted; anything else has to be "
    "named explicitly. Repeat the flag for multiple hosts.",
)
@click.option(
    "--trusted-host",
    "trusted_hosts",
    multiple=True,
    metavar="HOST",
    help="Add a hostname to the CSRF Host-header allow-list. Defends DNS "
    "rebinding when running with --allow-remote-ui. Loopback is always "
    "trusted. Repeat for multiple hosts.",
)
def web(
    host: str,
    port: int,
    open_browser: bool,
    timeout: int,
    mode: str | None,
    dev_flag: bool,
    allow_remote_ui: bool,
    trusted_origins: tuple[str, ...],
    trusted_hosts: tuple[str, ...],
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

    from memtomem.web.app import WebMode, resolve_web_mode_from_env

    resolved_mode: WebMode
    if dev_flag:
        resolved_mode = "dev"
    elif mode is not None:
        # click.Choice has already constrained this to the literal set.
        resolved_mode = mode.lower()  # type: ignore[assignment]
    else:
        try:
            resolved_mode = resolve_web_mode_from_env(strict=True)
        except ValueError as exc:
            raise click.BadParameter(str(exc), param_hint="MEMTOMEM_WEB__MODE") from exc

    import uvicorn

    from memtomem.web.app import _lifespan, create_app

    import asyncio

    click.echo(f"Starting memtomem Web UI at http://{host}:{port} (mode={resolved_mode})")

    bind_is_loopback = host in _LOOPBACK_BINDS
    if not bind_is_loopback and not allow_remote_ui:
        # PR1 (log-only) doesn't refuse to start — that's PR2's flip per
        # RFC #787 stage 2. But emit a loud warning so an operator who
        # ran `--host 0.0.0.0` for a screen-share knows the upcoming
        # release will gate it on `--allow-remote-ui`.
        click.secho(
            f"Warning: --host {host} exposes the Web UI off-loopback. RFC #787 "
            "stage 2 will require --allow-remote-ui (with --trusted-origin / "
            "--trusted-host) for non-loopback binds. See "
            "https://github.com/memtomem/memtomem/issues/787 .",
            fg="yellow",
        )

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
        app_instance = create_app(lifespan=_lifespan, mode=resolved_mode)
        # Push the operator-supplied allow-lists into the app state so
        # ``CSRFGuardMiddleware`` (RFC #787) can read them. Done here
        # rather than via env vars so the CLI surface stays the
        # source-of-truth and the app factory keeps being usable
        # standalone (tests, asgi mounts) with the safe defaults.
        if trusted_origins:
            app_instance.state.csrf_trusted_origins = frozenset(trusted_origins)
        if trusted_hosts:
            app_instance.state.csrf_trusted_hosts = frozenset(trusted_hosts)
        web_config = uvicorn.Config(
            app_instance,
            host=host,
            port=port,
        )
        web_server = uvicorn.Server(web_config)

        await asyncio.gather(
            web_server.serve(),
            after_started(web_server, timeout=float(timeout)),
        )

    asyncio.run(start_server())
