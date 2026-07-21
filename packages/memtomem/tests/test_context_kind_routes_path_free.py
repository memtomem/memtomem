"""Path-disclosure regression pins for the skills / commands / agents context
routes — the #1412 sweep that never finished (dde6fd33 hardened only
mcp-servers) — plus the settings axis that same sweep never covered (#1550,
section (d) below).

Two legs, both already fixed for mcp-servers, previously drifted here:

(a) ``_safe_rel`` under a symlinked / case-variant ``project_root`` emitted the
    ABSOLUTE resolved canonical path in ``canonical_path``: the naive per-kind
    copies only tried ``relative_to`` against the bare (unresolved) root, so a
    resolved canonical (``canonical_artifact_dir`` calls ``.resolve()``) fell
    through to the absolute fallback and leaked ``$HOME`` + the OS username to
    the loopback dashboard. Mirrors
    ``TestMcpServersParseBranchPathFree.test_list_symlinked_root_is_path_free``.

(c) The ``rendered_command`` / ``rendered_agent`` parse-error 422 embedded the
    raw ``str(exc)`` whose message carries the absolute source ``Path`` (the
    parsers raise ``... (source: {path})`` / ``missing YAML frontmatter:
    {path}``). Now routed through ``sanitize_diff_reason`` — the #1412 fix class
    for the mcp parse-error 422, here for commands + agents.

(Leg (b) — the mcp delete-skip raw ``str(exc)`` — lives next to the other mcp
delete tests in ``test_web_routes_context_mcp_servers.py``.)
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from memtomem.web.routes import context_agents, context_commands, context_skills


def _client_for(router: APIRouter, project_root: Path) -> TestClient:
    """Minimal app exposing one context router with ``project_root`` pinned.

    Mirrors ``TestMcpServersParseBranchPathFree._build_app`` — override
    ``resolve_scope_root`` so the route runs against a real (possibly
    symlinked) root without the selector / eligibility machinery.
    """
    from memtomem.web.routes.context_projects import resolve_scope_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[resolve_scope_root] = lambda: project_root
    return TestClient(app)


def _assert_path_free(body: dict, *leaky: Path) -> None:
    """Whole-body assertion: no absolute path (resolved or not) nor ``$HOME``.

    Serializes the entire response so a future shape change that echoes the
    path under a new key is still caught (mirrors the mcp whole-body check).
    """
    blob = json.dumps(body)
    for p in leaky:
        assert str(p) not in blob, body
        assert str(p.resolve()) not in blob, body
    assert str(Path.home()) not in blob, body


# ── (a) symlinked-root ``_safe_rel`` fallback ────────────────────────────


def _seed_skill(root: Path, name: str = "demo") -> None:
    from memtomem.context.skills import SKILL_MANIFEST

    d = root / ".memtomem" / "skills" / name
    d.mkdir(parents=True)
    (d / SKILL_MANIFEST).write_text("# demo skill\n", encoding="utf-8")


def _seed_command(root: Path, name: str = "demo") -> None:
    p = root / ".memtomem" / "commands" / f"{name}.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")


def _seed_agent(root: Path, name: str = "demo") -> None:
    p = root / ".memtomem" / "agents" / f"{name}.md"
    p.parent.mkdir(parents=True)
    p.write_text("---\nname: demo\ndescription: d\n---\nbody\n", encoding="utf-8")


# kind → (router, list URL, container key, seeder, expected relative path)
_LIST_SPECS: dict[str, tuple[APIRouter, str, str, object, str]] = {
    "skills": (
        context_skills.router,
        "/context/skills",
        "skills",
        _seed_skill,
        ".memtomem/skills/demo",
    ),
    "commands": (
        context_commands.router,
        "/context/commands",
        "commands",
        _seed_command,
        ".memtomem/commands/demo.md",
    ),
    "agents": (
        context_agents.router,
        "/context/agents",
        "agents",
        _seed_agent,
        ".memtomem/agents/demo.md",
    ),
}


@pytest.mark.requires_symlinks
@pytest.mark.parametrize("kind", ["skills", "commands", "agents"])
def test_list_symlinked_root_canonical_path_is_relative(kind: str, tmp_path: Path) -> None:
    router, url, container, seed, expected = _LIST_SPECS[kind]
    real = (tmp_path / "real").resolve()
    real.mkdir()
    seed(real)  # type: ignore[operator]
    link = tmp_path / "link"
    link.symlink_to(real)

    # The route receives the UNRESOLVED symlink as project_root; the engine
    # canonical is ``.resolve()``'d (→ under ``real``), so a bare
    # ``relative_to(link)`` raises ValueError and the naive fallback leaked the
    # absolute resolved path.
    res = _client_for(router, link).get(url)

    assert res.status_code == 200, res.text
    body = res.json()
    item = next(i for i in body[container] if i.get("canonical_path") is not None)
    assert item["canonical_path"] == expected, item
    _assert_path_free(body, real, real / ".memtomem")


def test_list_prefix_colliding_reason_is_path_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The bare list sanitizer must not emit a sibling path tail (#1889)."""
    from memtomem.context._runtime_targets import DiffRow

    _seed_skill(tmp_path)
    sibling = tmp_path.with_name(tmp_path.name + "-private") / "team" / "secret.md"
    monkeypatch.setattr(
        context_skills,
        "diff_skills",
        lambda *_a, **_k: [
            DiffRow("claude_skills", "demo", "parse error", f"unreadable: {sibling}")
        ],
    )

    res = _client_for(context_skills.router, tmp_path).get("/context/skills")

    assert res.status_code == 200, res.text
    body = res.json()
    reason = body["skills"][0]["runtimes"][0]["reason"]
    assert reason == "unreadable: <path>"
    assert tmp_path.name + "-private" not in json.dumps(body)
    assert "secret.md" not in json.dumps(body)


# ── (c) rendered parse-error 422 ─────────────────────────────────────────


def test_rendered_command_parse_error_422_is_path_free(tmp_path: Path) -> None:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    p = tmp_path / ".memtomem" / "commands" / "broken.md"
    p.parent.mkdir(parents=True)
    # A path-separator name fails ``validate_name`` → ``CommandParseError`` whose
    # message ends with ``(source: {absolute canonical path})``.
    p.write_text("---\nname: bad/name\ndescription: d\n---\nbody\n", encoding="utf-8")

    res = _client_for(context_commands.router, tmp_path).get("/context/commands/broken/rendered")

    assert res.status_code == 422, res.text
    body = res.json()
    assert body["detail"]["error_kind"] == "parse", body
    canon = canonical_artifact_dir("commands", "project_shared", tmp_path) / "broken.md"
    _assert_path_free(body, canon, canon.parent, tmp_path)
    assert "Parse error" in body["detail"]["message"], body


def test_rendered_agent_parse_error_422_is_path_free(tmp_path: Path) -> None:
    from memtomem.context.scope_resolver import canonical_artifact_dir

    p = tmp_path / ".memtomem" / "agents" / "broken.md"
    p.parent.mkdir(parents=True)
    # Agents REQUIRE frontmatter → ``AgentParseError("missing YAML frontmatter:
    # {absolute canonical path}")``.
    p.write_text("no frontmatter, just a body\n", encoding="utf-8")

    res = _client_for(context_agents.router, tmp_path).get("/context/agents/broken/rendered")

    assert res.status_code == 422, res.text
    body = res.json()
    assert body["detail"]["error_kind"] == "parse", body
    canon = canonical_artifact_dir("agents", "project_shared", tmp_path) / "broken.md"
    _assert_path_free(body, canon, canon.parent, tmp_path)
    assert "Parse error" in body["detail"]["message"], body


# ── (d) settings axis (#1550): SettingsSyncResult reason/target ──────────


def _settings_results(root: Path) -> dict[str, object]:
    """One result per branch that renders a reason or target on the web wire.

    Mirrors ``test_server_tools_context_redaction._settings_results`` (the
    MCP-twin pin from the #1539 fix) so the two boundaries redact the SAME
    engine rows.
    """
    from memtomem.context.settings import SettingsSyncResult

    return {
        "claude_settings": SettingsSyncResult(
            status="ok", target=root / ".claude" / "settings.json"
        ),
        "user_settings": SettingsSyncResult(
            status="needs_confirmation",
            reason=f"{Path.home() / '.claude' / 'settings.json'} is outside the project "
            "root; pass allow_host_writes=True.",
            target=Path.home() / ".claude" / "settings.json",
        ),
        "codex_settings": SettingsSyncResult(
            status="error",
            reason=f"{root / '.memtomem' / 'settings.json'} is not valid JSON "
            "(or not a JSON object).",
        ),
    }


def _settings_client(project_root: Path) -> TestClient:
    """`_client_for` variant for the write route (``resolve_writable_scope_root``)."""
    from memtomem.web.routes import settings_sync
    from memtomem.web.routes.context_projects import resolve_writable_scope_root

    app = FastAPI()
    app.include_router(settings_sync.router)
    app.dependency_overrides[resolve_writable_scope_root] = lambda: project_root
    return TestClient(app)


def test_settings_sync_results_reason_and_target_are_path_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """POST /settings-sync rows redact reason + target (#1550).

    The raw engine rows embed the absolute canonical/target paths
    (``context/settings.py`` f-strings) and the ok/needs_confirmation
    ``target`` is an absolute — $HOME-anchored for user scope — path.
    """
    from memtomem.web.routes import settings_sync as ss

    monkeypatch.setattr(ss, "generate_all_settings", lambda *a, **k: _settings_results(tmp_path))
    monkeypatch.setattr(ss, "detect_duplicate_tiers", lambda *a, **k: [])

    res = _settings_client(tmp_path).post("/settings-sync")

    assert res.status_code == 200, res.text
    body = res.json()
    _assert_path_free(body, tmp_path, tmp_path / ".memtomem", tmp_path / ".claude")
    rows = {r["name"]: r for r in body["results"]}
    # Project-tier target relativizes; the user-tier target $HOME-collapses —
    # the host-write confirm modal still shows the exact host target.
    assert rows["claude_settings"]["target"] == str(Path(".claude/settings.json")), rows
    assert rows["user_settings"]["target"] == os.path.join("~", ".claude", "settings.json"), rows
    # Diagnostics survive, path-stripped (actionable which-file remainder).
    assert "is not valid JSON" in rows["codex_settings"]["reason"], rows
    assert "outside the project root" in rows["user_settings"]["reason"], rows


def test_get_settings_sync_malformed_target_payload_is_path_free(tmp_path: Path) -> None:
    """GET /settings-sync sanitizes ``error`` AND the echoed path fields.

    ``_compare_hooks`` builds the malformed-JSON ``error`` from the absolute
    target path and echoes ``canonical_path`` / ``target_path`` raw — for a
    project under ``$HOME`` (the normal case) that is the same OS-username
    disclosure the #1412 ``_safe_rel`` sweep closed on the kind routes.
    """
    from memtomem.web.routes import settings_sync

    target = tmp_path / ".claude" / "settings.json"
    target.parent.mkdir(parents=True)
    target.write_text("{not json", encoding="utf-8")

    res = _client_for(settings_sync.router, tmp_path).get(
        "/settings-sync?target_scope=project_shared"
    )

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["status"] == "error", data
    _assert_path_free(data, tmp_path)
    assert "is not valid JSON" in data["error"], data
    assert ".claude" in data["error"], data  # which-file info survives
    assert data["canonical_path"] == str(Path(".memtomem/settings.json")), data
    assert data["target_path"] == str(Path(".claude/settings.json")), data


def test_get_settings_sync_user_scope_target_path_collapses_home(tmp_path: Path) -> None:
    """``target_scope=user`` echoes the $HOME-anchored target collapsed to ``~``.

    Field-level assert only: this scope reads the REAL ``~/.claude`` tier, so a
    developer machine's own hook rules may legitimately carry home paths inside
    the ``target_hooks`` rows (user content, not engine leakage).
    """
    from memtomem.web.routes import settings_sync

    res = _client_for(settings_sync.router, tmp_path).get("/settings-sync?target_scope=user")

    assert res.status_code == 200, res.text
    data = res.json()
    assert data["target_path"] == os.path.join("~", ".claude", "settings.json"), data
    assert data["canonical_path"] == str(Path(".memtomem/settings.json")), data


def test_settings_sync_duplicate_tier_path_is_sanitized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``duplicate_tier_warnings[*].path`` collapses the user-tier $HOME path.

    The Codex round on this fix (#1550) caught that ``_serialize_duplicate_tiers``
    still emitted ``str(dup.path)`` raw while the sibling ``results[*].target``
    was being sanitized — same payload, same disclosure class.
    """
    from memtomem.context.settings_doctor import DuplicateTier, HookSignature
    from memtomem.web.routes import settings_sync as ss

    user_dup = DuplicateTier(
        tier="user",
        path=Path.home() / ".claude" / "settings.json",
        entries=(HookSignature("PreToolUse", "Bash", "echo ok"),),
    )
    monkeypatch.setattr(ss, "generate_all_settings", lambda *a, **k: {})
    monkeypatch.setattr(ss, "detect_duplicate_tiers", lambda *a, **k: [user_dup])

    res = _settings_client(tmp_path).post("/settings-sync")

    assert res.status_code == 200, res.text
    body = res.json()
    _assert_path_free(body, tmp_path)
    dup = body["duplicate_tier_warnings"][0]
    assert dup["path"] == os.path.join("~", ".claude", "settings.json"), dup
    assert dup["tier"] == "user", dup  # banner keys survive for the JS renderer


def test_promote_privacy_block_422_hint_is_path_free(tmp_path: Path) -> None:
    """The Gate A 422 remediation hint names the file without the absolute path.

    The hint is the remediation-critical channel — it must keep pointing at the
    exact file holding the secret — but the root-stripped form does that
    without echoing the absolute host path (#1550 Codex round, leg 2).
    """
    from memtomem.web.routes import settings_sync
    from memtomem.web.routes.settings_sync import _rule_hash

    target = tmp_path / ".claude" / "settings.local.json"
    target.parent.mkdir(parents=True)
    rule = {
        "matcher": "Bash",
        "hooks": [{"type": "command", "command": "echo api_key=AKIA1234567890ABCDEF"}],
    }
    target.write_text(json.dumps({"hooks": {"PreToolUse": [rule]}}), encoding="utf-8")

    res = _client_for(settings_sync.router, tmp_path).post(
        "/settings-sync/rules/promote?target_scope=project_local",
        json={
            "event": "PreToolUse",
            "matcher": "Bash",
            "rule_index": 0,
            "rule_hash": _rule_hash(rule),
            "target_mtime_ns": str(target.stat().st_mtime_ns),
            "canonical_mtime_ns": None,
            "confirm_private_to_shared": True,
        },
    )

    assert res.status_code == 422, res.text
    detail = res.json()["detail"]
    assert "Gate A" in detail, detail
    assert str(tmp_path) not in detail, detail
    assert str(tmp_path.resolve()) not in detail, detail
    assert str(Path.home()) not in detail, detail
    # The which-file remediation hint survives, root-stripped.
    assert str(Path(".claude/settings.local.json")) in detail, detail
    # The matched secret bytes never echo back (existing Gate A contract).
    assert "AKIA1234567890ABCDEF" not in res.text
