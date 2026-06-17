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
* The overview grid resolves to one column at 480px. At 390px the base
  ``minmax(200px, 1fr)`` already happens to pick one track due to the
  narrow content width, which masks the cascade-order regression Codex
  caught — the 480px assertion is what actually pins it.
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
    # The Gateway now lands on Projects by default; this test pins the
    # Overview section's mobile layout, so switch to Overview explicitly.
    page.evaluate("() => _ctxSetSimpleMode(false)")  # ADR-0026 D-F: Advanced for the grid layout
    page.evaluate("() => switchSettingsSection('ctx-overview')")
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


def test_gateway_overview_grid_single_column_at_480px(page, mm_web_url) -> None:
    """At 480px the override must beat the base auto-fill rule.

    The cascade-order bug (Codex P2 on PR #1087) lived in the 436–520px
    band: the @media block was placed BEFORE the base
    ``.ctx-overview-grid { grid-template-columns: repeat(auto-fill,
    minmax(200px, 1fr)); }`` rule, so the equal-specificity base rule
    won source order and produced two columns. Pin this by asserting
    the resolved track count at 480px.
    """
    page.set_viewport_size({"width": 480, "height": 800})
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
    # The Gateway now lands on Projects by default; this test pins the
    # Overview grid's column count, so switch to Overview explicitly.
    page.evaluate("() => _ctxSetSimpleMode(false)")  # ADR-0026 D-F: Advanced for the grid layout
    page.evaluate("() => switchSettingsSection('ctx-overview')")
    page.wait_for_selector("#tab-context-gateway .ctx-overview-grid", timeout=4_000)

    track_count = page.evaluate(
        """
        () => {
          const grid = document.querySelector(
            '#tab-context-gateway .ctx-overview-grid'
          );
          if (!grid) return null;
          const tracks = getComputedStyle(grid).gridTemplateColumns.trim();
          return tracks ? tracks.split(/\\s+/).length : 0;
        }
        """
    )

    assert track_count == 1, (
        "ctx-overview-grid should resolve to one column at 480px, got "
        f"{track_count} tracks (cascade-order regression on the @media "
        "override — see PR #1087 review)"
    )
