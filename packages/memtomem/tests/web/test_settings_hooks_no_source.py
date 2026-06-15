"""Browser tests for the Hooks panel ``no_source`` state polish (PR D).

Pre-fix the panel rendered (1) the ``User target:`` line — even
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
    "target_mtime_ns": "100",
    "canonical_mtime_ns": None,
    "hooks": {"pending": [], "conflicts": [], "synced": []},
    "canonical_path": "/fake/project/.memtomem/settings.json",
    "duplicate_tier_warnings": [],
}

_NO_HOOKS_GET = {
    "status": "no_hooks",
    "target_path": "/fake/home/.claude/settings.json",
    "target_scope": "user",
    "target_mtime_ns": "100",
    "canonical_mtime_ns": "200",
    "hooks": {"pending": [], "conflicts": [], "synced": []},
    "target_hooks": {"configured": [], "target_only": []},
    "canonical_path": "/fake/project/.memtomem/settings.json",
    "duplicate_tier_warnings": [],
}

_NO_HOOKS_WITH_TARGET_GET = {
    "status": "no_hooks",
    "target_path": "/fake/home/.claude/settings.json",
    "target_scope": "user",
    "target_mtime_ns": "100",
    "canonical_mtime_ns": "200",
    "hooks": {"pending": [], "conflicts": [], "synced": []},
    "target_hooks": {
        "configured": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule_index": 0,
                "rule_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "rule": {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo target-only"}],
                },
            }
        ],
        "target_only": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule_index": 0,
                "rule_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "rule": {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo target-only"}],
                },
            }
        ],
    },
    "canonical_path": "/fake/project/.memtomem/settings.json",
    "duplicate_tier_warnings": [],
}

_NO_SOURCE_WITH_TARGET_GET = {
    **_NO_HOOKS_WITH_TARGET_GET,
    "status": "no_source",
    "canonical_mtime_ns": None,
}

_NO_SOURCE_WITH_TWO_TARGET_GET = {
    **_NO_SOURCE_WITH_TARGET_GET,
    "target_hooks": {
        "configured": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule_index": 0,
                "rule_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "rule": {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo target-a"}],
                },
            },
            {
                "event": "PostToolUse",
                "matcher": "Write",
                "rule_index": 0,
                "rule_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "rule": {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "echo target-b"}],
                },
            },
        ],
        "target_only": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule_index": 0,
                "rule_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "rule": {
                    "matcher": "Bash",
                    "hooks": [{"type": "command", "command": "echo target-a"}],
                },
            },
            {
                "event": "PostToolUse",
                "matcher": "Write",
                "rule_index": 0,
                "rule_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "rule": {
                    "matcher": "Write",
                    "hooks": [{"type": "command", "command": "echo target-b"}],
                },
            },
        ],
    },
}

_IN_SYNC_GET = {
    "status": "in_sync",
    "target_path": "/fake/home/.claude/settings.json",
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


def _stub_hooks_api(page, payload: dict, *, promote=None, delete=None) -> None:
    def _ok(route, body: dict) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def _api(route):
        url = route.request.url
        if "/api/settings-sync" in url and route.request.method == "GET":
            _ok(route, payload)
        elif "/api/context/settings/rules/promote" in url and promote is not None:
            promote(route)
        elif "/api/context/settings/rules/delete" in url and delete is not None:
            delete(route)
        elif "/api/system/ui-mode" in url:
            _ok(route, {"mode": "prod"})
        elif "/api/system/model-readiness" in url:
            _ok(route, {"ready": True})
        elif "/api/sources" in url:
            _ok(route, {"sources": []})
        elif "/api/namespaces" in url:
            _ok(route, {"namespaces": []})
        elif "/api/privacy/patterns" in url:
            _ok(route, {"patterns": []})
        else:
            _ok(route, {})

    page.route("**/api/**", _api)


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


def test_no_hooks_state_is_not_rendered_as_in_sync(page, mm_web_url: str) -> None:
    """An empty canonical settings file should explain that no hook rules
    exist instead of saying every hook is synced.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_HOOKS_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    badge = page.locator("#hooks-sync-status .badge")
    badge.wait_for(state="attached", timeout=4_000)
    assert "No hooks defined" in (badge.text_content() or "")

    content = page.locator("#hooks-sync-content").text_content() or ""
    assert "pending or synced" in content

    btn = page.locator("#hooks-sync-btn")
    assert btn.is_disabled(), "Sync Now button must be disabled when status=no_hooks"
    assert btn.get_attribute("data-no-hooks") == "true"
    assert btn.get_attribute("data-no-source") is None


def test_no_hooks_state_lists_actual_target_hooks(page, mm_web_url: str) -> None:
    """Even when canonical has no hooks, target settings hooks should be
    visible as actual configured hooks.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_HOOKS_WITH_TARGET_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    content = page.locator("#hooks-sync-content").text_content() or ""
    assert "Configured in target" in content
    assert "PreToolUse:Bash" in content
    assert "target-only" in content

    page.locator(".hooks-rule-row--configured").first.click()
    detail = page.locator("#hooks-rule-detail")
    detail.wait_for(state="visible", timeout=4_000)
    detail_text = detail.text_content() or ""
    assert "echo target-only" in detail_text


def test_no_source_state_lists_actual_target_hooks(page, mm_web_url: str) -> None:
    """A missing canonical source should not hide hooks already configured
    in the target settings file.
    """
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_SOURCE_WITH_TARGET_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    content = page.locator("#hooks-sync-content").text_content() or ""
    assert "Configured in target" in content
    assert "PreToolUse:Bash" in content


def test_target_hook_detail_renders_delete_and_promote_actions(page, mm_web_url: str) -> None:
    """Target-configured rows expose v1 actions and the edit-unavailable hint."""
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_SOURCE_WITH_TARGET_GET)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator(".hooks-rule-row--configured").first.click()
    detail = page.locator("#hooks-rule-detail")
    detail.wait_for(state="visible", timeout=4_000)
    assert detail.locator(".hooks-rule-delete-btn").count() == 1
    assert detail.locator(".hooks-rule-promote-btn").count() == 1
    detail_text = detail.text_content() or ""
    assert "Direct editing is not available" in detail_text
    assert "/fake/home/.claude/settings.json" in detail_text


def test_delete_target_hook_posts_exact_rule_identity(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_settings_sync(page, _NO_SOURCE_WITH_TARGET_GET)
    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator(".hooks-rule-row--configured").first.click()
    page.locator(".hooks-rule-delete-btn").click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden")
    with page.expect_request(
        lambda req: "/api/context/settings/rules/delete" in req.url and req.method == "POST",
        timeout=4_000,
    ) as delete_info:
        page.locator("#confirm-ok-btn").click()

    body = delete_info.value.post_data_json
    assert body["event"] == "PreToolUse"
    assert body["matcher"] == "Bash"
    assert body["rule_index"] == 0
    assert body["rule_hash"] == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    assert body["target_mtime_ns"] == "100"
    assert body["canonical_mtime_ns"] is None


def test_promote_private_target_hook_confirms_before_post(page, mm_web_url: str) -> None:
    install_default_stubs(page)

    def _promote(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "ok", "reason": "promoted"}),
        )

    _stub_hooks_api(page, _NO_SOURCE_WITH_TARGET_GET, promote=_promote)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator(".hooks-rule-row--configured").first.click()
    page.locator(".hooks-rule-promote-btn").click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden")
    assert ".memtomem/settings.json" in (page.locator("#confirm-message").text_content() or "")
    with page.expect_request(
        lambda req: "/api/context/settings/rules/promote" in req.url and req.method == "POST",
        timeout=4_000,
    ) as promote_info:
        page.locator("#confirm-ok-btn").click()

    assert promote_info.value.post_data_json["confirm_private_to_shared"] is True


def test_promote_with_remove_original_posts_delete(page, mm_web_url: str) -> None:
    install_default_stubs(page)

    def _promote(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "ok", "reason": "promoted"}),
        )

    def _delete(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "ok", "reason": "deleted"}),
        )

    _stub_hooks_api(page, _NO_SOURCE_WITH_TARGET_GET, promote=_promote, delete=_delete)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator(".hooks-rule-row--configured").first.click()
    page.locator(".hooks-rule-promote-btn").click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden")
    page.locator("#confirm-extra-checkbox").check()
    with (
        page.expect_request(
            lambda req: "/api/context/settings/rules/promote" in req.url and req.method == "POST",
            timeout=4_000,
        ),
        page.expect_request(
            lambda req: "/api/context/settings/rules/delete" in req.url and req.method == "POST",
            timeout=4_000,
        ) as delete_info,
    ):
        page.locator("#confirm-ok-btn").click()

    assert (
        delete_info.value.post_data_json["rule_hash"]
        == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    )


def test_bulk_promote_target_only_hooks(page, mm_web_url: str) -> None:
    install_default_stubs(page)

    def _promote(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "ok", "reason": "promoted"}),
        )

    _stub_hooks_api(page, _NO_SOURCE_WITH_TWO_TARGET_GET, promote=_promote)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    bulk = page.locator(".hooks-promote-all-btn")
    bulk.wait_for(state="attached", timeout=4_000)
    assert "Save all to memtomem" in (bulk.text_content() or "")
    bulk.click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden")
    with (
        page.expect_request(
            lambda req: (
                "/api/context/settings/rules/promote" in req.url
                and (req.post_data_json or {}).get("rule_hash")
                == "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
            ),
            timeout=4_000,
        ),
        page.expect_request(
            lambda req: (
                "/api/context/settings/rules/promote" in req.url
                and (req.post_data_json or {}).get("rule_hash")
                == "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
            ),
            timeout=4_000,
        ),
    ):
        page.locator("#confirm-ok-btn").click()


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
    assert "User target:" in (target.text_content() or ""), (
        "in_sync state must render the User target line"
    )

    btn = page.locator("#hooks-sync-btn")
    assert not btn.is_disabled(), "Sync Now button must be enabled when a canonical exists"
    no_source_attr = btn.get_attribute("data-no-source")
    assert no_source_attr is None, (
        f"data-no-source attribute must be cleared in non-no_source states, got {no_source_attr!r}"
    )
