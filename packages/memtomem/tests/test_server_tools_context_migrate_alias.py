"""Backcompat pins for the #1147 (B5-2) rename of ``mem_context_migrate``.

``mem_context_migrate`` was renamed to ``mem_context_memory_migrate`` because
it only ever covered *memory*-tier migration while its bare name implied parity
with the full CLI ``mm context migrate`` (artifact flat→dir + scope-tier moves,
now ``mem_context_artifact_migrate``).

The full wrapper contract is pinned in ``test_server_tools_context_migrate.py``
(now targeting the canonical name). This file pins ONLY the deprecation
contract: the old name still works as an MCP-direct alias that forwards every
argument, and the ``mem_do`` action ``context_migrate`` resolves to
``context_memory_migrate``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.server.tool_registry import ACTIONS
from memtomem.server.tools.context import (
    mem_context_memory_migrate,
    mem_context_migrate,
)
from memtomem.server.tools.meta import _ALIASES, mem_do


def test_deprecated_alias_docstring_marks_deprecation() -> None:
    """The alias docstring opens with DEPRECATED so MCP catalogs surface it."""
    assert (mem_context_migrate.__doc__ or "").lstrip().startswith("DEPRECATED")


def test_registry_renamed_old_action_not_registered() -> None:
    """The @register'd canonical action lives under the new name; the old
    action name is NOT registered (the alias tool is MCP-direct only) — mem_do
    reaches it solely via ``_ALIASES``."""
    assert "context_memory_migrate" in ACTIONS
    assert "context_migrate" not in ACTIONS


def test_mem_do_alias_maps_old_action_to_canonical() -> None:
    assert _ALIASES["context_migrate"] == "context_memory_migrate"


@pytest.mark.anyio
async def test_alias_delegates_from_equals_to(tmp_path: Path) -> None:
    """The alias forwards to the canonical: identical early-exit output, no
    storage backend needed (``from == to`` short-circuits at the wrapper)."""
    src = tmp_path / "rule.md"
    src.write_text("body", encoding="utf-8")

    canonical = await mem_context_memory_migrate(
        source=str(src), from_scope="user", to_scope="user"
    )
    aliased = await mem_context_migrate(source=str(src), from_scope="user", to_scope="user")
    assert aliased == canonical == "error: --from and --to must differ."


@pytest.mark.anyio
async def test_alias_delegates_unknown_scope(tmp_path: Path) -> None:
    src = tmp_path / "rule.md"
    src.write_text("body", encoding="utf-8")

    out = await mem_context_migrate(source=str(src), from_scope="bogus", to_scope="user")
    assert out.startswith("error:")
    assert "from_scope='bogus'" in out


@pytest.mark.anyio
async def test_mem_do_old_action_name_still_routes(tmp_path: Path) -> None:
    """``mem_do(action="context_migrate", ...)`` resolves through ``_ALIASES``
    to the renamed action and behaves identically."""
    src = tmp_path / "rule.md"
    src.write_text("body", encoding="utf-8")

    out = await mem_do(
        action="context_migrate",
        params={"source": str(src), "from_scope": "user", "to_scope": "user"},
    )
    assert out == "error: --from and --to must differ."
