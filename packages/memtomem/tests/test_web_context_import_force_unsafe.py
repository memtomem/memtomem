"""The web import body's ``force_unsafe_import`` flag reaches the engine.

ADR-0011 §5 gives ``user`` (and ``project_local``) a reviewed Gate A bypass —
the CLI's ``--force-unsafe-import`` — while ``project_shared`` hard-refuses
regardless. The three web import routes (skills/agents/commands) exposed
``overwrite`` and ``allow_host_writes`` but not the force valve, so a reviewed
false positive (e.g. a pydantic ``api_key: str`` type annotation, which Gate
A's ``(api_key|secret_key|...)\\s*[:=]`` heuristic matches even though no real
secret is present) could not be imported from the browser on any tier. These
pin that the body field is threaded verbatim to ``extract_*_to_canonical`` and
defaults to ``False`` when omitted, across all three kinds plus the
single-item route.

Engine *semantics* (force ignored on project_shared, honored on user) are
already pinned in the ``context/*`` engine tests and
``test_import_privacy_block_surfaces.py``; these are wiring tests, so they spy
on the engine and assert the kwarg it received — running under the default
``project_shared`` scope to skip the ``user``-tier host-write gate.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient


class _EngineSpy:
    """Records each call's kwargs and returns a canned ``ExtractResult``."""

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
    """Minimal app + the same ``get_project_root`` override the sibling
    422-translation test uses (``resolve_scope_root`` resolves through it)."""
    from memtomem.web.deps import get_project_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_project_root] = lambda: Path("/tmp/p")
    return TestClient(app)


def _section_cases() -> list[tuple[object, str, str]]:
    from memtomem.web.routes import context_agents, context_commands, context_skills

    return [
        (context_skills, "extract_skills_to_canonical", "/context/skills/import"),
        (context_agents, "extract_agents_to_canonical", "/context/agents/import"),
        (context_commands, "extract_commands_to_canonical", "/context/commands/import"),
    ]


class TestForceUnsafeThreadedToEngine:
    def test_section_import_threads_true(self, monkeypatch) -> None:
        for module, fn_name, url in _section_cases():
            spy = _EngineSpy(module.ExtractResult(imported=[], skipped=[]))
            monkeypatch.setattr(module, fn_name, spy)
            res = _client(module.router).post(url, json={"force_unsafe_import": True})
            assert res.status_code == 200, (url, res.text)
            assert spy.last["force_unsafe_import"] is True, url

    def test_section_import_defaults_false(self, monkeypatch) -> None:
        # Both an explicit empty body and no body at all must default the
        # flag off (the route reads ``body.force_unsafe_import if body else
        # False``).
        for module, fn_name, url in _section_cases():
            spy = _EngineSpy(module.ExtractResult(imported=[], skipped=[]))
            monkeypatch.setattr(module, fn_name, spy)
            client = _client(module.router)
            assert client.post(url, json={}).status_code == 200, url
            assert spy.last["force_unsafe_import"] is False, url
            assert client.post(url).status_code == 200, url
            assert spy.last["force_unsafe_import"] is False, url

    def test_single_skill_import_threads_true(self, monkeypatch) -> None:
        from memtomem.web.routes import context_skills

        # Non-empty so the single-item route doesn't 404 (it 404s on an empty
        # imported+skipped result — "you clicked an item that doesn't exist").
        spy = _EngineSpy(
            context_skills.ExtractResult(
                imported=[Path("/tmp/p/.memtomem/skills/llm-project-architect")],
                skipped=[],
            )
        )
        monkeypatch.setattr(context_skills, "extract_skills_to_canonical", spy)
        res = _client(context_skills.router).post(
            "/context/skills/llm-project-architect/import",
            json={"force_unsafe_import": True},
        )
        assert res.status_code == 200, res.text
        assert spy.last["force_unsafe_import"] is True
        assert spy.last["only_name"] == "llm-project-architect"
