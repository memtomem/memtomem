"""Browser tests for the localized write-time Gate A privacy block (#1651).

The canonical skills/commands/agents editor save (#1509 write-time Gate A)
refuses secret-shaped ``project_shared`` content with a 422 whose STRING
``detail`` is a raw-English, jargon-heavy Gate A message ("Gate A: … ADR-0011
§5 … target_scope=user"). #1651 hoists a top-level ``reason_code:
"privacy_blocked"`` sibling (the #1409 ``_sync_phase`` mechanism) so the client
shows a localized, jargon-free hint while keeping the raw English detail in a
hover tooltip for fidelity. These specs pin:

* EN save 422 → the localized editor hint, NOT the raw "Gate A"/"ADR-0011 §5"
  wall.
* The raw server detail survives verbatim in the toast ``.toast-msg`` ``title=``.
* KO locale renders its own hint (no English leak).
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


# The raw server detail the editor 422 carries — the path-free, issue-pinned
# string produced by ``format_scan_block_message`` (remediation_hint branch).
_GATE_A_DETAIL = (
    "Gate A: demo-skill contains 1 privacy pattern hit(s); write to "
    "scope='project_shared' rejected. git history is forever — no force bypass "
    "available for project_shared (ADR-0011 §5).\n"
    "  Remove the secret from the editor content, or keep the skill in your "
    "private user tier (target_scope=user)."
)

# Locale-pinned copy (en.json / ko.json are the source of truth).
EN_HINT = "A secret was detected in this content"
KO_HINT = "비밀값(secret)이 감지되어"


_SKILL_DETAIL = {
    "name": "demo-skill",
    "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
    "content": "name: demo\n",
    "mtime_ns": "1700000000000000000",
    "files": [],
}


def _open_skills(page) -> None:
    """Land on Settings → Skills and wait for the detail container to mount."""
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_selector("#ctx-skills-detail", state="attached", timeout=5_000)


def _stub_editor_save(page) -> None:
    """Method-branching handler for ``/api/context/skills/demo-skill`` — GET
    serves the detail payload (so the editor mounts); PUT returns the write-time
    Gate A privacy 422 with the hoisted ``reason_code`` sibling."""

    def _handler(route):
        if route.request.method == "PUT":
            route.fulfill(
                status=422,
                content_type="application/json",
                body=json.dumps({"detail": _GATE_A_DETAIL, "reason_code": "privacy_blocked"}),
            )
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _handler)


def _mount_and_save(page) -> None:
    """Open the demo-skill detail, enter edit mode, type, and click Save."""
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-edit-btn", timeout=4_000)
    page.locator("#ctx-skills-detail .ctx-detail-edit-btn").click()
    page.wait_for_selector("#ctx-skills-detail #ctx-edit-content", state="visible", timeout=3_000)
    page.locator("#ctx-skills-detail #ctx-edit-content").fill(
        "name: demo\nOPENAI_API_KEY=sk-secret\n"
    )
    page.locator("#ctx-skills-detail .ctx-edit-save").click()


def _stub_conflict_then_privacy(page) -> dict:
    """GET serves detail; the 1st PUT returns a 409 stale-mtime (opens the
    conflict modal), the 2nd PUT (Force save) returns the privacy 422 — force
    bypasses only the mtime guard, never Gate A."""
    state = {"puts": 0}

    def _handler(route):
        if route.request.method == "PUT":
            state["puts"] += 1
            if state["puts"] == 1:
                route.fulfill(
                    status=409,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "status": "aborted",
                            "reason": "File was modified by another process. Reload and retry.",
                            "mtime_ns": "1700000000000000001",
                            "error_kind": "conflict",
                            "reason_code": "stale_mtime",
                        }
                    ),
                )
            else:
                route.fulfill(
                    status=422,
                    content_type="application/json",
                    body=json.dumps({"detail": _GATE_A_DETAIL, "reason_code": "privacy_blocked"}),
                )
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _handler)
    return state


def _toast_text(page) -> str:
    toast = page.wait_for_selector("#toast-container .toast", timeout=3_000)
    return (toast.text_content() or "").strip()


def test_editor_privacy_block_localized_en(page, mm_web_url: str) -> None:
    """A save-time Gate A 422 renders the localized editor hint, and the raw
    English detail is preserved in the toast tooltip (never the visible text)."""
    install_default_stubs(page)
    _stub_editor_save(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _mount_and_save(page)

    text = _toast_text(page)
    assert EN_HINT in text, f"toast must surface the localized editor hint; got {text!r}"
    assert "Gate A" not in text, f"raw jargon must not reach the visible toast; got {text!r}"
    assert "ADR-0011" not in text, f"raw jargon must not reach the visible toast; got {text!r}"
    # The raw English server detail survives verbatim in the tooltip.
    title = page.locator("#toast-container .toast-msg").get_attribute("title") or ""
    assert "Gate A: demo-skill" in title, (
        f"raw detail must survive in the toast tooltip; got {title!r}"
    )


def test_editor_privacy_block_localized_ko(page, mm_web_url: str) -> None:
    """KO locale renders its own hint; the English copy must not leak."""
    install_default_stubs(page)
    _stub_editor_save(page)
    page.goto(mm_web_url)
    _open_skills(page)
    # The hint is resolved via ``t()`` at error time, so switch locale first.
    # ``setLang`` is async (it fetches ``/locales/ko.json``); awaiting it means
    # the KO catalog is loaded and active by the time we save.
    page.evaluate("async () => { await I18N.setLang('ko'); }")
    _mount_and_save(page)

    text = _toast_text(page)
    assert KO_HINT in text, f"KO toast must surface the KO hint; got {text!r}"
    assert "Gate A" not in text, f"raw jargon must not reach the visible toast; got {text!r}"
    assert EN_HINT not in text, f"EN copy must not leak under the KO locale; got {text!r}"


def test_editor_privacy_block_localized_on_conflict_force_save(page, mm_web_url: str) -> None:
    """force=true bypasses only the mtime guard, never Gate A: a secret saved
    through the conflict dialog's Force-save path must still surface the
    localized hint, not the raw English wall (#1651 conflict-path coverage)."""
    install_default_stubs(page)
    _stub_conflict_then_privacy(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _mount_and_save(page)
    # First save → 409 → the conflict modal opens; choose Force save, which
    # re-PUTs with force=true and trips Gate A.
    force = page.wait_for_selector("#ctx-conflict-force-btn", state="visible", timeout=4_000)
    force.click()

    text = _toast_text(page)
    assert EN_HINT in text, f"conflict Force-save must localize the privacy block; got {text!r}"
    assert "Gate A" not in text, f"raw jargon must not reach the visible toast; got {text!r}"
    title = page.locator("#toast-container .toast-msg").get_attribute("title") or ""
    assert "Gate A: demo-skill" in title, f"raw detail must survive in the tooltip; got {title!r}"
