"""Browser-test fixtures for the Web UI.

The harness exists to catch regressions in click → DOM-state wiring inside
``packages/memtomem/src/memtomem/web/static/app.js`` (see issue #751 for the
motivating tag-filter mutation cluster). It is deliberately scoped to JS
behaviour only — every ``/api/**`` call is intercepted via ``page.route()``
in the individual specs, so the harness needs to serve the static SPA but
does **not** need real components, a real DB, or a real index.

The lifespan is therefore skipped (``create_app(lifespan=None)``); route
handlers that try to read ``app.state.storage`` etc. would 500, but
``page.route()`` intercepts those requests before they reach the server.
This keeps the fixture under a second to spin up and removes a whole class
of flake (indexing timing, embedding model presence, port collisions).
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator

import pytest


@pytest.fixture(scope="session")
def mm_web_url() -> Iterator[str]:
    """Run the Web UI in a background thread on an ephemeral port.

    Yields the base URL (``http://127.0.0.1:<port>``). On teardown the
    uvicorn ``should_exit`` flag is set and the thread joined; the daemon
    flag is a belt-and-braces guard for the case where teardown raises
    before the join.
    """
    import asyncio

    import uvicorn

    from memtomem.web.app import create_app

    app = create_app(lifespan=None, mode="prod")

    # Bind to port 0, then read the actual port off the listening socket
    # after startup. Doing the bind ourselves (rather than letting uvicorn
    # do it) makes the port readable synchronously without polling
    # ``server.servers[0].sockets``, which is populated lazily.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        access_log=False,
        lifespan="off",
    )
    server = uvicorn.Server(config)
    sock.close()  # uvicorn rebinds; the probe was just to grab a free port

    def _run() -> None:
        asyncio.run(server.serve())

    thread = threading.Thread(target=_run, daemon=True, name="mm-web-test-server")
    thread.start()

    # Wait up to ~5s for the server to come up. ``server.started`` is the
    # documented flag for this; the loop is bounded so a misconfigured
    # server fails the suite instead of hanging CI.
    deadline = time.monotonic() + 5.0
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.02)
    if not server.started:
        server.should_exit = True
        thread.join(timeout=2.0)
        raise RuntimeError("uvicorn server did not start within 5s")

    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5.0)
