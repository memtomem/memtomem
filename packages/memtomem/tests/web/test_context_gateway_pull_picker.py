"""Browser tests for the source-selectable Pull picker (ADR-0030 PR-D2).

Route-stubbed coverage of the modal that drives
``GET /api/context/{kind}/{name}/pull-preview`` → ``POST …/pull``:

* opening the modal fires the preview and auto-selects the unambiguous source,
  enabling Apply;
* divergent copies (§5 ambiguous) leave Apply disabled until the user names a
  source;
* applying threads the ``confirm_project_shared`` round-trip through the shared
  confirm dialog, then re-POSTs with the flag and toasts on success;
* the open modal has no serious/critical axe violations.

The companion vitest spec (``tests-js/ctx-pull-modal.test.mjs``) pins the finer
state machine (overwrite/force visibility, CSRF, tier re-preview); these specs
pin the real-DOM click → modal → request flow.
"""

from __future__ import annotations

import json
from pathlib import Path

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


def _candidate(runtime, content_status, gate_status, group):
    return {
        "runtime": runtime,
        "content_status": content_status,
        "gate_status": gate_status,
        "importable": True,
        "landing_group": group,
        "override_warning": False,
        "reason": None,
    }


def _preview(**over):
    body = {
        "kind": "skills",
        "name": "demo-skill",
        "target_scope": "project_shared",
        "store_present": False,
        "candidates": [_candidate("claude", "new", "ok", 0)],
        "distinct_landing_count": 1,
        "ambiguous": False,
        "auto_source": "claude",
    }
    body.update(over)
    return body


_APPLIED = {
    "status": "applied",
    "kind": "skills",
    "name": "demo-skill",
    "target_scope": "project_shared",
    "reason": "ok",
    "reason_code": None,
    "selected_runtime": "claude",
    "write_outcome": "created",
    "duplicate_runtimes": [],
    "canonical_path": ".memtomem/skills/demo-skill",
    "candidates": [],
    "distinct_landing_count": 0,
    "gate_status": None,
    "gate_hits": None,
    "force_bypassable": False,
}
_NEEDS_CONFIRM = {
    "status": "needs_confirmation",
    "confirm": "confirm_project_shared",
    "reason": "raw dev prose",
    "host_targets": [],
}


def _open_skills(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_selector("#ctx-skills-detail", state="attached", timeout=5_000)


def _stub_pull(page, *, preview_body=None, apply_bodies=None) -> dict:
    """Leg-branching handler for ``/api/context/skills/demo-skill**``.

    GET ``…/pull-preview`` → the preview; POST ``…/pull`` → the next apply body
    (sticky on the last); any other GET → the detail (so ``loadCtxDetail`` mounts
    the Pull button)."""
    preview = preview_body if preview_body is not None else _preview()
    applies = list(apply_bodies or [_APPLIED])
    state: dict = {"preview": [], "apply": []}

    def _handler(route):
        req = route.request
        if "/pull-preview" in req.url:
            state["preview"].append(req.url)
            route.fulfill(status=200, content_type="application/json", body=json.dumps(preview))
            return
        if req.method == "POST" and req.url.split("?")[0].endswith("/pull"):
            state["apply"].append(json.loads(req.post_data or "{}"))
            body = applies[min(len(state["apply"]) - 1, len(applies) - 1)]
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_SKILL_DETAIL))

    page.route("**/api/context/skills/demo-skill**", _handler)
    return state


def _open_modal(page) -> None:
    page.evaluate("() => { loadCtxDetail('skills', 'demo-skill'); }")
    page.wait_for_selector("#ctx-skills-detail .ctx-detail-pull-btn", timeout=4_000)
    page.locator("#ctx-skills-detail .ctx-detail-pull-btn").click()
    page.wait_for_function(
        "() => { const m = document.getElementById('ctx-pull-modal'); return m && !m.hidden; }",
        timeout=3_000,
    )


def test_pull_preview_auto_selects_and_enables_apply(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    state = _stub_pull(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)

    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-pull-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )
    assert state["preview"], "a preview must fire on open"
    assert all("target_scope=project_shared" in u for u in state["preview"])
    assert page.locator(
        '#ctx-pull-modal input[name="ctx-pull-source"][value="claude"]'
    ).is_checked()


def test_pull_ambiguous_requires_source_pick(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_pull(
        page,
        preview_body=_preview(
            ambiguous=True,
            auto_source=None,
            distinct_landing_count=2,
            candidates=[
                _candidate("claude", "differs", "ok", 0),
                _candidate("codex", "new", "ok", 1),
            ],
        ),
    )
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)

    apply_btn = page.locator("#ctx-pull-apply-btn")
    page.wait_for_function(
        "() => document.getElementById('ctx-pull-apply-btn').disabled === true", timeout=3_000
    )
    assert apply_btn.is_disabled()
    page.locator('#ctx-pull-modal input[name="ctx-pull-source"][value="codex"]').check()
    page.wait_for_function(
        "() => document.getElementById('ctx-pull-apply-btn').disabled === false", timeout=3_000
    )


def test_pull_apply_confirms_shared_and_succeeds(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    state = _stub_pull(page, apply_bodies=[_NEEDS_CONFIRM, _APPLIED])
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)
    page.wait_for_function(
        "() => document.getElementById('ctx-pull-apply-btn').disabled === false", timeout=3_000
    )
    page.locator("#ctx-pull-apply-btn").click()

    # The shared-store confirm dialog opens; approve it.
    page.wait_for_function(
        "() => { const m = document.getElementById('confirm-modal'); return m && !m.hidden; }",
        timeout=3_000,
    )
    page.locator("#confirm-ok-btn").click()

    page.wait_for_selector("#toast-container .toast", timeout=3_000)
    assert len(state["apply"]) == 2, "a confirmed apply re-POSTs after the gate"
    assert "confirm_project_shared" not in state["apply"][0]
    assert state["apply"][1]["confirm_project_shared"] is True


def test_pull_picker_a11y(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_pull(page)
    page.goto(mm_web_url)
    _open_skills(page)
    _open_modal(page)
    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-pull-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )

    axe_source = (Path(__file__).with_name("vendor") / "axe.min.js").read_text(encoding="utf-8")
    page.evaluate(f"() => {{ {axe_source} }}")
    # Freeze CSS transitions/animations before scanning. Apply is toggled
    # disabled→enabled as the preview resolves, and ``.btn-primary``'s background
    # animates (``--motion-base``) from the disabled grey to the accent fill;
    # sampling axe mid-transition reads a transient low-contrast intermediate,
    # not the settled ``--accent-fill`` (#315fd5 light / #3b63e8 dark, both AA per
    # ``test_ui_refresh_contract``). With motion frozen, the WHOLE modal —
    # including the shared action buttons — passes, so nothing is excluded.
    page.evaluate(
        """() => {
            const s = document.createElement('style');
            s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
            document.head.appendChild(s);
        }"""
    )
    results = page.evaluate(
        """async () => await axe.run('#ctx-pull-modal', {
                resultTypes: ['violations'],
                runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
            })"""
    )
    blocking = [v for v in results["violations"] if v.get("impact") in {"serious", "critical"}]
    assert blocking == [], json.dumps([v["id"] for v in blocking])
