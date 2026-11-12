"""Browser tests: the namespace edit form sends ``color`` only when the user
changed it (#3116).

An ``<input type="color">`` only holds ``#rrggbb``, so the browser rewrites a
stored ``#abc`` to ``#aabbcc`` (and a 5-digit value to ``#000000``). Saving
only the description must therefore leave ``color`` out of the PATCH body.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from .test_scratch_and_ns_color_escape import _open_settings_section
from .test_sessions_panel_e2e import _dev_web_server
from .test_ui_smoke_matrix import _api_payload

pytestmark = pytest.mark.browser

_STORED = "#abc"


def _install_stubs(page, patches: list[dict]) -> None:
    def _route(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        if path == "/api/namespaces":
            body: dict[str, object] = {
                "namespaces": [
                    {
                        "namespace": "work",
                        "chunk_count": 3,
                        "description": "old",
                        "color": _STORED,
                    }
                ],
                "total": 1,
            }
        elif path == "/api/namespaces/work" and request.method == "PATCH":
            patches.append(request.post_data_json)
            body = {"namespace": "work", "chunk_count": 3, "description": "new", "color": ""}
        else:
            body = _api_payload(request.url, "dev")
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", _route)


def _open_edit_form(page, base_url: str) -> None:
    page.goto(base_url, wait_until="domcontentloaded")
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)
    _open_settings_section(page, "namespaces")
    page.wait_for_selector("#ns-list .ns-card", timeout=5_000)
    page.locator("#ns-list .ns-card button", has_text="Edit").first.click()
    page.locator("#ns-list .ns-edit-color").wait_for(timeout=5_000)


def test_description_only_save_omits_color(page) -> None:
    patches: list[dict] = []
    _install_stubs(page, patches)

    with _dev_web_server() as base_url:
        _open_edit_form(page, base_url)
        # The browser normalized the stored value; the form must not send it back.
        assert page.locator("#ns-list .ns-edit-color").input_value() == "#aabbcc"
        page.locator("#ns-list .ns-edit-desc").fill("new")
        page.locator("#ns-list .ns-edit-save").click()
        page.wait_for_selector(".toast-success", timeout=5_000)

    assert patches == [{"description": "new"}]


def test_changed_color_is_sent(page) -> None:
    patches: list[dict] = []
    _install_stubs(page, patches)

    with _dev_web_server() as base_url:
        _open_edit_form(page, base_url)
        page.locator("#ns-list .ns-edit-color").fill("#112233")
        page.locator("#ns-list .ns-edit-save").click()
        page.wait_for_selector(".toast-success", timeout=5_000)

    assert patches == [{"description": "old", "color": "#112233"}]


def test_reset_clears_the_stored_color(page) -> None:
    patches: list[dict] = []
    _install_stubs(page, patches)

    with _dev_web_server() as base_url:
        _open_edit_form(page, base_url)
        page.locator("#ns-list .ns-edit-reset-color").click()
        page.locator("#ns-list .ns-edit-save").click()
        page.wait_for_selector(".toast-success", timeout=5_000)

    assert patches == [{"description": "old", "color": ""}]
