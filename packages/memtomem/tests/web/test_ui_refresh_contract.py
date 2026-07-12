"""Static guardrails for the shared mm web visual-system refresh."""

from __future__ import annotations

from pathlib import Path


STATIC = Path(__file__).parents[2] / "src" / "memtomem" / "web" / "static"


def test_refresh_tokens_cover_color_spacing_shape_focus_and_motion() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    for token in (
        "--surface-raised:",
        "--surface-subtle:",
        "--border-strong:",
        "--space-1:",
        "--space-8:",
        "--radius-sm:",
        "--radius-lg:",
        "--shadow-sm:",
        "--shadow-md:",
        "--focus-ring:",
        "--motion-fast:",
        "--motion-base:",
    ):
        assert token in css


def test_refresh_replaces_global_opacity_hover_and_pins_mobile_targets() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert "2026 UI refresh foundation" in css
    refresh = css.split("2026 UI refresh foundation", maxsplit=1)[1]
    assert "button:hover { opacity: 1; }" in refresh
    assert "button:active:not(:disabled)" in refresh
    assert "button:focus-visible" in refresh
    assert "min-height: 44px" in refresh
    assert ".tab-btn { min-width: 44px;" in refresh
    assert 'input:not([type="checkbox"]):not([type="radio"])' in refresh
    assert "#settings-btn,\n  #help-toggle { display: none; }" not in refresh


def test_header_utility_icons_are_dependency_free_inline_svg() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    for button_id in ("settings-btn", "theme-toggle", "help-toggle"):
        start = html.index(f'id="{button_id}"')
        end = html.index("</button>", start)
        assert "<svg" in html[start:end]

    assert "⚙️" not in html
    assert ">🌙<" not in html
    assert ">☀️<" not in html


def test_changed_static_assets_bump_cache_versions() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")

    assert "/style.css?v=136" in html
    assert "/app.js?v=150" in html


def test_theme_icon_follows_document_theme_without_duplicate_js_state() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    js = (STATIC / "app.js").read_text(encoding="utf-8")

    assert "data-theme-state" not in html
    assert "data-theme-state" not in js
    assert ':root[data-theme="light"] .theme-icon-moon' in css


def test_settings_mobile_navigation_is_horizontal_chip_row() -> None:
    css = (STATIC / "style.css").read_text(encoding="utf-8")
    refresh = css.split("2026 UI refresh foundation", maxsplit=1)[1]
    mobile = refresh.split("@media (max-width: 520px)", maxsplit=1)[1]

    assert ".settings-nav" in mobile
    assert "flex-direction: row" in mobile
    assert "overflow-x: auto" in mobile
    assert "max-height: none !important" in mobile
    assert ".settings-nav-btn.collapsed-member { display: flex; }" in mobile


def test_index_uses_segmented_work_card_and_guarded_risk_disclosure() -> None:
    html = (STATIC / "index.html").read_text(encoding="utf-8")
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    assert 'class="index-mode-toggle" role="tablist"' in html
    assert 'class="card index-panels"' in html
    assert 'class="index-risk-disclosure"' in html
    disclosure = html.split('class="index-risk-disclosure"', maxsplit=1)[1]
    disclosure = disclosure.split("</details>", maxsplit=1)[0]
    assert 'id="index-force-unsafe"' in disclosure
    assert ".index-mode-toggle .btn-ghost.btn-active" in css
    assert ".index-risk-content" in css


def _relative_luminance(hex_color: str) -> float:
    """WCAG relative luminance of a ``#rrggbb`` sRGB color."""
    h = hex_color.lstrip("#")
    r, g, b = (int(h[i : i + 2], 16) / 255 for i in (0, 2, 4))

    def _lin(c: float) -> float:
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

    return 0.2126 * _lin(r) + 0.7152 * _lin(g) + 0.0722 * _lin(b)


def _contrast_on_white(hex_color: str) -> float:
    """Contrast ratio of white (#fff) text over ``hex_color``."""
    lum = _relative_luminance(hex_color)
    return (1.0 + 0.05) / (lum + 0.05)


def test_filled_button_accent_meets_aa_contrast_both_themes() -> None:
    """``.btn-primary`` uses ``--accent-fill`` for its background so white text
    clears WCAG AA (>=4.5:1). The lighter ``--accent`` (a text/tint color) is
    ~3:1 under white — the axe smoke gate caught it on the dark filled buttons.
    Pin the fill token per theme AND that the filled-control rules consume it,
    not the raw ``--accent``.
    """
    css = (STATIC / "style.css").read_text(encoding="utf-8")

    # Dark base :root and the light override both define an accessible fill.
    # (#3b63e8 dark, #315fd5 light — both >=4.5:1 under white.)
    assert "--accent-fill: #3b63e8;" in css
    assert "--accent-fill: var(--accent);" in css  # light: accent is already AA
    assert _contrast_on_white("#3b63e8") >= 4.5
    assert _contrast_on_white("#315fd5") >= 4.5
    # The lighter dark accent is intentionally NOT used as a filled background.
    assert _contrast_on_white("#7c9cff") < 4.5

    # Filled backgrounds (base + hover) consume the fill, never the raw accent.
    assert ".btn-primary { background: var(--accent-fill, var(--accent));" in css
    assert ".tl-view-btn.tl-view-active { background: var(--accent-fill, var(--accent));" in css
    hover = css.split(".btn-primary:hover {", maxsplit=1)[1].split("}", maxsplit=1)[0]
    assert "--accent-fill" in hover, "hover must derive from the accessible fill"
    assert "white" not in hover, "hover must not lighten toward white (loses contrast)"
