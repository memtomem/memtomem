"""Browser tests: caller-controlled scratch fields and namespace colors render
as text, and the scratch actions report through ``showToast`` (#3109).

Scratch entries come from ``POST /api/scratch`` / ``mem_scratch_set`` and the
namespace color from the metadata row, so neither may reach ``innerHTML`` raw.
A row stored before the server validated colors can hold anything; the SPA
renders such a value as its fallback color.

Not covered here: the D3 donut/legend override in ``settings-namespaces.js``
only runs when ``window.d3`` exists, and the shipped page loads no D3, so the
Home chart under test is the bar chart in ``app.js``.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from .test_sessions_panel_e2e import _dev_web_server
from .test_ui_smoke_matrix import _api_payload

pytestmark = pytest.mark.browser

# ``"`` and ``&amp;`` make body-only escaping visible: without ``escapeAttr``
# the quote ends ``data-key`` early, and the entity would decode on the way
# back through ``dataset.key``.
_KEY = 'a"b&amp;c<img id="pwn-key" src=x>'
_VALUE = '<img id="pwn-val" src=x onerror="window.__pwned = true">'
_SESSION = "<i id=s>"  # 8 characters: survives the panel's ``slice(0, 8)``
_COLOR = '"><img id="pwn-color" src=x onerror="window.__pwned = true">'


def _install_stubs(page, seen: list[tuple[str, str]]) -> None:
    def _route(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path.startswith("/api/scratch/"):
            seen.append((request.method, request.url))
            if request.method == "DELETE":
                route.fulfill(
                    status=500,
                    content_type="application/json",
                    body=json.dumps({"detail": "delete failed"}),
                )
                return
            body: dict[str, object] = {"ok": True}
        elif path == "/api/scratch":
            body = {
                "entries": [
                    {
                        "key": _KEY,
                        "value": _VALUE,
                        "session_id": _SESSION,
                        "expires_at": None,
                        "promoted": False,
                    }
                ],
                "total": 1,
            }
        elif path == "/api/namespaces":
            body = {
                "namespaces": [
                    {"namespace": "evil", "chunk_count": 3, "description": "", "color": _COLOR}
                ]
            }
        else:
            body = _api_payload(request.url, "dev")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", _route)


def _open_settings_section(page, section: str) -> None:
    page.locator('.tab-btn[data-tab="settings"]').click()
    nav = page.locator(f'#tab-settings .settings-nav-btn[data-section="{section}"]')
    if not nav.first.is_visible():
        group = nav.first.get_attribute("data-group")
        if group:
            group_loc = page.locator(f'#tab-settings .settings-nav-group[data-group="{group}"]')
            if group_loc.count():
                group_loc.first.click()
                page.wait_for_timeout(100)
    nav.first.click()


def test_scratch_fields_render_as_text_and_actions_toast(page) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    seen: list[tuple[str, str]] = []
    _install_stubs(page, seen)

    with _dev_web_server() as base_url:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)
        _open_settings_section(page, "harness-scratch")
        page.wait_for_selector("#scratch-list table", timeout=5_000)

        table = page.locator("#scratch-list table")
        assert page.locator("#scratch-list img, #scratch-list i").count() == 0
        text = table.inner_text()
        assert _KEY in text
        assert _VALUE in text
        assert _SESSION in text

        delete_btn = page.locator('#scratch-list [data-action="scratch-delete"]')
        promote_btn = page.locator('#scratch-list [data-action="scratch-promote"]')
        assert delete_btn.evaluate("el => el.dataset.key") == _KEY
        assert promote_btn.evaluate("el => el.dataset.key") == _KEY
        encoded = page.evaluate("k => encodeURIComponent(k)", _KEY)

        delete_btn.click()
        page.wait_for_selector(".toast-error", timeout=5_000)
        assert "delete failed" in page.locator(".toast-error").first.inner_text()

        promote_btn.click()
        page.wait_for_selector(".toast-success", timeout=5_000)
        assert "Promoted to long-term memory" in page.locator(".toast-success").first.inner_text()

        assert ("DELETE", f"{base_url}/api/scratch/{encoded}") in seen
        assert ("POST", f"{base_url}/api/scratch/{encoded}/promote") in seen
        assert page.evaluate("() => window.__pwned") is None
        assert errors == []


def test_hostile_namespace_color_renders_as_the_fallback(page) -> None:
    errors: list[str] = []
    page.on("pageerror", lambda exc: errors.append(str(exc)))
    _install_stubs(page, [])

    with _dev_web_server() as base_url:
        page.goto(base_url, wait_until="domcontentloaded")
        page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)

        # Home bar chart (app.js ``_renderNsChart``).
        page.locator('.tab-btn[data-tab="home"]').click()
        fill = page.locator("#home-ns-chart .home-ns-row .home-bar-fill")
        fill.first.wait_for(state="attached", timeout=5_000)
        assert fill.count() == 1
        assert page.locator("#pwn-color").count() == 0
        assert "background:var(--accent)" in (fill.first.get_attribute("style") or "")

        # Settings → Namespaces edit form (``value`` attribute).
        _open_settings_section(page, "namespaces")
        page.wait_for_selector("#ns-list .ns-card", timeout=5_000)
        # Card dot (``style.backgroundColor``): the browser would drop the
        # hostile string and leave the dot uncolored; the fallback is set.
        dot = page.locator("#ns-list .ns-card .ns-color-dot")
        assert dot.count() == 1
        assert dot.evaluate("el => el.style.backgroundColor") == "var(--muted)"
        page.locator("#ns-list .ns-card button", has_text="Edit").first.click()
        color_input = page.locator("#ns-list .ns-edit-color")
        color_input.wait_for(timeout=5_000)
        assert color_input.input_value() == "#808080"
        assert page.locator("#pwn-color").count() == 0

        assert page.evaluate("() => window.__pwned") is None
        assert errors == []
