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

    assert "/style.css?v=135" in html
    assert "/app.js?v=149" in html


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
