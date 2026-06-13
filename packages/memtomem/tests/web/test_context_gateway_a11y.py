"""Static a11y regression pins for the Context Gateway dashboard (B-2 #1285).

Text-level assertions over the shipped static assets (no app boot) — the
behavioral live-region / role / tooltip wiring is pinned by the vitest suite
``tests-js/ctx-a11y.test.mjs``; these guard the CSS utility, the focus
indicator, the badge dual-cue, and the already-compliant markers a future edit
could silently regress.
"""

from __future__ import annotations

import re
from pathlib import Path

_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "memtomem" / "web" / "static"


def _css() -> str:
    return (_STATIC_DIR / "style.css").read_text(encoding="utf-8")


def _html() -> str:
    return (_STATIC_DIR / "index.html").read_text(encoding="utf-8")


def test_sr_only_utility_uses_clip_pattern() -> None:
    """The sr-only utility must hide visually while staying in the a11y tree."""
    css = _css()
    block = re.search(r"\.sr-only\s*\{(?P<body>.*?)\}", css, re.S)
    assert block is not None, "style.css must define an .sr-only utility class"
    body = block.group("body")
    # The standard visually-hidden recipe — must NOT use display:none /
    # visibility:hidden (those drop the node from the accessibility tree).
    assert "position: absolute" in body
    assert "clip: rect(0, 0, 0, 0)" in body
    assert "width: 1px" in body and "height: 1px" in body
    assert "display: none" not in body
    assert "visibility: hidden" not in body


def test_keyboard_focus_indicator_present() -> None:
    """A visible :focus-visible outline must exist (focus differentiation)."""
    css = _css()
    assert ":focus-visible" in css
    main_focus = re.search(r"#main:focus-visible\s*\{(?P<body>.*?)\}", css, re.S)
    assert main_focus is not None, "the skip-link target must keep a focus-visible ring"
    assert "outline:" in main_focus.group("body")


def test_toast_container_is_not_a_live_region() -> None:
    """Per-toast roles own urgency; a live container would double-announce.

    Mutation that bites: re-adding ``aria-live`` to ``#toast-container`` (the
    pre-B-2 markup) — an assertive error toast nested in a polite region.
    """
    html = _html()
    toast = re.search(r"<div id=\"toast-container\"[^>]*>", html)
    assert toast is not None, "index.html must define #toast-container"
    tag = toast.group(0)
    assert "aria-live" not in tag
    assert "aria-atomic" not in tag


def test_ctx_sync_status_live_region_preserved() -> None:
    """Regression pin for the already-compliant Sync All status region.

    B-2 must not disturb the canonical role=status + aria-live=polite pattern
    the issue points to as the model.
    """
    html = _html()
    sync = re.search(r"<div id=\"ctx-sync-status\"[^>]*>", html)
    assert sync is not None, "index.html must keep #ctx-sync-status"
    tag = sync.group(0)
    assert 'role="status"' in tag
    assert 'aria-live="polite"' in tag


def test_missing_scope_badge_carries_non_color_cue() -> None:
    """Colorblind-safe: the 'missing' portal row must signal via more than hue.

    ``.ctx-portal-row--missing`` carries a dashed border + reduced opacity, not
    a color fill alone — so the state is perceivable without color vision.
    """
    css = _css()
    block = re.search(r"\.ctx-portal-row--missing\s*\{(?P<body>.*?)\}", css, re.S)
    assert block is not None, "style.css must style .ctx-portal-row--missing"
    body = block.group("body")
    # A border (shape cue) and/or opacity (brightness cue) in addition to color.
    assert "border" in body or "opacity" in body
