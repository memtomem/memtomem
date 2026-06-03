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
  button is rendered disabled with the matrix tooltip, so the 409 round-trip
  is avoided entirely.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# Locale-pinned copy (en.json / ko.json are the source of truth).
EN_PAUSED = "Project sync is paused — resume it on the Projects board."
EN_NOT_ENROLLED = "Project is not enrolled for sync — enroll it on the Projects board."
KO_PAUSED = "프로젝트 동기화가 중지되었습니다 — 프로젝트 보드에서 재개하세요."
MATRIX_PAUSED_TITLE = "Sync paused — resume it on the Projects board"
MATRIX_NOT_ENROLLED_TITLE = "Not enrolled — enroll this project on the Projects board to sync it"


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


def test_cascade_delete_button_disabled_when_scope_paused(page, mm_web_url: str) -> None:
    """When the active project is paused, the Delete button renders disabled
    with the matrix paused tooltip — the 409 round-trip is avoided."""
    install_default_stubs(page)
    delete_calls: list[str] = []

    def _detail_handler(route):
        if route.request.method == "DELETE":
            delete_calls.append(route.request.url)
            route.fulfill(status=200, content_type="application/json", body="{}")
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _detail_handler)
    # Active scope = the paused project, set before module init reads localStorage.
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
    _open_skills(page)
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")

    btn = page.wait_for_selector(
        "#ctx-skills-detail .ctx-detail-delete-btn[disabled]", timeout=4_000
    )
    assert btn.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_paused_title", (
        "disabled Delete must carry the matrix paused i18n-title key"
    )
    assert MATRIX_PAUSED_TITLE in (btn.get_attribute("title") or ""), (
        f"disabled Delete title must be the resolved paused copy; got {btn.get_attribute('title')!r}"
    )
    assert delete_calls == [], f"disabled Delete must not issue a DELETE; saw {delete_calls!r}"


def test_cascade_delete_button_disabled_when_scope_not_enrolled(page, mm_web_url: str) -> None:
    """A scan-only (never-enrolled) active scope uses the not-enrolled
    tooltip variant on the disabled Delete button."""
    install_default_stubs(page)
    page.route(
        "**/api/context/skills/demo-skill**",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL)
        ),
    )
    page.add_init_script("localStorage.setItem('memtomem_ctx_active_scope_id', 'p-scan')")
    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"scopes": [_paused_scope("p-scan", enrolled=False)]}),
        ),
    )
    page.goto(mm_web_url)
    _open_skills(page)
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")

    btn = page.wait_for_selector(
        "#ctx-skills-detail .ctx-detail-delete-btn[disabled]", timeout=4_000
    )
    assert btn.get_attribute("data-i18n-title") == "settings.ctx.matrix_sync_not_enrolled_title", (
        "disabled Delete must carry the matrix not-enrolled i18n-title key"
    )
    assert MATRIX_NOT_ENROLLED_TITLE in (btn.get_attribute("title") or "")
