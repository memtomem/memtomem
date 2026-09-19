"""memtomem serve — run the MCP stdio server from the unified CLI.

``memtomem-server`` has always been the server entry point and stays a
first-class, permanently supported one. This adds the same stdio server under
the CLI that the PyPI distribution is named after, because that is the only
shape the MCP registry can express: a client launches ``uvx <pypi-name>`` with
the record's ``packageArguments`` appended, and the distribution is ``memtomem``
— whose console script is this CLI, not the server. Without a subcommand a
registry-driven client would run the CLI, get its help text, and see a server
that exited immediately.

Deliberately stdio-only. Network transports stay on ``memtomem-server``, where
ADR-0029's trusted-network stance and its flags already live; duplicating that
surface here would fork a security-relevant contract across two commands. Click
rejects unknown options, so a user reaching for ``--transport`` gets an error
naming the flag rather than a silently ignored one.
"""

from __future__ import annotations

import click


@click.command("serve")
def serve() -> None:
    """Run the MCP server over stdio (for MCP clients).

    For network transports and their options, use `memtomem-server`.
    """
    from memtomem.server import main as server_main

    # Explicit empty argv, never the implicit ``None``. ``main()`` re-parses
    # the command line with argparse, and Click does not consume ``serve``
    # from ``sys.argv`` — so ``server_main()`` would hand argparse the
    # subcommand name and die with "unrecognized arguments: serve" (exit 2)
    # before the server ever starts.
    server_main([])
