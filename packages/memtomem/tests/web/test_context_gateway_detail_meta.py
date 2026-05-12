"""Browser tests for the Skills/Agents/Commands detail meta-header (#962).

The detail panel used to render only ``<pre>`` content + the
Canonical|Diff tabs. The backend already exposed ``description`` /
``target_scope`` / ``layout`` / parsed fields, but the JS UI ignored
them. PR C surfaces those above the tab strip as a meta header + a
chip row for agents (role / isolation / kind / temperature) and
commands (argument_hint / allowed_tools / model). Skills get the meta
header only — they have no analogous parsed-field set.

Tests pin the rendered surface so a regression that drops the meta
header (or the chip row) is caught by CI.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_SKILL_DETAIL = {
    "name": "my-skill",
    "content": "---\ndescription: A reusable skill for X.\n---\n\nBody text.",
    "mtime_ns": str(1_700_000_000 * 1_000_000_000),
    "files": [{"path": "helper.sh", "size": 42}, {"path": "notes.md", "size": 13}],
    "target_scope": "project_shared",
    "layout": "dir",
    "fields": {"description": "A reusable skill for X."},
}

_AGENT_DETAIL = {
    "name": "code-reviewer",
    "content": "---\nname: code-reviewer\ndescription: Reviews code\n---\n\nBody",
    "mtime_ns": str(1_700_000_000 * 1_000_000_000),
    "target_scope": "project_shared",
    "layout": "flat",
    "fields": {
        "description": "Reviews code",
        "role": "reviewer",
        "isolation": "worktree",
        "kind": "tool",
        "temperature": 0.2,
    },
}

_COMMAND_DETAIL = {
    "name": "review",
    "content": "---\ndescription: Run the review\n---\n\nBody",
    "mtime_ns": str(1_700_000_000 * 1_000_000_000),
    "target_scope": "project_shared",
    "layout": "flat",
    "fields": {
        "description": "Run the review",
        "argument_hint": "<pr-number>",
        "allowed_tools": ["Read", "Bash"],
        "model": "claude-opus-4-7",
    },
}


def _projects_payload(kind: str) -> dict:
    """Single-scope cwd payload — matches the shape ``_ctxScopeIsServerCwd``
    and ``_ctxScopeCount`` consume (``sources`` + ``counts.<type>``).
    """
    return {
        "scopes": [
            {
                "scope_id": "cwd-scope",
                "label": "cwd",
                "tier": "project_shared",
                "root": "/fake/project",
                "sources": ["server-cwd"],
                "experimental": False,
                "missing": False,
                "counts": {"skills": 0, "commands": 0, "agents": 0, kind: 1},
            }
        ]
    }


def _stub_list_and_detail(page, kind: str, detail: dict) -> None:
    """Set up minimal projects + list + detail stubs for one artifact type."""
    list_payload = {
        kind: [
            {
                "name": detail["name"],
                "canonical_path": f".claude/{kind}/{detail['name']}",
                "target_scope": detail["target_scope"],
                "runtimes": [{"runtime": "claude", "status": "in_sync"}],
            }
        ],
        "canonical_root": f".claude/{kind}",
        "scanned_dirs": [],
    }

    def _projects_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_projects_payload(kind)),
        )

    def _list_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(list_payload),
        )

    def _detail_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(detail),
        )

    page.route("**/api/context/projects", _projects_handler)
    # ``page.route`` resolves last-registered-first-matched, so the broad
    # list glob goes FIRST and the narrower detail glob goes LAST. This
    # is the same pattern test_context_gateway_lists.py uses.
    page.route(f"**/api/context/{kind}", _list_handler)
    page.route(f"**/api/context/{kind}/{detail['name']}**", _detail_handler)


def _open_detail(page, kind: str, name: str) -> None:
    """Navigate Settings → ctx-{kind}, wait for the cwd group items to
    render, click the card, await the detail meta header to mount.
    """
    page.evaluate("() => activateTab('settings')")
    page.evaluate(f"() => switchSettingsSection('ctx-{kind}')")
    # Wait for the cwd group items to populate (it auto-opens because
    # ``_ctxScopeIsServerCwd`` is true; the items fetch fires once).
    page.wait_for_function(
        f"() => document.querySelector("
        f"  '#ctx-{kind}-list details[data-scope-id=\"cwd-scope\"] .ctx-card'"
        f") !== null",
        timeout=5_000,
    )
    page.locator(f"#ctx-{kind}-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_function(
        f"() => {{"
        f"  const detail = document.getElementById('ctx-{kind}-detail');"
        f"  return detail && !detail.hidden && "
        f"    detail.querySelector('.ctx-detail-meta') !== null;"
        f"}}",
        timeout=5_000,
    )


def test_skill_detail_meta_header_renders_description_scope_layout(page, mm_web_url: str) -> None:
    """Skill detail meta header surfaces description / scope / layout /
    last synced. No chip row (skills have no analogous parsed-field set).
    """
    install_default_stubs(page)
    _stub_list_and_detail(page, "skills", _SKILL_DETAIL)

    page.goto(mm_web_url)
    _open_detail(page, "skills", "my-skill")

    meta_text = page.locator("#ctx-skills-detail .ctx-detail-meta").text_content() or ""
    assert "A reusable skill for X." in meta_text, (
        f"Skill description must appear in meta header, got {meta_text!r}"
    )
    assert "Directory" in meta_text or "dir" in meta_text.lower(), (
        f"Skill layout chip must appear in meta header, got {meta_text!r}"
    )
    assert "aux file" in meta_text.lower(), (
        f"Skill file count must appear in meta header, got {meta_text!r}"
    )

    # Symmetric negative pin: skills must NOT render the agent/command
    # chip row.
    chip_row = page.locator("#ctx-skills-detail .ctx-detail-chips")
    assert chip_row.count() == 0, "Skills detail must not include the agent/command chip row"


def test_agent_detail_chip_row_renders_role_isolation_kind_temperature(
    page, mm_web_url: str
) -> None:
    """Agent detail surfaces the parsed-field chip row above the tab strip."""
    install_default_stubs(page)
    _stub_list_and_detail(page, "agents", _AGENT_DETAIL)

    page.goto(mm_web_url)
    _open_detail(page, "agents", "code-reviewer")

    chips_locator = page.locator("#ctx-agents-detail .ctx-detail-chip")
    chips_locator.first.wait_for(timeout=4_000)
    chips_text = chips_locator.all_text_contents()
    flat = " | ".join(chips_text)
    assert "reviewer" in flat, f"Agent role chip missing, got chips = {chips_text!r}"
    assert "worktree" in flat, f"Agent isolation chip missing, got {chips_text!r}"
    assert "tool" in flat, f"Agent kind chip missing, got {chips_text!r}"
    assert "0.2" in flat, f"Agent temperature chip missing, got {chips_text!r}"


def test_command_detail_chip_row_renders_argument_hint_tools_model(page, mm_web_url: str) -> None:
    """Command detail surfaces argument_hint / allowed_tools / model chips.

    Commands live in the dev tier (``data-ui-tier="dev"``), so the test
    has to flip the SPA into dev mode — the prod-mode redirect path
    otherwise bounces ``switchSettingsSection('ctx-commands')`` to the
    first visible section.
    """
    install_default_stubs(page)
    page.route(
        "**/api/system/ui-mode",
        lambda r: r.fulfill(
            status=200, content_type="application/json", body=json.dumps({"mode": "dev"})
        ),
    )
    _stub_list_and_detail(page, "commands", _COMMAND_DETAIL)

    page.goto(mm_web_url)
    _open_detail(page, "commands", "review")

    chips_locator = page.locator("#ctx-commands-detail .ctx-detail-chip")
    chips_locator.first.wait_for(timeout=4_000)
    chips_text = chips_locator.all_text_contents()
    flat = " | ".join(chips_text)
    assert "<pr-number>" in flat, f"Command argument_hint chip missing, got {chips_text!r}"
    assert "Read" in flat and "Bash" in flat, (
        f"Command allowed_tools chip must include the tool list, got {chips_text!r}"
    )
    assert "claude-opus-4-7" in flat, f"Command model chip missing, got {chips_text!r}"


def test_agent_detail_omits_chip_when_field_is_empty(page, mm_web_url: str) -> None:
    """Missing parsed fields must NOT render an empty chip (regression
    guard for ``temperature: undefined`` rendering as literal
    ``undefined``).
    """
    install_default_stubs(page)
    sparse = {**_AGENT_DETAIL, "fields": {"description": "Reviews code", "role": "reviewer"}}
    _stub_list_and_detail(page, "agents", sparse)

    page.goto(mm_web_url)
    _open_detail(page, "agents", "code-reviewer")

    chips_locator = page.locator("#ctx-agents-detail .ctx-detail-chip")
    chips_text = chips_locator.all_text_contents()
    flat = " ".join(chips_text)
    assert "undefined" not in flat.lower(), (
        f"Empty fields must not render as 'undefined' chips, got {chips_text!r}"
    )
    # Role chip should still be present.
    assert "reviewer" in flat, f"Populated role chip dropped, got {chips_text!r}"
