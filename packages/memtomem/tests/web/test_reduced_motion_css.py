"""Static regression pins for reduced-motion CSS coverage."""

from __future__ import annotations

import re
from pathlib import Path


_STATIC_DIR = Path(__file__).resolve().parents[2] / "src" / "memtomem" / "web" / "static"


def test_reduced_motion_media_block_has_wildcard_sweep() -> None:
    css = (_STATIC_DIR / "style.css").read_text(encoding="utf-8")
    media = re.search(
        r"@media\s*\(\s*prefers-reduced-motion\s*:\s*reduce\s*\)\s*\{(?P<body>.*?)\n\}",
        css,
        re.S,
    )
    assert media is not None, "style.css must define a prefers-reduced-motion: reduce block"

    body = media.group("body")
    wildcard = re.search(r"\*,\s*\*::before,\s*\*::after\s*\{(?P<body>.*?)\}", body, re.S)
    assert wildcard is not None, (
        "reduced-motion block must include a near-wildcard selector so new "
        "transitions and animations inherit reduced motion coverage"
    )

    wildcard_body = wildcard.group("body")
    assert "transition-duration: 0.01ms !important" in wildcard_body
    assert "animation-duration: 0.01ms !important" in wildcard_body
