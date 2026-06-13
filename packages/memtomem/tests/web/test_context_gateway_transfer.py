"""Browser tests for the per-artifact Move/Copy destination modal (B-6 #1289).

Route-stubbed coverage of the modal that drives
``POST /api/context/{kind}/{name}/transfer`` (the A-5 #1276 endpoint):

* opening the modal runs a dry-run preview (``?dry_run=1``) and a clean plan
  enables Apply;
* a 409 ``destination_exists`` collision shows the inline warning and keeps
  Apply disabled (the endpoint has no overwrite — a collision is terminal);
* applying to ``project_shared`` threads the ``confirm_project_shared``
  round-trip through the shared confirm dialog, then re-POSTs with the flag.

The companion vitest spec (``tests-js/ctx-move-copy-modal.test.mjs``) pins the
finer state-machine wiring (CSRF header, host-write gate, rename copy-only,
destination pinning); these specs pin the real-DOM click → modal → request flow.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_SKILL_DETAIL = {
    "name": "demo-skill",
    "canonical_path": "/srv/cwd/.memtomem/skills/demo-skill.md",
    "content": "name: demo\n",
    "target_scope": "project_shared",
    "layout": "flat",
    "mtime_ns": "1700000000000000000",
    "files": [],
    "fields": {},
}

_PLAN = {
    "status": "plan",
    "transferred": False,
    "kind": "skills",
    "name": "demo-skill",
    "dst_name": "demo-skill",
    "mode": "copy",
    "from_scope": "project_shared",
    "to_scope": "project_local",
    "src_project_scope_id": "",
    "dst_project_scope_id": "",
    "src_path": "/srv/.memtomem/skills/demo-skill.md",
    "dst_path": "/srv/.memtomem/skills-local/demo-skill.md",
    "needs_sync": False,
    "sync_command": None,
    "notes": [],
}

_COLLISION = {
    "detail": {
        "error_kind": "conflict",
        "reason_code": "destination_exists",
        "message": "destination already exists: /srv/.memtomem/skills-local/demo-skill.md",
    }
}


def _open_skills(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_selector("#ctx-skills-detail", state="attached", timeout=5_000)


def _stub_transfer(
    page,
    *,
    dry_status: int = 200,
    dry_body: dict | None = None,
    apply_bodies: list[dict] | None = None,
) -> dict:
    """Method/leg-branching handler for ``/api/context/skills/demo-skill**``.

    GET → the detail payload (so ``loadCtxDetail`` mounts the Move/Copy button);
    POST ``?dry_run=1`` → ``dry_body`` (default a clean plan) with ``dry_status``;
    POST without dry_run → the next ``apply_bodies`` entry (sticky on the last).
    """
    plan = dry_body if dry_body is not None else _PLAN
    applies = list(apply_bodies or [{**_PLAN, "status": "ok", "transferred": True}])
    state: dict = {"dry": [], "apply": []}

    def _handler(route):
        req = route.request
        if req.method == "POST" and "/transfer" in req.url:
            if "dry_run" in req.url:
                state["dry"].append(req.url)
                route.fulfill(
                    status=dry_status, content_type="application/json", body=json.dumps(plan)
                )
            else:
                state["apply"].append(json.loads(req.post_data or "{}"))
                body = applies[min(len(state["apply"]) - 1, len(applies) - 1)]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _handler)
    return state


def _open_modal(page) -> None:
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-move-copy-btn", timeout=4_000)
    page.locator("#ctx-skills-detail .ctx-detail-move-copy-btn").click()
    page.wait_for_function(
        "() => { const m = document.getElementById('ctx-move-copy-modal'); return m && !m.hidden; }",
        timeout=3_000,
    )


def test_transfer_dry_run_preview_enables_apply(page, mm_web_url: str) -> None:
    """Opening the modal runs a dry-run; a clean plan enables Apply."""
    install_default_stubs(page)
    state = _stub_transfer(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)

    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-mc-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )
    assert state["dry"], "a dry-run preview must fire on open"
    assert all("dry_run" in u for u in state["dry"])


def test_transfer_collision_disables_apply(page, mm_web_url: str) -> None:
    """A 409 destination_exists collision shows the warning and disables Apply."""
    install_default_stubs(page)
    state = _stub_transfer(page, dry_status=409, dry_body=_COLLISION)
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)

    page.wait_for_function(
        "() => { const w = document.getElementById('ctx-mc-warning'); return w && !w.hidden; }",
        timeout=3_000,
    )
    assert page.locator("#ctx-mc-apply-btn").is_disabled()
    assert state["apply"] == [], "Apply must be unreachable while disabled"


def test_transfer_project_shared_gate_round_trip(page, mm_web_url: str) -> None:
    """project_shared destination → confirm_project_shared round-trip applies."""
    install_default_stubs(page)
    state = _stub_transfer(
        page,
        apply_bodies=[
            {
                "status": "needs_confirmation",
                "confirm": "confirm_project_shared",
                "reason": "Writes the canonical into the git-tracked project_shared tier.",
                "plan": _PLAN,
            },
            {**_PLAN, "status": "ok", "transferred": True, "to_scope": "project_shared"},
        ],
    )
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)

    # Target the shared tier; the change fires a fresh dry-run that re-enables Apply.
    page.locator('#ctx-move-copy-modal input[name="ctx-mc-tier"][value="project_shared"]').check()
    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-mc-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )
    page.locator("#ctx-mc-apply-btn").click()

    # The gate surfaces through the shared confirm dialog (not an error).
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden", timeout=3_000)
    page.locator("#confirm-ok-btn").click()

    # The success toast fires only after the confirmed re-POST applies — wait on
    # it (the modal is already hidden from the gate step, so its hidden state is
    # not a reliable "done" signal).
    page.wait_for_selector("#toast-container .toast", timeout=3_000)
    # Two apply POSTs: unconfirmed, then confirmed.
    assert len(state["apply"]) == 2
    assert state["apply"][0].get("confirm_project_shared") is False
    assert state["apply"][1].get("confirm_project_shared") is True
