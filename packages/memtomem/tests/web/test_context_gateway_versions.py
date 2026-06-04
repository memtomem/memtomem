"""Browser tests for the ADR-0022 detail-panel version manager (PR3).

The detail panel for a directory-layout agent/command grows a "Versions"
section: a list of immutable version snapshots, each with the label pointers
that land on it, a "Freeze current" button, a per-row promote control, and a
per-label remove button. These tests pin that the section renders and that the
freeze / promote / delete-label controls hit the ADR-0022 routes.
"""

from __future__ import annotations

import json

import pytest

from .conftest import install_default_stubs

pytestmark = pytest.mark.browser


_AGENT_DETAIL = {
    "name": "code-reviewer",
    "content": "---\ndescription: Reviews code\n---\n\nBody",
    "mtime_ns": str(1_700_000_000 * 1_000_000_000),
    "target_scope": "project_shared",
    "layout": "dir",
    "fields": {"description": "Reviews code"},
}

_VERSIONS = {
    "name": "code-reviewer",
    "artifact_type": "agents",
    "target_scope": "project_shared",
    "layout": "dir",
    "versions": [
        {"tag": "v2", "created_at": "2026-06-03T11:00:00Z", "note": "stable"},
        {"tag": "v1", "created_at": "2026-06-03T09:00:00Z", "note": ""},
    ],
    "labels": {"production": "v2"},
    "has_versions": True,
    "migrate_required": False,
}


def _projects_payload(kind: str) -> dict:
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
                "counts": {"skills": 0, "commands": 0, "agents": 1},
            }
        ]
    }


def _stub_versions(page, kind: str, detail: dict, versions: dict) -> None:
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
            status=200, content_type="application/json", body=json.dumps(_projects_payload(kind))
        )

    def _list_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(list_payload))

    def _detail_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))

    def _versions_handler(route):
        if route.request.method == "POST":
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"version": {"tag": "v3", "created_at": "", "note": ""}}),
            )
        else:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(versions))

    def _labels_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"labels": {}}))

    page.route("**/api/context/projects**", _projects_handler)
    # ``page.route`` is last-registered-first-matched, so order broad→narrow:
    # list, then detail, then the versions + labels routes win for their URLs.
    page.route(f"**/api/context/{kind}**", _list_handler)
    page.route(f"**/api/context/{kind}/{detail['name']}**", _detail_handler)
    page.route(f"**/api/context/{kind}/{detail['name']}/versions**", _versions_handler)
    page.route(f"**/api/context/{kind}/{detail['name']}/labels/**", _labels_handler)


def _open_detail(page, kind: str, name: str) -> None:
    page.evaluate("() => activateTab('settings')")
    page.evaluate(f"() => switchSettingsSection('ctx-{kind}')")
    page.wait_for_function(
        f"() => document.querySelector("
        f"  '#ctx-{kind}-list details[data-scope-id=\"cwd-scope\"] .ctx-card'"
        f") !== null",
        timeout=5_000,
    )
    page.locator(f"#ctx-{kind}-list details[data-scope-id='cwd-scope'] .ctx-card").first.click()
    page.wait_for_function(
        f"() => {{"
        f"  const d = document.getElementById('ctx-{kind}-detail');"
        f"  return d && !d.hidden && d.querySelector('.ctx-detail-meta') !== null;"
        f"}}",
        timeout=5_000,
    )


def _wait_versions(page, kind: str, count: int) -> None:
    page.wait_for_function(
        f"() => {{"
        f"  const s = document.querySelector('#ctx-{kind}-detail .ctx-detail-versions');"
        f"  return s && !s.hidden && s.querySelectorAll('.ctx-version-row').length === {count};"
        f"}}",
        timeout=5_000,
    )


def test_versions_section_renders_rows_and_label_chip(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_versions(page, "agents", _AGENT_DETAIL, _VERSIONS)
    page.goto(mm_web_url)
    _open_detail(page, "agents", "code-reviewer")
    _wait_versions(page, "agents", 2)

    # Newest first → v2 carries the production label chip; v1 carries none.
    chip = page.locator(
        "#ctx-agents-detail .ctx-version-row[data-tag='v2'] "
        ".ctx-version-label-chip[data-label='production']"
    )
    assert chip.count() == 1
    v1_chips = page.locator(
        "#ctx-agents-detail .ctx-version-row[data-tag='v1'] .ctx-version-label-chip"
    )
    assert v1_chips.count() == 0


def test_freeze_button_posts_to_versions_route(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_versions(page, "agents", _AGENT_DETAIL, _VERSIONS)
    page.goto(mm_web_url)
    _open_detail(page, "agents", "code-reviewer")
    _wait_versions(page, "agents", 2)

    with page.expect_request(
        lambda r: r.method == "POST" and "/api/context/agents/code-reviewer/versions" in r.url
    ) as info:
        page.locator("#ctx-agents-detail .ctx-version-freeze-btn").click()
    assert info.value.method == "POST"


def test_promote_button_puts_selected_label(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_versions(page, "agents", _AGENT_DETAIL, _VERSIONS)
    page.goto(mm_web_url)
    _open_detail(page, "agents", "code-reviewer")
    _wait_versions(page, "agents", 2)

    row = page.locator("#ctx-agents-detail .ctx-version-row[data-tag='v1']")
    row.locator(".ctx-version-label-select").select_option("staging")
    with page.expect_request(lambda r: r.method == "PUT" and "/labels/staging" in r.url) as info:
        row.locator(".ctx-version-promote-btn").click()
    assert info.value.method == "PUT"


def test_remove_label_button_deletes_pointer(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_versions(page, "agents", _AGENT_DETAIL, _VERSIONS)
    page.goto(mm_web_url)
    _open_detail(page, "agents", "code-reviewer")
    _wait_versions(page, "agents", 2)

    with page.expect_request(
        lambda r: r.method == "DELETE" and "/labels/production" in r.url
    ) as info:
        page.locator(
            "#ctx-agents-detail .ctx-version-label-remove[data-label='production']"
        ).click()
    assert info.value.method == "DELETE"


# ── Enable versioning for a flat artifact (ADR-0022 rank 6) ───────────────


_FLAT_AGENT_DETAIL = {
    "name": "flat-agent",
    "content": "---\ndescription: Flat one\n---\n\nBody",
    "mtime_ns": str(1_700_000_000 * 1_000_000_000),
    "target_scope": "project_shared",
    "layout": "flat",
    "fields": {"description": "Flat one"},
}

_MIGRATE_REQUIRED = {
    "name": "flat-agent",
    "artifact_type": "agents",
    "target_scope": "project_shared",
    "layout": "flat",
    "versions": [],
    "labels": {},
    "has_versions": False,
    "migrate_required": True,
}


def _stub_enable(page, kind: str, detail: dict) -> None:
    """Stubs for the flat → migrate_required path: the versions GET reports
    ``migrate_required`` and the ``/versions/enable`` POST succeeds."""
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
            status=200, content_type="application/json", body=json.dumps(_projects_payload(kind))
        )

    def _list_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(list_payload))

    def _detail_handler(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(detail))

    def _versions_handler(route):
        route.fulfill(
            status=200, content_type="application/json", body=json.dumps(_MIGRATE_REQUIRED)
        )

    def _enable_handler(route):
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "name": detail["name"],
                    "artifact_type": kind,
                    "target_scope": "project_shared",
                    "layout": "dir",
                    "migrated": True,
                }
            ),
        )

    page.route("**/api/context/projects**", _projects_handler)
    page.route(f"**/api/context/{kind}**", _list_handler)
    page.route(f"**/api/context/{kind}/{detail['name']}**", _detail_handler)
    page.route(f"**/api/context/{kind}/{detail['name']}/versions**", _versions_handler)
    # Registered last → wins over the broader ``/versions**`` glob for this URL.
    page.route(f"**/api/context/{kind}/{detail['name']}/versions/enable**", _enable_handler)


def _wait_enable_button(page, kind: str) -> None:
    page.wait_for_function(
        f"() => {{"
        f"  const s = document.querySelector('#ctx-{kind}-detail .ctx-detail-versions');"
        f"  return s && !s.hidden && s.querySelector('.ctx-version-enable-btn') !== null;"
        f"}}",
        timeout=5_000,
    )


def test_flat_artifact_shows_enable_versioning_button(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_enable(page, "agents", _FLAT_AGENT_DETAIL)
    page.goto(mm_web_url)
    _open_detail(page, "agents", "flat-agent")
    _wait_enable_button(page, "agents")

    # The freeze/promote controls must NOT be present for a flat artifact —
    # only the enable affordance + the migrate_required hint.
    assert page.locator("#ctx-agents-detail .ctx-version-freeze-btn").count() == 0
    assert page.locator("#ctx-agents-detail .ctx-version-empty").count() == 1


def test_enable_button_posts_to_versions_enable_route(page, mm_web_url: str) -> None:
    install_default_stubs(page)
    _stub_enable(page, "agents", _FLAT_AGENT_DETAIL)
    page.goto(mm_web_url)
    _open_detail(page, "agents", "flat-agent")
    _wait_enable_button(page, "agents")

    with page.expect_request(
        lambda r: r.method == "POST" and "/api/context/agents/flat-agent/versions/enable" in r.url
    ) as info:
        page.locator("#ctx-agents-detail .ctx-version-enable-btn").click()
    assert info.value.method == "POST"
