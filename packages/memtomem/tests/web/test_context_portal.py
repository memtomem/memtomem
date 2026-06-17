"""Browser tests for the Context Portal board (ADR-0021 PR4).

Drives the real ``context-portal.js`` in the SPA: navigate to the Projects
section, assert the board renders per-scope health (stale / missing gray-out)
and counts, that search filters client-side, and that rename / unregister fire
the PATCH / DELETE the backend expects. ``page.route`` short-circuits every
``/api`` call before it reaches the server, so no CSRF middleware / DB is in
play — the spec asserts the click → request wiring only.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

# Marks every test here as browser-dependent: the root conftest auto-skips
# ``@pytest.mark.browser`` when Chromium/pytest-playwright is absent (the CI
# ``test`` lane), and the ``test-browser`` lane selects it via ``-m browser``.
pytestmark = pytest.mark.browser

_PORTAL_SCOPES = {
    "target_scope": "project_shared",
    "scopes": [
        {
            "scope_id": "",
            "project_scope_id": "",
            "label": "Server CWD",
            "root": "/srv",
            "tier": "project",
            "sources": ["server-cwd"],
            "missing": False,
            "stale": False,
            "experimental": False,
            "counts": {"skills": 2, "commands": 1, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "p-alpha",
            "project_scope_id": "p-alpha",
            "label": "Alpha",
            "root": "/work/alpha",
            "tier": "project",
            "sources": ["known-projects"],
            "missing": False,
            "stale": False,
            "experimental": False,
            "counts": {"skills": 5, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "p-beta",
            "project_scope_id": "p-beta",
            "label": "Beta",
            "root": "/work/beta",
            "tier": "project",
            "sources": ["known-projects"],
            "missing": False,
            "stale": True,
            "experimental": False,
            "counts": {"skills": 0, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "p-gone",
            "project_scope_id": "p-gone",
            "label": "Ghost",
            "root": "/work/ghost",
            "tier": "project",
            "sources": ["known-projects"],
            "missing": True,
            "stale": False,
            "experimental": False,
            "counts": None,
        },
    ],
}


def _stub_portal(page, captured=None):
    """Catch-all + a rich multi-scope projects payload; capture known-projects
    mutations (PATCH/DELETE) when ``captured`` is provided. Registered AFTER
    install_default_stubs so it wins (last-route-wins)."""
    install_default_stubs(page)

    def _projects(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_PORTAL_SCOPES))

    page.route("**/api/context/projects**", _projects)

    def _mutate(route):
        req = route.request
        if captured is not None:
            captured.append({"method": req.method, "url": req.url, "body": req.post_data})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scope_id": "x", "label": "x"}),
        )

    page.route("**/api/context/known-projects/**", _mutate)


def _open_portal(page, mm_web_url: str) -> None:
    page.goto(mm_web_url)
    page.locator("#tabbtn-context-gateway").click()
    # ADR-0026 D-F flip: Simple is the default and hides the section nav while the
    # Overview is active — switch to Advanced so the Projects nav button is shown.
    page.evaluate("() => _ctxSetSimpleMode(false)")
    page.locator(".settings-nav-btn[data-section='ctx-projects']").click()
    page.wait_for_selector(".ctx-portal-row", timeout=3_000)


def test_portal_renders_health_and_counts(page, mm_web_url: str) -> None:
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    # Verify health rendering for ALL scopes incl. the stale Beta row, which the
    # default-on "Initialized only" toggle hides — turn it off for this test.
    page.locator("#ctx-portal-hide-uninit").uncheck()
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 4", timeout=2_000
    )

    rows = page.locator(".ctx-portal-row")
    assert rows.count() == 4

    # Stale row → stale badge; missing row → dimmed + missing badge + no counts.
    assert page.locator(
        ".ctx-portal-row[data-scope-id='p-beta'] .ctx-scope-badge--stale"
    ).is_visible()
    ghost = page.locator(".ctx-portal-row[data-scope-id='p-gone']")
    assert "ctx-portal-row--missing" in (ghost.get_attribute("class") or "")
    assert ghost.locator(".ctx-scope-badge--missing").is_visible()
    assert ghost.locator(".ctx-portal-counts").count() == 0

    # Healthy managed row shows its four count chips.
    assert page.locator(".ctx-portal-row[data-scope-id='p-alpha'] .ctx-portal-count").count() == 4


def test_portal_search_filters(page, mm_web_url: str) -> None:
    _stub_portal(page)
    _open_portal(page, mm_web_url)

    page.locator("#ctx-portal-search").fill("beta")
    # Only the Beta row survives the client-side filter.
    page.wait_for_function(
        "() => document.querySelectorAll('.ctx-portal-row').length === 1",
        timeout=2_000,
    )
    assert page.locator(".ctx-portal-row .ctx-portal-label").inner_text().strip() == "Beta"


def test_portal_rename_fires_patch(page, mm_web_url: str) -> None:
    captured: list[dict] = []
    _stub_portal(page, captured)
    _open_portal(page, mm_web_url)

    page.locator(".ctx-portal-row[data-scope-id='p-alpha'] .ctx-portal-rename").click()
    page.locator(".ctx-portal-label-input").fill("Alpha Prod")
    page.locator(".ctx-portal-label-save").click()
    page.wait_for_timeout(200)

    patch = next((c for c in captured if c["method"] == "PATCH"), None)
    assert patch is not None, f"expected a PATCH, got {captured}"
    assert "/api/context/known-projects/p-alpha" in patch["url"]
    assert json.loads(patch["body"]) == {"label": "Alpha Prod"}


def test_portal_unregister_fires_delete_after_confirm(page, mm_web_url: str) -> None:
    captured: list[dict] = []
    _stub_portal(page, captured)
    _open_portal(page, mm_web_url)

    # p-beta is stale (uninitialized); reveal it past the default-on
    # "Initialized only" toggle before exercising its remove button.
    page.locator("#ctx-portal-hide-uninit").uncheck()
    page.wait_for_function(
        "() => !!document.querySelector(\".ctx-portal-row[data-scope-id='p-beta']\")",
        timeout=2_000,
    )

    page.locator(".ctx-portal-row[data-scope-id='p-beta'] .ctx-portal-remove").click()
    # showConfirm modal — confirm.
    page.wait_for_selector("#confirm-modal:not([hidden])", timeout=2_000)
    page.locator("#confirm-ok-btn").click()
    page.wait_for_timeout(200)

    delete = next((c for c in captured if c["method"] == "DELETE"), None)
    assert delete is not None, f"expected a DELETE, got {captured}"
    assert "/api/context/known-projects/p-beta" in delete["url"]
