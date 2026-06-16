"""The web sync body's ``force_unsafe_sync`` flag reaches the engine, and the
engine honours the ADR-0011 §5 scope asymmetry under force.

Gate A's secret-shape heuristic matches more than real secrets — a pydantic
``api_key: str`` type annotation or a ``secret_key=settings.x`` kwarg trips
``(api_key|secret_key|...)\\s*[:=]`` with no actual secret present. The import
routes already expose a reviewed bypass valve (``force_unsafe_import``, #1379);
the fan-out (sync) routes did not, so a reviewed false positive could be
imported into the User store but never synced back out to the user's runtimes.

These pin both halves of the sync-side valve:

* ``TestForceUnsafeThreadedToEngine`` — the three sync routes
  (skills/agents/commands) thread ``body.force_unsafe_sync`` verbatim to
  ``generate_all_*`` and default it to ``False`` when omitted. Spy on the
  engine and run under the default ``project_shared`` scope so the assertion
  is about wiring, not the host-write gate.
* ``TestSyncForceUnsafeEngineSemantics`` — the REAL engine, pinning the
  trust-boundary invariant the valve must never break: ``project_shared``
  stays hard-refused even with ``force_unsafe=True`` (raises
  ``PrivacyBlockedError`` — git history is forever), while ``user`` flips the
  same hit from a ``privacy_blocked`` skip to a real fan-out. Covers both
  privacy-scan helpers: skills walk a staging tree (``scan_artifact_tree``),
  agents/commands scan in-memory bytes (``scan_text_content``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from memtomem.context.privacy_scan import PrivacyBlockedError
from memtomem.context.skills import SKILL_MANIFEST, generate_all_skills

from .helpers import set_home

# A line that trips Gate A's secret-shape heuristic with no real secret — the
# canonical false positive the reviewed valve exists for.
_FALSE_POSITIVE = "secret_key = settings.langfuse_secret_key\n"


class _EngineSpy:
    """Records each call's kwargs and returns a canned result dataclass."""

    def __init__(self, result: object) -> None:
        self._result = result
        self.calls: list[dict] = []

    def __call__(self, *args: object, **kwargs: object) -> object:
        self.calls.append(kwargs)
        return self._result

    @property
    def last(self) -> dict:
        return self.calls[-1]


def _client(router: object) -> TestClient:
    """Minimal app + the ``get_project_root`` override the sibling sync
    422-translation test uses (``resolve_writable_scope_root`` resolves
    through it)."""
    from memtomem.web.deps import get_project_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_project_root] = lambda: Path("/tmp/p")
    return TestClient(app)


def _sync_cases() -> list[tuple[object, str, str, object]]:
    from memtomem.context.agents import AgentSyncResult
    from memtomem.context.commands import CommandSyncResult
    from memtomem.context.skills import SkillSyncResult
    from memtomem.web.routes import context_agents, context_commands, context_skills

    return [
        (
            context_skills,
            "generate_all_skills",
            "/context/skills/sync",
            SkillSyncResult(generated=[], skipped=[]),
        ),
        (
            context_agents,
            "generate_all_agents",
            "/context/agents/sync",
            AgentSyncResult(generated=[], dropped=[], skipped=[]),
        ),
        (
            context_commands,
            "generate_all_commands",
            "/context/commands/sync",
            CommandSyncResult(generated=[], dropped=[], skipped=[]),
        ),
    ]


class TestForceUnsafeThreadedToEngine:
    def test_sync_threads_true(self, monkeypatch: pytest.MonkeyPatch) -> None:
        for module, fn_name, url, result in _sync_cases():
            spy = _EngineSpy(result)
            monkeypatch.setattr(module, fn_name, spy)
            res = _client(module.router).post(url, json={"force_unsafe_sync": True})
            assert res.status_code == 200, (url, res.text)
            assert spy.last["force_unsafe"] is True, url

    def test_sync_defaults_false(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Both an explicit empty body and no body at all must default the flag
        # off (the route reads ``body.force_unsafe_sync if body else False``).
        for module, fn_name, url, result in _sync_cases():
            spy = _EngineSpy(result)
            monkeypatch.setattr(module, fn_name, spy)
            client = _client(module.router)
            assert client.post(url, json={}).status_code == 200, url
            assert spy.last["force_unsafe"] is False, url
            assert client.post(url).status_code == 200, url
            assert spy.last["force_unsafe"] is False, url


class TestSyncForceUnsafeEngineSemantics:
    """Real-engine trust-boundary pins (no spy)."""

    def _make_user_skill(self, home: Path, name: str, body: str) -> None:
        skill_dir = home / ".memtomem" / "skills" / name
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MANIFEST).write_text(f"# {name}\n{body}", encoding="utf-8")

    def test_user_force_bypasses_and_fans_out(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        home = home.resolve()
        self._make_user_skill(home, "leaky", _FALSE_POSITIVE)
        proj = tmp_path / "proj"
        proj.mkdir()

        result = generate_all_skills(proj, scope="user", force_unsafe=True)

        # Forced: the hit flips from skip to fan-out — no privacy_blocked skip,
        # and the runtime files actually land in the user roots.
        assert not [s for s in result.skipped if s[2] == "privacy_blocked"], result.skipped
        assert result.generated, "forced user-tier sync should fan out"
        assert (home / ".claude" / "skills" / "leaky" / SKILL_MANIFEST).is_file()

    def test_user_no_force_skips_privacy_blocked(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        home = tmp_path / "home"
        home.mkdir()
        set_home(monkeypatch, home)
        home = home.resolve()
        self._make_user_skill(home, "leaky", _FALSE_POSITIVE)
        proj = tmp_path / "proj"
        proj.mkdir()

        result = generate_all_skills(proj, scope="user", force_unsafe=False)

        # Default: blocked → typed skip, nothing fanned out, no runtime write.
        assert any(s[2] == "privacy_blocked" for s in result.skipped), result.skipped
        assert not result.generated
        assert not (home / ".claude" / "skills" / "leaky").exists()

    def test_project_shared_force_still_hard_refuses_skills(self, tmp_path: Path) -> None:
        # scan_artifact_tree path. force_unsafe is NOT a blanket escape:
        # project_shared content goes into git history, so the engine's Gate A
        # hard-refuses regardless of the flag (raises before any write).
        proj = tmp_path / "proj"
        skill_dir = proj / ".memtomem" / "skills" / "leaky"
        skill_dir.mkdir(parents=True)
        (skill_dir / SKILL_MANIFEST).write_text(f"# leaky\n{_FALSE_POSITIVE}", encoding="utf-8")

        with pytest.raises(PrivacyBlockedError):
            generate_all_skills(proj, scope="project_shared", force_unsafe=True)

        # No skill content was promoted (a now-empty staging parent dir may
        # remain, but the runtime skill itself must never land).
        assert not (proj / ".claude" / "skills" / "leaky" / SKILL_MANIFEST).exists()

    def test_project_shared_force_still_hard_refuses_agents(self, tmp_path: Path) -> None:
        # scan_text_content path (agents/commands route through sync_atomic_artifact).
        from memtomem.context.agents import generate_all_agents

        proj = tmp_path / "proj"
        agents_dir = proj / ".memtomem" / "agents"
        agents_dir.mkdir(parents=True)
        (agents_dir / "leaky.md").write_text(
            f"---\nname: leaky\ndescription: x\n---\n{_FALSE_POSITIVE}",
            encoding="utf-8",
        )

        with pytest.raises(PrivacyBlockedError):
            generate_all_agents(proj, scope="project_shared", force_unsafe=True)
