"""The Search tab's context-window "Off" option follows the UI language.

``_updateContextWindowLabel`` (``settings-config.js``) owns the text of the
``value="0"`` option: "Off", or "Config (N↑M↓)" when the server config enables
a context window. It used to write English literals over the ``data-i18n``
translation, and ``applyDOM`` on a language switch then wrote "Off" back over
the "Config (…)" label.
"""

from __future__ import annotations

import json

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_OFF_OPTION = "#context-window option[value='0']"


def _config(enabled: bool) -> dict:
    return {
        "search": {"default_top_k": 10},
        "context": {"enabled": enabled, "window_before": 2, "window_after": 1},
    }


def _stub_config(page, payload: dict) -> None:
    page.route(
        "**/api/config",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(payload)
        ),
    )


def _boot(page, mm_web_url: str, *, enabled: bool) -> None:
    install_default_stubs(page)
    _stub_config(page, _config(enabled=enabled))
    goto_after_i18n_init(page, mm_web_url, lang="ko", probe_key="search.context_config")
    page.wait_for_function("() => !!STATE.serverConfig")


def _label(page) -> str:
    return page.evaluate(f'() => document.querySelector("{_OFF_OPTION}").textContent')


def test_off_label_is_translated_after_a_config_resync(page, mm_web_url: str) -> None:
    # At boot ``applyDOM`` runs last and happens to leave the translation in
    # place; any later ``_syncConfigToUI`` (config save, reload) used to write
    # the English literal back.
    _boot(page, mm_web_url, enabled=False)
    assert _label(page) == "\ub044\uae30"  # 끄기

    page.evaluate("() => _syncConfigToUI()")
    assert _label(page) == "\ub044\uae30"

    page.evaluate("() => I18N.setLang('en')")
    assert _label(page) == "Off"


def test_config_label_is_translated_and_survives_a_switch(page, mm_web_url: str) -> None:
    # ``applyDOM`` used to replace the JS-owned "Config (…)" label with the
    # ``data-i18n`` "Off" translation on boot and on every language switch.
    _boot(page, mm_web_url, enabled=True)
    assert _label(page) == "\uc124\uc815\uac12 (2\u21911\u2193)"  # 설정값 (2↑1↓)

    page.evaluate("() => I18N.setLang('en')")
    assert _label(page) == "Config (2\u21911\u2193)"

    page.evaluate("() => I18N.setLang('ko')")
    assert _label(page) == "\uc124\uc815\uac12 (2\u21911\u2193)"
