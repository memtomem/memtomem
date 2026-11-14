"""Browser test: a namespace description the server refuses as secret-shaped
shows a localized error and is not retried (#3352).

``PATCH /api/namespaces/{namespace}`` answers the shared 403
``redaction_blocked`` payload when ``storage.set_namespace_meta`` refuses the
description. That refusal has no bypass, so the edit form must not offer the
confirm-and-retry prompt the ``/api/add`` flow uses, and must not show the
literal ``redaction_blocked`` either.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from .test_ns_edit_color_preserved import _open_edit_form
from .test_sessions_panel_e2e import _dev_web_server
from .test_ui_smoke_matrix import _api_payload

pytestmark = pytest.mark.browser


def _install_stubs(page, patches: list[dict]) -> None:
    def _route(route) -> None:
        request = route.request
        path = urlparse(request.url).path
        status = 200
        if path == "/api/namespaces":
            body: dict[str, object] = {
                "namespaces": [
                    {"namespace": "work", "chunk_count": 3, "description": "old", "color": ""}
                ],
                "total": 1,
            }
        elif path == "/api/namespaces/work" and request.method == "PATCH":
            patches.append(request.post_data_json)
            status = 403
            body = {
                "detail": {
                    "detail": "redaction_blocked",
                    "hits": 2,
                    "surface": "web_api_namespace_update",
                }
            }
        else:
            body = _api_payload(request.url, "dev")
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", _route)


def test_refused_description_shows_the_localized_error_once(page) -> None:
    patches: list[dict] = []
    _install_stubs(page, patches)

    with _dev_web_server() as base_url:
        _open_edit_form(page, base_url)
        page.locator("#ns-list .ns-edit-desc").fill("token value")
        page.locator("#ns-list .ns-edit-save").click()
        toast = page.locator(".toast-error").first
        toast.wait_for(timeout=5_000)
        text = toast.inner_text()
        # Give a retry, had there been one, the time to fire.
        page.wait_for_timeout(300)
        confirm_visible = page.locator("#confirm-modal").is_visible()

    assert "matches 2 secret-like pattern(s)" in text
    assert "redaction_blocked" not in text
    assert not confirm_visible
    assert patches == [{"description": "token value"}]
