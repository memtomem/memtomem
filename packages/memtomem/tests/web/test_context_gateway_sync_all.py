"""Browser tests for the Sync All flow on the Context Gateway dashboard.

Audit goal (``scripts/context-gateway-review-plan.md`` gap 3): pin the
``ctx-sync-all-btn`` handler's user-facing trust gates and severity routing
so a regression in (a) confirm modal, (b) settings POST ``aborted`` envelope
handling, or (c) ``loadCtxOverview`` reload after sync surfaces in CI.

The handler's contract (``static/context-gateway.js:435-514``):

* ``showConfirm`` first; cancel returns without firing a single sync POST.
* On confirm, fan out ``POST /api/context/{type}/sync`` for each detected
  runtime (``['skills', 'agents']`` in prod mode), then
  ``POST /api/context/settings/sync``.
* Artifact POST non-OK responses surface the backend ``detail`` text in the
  failure toast instead of replacing it with a generic client message.
* The settings POST response body is parsed for severity:
  ``error`` → ``toast.sync_failed`` ``error`` /
  ``aborted`` → ``settings.ctx.mtime_conflict`` ``warning`` /
  ``needs_confirmation`` → info partial + Open Settings action /
  else (``ok`` / ``skipped``) → ``settings.ctx.sync_success``.
* After all POSTs, ``loadCtxOverview()`` re-fetches ``/api/context/overview``
  to refresh the cards.

The harness ``page.route()``-stubs every endpoint; ``lifespan=None`` (see
``conftest.py``) skips the real backend.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# Locale-pinned EN strings — module-level constants instead of asserting on
# i18n keys avoids the ``feedback_data_i18n_nested_children.md`` failure
# mode (matching the markup rather than what the user sees). Default locale
# is EN, so no explicit ``I18N.setLang`` call is needed.
SYNC_ALL_TITLE = "Sync All"  # settings.ctx.sync_all
SYNC_SUCCESS_TOAST = "Sync completed"  # settings.ctx.sync_success
MTIME_CONFLICT_TOAST = "File was modified externally. Reloading..."  # settings.ctx.mtime_conflict
SYNC_PARTIAL_NEEDS_CONFIRMATION_TOAST = (
    "Sync All complete except Settings — confirm host writes in the Settings panel."
)
SYNC_FAILED_TEMPLATE = "Sync failed: {error}"  # toast.sync_failed


_HEALTHY_OVERVIEW = {
    "skills": {"total": 2, "in_sync": 2},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 1, "in_sync": 1},
    "settings": {
        "total": 1,
        "in_sync": 1,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}


def _open_context_gateway(page) -> None:
    """Mirrors ``test_context_gateway_overview._open_context_gateway``.

    Activates the Settings main tab + the Context Gateway overview
    sub-section, then waits for the cards to render. ``activateTab`` /
    ``switchSettingsSection`` are invoked from ``page.evaluate`` rather
    than chasing click coordinates because the sidebar layout has churned
    twice (#813 / #816) and a click-based path keeps re-breaking.
    """
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-overview')")
    page.wait_for_function(
        "() => {"
        "  const tiles = document.querySelectorAll("
        "    '#ctx-overview-content .ctx-overview-stat');"
        "  if (tiles.length === 0) return false;"
        "  return Array.from(tiles).every("
        "    el => (el.textContent || '').trim().length > 0);"
        "}",
        timeout=5_000,
    )


def _stub_overview_with_counter(page, payloads: list[dict]) -> dict:
    """Serve a sequence of overview payloads — 1st GET → ``payloads[0]``,
    2nd → ``payloads[1]``, etc. Last entry is reused for trailing calls.

    Returns a dict the caller can read post-action: ``{"n": <call_count>}``.
    A counter closure is the cleanest way to differentiate the cold-mount
    GET (initial state) from the post-sync GET (after-sync state) without
    racing on a Python-side timer.
    """
    state = {"n": 0}

    def _handler(route):
        idx = min(state["n"], len(payloads) - 1)
        state["n"] += 1
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(payloads[idx]),
        )

    page.route("**/api/context/overview", _handler)
    return state


def test_sync_all_cancel_fires_no_post(page, mm_web_url: str) -> None:
    """S1-a: clicking Cancel on the Sync All confirm dialog must fire zero
    POSTs to any of the per-type sync endpoints.

    Negative half of the symmetric cancel/confirm pair
    (``feedback_pin_invert_symmetric_assertion.md``); the positive halves
    are ``test_sync_all_happy_path_emits_success_toast`` and
    ``test_sync_all_settings_aborted_emits_mtime_conflict_warning``.
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    sync_calls: list[str] = []

    def _record_sync(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync", _record_sync)
    page.route("**/api/context/agents/sync", _record_sync)
    page.route("**/api/context/settings/sync", _record_sync)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    title = (page.locator("#confirm-title").text_content() or "").strip()
    assert title == SYNC_ALL_TITLE, (
        f"Sync All confirm dialog title must be {SYNC_ALL_TITLE!r}, got {title!r}"
    )

    page.locator("#confirm-cancel-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # The cancel branch returns before any fetch is dispatched; the modal
    # hidden checkpoint is the deterministic settle. Any background POST
    # would have queued before the click handler returned.
    assert sync_calls == [], f"Cancel must not fire any sync POST, got {sync_calls!r}"


def test_sync_all_happy_path_emits_success_toast(page, mm_web_url: str) -> None:
    """S1-b: confirm → all POSTs succeed → success toast + overview reload.

    Pins the all-``ok`` branch of the severity ladder
    (``static/context-gateway.js:507-509``) and the unconditional
    ``loadCtxOverview()`` call at line 510. Settings POST returns the
    canonical ``{results: [{status: 'ok'}]}`` shape so the parser cannot
    fall through to ``error``/``aborted``/``needs_confirmation``.
    """
    install_default_stubs(page)
    overview_state = _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    sync_calls: list[str] = []

    def _record_sync(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync", _record_sync)
    page.route("**/api/context/agents/sync", _record_sync)

    def _settings_sync(route):
        sync_calls.append(route.request.url)
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

    page.route("**/api/context/settings/sync", _settings_sync)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    initial_overview_calls = overview_state["n"]

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # Wait for the success toast — its text is what differentiates ok from
    # aborted/error. The selector matches the first toast in the container;
    # ``btnLoading`` in the handler keeps the button disabled until the
    # toast has been shown, so racing isn't an issue.
    page.wait_for_selector("#toast-container .toast.toast-success", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-success .toast-msg").text_content() or ""
    ).strip()
    assert toast_text == SYNC_SUCCESS_TOAST, (
        f"Happy-path toast must be {SYNC_SUCCESS_TOAST!r}, got {toast_text!r}"
    )

    # All three POSTs fired in the prod-mode order: skills → agents → settings.
    # The exact order matters because a regression that reorders or drops one
    # would surface here. Commands is dev-only and should not appear.
    sync_paths = [u.split("/api/")[-1] for u in sync_calls]
    assert sync_paths == [
        "context/skills/sync",
        "context/agents/sync",
        "context/settings/sync",
    ], f"Sync All must fire skills→agents→settings in prod, got {sync_paths!r}"

    # After all POSTs the handler calls ``loadCtxOverview()`` (line 510) to
    # refresh the cards. Pin the second GET to catch a regression that drops
    # the post-sync reload — without it the dashboard would show stale
    # counts immediately after a sync.
    assert overview_state["n"] >= initial_overview_calls + 1, (
        f"Sync All must trigger a post-sync overview reload; calls before "
        f"= {initial_overview_calls}, after = {overview_state['n']}"
    )


@pytest.mark.parametrize(
    ("content_type", "body", "expected_error"),
    [
        (
            "application/json",
            json.dumps(
                {
                    "detail": (
                        "Privacy scan blocked project_shared skill sync. Remove the "
                        "secret or migrate the artifact to a writable tier."
                    )
                }
            ),
            (
                "Privacy scan blocked project_shared skill sync. Remove the "
                "secret or migrate the artifact to a writable tier."
            ),
        ),
        (
            "text/plain",
            "Plain-text artifact sync failure",
            "Plain-text artifact sync failure",
        ),
    ],
    ids=["json-detail", "text-body"],
)
def test_sync_all_artifact_422_surfaces_backend_error(
    page,
    mm_web_url: str,
    content_type: str,
    body: str,
    expected_error: str,
) -> None:
    """S1-b.1: artifact sync HTTP 422 must preserve the backend error body.

    Privacy blocks include remediation guidance in ``detail``. Sync All used
    to discard that body and throw ``Sync <type> failed``, leaving users with
    no way to resolve the blocked fan-out from the toast. Plain-text bodies
    cover the helper's non-JSON fallback path.
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    sync_calls: list[str] = []

    def _skills_privacy_block(route):
        sync_calls.append(route.request.url)
        route.fulfill(
            status=422,
            content_type=content_type,
            body=body,
        )

    def _unexpected_sync(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync", _skills_privacy_block)
    page.route("**/api/context/agents/sync", _unexpected_sync)
    page.route("**/api/context/settings/sync", _unexpected_sync)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    page.wait_for_selector("#toast-container .toast.toast-error", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-error .toast-msg").text_content() or ""
    ).strip()
    expected = SYNC_FAILED_TEMPLATE.format(error=expected_error)
    assert toast_text == expected, (
        f"Artifact privacy block toast must surface backend detail {expected!r}, got {toast_text!r}"
    )

    sync_paths = [u.split("/api/")[-1] for u in sync_calls]
    assert sync_paths == ["context/skills/sync"], (
        f"Sync All must stop after the blocked artifact sync, got {sync_paths!r}"
    )


def test_sync_all_settings_aborted_emits_mtime_conflict_warning(page, mm_web_url: str) -> None:
    """S1-c: settings POST returns ``{status: 'aborted'}`` → warning toast
    with ``settings.ctx.mtime_conflict`` text. Audit P0 regression lock.

    The mtime guard contract (``web/routes/settings_sync.py:329-334``)
    returns HTTP 200 with ``{"status": "aborted", "reason": "...",
    "mtime_ns": "..."}``, **not** HTTP 409. A regression that treats this
    envelope as success (e.g. only checking ``resp.ok``, or forgetting the
    ``aborted`` branch in the severity ladder) would silently overwrite a
    cross-process write — the exact failure mode the audit P0 was meant
    to lock out.

    Symmetric pin (``feedback_pin_invert_symmetric_assertion.md``):
    positive on the warning toast text, negative on the success toast not
    appearing — both must hold or the spec gives a false PASS.
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.route(
        "**/api/context/skills/sync",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.route(
        "**/api/context/agents/sync",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )

    def _settings_aborted(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "results": [
                        {
                            "name": "claude",
                            "status": "aborted",
                            "reason": ("Target file was modified by another process. Retry."),
                            "warnings": [],
                            "target": "/fake/.claude/settings.json",
                        }
                    ],
                    "duplicate_tier_warnings": [],
                }
            ),
        )

    page.route("**/api/context/settings/sync", _settings_aborted)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    # Positive: warning toast with mtime_conflict text.
    page.wait_for_selector("#toast-container .toast.toast-warning", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-warning .toast-msg").text_content() or ""
    ).strip()
    assert toast_text == MTIME_CONFLICT_TOAST, (
        f"Aborted envelope must surface mtime_conflict warning "
        f"({MTIME_CONFLICT_TOAST!r}), got {toast_text!r}"
    )

    # Negative: the success toast must NOT have rendered. A regression that
    # drops the ``aborted`` branch and falls through to ``ok`` would render
    # the success toast — checking only the warning toast wouldn't catch
    # that (both could co-exist in the container).
    success_count = page.locator("#toast-container .toast.toast-success").count()
    assert success_count == 0, (
        f"Aborted envelope must not emit the success toast; found {success_count}"
    )


def test_sync_all_settings_error_emits_failure_toast(page, mm_web_url: str) -> None:
    """S1-d: settings POST returns ``{status: 'error'}`` → error toast
    with the ``toast.sync_failed`` template and no success toast.

    This pins the highest-severity non-aborted branch in
    ``context-gateway.js`` so a regression cannot treat a per-result error
    as a full Sync All success just because the HTTP response itself is OK.
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.route(
        "**/api/context/skills/sync",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.route(
        "**/api/context/agents/sync",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )

    error_reason = "Settings merge failed"

    def _settings_error(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "results": [
                        {
                            "name": "claude",
                            "status": "error",
                            "reason": error_reason,
                            "warnings": [],
                            "target": "/fake/.claude/settings.json",
                        }
                    ],
                    "duplicate_tier_warnings": [],
                }
            ),
        )

    page.route("**/api/context/settings/sync", _settings_error)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    page.wait_for_selector("#toast-container .toast.toast-error", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-error .toast-msg").text_content() or ""
    ).strip()
    expected = SYNC_FAILED_TEMPLATE.format(error=error_reason)
    assert toast_text == expected, f"Settings error toast must be {expected!r}, got {toast_text!r}"

    success_count = page.locator("#toast-container .toast.toast-success").count()
    assert success_count == 0, (
        f"Settings error branch must not emit the success toast; found {success_count}"
    )


def test_sync_all_settings_needs_confirmation_opens_hooks_sync(page, mm_web_url: str) -> None:
    """S1-e: settings POST returns ``needs_confirmation`` → info toast with
    an Open Settings action that navigates to the Hooks Sync section.

    This pins the partial-success branch added for host-write confirmation:
    the user should see a non-success severity and get a direct action to
    the section that can resolve the pending hooks.
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    page.route(
        "**/api/context/skills/sync",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.route(
        "**/api/context/agents/sync",
        lambda r: r.fulfill(status=200, content_type="application/json", body="{}"),
    )
    page.route(
        "**/api/settings-sync",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "status": "conflicts",
                    "target_path": "/fake/.claude/settings.json",
                    "hooks": {"pending": [], "conflicts": [], "synced": []},
                }
            ),
        ),
    )

    def _settings_needs_confirmation(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "results": [
                        {
                            "name": "claude",
                            "status": "needs_confirmation",
                            "reason": "Host writes require confirmation",
                            "warnings": [],
                            "target": "/fake/.claude/settings.json",
                        }
                    ],
                    "duplicate_tier_warnings": [],
                }
            ),
        )

    page.route("**/api/context/settings/sync", _settings_needs_confirmation)

    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function(
        "() => !document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )
    page.locator("#confirm-ok-btn").click()
    page.wait_for_function(
        "() => document.getElementById('confirm-modal').hidden",
        timeout=2_000,
    )

    page.wait_for_selector("#toast-container .toast.toast-info", timeout=4_000)
    toast = page.locator("#toast-container .toast.toast-info")
    toast_text = (toast.locator(".toast-msg").text_content() or "").strip()
    assert toast_text == SYNC_PARTIAL_NEEDS_CONFIRMATION_TOAST, (
        f"Needs-confirmation toast must be {SYNC_PARTIAL_NEEDS_CONFIRMATION_TOAST!r}, "
        f"got {toast_text!r}"
    )

    success_count = page.locator("#toast-container .toast.toast-success").count()
    assert success_count == 0, (
        f"Needs-confirmation branch must not emit the success toast; found {success_count}"
    )

    toast.locator(".toast-action").click()
    page.wait_for_function(
        "() => {"
        "  const section = document.getElementById('settings-hooks-sync');"
        "  return section && !section.classList.contains('hidden');"
        "}",
        timeout=4_000,
    )
