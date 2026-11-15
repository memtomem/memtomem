"""Namespace descriptions must be strings when provided (#3355).

Every metadata writer (Web PATCH, ``mem_ns_update``, ``mem_do`` with ``ns_update``,
``mem_agent_register``, ``mm agent register --description``) lands at
``storage.set_namespace_meta``, so non-string descriptions are refused
before any database work or write guard scanning.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from memtomem.cli import cli
from memtomem.constants import validate_namespace_description
from memtomem.server.context import AppContext
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.multi_agent import mem_agent_register
from memtomem.server.tools.namespace import mem_ns_update
from memtomem.web.schemas.namespaces import NamespaceMetaRequest

from test_validate_namespace import _StubCtx

ACCEPTED = ["General project notes", "team: docs", ""]
REJECTED = [
    0,
    False,
    True,
    5,
    [],
    ["sk-secret-token"],
    {"k": 1},
    object(),
]


@pytest.mark.parametrize("desc", ACCEPTED)
def test_validator_accepts_strings(desc: str) -> None:
    assert validate_namespace_description(desc) == desc


def test_validator_passes_none_through() -> None:
    assert validate_namespace_description(None) is None


@pytest.mark.parametrize("desc", REJECTED)
def test_validator_rejects_non_strings(desc: object) -> None:
    with pytest.raises(ValueError, match="Invalid namespace description: expected a string"):
        validate_namespace_description(desc)


@pytest.mark.parametrize("desc", [["list"], {"key": "val"}])
def test_web_schema_agrees_with_the_validator(desc: object) -> None:
    with pytest.raises(ValidationError):
        NamespaceMetaRequest(description=desc)  # type: ignore[arg-type]


class TestStorageRefusesBeforeWriting:
    @pytest.mark.parametrize("bad", [0, False, [], 5, ["sk-test"]])
    async def test_invalid_description_leaves_an_existing_row_unchanged(
        self, storage, bad: object
    ) -> None:
        await storage.set_namespace_meta("proj", description="original", color="#123456")

        with pytest.raises(ValueError, match="Invalid namespace description: expected a string"):
            await storage.set_namespace_meta("proj", description=bad)

        meta = await storage.get_namespace_meta("proj")
        assert (meta["description"], meta["color"]) == ("original", "#123456")

    @pytest.mark.parametrize("bad", [0, False, [], 5])
    async def test_invalid_description_creates_no_row(self, storage, bad: object) -> None:
        with pytest.raises(ValueError, match="Invalid namespace description: expected a string"):
            await storage.set_namespace_meta("fresh", description=bad)

        assert await storage.get_namespace_meta("fresh") is None

    async def test_empty_string_clears(self, storage) -> None:
        await storage.set_namespace_meta("proj", description="original")
        await storage.set_namespace_meta("proj", description="")

        assert (await storage.get_namespace_meta("proj"))["description"] == ""

    async def test_none_leaves_unchanged(self, storage) -> None:
        await storage.set_namespace_meta("proj", description="original", color="#123456")
        await storage.set_namespace_meta("proj", description=None, color="#abcdef")

        meta = await storage.get_namespace_meta("proj")
        assert (meta["description"], meta["color"]) == ("original", "#abcdef")


class TestMcpSurfaces:
    @pytest.mark.parametrize("bad", [0, False, [], 5, ["sk-token"]])
    async def test_mem_ns_update_returns_an_error_and_stores_nothing(
        self, components, bad: object
    ) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_ns_update(
            namespace="proj",
            description=bad,  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert (
            out.startswith("Error:") and "Invalid namespace description: expected a string" in out
        )
        assert await components.storage.get_namespace_meta("proj") is None

    @pytest.mark.parametrize("bad", [0, False, [], 5, ["sk-token"]])
    async def test_mem_do_ns_update_returns_an_error_and_stores_nothing(
        self, components, bad: object
    ) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_do(
            action="ns_update",
            params={"namespace": "proj", "description": bad},
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert (
            out.startswith("Error:") and "Invalid namespace description: expected a string" in out
        )
        assert await components.storage.get_namespace_meta("proj") is None

    @pytest.mark.parametrize("bad", [0, False, [], 5])
    async def test_mem_agent_register_returns_an_error_and_stores_nothing(
        self, components, bad: object
    ) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_agent_register(
            agent_id="alpha",
            description=bad,  # type: ignore[arg-type]
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert (
            out.startswith("Error:") and "Invalid namespace description: expected a string" in out
        )
        assert await components.storage.get_namespace_meta("agent-runtime:alpha") is None


def test_cli_agent_register_reports_the_storage_refusal(monkeypatch) -> None:
    """The refusal itself is pinned above on real storage; this pins that the
    CLI turns it into a non-zero exit with the message, not a traceback."""
    from contextlib import asynccontextmanager

    storage = SimpleNamespace(
        set_namespace_meta=AsyncMock(
            side_effect=lambda ns, description=None, color=None, **kwargs: (
                validate_namespace_description(description)
            )
        ),
        get_namespace_meta=AsyncMock(return_value=None),
    )

    @asynccontextmanager
    async def fake():
        yield SimpleNamespace(storage=storage)

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

    result = CliRunner().invoke(cli, ["agent", "register", "alpha", "--description", "desc"])

    # Normal string description passes
    assert result.exit_code == 0

    # If storage refuses description, CLI turns it into a clean error exit
    storage.set_namespace_meta = AsyncMock(
        side_effect=ValueError("Invalid namespace description: expected a string")
    )
    result = CliRunner().invoke(cli, ["agent", "register", "alpha", "--description", "desc"])
    assert result.exit_code != 0
    assert "Invalid namespace description: expected a string" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
