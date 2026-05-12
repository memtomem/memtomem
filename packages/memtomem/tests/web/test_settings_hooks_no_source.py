"""Browser tests for the Hooks panel ``no_source`` state polish (PR D).

Pre-fix the panel rendered (1) the ``User-scope target:`` line — even
though the canonical file does not exist — and (2) left Sync Now
enabled, inviting a click that the server would reject. The badge
already names the condition ("No .memtomem/settings.json found"), so
the target line is misleading clutter and the button is a footgun.

Tests pin the post-fix behavior:

* The target line is NOT rendered in ``no_source`` state.
* Sync Now is ``disabled`` with the no-source tooltip.
* The empty-state hint still renders under the badge so the user has
  an actionable next step (run ``mm init``).
* Symmetric positive pin: in other states (``in_sync`` / ``out_of_sync``)
  the target line shows and the button is enabled.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_NO_SOURCE_GET = {
    "status": "no_source",
    "target_path": "/fake/home/.claude/settings.json",
    "target_scope": "user",
    "hooks": {"pending": [], "conflicts": [], "synced": []},
    "canonical_path": "/fake/project/.memtomem/settings.json",
    "duplicate_tier_warnings": [],
}

_IN_SYNC_GET = {
    "status": "in_sync",
    "target_path": "/fake/home/.claude/settings.json",
    "target_scope": "user",
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
    "duplicate_tier_warnings": [],
}


def _stub_settings_sync(page, payload: dict) -> None:
    page.route(
        "**/api/settings-sync",
        lambda r: (
            r.fulfill(status=200, content_type="application/json", body=json.dumps(payload))
            if r.request.method == "GET"
            else r.fulfill(status=200, content_type="application/json", body="{}")
        ),
    )


def _open_hooks_sync(page) -> None:
    page.evaluate("() => activateTab('context-gateway')")
    page.evaluate("() => switchSettingsSection('hooks-sync')")
    # Wait until the badge mounts — the rest of the assertions depend on
    # ``loadHooksSync`` having run.
    page.wait_for_function(
        "() => {"
        "  const badge = document.querySelector('#hooks-sync-status .badge');"
        "  return badge && (badge.textContent || '').trim().length > 0;"
        "}",
        timeout=5_000,
    )


def test_no_source_state_suppresses_target_line(page, mm_web_url: str) -> None:
    """``no_source`` state must not render ``hooks-status-target`` —
    the canonical file does not exist so the target path is irrelevant
    clutter. The badge ("No .memtomem/settings.json found") already
    names the condition.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_SOURCE_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    target = page.locator("#hooks-sync-status .hooks-status-target")
    assert target.count() == 0, (
        f"no_source state must not render the target line; got {target.count()} occurrence(s)"
    )

    # The empty-state hint still renders so the user has an actionable
    # next step (``mm init``). Pin the hint copy as a positive check
    # that the rest of the empty-state path didn't regress.
    content = page.locator("#hooks-sync-content").text_content() or ""
    assert "mm init" in content, (
        f"no_source hint must guide the user to ``mm init``; got {content!r}"
    )


def test_no_source_state_disables_sync_now_button(page, mm_web_url: str) -> None:
    """Sync Now must be ``disabled`` in ``no_source`` so a click can't
    fire a POST that the server can never satisfy. The disabled tooltip
    explains the gate.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_SOURCE_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    btn = page.locator("#hooks-sync-btn")
    btn.wait_for(state="attached", timeout=4_000)
    assert btn.is_disabled(), "Sync Now button must be disabled when status=no_source"
    no_source_attr = btn.get_attribute("data-no-source")
    assert no_source_attr == "true", (
        f"data-no-source attribute must pin the gate, got {no_source_attr!r}"
    )
    title = btn.get_attribute("title") or ""
    assert "mm init" in title or "create" in title.lower(), (
        f"Disabled tooltip must hint at the remedy, got {title!r}"
    )


def test_in_sync_state_restores_target_line_and_enabled_button(page, mm_web_url: str) -> None:
    """Symmetric positive pin: once a canonical exists, the target line
    is back and Sync Now is enabled. Catches a regression where the
    disable / suppress branches accidentally apply to every state.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _IN_SYNC_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    target = page.locator("#hooks-sync-status .hooks-status-target")
    target.wait_for(state="attached", timeout=4_000)
    assert "User-scope target:" in (target.text_content() or ""), (
        "in_sync state must render the User-scope target line"
    )

    btn = page.locator("#hooks-sync-btn")
    assert not btn.is_disabled(), "Sync Now button must be enabled when a canonical exists"
    no_source_attr = btn.get_attribute("data-no-source")
    assert no_source_attr is None, (
        f"data-no-source attribute must be cleared in non-no_source states, got {no_source_attr!r}"
    )
