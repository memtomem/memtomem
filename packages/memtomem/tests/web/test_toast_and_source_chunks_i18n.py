"""Browser tests: toast close button tooltip and empty Source Chunks panel translations (#3136).

- ``showToast`` sets the close button's ``title`` using the ``common.close`` key,
  falling back to "Close" only if I18N is unavailable.
- Clicking ``#d-source-btn`` when a source has no chunks renders an empty state
  bound via ``data-i18n="chunks.none_found"``, displaying the localized string initially
  and updating in place on a subsequent language switch.
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser


def test_toast_close_button_tooltip_translated(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    goto_after_i18n_init(page, mm_web_url, probe_key="common.close", lang="ko")

    page.evaluate("() => showToast('x')")

    expected_title = page.evaluate("() => t('common.close')")
    assert expected_title != "Close", "fixture must render the Korean locale"

    close_btn = page.locator(".toast-close")
    expect(close_btn).to_have_attribute("title", expected_title)


def test_empty_source_chunks_panel_translated_and_updates_on_lang_switch(
    page, mm_web_url: str
) -> None:
    install_default_stubs(page)
    page.route(
        "**/api/chunks?*",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"chunks": []}),
        ),
    )
    goto_after_i18n_init(page, mm_web_url, probe_key="chunks.none_found", lang="ko")

    page.evaluate(
        """() => {
            activateTab('search');
            hide(qs('detail-empty'));
            show(qs('detail-view'));
            show(qs('d-source-btn'));
            qs('d-file').textContent = 'test_source.md';
        }"""
    )

    page.locator("#d-source-btn").click()

    empty_state = page.locator("#source-chunks-list .empty-state")
    expect(empty_state).to_be_visible()

    expected_ko = page.evaluate("() => t('chunks.none_found')")
    assert expected_ko != "No chunks found", "fixture must render the Korean locale"
    expect(empty_state).to_have_text(expected_ko)

    page.evaluate("() => I18N.setLang('en')")

    expect(empty_state).to_be_visible()
    expect(empty_state).to_have_text("No chunks found")
