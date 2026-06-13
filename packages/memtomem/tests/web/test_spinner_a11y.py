"""Static a11y pin: every spinner render carries a screen-reader text
alternative (#1316).

The decorative ``.spinner-panel`` is silent to screen readers on its own.
The shared ``panelLoading()`` helper and the ``srLoading()`` helper (for
bespoke wrappers that keep their own layout) both inject an ``sr-only``
``common.loading`` span; the one ``home.state.loading`` spinner uses an
``aria-label`` instead. This guard fails if a new hand-rolled spinner render
lands without any of those, regressing the #1316 sweep — and it failed on
every one of the 13 sites that #1316 fixed.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "memtomem" / "web" / "static"

# A real spinner render is markup: ``class="…spinner-panel…"``. Prose/comments
# that merely mention ``.spinner-panel`` (CSS-selector style, no ``class="``)
# are deliberately NOT matched.
_SPINNER_MARKUP = re.compile(r'class="[^"]*\bspinner-panel\b[^"]*"')
# Any of these on the same line — or the immediately following line, so the
# two-line ``panelLoading()`` author site counts — gives the spinner a voice.
_TEXT_ALT = re.compile(r"sr-only|aria-label|srLoading\(")


def _js_files() -> list[Path]:
    return sorted(_STATIC_DIR.glob("*.js"))


def test_static_js_present() -> None:
    assert _js_files(), f"no static JS under {_STATIC_DIR} — path drift?"


def test_some_spinner_markup_exists() -> None:
    """Guard against the assertion vacuously passing if the markup is renamed."""
    blob = "\n".join(f.read_text(encoding="utf-8") for f in _js_files())
    assert _SPINNER_MARKUP.search(blob), (
        "no spinner-panel markup found at all — did the class get renamed? "
        "Update this guard's pattern alongside the markup."
    )


@pytest.mark.parametrize("js", _js_files(), ids=lambda p: p.name)
def test_every_spinner_render_has_sr_text_alternative(js: Path) -> None:
    lines = js.read_text(encoding="utf-8").splitlines()
    offenders: list[str] = []
    for i, line in enumerate(lines):
        if not _SPINNER_MARKUP.search(line):
            continue
        window = line + "\n" + (lines[i + 1] if i + 1 < len(lines) else "")
        if not _TEXT_ALT.search(window):
            offenders.append(f"{js.name}:{i + 1}: {line.strip()}")
    assert not offenders, (
        "spinner render(s) without an sr-only / aria-label / srLoading() text "
        "alternative (#1316) — route the markup through panelLoading() or append "
        "srLoading():\n" + "\n".join(offenders)
    )
