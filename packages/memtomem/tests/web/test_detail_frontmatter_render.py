"""A note's YAML frontmatter renders as metadata, not as a heading (#3030, #3093).

Drives the real Search UI against a subprocess-isolated dev server that has
indexed three notes: one that opens with frontmatter, one that opens with a
heading followed by a ``---`` block, which stores the same content shape, and
one that has the first as a related chunk. Only the first gets the frontmatter
box, in the detail view and in the preview under Related; the second renders as
Markdown as before.
"""

from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import expect

from _subprocess_env import subprocess_env

pytestmark = pytest.mark.browser

FRONTMATTER_NOTE = (
    "---\ntags: [alpha]\nowner: zqxvteam\n---\n\n# Cache policy\n\nRedis LRU eviction qmarkfm.\n"
)
TOPIC_NOTE = "# Topic\n---\nStatus: draft\n---\nBody qmarktopic.\n"
# Its words are all in ``FRONTMATTER_NOTE``, so that note is a related chunk of
# this one (the related search queries with the open chunk's content).
RELATED_NOTE = "Redis LRU eviction.\n"


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
def frontmatter_dev_server() -> Iterator[str]:
    """Subprocess-isolated dev server over a temp home holding the three notes.

    Runs on the interpreter running pytest, with the package pytest imported
    first on ``PYTHONPATH``, as ``test_xss_search_render.py`` does.
    """
    with tempfile.TemporaryDirectory(prefix="memtomem-fm-") as home:
        env = subprocess_env(
            HOME=home,
            USERPROFILE=home,
            TMPDIR=str(Path(home) / "tmp"),
            MEMTOMEM_WEB__CSRF_ENFORCE="0",
        )
        (Path(home) / "tmp").mkdir(parents=True, exist_ok=True)
        os.chmod(Path(home) / "tmp", 0o700)
        project = Path(home) / "project"
        project.mkdir()
        notes = Path(home) / "notes"
        notes.mkdir()
        (notes / "cache.md").write_text(FRONTMATTER_NOTE, encoding="utf-8")
        (notes / "topic.md").write_text(TOPIC_NOTE, encoding="utf-8")
        (notes / "related.md").write_text(RELATED_NOTE, encoding="utf-8")
        cli = [sys.executable, "-c", "from memtomem.cli import cli; cli()"]
        run = dict(cwd=project, env=env, check=True, capture_output=True)
        # ``notes`` is the memory directory: the detail pane edits a chunk by
        # id, which is refused for a file outside every memory directory.
        subprocess.run(
            [*cli, "init", "--non-interactive", "--provider", "none", "--preset", "minimal"]
            + ["--mcp", "skip", "--memory-dir", str(notes)],
            **run,
        )
        subprocess.run([*cli, "index", str(notes)], **run)

        port = _free_port()
        url = f"http://127.0.0.1:{port}"
        proc = subprocess.Popen(
            [*cli, "web", "--dev", "--port", str(port), "--host", "127.0.0.1"],
            cwd=project,
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
                proc.wait(timeout=5)


def _open_first_result(page, url: str, query: str) -> None:
    page.goto(url)
    page.fill("#search-input", query)
    page.click("#search-btn")
    page.wait_for_selector("#results-list .result-snippet", timeout=5000)
    page.locator("#results-list .result-item").first.click()
    page.wait_for_selector("#d-code-view.md-preview p", timeout=5000)


def test_frontmatter_renders_as_metadata(frontmatter_dev_server: str, page) -> None:
    _open_first_result(page, frontmatter_dev_server, "qmarkfm")
    box = page.locator("#d-code-view pre.md-frontmatter")
    box.wait_for(timeout=5000)

    assert box.inner_text() == "---\ntags: [alpha]\nowner: zqxvteam\n---"
    headings = page.locator("#d-code-view :is(h1, h2, h3, h4, h5, h6)").all_inner_texts()
    assert not any("tags:" in h for h in headings), headings
    assert page.locator("#d-code-view hr").count() == 0

    # Edit loads the source text; View then shows the heading the indexed
    # content left out, under the same box.
    page.click("#d-view-btn")
    # Polled from Python: the page CSP (``script-src 'self'``) refuses string eval.
    expect(page.locator("#d-editor")).to_have_value(re.compile("# Cache policy"), timeout=5000)
    page.click("#d-view-btn")
    page.wait_for_selector("#d-code-view h1", timeout=5000)
    assert page.locator("#d-code-view pre.md-frontmatter").inner_text().startswith("---\ntags:")
    assert page.locator("#d-code-view h1").inner_text() == "Cache policy"


def test_related_preview_renders_frontmatter_as_metadata(frontmatter_dev_server: str, page) -> None:
    """The preview under Related shows the same box (#3093)."""
    page.goto(frontmatter_dev_server)
    # Typing searches by itself (debounced). Clicking Search as well would send
    # a second search, which can reselect and hide the preview mid-assertion.
    page.fill("#search-input", "eviction")
    row = page.locator("#results-list .result-item").filter(
        has=page.locator(".result-filename", has_text="related.md")
    )
    row.wait_for(timeout=5000)
    # The search selects its first row itself. Clicking that row again would
    # start a second related load, which hides the list and preview it finds.
    if "selected" not in (row.get_attribute("class") or "").split():
        # The snippet selects the row; the filename opens the source modal instead.
        row.locator(".result-snippet").click()
    # The related list fills while collapsed.
    neighbour = page.locator("#related-list .similar-item").filter(has_text="cache.md")
    neighbour.wait_for(state="attached", timeout=5000)
    page.click("#related-toggle")
    neighbour.click()

    box = page.locator("#related-preview pre.md-frontmatter")
    box.wait_for(timeout=5000)
    assert box.inner_text() == "---\ntags: [alpha]\nowner: zqxvteam\n---"
    content = page.locator("#related-preview .related-preview-content")
    assert content.locator("hr").count() == 0
    assert content.locator(":is(h1, h2, h3, h4, h5, h6)").count() == 0


def test_a_heading_led_dash_block_renders_as_before(frontmatter_dev_server: str, page) -> None:
    _open_first_result(page, frontmatter_dev_server, "qmarktopic")
    # Give the frontmatter answer time to arrive and (wrongly) re-render.
    page.wait_for_timeout(500)

    assert page.locator("#d-code-view pre.md-frontmatter").count() == 0
    assert page.locator("#d-code-view hr").count() == 1
