"""Browser coverage for drag-to-choose-a-destination (#2297).

Dragging an artifact card onto another project's group header opens the
existing Move/Copy modal (#1289) with that project pre-filled; the drop itself
never transfers. The companion vitest spec
(``tests-js/ctx-move-copy-dnd.test.mjs``) pins the full eligibility matrix and
the refusal paths against synthetic events. What is worth a real browser here
is the part JSDOM cannot model: an actual HTML5 drag sequence driven by the
mouse, the modal's focus restore landing back on the dragged card, and an axe
pass over the list while a drag is in flight.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser

_NAME = "demo-skill"

# Two sync-eligible projects: the active Server CWD source and a cross-project
# destination. The default conftest roster is CWD-only, and the lists spec's
# second scope is not sync-eligible, so neither can serve as a drop target.
_ROSTER = {
    "scopes": [
        {
            "scope_id": "cwd-9f1c2a",
            "label": "Server CWD",
            "root": "/srv",
            "tier": "project",
            "sources": ["server-cwd"],
            "experimental": False,
            "missing": False,
            "stale": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 1, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
        {
            "scope_id": "proj-a",
            "label": "Project A",
            "root": "/work/a",
            "tier": "project",
            "sources": ["known-projects"],
            "experimental": False,
            "missing": False,
            "stale": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 0, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
    ],
    "target_scope": "project_shared",
}

_ITEMS = {
    "skills": [
        {
            "name": _NAME,
            "canonical_path": "/srv/.memtomem/skills/demo-skill.md",
            "target_scope": "project_shared",
            "runtimes": [{"runtime": "claude", "status": "in sync"}],
        }
    ],
    "scanned_dirs": [],
}

_DETAIL = {
    "content": "name: demo\n",
    "target_scope": "project_shared",
    "layout": "flat",
    "files": [],
    "mtime_ns": "1700000000000000000",
    "fields": {},
}

_PLAN = {
    "status": "plan",
    "transferred": False,
    "kind": "skills",
    "name": _NAME,
    "dst_name": _NAME,
    "mode": "copy",
    "from_scope": "project_shared",
    "to_scope": "project_shared",
    "src_project_scope_id": "cwd-9f1c2a",
    "dst_project_scope_id": "proj-a",
    "src_path": "/srv/.memtomem/skills/demo-skill.md",
    "dst_path": "/work/a/.memtomem/skills/demo-skill.md",
    "needs_sync": False,
    "sync_command": None,
    "notes": [],
}


def _stub(page) -> dict:
    """Roster, skills list, detail, and the transfer dry-run.

    The list route matters as much as the transfer one: without it the default
    empty payload renders no card, and there is nothing to drag.
    """
    # The dry-run and apply legs are recorded SEPARATELY. A drop previewing and
    # a drop transferring hit the same path and differ only by ``dry_run=1``, so
    # a stub that lumps them together cannot tell them apart — a real mutating
    # POST would land in the same list and every assertion would still pass.
    state: dict = {"dry": [], "apply": []}

    def _detail_or_transfer(route):
        req = route.request
        if req.method == "POST" and "/transfer" in req.url:
            leg = "dry" if "dry_run=1" in req.url else "apply"
            state[leg].append(json.loads(req.post_data or "{}"))
            route.fulfill(status=200, content_type="application/json", body=json.dumps(_PLAN))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_DETAIL))

    page.route(
        "**/api/context/projects**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ROSTER)
        ),
    )
    page.route(
        "**/api/context/skills?**",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ITEMS)
        ),
    )
    page.route(
        "**/api/context/skills",
        lambda route: route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_ITEMS)
        ),
    )
    page.route(f"**/api/context/skills/{_NAME}**", _detail_or_transfer)
    return state


def _seed_advanced(page) -> None:
    """Opt the gateway into Advanced BEFORE the page loads.

    The gateway's Simple/Advanced flag is separate from the app-wide one the
    conftest seeds, and Simple is the default-when-unset. The drag affordance is
    Advanced-only and is decided when the card HTML is built, so seeding it
    after ``goto`` would be too late.
    """
    page.add_init_script("localStorage.setItem('memtomem_ctx_simple_mode', '0')")


def _open_skills(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-skills')")
    page.wait_for_selector(f'#ctx-skills-list .ctx-card[data-name="{_NAME}"]', timeout=5_000)
    page.locator("#ctx-skills-show-all").check()
    page.wait_for_function(
        "() => document.querySelectorAll('#ctx-skills-list .ctx-scope-group').length > 1",
        timeout=5_000,
    )


_CARD = f'#ctx-skills-list .ctx-card[data-name="{_NAME}"]'
_TARGET = '#ctx-skills-list details[data-scope-id="proj-a"] > summary'


def test_real_drag_onto_a_project_header_opens_a_prefilled_modal(page, mm_web_url: str) -> None:
    """A browser-driven drag lands on the group header and pre-fills the modal."""
    install_default_stubs(page)
    state = _stub(page)
    _seed_advanced(page)
    page.goto(mm_web_url)
    _open_skills(page)

    page.drag_and_drop(_CARD, _TARGET)
    page.wait_for_function(
        "() => { const m = document.getElementById('ctx-move-copy-modal'); return m && !m.hidden; }",
        timeout=3_000,
    )
    assert page.locator("#ctx-mc-project").input_value() == "proj-a"
    assert (
        page.locator('#ctx-move-copy-modal input[name="ctx-mc-mode"]:checked').get_attribute(
            "value"
        )
        == "copy"
    )
    # The dry-run fired against the dropped destination, not the default.
    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-mc-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )
    assert state["dry"], "a dry-run preview must fire on open"
    assert state["dry"][0]["to_project_scope_id"] == "proj-a"
    # The drop previews and stops there. Applying is a separate, explicit click,
    # so an empty apply leg is what actually carries "a drop never transfers".
    assert state["apply"] == [], "a drop must not issue a real transfer"


def test_cancel_returns_focus_to_the_dragged_card(page, mm_web_url: str) -> None:
    """Escaping the modal puts focus back on the card the user picked up.

    The drop opens a focus-trapping modal from a pointer gesture, so without an
    explicit focus on the drag source the trap would capture ``body`` and
    release focus to nowhere.
    """
    install_default_stubs(page)
    _stub(page)
    _seed_advanced(page)
    page.goto(mm_web_url)
    _open_skills(page)

    page.drag_and_drop(_CARD, _TARGET)
    page.wait_for_function(
        "() => { const m = document.getElementById('ctx-move-copy-modal'); return m && !m.hidden; }",
        timeout=3_000,
    )
    page.locator("#ctx-mc-cancel-btn").click()
    page.wait_for_function(
        "() => document.getElementById('ctx-move-copy-modal').hidden === true", timeout=3_000
    )
    focused = page.evaluate("() => document.activeElement && document.activeElement.dataset.name")
    assert focused == _NAME


def test_drag_hover_marks_the_target_and_announces_it(page, mm_web_url: str) -> None:
    """A held drag over an eligible header shows the drop state and narrates it."""
    install_default_stubs(page)
    _stub(page)
    _seed_advanced(page)
    page.goto(mm_web_url)
    _open_skills(page)

    card = page.locator(_CARD)
    target = page.locator(_TARGET)
    card.hover()
    page.mouse.down()
    box = target.bounding_box()
    # Two moves: the first starts the drag, the second lands inside the target.
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2, steps=4)
    page.mouse.move(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2 + 1, steps=2)

    page.wait_for_function(
        """() => {
            const s = document.querySelector(
                '#ctx-skills-list details[data-scope-id="proj-a"] > summary');
            return s && s.classList.contains('ctx-drop-target--over');
        }""",
        timeout=3_000,
    )
    announced = page.locator("#ctx-drag-announce").text_content()
    assert "Project A" in announced
    assert _NAME in announced

    # An axe pass over what the drag ADDS, while the drag is live — the marked
    # drop target and the live region only exist in this state. The scope is the
    # drop target plus the announcer rather than the whole gateway: the gateway
    # carries pre-existing token-level contrast debt (the settings nav sub-label
    # and ``.badge-tier``) that is present with no drag in flight and belongs to
    # the design-token suite (``test_ui_refresh_contract`` /
    # ``test_ui_smoke_matrix``), not to this feature.
    axe_source = (Path(__file__).with_name("vendor") / "axe.min.js").read_text(encoding="utf-8")
    page.evaluate(f"() => {{ {axe_source} }}")
    page.evaluate(
        """() => {
            const s = document.createElement('style');
            s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
            document.head.appendChild(s);
        }"""
    )
    results = page.evaluate(
        """async () => await axe.run(
            { include: [
                ['#ctx-skills-list details[data-scope-id="proj-a"] > summary'],
                ['#ctx-drag-announce'],
            ] },
            {
                resultTypes: ['violations'],
                runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
            })"""
    )
    page.mouse.up()
    blocking = [v for v in results["violations"] if v.get("impact") in {"serious", "critical"}]
    assert blocking == [], json.dumps([v["id"] for v in blocking])
