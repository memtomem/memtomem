"""Browser test pinning the Context Gateway mobile layout (issue #1072).

At 390px viewport the two-pane Settings/Gateway layout used to leave only
~178px of usable content width, clipping overview cards and pushing the
Sync All button past the viewport edge. The fix in ``style.css`` adds a
``@media (max-width: 520px)`` block that stacks the layout single-column
and collapses the overview grid to one column.

This test asserts:

* No horizontal page overflow at 390px.
* ``#ctx-sync-all-btn`` fits inside the viewport.
* The settings nav is no longer occupying the 180px sidebar slot — the
  content pane takes the full viewport width.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_EMPTY_OVERVIEW = {
    "skills": {"total": 0, "in_sync": 0},
    "commands": {"total": 0, "in_sync": 0},
    "agents": {"total": 0, "in_sync": 0},
    "settings": {"total": 0, "in_sync": 0, "out_of_sync": 0, "missing_target": 0},
    "hooks": {"total": 0, "in_sync": 0, "pending": 0, "conflicts": 0},
    "root": "/tmp/mtm-mobile-pin",
    "runtimes": [],
    "last_sync_at": None,
}


def test_gateway_mobile_390px_no_overflow(page, mm_web_url) -> None:
    page.set_viewport_size({"width": 390, "height": 844})
    install_default_stubs(page)
    page.route(
        "**/api/context/overview",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_EMPTY_OVERVIEW),
        ),
    )

    page.goto(mm_web_url, wait_until="domcontentloaded")
    page.wait_for_selector(".tab-nav .tab-btn", timeout=5_000)

    page.locator('.tab-btn[data-tab="context-gateway"]').click()
    page.wait_for_function(
        "() => document.querySelector('.tab-btn.active')?.dataset.tab === 'context-gateway'",
        timeout=4_000,
    )
    page.wait_for_selector("#ctx-sync-all-btn", timeout=4_000)

    metrics = page.evaluate(
        """
        () => {
          const root = document.documentElement;
          const btn = document.getElementById('ctx-sync-all-btn');
          const content = document.querySelector(
            '#tab-context-gateway .settings-content'
          );
          const layout = document.querySelector(
            '#tab-context-gateway .settings-layout'
          );
          const btnRect = btn ? btn.getBoundingClientRect() : null;
          return {
            innerWidth: window.innerWidth,
            scrollWidth: root.scrollWidth,
            btnRight: btnRect ? btnRect.right : null,
            contentWidth: content ? content.getBoundingClientRect().width : null,
            layoutDirection: layout
              ? getComputedStyle(layout).flexDirection
              : null,
          };
        }
        """
    )

    assert metrics["scrollWidth"] <= metrics["innerWidth"], (
        f"horizontal overflow at 390px: scrollWidth={metrics['scrollWidth']} "
        f"> innerWidth={metrics['innerWidth']}"
    )
    assert metrics["btnRight"] is not None
    assert metrics["btnRight"] <= metrics["innerWidth"], (
        f"#ctx-sync-all-btn extends past viewport: right={metrics['btnRight']} "
        f"> innerWidth={metrics['innerWidth']}"
    )
    assert metrics["layoutDirection"] == "column", (
        f"settings-layout should stack to column at 390px, got {metrics['layoutDirection']!r}"
    )
    assert metrics["contentWidth"] is not None
    assert metrics["contentWidth"] >= metrics["innerWidth"] - 1, (
        "settings-content should span the full viewport width at 390px, got "
        f"contentWidth={metrics['contentWidth']} vs "
        f"innerWidth={metrics['innerWidth']}"
    )
