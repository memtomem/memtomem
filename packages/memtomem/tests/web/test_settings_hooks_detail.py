"""Browser tests for the Hooks per-rule detail panel (#962).

Pre-#962 the Hooks panel rendered each rule as a flat ``<div>`` row
with just the ``event:matcher`` label (and a ``<pre>``-block preview
for pending rules only). PR C makes every synced/pending rule
clickable: the click reveals a shared detail panel showing event /
matcher / command / type / timeout / raw rule JSON. Conflict cards
are intentionally untouched — their diff view IS the effective detail.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_PENDING_RULE = {
    "event": "PreToolUse",
    "matcher": "Bash",
    "rule": {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo pre-bash", "timeout": 5}],
    },
}

_SYNCED_RULE = {
    "event": "PostToolUse",
    "matcher": "Edit",
    "rule": {
        "matcher": "Edit",
        "hooks": [{"type": "command", "command": "echo post-edit"}],
    },
}

_MIXED_GET = {
    "status": "out_of_sync",
    "target_path": "/fake/.claude/settings.json",
    "target_scope": "user",
    "hooks": {
        "pending": [_PENDING_RULE],
        "synced": [_SYNCED_RULE],
        "conflicts": [],
    },
}


def _stub_settings_sync(page, payload: dict) -> None:
    def _handler(route):
        if route.request.method == "GET":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/settings-sync", _handler)


def _open_hooks_sync(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('hooks-sync')")
    page.wait_for_function(
        "() => document.querySelectorAll('#hooks-sync-content .hooks-rule-row').length > 0",
        timeout=5_000,
    )


def test_clicking_synced_rule_opens_detail_panel(page, mm_web_url: str) -> None:
    """Click a synced rule → shared #hooks-rule-detail panel reveals
    event/matcher/command and the raw rule JSON.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _MIXED_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    # The visible label is ``PostToolUse:Edit`` but ``data-hook-key`` is
    # now an index (#962 review fold — duplicate (event, matcher) rules
    # would collapse on label keys). Locate by the synced bucket class.
    page.locator(".hooks-rule-row.hooks-rule-row--synced").first.click()

    detail = page.locator("#hooks-rule-detail")
    detail.wait_for(state="visible", timeout=4_000)
    assert detail.locator(".hooks-rule-detail-header").count() == 1
    assert detail.locator(".hooks-rule-detail-inner").count() == 1
    text = detail.text_content() or ""
    assert "PostToolUse" in text, f"event missing from detail panel, got {text!r}"
    assert "Edit" in text, f"matcher missing from detail panel, got {text!r}"
    assert "echo post-edit" in text, f"command missing from detail panel, got {text!r}"

    # Raw rule JSON block must also appear.
    json_block = detail.locator(".hooks-rule-detail-json")
    assert json_block.count() == 1, "Raw rule JSON block must render alongside the labeled fields"
    json_text = json_block.text_content() or ""
    assert '"matcher"' in json_text, f"rule JSON must include the matcher key, got {json_text!r}"


def test_clicking_pending_rule_opens_same_detail_panel(page, mm_web_url: str) -> None:
    """Pending rules use the same per-rule detail panel — symmetry pin
    so future drift between synced/pending row rendering can't leave one
    bucket without the detail affordance.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _MIXED_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    # Pending row sits in the Pending section above the Synced bucket.
    page.locator(".hooks-rule-row:not(.hooks-rule-row--synced)").first.click()

    detail = page.locator("#hooks-rule-detail")
    detail.wait_for(state="visible", timeout=4_000)
    text = detail.text_content() or ""
    assert "PreToolUse" in text, f"event missing, got {text!r}"
    assert "Bash" in text, f"matcher missing, got {text!r}"
    assert "echo pre-bash" in text, f"command missing, got {text!r}"
    # Timeout was set on this rule; should surface as its own row.
    assert "5" in text, f"timeout value 5 missing, got {text!r}"


def test_detail_panel_swaps_content_on_second_click(page, mm_web_url: str) -> None:
    """Single panel — clicking a different rule overwrites the previous
    detail content. Negative pin so a regression that keeps stale
    content (e.g. by appending instead of replacing) is caught.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _MIXED_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator(".hooks-rule-row.hooks-rule-row--synced").first.click()
    page.wait_for_function(
        "() => {"
        "  const d = document.getElementById('hooks-rule-detail');"
        "  return d && !d.hidden && (d.textContent || '').includes('PostToolUse');"
        "}",
        timeout=4_000,
    )

    page.locator(".hooks-rule-row:not(.hooks-rule-row--synced)").first.click()
    page.wait_for_function(
        "() => {"
        "  const d = document.getElementById('hooks-rule-detail');"
        "  return d && (d.textContent || '').includes('PreToolUse');"
        "}",
        timeout=4_000,
    )

    detail_text = page.locator("#hooks-rule-detail").text_content() or ""
    # The new content is for PreToolUse:Bash — the old PostToolUse value
    # should be gone (the JSON-rule pretty-print also contains the
    # event, so we anchor on the command body which is the unambiguous
    # discriminator).
    assert "echo pre-bash" in detail_text, (
        f"detail panel did not swap to the new rule, got {detail_text!r}"
    )
    assert "echo post-edit" not in detail_text, (
        f"detail panel kept stale content from the previous rule, got {detail_text!r}"
    )


def test_duplicate_event_matcher_rules_keep_distinct_detail(page, mm_web_url: str) -> None:
    """Regression pin: Claude Code allows two rules to share the same
    ``(event, matcher)`` pair, and the server preserves multiplicity
    (``settings_sync.py:128`` PR #844 fix). The per-rule detail panel
    must therefore key on a stable per-row id, not on ``event:matcher``
    — otherwise both rows resolve to the last rule's detail and the
    first rule's command body is silently lost. Pre-fix, both rows
    shared ``data-hook-key="PreToolUse:Bash"`` and clicking either
    showed only ``echo dup-second``.
    """
    install_default_stubs(page)

    payload = {
        "status": "in_sync",
        "target_path": "/fake/.claude/settings.json",
        "target_scope": "user",
        "hooks": {
            "pending": [],
            "conflicts": [],
            "synced": [
                {
                    "event": "PreToolUse",
                    "matcher": "Bash",
                    "rule": {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo dup-first"}],
                    },
                },
                {
                    "event": "PreToolUse",
                    "matcher": "Bash",
                    "rule": {
                        "matcher": "Bash",
                        "hooks": [{"type": "command", "command": "echo dup-second"}],
                    },
                },
            ],
        },
    }
    _stub_settings_sync(page, payload)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    rows = page.locator(".hooks-rule-row[data-hook-key]")
    assert rows.count() == 2, (
        f"Two duplicate-matcher rows must render distinct elements, got count = {rows.count()}"
    )
    keys = [rows.nth(i).get_attribute("data-hook-key") or "" for i in range(rows.count())]
    assert len(set(keys)) == 2, (
        f"Duplicate-matcher rows must carry distinct data-hook-key values, got {keys!r}"
    )

    # Click the FIRST row → detail must show the first rule's command body,
    # not the second's. Without the fix the registry would have overwritten
    # the first entry and both clicks would render ``echo dup-second``.
    rows.nth(0).click()
    detail = page.locator("#hooks-rule-detail")
    detail.wait_for(state="visible", timeout=4_000)
    first_text = detail.text_content() or ""
    assert "echo dup-first" in first_text, (
        f"First duplicate-matcher row must render its own command body, got {first_text!r}"
    )
    assert "echo dup-second" not in first_text, (
        f"First row must NOT bleed the second row's command body into its detail, "
        f"got {first_text!r}"
    )

    # Symmetric: click the SECOND row → distinct content.
    rows.nth(1).click()
    page.wait_for_function(
        "() => {"
        "  const d = document.getElementById('hooks-rule-detail');"
        "  return d && (d.textContent || '').includes('dup-second');"
        "}",
        timeout=4_000,
    )
    second_text = detail.text_content() or ""
    assert "echo dup-second" in second_text, (
        f"Second duplicate-matcher row must render its own command body, got {second_text!r}"
    )
    assert "echo dup-first" not in second_text, (
        f"Second row must NOT leak the first row's command body, got {second_text!r}"
    )
