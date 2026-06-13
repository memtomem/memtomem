"""Browser tests for the mcp-servers Move/Copy modal variant (#1314).

The B-6 #1289 destination modal grew a constrained mcp-servers branch (engine
A-12 #1282; the web ``/transfer`` route already accepts ``kind=mcp-servers``).
Route-stubbed coverage of the real DOM flow:

* the detail pane mounts the "Move / Copy" button for an mcp-server, and opening
  it shows the constrained shape — mode + tier fieldsets and the rename row
  hidden, only the destination-project picker (source project excluded);
* a clean dry-run preview enables Apply with the pinned body;
* applying threads the ``confirm_project_shared`` round-trip, then re-POSTs.

The companion vitest spec (``tests-js/ctx-mcp-move-copy-modal.test.mjs``) pins
the finer state machine (CSRF header, no-destination short-circuit, paused-scope
filtering, destination-pinned Sync now).
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser

_NAME = "demo-mcp"

# Two-project roster: source = Server CWD (''), destination = Project A. The
# default conftest roster is server-cwd only, so a cross-project mcp copy needs
# this override (registered last → page.route last-wins).
_ROSTER = {
    "scopes": [
        {
            "scope_id": "",
            "label": "Server CWD",
            "root": "/srv",
            "tier": "project",
            "sources": ["server-cwd"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 0, "commands": 0, "agents": 0, "mcp-servers": 1},
        },
        {
            "scope_id": "proj-a",
            "label": "Project A",
            "root": "/work/a",
            "tier": "project",
            "sources": ["known-projects"],
            "experimental": False,
            "missing": False,
            "enabled": True,
            "sync_eligible": True,
            "counts": {"skills": 0, "commands": 0, "agents": 0, "mcp-servers": 0},
        },
    ],
    "target_scope": "project_shared",
}

_DETAIL = {
    "name": _NAME,
    "content": '{"mcpServers": {}}\n',
    "target_scope": "project_shared",
    "layout": "flat",
    "mtime_ns": "1700000000000000000",
    "fields": {"command": "node", "args_count": 1, "env_count": 0},
}

_PLAN = {
    "status": "plan",
    "transferred": False,
    "kind": "mcp-servers",
    "name": _NAME,
    "dst_name": _NAME,
    "mode": "copy",
    "from_scope": "project_shared",
    "to_scope": "project_shared",
    "src_project_scope_id": "",
    "dst_project_scope_id": "proj-a",
    "src_path": "/srv/.memtomem/mcp_servers/demo-mcp.json",
    "dst_path": "/work/a/.memtomem/mcp_servers/demo-mcp.json",
    "needs_sync": True,
    "sync_command": "cd /work/a && mm context sync --include=mcp-servers",
    "sync_hint": "Run sync in Project A",
    "notes": [],
}


def _stub_mcp(page, *, apply_bodies: list[dict] | None = None) -> dict:
    """Method/leg-branching handler for ``/api/context/mcp-servers/demo-mcp**``.

    GET → the detail payload (so ``loadCtxDetail`` mounts the button);
    POST ``?dry_run=1`` → a clean plan; POST without dry_run → the next
    ``apply_bodies`` entry (sticky on the last).
    """
    applies = list(apply_bodies or [{**_PLAN, "status": "ok", "transferred": True}])
    state: dict = {"dry": [], "apply": []}

    def _handler(route):
        req = route.request
        if req.method == "POST" and "/transfer" in req.url:
            if "dry_run" in req.url:
                state["dry"].append(req.url)
                route.fulfill(status=200, content_type="application/json", body=json.dumps(_PLAN))
            else:
                state["apply"].append(json.loads(req.post_data or "{}"))
                body = applies[min(len(state["apply"]) - 1, len(applies) - 1)]
                route.fulfill(status=200, content_type="application/json", body=json.dumps(body))
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(_DETAIL))

    page.route(
        "**/api/context/projects**",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(_ROSTER)),
    )
    page.route("**/api/context/mcp-servers/demo-mcp**", _handler)
    return state


def _open_mcp(page) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate("() => switchSettingsSection('ctx-mcp-servers')")
    page.wait_for_selector("#ctx-mcp-servers-detail", state="attached", timeout=5_000)


def _open_modal(page) -> None:
    page.evaluate("() => { loadCtxDetail('mcp-servers', 'demo-mcp'); }")
    page.wait_for_selector("#ctx-mcp-servers-detail .ctx-detail-move-copy-btn", timeout=4_000)
    page.locator("#ctx-mcp-servers-detail .ctx-detail-move-copy-btn").click()
    page.wait_for_function(
        "() => { const m = document.getElementById('ctx-move-copy-modal'); return m && !m.hidden; }",
        timeout=3_000,
    )


def test_mcp_modal_opens_in_constrained_shape(page, mm_web_url: str) -> None:
    """The mcp-servers detail pane mounts the button; the modal hides the
    mode/tier/rename controls and shows only the destination-project picker."""
    install_default_stubs(page)
    state = _stub_mcp(page)
    page.goto(mm_web_url)
    _open_mcp(page)
    _open_modal(page)

    assert page.locator("#ctx-mc-mode-field").is_hidden()
    assert page.locator("#ctx-mc-tier-field").is_hidden()
    assert page.locator("#ctx-mc-rename-row").is_hidden()
    assert page.locator("#ctx-mc-project-row").is_visible()
    assert page.locator("#ctx-mc-mcp-note").is_visible()
    # Destination picker excludes the source project (Server CWD, '').
    values = page.eval_on_selector_all("#ctx-mc-project option", "els => els.map(e => e.value)")
    assert values == ["proj-a"]
    # A clean dry-run enables Apply.
    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-mc-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )
    assert state["dry"], "a dry-run preview must fire on open"


def test_mcp_project_shared_gate_round_trip(page, mm_web_url: str) -> None:
    """Apply threads the confirm_project_shared round-trip, then re-POSTs."""
    install_default_stubs(page)
    state = _stub_mcp(
        page,
        apply_bodies=[
            {
                "status": "needs_confirmation",
                "confirm": "confirm_project_shared",
                "reason": "Writes the canonical into the git-tracked project_shared tier.",
                "plan": _PLAN,
            },
            {**_PLAN, "status": "ok", "transferred": True},
        ],
    )
    page.goto(mm_web_url)
    _open_mcp(page)
    _open_modal(page)

    page.wait_for_function(
        "() => { const b = document.getElementById('ctx-mc-apply-btn'); return b && !b.disabled; }",
        timeout=3_000,
    )
    page.locator("#ctx-mc-apply-btn").click()

    # The gate surfaces through the shared confirm dialog (not an error).
    page.wait_for_function("() => !document.getElementById('confirm-modal').hidden", timeout=3_000)
    page.locator("#confirm-ok-btn").click()

    page.wait_for_selector("#toast-container .toast", timeout=3_000)
    assert len(state["apply"]) == 2
    assert state["apply"][0].get("mode") == "copy"
    assert state["apply"][0].get("to_target_scope") == "project_shared"
    assert state["apply"][0].get("to_project_scope_id") == "proj-a"
    assert "as_name" not in state["apply"][0]
    assert state["apply"][0].get("confirm_project_shared") is False
    assert state["apply"][1].get("confirm_project_shared") is True
