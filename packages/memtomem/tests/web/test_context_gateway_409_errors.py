"""Browser tests for the structured 409 (#1210) write-guard handling on the
Context Gateway cascade-delete surface (follow-up to #1210).

Backend #1210 made the runtime-writing routes 409 a sync-ineligible project
with a STRUCTURED ``detail`` body::

    {"detail": {"reason_code": "sync_paused" | "sync_not_enrolled",
                "message": "...", "project_scope_id": "..."}}

Before this PR the cascade-delete error handler did
``showToast(err.detail || ...)`` — passing the *object* straight to
``showToast`` (which renders ``textContent``), so the user saw the literal
string ``[object Object]``. The ``_ctxErrDetail`` extractor maps the
``reason_code`` to a localized toast and falls back to ``detail.message`` /
a plain string. These specs pin:

* The cascade-delete 409 renders the localized paused / not-enrolled copy
  (and NEVER ``[object Object]``).
* An unknown ``reason_code`` falls back to the backend ``message``.
* The KO locale renders its own copy (translation + josa parity).
* §5a proactive gate: when the active project is sync-ineligible the Delete
  button stays ENABLED (the backend allows a canonical-only ``cascade=false``
  delete) but the cascade checkbox is hidden and the confirm explains the
  canonical-only delete — so the gated runtime fan-out 409 is avoided without
  blocking the delete the backend permits.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# Locale-pinned copy (en.json / ko.json are the source of truth).
EN_PAUSED = "Project sync is paused — resume it on the Projects board."
EN_NOT_ENROLLED = "Project is not active for sync — activate it on the Projects board."
KO_PAUSED = "프로젝트 동기화가 중지되었습니다 — 프로젝트 보드에서 재개하세요."
EN_CASCADE_HINT = (
    "Sync is paused or not active for this project, so only the stored "
    "copy is deleted — runtime files are left in place."
)


_SKILL_DETAIL = {
    "name": "demo-skill",
    "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
    "content": "name: demo\n",
    "mtime_ns": "1700000000000000000",
    "files": [],
}


def _paused_scope(scope_id: str = "p-off", *, enrolled: bool = True) -> dict:
    """A sync-ineligible project scope. ``enrolled`` toggles paused
    (known-projects + enabled:false) vs scan-only never-enrolled."""
    return {
        "scope_id": scope_id,
        "project_scope_id": scope_id,
        "label": "Paused" if enrolled else "Scanned",
        "root": "/work/off",
        "tier": "project",
        "sources": ["known-projects"] if enrolled else ["claude-projects"],
        "missing": False,
        "stale": False,
        "experimental": False,
        "enabled": False if enrolled else True,
        "sync_eligible": False,
        "counts": {"skills": 1, "commands": 0, "agents": 0},
    }


def _open_skills(page) -> None:
    """Land on Settings → Skills and wait for the detail container to mount."""
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_selector("#ctx-skills-detail", state="attached", timeout=5_000)


def _stub_cascade_delete(page, status_code: int, body: dict) -> dict:
    """Method-branching handler for ``/api/context/skills/demo-skill`` —
    GET serves the detail payload (so ``loadCtxDetail`` mounts the Delete
    button), DELETE returns the supplied response. Records DELETE URLs."""
    state: dict = {"delete_urls": []}

    def _handler(route):
        if route.request.method == "DELETE":
            state["delete_urls"].append(route.request.url)
            route.fulfill(
                status=status_code,
                content_type="application/json",
                body=json.dumps(body),
            )
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _handler)
    return state


def _mount_detail_and_delete(page, *, cascade: bool = True) -> None:
    """Open the demo-skill detail, click Delete, tick the cascade box, confirm."""
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-delete-btn", timeout=4_000)
    page.locator("#ctx-skills-detail .ctx-detail-delete-btn").click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden", timeout=2_000)
    if cascade:
        page.locator("#confirm-extra-checkbox").check()
    page.locator("#confirm-ok-btn").click()


def _toast_text(page) -> str:
    toast = page.wait_for_selector("#toast-container .toast", timeout=3_000)
    return (toast.text_content() or "").strip()


# ---------------------------------------------------------------------------
# §2a error-handling — the broken-now surface
# ---------------------------------------------------------------------------


def test_cascade_delete_409_sync_paused(page, mm_web_url: str) -> None:
    """sync_paused 409 → localized paused toast, never ``[object Object]``."""
    install_default_stubs(page)
    state = _stub_cascade_delete(
        page,
        409,
        {
            "detail": {
                "reason_code": "sync_paused",
                "message": "Project 'p-off' is not enrolled for sync (enrollment paused).",
                "project_scope_id": "p-off",
            }
        },
    )
    page.goto(mm_web_url)
    _open_skills(page)
    _mount_detail_and_delete(page)

    text = _toast_text(page)
    assert EN_PAUSED in text, f"toast must surface the localized paused copy; got {text!r}"
    assert "[object Object]" not in text, f"structured detail leaked as object; got {text!r}"
    assert any("cascade=true" in u for u in state["delete_urls"]), (
        f"cascade box must drive cascade=true DELETE; got {state['delete_urls']!r}"
    )


def test_cascade_delete_409_sync_not_enrolled(page, mm_web_url: str) -> None:
    """sync_not_enrolled 409 → localized not-enrolled toast."""
    install_default_stubs(page)
    _stub_cascade_delete(
        page,
        409,
        {
            "detail": {
                "reason_code": "sync_not_enrolled",
                "message": "Project 'p-off' is not enrolled (discovery-only).",
                "project_scope_id": "p-off",
            }
        },
    )
    page.goto(mm_web_url)
    _open_skills(page)
    _mount_detail_and_delete(page)

    text = _toast_text(page)
    assert EN_NOT_ENROLLED in text, f"toast must surface the not-enrolled copy; got {text!r}"
    assert "[object Object]" not in text


def test_cascade_delete_409_unknown_reason_falls_back_to_message(page, mm_web_url: str) -> None:
    """An unrecognized reason_code falls back to the backend ``message`` —
    proving the extractor degrades gracefully rather than dropping to the
    generic fallback or leaking the object."""
    install_default_stubs(page)
    _stub_cascade_delete(
        page,
        409,
        {
            "detail": {
                "reason_code": "some_future_code",
                "message": "Backend-specific explanation here.",
                "project_scope_id": "p-off",
            }
        },
    )
    page.goto(mm_web_url)
    _open_skills(page)
    _mount_detail_and_delete(page)

    text = _toast_text(page)
    assert "Backend-specific explanation here." in text, (
        f"unknown reason_code must fall back to detail.message; got {text!r}"
    )
    assert "[object Object]" not in text


def test_cascade_delete_409_sync_paused_ko(page, mm_web_url: str) -> None:
    """KO locale renders its own paused copy (translation + josa parity)."""
    install_default_stubs(page)
    _stub_cascade_delete(
        page,
        409,
        {
            "detail": {
                "reason_code": "sync_paused",
                "message": "English message that must NOT appear when lang=ko.",
                "project_scope_id": "p-off",
            }
        },
    )
    page.goto(mm_web_url)
    _open_skills(page)
    # Toast copy is resolved via ``t()`` at error time, so switch locale before
    # triggering the delete. ``setLang`` is async (it fetches ``/locales/ko.json``),
    # so we await it — by the time it resolves the KO catalog is loaded and the
    # active locale is switched.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    _mount_detail_and_delete(page)

    text = _toast_text(page)
    assert KO_PAUSED in text, f"KO toast must surface the KO paused copy; got {text!r}"
    assert "[object Object]" not in text
    # The English backend message must not leak when a reason_code maps.
    assert "English message" not in text


# ---------------------------------------------------------------------------
# §5a proactive gate — disable Delete on a sync-ineligible active scope
# ---------------------------------------------------------------------------


def _run_canonical_only_delete(page, mm_web_url: str, scope: dict, active_scope_id: str) -> None:
    """Shared body for the ineligible-scope cases: Delete stays ENABLED (the
    backend allows a canonical-only `cascade=false` delete), but the cascade
    checkbox is hidden and the confirm message explains canonical-only delete.
    Confirming issues a `cascade=false` DELETE that succeeds."""
    install_default_stubs(page)

    def _detail_handler(route):
        if route.request.method == "DELETE":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"deleted": True}),
            )
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _detail_handler)
    page.add_init_script(
        f"localStorage.setItem('memtomem_ctx_active_scope_id', {active_scope_id!r})"
    )
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scopes": [scope]}),
        ),
    )
    page.goto(mm_web_url)
    _open_skills(page)
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")

    # Delete stays ENABLED — a canonical-only delete is allowed on a paused /
    # not-enrolled scope (only the runtime fan-out is gated by #1210).
    btn = page.wait_for_selector("#ctx-skills-detail .ctx-detail-delete-btn", timeout=4_000)
    assert btn.is_disabled() is False, (
        "Delete must stay enabled on an ineligible scope — canonical-only delete is allowed"
    )
    assert btn.get_attribute("disabled") is None

    btn.click()
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden", timeout=2_000)
    # The cascade checkbox row must be HIDDEN (no runtime fan-out while ineligible)
    # and the confirm message must explain the canonical-only delete.
    assert page.locator("#confirm-extra-row").is_hidden(), (
        "cascade option must be hidden when the active scope is sync-ineligible"
    )
    msg = page.locator("#confirm-message").text_content() or ""
    assert EN_CASCADE_HINT in msg, f"confirm must explain the canonical-only delete; got {msg!r}"

    # Confirming issues a canonical (cascade=false) DELETE; the success path hides
    # the detail pane.
    with page.expect_request(
        lambda r: "/api/context/skills/demo-skill" in r.url and r.method == "DELETE",
        timeout=4_000,
    ) as req_info:
        page.locator("#confirm-ok-btn").click()
    delete_req = req_info.value
    assert "cascade=false" in delete_req.url, (
        f"an ineligible scope must delete canonical-only (cascade=false); got {delete_req.url!r}"
    )
    page.wait_for_selector("#ctx-skills-detail", state="hidden", timeout=3_000)


def test_cascade_delete_paused_scope_offers_canonical_only_delete(page, mm_web_url: str) -> None:
    """A paused active project keeps Delete enabled but drops the cascade option
    and runs a canonical-only delete (which the backend allows)."""
    _run_canonical_only_delete(page, mm_web_url, _paused_scope(), "p-off")


def test_cascade_delete_not_enrolled_scope_offers_canonical_only_delete(
    page, mm_web_url: str
) -> None:
    """A scan-only (never-enrolled) active project behaves the same — cascade
    hidden, canonical-only delete proceeds."""
    _run_canonical_only_delete(page, mm_web_url, _paused_scope("p-scan", enrolled=False), "p-scan")
