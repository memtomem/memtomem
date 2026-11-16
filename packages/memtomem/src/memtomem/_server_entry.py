"""``memtomem-server``'s entry point: parse, then hand over (#3381).

The console script points here rather than at :func:`memtomem.server.main`
because importing :mod:`memtomem.server` registers every MCP tool, which is
most of a standalone server's startup time and memory. Arguments are parsed
before that import, so a process that turns out not to need the server — ``--help``
today, the shared-daemon bridge next — never pays for it. ``mm serve`` and
``memtomem.server.main`` (and so ``python -m memtomem.server``) go through
:func:`main` as well, so every way of starting a server parses one way.
"""

from __future__ import annotations

import argparse


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
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    """Run ``memtomem-server`` with ``argv`` (``None``: the process's own)."""
    args = parse_server_args(argv)

    from memtomem.server import run_standalone

    run_standalone(args)
