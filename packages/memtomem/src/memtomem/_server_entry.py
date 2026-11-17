"""``memtomem-server``'s entry point: parse, then hand over (#3381).

The console script points here rather than at :func:`memtomem.server.main`
because importing :mod:`memtomem.server` registers every MCP tool, which is
most of a standalone server's startup time and memory. Arguments are parsed
before that import, so a process that turns out not to need the server — ``--help``,
or a ``--shared`` session that joins a daemon — never pays for it. ``mm serve`` and
``memtomem.server.main`` (and so ``python -m memtomem.server``) go through
:func:`main` as well, so every way of starting a server parses one way.
"""

from __future__ import annotations

import argparse
import sys

_TRANSPORT_OPTIONS = (
    "transport",
    "host",
    "port",
    "url",
    "allowed_host",
    "allowed_origin",
    "disable_dns_rebinding_protection",
)


def _daemon_name(value: str) -> str:
    from memtomem._daemon_paths import NAME_PATTERN

    if not NAME_PATTERN.fullmatch(value):
        raise argparse.ArgumentTypeError(
            f"{value!r} is not a daemon name: use 1 to 32 characters from a-z, 0-9 and '-', "
            "starting with a letter or digit"
        )
    return value


def _idle_timeout(value: str) -> float:
    try:
        seconds = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{value!r} is not a number of seconds") from None
    if not seconds >= 0:
        raise argparse.ArgumentTypeError("must be 0 or more")
    return seconds


def parse_server_args(argv: list[str] | None = None) -> argparse.Namespace:
    """Parse ``memtomem-server`` transport options."""
    parser = argparse.ArgumentParser(
        prog="memtomem-server",
        description="Run the memtomem MCP server.",
        epilog=(
            "Security: sse/http transports have no first-party authentication; "
            "treat them as trusted-network only and place an authenticated reverse "
            "proxy in front before exposing publicly."
        ),
    )
    parser.add_argument(
        "--transport",
        choices=("stdio", "sse", "streamable-http", "http"),
        default="stdio",
        help="MCP transport to use. 'http' is an alias for 'streamable-http'.",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help=(
            "Local host/interface to listen on for sse/http transports, usually "
            "127.0.0.1 behind nginx or 0.0.0.0 for trusted direct network access."
        ),
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8000,
        help="Port for sse/http transports.",
    )
    parser.add_argument(
        "--url",
        default=None,
        help=(
            "Full MCP endpoint URL clients connect to, e.g. https://example.com/mcp. "
            "The URL path is also used as this server's endpoint path; reverse "
            "proxies should forward that path unchanged."
        ),
    )
    parser.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="Advanced: extra allowed Host header for sse/http transports. Repeatable.",
    )
    parser.add_argument(
        "--allowed-origin",
        action="append",
        default=[],
        help="Advanced: extra allowed Origin header for sse/http transports. Repeatable.",
    )
    parser.add_argument(
        "--disable-dns-rebinding-protection",
        action="store_true",
        help=(
            "Advanced/dangerous: disables DNS rebinding protection; only safe "
            "behind an authenticated reverse proxy."
        ),
    )
    shared = parser.add_argument_group(
        "shared daemon (POSIX)",
        "One engine for several MCP clients. Without these options nothing is shared.",
    )
    shared.add_argument(
        "--shared",
        metavar="NAME",
        type=_daemon_name,
        help=(
            "stdio only: join the shared daemon NAME, starting it when none is running. "
            "A session that cannot join (another configuration, a store that cannot be "
            "shared, Windows) runs as a standalone server instead, and mem_status says why."
        ),
    )
    shared.add_argument(
        "--daemon",
        metavar="NAME",
        type=_daemon_name,
        help="run the shared daemon NAME in this process, with this environment",
    )
    shared.add_argument(
        "--idle-timeout",
        metavar="S",
        type=_idle_timeout,
        default=None,
        help="with --daemon: seconds with no session before exiting (default 600; 0 never)",
    )
    # The transport options start as ``None``, which argparse leaves alone
    # unless the option is given, so one given with its default value still
    # counts as given. The defaults are filled in below.
    args = parser.parse_args(argv, argparse.Namespace(**dict.fromkeys(_TRANSPORT_OPTIONS)))
    explicit = [
        "--" + name.replace("_", "-")
        for name in _TRANSPORT_OPTIONS
        if getattr(args, name) is not None
    ]
    for name in _TRANSPORT_OPTIONS:
        if getattr(args, name) is None:
            setattr(args, name, parser.get_default(name))
    if args.shared is not None and args.daemon is not None:
        parser.error("--shared and --daemon cannot be combined")
    if args.shared is not None and args.transport != "stdio":
        parser.error("--shared is stdio only")
    if args.daemon is not None and explicit:
        parser.error("--daemon takes no transport options: " + ", ".join(explicit))
    if args.idle_timeout is not None and args.daemon is None:
        parser.error("--idle-timeout needs --daemon")
    return args


def _is_direct_stdio_terminal() -> bool:
    return sys.stdin.isatty() and sys.stdout.isatty()


def main(argv: list[str] | None = None) -> None:
    """Run ``memtomem-server`` with ``argv`` (``None``: the process's own)."""
    args = parse_server_args(argv)

    if args.daemon is not None:
        from memtomem.server.daemon import main as daemon_main

        daemon_argv = [args.daemon]
        if args.idle_timeout is not None:
            daemon_argv += ["--idle-timeout", str(args.idle_timeout)]
        raise SystemExit(daemon_main(daemon_argv))

    fallback: dict[str, str] | None = None
    # A terminal is not a client: the standalone path explains that and exits,
    # before any bridge work could connect to, or start, a daemon.
    if args.shared is not None and not _is_direct_stdio_terminal():
        from memtomem._bridge import run_bridge

        fallback = {"name": args.shared, "reason": run_bridge(args.shared)}

    from memtomem.server import run_standalone

    run_standalone(args, fallback=fallback)
