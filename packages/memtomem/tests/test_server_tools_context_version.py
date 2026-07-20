"""Real-FS integration pins for the ``mem_context_version`` /
``mem_context_promote`` MCP tools (ADR-0022 PR2 — headless-agent parity).

The two tools surface the ADR-0022 edit/deploy split (immutable version
snapshots + movable label pointers) over the SAME pure-filesystem
``context/versioning.py`` store the CLI ``mm context version`` group and the web
``context_versions`` routes use. These tests exercise them against real on-disk
canonical layouts (not mocked) through the same ``.git`` + ``HOME`` fixture the
sibling ``mem_context_artifact_migrate`` tests use, and pin:

* list / create roundtrip across agents + commands, newest-first ordering;
* flat-layout: ``list`` returns a benign migrate hint, ``create`` / ``promote``
  refuse (ADR-0022 invariant 3);
* unsupported type / unknown action / invalid name / missing artifact, plus
  the READ-ONLY ``skills`` version surface (ADR-0030 §10);
* the ``project_shared`` explicit-scope confirmation gate (the implicit default
  scope stays frictionless — mirrors ``mem_context_init``);
* Gate A privacy scan on ``create`` — ``project_shared`` hard-refuses and
  nothing lands, ``user`` is permissive (mirrors the CLI ``version create``);
* promote == rollback (pointer move), delete label, reserved + version-shaped
  label rejection, promote-missing-version, delete+version contradiction;
* ``mem_do`` routing via the canonical action name and the alias.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.server.tools.context import mem_context_promote, mem_context_version

_DIR_FILE = {"agents": "agent.md", "commands": "command.md"}
# A secret that trips Gate A: ``api_key=`` matches the api-key pattern and the
# AKIA token matches the AWS access-key pattern (mirrors the CLI e2e fixture).
_SECRET_BODY = "---\nname: leaky\ndescription: d\n---\n\napi_key=AKIA1234567890ABCDEF\n"


@pytest.fixture
def layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    """A project root (with ``.git`` so ``_find_project_root`` terminates) plus a
    fake ``HOME`` so the ``user`` tier resolves under ``tmp_path``."""
    project_root = tmp_path / "proj"
    project_root.mkdir()
    (project_root / ".git").mkdir()
    user_home = tmp_path / "home"
    user_home.mkdir()
    monkeypatch.setenv("HOME", str(user_home))
    monkeypatch.setenv("USERPROFILE", str(user_home))
    monkeypatch.chdir(project_root)
    return {"project_root": project_root, "user_home": user_home}


def _canonical_root(layout: dict[str, Path], kind: str, scope: str) -> Path:
    if scope == "user":
        return layout["user_home"] / ".memtomem" / kind
    if scope == "project_shared":
        return layout["project_root"] / ".memtomem" / kind
    if scope == "project_local":
        return layout["project_root"] / ".memtomem" / f"{kind}.local"
    raise ValueError(scope)


def _seed_dir(
    layout: dict[str, Path],
    kind: str,
    name: str,
    *,
    scope: str = "project_shared",
    body: str = "a harmless body\n",
) -> Path:
    """Directory-layout canonical artifact; returns the artifact directory."""
    d = _canonical_root(layout, kind, scope) / name
    d.mkdir(parents=True, exist_ok=True)
    (d / _DIR_FILE[kind]).write_text(body, encoding="utf-8")
    return d


def _seed_flat(layout: dict[str, Path], kind: str, name: str) -> Path:
    """Flat-layout canonical (``<kind>/<name>.md``); returns the flat file."""
    flat = _canonical_root(layout, kind, "project_shared") / f"{name}.md"
    flat.parent.mkdir(parents=True, exist_ok=True)
    flat.write_text("# flat\n", encoding="utf-8")
    return flat


# ── list ──────────────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_list_default_action_empty(layout):
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_version(artifact_type="agents", name="foo")
    assert "no versions yet" in out


@pytest.mark.anyio
async def test_list_missing_artifact_errors(layout):
    out = await mem_context_version(artifact_type="agents", name="ghost")
    assert out.startswith("error:") and "not found" in out


@pytest.mark.anyio
async def test_unsupported_type(layout):
    out = await mem_context_version(artifact_type="dogs", name="foo", action="create")
    assert out.startswith("error:") and "agents, commands, skills only" in out


@pytest.mark.anyio
async def test_unknown_action(layout):
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_version(artifact_type="agents", name="foo", action="freeze")
    assert out.startswith("error: unknown action")


@pytest.mark.anyio
async def test_invalid_name(layout):
    out = await mem_context_version(artifact_type="agents", name="../escape", action="list")
    assert out.startswith("error:")


# ── create + list roundtrip ───────────────────────────────────────────────


@pytest.mark.anyio
async def test_create_then_list_roundtrip_agents(layout):
    artifact_dir = _seed_dir(layout, "agents", "foo")

    r1 = await mem_context_version("agents", "foo", action="create", note="first")
    assert "version v1" in r1 and "first" in r1
    assert (artifact_dir / "versions" / "v1.md").is_file()
    assert (artifact_dir / "versions.json").is_file()

    r2 = await mem_context_version("agents", "foo", action="create")
    assert "version v2" in r2
    assert (artifact_dir / "versions" / "v2.md").is_file()

    listing = await mem_context_version("agents", "foo")
    # Newest first: v2 line precedes v1 line.
    assert listing.index("v2") < listing.index("v1")
    assert "first" in listing  # v1's note is shown


@pytest.mark.anyio
async def test_create_roundtrip_commands(layout):
    artifact_dir = _seed_dir(layout, "commands", "mycmd")
    r = await mem_context_version("commands", "mycmd", action="create")
    assert "commands/mycmd version v1" in r
    assert (artifact_dir / "versions" / "v1.md").is_file()


@pytest.mark.anyio
async def test_create_implicit_project_shared_no_confirm(layout):
    # The implicit default scope must stay frictionless (mirrors mem_context_init).
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_version("agents", "foo", action="create")
    assert out.startswith("Created")


@pytest.mark.anyio
async def test_create_explicit_project_shared_requires_confirm(layout):
    artifact_dir = _seed_dir(layout, "agents", "foo")
    out = await mem_context_version("agents", "foo", action="create", scope="project_shared")
    assert out.startswith("needs confirmation")
    assert not (artifact_dir / "versions.json").exists()


@pytest.mark.anyio
async def test_create_explicit_project_shared_with_confirm(layout):
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_version(
        "agents", "foo", action="create", scope="project_shared", confirm_project_shared=True
    )
    assert out.startswith("Created")


@pytest.mark.anyio
async def test_create_user_scope(layout):
    artifact_dir = _seed_dir(layout, "agents", "uagent", scope="user")
    out = await mem_context_version("agents", "uagent", action="create", scope="user")
    assert "[user]" in out
    assert (artifact_dir / "versions" / "v1.md").is_file()


# ── flat-layout (invariant 3) ─────────────────────────────────────────────


@pytest.mark.anyio
async def test_flat_layout_list_returns_hint(layout):
    _seed_flat(layout, "agents", "flatone")
    out = await mem_context_version("agents", "flatone", action="list")
    # Benign hint (parity with the web read route's migrate_required flag),
    # NOT an ``error:`` prefix.
    assert not out.startswith("error:")
    assert "flat layout" in out and "mem_context_artifact_migrate" in out


@pytest.mark.anyio
async def test_flat_layout_create_refuses(layout):
    flat = _seed_flat(layout, "agents", "flatone")
    out = await mem_context_version("agents", "flatone", action="create")
    assert out.startswith("error:") and "flat layout" in out
    # The refusal now leads with the enable action (the lightweight adopt).
    assert "action='enable'" in out
    # Nothing was written next to the flat file.
    assert not (flat.parent / "flatone" / "versions.json").exists()


# ── enable (flat → dir adopt, ADR-0022) ───────────────────────────────────


@pytest.mark.anyio
async def test_enable_flat_adopts_to_dir(layout):
    # The MCP twin of ``mm context version enable`` / web ``POST …/versions/enable``:
    # a byte-identical flat→dir move that unlocks versioning.
    flat = _seed_flat(layout, "agents", "flatone")
    body = flat.read_text(encoding="utf-8")
    out = await mem_context_version("agents", "flatone", action="enable")
    assert out.startswith("Adopted") and "directory layout" in out
    root = _canonical_root(layout, "agents", "project_shared")
    # The flat file moved (byte-identical) into <name>/agent.md.
    assert not flat.exists()
    adopted = root / "flatone" / "agent.md"
    assert adopted.is_file() and adopted.read_text(encoding="utf-8") == body
    # Versioning is now unlocked: create freezes a v1 snapshot.
    created = await mem_context_version("agents", "flatone", action="create")
    assert created.startswith("Created")
    assert (root / "flatone" / "versions" / "v1.md").is_file()


@pytest.mark.anyio
async def test_enable_idempotent_on_dir(layout):
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_version("agents", "foo", action="enable")
    assert not out.startswith("error:")
    assert "already uses directory layout" in out


@pytest.mark.anyio
async def test_enable_commands_artifact(layout):
    # Parity across both versionable types (agents + commands).
    _seed_flat(layout, "commands", "flatcmd")
    out = await mem_context_version("commands", "flatcmd", action="enable")
    assert out.startswith("Adopted")
    root = _canonical_root(layout, "commands", "project_shared")
    assert (root / "flatcmd" / "command.md").is_file()


@pytest.mark.anyio
async def test_enable_flat_dir_collision_refuses(layout):
    # Both <name>.md and <name>/agent.md present (resolver picks dir) → refuse
    # rather than report a misleading idempotent success while a stray flat lingers.
    _seed_flat(layout, "agents", "foo")
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_version("agents", "foo", action="enable")
    assert out.startswith("error:") and "both flat" in out


@pytest.mark.anyio
async def test_enable_orphaned_store_refuses(layout):
    # A flat canonical plus a pre-existing version store under <name>/ (no
    # working canonical) → adopting would silently attach that history; refuse.
    _seed_flat(layout, "agents", "foo")
    root = _canonical_root(layout, "agents", "project_shared")
    (root / "foo" / "versions").mkdir(parents=True)
    out = await mem_context_version("agents", "foo", action="enable")
    assert out.startswith("error:") and "orphaned version store" in out


@pytest.mark.anyio
async def test_enable_missing_artifact_errors(layout):
    out = await mem_context_version("agents", "ghost", action="enable")
    assert out.startswith("error:") and "not found" in out


@pytest.mark.anyio
async def test_enable_explicit_project_shared_confirm_gate(layout):
    _seed_flat(layout, "agents", "flatone")
    gated = await mem_context_version("agents", "flatone", action="enable", scope="project_shared")
    assert gated.startswith("needs confirmation")
    root = _canonical_root(layout, "agents", "project_shared")
    # Untouched until confirmed — the flat file still sits where it was.
    assert (root / "flatone.md").is_file()
    ok = await mem_context_version(
        "agents",
        "flatone",
        action="enable",
        scope="project_shared",
        confirm_project_shared=True,
    )
    assert ok.startswith("Adopted")
    assert (root / "flatone" / "agent.md").is_file()


# ── Gate A privacy scan on create ─────────────────────────────────────────


@pytest.mark.anyio
async def test_privacy_block_project_shared_nothing_lands(layout):
    artifact_dir = _seed_dir(layout, "agents", "leaky", body=_SECRET_BODY)
    out = await mem_context_version("agents", "leaky", action="create")
    assert out.startswith("privacy block:")
    # The version must NOT have been frozen — the scan precedes create_version.
    assert not (artifact_dir / "versions.json").exists()
    assert not (artifact_dir / "versions").exists()


@pytest.mark.anyio
async def test_privacy_permissive_user_tier(layout):
    # user / project_local are permissive: the working file already holds the
    # content locally, so a snapshot adds no exposure (mirrors the CLI).
    artifact_dir = _seed_dir(layout, "agents", "uleaky", scope="user", body=_SECRET_BODY)
    out = await mem_context_version("agents", "uleaky", action="create", scope="user")
    assert out.startswith("Created")
    assert (artifact_dir / "versions" / "v1.md").is_file()


# ── promote / rollback / delete ───────────────────────────────────────────


@pytest.mark.anyio
async def test_promote_then_rollback(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    await mem_context_version("agents", "foo", action="create")

    up = await mem_context_promote("agents", "foo", label="production", version="v2")
    assert "production → v2" in up

    # Rollback is the same act — just move the pointer back.
    down = await mem_context_promote("agents", "foo", label="production", version="v1")
    assert "production → v1" in down

    # Pin WHICH version the pointer lands on: production must render on v1's
    # line (the rollback target) and NOT on v2's — `"[production]" in listing`
    # alone would pass even if the rollback silently didn't move the pointer.
    lines = (await mem_context_version("agents", "foo")).splitlines()
    v1_line = next(ln for ln in lines if ln.strip().startswith("v1 "))
    v2_line = next(ln for ln in lines if ln.strip().startswith("v2 "))
    assert "[production]" in v1_line
    assert "[production]" not in v2_line


@pytest.mark.anyio
async def test_delete_label(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    await mem_context_promote("agents", "foo", label="staging", version="v1")

    out = await mem_context_promote("agents", "foo", label="staging", delete=True)
    assert "Dropped label 'staging'" in out and "(no labels)" in out


@pytest.mark.anyio
async def test_delete_absent_label_is_noop(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    out = await mem_context_promote("agents", "foo", label="nope", delete=True)
    assert "Dropped label 'nope'" in out  # no-op still succeeds


@pytest.mark.anyio
async def test_promote_missing_version_errors(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    out = await mem_context_promote("agents", "foo", label="production", version="v9")
    assert out.startswith("error:") and "v9" in out


@pytest.mark.anyio
async def test_promote_reserved_label_rejected(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    out = await mem_context_promote("agents", "foo", label="latest", version="v1")
    assert out.startswith("error:") and "reserved" in out


@pytest.mark.anyio
async def test_promote_version_shaped_label_rejected(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    out = await mem_context_promote("agents", "foo", label="v1", version="v1")
    assert out.startswith("error:") and "version tag" in out


@pytest.mark.anyio
async def test_promote_requires_version(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    out = await mem_context_promote("agents", "foo", label="production")
    assert out.startswith("error:") and "version is required" in out


@pytest.mark.anyio
async def test_delete_with_version_is_contradiction(layout):
    _seed_dir(layout, "agents", "foo")
    out = await mem_context_promote("agents", "foo", label="x", version="v1", delete=True)
    assert out.startswith("error:") and "takes no version" in out


@pytest.mark.anyio
async def test_promote_flat_layout_refuses(layout):
    _seed_flat(layout, "agents", "flatone")
    out = await mem_context_promote("agents", "flatone", label="production", version="v1")
    assert out.startswith("error:") and "flat layout" in out


@pytest.mark.anyio
async def test_promote_missing_artifact_errors(layout):
    out = await mem_context_promote("agents", "ghost", label="production", version="v1")
    assert out.startswith("error:") and "not found" in out


@pytest.mark.anyio
async def test_promote_explicit_project_shared_confirm_gate(layout):
    _seed_dir(layout, "agents", "foo")
    await mem_context_version("agents", "foo", action="create")
    out = await mem_context_promote(
        "agents", "foo", label="production", version="v1", scope="project_shared"
    )
    assert out.startswith("needs confirmation")


# ── mem_do routing ────────────────────────────────────────────────────────


@pytest.mark.anyio
async def test_mem_do_routes_version_and_promote(layout):
    from memtomem.server.tools.meta import mem_do

    _seed_dir(layout, "agents", "foo")
    # Canonical action name ``context_version`` (there is no "version" alias —
    # that name belongs to mem_version, the protocol-negotiation action).
    created = await mem_do(
        "context_version", {"artifact_type": "agents", "name": "foo", "action": "create"}
    )
    assert created.startswith("Created")
    # Surviving discoverability alias ``promote`` → ``context_promote``.
    promoted = await mem_do(
        "promote",
        {"artifact_type": "agents", "name": "foo", "label": "production", "version": "v1"},
    )
    assert "production → v1" in promoted


@pytest.mark.anyio
async def test_mem_do_routes_enable(layout):
    from memtomem.server.tools.meta import mem_do

    _seed_flat(layout, "agents", "flatone")
    out = await mem_do(
        "context_version",
        {"artifact_type": "agents", "name": "flatone", "action": "enable"},
    )
    assert out.startswith("Adopted")


# ── label-aware deploy (mem_context_sync / mem_context_generate --label) ────


def _agent_md(name: str, marker: str) -> str:
    """A valid sub-agent canonical whose body carries a distinguishable marker."""
    return f"---\nname: {name}\ndescription: example\n---\n{marker}\n"


def _runtime_agent_text(layout: dict[str, Path], name: str) -> str:
    """Read the user-tier Claude runtime fan-out for agent *name*."""
    return (layout["user_home"] / ".claude" / "agents" / f"{name}.md").read_text(encoding="utf-8")


@pytest.mark.anyio
async def test_sync_label_deploys_frozen_version_not_working_file(layout):
    from memtomem.server.tools.context import mem_context_sync

    adir = _seed_dir(
        layout, "agents", "deployer", scope="user", body=_agent_md("deployer", "FROZEN_V1")
    )
    assert "version v1" in await mem_context_version(
        "agents", "deployer", action="create", scope="user"
    )

    # Edit the working canonical AFTER freezing — a labeled sync must ignore it.
    (adir / "agent.md").write_text(_agent_md("deployer", "WORKING_V2"), encoding="utf-8")
    await mem_context_promote("agents", "deployer", label="production", version="v1", scope="user")

    out = await mem_context_sync(include="agents", scope="user", label="production")
    assert "Sub-agent fan-out:" in out
    text = _runtime_agent_text(layout, "deployer")
    assert "FROZEN_V1" in text and "WORKING_V2" not in text


@pytest.mark.anyio
async def test_sync_label_latest_deploys_working_file(layout):
    from memtomem.server.tools.context import mem_context_sync

    adir = _seed_dir(layout, "agents", "d2", scope="user", body=_agent_md("d2", "FROZEN_V1"))
    await mem_context_version("agents", "d2", action="create", scope="user")
    (adir / "agent.md").write_text(_agent_md("d2", "WORKING_V2"), encoding="utf-8")

    # "latest" is the reserved working-file label (invariant 2) — unchanged behavior.
    await mem_context_sync(include="agents", scope="user", label="latest")
    text = _runtime_agent_text(layout, "d2")
    assert "WORKING_V2" in text and "FROZEN_V1" not in text


@pytest.mark.anyio
async def test_sync_unknown_label_isolates_as_skip(layout):
    from memtomem.server.tools.context import mem_context_sync

    _seed_dir(layout, "agents", "d3", scope="user", body=_agent_md("d3", "BODY"))
    await mem_context_version("agents", "d3", action="create", scope="user")
    # No label promoted → resolving "ghost" fails per-artifact, isolated as a
    # skip (not a whole-run error), so nothing is deployed.
    out = await mem_context_sync(include="agents", scope="user", label="ghost")
    assert "skipped" in out
    assert not (layout["user_home"] / ".claude" / "agents" / "d3.md").exists()


@pytest.mark.anyio
async def test_sync_label_notes_ineligible_kinds(layout):
    from memtomem.server.tools.context import mem_context_sync

    _seed_dir(layout, "agents", "d4", scope="user", body=_agent_md("d4", "BODY"))
    await mem_context_version("agents", "d4", action="create", scope="user")
    await mem_context_promote("agents", "d4", label="production", version="v1", scope="user")
    # skills is ineligible for labels → a note, but agents still deploy labeled.
    out = await mem_context_sync(include="agents,skills", scope="user", label="production")
    assert "note: label does not apply to skills" in out
    assert "FROZEN" not in out  # sanity: note text is about skills, not a leak


@pytest.mark.anyio
async def test_generate_label_deploys_frozen_version(layout):
    from memtomem.server.tools.context import mem_context_generate

    adir = _seed_dir(layout, "agents", "g1", scope="user", body=_agent_md("g1", "FROZEN_V1"))
    await mem_context_version("agents", "g1", action="create", scope="user")
    (adir / "agent.md").write_text(_agent_md("g1", "WORKING_V2"), encoding="utf-8")
    await mem_context_promote("agents", "g1", label="production", version="v1", scope="user")

    out = await mem_context_generate(include="agents", scope="user", label="v1")
    assert "Sub-agent fan-out:" in out
    text = _runtime_agent_text(layout, "g1")
    assert "FROZEN_V1" in text and "WORKING_V2" not in text  # bare version tag resolves directly


@pytest.mark.anyio
async def test_sync_label_note_survives_nothing_to_sync_exit(layout):
    from memtomem.server.tools.context import mem_context_sync

    # No context.md + no eligible kind, but a label → the "had no effect" note
    # must survive the nothing-to-sync early return (CLI parity; Codex review).
    out = await mem_context_sync(include="", scope="user", label="production")
    assert "had no effect" in out  # the label note is carried, not dropped
    assert "not found" in out  # the terminal nothing-to-do message still present


@pytest.mark.anyio
async def test_generate_label_note_survives_nothing_to_do_exit(layout):
    from memtomem.server.tools.context import mem_context_generate

    out = await mem_context_generate(include="", scope="user", label="production")
    assert "had no effect" in out and "not found" in out


# ── Skills: read-only version surface (ADR-0030 §10, PR-G3) ──────────


def _seed_skill(layout, name="demo", *, scope="project_shared"):
    from memtomem.context.skills import SKILL_MANIFEST

    skill_dir = _canonical_root(layout, "skills", scope) / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / SKILL_MANIFEST).write_text(
        f"---\nname: {name}\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return skill_dir


class TestSkillsReadOnly:
    """Skills READ their tree-snapshot store; every mutating verb refuses with a
    message distinct from the unsupported-type one."""

    @pytest.mark.anyio
    async def test_list_empty_store(self, layout):
        _seed_skill(layout)
        out = await mem_context_version(artifact_type="skills", name="demo", action="list")
        assert "no versions yet" in out
        assert not out.startswith("error:")

    @pytest.mark.anyio
    async def test_list_renders_tree_versions(self, layout):
        from memtomem.context import versioning

        skill_dir = _seed_skill(layout)
        versioning.create_tree_version(skill_dir, [("SKILL.md", b"x")], note="from pull")

        out = await mem_context_version(artifact_type="skills", name="demo", action="list")
        assert "v1" in out and "(tree)" in out and "from pull" in out

    @pytest.mark.anyio
    @pytest.mark.parametrize("action", ["create"])
    async def test_write_actions_refuse_without_mutating(self, layout, action):
        skill_dir = _seed_skill(layout)
        out = await mem_context_version(artifact_type="skills", name="demo", action=action)
        assert out.startswith("error:")
        assert "read-only" in out
        # No remediation pointing at a path that is itself refused (PR-G4).
        assert "overwrite=True" not in out
        assert not (skill_dir / "versions").exists()
        assert not (skill_dir / "versions.json").exists()

    @pytest.mark.anyio
    async def test_enable_is_a_noop_not_a_refusal(self, layout):
        """Skills are always dir layout, so the end state already holds."""
        skill_dir = _seed_skill(layout)
        before = sorted(p.name for p in skill_dir.iterdir())
        out = await mem_context_version(artifact_type="skills", name="demo", action="enable")
        assert not out.startswith("error:")
        assert "nothing to do" in out
        assert sorted(p.name for p in skill_dir.iterdir()) == before

    @pytest.mark.anyio
    async def test_promote_refuses_without_mutating(self, layout):
        from memtomem.context import versioning

        skill_dir = _seed_skill(layout)
        versioning.create_tree_version(skill_dir, [("SKILL.md", b"x")])
        before = (skill_dir / "versions.json").read_bytes()

        out = await mem_context_promote(
            artifact_type="skills", name="demo", label="production", version="v1"
        )
        assert out.startswith("error:") and "read-only" in out
        assert (skill_dir / "versions.json").read_bytes() == before

    @pytest.mark.anyio
    async def test_delete_label_refuses_without_mutating(self, layout):
        from memtomem.context import versioning

        skill_dir = _seed_skill(layout)
        versioning.create_tree_version(skill_dir, [("SKILL.md", b"x")])
        before = (skill_dir / "versions.json").read_bytes()

        out = await mem_context_promote(
            artifact_type="skills", name="demo", label="production", delete=True
        )
        assert out.startswith("error:") and "read-only" in out
        assert (skill_dir / "versions.json").read_bytes() == before

    @pytest.mark.anyio
    async def test_read_only_message_is_distinct_from_unsupported_type(self, layout):
        _seed_skill(layout)
        read_only = await mem_context_version(artifact_type="skills", name="demo", action="create")
        unsupported = await mem_context_version(artifact_type="dogs", name="demo", action="create")
        assert "read-only" in read_only and "read-only" not in unsupported
        assert "not supported" in unsupported and "not supported" not in read_only

    @pytest.mark.anyio
    async def test_missing_skill_resolves_before_the_write_gate(self, layout):
        """A typo'd name must report "not found", not "read-only".

        Deliberately the opposite order from the web router, where the
        read-only gate is type-level and sits alongside the tier gate ahead of
        resolution. MCP resolves first because its resolver is also its name
        validator, so it can afford the more precise message. Both orders are
        pinned; the divergence is intentional, not drift.
        """
        out = await mem_context_version(artifact_type="skills", name="ghost", action="create")
        assert "not found" in out and "read-only" not in out

    @pytest.mark.anyio
    async def test_refusal_precedes_the_confirm_prompt(self, layout):
        """Never ask a user to confirm a write that cannot land."""
        _seed_skill(layout)
        out = await mem_context_version(
            artifact_type="skills", name="demo", action="create", scope="project_shared"
        )
        assert out.startswith("error:")
        assert "needs confirmation" not in out

    @pytest.mark.anyio
    async def test_refusals_carry_no_filesystem_paths(self, layout):
        """Privacy boundary: MCP messages never echo absolute canonical paths."""
        _seed_skill(layout)
        outs = [
            await mem_context_version(artifact_type="skills", name="demo", action="create"),
            await mem_context_promote(
                artifact_type="skills", name="demo", label="production", version="v1"
            ),
        ]
        for out in outs:
            assert str(layout["project_root"]) not in out
            assert str(layout["user_home"]) not in out
