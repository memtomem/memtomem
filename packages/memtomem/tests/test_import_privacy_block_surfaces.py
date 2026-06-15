"""Regression pins: the import-side ``project_shared`` Gate A block must
render as HTTP 422 at the web surface, never a 500.

Sibling of ``test_sync_privacy_block_surfaces.py`` (#895, the sync
direction). The reverse-import engine
(``extract_{skills,agents,commands}_to_canonical``) hard-aborts a
``project_shared`` privacy hit by raising ``click.ClickException`` inside
``_gate_a.apply_gate_a`` (ADR-0011 §5 — git history is forever, so there is
no force bypass for ``project_shared``). The MCP import tool already catches
that exception (``server/tools/context.py``) and the CLI runs under Click,
but the three web import routes caught only ``TimeoutError`` — so a real
secret inside a skill the user clicked "Import" on fell through to the
generic ``Exception`` handler and came back as
``{"detail": "Internal server error"}`` (HTTP 500). These pin the 422
translation across all three artifact kinds plus the single-item route.
"""

from __future__ import annotations

from pathlib import Path

import click
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

# Echoes the project_shared block wording; never carries the matched bytes
# (the ``never echo secrets`` contract — only the hit count + file name).
_MSG = (
    "Gate A: SKILL.md contains 2 privacy pattern hit(s); import to scope='project_shared' rejected."
)


def _raise_click(*args, **kwargs):
    raise click.ClickException(_MSG)


def _build_app_with(router) -> FastAPI:
    """Minimal FastAPI app + ``get_project_root`` override + the production
    generic handler.

    Mirrors ``test_sync_privacy_block_surfaces.py`` (booting the prod factory
    pulls in storage/embedder the import route does not need). The generic
    ``Exception`` handler is registered to mirror ``web/app.py`` so a route
    that fails to translate ``ClickException`` reproduces the production 500
    fall-through — without it, ``TestClient`` would re-raise the exception
    instead of returning the response the user actually saw.
    """
    from memtomem.web.deps import get_project_root

    app = FastAPI()
    app.include_router(router)
    app.dependency_overrides[get_project_root] = lambda: Path("/tmp/p")

    @app.exception_handler(Exception)
    async def _generic(request, exc):  # pragma: no cover - mirrors app.py:261
        return JSONResponse(status_code=500, content={"detail": "Internal server error"})

    return app


class TestWebImportTranslatesTo422:
    def test_skills_section_import_returns_422(self, monkeypatch) -> None:
        from memtomem.web.routes import context_skills

        monkeypatch.setattr(context_skills, "extract_skills_to_canonical", _raise_click)
        client = TestClient(_build_app_with(context_skills.router))

        res = client.post("/context/skills/import", json={})

        assert res.status_code == 422, res.text
        assert _MSG in res.json()["detail"]

    def test_skills_single_import_returns_422(self, monkeypatch) -> None:
        from memtomem.web.routes import context_skills

        monkeypatch.setattr(context_skills, "extract_skills_to_canonical", _raise_click)
        client = TestClient(_build_app_with(context_skills.router))

        res = client.post("/context/skills/leak/import", json={})

        assert res.status_code == 422, res.text
        assert _MSG in res.json()["detail"]

    def test_agents_section_import_returns_422(self, monkeypatch) -> None:
        from memtomem.web.routes import context_agents

        monkeypatch.setattr(context_agents, "extract_agents_to_canonical", _raise_click)
        client = TestClient(_build_app_with(context_agents.router))

        res = client.post("/context/agents/import", json={})

        assert res.status_code == 422, res.text
        assert _MSG in res.json()["detail"]

    def test_agents_single_import_returns_422(self, monkeypatch) -> None:
        from memtomem.web.routes import context_agents

        monkeypatch.setattr(context_agents, "extract_agents_to_canonical", _raise_click)
        client = TestClient(_build_app_with(context_agents.router))

        res = client.post("/context/agents/leak/import", json={})

        assert res.status_code == 422, res.text
        assert _MSG in res.json()["detail"]

    def test_commands_section_import_returns_422(self, monkeypatch) -> None:
        from memtomem.web.routes import context_commands

        monkeypatch.setattr(context_commands, "extract_commands_to_canonical", _raise_click)
        client = TestClient(_build_app_with(context_commands.router))

        res = client.post("/context/commands/import", json={})

        assert res.status_code == 422, res.text
        assert _MSG in res.json()["detail"]

    def test_commands_single_import_returns_422(self, monkeypatch) -> None:
        from memtomem.web.routes import context_commands

        monkeypatch.setattr(context_commands, "extract_commands_to_canonical", _raise_click)
        client = TestClient(_build_app_with(context_commands.router))

        res = client.post("/context/commands/leak/import", json={})

        assert res.status_code == 422, res.text
        assert _MSG in res.json()["detail"]
