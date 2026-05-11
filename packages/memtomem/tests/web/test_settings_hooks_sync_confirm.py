"""Browser tests for the Hooks Sync confirm flow + status badge transition.

Audit goal (``scripts/context-gateway-review-plan.md`` 갭 3): the CLI's
``_confirm_settings_host_writes`` (``cli/context_cmd.py:332``) is the trust
gate for user-scope ``~/.claude/settings.json`` writes; the browser's
equivalent is the confirm modal in front of the Sync Now button. Without
it, the Web UI bypasses the host-write gate the audit P0 was designed to
enforce. This spec pins the modal + the badge transition that follows a
successful sync.

The handler's contract (``static/settings-hooks-watchdog.js:158-188``):

* ``showConfirm`` first; cancel returns without firing the POST.
* On confirm, ``POST /api/settings-sync`` (with CSRF if available).
* The POST response is parsed for ``data.results[].warnings`` only —
  ``status`` is **not** read from the POST body.
* After the POST, ``loadHooksSync()`` (line 184) fires a fresh
  ``GET /api/settings-sync`` and the badge is rendered from
  ``data.status`` of the GET response (line 36-43).

Stub sequence: GETs return ``out_of_sync`` until the POST flips a state
flag, then return ``in_sync``. State-based switching (rather than a
GET-call counter) is robust against cold mount firing
``loadHooksSync`` more than once — see ``_stub_settings_sync`` below.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# Locale-pinned EN strings (default locale).
HOOKS_SYNC_TITLE = "Sync settings"  # confirm.hooks_sync_title
HOOKS_IN_SYNC_BADGE = "All hooks are in sync"  # settings.hooks.in_sync


_OUT_OF_SYNC_GET = {
    "status": "out_of_sync",
    "target_path": "/fake/.claude/settings.json",
    "hooks": {
        "pending": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule": {"hooks": [{"type": "command", "command": "echo hi"}]},
            }
        ],
        "conflicts": [],
        "synced": [],
    },
}

_IN_SYNC_GET = {
    "status": "in_sync",
    "target_path": "/fake/.claude/settings.json",
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
}


def _open_hooks_sync(page) -> None:
    """Activate the Settings tab + Hooks Sync sub-section so
    ``loadHooksSync`` runs (see ``app.js:1205``).
    """
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('hooks-sync')")
    # The badge populates inside ``#hooks-sync-status`` once the cold-mount
    # GET resolves. Wait for the badge text to be non-empty rather than
    # for visibility — other settings sub-sections also exist in the DOM
    # so a visibility check is fragile.
    page.wait_for_function(
        "() => {"
        "  const status = document.getElementById('hooks-sync-status');"
        "  if (!status) return false;"
        "  const badge = status.querySelector('.badge');"
        "  return badge && (badge.textContent || '').trim().length > 0;"
        "}",
        timeout=5_000,
    )


def _stub_settings_sync(
    page, before_post: dict, after_post: dict | None = None, post_handler=None
) -> dict:
    """Single handler for ``/api/settings-sync`` that dispatches by HTTP
    method. GETs return ``before_post`` until the test flips
    ``state["posted"] = True`` (typically inside the POST handler), then
    return ``after_post`` for every subsequent GET. The POST defers to
    ``post_handler`` (or a default 200 ack).

    Why state-based instead of a payload-list rollover: cold mount may
    fire ``loadHooksSync`` more than once
    (``test_context_gateway_overview`` counts ``initial_calls >= 1`` for
    the overview equivalent). A second cold-mount GET serving
    ``after_post`` would render the post-action DOM before the user
    clicked Sync Now, hiding the regression the spec is meant to assert
    against. State-based switching keys on the actual user action.
    """
    state: dict = {"posted": False, "get_count": 0}

    def _handler(route):
        if route.request.method == "GET":
            state["get_count"] += 1
            payload = after_post if (state["posted"] and after_post is not None) else before_post
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps(payload),
            )
            return
        if route.request.method == "POST":
            state["posted"] = True
            if post_handler is not None:
                post_handler(route)
                return
            # Default POST ack — used when callers don't care about the
            # POST body (e.g. the cancel spec where confirming never
            # happens; the flag will not be flipped because Cancel does
            # not POST).
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/settings-sync", _handler)
    return state


def test_hooks_sync_cancel_fires_no_post(page, mm_web_url: str) -> None:
    """S2-a: clicking Cancel on the Hooks Sync confirm dialog must fire
    zero ``POST /api/settings-sync`` requests. Audit P0 host-write trust
    gate — without this guard, every Sync Now click writes to
    ``~/.claude/settings.json`` unconditionally.

    Negative half of the symmetric cancel/confirm pair.
    """
    install_default_stubs(page)

    post_calls: list[str] = []

    def _post(route):
        post_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    # Cancel never POSTs, so the GET payload only ever needs to reflect the
    # cold-mount ``out_of_sync`` state.
    get_state = _stub_settings_sync(page, _OUT_OF_SYNC_GET, post_handler=_post)

    page.goto(mm_web_url)
    _open_hooks_sync(page)
    assert get_state["get_count"] >= 1, "cold-mount GET must have fired"

    page.locator("#hooks-sync-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    title = (page.locator("#confirm-title").text_content() or "").strip()
    assert title == HOOKS_SYNC_TITLE, (
        f"Hooks Sync confirm title must be {HOOKS_SYNC_TITLE!r}, got {title!r}"
    )

    page.locator("#confirm-cancel-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    assert post_calls == [], (
        f"Cancel on Sync Now must not POST to /api/settings-sync, got {post_calls!r}"
    )


def test_hooks_sync_confirm_transitions_badge_to_in_sync(page, mm_web_url: str) -> None:
    """S2-b: confirm → POST + post-sync GET → badge transitions to
    ``badge-success`` with ``settings.hooks.in_sync`` text.

    Pins the post-sync ``loadHooksSync()`` call (line 184) and the badge
    rendering off ``data.status`` of the GET (line 36-43). A regression
    that drops the loadHooksSync() call would leave the badge stuck on
    the cold-mount ``out_of_sync`` value even though the POST succeeded.
    The class assertion (``badge-success``, not ``badge-green``) catches
    a regression that renames the success class.

    Symmetric pin: positive on the in-sync badge text + class, negative
    on the cold-mount conflict/pending text not lingering.
    """
    install_default_stubs(page)

    post_calls: list[str] = []

    def _post(route):
        post_calls.append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "results": [
                        {
                            "name": "claude",
                            "status": "ok",
                            "reason": None,
                            "warnings": [],
                            "target": "/fake/.claude/settings.json",
                        }
                    ],
                    "duplicate_tier_warnings": [],
                }
            ),
        )

    get_state = _stub_settings_sync(
        page, _OUT_OF_SYNC_GET, after_post=_IN_SYNC_GET, post_handler=_post
    )

    page.goto(mm_web_url)
    _open_hooks_sync(page)
    initial_get_count = get_state["get_count"]

    page.locator("#hooks-sync-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    # Anchor on ``expect_request`` (mirrors ``test_redaction_blocked_retry``
    # line 138) instead of relying on the Python-side ``post_calls`` list to
    # observe the POST after-the-fact. The route handler runs in Playwright's
    # async loop; on slower runners (Linux CI) a Python-side post-action
    # check can race the asyncio dispatch — ``page.expect_request`` is the
    # documented synchronization point for "wait until this request fires".
    with page.expect_request(
        lambda req: "/api/settings-sync" in req.url and req.method == "POST",
        timeout=4_000,
    ) as post_info:
        page.locator("#confirm-ok-btn").click()
    post_request = post_info.value
    assert post_request.method == "POST", post_request
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # After ``loadHooksSync()`` re-fetches, the badge must transition to
    # ``badge-success`` with the in-sync text. Wait on the badge text
    # because the GET → render is the user-visible signal we care about.
    badge_check = (
        "() => {"
        "  const badge = document.querySelector('#hooks-sync-status .badge');"
        "  if (!badge) return false;"
        "  const text = (badge.textContent || '').trim();"
        "  const cls = badge.className || '';"
        f"  return cls.includes('badge-success') && text === {json.dumps(HOOKS_IN_SYNC_BADGE)};"
        "}"
    )
    page.wait_for_function(badge_check, timeout=4_000)

    # ``post_calls`` is a defence-in-depth check that the route stub itself
    # ran (not just the request was dispatched). On the in-process route
    # handler, this should equal 1; if Playwright optimised it away (e.g.
    # request was matched but stub ran later), expect_request above already
    # caught the dispatch — keep the post_calls assertion as a soft pin
    # rather than the primary signal.
    assert len(post_calls) >= 1, (
        f"Route stub must have intercepted the POST at least once, got {post_calls!r}"
    )

    badge = page.locator("#hooks-sync-status .badge")
    badge_text = (badge.text_content() or "").strip()
    badge_classes = (badge.get_attribute("class") or "").split()

    assert badge_text == HOOKS_IN_SYNC_BADGE, (
        f"After confirm, badge text must be {HOOKS_IN_SYNC_BADGE!r}, got {badge_text!r}"
    )
    assert "badge-success" in badge_classes, (
        f"in_sync badge must use badge-success class, got {badge_classes!r}"
    )
    # Negative: a regression that swaps the class would still render the
    # text correctly — pin the warning/danger classes are absent so a
    # pure class rename is caught.
    assert "badge-warning" not in badge_classes, (
        f"in_sync badge must not use badge-warning, got {badge_classes!r}"
    )
    assert "badge-danger" not in badge_classes, (
        f"in_sync badge must not use badge-danger, got {badge_classes!r}"
    )

    # At least one post-sync GET must have happened (cold-mount + ≥1
    # post-sync reload). A regression that drops ``loadHooksSync()`` after
    # the POST keeps ``get_count == initial_get_count``.
    assert get_state["get_count"] > initial_get_count, (
        f"Confirm must trigger a post-sync GET reload; before = "
        f"{initial_get_count}, after = {get_state['get_count']}"
    )
