"""Project-scope context writes, and pulls, are not carried through a link in the project.

A checkout can deliver any directory as a symbolic link: ``.claude -> ~/.claude``
would carry a project-scope skills push into the user's home, ``.github`` the
generated ``copilot-instructions.md``, ``.gitignore`` the marker block, and
``.mcp.json -> ~/.claude.json`` the project's MCP servers. Each entry from the
project root down to the target is judged by ``lstat``; where the link points
does not matter. Display reads (diff, listing, preview) still follow the link.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from httpx import ASGITransport, AsyncClient

from memtomem.cli import cli
from memtomem.config import ContextGatewayConfig, Mem2MemConfig
from memtomem.context._runtime_targets import (
    RUNTIME_FANOUT_TABLE,
    linked_runtime_reads,
    runtime_fanout_root,
)
from memtomem.memory_scope import (
    ProjectEntryRelinked,
    refuse_relinked_project_entry,
    relinked_project_entry,
)
from memtomem.web.app import create_app

from .helpers import set_home

_SKILL = "---\nname: demo\ndescription: d\n---\nPROJECT SKILL\n"
_AGENT = "---\nname: demo\ndescription: d\n---\nPROJECT AGENT\n"


def _link(link: Path, target: Path, *, directory: bool = True) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        link.symlink_to(target, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - Windows without the right
        pytest.skip(f"cannot create a symlink here: {exc}")


def _files(root: Path) -> dict[str, bytes]:
    """Every regular file under ``root``, links not followed."""
    if not root.exists():
        return {}
    return {
        p.relative_to(root).as_posix(): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home


@pytest.fixture
def project(tmp_path: Path, home: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "proj"
    (root / ".git").mkdir(parents=True)
    store = root / ".memtomem"
    (store / "skills" / "demo").mkdir(parents=True)
    (store / "skills" / "demo" / "SKILL.md").write_text(_SKILL, encoding="utf-8")
    (store / "agents").mkdir()
    (store / "agents" / "demo.md").write_text(_AGENT, encoding="utf-8")
    (store / "context.md").write_text("## Project\n\nPROJECT CONTEXT\n", encoding="utf-8")
    (store / "mcp-servers").mkdir()
    (store / "mcp-servers" / "pg.json").write_text(
        json.dumps({"command": "echo", "args": ["PROJECT"]}), encoding="utf-8"
    )
    monkeypatch.chdir(root)
    return root


def _mm(*args: str):
    return CliRunner().invoke(cli, list(args), input="n\n")


# ── The rule ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("depth", [1, 2, 3])
def test_the_first_linked_entry_is_named(tmp_path, depth):
    project, outside = tmp_path / "p", tmp_path / "o"
    project.mkdir()
    outside.mkdir()
    rel = Path("a/b/c/leaf.md")
    entry = Path(*rel.parts[:depth])
    (project / entry).parent.mkdir(parents=True, exist_ok=True)
    _link(project / entry, outside)

    assert relinked_project_entry(project, rel, judge_leaf=False) == entry.as_posix()
    with pytest.raises(ProjectEntryRelinked) as caught:
        refuse_relinked_project_entry(project, rel, judge_leaf=False)
    assert caught.value.entry == entry.as_posix()
    assert str(tmp_path) not in str(caught.value)


def test_the_leaf_is_judged_only_when_asked(tmp_path):
    project = tmp_path / "p"
    project.mkdir()
    victim = tmp_path / "victim"
    victim.write_text("x", encoding="utf-8")
    _link(project / ".gitignore", victim, directory=False)

    assert relinked_project_entry(project, Path(".gitignore"), judge_leaf=False) is None
    assert relinked_project_entry(project, Path(".gitignore"), judge_leaf=True) == ".gitignore"


def test_a_link_at_or_above_the_project_root_is_not_judged(tmp_path):
    real = tmp_path / "real"
    (real / ".claude" / "skills").mkdir(parents=True)
    linked_root = tmp_path / "linked-root"
    _link(linked_root, real)

    assert relinked_project_entry(linked_root, Path(".claude/skills"), judge_leaf=True) is None


@pytest.mark.parametrize("rel", ["/abs/path", "../outside", "a/../../b", ""])
def test_a_path_that_is_not_inside_the_project_is_rejected(tmp_path, rel):
    with pytest.raises(ValueError, match="not a path inside the project"):
        relinked_project_entry(tmp_path, Path(rel), judge_leaf=True)


def test_a_junction_counts_as_a_link(tmp_path, monkeypatch):
    """Injected off Windows, as in the pinned tests."""
    project = tmp_path / "p"
    (project / ".claude" / "skills").mkdir(parents=True)
    entry = project / ".claude"
    monkeypatch.setattr(Path, "is_junction", lambda self: self == entry)

    assert relinked_project_entry(project, Path(".claude/skills"), judge_leaf=True) == ".claude"


# ── Every project-scope fan-out root, from the table ────────────────────

_PROJECT_ROOTS = sorted(
    (key, tail)
    for key, tail in RUNTIME_FANOUT_TABLE.items()
    if key[2] != "user" and isinstance(tail, Path)
)


@pytest.mark.parametrize(
    ("key", "tail", "depth"),
    [(key, tail, d) for key, tail in _PROJECT_ROOTS for d in range(1, len(tail.parts) + 1)],
    ids=lambda v: "-".join(v) if isinstance(v, tuple) else str(v),
)
def test_every_project_fanout_root_refuses_a_link_at_each_entry(tmp_path, key, tail, depth):
    artifact, runtime, scope = key
    project, outside = tmp_path / "p", tmp_path / "o"
    project.mkdir()
    outside.mkdir()
    entry = Path(*tail.parts[:depth])
    _link(project / entry, outside)

    with pytest.raises(ProjectEntryRelinked) as caught:
        runtime_fanout_root(artifact, runtime, scope, project)
    assert caught.value.entry == entry.as_posix()

    with linked_runtime_reads():
        shown = runtime_fanout_root(artifact, runtime, scope, project)
    assert shown is not None and shown.is_relative_to(outside.resolve())


def test_a_project_without_links_resolves_as_before(tmp_path):
    assert _PROJECT_ROOTS, "the table lost its project-scope rows"
    for (artifact, runtime, scope), tail in _PROJECT_ROOTS:
        assert (
            runtime_fanout_root(artifact, runtime, scope, tmp_path) == (tmp_path / tail).resolve()
        )


# ── CLI: the measured routes ────────────────────────────────────────────


def test_sync_through_a_linked_claude_dir_writes_nothing_there(project, home):
    target = home / "dot-claude"
    target.mkdir()
    _link(project / ".claude", target)
    (project / ".gemini").mkdir()

    res = _mm("context", "sync", "--include", "skills,agents", "--scope", "project_shared")

    assert res.exit_code == 1, res.output
    assert _files(target) == {}
    assert ".claude is a symbolic link or junction inside the project" in res.output
    # The other runtimes still get their copy.
    assert (project / ".gemini" / "skills" / "demo" / "SKILL.md").read_text() == _SKILL
    assert (project / ".gemini" / "agents" / "demo.md").exists()


def test_sync_without_links_still_exits_zero(project):
    res = _mm("context", "sync", "--include", "skills", "--scope", "project_shared")

    assert res.exit_code == 0, res.output
    assert (project / ".claude" / "skills" / "demo" / "SKILL.md").read_text() == _SKILL


@pytest.mark.parametrize("entry", [".claude/skills", ".agents", ".kimi"])
def test_sync_through_other_linked_runtime_dirs(project, home, entry):
    target = home / "target"
    target.mkdir()
    _link(project / entry, target)

    res = _mm("context", "sync", "--include", "skills", "--scope", "project_shared")

    assert res.exit_code == 1, res.output
    assert _files(target) == {}


def test_generate_through_a_linked_github_dir_writes_nothing_there(project, home):
    target = home / "gh"
    target.mkdir()
    _link(project / ".github", target)

    res = _mm("context", "generate", "--agent", "all")

    assert res.exit_code == 1, res.output
    assert _files(target) == {}
    assert "PROJECT CONTEXT" in (project / "CLAUDE.md").read_text()


def test_sync_does_not_rewrite_a_detected_file_under_a_linked_dir(project, home):
    target = home / "gh"
    target.mkdir()
    (target / "copilot-instructions.md").write_text("ORIGINAL\n", encoding="utf-8")
    _link(project / ".github", target)

    res = _mm("context", "sync")

    assert res.exit_code == 1, res.output
    assert (target / "copilot-instructions.md").read_text() == "ORIGINAL\n"


def test_init_does_not_read_a_detected_file_under_a_linked_dir(project, home):
    target = home / "gh"
    target.mkdir()
    (target / "copilot-instructions.md").write_text("## Rules\n\nHOST TEXT\n", encoding="utf-8")
    _link(project / ".github", target)
    (project / ".memtomem" / "context.md").unlink()

    res = _mm("context", "init")

    assert res.exit_code == 1, res.output
    assert "HOST TEXT" not in (project / ".memtomem" / "context.md").read_text()


@pytest.mark.parametrize(
    "argv",
    [("mem", "init", "--scope", "project_local"), ("context", "init", "--scope", "project_local")],
    ids=["mem-init", "context-init"],
)
def test_the_gitignore_marker_is_not_appended_through_a_link(project, home, argv):
    victim = home / "victim"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    _link(project / ".gitignore", victim, directory=False)

    res = _mm(*argv)

    assert res.exit_code == 1, res.output
    assert victim.read_text() == "ORIGINAL\n"


def test_mem_init_does_not_register_the_tier_when_gitignore_is_a_link(project, home):
    victim = home / "victim"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    _link(project / ".gitignore", victim, directory=False)

    res = _mm("mem", "init", "--scope", "project_local")

    assert res.exit_code == 1, res.output
    config = home / ".memtomem" / "config.json"
    assert "memories.local" not in (config.read_text() if config.exists() else "")
    assert not (project / ".memtomem" / "memories.local").exists()


@pytest.mark.parametrize("required", [True, False])
def test_the_shared_gitignore_helper_refuses_a_link(project, home, required):
    """``required=True`` is the bundle-import path: a tier it cannot protect is refused."""
    import click

    from memtomem.cli.context_cmd import _ensure_local_tier_gitignored

    victim = home / "victim"
    victim.write_text("ORIGINAL\n", encoding="utf-8")
    _link(project / ".gitignore", victim, directory=False)

    if required:
        with pytest.raises(click.ClickException, match=r"\.gitignore is a symbolic link"):
            _ensure_local_tier_gitignored(project, required=True)
    else:
        _ensure_local_tier_gitignored(project)
    assert victim.read_text() == "ORIGINAL\n"


def test_mcp_servers_are_not_merged_through_a_linked_mcp_json(project, home):
    victim = home / "claude.json"
    original = json.dumps({"mcpServers": {"keep": {"command": "keep"}}}) + "\n"
    victim.write_text(original, encoding="utf-8")
    _link(project / ".mcp.json", victim, directory=False)

    res = _mm("context", "sync", "--include", "mcp-servers")

    assert res.exit_code == 1, res.output
    assert victim.read_text() == original
    assert sorted(p.name for p in home.iterdir()) == ["claude.json"]  # no lock sidecar


def _host_skill(target: Path, body: str = "---\nname: demo\ndescription: d\n---\nHOST\n") -> None:
    (target / "skills" / "demo").mkdir(parents=True)
    (target / "skills" / "demo" / "SKILL.md").write_text(body, encoding="utf-8")


def test_pull_from_a_linked_runtime_dir_copies_nothing(project, home):
    import shutil

    shutil.rmtree(project / ".memtomem" / "skills")
    target = home / "dot-claude"
    _host_skill(target)
    _link(project / ".claude", target)

    res = _mm(
        "context",
        "pull",
        "skills",
        "demo",
        "--from",
        "claude",
        "--apply",
        "--yes",
        "--scope",
        "project_shared",
        "--confirm-project-shared",
    )

    assert res.exit_code == 1, res.output
    assert not (project / ".memtomem" / "skills" / "demo").exists()


def test_pull_from_another_runtime_is_not_blocked_by_a_linked_one(project, home):
    import shutil

    shutil.rmtree(project / ".memtomem" / "skills")
    target = home / "dot-gemini"
    _host_skill(target)
    _link(project / ".gemini", target)
    claude = project / ".claude" / "skills" / "demo"
    claude.mkdir(parents=True)
    (claude / "SKILL.md").write_text(_SKILL, encoding="utf-8")

    res = _mm(
        "context",
        "pull",
        "skills",
        "demo",
        "--from",
        "claude",
        "--apply",
        "--yes",
        "--scope",
        "project_shared",
        "--confirm-project-shared",
    )

    assert res.exit_code == 0, res.output
    assert (project / ".memtomem" / "skills" / "demo" / "SKILL.md").read_text() == _SKILL


def test_pull_with_no_source_named_refuses_when_one_runtime_is_linked(project, home):
    import shutil

    shutil.rmtree(project / ".memtomem" / "skills")
    target = home / "dot-gemini"
    _host_skill(target)
    _link(project / ".gemini", target)
    claude = project / ".claude" / "skills" / "demo"
    claude.mkdir(parents=True)
    (claude / "SKILL.md").write_text(_SKILL, encoding="utf-8")

    res = _mm(
        "context",
        "pull",
        "skills",
        "demo",
        "--apply",
        "--yes",
        "--scope",
        "project_shared",
        "--confirm-project-shared",
    )

    assert res.exit_code != 0, res.output
    assert not (project / ".memtomem" / "skills" / "demo").exists()


def test_init_import_skips_a_linked_runtime_and_keeps_the_others(project, home):
    import shutil

    shutil.rmtree(project / ".memtomem" / "skills")
    target = home / "dot-claude"
    _host_skill(target)
    _link(project / ".claude", target)
    gemini = project / ".gemini" / "skills" / "other"
    gemini.mkdir(parents=True)
    (gemini / "SKILL.md").write_text("---\nname: other\ndescription: d\n---\nG\n", "utf-8")

    res = _mm(
        "context",
        "init",
        "--include",
        "skills",
        "--scope",
        "project_shared",
        "--confirm-project-shared",
    )

    assert res.exit_code == 1, res.output
    assert not (project / ".memtomem" / "skills" / "demo").exists()
    assert (project / ".memtomem" / "skills" / "other" / "SKILL.md").exists()


def test_a_claude_only_commands_import_ignores_a_linked_gemini_dir(project, home):
    target = home / "dot-gemini"
    (target / "commands").mkdir(parents=True)
    (target / "commands" / "hostcmd.toml").write_text('prompt = "HOST"\n', encoding="utf-8")
    _link(project / ".gemini", target)
    cmds = project / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "mine.md").write_text("---\ndescription: d\n---\nMINE\n", encoding="utf-8")

    from memtomem.context.commands import extract_commands_to_canonical

    result = extract_commands_to_canonical(project, scope="project_shared", source_runtime="claude")

    assert [p.relative_to(project).parts[2] for p, _layout in result.imported] == ["mine"]
    assert not any(code == "project_entry_relinked" for _n, _r, code in result.skipped)


def test_a_commands_import_with_gemini_skips_a_linked_gemini_dir(project, home):
    target = home / "dot-gemini"
    (target / "commands").mkdir(parents=True)
    (target / "commands" / "hostcmd.toml").write_text('prompt = "HOST"\n', encoding="utf-8")
    _link(project / ".gemini", target)
    cmds = project / ".claude" / "commands"
    cmds.mkdir(parents=True)
    (cmds / "mine.md").write_text("---\ndescription: d\n---\nMINE\n", encoding="utf-8")

    from memtomem.context.commands import extract_commands_to_canonical

    result = extract_commands_to_canonical(project, scope="project_shared")

    assert [p.relative_to(project).parts[2] for p, _layout in result.imported] == ["mine"]
    assert [(n, code) for n, _r, code in result.skipped] == [("gemini", "project_entry_relinked")]
    assert not (project / ".memtomem" / "commands" / "hostcmd").exists()


def test_init_agents_import_skips_a_linked_runtime_and_keeps_the_others(project, home):
    import shutil

    shutil.rmtree(project / ".memtomem" / "agents")
    target = home / "dot-claude"
    (target / "agents").mkdir(parents=True)
    (target / "agents" / "hostagent.md").write_text(_AGENT.replace("demo", "hostagent"), "utf-8")
    _link(project / ".claude", target)
    gemini = project / ".gemini" / "agents"
    gemini.mkdir(parents=True)
    (gemini / "other.md").write_text(_AGENT.replace("demo", "other"), "utf-8")

    res = _mm(
        "context",
        "init",
        "--include",
        "agents",
        "--scope",
        "project_shared",
        "--confirm-project-shared",
    )

    assert res.exit_code == 1, res.output
    agents = project / ".memtomem" / "agents"
    assert not any("hostagent" in p.as_posix() for p in agents.rglob("*"))
    assert any("other" in p.as_posix() for p in agents.rglob("*"))


@pytest.mark.parametrize("apply", [False, True], ids=["preview", "apply"])
def test_move_leaves_a_linked_runtime_copy_in_place(project, home, apply):
    """Cleanup of the source's runtime copies would delete through the link."""
    target = home / "dot-claude"
    _host_skill(target, _SKILL)
    _link(project / ".claude", target)

    argv = [
        "context",
        "move",
        "skills",
        "demo",
        "--from",
        "project_shared",
        "--to",
        "project_local",
    ]
    res = _mm(*argv, *(("--apply", "--yes") if apply else ()))

    assert res.exit_code == 0, res.output
    assert (target / "skills" / "demo" / "SKILL.md").read_text() == _SKILL
    if apply:
        assert (project / ".memtomem" / "skills.local" / "demo" / "SKILL.md").exists()


def test_diff_still_shows_a_linked_runtime_dir(project, home):
    target = home / "dot-claude"
    _host_skill(target)
    _link(project / ".claude", target)

    res = _mm("context", "diff", "--include", "skills", "--scope", "project_shared")

    assert res.exit_code == 0, res.output
    assert "demo" in res.output


def test_diff_still_shows_a_linked_agents_dir(project, home):
    target = home / "dot-claude"
    (target / "agents").mkdir(parents=True)
    (target / "agents" / "demo.md").write_text(_AGENT.replace("PROJECT", "HOST"), "utf-8")
    _link(project / ".claude", target)

    res = _mm("context", "diff", "--include", "agents", "--scope", "project_shared")

    assert res.exit_code == 0, res.output
    assert "out of sync" in res.output


def test_the_pull_preview_still_reads_a_linked_runtime_dir(project, home):
    from memtomem.context.pull_preview import preview_pull

    target = home / "dot-claude"
    _host_skill(target)
    _link(project / ".claude", target)

    preview = preview_pull("skills", "demo", scope="project_shared", project_root=project)

    claude = [c for c in preview.candidates if c.runtime == "claude"]
    assert claude and claude[0].content_status != "landing_error"


def test_all_projects_sync_counts_a_project_with_a_link_as_failed(tmp_path, home, monkeypatch):
    from memtomem.cli.context_cmd import context as context_group

    known = tmp_path / "kp.json"
    monkeypatch.setattr(
        "memtomem.cli.context_cmd.ContextGatewayConfig",
        lambda: ContextGatewayConfig(
            known_projects_path=known, experimental_claude_projects_scan=False
        ),
    )
    roots = []
    for name in ("linked", "clean"):
        root = tmp_path / name
        skill = root / ".memtomem" / "skills" / "demo"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(_SKILL, encoding="utf-8")
        roots.append(root)
    target = home / "dot-claude"
    target.mkdir()
    _link(roots[0] / ".claude", target)
    (roots[1] / ".claude").mkdir()
    known.write_text(
        json.dumps(
            {
                "version": 1,
                "projects": [
                    {
                        "root": str(r),
                        "added_at": "2026-01-01T00:00:00Z",
                        "label": None,
                        "enabled": True,
                    }
                    for r in roots
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.chdir(roots[1])

    res = CliRunner().invoke(
        context_group, ["sync", "--all-projects", "--include", "skills", "--yes"]
    )

    assert res.exit_code == 1, res.output
    assert "1 pushed, 1 failed" in res.output
    assert _files(target) == {}
    assert (roots[1] / ".claude" / "skills" / "demo" / "SKILL.md").exists()


# ── Web ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def client(project, home, tmp_path):
    app = create_app(lifespan=None, mode="dev")
    app.state.project_root = project
    app.state.storage = AsyncMock()
    config = Mem2MemConfig()
    config.context_gateway = ContextGatewayConfig(
        known_projects_path=tmp_path / "kp.json", experimental_claude_projects_scan=False
    )
    app.state.config = config
    for attr in ("search_pipeline", "index_engine", "embedder", "dedup_scanner"):
        setattr(app.state, attr, None)
    app.state.last_reload_error = None
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


async def test_web_skills_sync_skips_a_linked_runtime(client, project, home):
    target = home / "dot-claude"
    target.mkdir()
    _link(project / ".claude", target)

    resp = await client.post("/api/context/skills/sync")

    assert resp.status_code == 200, resp.text
    codes = {row.get("reason_code") for row in resp.json()["skipped"]}
    assert "project_entry_relinked" in codes
    assert _files(target) == {}
    # JSON escapes a Windows backslash, so look for the path as JSON spells it.
    assert json.dumps(str(home))[1:-1] not in resp.text


async def test_web_mcp_servers_sync_refuses_a_linked_mcp_json(client, project, home):
    victim = home / "claude.json"
    original = json.dumps({"mcpServers": {}}) + "\n"
    victim.write_text(original, encoding="utf-8")
    _link(project / ".mcp.json", victim, directory=False)

    resp = await client.post("/api/context/mcp-servers/sync")

    assert resp.status_code == 409, resp.text
    assert victim.read_text() == original
    # JSON escapes a Windows backslash, so look for the path as JSON spells it.
    assert json.dumps(str(home))[1:-1] not in resp.text


async def test_web_sync_all_reports_the_refusal_not_a_500(client, project, home):
    victim = home / "claude.json"
    victim.write_text(json.dumps({"mcpServers": {}}) + "\n", encoding="utf-8")
    _link(project / ".mcp.json", victim, directory=False)

    resp = await client.post("/api/context/sync-all")

    assert resp.status_code == 200, resp.text
    phase = next(p for p in resp.json()["phases"] if p["type"] == "mcp-servers")
    assert phase["status"] == "failed"
    assert phase["error"]["http_status"] == 409


async def test_web_skill_detail_diff_still_shows_a_linked_runtime(client, project, home):
    target = home / "dot-claude"
    _host_skill(target)
    _link(project / ".claude", target)

    resp = await client.get("/api/context/skills/demo/diff")

    assert resp.status_code == 200, resp.text
    claude = [r for r in resp.json()["runtimes"] if r["runtime"] == "claude_skills"]
    assert claude and claude[0]["status"] == "out of sync"


async def test_web_agent_detail_diff_still_shows_a_linked_runtime(client, project, home):
    target = home / "dot-claude"
    (target / "agents").mkdir(parents=True)
    (target / "agents" / "demo.md").write_text(_AGENT.replace("PROJECT", "HOST"), "utf-8")
    _link(project / ".claude", target)

    resp = await client.get("/api/context/agents/demo/diff")

    assert resp.status_code == 200, resp.text
    claude = [r for r in resp.json()["runtimes"] if r["runtime"] == "claude_agents"]
    assert claude and claude[0]["status"] == "out of sync"


async def test_web_cascade_delete_through_a_linked_runtime_is_refused(client, project, home):
    target = home / "dot-claude"
    _host_skill(target)
    _link(project / ".claude", target)

    resp = await client.delete("/api/context/skills/demo", params={"cascade": "true"})

    assert resp.status_code == 409, resp.text
    assert resp.json()["reason_code"] == "project_entry_relinked"
    assert (target / "skills" / "demo" / "SKILL.md").exists()
