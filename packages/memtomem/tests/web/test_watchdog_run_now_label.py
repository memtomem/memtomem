"""Browser test: the watchdog Run Now button keeps its translated label.

``runWatchdogNow`` swaps the button text to a busy label while the run is in
flight and restores it in ``finally``. The restore must use the
``settings.watchdog.run_btn`` key: the button's ``data-i18n`` binding is only
re-applied on a language switch, so a literal English restore would leave a
Korean UI showing "Run Now" after a single click.
"""

from __future__ import annotations

import json

import pytest

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser

_STATUS = {
    "enabled": True,
    "checks": {"db_size": {"status": "ok", "value": {}, "tier": "periodic"}},
}


def _ok(route, payload: dict) -> None:
    route.fulfill(status=200, content_type="application/json", body=json.dumps(payload))


def _open_health_with_watchdog(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "dev"}))
    page.route("**/api/watchdog/status", lambda r: _ok(r, _STATUS))
    page.route("**/api/watchdog/run", lambda r: _ok(r, {}))
    goto_after_i18n_init(page, mm_web_url, probe_key="settings.watchdog.run_btn", lang="ko")
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('harness-health')")
    page.wait_for_function(
        "() => document.querySelector('#health-watchdog-report .health-grid') !== null",
        timeout=5_000,
    )


def test_run_now_label_stays_translated_after_a_run(page, mm_web_url: str) -> None:
    _open_health_with_watchdog(page, mm_web_url)
    expected = page.evaluate("() => t('settings.watchdog.run_btn')")
    assert expected != "Run Now", "fixture must render the Korean locale"

    btn = page.locator("#health-watchdog-run-btn")
    assert (btn.text_content() or "").strip() == expected

    # ``click`` disables the button synchronously, before the POST; it is
    # re-enabled only in ``finally``, after the status reload settles.
    with page.expect_response("**/api/watchdog/run"):
        btn.click()
    page.wait_for_function(
        "() => !document.getElementById('health-watchdog-run-btn').disabled",
        timeout=5_000,
    )

    assert (btn.text_content() or "").strip() == expected


def test_run_now_success_message_stays_visible_after_the_reload(page, mm_web_url: str) -> None:
    """The run reloads the report, and that reload hides the status bar as it
    starts; the success message has to be shown after it, not before (#2988)."""
    _open_health_with_watchdog(page, mm_web_url)

    with page.expect_response("**/api/watchdog/run"):
        page.locator("#health-watchdog-run-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('health-watchdog-run-btn').disabled",
        timeout=5_000,
    )

    bar = page.evaluate(
        """() => {
            const el = document.getElementById('health-watchdog-status-bar');
            return {display: el.style.display, cls: el.className, text: el.textContent};
        }"""
    )
    assert bar["display"] == "block", bar
    assert bar["cls"] == "status-msg ok", bar
    assert bar["text"].strip(), bar
