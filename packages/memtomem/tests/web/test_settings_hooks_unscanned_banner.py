"""Browser tests for the Hooks panel's unverified-settings banner (#2644).

``GET /api/settings-sync`` carries ``unscanned_settings``: the settings files
that exist but no check could read. The panel shows one row per file in
``#hooks-unscanned-banner``, clears it when a reload starts (so a reload that
fails does not leave the previous project's rows), and repaints it from the
cached payload on a language switch only while it is visible.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import memtomem

from .conftest import goto_after_i18n_init, install_default_stubs

pytestmark = pytest.mark.browser


_ROWS = [
    {
        "source": "tier",
        "tier": "project_local",
        "path": ".claude/<b>settings.local.json",
        "reason": "invalid JSON",
    },
    {
        "source": "canonical",
        "tier": None,
        "path": ".memtomem/settings.json",
        "reason": "not a JSON object",
    },
]

_GET = {
    "status": "in_sync",
    "target_path": "~/.claude/settings.json",
    "target_scope": "user",
    "target_mtime_ns": "100",
    "canonical_mtime_ns": "200",
    "hooks": {
        "pending": [],
        "conflicts": [],
        "synced": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule": {"hooks": [{"type": "command", "command": "echo hi"}]},
            }
        ],
    },
    "canonical_path": ".memtomem/settings.json",
    "duplicate_tier_warnings": [],
    "unscanned_settings": _ROWS,
}


def _stub(page, state: dict) -> None:
    """GET answers from ``state`` at request time, so a test can make the
    next reload fail."""

    def _route(route) -> None:
        if route.request.method != "GET":
            route.fulfill(status=200, content_type="application/json", body="{}")
        elif state.get("fail"):
            route.fulfill(status=500, content_type="application/json", body='{"detail": "boom"}')
        else:
            route.fulfill(
                status=200, content_type="application/json", body=json.dumps(state["payload"])
            )

    page.route("**/api/settings-sync**", _route)


def _goto(page, url: str) -> None:
    """English, and only once boot (locale load, hash handling) has finished,
    so the assertions read the localized rows rather than a boot-time race."""
    goto_after_i18n_init(page, url, lang="en", probe_key="settings.hooks.unscanned_target_row")


def _open(page) -> None:
    page.evaluate("() => activateTab('context-gateway')")
    page.evaluate("() => switchSettingsSection('hooks-sync')")
    page.wait_for_function(
        "() => {"
        "  const badge = document.querySelector('#hooks-sync-status .badge');"
        "  return badge && (badge.textContent || '').trim().length > 0;"
        "}",
        timeout=5_000,
    )


def _banner_state(page) -> dict:
    return page.evaluate(
        "() => { const el = document.getElementById('hooks-unscanned-banner');"
        " return { hidden: el.hidden,"
        "  rows: [...el.querySelectorAll('.hooks-duplicate-banner-row')].map(r => r.textContent),"
        "  bold: el.querySelectorAll('b').length }; }"
    )


def test_rows_render_escaped(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub(page, {"payload": _GET})
    _goto(page, mm_web_url)
    _open(page)

    state = _banner_state(page)
    assert state["hidden"] is False, state
    assert state["rows"] == [
        "project_local target file (.claude/<b>settings.local.json) was not checked: "
        "invalid JSON. Hook duplicates, matchers and commands in it are unverified.",
        "Canonical settings file (.memtomem/settings.json) was not checked: "
        "not a JSON object. Hook duplicates, matchers and commands in it are unverified.",
    ], state
    # The planted tag is text, not markup.
    assert state["bold"] == 0, state


def test_no_rows_leaves_the_banner_hidden(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub(page, {"payload": {**_GET, "unscanned_settings": []}})
    _goto(page, mm_web_url)
    _open(page)

    state = _banner_state(page)
    assert state == {"hidden": True, "rows": [], "bold": 0}, state


def test_a_payload_without_the_field_leaves_the_banner_hidden(page, mm_web_url: str) -> None:
    payload = {k: v for k, v in _GET.items() if k != "unscanned_settings"}
    install_default_stubs(page)
    _stub(page, {"payload": payload})
    _goto(page, mm_web_url)
    _open(page)

    assert _banner_state(page)["hidden"] is True


def test_a_failed_reload_clears_the_banner_and_a_language_switch_keeps_it_cleared(
    page, mm_web_url: str
) -> None:
    state = {"payload": _GET}
    install_default_stubs(page)
    _stub(page, state)
    _goto(page, mm_web_url)
    _open(page)
    assert _banner_state(page)["hidden"] is False

    state["fail"] = True
    page.evaluate("async () => { await loadHooksSync(); }")
    assert _banner_state(page) == {"hidden": True, "rows": [], "bold": 0}

    # The cached payload still holds the old rows; the repaint is guarded on
    # the banner being visible, so it must not bring them back.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    assert _banner_state(page)["hidden"] is True


def test_a_visible_banner_repaints_in_the_new_language(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub(page, {"payload": _GET})
    _goto(page, mm_web_url)
    _open(page)
    assert _banner_state(page)["hidden"] is False

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => { const el = document.getElementById('hooks-unscanned-banner');"
        " return el && el.textContent.includes('확인하지 못했습니다'); }",
        timeout=3_000,
    )
    rows = _banner_state(page)["rows"]
    assert len(rows) == 2, rows
    assert rows[0].startswith("project_local 대상 파일(.claude/<b>settings.local.json)"), rows
    assert rows[1].startswith("공유 훅 설정 파일(.memtomem/settings.json)"), rows


def test_cold_boot_renders_the_english_fallback(page, mm_web_url: str) -> None:
    """With the locale files held back, ``t()`` echoes the key and the banner
    falls back to its English literal rather than showing the raw key."""
    install_default_stubs(page)
    page.route("**/locales/**", lambda r: r.fulfill(status=404, body=""))
    _stub(page, {"payload": _GET})
    page.add_init_script("try { localStorage.setItem('m2m-lang', 'en'); } catch (e) {}")
    page.goto(mm_web_url)
    page.wait_for_function("() => document.documentElement.dataset.booted === 'true'")
    assert page.evaluate("() => t('settings.hooks.unscanned_target_row')") == (
        "settings.hooks.unscanned_target_row"
    )
    _open(page)

    assert _banner_state(page)["rows"] == [
        "project_local target file (.claude/<b>settings.local.json) was not checked: "
        "invalid JSON. Hook duplicates, matchers and commands in it are unverified.",
        "Canonical settings file (.memtomem/settings.json) was not checked: "
        "not a JSON object. Hook duplicates, matchers and commands in it are unverified.",
    ]


def test_no_field_and_no_template_is_read_as_markup(page, mm_web_url: str) -> None:
    """Markup planted in every field and in the locale template itself stays
    text: the banner sets ``textContent``, so a change that rendered any of
    them as HTML would grow elements here."""
    locale_path = Path(memtomem.__file__).parent / "web" / "static" / "locales" / "en.json"
    en = json.loads(locale_path.read_text(encoding="utf-8"))
    en["settings.hooks.unscanned_target_row"] = "<s>{tier}</s> {path}: {reason}"
    page.route(
        "**/locales/en.json",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(en)),
    )
    row = {
        "source": "tier",
        "tier": "<i>project_local</i>",
        "path": "<b>.claude/settings.local.json",
        "reason": "<u>invalid JSON</u>",
    }
    install_default_stubs(page)
    _stub(page, {"payload": {**_GET, "unscanned_settings": [row]}})
    _goto(page, mm_web_url)
    _open(page)

    counts = page.evaluate(
        "() => { const el = document.getElementById('hooks-unscanned-banner');"
        " return ['s', 'i', 'b', 'u'].map(tag => el.querySelectorAll(tag).length); }"
    )
    assert counts == [0, 0, 0, 0], counts
    assert _banner_state(page)["rows"] == [
        "<s><i>project_local</i></s> <b>.claude/settings.local.json: <u>invalid JSON</u>"
    ]
