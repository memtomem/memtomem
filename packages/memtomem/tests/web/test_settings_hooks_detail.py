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

    page.locator('.hooks-rule-row[data-hook-key="PostToolUse:Edit"]').click()

    detail = page.locator("#hooks-rule-detail")
    detail.wait_for(state="visible", timeout=4_000)
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

    page.locator('.hooks-rule-row[data-hook-key="PreToolUse:Bash"]').click()

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

    page.locator('.hooks-rule-row[data-hook-key="PostToolUse:Edit"]').click()
    page.wait_for_function(
        "() => {"
        "  const d = document.getElementById('hooks-rule-detail');"
        "  return d && !d.hidden && (d.textContent || '').includes('PostToolUse');"
        "}",
        timeout=4_000,
    )

    page.locator('.hooks-rule-row[data-hook-key="PreToolUse:Bash"]').click()
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
