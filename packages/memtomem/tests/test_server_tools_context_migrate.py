"""MCP parity pins for ``mem_context_migrate`` wrapping the CLI
``mm context memory-migrate`` verb.

Closes the second half of #887. The underlying migration semantics
(chunk-id-stable rename, glob, lockfile, compensation) are pinned by
``test_context_memory_migrate.py``; this file pins only the MCP-shape
contract:

1. Vocabulary validation rejects unknown scope values without touching
   the helper.
2. ``from_scope == to_scope`` short-circuits at the wrapper.
3. ``to_scope='project_shared'`` without ``confirm_project_shared=True``
   short-circuits at the wrapper — the heavy helper is never invoked.
4. ``to_scope='project_shared'`` with confirm + a secret in the source
   surfaces as a ``privacy block:`` string (the CLI's
   ``click.exceptions.Exit`` translated for MCP).
5. Dry-run captures the plan output via ``click.echo`` redirection and
   returns it as a string; the DB UPDATE is never called.
6. ``apply_=True`` against a non-project_shared target calls the
   helper end-to-end and returns the success summary.

Reuses the AsyncMock-based ``cli_components`` patching from
``test_context_memory_migrate.py`` to avoid spinning up a real storage
backend for the wrapper contract tests.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from memtomem import privacy
from memtomem.server.tools.context import mem_context_migrate


_SECRET = "api_key=AKIA1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


@pytest.fixture
def fake_project_layout(tmp_path: Path):
    project_root = tmp_path / "proj"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_local = project_root / ".memtomem" / "memories.local"
    proj_shared.mkdir(parents=True)
    proj_local.mkdir(parents=True)
    (project_root / ".git").mkdir()

    user_tier = tmp_path / "user_home" / ".memtomem" / "memories"
    user_tier.mkdir(parents=True)
    src = user_tier / "rule.md"
    src.write_text("## Rule\n\nharmless team rule body.\n", encoding="utf-8")
    return {
        "project_root": project_root,
        "proj_shared": proj_shared,
        "proj_local": proj_local,
        "user_tier": user_tier,
        "src": src,
    }


def _patch_cli_components(monkeypatch: pytest.MonkeyPatch, comp) -> None:
    """Replace ``cli_components`` with a no-op context yielding ``comp``."""

    @asynccontextmanager
    async def _fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake)


def _stub_components(layout):
    comp = AsyncMock()
    comp.config.indexing.memory_dirs = [layout["user_tier"]]
    # Register both project tiers so the ``is_project_tier_registered``
    # guard accepts moves to either project_shared or project_local.
    comp.config.indexing.project_memory_dirs = [
        layout["proj_shared"],
        layout["proj_local"],
    ]
    comp.storage = AsyncMock()
    comp.storage.count_chunks_by_source = AsyncMock(return_value=2)
    comp.storage.count_chunk_links_for_source = AsyncMock(return_value=0)
    comp.storage.update_chunks_scope_for_source = AsyncMock(return_value=2)
    comp.search_pipeline = AsyncMock()
    return comp


# ---------------------------------------------------------------------------
# 1) Vocabulary + from==to validation — refuse before touching the helper.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mem_context_migrate_unknown_from_scope_rejected_without_helper(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Bogus ``from_scope`` must return a clean error string without
    spinning up ``cli_components``. Otherwise the helper raises a
    ``MemoryScopeError`` wrapped in ``ClickException`` deeper in, which
    works but pays the bootstrap cost for a vocabulary typo.
    """
    src = tmp_path / "rule.md"
    src.write_text("body", encoding="utf-8")

    out = await mem_context_migrate(
        source=str(src),
        from_scope="bogus",
        to_scope="user",
    )
    assert out.startswith("error:")
    assert "Unknown from_scope='bogus'" in out


@pytest.mark.anyio
async def test_mem_context_migrate_from_equals_to_rejected(
    tmp_path: Path,
) -> None:
    src = tmp_path / "rule.md"
    src.write_text("body", encoding="utf-8")

    out = await mem_context_migrate(
        source=str(src),
        from_scope="user",
        to_scope="user",
    )
    assert out == "error: --from and --to must differ."


# ---------------------------------------------------------------------------
# 2) project_shared Gate B — short-circuit at the wrapper.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mem_context_migrate_project_shared_requires_confirmation(
    monkeypatch: pytest.MonkeyPatch,
    fake_project_layout,
) -> None:
    """``to_scope='project_shared'`` without ``confirm_project_shared=True``
    must short-circuit at the MCP wrapper. The heavy helper
    (``_memory_migrate_run``) must NOT be invoked — assert via a sentinel
    that records calls.
    """
    layout = fake_project_layout
    invoked = []

    async def _sentinel(*args, **kwargs):  # noqa: ANN001
        invoked.append((args, kwargs))

    monkeypatch.setattr("memtomem.cli.context_cmd._memory_migrate_run", _sentinel)

    out = await mem_context_migrate(
        source=str(layout["src"]),
        from_scope="user",
        to_scope="project_shared",
    )
    assert out.startswith("needs confirmation:")
    assert "confirm_project_shared=True" in out
    assert invoked == []
    # Source untouched.
    assert layout["src"].exists()
    assert not (layout["proj_shared"] / "rule.md").exists()


# ---------------------------------------------------------------------------
# 3) Gate A privacy block — translate click.exceptions.Exit to "privacy block:".
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mem_context_migrate_project_shared_privacy_block_surfaces(
    monkeypatch: pytest.MonkeyPatch,
    fake_project_layout,
) -> None:
    """A secret in the source rejects the migration with no force bypass
    (ADR-0011 §5). The underlying helper emits to stderr via ``click.secho``
    then raises ``click.exceptions.Exit(1)``; the MCP wrapper must
    translate that into the ``privacy block:`` shape so callers can
    branch on the prefix the same way they do for
    ``mem_context_init`` / ``mem_context_generate``.
    """
    layout = fake_project_layout
    src = layout["src"]
    src.write_text(f"## Token\n\n{_SECRET}\n", encoding="utf-8")

    comp = _stub_components(layout)
    comp.storage.count_chunks_by_source = AsyncMock(return_value=1)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    out = await mem_context_migrate(
        source=str(src),
        from_scope="user",
        to_scope="project_shared",
        apply_=True,
        confirm_project_shared=True,
    )
    assert out.startswith("privacy block:")
    assert "Gate A" in out
    assert "git history is forever" in out
    # Source untouched on Gate A rejection.
    assert src.exists()
    comp.storage.update_chunks_scope_for_source.assert_not_called()


# ---------------------------------------------------------------------------
# 4) Dry-run — capture click.echo plan output, no DB UPDATE.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mem_context_migrate_dry_run_returns_plan_and_does_not_mutate(
    monkeypatch: pytest.MonkeyPatch,
    fake_project_layout,
) -> None:
    layout = fake_project_layout
    src = layout["src"]

    comp = _stub_components(layout)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    out = await mem_context_migrate(
        source=str(src),
        from_scope="user",
        to_scope="project_local",
    )
    # Plan listing from click.echo arrived through stdout capture.
    assert "Plan: migrate rule.md" in out
    assert "chunks affected: 2" in out
    assert "Run with --apply" in out
    # Dry-run never calls the DB UPDATE.
    comp.storage.update_chunks_scope_for_source.assert_not_called()
    # File never moved.
    assert src.exists()
    assert not (layout["proj_local"] / "rule.md").exists()


# ---------------------------------------------------------------------------
# 5) Apply path — non-project_shared, clean content, success summary.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mem_context_migrate_apply_user_to_project_local_calls_helper(
    monkeypatch: pytest.MonkeyPatch,
    fake_project_layout,
) -> None:
    """End-to-end happy path through the wrapper: clean source, non-
    project_shared target (so no Gate B prompt), ``apply_=True``. Asserts
    the helper's DB UPDATE was called once and the success line is in
    the captured stdout.
    """
    layout = fake_project_layout
    src = layout["src"]

    comp = _stub_components(layout)
    _patch_cli_components(monkeypatch, comp)
    monkeypatch.chdir(layout["project_root"])

    out = await mem_context_migrate(
        source=str(src),
        from_scope="user",
        to_scope="project_local",
        apply_=True,
    )
    # The wrapper captures both stdout and stderr; the success secho
    # ("✓ moved ...") goes to stdout.
    assert "moved rule.md" in out
    assert "project_local tier" in out
    comp.storage.update_chunks_scope_for_source.assert_awaited_once()
    # File actually moved on disk.
    assert not src.exists()
    assert (layout["proj_local"] / "rule.md").exists()


# ---------------------------------------------------------------------------
# 6) Source-resolution error — bad glob surfaces as "error: ...".
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_mem_context_migrate_no_glob_match_returns_clean_error(
    tmp_path: Path,
) -> None:
    """A typo'd glob would normally raise ``ClickException`` inside the
    resolver; the wrapper must translate that to an ``error:`` string
    rather than letting the exception bubble through ``tool_handler`` as
    ``internal error``.
    """
    bogus_glob = str(tmp_path / "does-not-exist" / "**" / "*.md")
    out = await mem_context_migrate(
        source=bogus_glob,
        from_scope="user",
        to_scope="project_local",
    )
    assert out.startswith("error:")
    assert "No .md files matched" in out
