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

import json
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


@pytest.fixture(autouse=True)
def _returning_install(request) -> None:
    """Boot every browser spec as a returning install on the full surface
    (S2.1 + S2.2).

    S2.1 routes a *genuine* first run — a fresh context with no app-owned
    localStorage key — to the Home tab for orientation. S2.2 then defaults a
    fresh install to *Simple* mode, which hides the Tags + Timeline tabs and the
    Settings → Data group. These specs each open a fresh browser context, so
    without seeds they would (a) land on Home and (b) sit in Simple mode —
    failing the moment they touch a Search element or a now-hidden advanced tab
    (tag filters, the Timeline view, the skip-link to ``#main``). Seeding both
    flags before navigation restores the historical full-surface Search default;
    a spec that wants the first-run landing or Simple mode opts in by overriding
    the relevant key with its own ``add_init_script``.

    Gated on the ``browser`` marker and requests ``page`` lazily so the
    non-browser specs in this directory (CSS / asset-pin checks) aren't forced
    to launch a browser in the no-browser test job.
    """
    if request.node.get_closest_marker("browser") is None:
        return
    page = request.getfixturevalue("page")
    page.add_init_script(
        "try {"
        " localStorage.setItem('m2m-app-initialized', '1');"
        " localStorage.setItem('m2m-app-simple', '0');"
        " } catch (e) {}"
    )


def install_default_stubs(page) -> None:
    """Stub every endpoint the SPA hits during boot so the page renders
    cleanly without any real components wired up.

    Boot fetches not stubbed individually get a generic empty-shape
    response. The pattern is intentionally permissive — specs override
    only the endpoints they assert on.

    **Last-route-wins.** ``page.route`` resolves last-registered-wins,
    so the catch-all goes FIRST and specific overrides go LAST. Specs
    that need to assert on a particular endpoint register their
    capturing handler AFTER calling this helper, and the same last-wins
    rule gives the spec-local handler precedence over the default empty
    response.

    Extracted from 7 per-spec duplicates per #879 (PR #878 review note).
    ``test_sources_reindex_retry.py`` uses a different stub set
    (memory-dirs scope) and intentionally keeps its own local helper.
    """

    def _ok(route, payload):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/**", lambda r: _ok(r, {}))
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/namespaces", lambda r: _ok(r, {"namespaces": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    page.route("**/api/privacy/patterns", lambda r: _ok(r, {"patterns": []}))
    # ``/api/context/projects`` needs a valid ``{scopes: [...]}`` shape, not
    # the catch-all ``{}``: since #1100 ``_ctxFetchProjects`` treats a 200 that
    # isn't ``{scopes: Array}`` as a failure and fires a "project list failed to
    # load" error toast. Under the bare ``{}`` that toast lands in
    # ``#toast-container`` on every boot and shadows / duplicates the toast
    # specs assert on (strict-mode "resolved to 2 elements"). One synthetic
    # server-CWD scope mirrors the legacy single-project payload and stays
    # silent. ``**`` tail also matches the ``?target_scope=`` query variant.
    page.route(
        "**/api/context/projects**",
        lambda r: _ok(
            r,
            {
                "scopes": [
                    {
                        "scope_id": "",
                        "label": "Server CWD",
                        "root": "",
                        "tier": "project",
                        "sources": ["server-cwd"],
                        "experimental": False,
                        "missing": False,
                        "counts": {"skills": 0, "commands": 0, "agents": 0},
                    }
                ]
            },
        ),
    )
