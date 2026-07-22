"""Browser-level checks for the dev Sessions panel (#1913).

Complements the jsdom unit tests (``tests-js/sessions-panel.test.mjs``) with the
one thing jsdom cannot verify: that the metadata toggle works under the real
``script-src`` CSP. The panel replaced an inline ``onclick`` with a delegated
``data-action`` handler precisely because the inline handler is blocked in a
real browser; only a real Chromium run proves the replacement.

Also confirms the summary-origin badge renders, caller-controlled fields do not
execute as markup, and a language toggle re-localizes the badge live.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import pytest

from .test_ui_smoke_matrix import _api_payload

pytestmark = pytest.mark.browser

_SESSION_ID = "11111111-1111-4111-8111-111111111111"
_XSS = '<img src=x onerror="window.__pwned = true">'


@contextmanager
def _dev_web_server() -> Iterator[str]:
    import uvicorn

    from memtomem.web.app import create_app

    app = create_app(lifespan=None, mode="dev")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]

    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", access_log=False, lifespan="off"
    )
    server = uvicorn.Server(config)
    sock.close()

    thread = threading.Thread(
        target=lambda: asyncio.run(server.serve()), daemon=True, name="mm-web-sessions-e2e"
    )
    thread.start()
    try:
        for _ in range(100):
            if getattr(server, "started", False):
                break
            threading.Event().wait(0.05)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)


def _install_stubs(page) -> None:
    def _route(route) -> None:
        url = route.request.url
        if "/api/sessions/" in url and url.endswith("/events"):
            body = {
                "events": [
                    {
                        "event_type": "add",
                        "content": _XSS,
                        "metadata": {"provenance": "write-v1", "note": _XSS},
                        "created_at": "2026-07-22T00:00:00+00:00",
                    }
                ]
            }
        elif "/api/sessions" in url:
            body = {
                "sessions": [
                    {
                        "id": _SESSION_ID,
                        "agent_id": _XSS,
                        "namespace": "agent-runtime:planner",
                        "started_at": "2026-07-22T00:00:00+00:00",
                        "ended_at": "2026-07-22T01:00:00+00:00",
                        "summary": "did some work",
                        "metadata": {"title": "Sprint planning", "summary_provenance": "exact"},
                    }
                ],
                "total": 1,
            }
        else:
            # Delegate the common bootstrap endpoints (ui-mode, session,
            # config, stats, …) to the smoke matrix's dev payloads so the
            # client reveals the dev-tier nav that hosts this panel.
            body = _api_payload(url, "dev")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", _route)


def _open_sessions_panel(page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)
    page.locator('.tab-btn[data-tab="settings"]').click()
    nav = page.locator('#tab-settings .settings-nav-btn[data-section="harness-sessions"]')
    # The harness nav lives in a collapsible group; expand it if the button
    # is not already visible (mirrors _ensure_nav_visible in the smoke matrix).
    if not nav.first.is_visible():
        group = nav.first.get_attribute("data-group")
        if group:
            group_loc = page.locator(f'#tab-settings .settings-nav-group[data-group="{group}"]')
            if group_loc.count():
                group_loc.first.click()
                page.wait_for_timeout(100)
    nav.first.click()
    page.wait_for_selector("#sessions-list table", timeout=5_000)


def test_sessions_panel_renders_metadata_without_xss(page) -> None:
    _install_stubs(page)

    with _dev_web_server() as base_url:
        _open_sessions_panel(page, base_url)

        table = page.locator("#sessions-list table")
        assert "Sprint planning" in table.inner_text()
        # exact → the "recorded" origin badge (en)
        assert page.locator("#sessions-list .badge-success").inner_text() == "recorded"
        # the hostile agent_id rendered as text, not an executing image
        assert page.locator("#sessions-list img").count() == 0
        assert page.evaluate("() => window.__pwned") is None


def test_metadata_toggle_works_under_csp(page) -> None:
    """The delegated data-action toggle must fire under the real CSP — an
    inline onclick would be blocked and silently do nothing."""
    csp_violations: list[str] = []
    page.on(
        "console",
        lambda m: csp_violations.append(m.text) if "Content Security Policy" in m.text else None,
    )
    _install_stubs(page)

    with _dev_web_server() as base_url:
        _open_sessions_panel(page, base_url)
        page.locator('#sessions-list [data-action="session-events"]').click()
        page.wait_for_selector("#session-events-list .harness-event", timeout=5_000)

        meta = page.locator("#session-events-list .harness-event-meta")
        assert meta.evaluate("el => el.hidden") is True
        page.locator('#session-events-list [data-action="toggle-next"]').click()
        assert meta.evaluate("el => el.hidden") is False
        assert page.locator("#session-events-list img").count() == 0
        assert csp_violations == []


def test_events_request_survives_browser_path_canonicalization(page) -> None:
    """A real browser resolves ``/../`` in a URL path. An external session id
    that embeds ``a/../b`` must reach the server intact, so the slash is sent
    percent-encoded (``%2F``) rather than literal — otherwise the browser would
    collapse the segment and request a different session."""
    slash_id = "external:a/../b:0123456789abcdef01234567"
    seen_paths: list[str] = []

    def _route(route) -> None:
        url = route.request.url
        if "/events" in url:
            seen_paths.append(url)
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps({"events": []})
            )
            return
        if "/api/sessions" in url:
            body = {
                "sessions": [
                    {
                        "id": slash_id,
                        "agent_id": "planner",
                        "namespace": "ns",
                        "started_at": "2026-07-22T00:00:00+00:00",
                        "ended_at": "2026-07-22T01:00:00+00:00",
                        "summary": "s",
                        "metadata": {},
                    }
                ],
                "total": 1,
            }
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_api_payload(url, "dev"))
        )

    page.route("**/api/**", _route)

    with _dev_web_server() as base_url:
        _open_sessions_panel(page, base_url)
        with page.expect_request("**/events") as req_info:
            page.locator('#sessions-list [data-action="session-events"]').click()
        req_info.value  # ensure the request fired before the server tears down

    assert seen_paths, "the events request never fired"
    # the /../ survived as %2F.. rather than being canonicalized away
    assert "%2F..%2F" in seen_paths[0]
    assert "/a/../b" not in seen_paths[0]


def test_language_toggle_relocalizes_the_origin_badge(page) -> None:
    _install_stubs(page)
    with _dev_web_server() as base_url:
        _open_sessions_panel(page, base_url)
        assert page.locator("#sessions-list .badge-success").inner_text() == "recorded"
        page.locator("#lang-toggle").click()
        # Wait on the badge itself, not documentElement.lang: i18n.js sets the
        # lang attribute before the locale fetch resolves and langchange fires,
        # so the badge repaint lags the attribute.
        page.wait_for_function(
            "() => document.querySelector('#sessions-list .badge-success')?.textContent === '정확 기록'"
        )
