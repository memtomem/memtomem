"""Start a shared daemon detached from the bridge that asked for it (#3381).

The bridge runs this as a short-lived child and reaps it, so the daemon is
never the bridge's child: it is reparented when this process exits, owns a new
session, and holds none of the bridge's descriptors. Standard library only.
"""

from __future__ import annotations

import subprocess  # nosec B404 - a fixed argv of this interpreter and module
import sys


def main(argv: list[str]) -> int:
    if len(argv) != 1:
        print("usage: python -m memtomem._spawn_helper NAME", file=sys.stderr)
        return 2
    subprocess.Popen(  # nosec B603 - fixed argv, no shell
        [sys.executable, "-P", "-m", "memtomem.server.daemon", argv[0], "--log-to-runtime"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
