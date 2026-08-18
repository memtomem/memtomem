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

import asyncio
import inspect
import json
import socket
import threading
import time
from collections.abc import Callable, Coroutine, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, TypeVar

import pytest
import pytest_asyncio

_T = TypeVar("_T")
_WEB_TESTS = Path(__file__).resolve().parent


def _drives_a_main_thread_loop(item: pytest.Item) -> bool:
    """Whether running ``item`` would spin an event loop on the calling thread.

    Two ways in, because each misses what the other catches:

    * ``pytest_asyncio.is_async_test`` — the library's own classification, which
      covers async ``staticmethod``s and Hypothesis-wrapped coroutines that a
      bare ``iscoroutinefunction`` reads as sync.
    * a coroutine ``item.obj`` pytest-asyncio did *not* claim, e.g. a
      ``unittest.IsolatedAsyncioTestCase`` method, which runs its own
      ``asyncio.run``.

    Async *fixtures* drive the same runner and are caught in
    ``pytest_fixture_setup`` instead: which definition wins an override, and
    whether a fixture is requested at all (``request.getfixturevalue``), is only
    known once pytest resolves it.
    """

    if pytest_asyncio.is_async_test(item):
        return True
    return inspect.iscoroutinefunction(getattr(item, "obj", None))


def is_main_thread_async_item(item: pytest.Item) -> bool:
    """Whether ``item`` drives an event loop *and* is collected from here.

    Collection position, not the source file, is the risk axis: coroutines
    defined in a ``__test__ = False`` module here (``test_upload_quarantine.py``)
    are collected through ``tests/test_web_routes.py``, ahead of every browser
    spec, and their item path says so.
    """

    if _WEB_TESTS not in Path(str(item.path)).resolve().parents:
        return False
    return _drives_a_main_thread_loop(item)


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Refuse to run a coroutine test collected from ``tests/web`` (#2099).

    Playwright's sync API parks a dispatcher greenlet inside
    ``loop.run_until_complete`` for the whole session. Greenlets share the
    thread, so asyncio can still see that loop as *running* on MainThread when a
    later test starts, and pytest-asyncio then fails the test before its body
    runs with ``RuntimeError: Runner.run() cannot be called from a running event
    loop``. Whether a given test trips it depends on where the dispatcher
    parked, which made #2099 look like order-dependent pollution — and it never
    reproduced on CI, where the browser specs auto-skip without Chromium.

    Coroutine work here goes through the ``run_async`` fixture below instead.

    A hook rather than a guard test, so enforcement never depends on selection:
    ``-m``/``-k`` deselect tests, and the Windows file shards would put a guard
    test in one shard only. ``tryfirst`` puts this ahead of the mark plugin's
    own ``pytest_collection_modifyitems``, which is where deselection happens —
    without it a deselected offender is invisible here. Collection has already
    finished at this point, so reporting does not mask later collection errors,
    and every offender is named at once.
    """

    offenders = [item.nodeid for item in items if is_main_thread_async_item(item)]
    if offenders:
        raise pytest.UsageError(
            "async test functions collected from tests/web race Playwright's "
            f"parked event loop (#2099): {offenders}. Make the test sync and "
            "drive the coroutine with the ``run_async`` fixture in "
            "tests/web/conftest.py."
        )


@pytest.hookimpl(tryfirst=True)
def pytest_fixture_setup(fixturedef: pytest.FixtureDef, request: pytest.FixtureRequest) -> None:
    """Reject an async fixture resolved for a test in this directory (#2099).

    Same hazard as an ``async def`` test — pytest-asyncio drives async fixtures
    through the same ``Runner`` on MainThread, so a sync test body is no proof
    of safety. Enforced here rather than by walking ``_fixtureinfo`` at
    collection because only pytest knows which definition survives an override
    and which fixtures a ``request.getfixturevalue`` call will ask for.
    """

    # ``request.node`` is the *scope* node — the session root for a
    # session-scoped fixture, the package for a package-scoped one — so it says
    # nothing about who asked. ``_pyfuncitem`` is the test that actually
    # triggered the resolution, whatever the fixture's scope.
    item = getattr(request, "_pyfuncitem", None) or getattr(request, "node", None)
    item_path = getattr(item, "path", None)
    if item_path is None or _WEB_TESTS not in Path(str(item_path)).resolve().parents:
        return
    func = getattr(fixturedef, "func", None)
    if func is None:
        return
    # pytest-asyncio's own ``pytest_fixture_setup`` is a hookwrapper, so by the
    # time this runs it has already swapped ``func`` for a sync synchronizer.
    # ``functools.wraps`` leaves ``__wrapped__`` behind — unwrap to see what the
    # author actually declared.
    original = inspect.unwrap(func)
    if any(
        inspect.iscoroutinefunction(candidate) or inspect.isasyncgenfunction(candidate)
        for candidate in (func, original)
    ):
        raise pytest.UsageError(
            f"async fixture {fixturedef.argname!r} requested by {getattr(item, 'nodeid', item)} "
            "races Playwright's parked event loop (#2099). Make the fixture sync and drive "
            "the coroutine with the ``run_async`` fixture in tests/web/conftest.py."
        )


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


@pytest.fixture(scope="session")
def run_async() -> Callable[[Coroutine[Any, Any, _T]], _T]:
    """Run a coroutine on a private loop in a worker thread.

    Playwright's sync API parks a dispatcher greenlet inside
    ``loop.run_until_complete`` for the lifetime of the session
    (``playwright/sync_api/_context_manager.py``). Greenlets share the thread,
    so asyncio keeps that loop registered as *running* on MainThread, and
    pytest-asyncio then refuses to start any coroutine test with
    ``RuntimeError: Runner.run() cannot be called from a running event loop``
    (issue #2099). Whether a given test sees the flag depends on where the
    dispatcher happened to park, which is what made the failure look like
    order-dependent pollution.

    A worker thread has its own asyncio state, so a coroutine driven through
    here is immune to whatever the browser specs leave on MainThread. Tests in
    this directory must not be ``async def`` — ``pytest_collection_modifyitems``
    above rejects one at collection time, and ``test_no_main_thread_async.py``
    pins that the rejection is armed.
    """

    def _run(coro: Coroutine[Any, Any, _T]) -> _T:
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="mm-web-async") as pool:
            return pool.submit(asyncio.run, coro).result()

    return _run
