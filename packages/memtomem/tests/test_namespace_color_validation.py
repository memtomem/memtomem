"""Namespace colors are stored only as ``#`` + 3-8 hex digits (#3109).

The Web UI interpolates ``ns.color`` into ``style`` and ``value`` attributes,
so the value is validated where every writer lands
(``storage.set_namespace_meta``), the web schema derives its pattern from the
same constant, and the SPA's render-side fallback uses the same expression.
"""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from memtomem.cli import cli
from memtomem.constants import NAMESPACE_COLOR_PATTERN, validate_namespace_color
from memtomem.server.context import AppContext
from memtomem.server.tools.multi_agent import mem_agent_register
from memtomem.server.tools.namespace import mem_ns_update
from memtomem.web.schemas.namespaces import NamespaceMetaRequest

from test_validate_namespace import _StubCtx

_STATIC = Path(__file__).resolve().parents[1] / "src" / "memtomem" / "web" / "static"

ACCEPTED = ["#abc", "#ABCD", "#a1b2c3", "#AbCdEf12", "#12345"]
REJECTED = [
    "blue",
    "#12",
    "#123456789",
    "abc",
    "#abc\n",
    " #abc",
    "#ggg",
    '"><img src=x onerror=alert(1)>',
    "#abc;background:url(x)",
]


@pytest.mark.parametrize("color", ACCEPTED)
def test_validator_accepts_hex(color: str) -> None:
    assert validate_namespace_color(color) == color


@pytest.mark.parametrize("color", [None, ""])
def test_validator_passes_none_and_empty_through(color: str | None) -> None:
    assert validate_namespace_color(color) == color


@pytest.mark.parametrize("color", [*REJECTED, 123])
def test_validator_rejects_everything_else(color: object) -> None:
    with pytest.raises(ValueError, match="Invalid namespace color"):
        validate_namespace_color(color)


@pytest.mark.parametrize("color", [*ACCEPTED, *REJECTED, ""])
def test_web_schema_agrees_with_the_validator(color: str) -> None:
    try:
        validate_namespace_color(color)
        storage_ok = True
    except ValueError:
        storage_ok = False
    try:
        NamespaceMetaRequest(color=color)
        schema_ok = True
    except ValidationError:
        schema_ok = False
    assert schema_ok == storage_ok


def test_spa_fallback_uses_the_same_pattern() -> None:
    app_js = (_STATIC / "app.js").read_text(encoding="utf-8")
    match = re.search(r"function safeCssColor\(color, fallback\) \{\n\s+return (.+?);\n\}", app_js)
    assert match is not None
    assert f"/^{NAMESPACE_COLOR_PATTERN}$/.test(color)" in match.group(1)


class TestStorageRefusesBeforeWriting:
    async def test_invalid_color_leaves_an_existing_row_unchanged(self, storage) -> None:
        await storage.set_namespace_meta("proj", description="d", color="#123456")

        with pytest.raises(ValueError, match="Invalid namespace color"):
            await storage.set_namespace_meta("proj", description="new", color='"><b>')

        meta = await storage.get_namespace_meta("proj")
        assert (meta["description"], meta["color"]) == ("d", "#123456")

    async def test_invalid_color_creates_no_row(self, storage) -> None:
        with pytest.raises(ValueError):
            await storage.set_namespace_meta("fresh", color="red")

        assert await storage.get_namespace_meta("fresh") is None

    async def test_empty_string_clears(self, storage) -> None:
        await storage.set_namespace_meta("proj", color="#123456")
        await storage.set_namespace_meta("proj", color="")

        assert (await storage.get_namespace_meta("proj"))["color"] == ""


class TestMcpSurfaces:
    async def test_mem_ns_update_returns_an_error_and_stores_nothing(self, components) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_ns_update(
            namespace="proj",
            color='"><img src=x onerror=alert(1)>',
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith("Error:") and "Invalid namespace color" in out
        assert await components.storage.get_namespace_meta("proj") is None

    async def test_mem_agent_register_returns_an_error_and_stores_nothing(self, components) -> None:
        ctx = _StubCtx(AppContext.from_components(components))

        out = await mem_agent_register(
            agent_id="alpha",
            color="red",
            ctx=ctx,  # type: ignore[arg-type]
        )

        assert out.startswith("Error:") and "Invalid namespace color" in out
        assert await components.storage.get_namespace_meta("agent-runtime:alpha") is None


def test_cli_agent_register_reports_the_storage_refusal(monkeypatch) -> None:
    """The refusal itself is pinned above on real storage; this pins that the
    CLI turns it into a non-zero exit with the message, not a traceback."""
    from contextlib import asynccontextmanager

    storage = SimpleNamespace(
        set_namespace_meta=AsyncMock(
            side_effect=lambda ns, description=None, color=None: validate_namespace_color(color)
        ),
        get_namespace_meta=AsyncMock(return_value=None),
    )

    @asynccontextmanager
    async def fake():
        yield SimpleNamespace(storage=storage)

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)

    result = CliRunner().invoke(cli, ["agent", "register", "alpha", "--color", '"><x>'])

    assert result.exit_code != 0
    assert "Invalid namespace color" in result.output
    assert result.exception is None or isinstance(result.exception, SystemExit)
