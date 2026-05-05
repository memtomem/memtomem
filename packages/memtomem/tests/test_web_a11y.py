"""Pin tests for list-row accessibility attributes in the static web UI.

Search/source/timeline list-rows are constructed in JS, so behavior tests would
require a browser. These source-scan tests pin the a11y semantics so removing
``role=`` or ``aria-label`` silently can't slip past review (issue #700).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

_STATIC_DIR = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"
_APP_JS = _STATIC_DIR / "app.js"


@pytest.fixture(scope="module")
def app_js() -> str:
    assert _APP_JS.exists(), f"app.js missing: {_APP_JS}"
    return _APP_JS.read_text(encoding="utf-8")


def _extract_function(source: str, name: str) -> str:
    """Extract a top-level ``function name(...) { ... }`` body via brace matching."""
    m = re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", source)
    assert m, f"function {name} not found in app.js"
    i = source.index("{", m.end())
    depth = 0
    for j in range(i, len(source)):
        c = source[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return source[i : j + 1]
    raise AssertionError(f"unterminated function body for {name}")


class TestSearchResultItemA11y:
    """Pin a11y attributes on ``.result-item`` rows (issue #700)."""

    def test_buildresultitem_sets_role_button(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "setAttribute('role', 'button')" in body, (
            "result-item must expose role=button so screen readers announce it "
            "as a selectable row, not a generic group"
        )

    def test_buildresultitem_sets_aria_label(self, app_js: str) -> None:
        body = _extract_function(app_js, "_buildResultItem")
        assert "setAttribute('aria-label'" in body, (
            "result-item must set aria-label from filename/lines/namespace/age "
            "so screen readers announce a meaningful name"
        )
