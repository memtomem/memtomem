"""Browser test: a namespace description the server refuses as secret-shaped
shows a localized error and is not retried (#3352).

``PATCH /api/namespaces/{namespace}`` answers the shared 403
``redaction_blocked`` payload when ``storage.set_namespace_meta`` refuses the
description. That refusal has no bypass, so the edit form must not offer the
confirm-and-retry prompt the ``/api/add`` flow uses, and must not show the
literal ``redaction_blocked`` either.

When the payload says every hit was a label rule (``label_only``, #3354), the
form shows the wording that names the label instead of telling the user to
remove a secret that may not be there.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest

from .test_ns_edit_color_preserved import _open_edit_form
from .test_sessions_panel_e2e import _dev_web_server
from .test_ui_smoke_matrix import _api_payload

pytestmark = pytest.mark.browser


def _install_stubs(page, patches: list[dict], *, label_only: bool | None = None) -> None:
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
            detail: dict[str, object] = {
                "detail": "redaction_blocked",
                "hits": 2,
                "surface": "web_api_namespace_update",
            }
            if label_only is not None:
                detail["label_only"] = label_only
            body = {"detail": detail}
        else:
            body = _api_payload(request.url, "dev")
        route.fulfill(status=status, content_type="application/json", body=json.dumps(body))

    page.route("**/api/**", _route)


# A payload without the field is what a server older than the page sends.
@pytest.mark.parametrize("label_only", [None, False])
def test_refused_description_shows_the_localized_error_once(page, label_only) -> None:
    patches: list[dict] = []
    _install_stubs(page, patches, label_only=label_only)

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


_LABEL_TOAST = {
    "en": (
        "Description not saved: it has a credential label before ':' or '=' (such as "
        "'password:' or 'api_key='). Reword it, for example 'password rotation', and save "
        "again. A real credential does not belong in a description."
    ),
    "ko": (
        "설명이 저장되지 않았습니다: ':' 또는 '=' 앞에 자격 증명 라벨('password:', "
        "'api_key=' 등)이 있습니다. 'password rotation'처럼 고쳐 쓴 뒤 다시 저장하세요. "
        "실제 자격 증명은 설명에 넣지 마세요."
    ),
}


@pytest.mark.parametrize("lang", ["en", "ko"])
def test_label_only_refusal_names_the_label_once(page, lang) -> None:
    patches: list[dict] = []
    _install_stubs(page, patches, label_only=True)

    with _dev_web_server() as base_url:
        _open_edit_form(page, base_url)
        if lang != "en":
            # After the form is open: the helper finds its button by English text.
            page.evaluate("async (lang) => { await I18N.setLang(lang); }", lang)
        page.locator("#ns-list .ns-edit-desc").fill("set password: rotate monthly")
        page.locator("#ns-list .ns-edit-save").click()
        toast = page.locator(".toast-error").first
        toast.wait_for(timeout=5_000)
        text = toast.inner_text()
        page.wait_for_timeout(300)
        confirm_visible = page.locator("#confirm-modal").is_visible()
        toasts = page.locator(".toast-error").count()

    # The toast's own close button follows the message.
    assert text == f"{_LABEL_TOAST[lang]}\n✕"
    assert toasts == 1
    assert not confirm_visible
    assert patches == [{"description": "set password: rotate monthly"}]
