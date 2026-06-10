"""Browser tests for the Sync All flow on the Context Gateway dashboard.

Audit goal (``scripts/context-gateway-review-plan.md`` gap 3): pin the
``ctx-sync-all-btn`` handler's user-facing trust gates and severity routing
so a regression in (a) confirm modal, (b) settings POST ``aborted`` envelope
handling, or (c) ``loadCtxOverview`` reload after sync surfaces in CI.

The handler's contract (``static/context-gateway.js:435-514``):

* ``showConfirm`` first; cancel returns without firing a single sync POST.
* On confirm, fan out ``POST /api/context/{type}/sync`` for skills,
  commands, agents, and mcp-servers in that order, then
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
SYNC_PARTIAL_FAILED_TEMPLATE = (
    "{succeeded} synced — {failed_phase} failed: {reason}"  # toast.sync_partial_failed
)


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

    # ``**`` suffix: with a registered active project the overview GET
    # carries ``?scope_id=<id>`` — a bare glob misses it and the request
    # falls through to the conftest all-zero default, which (since the
    # all-empty Sync All gate) disables the button under test.
    page.route("**/api/context/overview**", _handler)
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
    page.route("**/api/context/commands/sync", _record_sync)
    page.route("**/api/context/agents/sync", _record_sync)
    # mcp-servers is part of the artifact fan-out (context-gateway.js ``types``)
    # but was previously absorbed by the conftest ``**/api/**`` catch-all and
    # never pinned. Route it explicitly so the phase order assertion below
    # covers all four artifact phases (ADR-0021 PR6 / Codex round-2 Minor).
    page.route("**/api/context/mcp-servers/sync", _record_sync)

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

    # All five POSTs fired in order: skills → commands → agents →
    # mcp-servers → settings. The exact order matters because a regression
    # that reorders or drops one would surface here. mcp-servers is pinned
    # explicitly (PR6) so it can no longer hide behind the catch-all.
    sync_paths = [u.split("/api/")[-1].split("?")[0] for u in sync_calls]
    assert sync_paths == [
        "context/skills/sync",
        "context/commands/sync",
        "context/agents/sync",
        "context/mcp-servers/sync",
        "context/settings/sync",
    ], f"Sync All must fire skills→commands→agents→mcp-servers→settings, got {sync_paths!r}"

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


def test_sync_all_mid_run_failure_refreshes_overview_with_partial_toast(
    page, mm_web_url: str
) -> None:
    """S1-b.2 (#1074): skills succeed → agents fails → settings is skipped,
    but the overview is still refreshed and the toast names what landed.

    Pre-fix the handler ``throw``-ed on the first non-OK response and the
    ``catch`` branch fell straight through to ``btnLoading(btn, false)``
    without re-fetching the overview. Disk had changed (skills already
    wrote) but the dashboard kept showing the pre-sync counts — a stale
    diff target that made retries confusing.

    Symmetric pin (``feedback_pin_invert_symmetric_assertion.md``):
    positive on the partial toast text + overview reload counter, negative
    on the settings sync POST not firing (we stop on first failure to
    avoid cascading noise; settings often shares root cause with
    artifact failures).
    """
    install_default_stubs(page)
    overview_state = _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    sync_calls: list[str] = []

    def _record_ok(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    agents_reason = "Agents target folder is read-only"

    def _agents_fail(route):
        sync_calls.append(route.request.url)
        route.fulfill(
            status=422,
            content_type="application/json",
            body=json.dumps({"detail": agents_reason}),
        )

    def _unexpected_settings(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/skills/sync", _record_ok)
    page.route("**/api/context/commands/sync", _record_ok)
    page.route("**/api/context/agents/sync", _agents_fail)
    page.route("**/api/context/settings/sync", _unexpected_settings)

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

    page.wait_for_selector("#toast-container .toast.toast-error", timeout=4_000)
    toast_text = (
        page.locator("#toast-container .toast.toast-error .toast-msg").text_content() or ""
    ).strip()
    expected = SYNC_PARTIAL_FAILED_TEMPLATE.format(
        succeeded="Skills, Custom Commands",
        failed_phase="Subagents",
        reason=agents_reason,
    )
    assert toast_text == expected, (
        f"Partial-failure toast must name landed + failed phase ({expected!r}), got {toast_text!r}"
    )

    # Negative: the generic single-phase ``Sync failed`` toast must NOT
    # render. A regression that drops the partial branch and falls back
    # to ``toast.sync_failed`` would still produce an error toast with
    # the agents reason — distinguishing them requires the exact text.
    sync_failed_only = SYNC_FAILED_TEMPLATE.format(error=agents_reason)
    error_toasts = page.locator(
        "#toast-container .toast.toast-error .toast-msg"
    ).all_text_contents()
    assert sync_failed_only not in [t.strip() for t in error_toasts], (
        f"Partial failure must not render the bare {sync_failed_only!r} toast; saw {error_toasts!r}"
    )

    # Settings POST must not have fired — we stop on first failure.
    sync_paths = [u.split("/api/")[-1] for u in sync_calls]
    assert sync_paths == [
        "context/skills/sync",
        "context/commands/sync",
        "context/agents/sync",
    ], f"Sync All must stop at the failed phase, got {sync_paths!r}"

    # Load-bearing assertion for #1074: overview reloaded even though the
    # run failed mid-way. Without this the dashboard would show the
    # pre-sync skills count after skills has already written to disk.
    assert overview_state["n"] >= initial_overview_calls + 1, (
        f"Mid-run failure must still refresh overview; calls before "
        f"= {initial_overview_calls}, after = {overview_state['n']}"
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
        "**/api/context/commands/sync",
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
        "**/api/context/commands/sync",
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
        "**/api/context/commands/sync",
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


def test_sync_all_mid_run_tier_flip_pins_all_phases_to_start_tier(page, mm_web_url: str) -> None:
    """S1-f (ADR-0021 PR6 / Codex round-2 Major-1): a tier flip *during* a Sync
    All run must not make later phases land in a different tier.

    The handler snapshots both the active scope id and the target tier right
    after confirm, then pins them into every phase URL. Before the fix
    ``_ctxWithTargetScope`` re-read the mutable ``_ctxTargetScope`` global on
    each phase, so flipping the tier button mid-sequence sent the remaining
    phases to a different tier — violating "one (project, tier) per
    invocation" (ADR-0016 §5).

    Mechanism: an init-script wraps ``window.fetch`` and flips the live tier
    to ``user`` the instant the first artifact phase (skills) fires — i.e.
    after phase 1's URL is built but before phase 2's. Sync All is only
    enabled on ``project_shared`` (the default, which emits no
    ``target_scope`` param), so the regression shows up as later phases
    carrying ``target_scope=user``.

    Symmetric pin (``feedback_pin_invert_symmetric_assertion.md``): positive
    that no phase URL carries ``target_scope=``; negative guard that the flip
    actually fired (otherwise the positive assertion is vacuous).
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    phase_urls: list[str] = []

    def _record(route):
        phase_urls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    # ``**`` suffix matches the ``?scope_id=`` / ``?target_scope=`` query
    # variants — a bare ``.../sync`` glob would miss a URL with a query string
    # and let it fall through to the conftest catch-all (unrecorded).
    page.route("**/api/context/skills/sync**", _record)
    page.route("**/api/context/commands/sync**", _record)
    page.route("**/api/context/agents/sync**", _record)
    page.route("**/api/context/mcp-servers/sync**", _record)

    def _settings(route):
        phase_urls.append(route.request.url)
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

    page.route("**/api/context/settings/sync**", _settings)

    # Flip the live tier global the moment skills/sync fires. ``_ctxTargetScope``
    # is a top-level ``let`` in the classic (non-module) context-gateway.js
    # script, so it is reachable as a bare global from the wrapped fetch.
    page.add_init_script(
        """
        (() => {
          const realFetch = window.fetch.bind(window);
          let flipped = false;
          window.fetch = (url, opts) => {
            if (!flipped && String(url).includes('/api/context/skills/sync')) {
              flipped = true;
              try { _ctxTargetScope = 'user'; } catch (e) {}
            }
            return realFetch(url, opts);
          };
        })();
        """
    )

    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Default tier is project_shared → Sync All enabled.
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

    # All phases return 200 → success toast settles the run.
    page.wait_for_selector("#toast-container .toast.toast-success", timeout=4_000)

    assert len(phase_urls) == 5, (
        f"all five phases must fire (skills→commands→agents→mcp-servers→settings); "
        f"got {phase_urls!r}"
    )

    # Positive: no phase straddled to another tier. project_shared emits no
    # ``target_scope`` param, so the pinned run leaves it absent on every URL.
    straddled = [u for u in phase_urls if "target_scope=" in u]
    assert straddled == [], (
        f"mid-run tier flip must not straddle phases across tiers; these phase "
        f"URLs carried a target_scope param: {straddled!r}"
    )

    # Negative guard: confirm the flip actually took effect on the live global,
    # so the positive assertion above is not vacuously true.
    live_tier = page.evaluate("() => _ctxTargetScope")
    assert live_tier == "user", (
        f"harness must have flipped the live tier to 'user' mid-run "
        f"(got {live_tier!r}); the pin assertion would otherwise be vacuous"
    )


def test_sync_all_mid_run_cache_refresh_pins_scope_to_start_project(page, mm_web_url: str) -> None:
    """S1-g (ADR-0021 PR6 / review blocker): the active project scope must stay
    pinned for the whole run even if ``_ctxProjectsCache`` is refreshed mid-run.

    Sister of the tier-flip test on the *scope* dimension. The handler
    snapshots the effective scope id at confirm and passes it with
    ``scopeResolved`` so ``_ctxWithTargetScope`` emits it verbatim. Before the
    fix the snapshot was re-resolved per phase via
    ``_ctxScopeParam`` → ``_ctxEffectiveScopeId``, which looks the scope up in
    the live ``_ctxProjectsCache``; clearing that cache mid-run (the pinned
    project goes ``missing``) collapsed later phases to Server-CWD (``scope_id``
    dropped) — straddling the run across two projects (ADR-0016 §5).

    Mechanism: the projects payload registers a single non-cwd project so the
    active scope resolves to ``proj-x``; an init-script wraps ``window.fetch``
    and empties ``_ctxProjectsCache`` the instant skills/sync fires.
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    # Override the conftest server-CWD payload with a real (non-cwd) project so
    # the active scope id is a concrete ``proj-x`` that appears on phase URLs.
    project_scope = {
        "scope_id": "proj-x",
        "label": "Project X",
        "root": "/fake/project-x",
        "tier": "project",
        # Real backend source string is ``known-projects``; enrolled + enabled so
        # the scope is sync-eligible and Sync All stays enabled (#1203 gate).
        "sources": ["known-projects"],
        "experimental": False,
        "missing": False,
        "stale": False,
        "enabled": True,
        "sync_eligible": True,
        "counts": {"skills": 2, "commands": 0, "agents": 1, "mcp-servers": 0},
    }
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scopes": [project_scope]}),
        ),
    )

    phase_urls: list[str] = []

    def _record(route):
        phase_urls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    # ``**`` suffix matches the ``?scope_id=proj-x`` query these phase URLs
    # carry — a bare ``.../sync`` glob would miss them and let them fall through
    # to the conftest catch-all (unrecorded).
    page.route("**/api/context/skills/sync**", _record)
    page.route("**/api/context/commands/sync**", _record)
    page.route("**/api/context/agents/sync**", _record)
    page.route("**/api/context/mcp-servers/sync**", _record)

    def _settings(route):
        phase_urls.append(route.request.url)
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

    page.route("**/api/context/settings/sync**", _settings)

    # Empty the live projects cache the instant skills/sync fires — i.e. after
    # phase 1's URL is built but before phase 2's. ``_ctxProjectsCache`` is a
    # top-level ``let`` in the classic context-gateway.js script, reachable as a
    # bare global from the wrapped fetch.
    # Record the cache length *right after* clearing into a window flag — the
    # handler's finally re-fetches projects and restores the cache, so reading
    # it post-run can't prove the mid-run emptying. Reading the same binding
    # inside the wrapper proves (a) the bare-global assignment hit the real
    # _ctxProjectsCache and (b) phases 2..5 were built against an empty cache.
    page.add_init_script(
        """
        (() => {
          const realFetch = window.fetch.bind(window);
          let cleared = false;
          window.fetch = (url, opts) => {
            if (!cleared && String(url).includes('/api/context/skills/sync')) {
              cleared = true;
              try {
                _ctxProjectsCache = [];
                window.__ctxCacheLenAfterClear = _ctxProjectsCache.length;
              } catch (e) {
                window.__ctxCacheLenAfterClear = -1;
              }
            }
            return realFetch(url, opts);
          };
        })();
        """
    )

    page.goto(mm_web_url)
    _open_context_gateway(page)

    # Sanity: the active scope resolved to the registered project.
    assert page.evaluate("() => _ctxActiveScopeId") == "proj-x", (
        "active scope must resolve to the registered project before the run"
    )

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
    page.wait_for_selector("#toast-container .toast.toast-success", timeout=4_000)

    assert len(phase_urls) == 5, f"all five phases must fire; got {phase_urls!r}"

    # Positive: every phase carries the pinned scope_id, even the ones built
    # after the cache was emptied.
    unpinned = [u for u in phase_urls if "scope_id=proj-x" not in u]
    assert unpinned == [], (
        f"mid-run cache refresh must not unpin the scope; these phase URLs "
        f"dropped scope_id=proj-x: {unpinned!r}"
    )

    # Negative guard: confirm the cache was actually emptied mid-run (captured
    # inside the wrapper before the finally re-fetch restored it), so the
    # positive assertion is not vacuous — without the empty cache the scope
    # would resolve normally and pass regardless of the pin.
    cleared_len = page.evaluate("() => window.__ctxCacheLenAfterClear")
    assert cleared_len == 0, (
        f"harness must have emptied _ctxProjectsCache mid-run (got "
        f"{cleared_len!r}); the pin assertion would otherwise be vacuous"
    )


# --- 2026-06-10 diagnostics package (review U5) -------------------------------

_ALL_EMPTY_OVERVIEW = {
    "skills": {"total": 0, "in_sync": 0},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "mcp_servers": {"total": 0, "local_draft": 0},
    "settings": {
        "total": 0,
        "in_sync": 0,
        "out_of_sync": 0,
        "missing_target": 0,
        "error": 0,
        "status": "in_sync",
    },
}


def test_sync_all_all_empty_pre_click_gate(page, mm_web_url: str) -> None:
    """U5(a): with zero stored artifacts everywhere (settings included),
    Sync All must be gated pre-click — pre-change the empty project kept the
    button live, ran five no-op phases, and toasted "Sync completed".
    """
    install_default_stubs(page)
    _stub_overview_with_counter(page, [_ALL_EMPTY_OVERVIEW])

    sync_calls: list[str] = []

    def _record(route):
        sync_calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body="{}")

    page.route("**/api/context/*/sync**", _record)
    page.goto(mm_web_url)
    _open_context_gateway(page)

    btn = page.locator("#ctx-sync-all-btn")
    assert btn.get_attribute("data-runtime-only") == "true", (
        "all-empty overview must gate Sync All pre-click"
    )
    assert btn.get_attribute("aria-disabled") == "true"
    title = btn.get_attribute("title") or ""
    assert "Nothing to push yet" in title, f"gate tooltip must explain why, got {title!r}"

    # ``aria-disabled`` blocks Playwright's actionability click; the real
    # browser still dispatches the event, which the handler turns into the
    # explanatory toast (clicks-fire-a-toast pattern, see #1075).
    page.evaluate("() => document.getElementById('ctx-sync-all-btn').click()")
    page.wait_for_selector("#toast-container .toast.toast-info", timeout=3_000)
    assert sync_calls == [], f"no phase may fire on a gated click, got {sync_calls!r}"


def test_sync_all_noop_run_shows_nothing_synced_toast(page, mm_web_url: str) -> None:
    """U5(b): a run whose phases all return only ``no_canonical_root`` skips
    (0 generated) with an all-skipped settings phase wrote nothing — the
    final toast must say so instead of "Sync completed". Covers a stale
    overview racing an emptied store past the pre-click gate.
    """
    install_default_stubs(page)
    # Healthy counts keep the pre-click gate open; the *run* is the no-op.
    _stub_overview_with_counter(page, [_HEALTHY_OVERVIEW])

    noop_body = json.dumps(
        {"generated": [], "dropped": [], "skipped": [{"reason_code": "no_canonical_root"}]}
    )

    def _noop_handler(route):
        route.fulfill(status=200, content_type="application/json", body=noop_body)

    for typ in ("skills", "commands", "agents", "mcp-servers"):
        page.route(f"**/api/context/{typ}/sync**", _noop_handler)
    page.route(
        "**/api/context/settings/sync**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "results": [
                        {
                            "name": "claude",
                            "status": "skipped",
                            "reason": "no canonical settings",
                            "warnings": [],
                            "target": None,
                        }
                    ],
                    "duplicate_tier_warnings": [],
                }
            ),
        ),
    )
    page.goto(mm_web_url)
    _open_context_gateway(page)

    page.locator("#ctx-sync-all-btn").click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden", timeout=2_000)
    page.locator("#confirm-ok-btn").click()

    toast = page.wait_for_selector("#toast-container .toast.toast-info", timeout=4_000)
    text = toast.text_content() or ""
    assert "Nothing to sync yet" in text, (
        f"no-op run must surface the nothing-synced toast, got {text!r}"
    )
    success = page.locator("#toast-container .toast.toast-success")
    assert success.count() == 0, "a no-op run must not toast 'Sync completed'"
