"""Browser tests for the structured 409 (#1210) write-guard handling on the
Hooks settings-sync surface (follow-up to #1210).

``POST /api/settings-sync`` is gated by ``resolve_writable_scope_root``: a
sync-ineligible project is rejected with the structured ``detail`` body
``{reason_code, message, project_scope_id}``. The Sync Now handler throws
``new Error(err.detail || ...)`` which, before this PR, stringified the object
to ``[object Object]`` inside the outer ``toast.sync_failed`` wrapper. The
``_hooksErrDetail`` trampoline (→ ``_ctxErrDetail``) now maps it to localized
copy. These specs pin:

* The settings-sync 409 surfaces the localized paused / not-enrolled copy
  through the ``toast.sync_failed`` wrapper, never ``[object Object]``.
* §5b proactive gate: a sync-ineligible active project disables the Sync Now
  button (with the matrix tooltip) before any POST fires.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# Locale-pinned copy (en.json is the source of truth).
EN_PAUSED = "Project sync is paused — resume it on the Projects board."
EN_NOT_ENROLLED = "Project is not active for sync — activate it on the Projects board."
MATRIX_PAUSED_TITLE = "Sync paused — resume it on the Projects board"


_OUT_OF_SYNC_GET = {
    "status": "out_of_sync",
    "target_path": "/fake/.claude/settings.json",
    "target_scope": "project_shared",
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


def _paused_scope(scope_id: str = "p-off") -> dict:
    return {
        "scope_id": scope_id,
        "project_scope_id": scope_id,
        "label": "Paused",
        "root": "/work/off",
        "tier": "project",
        "sources": ["known-projects"],
        "missing": False,
        "stale": False,
        "experimental": False,
        "enabled": False,
        "sync_eligible": False,
        "counts": {"skills": 1, "commands": 0, "agents": 0},
    }


def _open_hooks_sync(page) -> None:
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


def _stub_settings_sync_409(page, reason_code: str) -> dict:
    """GET serves a syncable status (so the button is enabled); POST returns
    the structured 409. Records POST count."""
    state: dict = {"post_count": 0}

    def _handler(route):
        if route.request.method == "POST":
            state["post_count"] += 1
            route.fulfill(
                status=409,
                content_type="application/json",
                body=json.dumps(
                    {
                        "detail": {
                            "reason_code": reason_code,
                            "message": "English backend message.",
                            "project_scope_id": "p-off",
                        }
                    }
                ),
            )
            return
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_OUT_OF_SYNC_GET)
        )

    page.route("**/api/settings-sync**", _handler)
    return state


def _error_toast_text(page) -> str:
    el = page.wait_for_selector("#toast-container .toast-error .toast-msg", timeout=4_000)
    return (el.text_content() or "").strip()


# ---------------------------------------------------------------------------
# §2b error-handling — structured 409 through the toast.sync_failed wrapper
# ---------------------------------------------------------------------------


def test_hooks_sync_409_sync_paused(page, mm_web_url: str) -> None:
    """A sync_paused 409 on the authorized POST surfaces the localized paused
    copy (via the ``toast.sync_failed`` wrapper), never ``[object Object]``."""
    install_default_stubs(page)
    state = _stub_settings_sync_409(page, "sync_paused")
    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator("#hooks-sync-btn").click()
    text = _error_toast_text(page)
    assert EN_PAUSED in text, f"toast must surface the localized paused copy; got {text!r}"
    assert "[object Object]" not in text, f"structured detail leaked as object; got {text!r}"
    assert state["post_count"] >= 1, "Sync Now must have issued the POST that 409'd"


def test_hooks_sync_409_sync_not_enrolled(page, mm_web_url: str) -> None:
    """sync_not_enrolled 409 → localized not-enrolled copy."""
    install_default_stubs(page)
    _stub_settings_sync_409(page, "sync_not_enrolled")
    page.goto(mm_web_url)
    _open_hooks_sync(page)

    page.locator("#hooks-sync-btn").click()
    text = _error_toast_text(page)
    assert EN_NOT_ENROLLED in text, f"toast must surface the not-enrolled copy; got {text!r}"
    assert "[object Object]" not in text


# ---------------------------------------------------------------------------
# §5b proactive gate — disable Sync Now on a sync-ineligible active scope
# ---------------------------------------------------------------------------


def test_hooks_sync_button_disabled_when_scope_paused(page, mm_web_url: str) -> None:
    """A paused active project disables Sync Now (with the matrix tooltip)
    before any POST fires — the 409 round-trip is avoided."""
    install_default_stubs(page)
    state: dict = {"post_count": 0}

    def _handler(route):
        if route.request.method == "POST":
            state["post_count"] += 1
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_OUT_OF_SYNC_GET)
        )

    page.route("**/api/settings-sync**", _handler)
    page.add_init_script("localStorage.setItem('memtomem_ctx_active_scope_id', 'p-off')")
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scopes": [_paused_scope()]}),
        ),
    )
    page.goto(mm_web_url)
    _open_hooks_sync(page)

    # The button must be disabled with the eligibility marker + matrix tooltip.
    page.wait_for_function(
        "() => {"
        "  const b = document.getElementById('hooks-sync-btn');"
        "  return b && b.disabled && b.getAttribute('data-sync-ineligible')"
        "    === 'settings.ctx.matrix_sync_paused_title';"
        "}",
        timeout=4_000,
    )
    btn = page.locator("#hooks-sync-btn")
    assert MATRIX_PAUSED_TITLE in (btn.get_attribute("title") or ""), (
        f"disabled Sync Now must show the paused tooltip; got {btn.get_attribute('title')!r}"
    )
    # No source / no hooks markers must NOT be the disable reason here.
    assert btn.get_attribute("data-no-source") is None
    assert btn.get_attribute("data-no-hooks") is None
    assert state["post_count"] == 0, (
        f"a pre-disabled Sync Now must not POST during load; saw {state['post_count']}"
    )


def test_hooks_sync_button_enabled_on_user_tier_despite_paused_project(
    page, mm_web_url: str
) -> None:
    """The backend gates only project-tier writes (``target_scope != 'user'``):
    a user-tier hooks sync targets global ``~/.claude``, not the project runtime,
    so it is allowed even when the active project is paused. The proactive gate
    must mirror that exemption — Sync Now stays ENABLED on the user tier (and must
    NOT show the misleading 'resume on the Projects board' tooltip)."""
    install_default_stubs(page)
    state: dict = {"post_count": 0}

    def _handler(route):
        if route.request.method == "POST":
            state["post_count"] += 1
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_OUT_OF_SYNC_GET)
        )

    page.route("**/api/settings-sync**", _handler)
    page.add_init_script("localStorage.setItem('memtomem_ctx_active_scope_id', 'p-off')")
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scopes": [_paused_scope()]}),
        ),
    )
    page.goto(mm_web_url)
    # Switch the active tier to user BEFORE the section loads so loadHooksSync
    # reads target_scope='user'.
    page.evaluate("() => { _ctxTargetScope = 'user'; }")
    _open_hooks_sync(page)

    # The eligibility gate must NOT fire on the user tier: no data-sync-ineligible
    # marker, button enabled.
    page.wait_for_function(
        "() => {"
        "  const b = document.getElementById('hooks-sync-btn');"
        "  return b && !b.disabled && !b.hasAttribute('data-sync-ineligible');"
        "}",
        timeout=4_000,
    )
    btn = page.locator("#hooks-sync-btn")
    assert btn.is_disabled() is False, (
        "user-tier Sync Now must stay enabled even when the active project is paused"
    )
    assert btn.get_attribute("data-sync-ineligible") is None
    assert MATRIX_PAUSED_TITLE not in (btn.get_attribute("title") or ""), (
        "user-tier Sync Now must not show the paused 'Projects board' tooltip"
    )
