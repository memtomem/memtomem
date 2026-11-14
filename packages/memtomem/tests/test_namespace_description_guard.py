"""A namespace description is scanned by the write guard on every writer (#3352).

Four surfaces set it: the web PATCH, MCP ``mem_ns_update`` and
``mem_agent_register``, and ``mm agent register``. All of them land in
``storage.set_namespace_meta``, which scans a non-empty description before any
DB work and raises ``RedactionBlockedError``; each surface's existing error
path renders the refusal. There is no bypass valve: a description is a label.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.cli import cli
from memtomem.cli.agent_cmd import _run_register
from memtomem.errors import RedactionBlockedError
from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import mem_agent_register
from memtomem.server.tools.namespace import mem_ns_update
from test_excluded_target_writes import _patch_cli_components, _web_client
from test_validate_namespace import _StubCtx

# Built at runtime so the source holds no credential-shaped literal.
SECRET = "deploy token sk-" + "a" * 30


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _counts(surface: str) -> dict[str, int]:
    return privacy.snapshot()["by_tool"].get(surface, {})


class TestStorage:
    async def test_a_secret_leaves_an_existing_row_unchanged(self, storage) -> None:
        await storage.set_namespace_meta("proj", description="old", color="#123456")

        with pytest.raises(RedactionBlockedError) as excinfo:
            await storage.set_namespace_meta(
                "proj", description=SECRET, color="#abcdef", surface="s"
            )

        assert excinfo.value.surface == "s" and excinfo.value.hits >= 1
        assert "sk-" not in str(excinfo.value)
        meta = await storage.get_namespace_meta("proj")
        assert (meta["description"], meta["color"]) == ("old", "#123456")
        assert _counts("s")["blocked"] == 1

    async def test_a_secret_creates_no_row(self, storage) -> None:
        with pytest.raises(RedactionBlockedError):
            await storage.set_namespace_meta("fresh", description=SECRET)

        assert await storage.get_namespace_meta("fresh") is None
        assert _counts("storage")["blocked"] == 1

    async def test_a_clean_description_is_stored_and_counted_as_a_pass(self, storage) -> None:
        await storage.set_namespace_meta("proj", description="team notes", surface="s")

        assert (await storage.get_namespace_meta("proj"))["description"] == "team notes"
        assert _counts("s")["pass"] == 1

    @pytest.mark.parametrize("description", [None, ""])
    async def test_leave_and_clear_are_not_scanned(self, storage, description) -> None:
        await storage.set_namespace_meta("proj", description="d")
        privacy.reset_for_tests()

        await storage.set_namespace_meta("proj", description=description, color="#abc")

        assert privacy.snapshot()["by_tool"] == {}
        expected = "d" if description is None else ""
        assert (await storage.get_namespace_meta("proj"))["description"] == expected


class TestWeb:
    async def test_a_secret_answers_the_redaction_payload_and_stores_nothing(
        self, components
    ) -> None:
        await components.storage.set_namespace_meta("proj", description="old")

        async with _web_client(components) as client:
            resp = await client.patch("/api/namespaces/proj", json={"description": SECRET})

        assert resp.status_code == 403, resp.text
        assert resp.json() == {
            "detail": {
                "detail": "redaction_blocked",
                "hits": resp.json()["detail"]["hits"],
                "surface": "web_api_namespace_update",
            }
        }
        assert resp.json()["detail"]["hits"] >= 1
        assert "sk-" not in resp.text
        assert (await components.storage.get_namespace_meta("proj"))["description"] == "old"
        assert _counts("web_api_namespace_update")["blocked"] == 1

    async def test_a_clean_description_saves(self, components) -> None:
        async with _web_client(components) as client:
            resp = await client.patch("/api/namespaces/proj", json={"description": "notes"})

        assert resp.status_code == 200, resp.text
        assert resp.json()["description"] == "notes"
        assert _counts("web_api_namespace_update")["pass"] == 1


class TestMcp:
    async def test_mem_ns_update_refuses_a_secret(self, components) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_ns_update(namespace="proj", description=SECRET, ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error:") and "privacy pattern" in out, out
        assert "sk-" not in out
        assert await components.storage.get_namespace_meta("proj") is None
        assert _counts("mem_ns_update")["blocked"] == 1

    async def test_mem_ns_update_saves_a_clean_description(self, components) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_ns_update(namespace="proj", description="notes", ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error"), out
        assert (await components.storage.get_namespace_meta("proj"))["description"] == "notes"

    async def test_mem_agent_register_refuses_before_any_row(self, components) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_agent_register(agent_id="alpha", description=SECRET, ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error:") and "privacy pattern" in out, out
        assert await components.storage.get_namespace_meta("agent-runtime:alpha") is None
        assert await components.storage.get_namespace_meta("shared") is None
        assert _counts("mem_agent_register")["blocked"] == 1

    async def test_mem_agent_register_saves_a_clean_description(self, components) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_agent_register(agent_id="alpha", description="planner", ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error"), out
        meta = await components.storage.get_namespace_meta("agent-runtime:alpha")
        assert meta["description"] == "planner"
        assert await components.storage.get_namespace_meta("shared") is not None
        # Both scans, the agent's and the shared bootstrap's, count under the tool.
        assert _counts("mem_agent_register")["pass"] == 2
        assert "storage" not in privacy.snapshot()["by_tool"]


class TestCli:
    async def test_register_refuses_before_any_row(self, components, monkeypatch) -> None:
        _patch_cli_components(monkeypatch, components)

        with pytest.raises(RedactionBlockedError):
            await _run_register("alpha", SECRET, None)

        assert await components.storage.get_namespace_meta("agent-runtime:alpha") is None
        assert await components.storage.get_namespace_meta("shared") is None
        assert _counts("cli_agent_register")["blocked"] == 1

    async def test_register_saves_a_clean_description(self, components, monkeypatch) -> None:
        _patch_cli_components(monkeypatch, components)

        await _run_register("alpha", "planner", None)

        meta = await components.storage.get_namespace_meta("agent-runtime:alpha")
        assert meta["description"] == "planner"
        assert await components.storage.get_namespace_meta("shared") is not None
        assert _counts("cli_agent_register")["pass"] == 2
        assert "storage" not in privacy.snapshot()["by_tool"]


def test_cli_register_reports_the_refusal_as_an_error_exit(monkeypatch) -> None:
    """The refusal is pinned on real storage above; this pins that the command
    turns it into a non-zero exit with the count, not a traceback."""
    from contextlib import asynccontextmanager

    def refuse(ns, description=None, color=None, *, surface="storage"):
        raise RedactionBlockedError("Namespace description", hits=2, surface=surface)

    storage = SimpleNamespace(
        set_namespace_meta=AsyncMock(side_effect=refuse),
        get_namespace_meta=AsyncMock(return_value=None),
    )

    @asynccontextmanager
    async def fake():
        yield SimpleNamespace(storage=storage)

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

    result = CliRunner().invoke(cli, ["agent", "register", "alpha", "--description", "x"])

    assert result.exit_code != 0
    assert "Namespace description matches 2 privacy pattern(s); not saved" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
    assert storage.set_namespace_meta.await_args.kwargs["surface"] == "cli_agent_register"
