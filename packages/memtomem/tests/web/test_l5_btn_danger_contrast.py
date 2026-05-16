"""Effective-contrast regression guard for the F-L5-1 .btn-danger fix.

The kind-moth L5 smoke (2026-05-15) found dark-theme ``.btn-danger`` at
3.63:1 against white, failing WCAG AA. Two ``style.css`` rules close the
gap (a dark-only solid background override and a hover ``brightness(0.92)``
that replaces the global ``opacity: 0.85`` dim). The companion static pin
``TestBtnDangerContrastGuard`` in ``test_qa_audit_pins.py`` catches a
deliberate removal of either rule; this Playwright test catches the
cascade-interaction regressions that a substring check cannot see —
a more-specific selector overriding the background, a parent ``filter``
composing into hover, or the global ``button:hover`` dim leaking back in
because ``opacity: 1`` was dropped.

The 4-state matrix mirrors the original smoke measurements
(``memtomem-web-kind-moth-findings.md::F-L5-1``): theme × hover. Each
state must clear WCAG AA 4.5:1 once ``filter: brightness()`` has been
folded into both background and foreground (which is how the rule
actually composes — ``filter`` applies to the element including its
text descendant).

The E-L5-b mobile-viewport bounding-box check piggybacks on this fixture
because the confirm modal is already open — see
``test_confirm_button_fits_in_414x844_viewport``.
"""

from __future__ import annotations

import re

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_RGB_RE = re.compile(r"rgba?\(\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)")
_BRIGHTNESS_RE = re.compile(r"brightness\(([\d.]+)\)")


def _parse_rgb(value: str) -> tuple[int, int, int]:
    """Extract the RGB triple from ``rgb(...)`` or ``rgba(...)``.

    Alpha is intentionally ignored — the F-L5-1 fix sets ``opacity: 1``
    and the AA target presumes a fully-opaque button. A regression that
    re-introduces partial opacity is caught by the explicit opacity
    assertion in the test, not by quietly compositing against the modal
    backdrop here (which would mask the regression).
    """
    match = _RGB_RE.match(value)
    assert match is not None, f"could not parse RGB from {value!r}"
    return int(match.group(1)), int(match.group(2)), int(match.group(3))


def _brightness_factor(filter_value: str) -> float:
    """Return the ``brightness(N)`` factor from a computed ``filter``
    string, or ``1.0`` if the filter is ``none``/absent."""
    if not filter_value or filter_value == "none":
        return 1.0
    match = _BRIGHTNESS_RE.search(filter_value)
    return float(match.group(1)) if match else 1.0


def _apply_brightness(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(min(255, max(0, round(c * factor))) for c in rgb)  # type: ignore[return-value]


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def _lin(c: int) -> float:
        v = c / 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4

    r, g, b = rgb
    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast_ratio(fg: tuple[int, int, int], bg: tuple[int, int, int]) -> float:
    l1 = _relative_luminance(fg)
    l2 = _relative_luminance(bg)
    if l1 < l2:
        l1, l2 = l2, l1
    return (l1 + 0.05) / (l2 + 0.05)


def _disable_transitions(page) -> None:
    """Inject a stylesheet that disables all CSS transitions.

    The button has ``transition: opacity 0.15s`` at ``style.css:915``
    so after ``CSS.forcePseudoState`` switches the ``:hover`` state,
    a computed-style read taken mid-transition returns an interpolated
    value instead of the final settled one (e.g. ``0.994331`` partway
    from 1 → 0.85). Disabling transitions globally makes the read
    deterministic without ``page.wait_for_timeout`` slop."""
    page.add_style_tag(content="*, *::before, *::after { transition: none !important; }")


def _open_confirm_modal(page) -> None:
    """Open the confirm modal so the OK button is rendered and hoverable.

    ``showConfirm`` returns a Promise that resolves on OK/Cancel; we
    intentionally do not await it — the modal stays open for the duration
    of the test and tears down with the page."""
    page.evaluate(
        """() => {
            window.showConfirm({
                title: 'L5 contrast probe',
                message: 'effective contrast measurement',
            });
        }"""
    )
    page.wait_for_function(
        """() => {
            const m = document.getElementById('confirm-modal');
            return m && !m.hidden;
        }""",
        timeout=4_000,
    )


def _set_theme(page, theme: str) -> None:
    """Force the documentElement ``data-theme`` to the given value.

    Mirrors the production theme toggle (``app.js:1410-1411``) without
    going through the toggle button, which would also flip a localStorage
    key the test doesn't care about."""
    page.evaluate(
        "(t) => document.documentElement.setAttribute('data-theme', t)",
        theme,
    )


def _cdp_for_button(page, selector: str) -> tuple[object, int]:
    """Open (once) a CDP session pinned to ``page`` and return the
    ``nodeId`` for ``selector``.

    The session and nodeId are cached on the ``page`` object so the
    same identifiers stay alive across ``_force_hover`` / state-read
    calls. Critical: ``DOM.getDocument`` invalidates previously
    returned nodeIds, so call it exactly once per page lifetime —
    otherwise ``CSS.forcePseudoState`` and ``CSS.getComputedStyleForNode``
    end up referencing different DOM tree snapshots and the force
    silently clears."""
    cdp = getattr(page, "_l5_cdp", None)
    if cdp is None:
        cdp = page.context.new_cdp_session(page)
        cdp.send("DOM.enable")
        cdp.send("CSS.enable")
        doc = cdp.send("DOM.getDocument", {"depth": -1, "pierce": True})
        node = cdp.send(
            "DOM.querySelector",
            {"nodeId": doc["root"]["nodeId"], "selector": selector},
        )
        assert node.get("nodeId"), (
            f"DOM.querySelector returned no node for {selector!r}; raw response={node!r}"
        )
        page._l5_cdp = cdp
        page._l5_button_node_id = node["nodeId"]
    return cdp, page._l5_button_node_id


def _force_hover(page, selector: str, hover: bool) -> None:
    """Toggle the CSS ``:hover`` pseudo-class on ``selector`` via CDP.

    Playwright's ``locator.hover()`` dispatches mouse events but does
    not reliably trigger ``:hover`` in headless Chromium — the computed
    style stays at the no-hover values. CDP's ``CSS.forcePseudoState``
    bypasses input emulation and toggles the pseudo-class directly so
    the cascade applies the right rules regardless of headless mouse
    state."""
    cdp, node_id = _cdp_for_button(page, selector)
    cdp.send(
        "CSS.forcePseudoState",
        {
            "nodeId": node_id,
            "forcedPseudoClasses": ["hover"] if hover else [],
        },
    )


def _read_button_state(page) -> dict[str, str]:
    """Snapshot computed bg / fg / filter / opacity for the OK button.

    Reads via CDP ``CSS.getComputedStyleForNode`` (using the same
    cached nodeId as ``_force_hover``) so the forced ``:hover``
    pseudo-state is visible. Reading through ``getComputedStyle`` in
    JS does NOT reflect ``CSS.forcePseudoState`` — the JS and CDP
    layers maintain separate views."""
    cdp, node_id = _cdp_for_button(page, "#confirm-ok-btn")
    props = cdp.send("CSS.getComputedStyleForNode", {"nodeId": node_id})
    style = {p["name"]: p["value"] for p in props["computedStyle"]}
    return {
        "bg": style.get("background-color", ""),
        "fg": style.get("color", ""),
        "filter": style.get("filter", "none"),
        "opacity": style.get("opacity", "1"),
    }


@pytest.mark.parametrize(
    ("theme", "hover"),
    [
        ("dark", False),
        ("dark", True),
        ("light", False),
        ("light", True),
    ],
)
def test_btn_danger_contrast_meets_aa(page, mm_web_url: str, theme: str, hover: bool) -> None:
    """Each of the 4 ``theme × hover`` states for ``#confirm-ok-btn`` must
    clear WCAG AA 4.5:1 after the ``filter: brightness()`` is folded in.

    This is the cascade-aware oracle for F-L5-1. The substring pin in
    ``test_qa_audit_pins.py::TestBtnDangerContrastGuard`` covers the
    "someone deleted the rule" regression; this catches the subtler
    cases where the rule survives but a parent filter / a more-specific
    selector / a missing ``opacity: 1`` leaves the rendered button below
    AA."""
    install_default_stubs(page)
    page.goto(mm_web_url)
    _disable_transitions(page)
    _open_confirm_modal(page)

    _set_theme(page, theme)
    _force_hover(page, "#confirm-ok-btn", hover)

    state = _read_button_state(page)
    assert state["opacity"] == "1", (
        f"#confirm-ok-btn opacity must be 1 in {theme} × hover={hover} "
        f"(F-L5-1 explicitly overrides the global button:hover opacity "
        f"0.85 dim with brightness(0.92)); got {state['opacity']!r}"
    )

    bg = _parse_rgb(state["bg"])
    fg = _parse_rgb(state["fg"])
    factor = _brightness_factor(state["filter"])
    eff_bg = _apply_brightness(bg, factor)
    eff_fg = _apply_brightness(fg, factor)
    contrast = _contrast_ratio(eff_fg, eff_bg)

    assert contrast >= 4.5, (
        f"#confirm-ok-btn contrast in {theme} × hover={hover} fell below "
        f"WCAG AA: got {contrast:.3f}:1 (bg={bg} fg={fg} "
        f"filter={state['filter']!r} → eff_bg={eff_bg} eff_fg={eff_fg}). "
        f"Audit F-L5-1 baseline: dark normal 5.17 / light 4.77 / "
        f"dark hover 4.94 / light hover 4.58."
    )


def test_btn_ghost_btn_danger_stays_transparent(page, mm_web_url: str) -> None:
    """The ``.btn-ghost.btn-danger`` combo (e.g. context-gateway header
    danger chip) must keep its transparent background. The F-L5-1 fix
    scopes both rules with ``:not(.btn-ghost)`` so this combo is
    untouched; this pin guards the scoping."""
    install_default_stubs(page)
    page.goto(mm_web_url)

    for theme in ("dark", "light"):
        _set_theme(page, theme)
        bg = page.evaluate(
            """() => {
                const probe = document.createElement('button');
                probe.className = 'btn-ghost btn-danger';
                probe.style.position = 'absolute';
                probe.style.left = '-9999px';
                document.body.appendChild(probe);
                const bg = getComputedStyle(probe).backgroundColor;
                probe.remove();
                return bg;
            }"""
        )
        # rgba(0, 0, 0, 0) is the canonical transparent computed value.
        assert "0, 0, 0, 0" in bg.replace(" ", ", ").replace(",,", ","), (
            f"btn-ghost.btn-danger background must stay transparent in "
            f"{theme} theme; got {bg!r}. F-L5-1 protects this with "
            f":not(.btn-ghost) — re-check the scoping if this fails."
        )


def test_confirm_button_fits_in_414x844_viewport(page, mm_web_url: str) -> None:
    """E-L5-b piggyback: at the 414×844 mobile viewport the OK button's
    bounding-box bottom must stay within the viewport. Bundled into this
    file because the confirm modal is already open under the same
    fixture — adding a standalone browser file for one geometric check
    is not worth the marginal infra."""
    page.set_viewport_size({"width": 414, "height": 844})
    install_default_stubs(page)
    page.goto(mm_web_url)
    _open_confirm_modal(page)

    bbox = page.locator("#confirm-ok-btn").bounding_box()
    assert bbox is not None, "#confirm-ok-btn has no bounding box"
    bottom = bbox["y"] + bbox["height"]
    assert bottom <= 844, (
        f"#confirm-ok-btn bottom={bottom:.1f}px overflows the 414×844 "
        f"mobile viewport (E-L5-b smoke baseline: ≤ 844)"
    )
