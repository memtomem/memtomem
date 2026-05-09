"""Browser tests for the per-conflict ``Use Proposed`` resolve flow.

Audit goal (``scripts/context-gateway-review-plan.md`` 갭 3): pin the
mtime-guard envelope contract for ``POST /api/context/settings/resolve``.

The route returns HTTP 200 with ``{"status": "aborted", "reason": "...",
"mtime_ns": "..."}`` when an external write changes the target file
between the read and the write — **not** HTTP 409 (see
``web/routes/settings_sync.py:329-334``). A regression that treats this
envelope as success would silently overwrite a cross-process write — the
exact failure mode this spec is meant to lock out.

The handler's contract (``static/settings-hooks-watchdog.js:117-150``):

* ``showConfirm`` first; cancel returns without firing the POST.
* On confirm, ``POST /api/context/settings/resolve``.
* ``!r.ok`` (HTTP 4xx/5xx) → toast.error + return (no ``loadHooksSync``).
* ``r.ok`` (HTTP 200) → parse body. ``status === 'ok'`` → toast +
  ``loadHooksSync()`` (the conflict card vanishes after the GET).
* Any other status (``aborted`` included) → toast.error + return; no
  ``loadHooksSync`` call, so the conflict card stays in place.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.browser


# Locale-pinned EN strings (default locale).
HOOKS_REPLACE_TITLE = "Replace hook rule"  # confirm.hooks_replace_title
HOOKS_IN_SYNC_BADGE = "All hooks are in sync"  # settings.hooks.in_sync


_CONFLICT_RULE = {
    "hooks": [{"type": "command", "command": "memtomem-hook"}],
}
_EXISTING_RULE = {
    "hooks": [{"type": "command", "command": "user-hook"}],
}

_CONFLICTS_GET = {
    "status": "conflicts",
    "target_path": "/fake/.claude/settings.json",
    "hooks": {
        "pending": [],
        "conflicts": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "existing": _EXISTING_RULE,
                "proposed": _CONFLICT_RULE,
            }
        ],
        "synced": [],
    },
}

_AFTER_RESOLVE_GET = {
    "status": "in_sync",
    "target_path": "/fake/.claude/settings.json",
    "hooks": {
        "pending": [],
        "conflicts": [],
        "synced": [
            {
                "event": "PreToolUse",
                "matcher": "Bash",
                "rule": _CONFLICT_RULE,
            }
        ],
    },
}


def _install_default_stubs(page) -> None:
    """Mirrors ``test_redaction_blocked_retry._install_default_stubs``."""

    def _ok(route, payload):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payload),
        )

    page.route("**/api/**", lambda r: _ok(r, {}))
    page.route("**/api/system/ui-mode", lambda r: _ok(r, {"mode": "prod"}))
    page.route("**/api/system/model-readiness", lambda r: _ok(r, {"ready": True}))
    page.route("**/api/sources", lambda r: _ok(r, {"sources": []}))
    page.route("**/api/namespaces", lambda r: _ok(r, {"namespaces": []}))
    page.route("**/api/stats", lambda r: _ok(r, {}))
    page.route("**/api/privacy/patterns", lambda r: _ok(r, {"patterns": []}))


def _open_hooks_sync(page) -> None:
    """Activate the Settings tab + Hooks Sync sub-section so
    ``loadHooksSync`` runs (see ``app.js:1205``).
    """
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('hooks-sync')")
    page.wait_for_function(
        "() => {"
        "  const status = document.getElementById('hooks-sync-status');"
        "  if (!status) return false;"
        "  const badge = status.querySelector('.badge');"
        "  return badge && (badge.textContent || '').trim().length > 0;"
        "}",
        timeout=5_000,
    )


def _stub_settings_sync_get(page, payloads: list[dict]) -> dict:
    """GET sequence for ``/api/settings-sync`` — single handler, dispatch
    by method. Resolve POST goes to a separate URL pattern, so this
    handler doesn't need to multiplex.
    """
    state = {"n": 0}

    def _handler(route):
        if route.request.method != "GET":
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        idx = min(state["n"], len(payloads) - 1)
        state["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payloads[idx]),
        )

    page.route("**/api/settings-sync", _handler)
    return state


def test_resolve_ok_removes_conflict_card(page, mm_web_url: str) -> None:
    """S3-a: Use Proposed → confirm → POST returns ``{status: 'ok'}`` →
    ``loadHooksSync()`` re-fetches and the conflict card disappears.

    Pins the happy-path branch (``settings-hooks-watchdog.js:142-144``)
    and the post-resolve GET reload. A regression that drops the
    ``loadHooksSync()`` call would leave the conflict card on screen
    even though the underlying state changed.
    """
    _install_default_stubs(page)
    get_state = _stub_settings_sync_get(page, [_CONFLICTS_GET, _AFTER_RESOLVE_GET])

    resolve_calls: list[dict] = []

    def _resolve_ok(route):
        resolve_calls.append(route.request.post_data_json or {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"status": "ok", "reason": "Replaced PreToolUse:Bash"}),
        )

    page.route("**/api/context/settings/resolve", _resolve_ok)

    page.goto(mm_web_url)
    _open_hooks_sync(page)

    # Cold mount renders one conflict card.
    conflict_locator = page.locator(".hooks-sync-conflict")
    page.wait_for_function(
        "() => document.querySelectorAll('.hooks-sync-conflict').length === 1",
        timeout=4_000,
    )
    assert conflict_locator.count() == 1, "cold mount must render 1 conflict card"

    # Click the inline Use Proposed button on the conflict card.
    page.locator(".hooks-sync-conflict .hooks-resolve-btn").click()

    # Confirm modal pops with the replace prompt.
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    title = (page.locator("#confirm-title").text_content() or "").strip()
    assert title == HOOKS_REPLACE_TITLE, (
        f"Resolve confirm title must be {HOOKS_REPLACE_TITLE!r}, got {title!r}"
    )

    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # After loadHooksSync() runs the post-resolve GET, the conflict card
    # is replaced by the synced view. Wait on the DOM transition rather
    # than on the GET counter — the user-visible signal is what we care
    # about.
    page.wait_for_function(
        "() => document.querySelectorAll('.hooks-sync-conflict').length === 0",
        timeout=4_000,
    )

    # Positive: in-sync badge.
    badge_check = (
        "() => {"
        "  const badge = document.querySelector('#hooks-sync-status .badge');"
        "  if (!badge) return false;"
        "  const text = (badge.textContent || '').trim();"
        f"  return text === {json.dumps(HOOKS_IN_SYNC_BADGE)};"
        "}"
    )
    page.wait_for_function(badge_check, timeout=4_000)

    # POST body must carry the canonical ``use_proposed`` action and the
    # event/matcher pair from the card's data attributes.
    assert len(resolve_calls) == 1, (
        f"Use Proposed must fire exactly one POST, got {resolve_calls!r}"
    )
    body = resolve_calls[0]
    assert body.get("event") == "PreToolUse", body
    assert body.get("matcher") == "Bash", body
    assert body.get("action") == "use_proposed", body

    # Two GETs must have happened: cold-mount + post-resolve reload.
    assert get_state["n"] >= 2, (
        f"Resolve OK must trigger a post-resolve GET reload, got {get_state['n']}"
    )


def test_resolve_aborted_envelope_keeps_card_and_emits_error_toast(page, mm_web_url: str) -> None:
    """S3-b: POST ``/resolve`` returns HTTP 200 + ``{status: 'aborted',
    reason: ..., mtime_ns: ...}`` → error toast + conflict card stays +
    no post-resolve GET reload. Audit P0 mtime-guard regression lock.

    The mtime guard contract returns HTTP 200 (not 409) — a regression
    that only checks ``resp.ok`` and ignores ``result.status`` would
    silently overwrite a cross-process write. The card-stays assertion
    is the true regression catch: a buggy handler that calls
    ``loadHooksSync()`` despite the aborted status would clear the card
    even though the resolve never happened.

    Symmetric pin: positive on the error toast text, negative on the
    conflict card not vanishing AND on the GET count not incrementing
    after the POST.
    """
    _install_default_stubs(page)
    get_state = _stub_settings_sync_get(page, [_CONFLICTS_GET])

    aborted_reason = "Target file was modified by another process. Retry."
    resolve_calls: list[dict] = []

    def _resolve_aborted(route):
        resolve_calls.append(route.request.post_data_json or {})
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "aborted",
                    "reason": aborted_reason,
                    "mtime_ns": "1234567890000000000",
                }
            ),
        )

    page.route("**/api/context/settings/resolve", _resolve_aborted)

    page.goto(mm_web_url)
    _open_hooks_sync(page)
    page.wait_for_function(
        "() => document.querySelectorAll('.hooks-sync-conflict').length === 1",
        timeout=4_000,
    )
    initial_get_count = get_state["n"]

    page.locator(".hooks-sync-conflict .hooks-resolve-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # Positive: error toast with the aborted reason text. The handler
    # passes ``result.reason`` to ``showToast(_, 'error')`` (line 146).
    page.wait_for_selector("#toast-container .toast.toast-error", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-error .toast-msg").text_content() or ""
    ).strip()
    assert toast_text == aborted_reason, (
        f"Aborted envelope must surface its reason as the error toast; "
        f"expected {aborted_reason!r}, got {toast_text!r}"
    )

    # Negative #1: the conflict card must remain in place. A regression
    # that calls ``loadHooksSync()`` regardless of status would clear it.
    assert page.locator(".hooks-sync-conflict").count() == 1, (
        "Aborted envelope must not clear the conflict card — the resolve never landed on disk."
    )

    # Negative #2: no post-resolve GET reload. The aborted branch returns
    # before ``loadHooksSync()``, so the GET count must equal the
    # cold-mount count.
    assert get_state["n"] == initial_get_count, (
        f"Aborted envelope must not trigger a GET reload; before = "
        f"{initial_get_count}, after = {get_state['n']}"
    )

    # The POST itself must have fired exactly once — the bug isn't that
    # the request didn't happen, it's that the response was misparsed.
    assert len(resolve_calls) == 1, (
        f"Use Proposed must fire one POST even when aborted, got {resolve_calls!r}"
    )
