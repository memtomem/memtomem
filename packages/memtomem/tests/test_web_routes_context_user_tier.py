"""User-tier write/sync routes behind the host-write confirm gate (#1263).

Pins the A-6 contract on the skills / commands / agents web routes:

- ``target_scope=user`` + no ``allow_host_writes`` → HTTP 200
  ``needs_confirmation`` envelope (full-equality literals, including the
  exact reason prose and the disclosed ``host_targets``), with **no**
  disk mutation and **no** engine apply call.
- The confirmed re-request applies, passing ``scope="user"`` through to
  the engine (monkeypatched-engine tests pin the pass-through kwargs —
  the campaign's false-pass lesson).
- The gate never fires for requests that would not write: idempotent
  deletes, nothing-to-import imports, empty canonical sets, and requests
  refused by cheaper checks (duplicate create 409, missing update 404,
  stale-mtime 409).
- Cascade deletes resolve runtime copies AT the requested tier: a
  user-tier cascade removes ``~/.claude/...`` copies and leaves the
  project's runtime copies untouched (the design-gate Blocker).
- ``project_local`` writes stay 400; the explicit ``?dry_run=true``
  import preview stays flag-free.

Every test isolates HOME via ``set_home`` — user-tier paths
(``~/.memtomem``, ``~/.claude``, …) must never touch the real home.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.context.skills import SKILL_MANIFEST, SkillSyncResult
from memtomem.web.app import create_app

from .helpers import set_home

_HOST_REASON = (
    "{action} targets the user tier — host paths outside any project "
    "root. Re-send the request with allow_host_writes=true after "
    "confirming with the user."
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def proj(tmp_path: Path) -> Path:
    """Project root as a SIBLING of the fake home — ``~/.memtomem`` must
    not live under the project root, or ``_safe_rel`` legitimately
    relativizes user-tier paths and the absolute-path pins go soft."""
    proj = tmp_path / "proj"
    proj.mkdir()
    return proj


@pytest.fixture
def ctx_app(proj: Path):
    """App with project_root pointing to a temp directory."""
    from memtomem.config import Mem2MemConfig

    application = create_app(lifespan=None, mode="dev")
    application.state.project_root = proj
    application.state.storage = AsyncMock()
    application.state.config = Mem2MemConfig()
    application.state.search_pipeline = None
    application.state.index_engine = None
    application.state.embedder = None
    application.state.dedup_scanner = None
    return application


@pytest.fixture
async def client(ctx_app):
    transport = ASGITransport(app=ctx_app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolated fake home; resolved so expectations match engine paths.

    The engine resolves user-tier paths via ``expanduser().resolve()`` —
    resolve here too so macOS ``/var`` → ``/private/var`` symlinks can't
    desync the full-equality envelope pins.
    """
    home = tmp_path / "home"
    home.mkdir()
    set_home(monkeypatch, home)
    return home.resolve()


def _make_user_skill(home: Path, name: str, content: str = "# User skill\n") -> Path:
    skill_dir = home / ".memtomem" / "skills" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / SKILL_MANIFEST).write_text(content, encoding="utf-8")
    return skill_dir


def _make_user_command(home: Path, name: str, content: str = "# User command\n") -> Path:
    cmd_dir = home / ".memtomem" / "commands"
    cmd_dir.mkdir(parents=True, exist_ok=True)
    cmd_file = cmd_dir / f"{name}.md"
    cmd_file.write_text(content, encoding="utf-8")
    return cmd_file


_AGENT_CONTENT = """---
name: uagent
description: User-tier agent
---
You are a user-tier agent.
"""


def _make_user_agent(home: Path, name: str, content: str = _AGENT_CONTENT) -> Path:
    agent_dir = home / ".memtomem" / "agents"
    agent_dir.mkdir(parents=True, exist_ok=True)
    agent_file = agent_dir / f"{name}.md"
    agent_file.write_text(content, encoding="utf-8")
    return agent_file


# ---------------------------------------------------------------------------
# Sync — envelope literal, engine pass-through, empty-set bypass
# ---------------------------------------------------------------------------


class TestUserTierSync:
    @pytest.mark.anyio
    async def test_no_flag_returns_envelope_literal_and_never_calls_engine(
        self, client: AsyncClient, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_user_skill(home, "uskill")

        def _boom(*a, **k):  # engine must not run on the disclose leg
            raise AssertionError("generate_all_skills must not be called before confirmation")

        monkeypatch.setattr("memtomem.web.routes.context_skills.generate_all_skills", _boom)
        r = await client.post("/api/context/skills/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        assert r.json() == {
            "status": "needs_confirmation",
            "confirm": "allow_host_writes",
            "reason": _HOST_REASON.format(action="Sync skills"),
            "host_targets": [
                str(home / ".agents" / "skills" / "uskill"),
                str(home / ".claude" / "skills" / "uskill"),
                str(home / ".gemini" / "skills" / "uskill"),
                str(home / ".kimi" / "skills" / "uskill"),
            ],
        }
        assert not (home / ".claude" / "skills").exists()

    @pytest.mark.anyio
    async def test_confirmed_passes_scope_kwarg_to_engine(
        self, client: AsyncClient, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        _make_user_skill(home, "uskill")
        calls: list[tuple[tuple, dict]] = []

        def _spy(*args, **kwargs):
            calls.append((args, kwargs))
            return SkillSyncResult(generated=[], skipped=[])

        monkeypatch.setattr("memtomem.web.routes.context_skills.generate_all_skills", _spy)
        r = await client.post(
            "/api/context/skills/sync",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        assert r.status_code == 200
        # Pin the pass-through kwargs in full — a monkeypatched engine that
        # ignores them would false-pass a route that dropped scope=.
        assert calls == [
            (
                (Path(client._transport.app.state.project_root),),
                {
                    "scope": "user",
                    "surface": "web_context_skills_sync",
                    "force_unsafe": False,
                },
            )  # type: ignore[attr-defined]
        ]

    @pytest.mark.anyio
    async def test_no_user_canonicals_skips_gate(self, client: AsyncClient, home: Path):
        """Empty user canonical set → nothing to disclose → engine no-op runs."""
        r = await client.post("/api/context/skills/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert "status" not in body  # not an envelope
        assert body["generated"] == []
        assert body["skipped"][0]["reason_code"] == "no_canonical_root"

    @pytest.mark.anyio
    async def test_confirmed_sync_writes_user_runtime_roots(
        self, client: AsyncClient, home: Path, proj: Path
    ):
        _make_user_skill(home, "uskill", content="# Fan me out\n")
        r = await client.post(
            "/api/context/skills/sync",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        assert r.status_code == 200
        assert (home / ".claude" / "skills" / "uskill" / SKILL_MANIFEST).is_file()
        assert (home / ".gemini" / "skills" / "uskill" / SKILL_MANIFEST).is_file()
        # The project's runtime tree is untouched by a user-tier sync.
        assert not (proj / ".claude" / "skills").exists()

    @pytest.mark.anyio
    async def test_agents_and_commands_sync_pass_scope_kwarg(
        self, client: AsyncClient, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem.context.agents import AgentSyncResult
        from memtomem.context.commands import CommandSyncResult

        _make_user_agent(home, "uagent")
        _make_user_command(home, "ucmd")
        agent_calls: list[dict] = []
        cmd_calls: list[dict] = []

        def _agent_spy(*args, **kwargs):
            agent_calls.append(kwargs)
            return AgentSyncResult(generated=[], dropped=[], skipped=[])

        def _cmd_spy(*args, **kwargs):
            cmd_calls.append(kwargs)
            return CommandSyncResult(generated=[], dropped=[], skipped=[])

        monkeypatch.setattr("memtomem.web.routes.context_agents.generate_all_agents", _agent_spy)
        monkeypatch.setattr("memtomem.web.routes.context_commands.generate_all_commands", _cmd_spy)
        ra = await client.post(
            "/api/context/agents/sync",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        rc = await client.post(
            "/api/context/commands/sync",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        assert ra.status_code == 200 and rc.status_code == 200
        assert agent_calls == [
            {
                "on_drop": "warn",
                "scope": "user",
                "surface": "web_context_agents_sync",
                "force_unsafe": False,
            }
        ]
        assert cmd_calls == [
            {
                "on_drop": "warn",
                "scope": "user",
                "surface": "web_context_commands_sync",
                "force_unsafe": False,
            }
        ]

    @pytest.mark.anyio
    async def test_agents_sync_no_flag_envelope_targets(self, client: AsyncClient, home: Path):
        """Agent fan-out targets are per-runtime files at the user tier."""
        _make_user_agent(home, "uagent")
        r = await client.post("/api/context/agents/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert body["confirm"] == "allow_host_writes"
        assert str(home / ".claude" / "agents" / "uagent.md") in body["host_targets"]
        assert all(str(home) in t for t in body["host_targets"])

    @pytest.mark.anyio
    async def test_sync_disclosure_uses_parsed_name_not_filename(
        self, client: AsyncClient, home: Path
    ):
        """Disclosure-vs-mutation parity (review Blocker): the engine fans
        out under the PARSED frontmatter ``name``, so the envelope must
        disclose that name's paths — a filename-keyed disclosure would
        confirm ``benign.md`` while the engine writes ``surprise.md``."""
        _make_user_agent(
            home,
            "benign",
            content="---\nname: surprise\ndescription: Renamed\n---\nBody.\n",
        )
        r = await client.post("/api/context/agents/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert str(home / ".claude" / "agents" / "surprise.md") in body["host_targets"]
        assert not any("benign" in t for t in body["host_targets"])

    @pytest.mark.anyio
    async def test_command_sync_disclosure_uses_parsed_name_not_filename(
        self, client: AsyncClient, home: Path
    ):
        """Commands share the sync_atomic_artifact parsed-name fan-out, so
        the same Blocker pin applies to their disclosure helper."""
        _make_user_command(
            home,
            "benign",
            content="---\nname: surprise\ndescription: Renamed\n---\nBody.\n",
        )
        r = await client.post("/api/context/commands/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert str(home / ".claude" / "commands" / "surprise.md") in body["host_targets"]
        assert not any("benign" in t for t in body["host_targets"])

    @pytest.mark.anyio
    async def test_sync_disclosure_excludes_unparseable_canonicals(
        self, client: AsyncClient, home: Path
    ):
        """A canonical the engine would parse-skip is not disclosed; with
        nothing else pending the gate stays open (engine no-ops)."""
        _make_user_agent(home, "broken", content="---\nname: [unclosed\n---\nBody.\n")
        r = await client.post("/api/context/agents/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert "status" not in body  # no envelope — nothing would be written
        assert body["generated"] == []

    @pytest.mark.anyio
    async def test_command_sync_disclosure_excludes_unparseable_canonicals(
        self, client: AsyncClient, home: Path
    ):
        _make_user_command(home, "broken", content="---\nname: [unclosed\n---\nBody.\n")
        r = await client.post("/api/context/commands/sync", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert "status" not in body
        assert body["generated"] == []


# ---------------------------------------------------------------------------
# Create — pre-checks before gate, absolute canonical_path, real write
# ---------------------------------------------------------------------------


class TestUserTierCreate:
    @pytest.mark.anyio
    async def test_no_flag_returns_envelope_literal(self, client: AsyncClient, home: Path):
        r = await client.post(
            "/api/context/skills",
            params={"target_scope": "user"},
            json={"name": "newskill", "content": "# x\n"},
        )
        assert r.status_code == 200
        assert r.json() == {
            "status": "needs_confirmation",
            "confirm": "allow_host_writes",
            "reason": _HOST_REASON.format(action="Create skill"),
            "host_targets": [str(home / ".memtomem" / "skills" / "newskill" / SKILL_MANIFEST)],
        }
        assert not (home / ".memtomem" / "skills" / "newskill").exists()

    @pytest.mark.anyio
    async def test_confirmed_writes_user_canonical_and_returns_absolute_path(
        self, client: AsyncClient, home: Path
    ):
        r = await client.post(
            "/api/context/skills",
            params={"target_scope": "user"},
            json={"name": "newskill", "content": "# x\n", "allow_host_writes": True},
        )
        assert r.status_code == 200
        manifest = home / ".memtomem" / "skills" / "newskill" / SKILL_MANIFEST
        assert manifest.is_file()
        # Regression: bare relative_to(project_root) 500'd on user-tier paths.
        # The POSIX fallback (``p.as_posix()``) keeps ``canonical_path``
        # ``/``-joined on every platform — parity with the agents user-tier
        # assertion below and the #1325 separator contract.
        assert r.json()["canonical_path"] == manifest.parent.as_posix()

    @pytest.mark.anyio
    async def test_duplicate_is_409_not_confirmation_prompt(self, client: AsyncClient, home: Path):
        _make_user_skill(home, "dup")
        r = await client.post(
            "/api/context/skills",
            params={"target_scope": "user"},
            json={"name": "dup", "content": "# x\n"},
        )
        assert r.status_code == 409

    @pytest.mark.anyio
    async def test_agent_create_confirmed_uses_versioned_layout(
        self, client: AsyncClient, home: Path
    ):
        r = await client.post(
            "/api/context/agents",
            params={"target_scope": "user"},
            json={"name": "uagent", "content": _AGENT_CONTENT, "allow_host_writes": True},
        )
        assert r.status_code == 200
        agent_md = home / ".memtomem" / "agents" / "uagent" / "agent.md"
        assert agent_md.is_file()
        assert r.json()["canonical_path"] == agent_md.as_posix()

    @pytest.mark.anyio
    async def test_agent_duplicate_flat_user_canonical_is_409(
        self, client: AsyncClient, home: Path
    ):
        """The unlocked duplicate pre-check resolves flat layout too."""
        _make_user_agent(home, "uagent")
        r = await client.post(
            "/api/context/agents",
            params={"target_scope": "user"},
            json={"name": "uagent", "content": _AGENT_CONTENT},
        )
        assert r.status_code == 409


# ---------------------------------------------------------------------------
# Update — refusals win over the gate
# ---------------------------------------------------------------------------


class TestUserTierUpdate:
    @pytest.mark.anyio
    async def test_missing_is_404_not_confirmation_prompt(self, client: AsyncClient, home: Path):
        r = await client.put(
            "/api/context/commands/ghost",
            params={"target_scope": "user"},
            json={"content": "# x\n", "mtime_ns": "0"},
        )
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_stale_mtime_is_409_not_confirmation_prompt(
        self, client: AsyncClient, home: Path
    ):
        _make_user_command(home, "ucmd")
        r = await client.put(
            "/api/context/commands/ucmd",
            params={"target_scope": "user"},
            json={"content": "# y\n", "mtime_ns": "1"},
        )
        assert r.status_code == 409
        assert r.json()["status"] == "aborted"

    @pytest.mark.anyio
    async def test_fresh_mtime_no_flag_returns_envelope(self, client: AsyncClient, home: Path):
        cmd_file = _make_user_command(home, "ucmd")
        mtime_ns = str(cmd_file.stat().st_mtime_ns)
        r = await client.put(
            "/api/context/commands/ucmd",
            params={"target_scope": "user"},
            json={"content": "# y\n", "mtime_ns": mtime_ns},
        )
        assert r.status_code == 200
        assert r.json() == {
            "status": "needs_confirmation",
            "confirm": "allow_host_writes",
            "reason": _HOST_REASON.format(action="Update command"),
            "host_targets": [str(cmd_file)],
        }
        assert cmd_file.read_text(encoding="utf-8") == "# User command\n"

    @pytest.mark.anyio
    async def test_confirmed_update_writes(self, client: AsyncClient, home: Path):
        cmd_file = _make_user_command(home, "ucmd")
        mtime_ns = str(cmd_file.stat().st_mtime_ns)
        r = await client.put(
            "/api/context/commands/ucmd",
            params={"target_scope": "user"},
            json={"content": "# y\n", "mtime_ns": mtime_ns, "allow_host_writes": True},
        )
        assert r.status_code == 200
        assert cmd_file.read_text(encoding="utf-8") == "# y\n"


# ---------------------------------------------------------------------------
# Delete — idempotent no-ops never prompt; cascade is tier-scoped
# ---------------------------------------------------------------------------


class TestUserTierDelete:
    @pytest.mark.anyio
    async def test_missing_is_idempotent_no_prompt(self, client: AsyncClient, home: Path):
        r = await client.delete("/api/context/skills/ghost", params={"target_scope": "user"})
        assert r.status_code == 200
        assert r.json() == {"deleted": [], "skipped": []}

    @pytest.mark.anyio
    async def test_no_flag_returns_envelope_with_pending_targets(
        self, client: AsyncClient, home: Path
    ):
        skill_dir = _make_user_skill(home, "uskill")
        r = await client.delete("/api/context/skills/uskill", params={"target_scope": "user"})
        assert r.status_code == 200
        assert r.json() == {
            "status": "needs_confirmation",
            "confirm": "allow_host_writes",
            "reason": _HOST_REASON.format(action="Delete skill"),
            "host_targets": [str(skill_dir)],
        }
        assert skill_dir.exists()

    @pytest.mark.anyio
    async def test_cascade_envelope_lists_user_runtime_copies_only(
        self, client: AsyncClient, home: Path, proj: Path
    ):
        skill_dir = _make_user_skill(home, "uskill")
        user_copy = home / ".claude" / "skills" / "uskill"
        user_copy.mkdir(parents=True)
        (user_copy / SKILL_MANIFEST).write_text("# copy\n", encoding="utf-8")
        project_copy = proj / ".claude" / "skills" / "uskill"
        project_copy.mkdir(parents=True)
        (project_copy / SKILL_MANIFEST).write_text("# project copy\n", encoding="utf-8")

        r = await client.delete(
            "/api/context/skills/uskill",
            params={"target_scope": "user", "cascade": "true"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert body["host_targets"] == [str(skill_dir), str(user_copy)]
        assert str(project_copy) not in body["host_targets"]

    @pytest.mark.anyio
    async def test_confirmed_cascade_spares_project_runtime_copies(
        self, client: AsyncClient, home: Path, proj: Path
    ):
        """The design-gate Blocker: user-tier cascade must not touch the
        project's runtime fan-out (and the disclosure above must match
        what is actually removed)."""
        _make_user_skill(home, "uskill")
        user_copy = home / ".claude" / "skills" / "uskill"
        user_copy.mkdir(parents=True)
        (user_copy / SKILL_MANIFEST).write_text("# copy\n", encoding="utf-8")
        project_copy = proj / ".claude" / "skills" / "uskill"
        project_copy.mkdir(parents=True)
        (project_copy / SKILL_MANIFEST).write_text("# project copy\n", encoding="utf-8")

        r = await client.delete(
            "/api/context/skills/uskill",
            params={
                "target_scope": "user",
                "cascade": "true",
                "allow_host_writes": "true",
            },
        )
        assert r.status_code == 200
        assert not (home / ".memtomem" / "skills" / "uskill").exists()
        assert not user_copy.exists()
        assert project_copy.exists()  # project runtime copy survives

    @pytest.mark.anyio
    async def test_agent_confirmed_cascade_spares_project_runtime_copies(
        self, client: AsyncClient, home: Path, proj: Path
    ):
        """File-shaped sibling of the skills cascade pin (agents/commands
        share the target_file cascade loop)."""
        _make_user_agent(home, "uagent")
        user_copy = home / ".claude" / "agents" / "uagent.md"
        user_copy.parent.mkdir(parents=True)
        user_copy.write_text(_AGENT_CONTENT, encoding="utf-8")
        project_copy = proj / ".claude" / "agents" / "uagent.md"
        project_copy.parent.mkdir(parents=True)
        project_copy.write_text(_AGENT_CONTENT, encoding="utf-8")

        r = await client.delete(
            "/api/context/agents/uagent",
            params={
                "target_scope": "user",
                "cascade": "true",
                "allow_host_writes": "true",
            },
        )
        assert r.status_code == 200
        assert not (home / ".memtomem" / "agents" / "uagent.md").exists()
        assert not user_copy.exists()
        assert project_copy.exists()

    @pytest.mark.anyio
    async def test_project_shared_cascade_unaffected(
        self, client: AsyncClient, home: Path, proj: Path
    ):
        """Behavior pin: shared-tier cascade still removes project copies
        and never prompts (scope= addition must be a no-op there)."""
        skill_dir = proj / ".memtomem" / "skills" / "pskill"
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MANIFEST).write_text("# p\n", encoding="utf-8")
        project_copy = proj / ".claude" / "skills" / "pskill"
        project_copy.mkdir(parents=True)
        (project_copy / SKILL_MANIFEST).write_text("# p\n", encoding="utf-8")

        r = await client.delete("/api/context/skills/pskill", params={"cascade": "true"})
        assert r.status_code == 200
        assert not skill_dir.exists()
        assert not project_copy.exists()


# ---------------------------------------------------------------------------
# Import — dry-run-backed disclosure, 404 contract, dry_run stays flag-free
# ---------------------------------------------------------------------------


class TestUserTierImport:
    def _seed_user_runtime_skill(self, home: Path, name: str) -> Path:
        runtime_dir = home / ".claude" / "skills" / name
        runtime_dir.mkdir(parents=True)
        (runtime_dir / SKILL_MANIFEST).write_text("# runtime skill\n", encoding="utf-8")
        return runtime_dir

    @pytest.mark.anyio
    async def test_no_flag_returns_envelope_with_plan(self, client: AsyncClient, home: Path):
        self._seed_user_runtime_skill(home, "imp1")
        r = await client.post("/api/context/skills/import", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert body["status"] == "needs_confirmation"
        assert body["confirm"] == "allow_host_writes"
        assert body["reason"] == _HOST_REASON.format(action="Import skills")
        assert body["host_targets"] == [str(home / ".memtomem" / "skills" / "imp1")]
        assert body["plan"]["dry_run"] is True
        assert [i["name"] for i in body["plan"]["imported"]] == ["imp1"]
        # The user-tier scan hint names the host runtime roots, not the
        # project-relative detector dirs.
        assert str(home / ".claude" / "skills") in body["plan"]["scanned_dirs"]
        assert not (home / ".memtomem" / "skills").exists()

    @pytest.mark.anyio
    async def test_confirmed_import_writes_user_canonical(self, client: AsyncClient, home: Path):
        self._seed_user_runtime_skill(home, "imp1")
        r = await client.post(
            "/api/context/skills/import",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        assert r.status_code == 200
        body = r.json()
        canonical = home / ".memtomem" / "skills" / "imp1"
        assert (canonical / SKILL_MANIFEST).is_file()
        # Regression: bare relative_to(project_root) 500'd on user-tier paths;
        # the POSIX fallback keeps ``canonical_path`` ``/``-joined (#1325).
        assert body["imported"] == [{"name": "imp1", "canonical_path": canonical.as_posix()}]
        assert body["dry_run"] is False

    @pytest.mark.anyio
    async def test_nothing_to_import_skips_gate(self, client: AsyncClient, home: Path):
        r = await client.post("/api/context/skills/import", params={"target_scope": "user"})
        assert r.status_code == 200
        body = r.json()
        assert "status" not in body
        assert body["imported"] == []

    @pytest.mark.anyio
    async def test_explicit_dry_run_needs_no_flag(self, client: AsyncClient, home: Path):
        self._seed_user_runtime_skill(home, "imp1")
        r = await client.post(
            "/api/context/skills/import",
            params={"target_scope": "user", "dry_run": "true"},
        )
        assert r.status_code == 200
        body = r.json()
        assert "status" not in body
        assert body["dry_run"] is True
        assert not (home / ".memtomem" / "skills").exists()

    @pytest.mark.anyio
    async def test_single_import_unknown_name_404_not_prompt(self, client: AsyncClient, home: Path):
        r = await client.post("/api/context/skills/ghost/import", params={"target_scope": "user"})
        assert r.status_code == 404

    @pytest.mark.anyio
    async def test_single_import_envelope_then_confirmed(self, client: AsyncClient, home: Path):
        self._seed_user_runtime_skill(home, "imp1")
        first = await client.post(
            "/api/context/skills/imp1/import", params={"target_scope": "user"}
        )
        assert first.status_code == 200
        body = first.json()
        assert body["status"] == "needs_confirmation"
        assert body["host_targets"] == [str(home / ".memtomem" / "skills" / "imp1")]
        assert "dry_run" not in body["plan"]  # single-import shape has no key

        second = await client.post(
            "/api/context/skills/imp1/import",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        assert second.status_code == 200
        assert (home / ".memtomem" / "skills" / "imp1" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_import_project_local_still_400(self, client: AsyncClient, home: Path):
        r = await client.post(
            "/api/context/skills/import", params={"target_scope": "project_local"}
        )
        assert r.status_code == 400
        assert "project_shared" in r.json()["detail"]["message"]
        assert r.json()["detail"]["error_kind"] == "validation"
        assert r.json()["detail"]["reason_code"] == "project_local_unsupported"

    @pytest.mark.anyio
    async def test_import_passes_scope_kwargs_to_engine(
        self, client: AsyncClient, home: Path, monkeypatch: pytest.MonkeyPatch
    ):
        from memtomem.context.skills import ExtractResult

        calls: list[dict] = []

        def _spy(*args, **kwargs):
            calls.append(kwargs)
            return ExtractResult(imported=[], skipped=[])

        monkeypatch.setattr("memtomem.web.routes.context_skills.extract_skills_to_canonical", _spy)
        r = await client.post(
            "/api/context/skills/import",
            params={"target_scope": "user"},
            json={"allow_host_writes": True},
        )
        assert r.status_code == 200
        assert calls == [
            {
                "overwrite": False,
                "dry_run": False,
                "scope": "user",
                "force_unsafe_import": False,
                "surface": "web_context_skills_import",
            }
        ]


class TestImportSkillToUserLibrary:
    """``POST /context/skills/{name}/import-to-user`` — read the PROJECT
    runtime, write the USER canonical. The one web path for a project-runtime
    skill that trips Gate A's false-positive secret heuristic (project_shared
    dest is hard-blocked; user dest is force-bypassable)."""

    def _seed_project_skill(self, proj: Path, name: str, content: str = "# proj skill\n") -> Path:
        d = proj / ".claude" / "skills" / name
        d.mkdir(parents=True)
        (d / SKILL_MANIFEST).write_text(content, encoding="utf-8")
        return d

    @pytest.mark.anyio
    async def test_clean_skill_discloses_host_write_then_imports(
        self, client: AsyncClient, proj: Path, home: Path
    ):
        self._seed_project_skill(proj, "proj_skill")
        # First call: user dest → host-write disclosure envelope naming the
        # ~/.memtomem destination.
        r = await client.post("/api/context/skills/proj_skill/import-to-user")
        assert r.status_code == 200, r.text
        env = r.json()
        assert env["status"] == "needs_confirmation"
        assert env["confirm"] == "allow_host_writes"
        assert env["host_targets"] == [str(home / ".memtomem" / "skills" / "proj_skill")]
        assert not (home / ".memtomem" / "skills").exists()  # nothing written yet
        # Confirmed: reads the PROJECT runtime, writes the USER canonical.
        r2 = await client.post(
            "/api/context/skills/proj_skill/import-to-user",
            json={"allow_host_writes": True},
        )
        assert r2.status_code == 200, r2.text
        assert [i["name"] for i in r2.json()["imported"]] == ["proj_skill"]
        assert (home / ".memtomem" / "skills" / "proj_skill" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_flagged_skill_skips_without_force_imports_with_force(
        self, client: AsyncClient, proj: Path, home: Path
    ):
        self._seed_project_skill(
            proj, "flagged", "---\nname: flagged\n---\nclass S:\n    api_key: str\n"
        )
        # No force (host-write already confirmed): user dest blocks → surfaced
        # as a privacy_blocked skip, NOT a 500 and NOT a host envelope (nothing
        # would import without force).
        r = await client.post(
            "/api/context/skills/flagged/import-to-user",
            json={"allow_host_writes": True},
        )
        assert r.status_code == 200, r.text
        data = r.json()
        assert data["imported"] == []
        assert any(s["reason_code"] == "privacy_blocked" for s in data["skipped"])
        assert not (home / ".memtomem" / "skills" / "flagged").exists()
        # Reviewed force: the project skill lands in the user library.
        r2 = await client.post(
            "/api/context/skills/flagged/import-to-user",
            json={"allow_host_writes": True, "force_unsafe_import": True},
        )
        assert r2.status_code == 200, r2.text
        assert [i["name"] for i in r2.json()["imported"]] == ["flagged"]
        assert (home / ".memtomem" / "skills" / "flagged" / SKILL_MANIFEST).is_file()

    @pytest.mark.anyio
    async def test_reads_project_runtime_not_user_runtime(
        self, client: AsyncClient, proj: Path, home: Path
    ):
        # Same name in BOTH runtimes — the route must read the PROJECT copy.
        self._seed_project_skill(proj, "dup", "# from project\n")
        ud = home / ".claude" / "skills" / "dup"
        ud.mkdir(parents=True)
        (ud / SKILL_MANIFEST).write_text("# from user runtime\n", encoding="utf-8")
        r = await client.post(
            "/api/context/skills/dup/import-to-user",
            json={"allow_host_writes": True},
        )
        assert r.status_code == 200, r.text
        canonical = home / ".memtomem" / "skills" / "dup" / SKILL_MANIFEST
        assert canonical.read_text(encoding="utf-8") == "# from project\n"

    @pytest.mark.anyio
    async def test_missing_project_skill_404(self, client: AsyncClient, proj: Path, home: Path):
        r = await client.post(
            "/api/context/skills/ghost/import-to-user",
            json={"allow_host_writes": True},
        )
        assert r.status_code == 404, r.text
