"""No-interception golden for the real ``mm web`` lifespan."""

from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

pytestmark = pytest.mark.browser


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


@pytest.fixture
def actual_web_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    home = tmp_path / "home"
    runtime = tmp_path / "runtime"
    temp_dir = tmp_path / "tmp"
    project = tmp_path / "project"
    source = tmp_path / "source"
    for directory in (home, runtime, temp_dir, project, source):
        directory.mkdir(mode=0o700)
    (source / "golden.md").write_text(
        "# Golden\n\nactual-lifespan-unique-marker\n", encoding="utf-8"
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "USERPROFILE": str(home),
            "TMPDIR": str(temp_dir),
            "XDG_RUNTIME_DIR": str(runtime),
            "MEMTOMEM_WEB__CSRF_ENFORCE": "1",
            "MEMTOMEM_STORAGE__SQLITE_PATH": str(home / ".memtomem" / "golden.db"),
        }
    )
    cli = [sys.executable, "-c", "from memtomem.cli import cli; cli()"]
    subprocess.run(
        cli
        + [
            "init",
            "--non-interactive",
            "--provider",
            "none",
            "--preset",
            "minimal",
            "--mcp",
            "skip",
        ],
        cwd=project,
        env=env,
        check=True,
        capture_output=True,
    )

    port = _free_port()
    url = f"http://127.0.0.1:{port}"
    log_path = tmp_path / "web.log"
    with log_path.open("wb") as log:
        process = subprocess.Popen(
            cli + ["web", "--mode", "prod", "--host", "127.0.0.1", "--port", str(port)],
            cwd=project,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
    try:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError(log_path.read_text(encoding="utf-8", errors="replace"))
            try:
                response = httpx.get(f"{url}/api/readiness", timeout=1)
                if response.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
        else:
            raise RuntimeError("real lifespan server did not become ready")
        yield url, source
    finally:
        process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=2)


def test_actual_lifespan_add_search_export_and_history(page, actual_web_server):
    url, source = actual_web_server
    session = httpx.get(f"{url}/api/session").json()
    added = httpx.post(
        f"{url}/api/memory-dirs/add",
        json={"path": str(source), "auto_index": True},
        headers={"X-Memtomem-CSRF": session["csrf"]},
        timeout=20,
    )
    assert added.status_code == 200, added.text
    payload = added.json()
    assert payload["index_status"] == "success"
    assert payload["indexed"]["total_files"] == 1
    assert payload["indexed"]["indexed_chunks"] > 0

    console_errors: list[str] = []
    page_errors: list[str] = []
    network_5xx: list[str] = []
    page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)
    page.on("pageerror", lambda exc: page_errors.append(str(exc)))
    page.on(
        "response",
        lambda response: network_5xx.append(response.url) if response.status >= 500 else None,
    )

    page.set_viewport_size({"width": 1440, "height": 900})
    page.goto(f"{url}/#search")
    page.fill("#search-input", "actual-lifespan-unique-marker")
    page.click("#search-btn")
    page.wait_for_selector("#results-list .result-item")
    page.click('.tab-btn[data-tab="sources"]')
    page.go_back()
    page.wait_for_selector('.tab-btn[data-tab="search"].active')
    assert page.url.endswith("#search")
    assert page.locator("#tab-search").is_visible()

    preview = httpx.get(f"{url}/api/export/stats").json()["total_chunks"]
    bundle = json.loads(httpx.get(f"{url}/api/export").text)
    assert preview == bundle["total_chunks"]
    assert preview > 0
    assert console_errors == []
    assert page_errors == []
    assert network_5xx == []
