"""Browser tests for the user-tier Global library section (ADR-0030 PR-F2).

Route-stubbed coverage of the section fed by ``GET /api/context/status-global``
(PR-F1):

* it renders the global-library inventory counts, the pull-direction drift rows
  + badge, and lights the sidebar glance-dot on ``has_pull_drift``;
* a leaf Pull button opens the SHARED pull modal defaulted to the USER tier;
* the JS-owned rows re-localize on ``langchange`` without a refetch;
* the rendered section has no serious/critical axe violations.

The finer loader state (supersession, error fallback) is pinned by the vitest
spec ``tests-js/ctx-global-section.test.mjs``; these pin the real-DOM
switch → fetch → render flow.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_DRIFT = {
    "scope": "user",
    "store": {"skills": 2, "agents": 1, "commands": 0},
    "runtime_coverage": [
        {"name": "claude", "available": True, "installed": True, "memtomem_registered": True},
        {"name": "codex", "available": False, "installed": None, "memtomem_registered": None},
    ],
    "pull_drift": {
        "has_pull_drift": True,
        "total": 2,
        "differs": 1,
        "errors": 0,
        "identical": 1,
        "rows": [
            {
                "kind": "skills",
                "name": "reviewer",
                "verdict": "differs",
                "runtimes": ["claude"],
                "reason": None,
            },
            {
                "kind": "agents",
                "name": "planner",
                "verdict": "identical",
                "runtimes": [],
                "reason": None,
            },
        ],
    },
}

_PREVIEW = {
    "kind": "skills",
    "name": "reviewer",
    "target_scope": "user",
    "store_present": True,
    "candidates": [],
    "distinct_landing_count": 0,
    "ambiguous": False,
    "auto_source": None,
}


def _stub_global(page, body=None) -> list[str]:
    calls: list[str] = []

    def _handler(route):
        calls.append(route.request.url)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body or _DRIFT))

    page.route("**/api/context/status-global**", _handler)
    return calls


def _open_global(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-global')")
    page.wait_for_selector("#ctx-global-content", state="attached", timeout=5_000)


def test_global_section_renders_inventory_and_drift(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_global(page)
    page.goto(mm_web_url)
    _open_global(page)
    page.wait_for_selector(".ctx-global-row", timeout=4_000)

    inv = page.locator(".ctx-global-inventory").text_content() or ""
    assert "2" in inv  # skills inventory count

    assert page.locator(".ctx-global-badge--differs").count() == 1
    assert page.locator(".ctx-global-badge--identical").count() == 1
    # Only the differs row offers a leaf Pull.
    assert page.locator(".ctx-global-pull-btn").count() == 1

    page.wait_for_function(
        "() => { const d = document.querySelector("
        "'.settings-nav-btn[data-section=\"ctx-global\"] .ctx-global-nav-dot'); "
        "return d && !d.hidden; }",
        timeout=3_000,
    )


def test_global_leaf_pull_opens_user_tier_modal(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_global(page)
    page.route(
        "**/pull-preview**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(_PREVIEW)),
    )
    page.goto(mm_web_url)
    _open_global(page)
    page.wait_for_selector(".ctx-global-pull-btn", timeout=4_000)

    page.locator(".ctx-global-pull-btn").first.click()
    page.wait_for_selector("#ctx-pull-modal", state="visible", timeout=3_000)

    # A user-tier drift row must open Pull defaulted to the user tier.
    checked = page.evaluate(
        "() => { const el = document.querySelector('input[name=\"ctx-pull-tier\"]:checked'); "
        "return el && el.value; }"
    )
    assert checked == "user"


def test_global_rows_relocalize_on_langchange_without_refetch(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    calls = _stub_global(page)
    page.goto(mm_web_url)
    _open_global(page)
    page.wait_for_selector(".ctx-global-badge--differs", timeout=4_000)

    pre = (page.locator(".ctx-global-badge--differs").text_content() or "").strip()
    assert pre == "differs", f"EN verdict badge should read 'differs', got {pre!r}"
    initial_calls = len(calls)

    page.evaluate("async () => { await I18N.setLang('ko'); }")
    page.wait_for_function(
        "() => { const b = document.querySelector('.ctx-global-badge--differs'); "
        "return b && b.textContent.trim() === '다름'; }",
        timeout=3_000,
    )
    post = (page.locator(".ctx-global-badge--differs").text_content() or "").strip()
    assert post == "다름"
    # Cache-driven re-render — no second probe on the locale toggle.
    assert len(calls) == initial_calls, (
        f"langchange must repaint from cache, not refetch; "
        f"baseline={initial_calls}, after={len(calls)}"
    )


def test_cold_gateway_open_flags_drift_on_nav_dot(page, mm_web_url: str) -> None:
    """ADR-0030 §1: detection at portal open. Opening the gateway and landing on
    ANOTHER section must still light the Global nav dot via the eager probe —
    without rendering the section body (nav-only)."""
    install_default_stubs(page)
    _stub_global(page)
    page.goto(mm_web_url)
    page.evaluate("() => activateTab('context-gateway')")  # lands on Overview, not Global

    page.wait_for_function(
        "() => { const d = document.querySelector("
        "'.settings-nav-btn[data-section=\"ctx-global\"] .ctx-global-nav-dot'); "
        "return d && !d.hidden; }",
        timeout=3_000,
    )
    # Eager = nav-only: the section rows are NOT rendered until the user opens it.
    assert page.locator(".ctx-global-row").count() == 0


def test_user_pull_from_global_refreshes_section(page, mm_web_url: str) -> None:
    """A successful user-tier Pull launched from a Global drift row must refresh
    the section (re-probe status-global) so the reconciled row/dot don't linger
    stale (Codex F2 Major)."""
    install_default_stubs(page)
    _stub_global(page)

    applied = {
        "status": "applied",
        "kind": "skills",
        "name": "reviewer",
        "target_scope": "user",
        "reason": "ok",
        "reason_code": None,
        "selected_runtime": "claude",
        "write_outcome": "created",
        "duplicate_runtimes": [],
        "canonical_path": "~/.memtomem/skills/reviewer",
        "candidates": [],
        "distinct_landing_count": 0,
        "gate_status": None,
        "gate_hits": None,
        "force_bypassable": False,
    }
    preview = {
        "kind": "skills",
        "name": "reviewer",
        "target_scope": "user",
        "store_present": False,
        "candidates": [
            {
                "runtime": "claude",
                "content_status": "new",
                "gate_status": "ok",
                "importable": True,
                "landing_group": 0,
                "override_warning": False,
                "reason": None,
            }
        ],
        "distinct_landing_count": 1,
        "ambiguous": False,
        "auto_source": "claude",
    }

    def _pull_handler(route):
        req = route.request
        if "/pull-preview" in req.url:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(preview))
        elif req.method == "POST" and req.url.split("?")[0].endswith("/pull"):
            route.fulfill(status=200, content_type="application/json", body=json.dumps(applied))
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps({}))

    page.route("**/api/context/skills/reviewer**", _pull_handler)
    page.goto(mm_web_url)
    _open_global(page)
    page.wait_for_selector(".ctx-global-pull-btn", timeout=4_000)

    page.locator(".ctx-global-pull-btn").first.click()
    page.wait_for_selector("#ctx-pull-modal", state="visible", timeout=3_000)
    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-pull-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )

    # Applying must trigger a fresh status-global probe (the section refresh).
    with page.expect_request("**/api/context/status-global**", timeout=3_000):
        page.locator("#ctx-pull-apply-btn").click()


def test_global_section_a11y(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_global(page)
    page.goto(mm_web_url)
    _open_global(page)
    page.wait_for_selector(".ctx-global-row", timeout=4_000)

    axe_source = (Path(__file__).with_name("vendor") / "axe.min.js").read_text(encoding="utf-8")
    page.evaluate(f"() => {{ {axe_source} }}")
    # Freeze transitions/animations so a badge/pill mid-transition intermediate
    # color isn't sampled as a contrast failure (PR-E axe-flake lesson).
    page.evaluate(
        """() => {
            const s = document.createElement('style');
            s.textContent = '*,*::before,*::after{transition:none!important;animation:none!important}';
            document.head.appendChild(s);
        }"""
    )
    results = page.evaluate(
        """async () => await axe.run('#settings-ctx-global', {
                resultTypes: ['violations'],
                runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21aa'] },
            })"""
    )
    blocking = [v for v in results["violations"] if v.get("impact") in {"serious", "critical"}]
    assert blocking == [], json.dumps([v["id"] for v in blocking])
