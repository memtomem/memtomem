"""XSS regression guard for the search result snippet.

Three browser tests drive the real Search UI against a subprocess-isolated
memtomem dev server, ingesting XSS payloads via /api/add and asserting the
rendered snippet contains zero forbidden child elements (script/img/svg/
iframe/object/embed). The dialog tripwire is a secondary signal — the page
CSP (`script-src 'self'`, web/app.py) blocks inline handlers even when
escape regresses, so DOM-element count is the real oracle.

A fourth test exercises escapeHtml() in page context directly to cover the
F-XSS-2 single-quote escape. Browser innerHTML serialization normalizes
&#39; back to a raw apostrophe in text nodes, so the regression is only
observable by invoking the function — not by reading the rendered snippet.
"""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.browser


def _free_port() -> int:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_ready(url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            r = httpx.get(f"{url}/api/system/ui-mode", timeout=1)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(0.1)
    raise RuntimeError(f"dev server did not become ready at {url}")


@pytest.fixture(scope="module")
def xss_dev_server() -> Iterator[str]:
    """Subprocess-isolated dev server, one per module.

    Ingest stays additive across the parametrized cases — each uses a
    unique MARKER token in the content so the BM25 search matches only
    its own chunk.
    """
    with tempfile.TemporaryDirectory(prefix="memtomem-xss-") as home:
        env = os.environ.copy()
        env["HOME"] = home
        env["TMPDIR"] = str(Path(home) / "tmp")
        env["MEMTOMEM_WEB__CSRF_ENFORCE"] = "0"
        (Path(home) / "tmp").mkdir(parents=True, exist_ok=True)
        os.chmod(Path(home) / "tmp", 0o700)

        subprocess.run(
            [
                "uv",
                "run",
                "mm",
                "init",
                "-y",
                "--provider",
                "none",
                "--preset",
                "minimal",
                "--mcp",
                "skip",
            ],
            env=env,
            check=True,
            capture_output=True,
        )

        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen(
            [
                "uv",
                "run",
                "memtomem",
                "web",
                "--dev",
                "--port",
                str(port),
                "--host",
                "127.0.0.1",
            ],
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        try:
            _wait_ready(url)
            yield url
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


XSS_PAYLOADS = [
    (
        "img-onerror",
        "MARKER_IMGOERR <img src=x onerror=alert('XSS_IMG')>",
        "marker_imgoerr",
    ),
    (
        "svg-onload",
        "MARKER_SVGOL <svg onload=alert('XSS_SVG')></svg>",
        "marker_svgol",
    ),
    (
        "script-tag",
        "MARKER_SCRIPT <script>alert('XSS_SCR')</script>",
        "marker_script",
    ),
]


@pytest.mark.parametrize(
    "label,content,query",
    XSS_PAYLOADS,
    ids=[p[0] for p in XSS_PAYLOADS],
)
def test_search_snippet_escapes_xss_payload(
    xss_dev_server: str, page, label: str, content: str, query: str
) -> None:
    dialogs: list[str] = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    r = httpx.post(
        f"{xss_dev_server}/api/add",
        json={
            "content": content,
            "title": f"xss-{label}",
            "tags": ["xss-test"],
        },
        timeout=10,
    )
    assert r.status_code == 200, f"ingest failed ({r.status_code}): {r.text}"

    page.goto(xss_dev_server)
    page.fill("#search-input", query)
    page.click("#search-btn")
    page.wait_for_selector("#results-list .result-snippet", timeout=5000)
    # Allow image/svg load handlers a tick to fire if escape regresses.
    page.wait_for_timeout(200)

    # Primary oracle: no forbidden injected elements in the snippet.
    forbidden = page.locator(
        "#results-list .result-snippet :is(script, img, svg, iframe, object, embed)"
    ).count()
    assert forbidden == 0, (
        f"forbidden injected elements in snippet for [{label}]: count={forbidden}"
    )

    # Secondary: snippet's serialized HTML contains the escaped lt entity.
    snippet_html = page.locator("#results-list .result-snippet").first.inner_html()
    assert "&lt;" in snippet_html, f"snippet did not escape '<' for [{label}]: {snippet_html!r}"

    # Tripwire: CSP currently blocks execution, but record-and-assert anyway
    # so this re-arms if CSP is ever loosened.
    assert dialogs == [], f"unexpected dialog(s): {dialogs}"


def test_escape_html_handles_single_quote(xss_dev_server: str, page) -> None:
    """F-XSS-2 regression guard.

    Browser HTML serialization normalizes &#39; back to a raw apostrophe
    in text nodes, so the escape is not observable through inner_html().
    Invoking escapeHtml() directly is the actual source of truth.
    """
    page.goto(xss_dev_server)
    page.wait_for_function("typeof escapeHtml === 'function'", timeout=5000)
    escaped = page.evaluate(
        "(s) => escapeHtml(s)",
        "<a href='x' onclick=\"alert('XSS')\">",
    )
    assert "&#39;" in escaped, f"single quote not escaped — F-XSS-2 regressed: {escaped!r}"
    assert "&lt;" in escaped and "&gt;" in escaped and "&quot;" in escaped, (
        f"other entities missing: {escaped!r}"
    )
