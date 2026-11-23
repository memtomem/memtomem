"""Browser test for namespace action labels across a live locale switch (#2851)."""

from __future__ import annotations

import pytest

from .conftest import goto_after_i18n_init
from .test_ns_edit_color_preserved import _install_stubs
from .test_scratch_and_ns_color_escape import _open_settings_section
from .test_sessions_panel_e2e import _dev_web_server

pytestmark = pytest.mark.browser


def test_namespace_actions_translate_without_losing_edit_values(page) -> None:
    _install_stubs(page, [])

    with _dev_web_server() as base_url:
        goto_after_i18n_init(page, base_url, probe_key="common.delete", lang="ko")
        _open_settings_section(page, "namespaces")
        card = page.locator("#ns-list .ns-card").first
        card.wait_for(timeout=5_000)

        delete_button = card.locator(".btn-danger")
        assert delete_button.inner_text() == "삭제"

        card.locator(".ns-actions button").filter(has_text="Edit").click()
        form = card.locator(".ns-edit-form")
        form.wait_for(timeout=5_000)
        cancel_button = form.locator(".ns-edit-cancel")
        save_button = form.locator(".ns-edit-save")
        assert cancel_button.inner_text() == "취소"
        assert save_button.inner_text() == "저장"

        description = form.locator(".ns-edit-desc")
        color = form.locator(".ns-edit-color")
        description.fill("한국어 설명")
        color.fill("#123456")

        page.evaluate("async () => { await I18N.setLang('en'); }")

        assert delete_button.inner_text() == "Delete"
        assert cancel_button.inner_text() == "Cancel"
        assert save_button.inner_text() == "Save"
        assert description.input_value() == "한국어 설명"
        assert color.input_value() == "#123456"
