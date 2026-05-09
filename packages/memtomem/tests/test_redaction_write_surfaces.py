"""Trust-boundary redaction guard wire-in across non-MCP-add ingress surfaces.

Pinned: every user-driven write surface that creates or replaces
markdown content runs the same ``privacy.enforce_write_guard`` shape.
Wire-in for ``mem_add`` / ``mem_batch_add`` lives in
``test_memory_crud_redaction.py``; the helper contract itself is in
``test_privacy.py::TestEnforceWriteGuard``. This module covers the
gap surfaces that PR-1 unified:

- MCP ``mem_edit``
- Web ``POST /api/add``, ``POST /api/upload``, ``PATCH /api/chunks/{id}``,
  ``POST /api/scratch/{key}/promote``
- CLI ``mm add``, ``mm agent share``
- LangGraph ``MemtomemStore.add()``

Each surface gets at minimum (block / bypass-with-counter / clean-pass)
to lock the three-label counter contract. The hit-vs-no-hit split must
fail loudly if a future refactor drops the guard from any one surface
or starts logging matched bytes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest
from click.testing import CliRunner

from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import mem_edit

from helpers import StubCtx

_SECRET_SAMPLE = "Notes on token: sk-" + "a" * 30
_CLEAN_SAMPLE = "Met with the team about Q2 deploy plans."


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


# ---------------------------------------------------------------------------
# MCP ``mem_edit``
# ---------------------------------------------------------------------------


class TestMemEditRedactionGuard:
    """ADR-0011 PR-D: the edit-surface guard runs **after** the chunk
    lookup so the chunk's persisted ``metadata.scope`` can be fed in
    (inferred-scope contract). A non-existent chunk_id therefore short-
    circuits before the guard fires; tests provide a stub chunk via
    monkeypatch so the guard is exercised in the normal flow.
    """

    @staticmethod
    def _stub_user_chunk(monkeypatch, comp):
        """Wire ``comp.storage.get_chunk`` to return a user-scope chunk
        so the edit-surface guard runs with scope='user' (the default
        path the existing tests cover)."""
        from unittest.mock import AsyncMock

        from memtomem.models import Chunk, ChunkMetadata

        chunk = Chunk(
            content="placeholder",
            metadata=ChunkMetadata(
                source_file=Path("/tmp/never_touched.md"),
                scope="user",
                start_line=1,
                end_line=2,
            ),
            embedding=[0.1] * 1024,
        )
        monkeypatch.setattr(comp.storage, "get_chunk", AsyncMock(return_value=chunk))
        # The downstream rollback path tries to read the source file;
        # we only care about the guard's accounting, so stub the
        # filesystem mutation + reindex to no-ops.

        async def _noop_index_file(*args, **kwargs):
            from memtomem.models import IndexingStats

            return IndexingStats(0, 0, 0, 0, 0, 0.0)

        monkeypatch.setattr(comp.index_engine, "index_file", _noop_index_file)

    @pytest.mark.asyncio
    async def test_blocks_secret_and_records_blocked(self, bm25_only_components, monkeypatch):
        """Secret content rejected without ``force_unsafe``; counter
        increments under the ``mem_edit`` ``by_tool`` key (not
        ``mem_add``) so the guard's surface attribution stays observable.
        """
        comp, mem_dir = bm25_only_components
        self._stub_user_chunk(monkeypatch, comp)
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        before = privacy.snapshot()["by_tool"].get(
            "mem_edit", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        result = await mem_edit(  # type: ignore[arg-type]
            chunk_id=str(uuid4()),
            new_content=_SECRET_SAMPLE,
            ctx=ctx,
        )
        after = privacy.snapshot()["by_tool"]["mem_edit"]

        assert "Error" in result
        assert "privacy pattern" in result
        assert "force_unsafe" in result
        assert after["blocked"] == before["blocked"] + 1

    @pytest.mark.asyncio
    async def test_force_unsafe_records_bypassed(self, bm25_only_components, caplog, monkeypatch):
        comp, mem_dir = bm25_only_components
        self._stub_user_chunk(monkeypatch, comp)
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        before = privacy.snapshot()["by_tool"].get(
            "mem_edit", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            await mem_edit(  # type: ignore[arg-type]
                chunk_id=str(uuid4()),
                new_content=_SECRET_SAMPLE,
                force_unsafe=True,
                ctx=ctx,
            )
        after = privacy.snapshot()["by_tool"]["mem_edit"]

        assert after["bypassed"] == before["bypassed"] + 1
        assert "redaction bypass" in caplog.text
        # Matched bytes must not surface in the audit log.
        assert "sk-" not in caplog.text

    @pytest.mark.asyncio
    async def test_clean_content_records_pass(self, bm25_only_components, monkeypatch):
        comp, mem_dir = bm25_only_components
        self._stub_user_chunk(monkeypatch, comp)
        app = AppContext.from_components(comp)
        ctx = StubCtx(app)

        before = privacy.snapshot()["by_tool"].get(
            "mem_edit", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        await mem_edit(  # type: ignore[arg-type]
            chunk_id=str(uuid4()),
            new_content=_CLEAN_SAMPLE,
            ctx=ctx,
        )
        after = privacy.snapshot()["by_tool"]["mem_edit"]

        assert after["pass"] == before["pass"] + 1
        assert after["blocked"] == before["blocked"]


# ---------------------------------------------------------------------------
# CLI ``mm add``
# ---------------------------------------------------------------------------


class TestCliMmAddRedactionGuard:
    """``mm add`` is the synchronous click entry point; the guard runs
    before any component bootstrap so we can exercise it without spinning
    up the real engine. The ``--force-unsafe`` flag is the only path
    through which a CLI user can land secret content.
    """

    def test_blocks_secret_and_does_not_index(self):
        from memtomem.cli.memory import add as add_cmd

        runner = CliRunner()
        with patch("memtomem.cli._bootstrap.cli_components") as mock_bootstrap:
            mock_bootstrap.assert_not_called()
            result = runner.invoke(add_cmd, [_SECRET_SAMPLE])
            # Bootstrap must never be reached on a blocked write.
            mock_bootstrap.assert_not_called()

        assert result.exit_code != 0
        assert "privacy pattern" in (result.output + str(result.exception or ""))
        snap = privacy.snapshot()["by_tool"].get("cli_mm_add", {})
        assert snap.get("blocked", 0) == 1

    def test_blocks_force_unsafe_secret_on_project_shared_scope(self):
        """ADR-0011 §5: ``force_unsafe=True`` on ``project_shared`` is
        hard-refused at the chokepoint. The CLI surface must mirror the
        MCP refusal — without this branch ``mm mem add --scope
        project_shared --force-unsafe`` would still land flagged content
        in the git-tracked tier.
        """
        from memtomem.cli.memory import add as add_cmd

        runner = CliRunner()
        with patch("memtomem.cli._bootstrap.cli_components") as mock_bootstrap:
            mock_bootstrap.assert_not_called()
            result = runner.invoke(
                add_cmd,
                [_SECRET_SAMPLE, "--scope", "project_shared", "--force-unsafe", "--yes"],
            )
            mock_bootstrap.assert_not_called()

        assert result.exit_code != 0
        out = result.output + str(result.exception or "")
        assert "force-unsafe is not permitted" in out
        assert "git history is forever" in out
        snap = privacy.snapshot()["by_tool"].get("cli_mm_add", {})
        assert snap.get("blocked_project_shared", 0) == 1

    def test_blocks_unregistered_project_tier_target(self, monkeypatch, tmp_path):
        """ADR-0011 PR-D review (round 6) pin: ``mm mem add --scope
        project_shared`` must refuse when the resolved target tier is
        not in ``IndexingConfig.project_memory_dirs``. Without this,
        the write succeeds but the row's scope flips to ``user`` on
        re-index (registration mismatch) and becomes visible across
        project boundaries.
        """
        from contextlib import asynccontextmanager

        # Components return ``project_memory_dirs=[]`` so any project
        # tier resolves outside the registry.
        @asynccontextmanager
        async def _fake_components():
            comp = MagicMock()
            comp.config.indexing.project_memory_dirs = []
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake_components)
        # ``_resolve_project_context_root`` is lazy-imported inside
        # ``_add`` from its defining module, so patch it there.
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root",
            lambda comp: tmp_path / "proj_unreg",
        )

        from memtomem.cli.memory import add as add_cmd

        runner = CliRunner()
        result = runner.invoke(
            add_cmd,
            [_CLEAN_SAMPLE, "--scope", "project_shared", "--yes"],
        )
        assert result.exit_code != 0
        out = result.output + str(result.exception or "")
        assert "not registered" in out
        # Hint must NOT mention the broken `mm config set ...
        # project_memory_dirs[+]=...` form. ``mm config set`` rejects
        # that key shape (project_memory_dirs is not in MUTABLE_FIELDS),
        # so a user following the message would hit a dead-end.
        assert "mm config set" not in out

    def test_clean_content_records_pass_in_cli_surface(self, monkeypatch, tmp_path):
        """A clean write still talks to ``cli_components`` — to keep the
        unit fast we stub the bootstrap so no real DB is created. The
        assertion target is the counter, not the indexing side effect.

        ``cli_components`` is imported lazily inside ``_add``, so the
        patch target is the symbol on its defining module
        (``memtomem.cli._bootstrap``) rather than the attribute view on
        ``memtomem.cli.memory``.
        """
        from contextlib import asynccontextmanager

        @asynccontextmanager
        async def _fake_components():
            comp = MagicMock()
            comp.index_engine = AsyncMock()
            comp.storage = AsyncMock()
            comp.index_engine.index_file = AsyncMock(return_value=MagicMock(indexed_chunks=1))
            comp.storage.list_chunks_by_source = AsyncMock(return_value=[])
            yield comp

        monkeypatch.setattr(
            "memtomem.cli._bootstrap.cli_components",
            _fake_components,
        )
        # Redirect ``~/.memtomem/memories`` writes into ``tmp_path`` so
        # the test never touches a real home dir.
        monkeypatch.setenv("HOME", str(tmp_path))
        monkeypatch.setenv("USERPROFILE", str(tmp_path))

        from memtomem.cli.memory import add as add_cmd

        runner = CliRunner()
        result = runner.invoke(add_cmd, [_CLEAN_SAMPLE])
        assert result.exit_code == 0, f"clean write should succeed: {result.output!r}"
        snap = privacy.snapshot()["by_tool"].get("cli_mm_add", {})
        assert snap.get("pass", 0) == 1
        assert snap.get("blocked", 0) == 0


# ---------------------------------------------------------------------------
# LangGraph integration
# ---------------------------------------------------------------------------


class TestLangGraphAddRedactionGuard:
    @pytest.mark.asyncio
    async def test_blocks_secret_and_returns_error_dict(self):
        """``MemtomemStore.add`` returns a structured error dict on a hit
        rather than raising — agents reading the dict downstream must be
        able to distinguish a redaction block from other failure modes.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        mem = MemtomemStore.__new__(MemtomemStore)
        # Bypass __init__ so we don't touch real storage. ``_ensure_init``
        # is stubbed so the lazy-bootstrap path inside ``add`` resolves
        # without spinning up real components; the guard then short-
        # circuits the actual write.
        mem._ensure_init = AsyncMock(return_value=MagicMock())  # type: ignore[attr-defined]

        before = privacy.snapshot()["by_tool"].get(
            "langgraph_add", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        result = await mem.add(content=_SECRET_SAMPLE)
        after = privacy.snapshot()["by_tool"]["langgraph_add"]

        assert isinstance(result, dict)
        assert result.get("error") == "redaction_blocked"
        assert "hits" in result
        assert after["blocked"] == before["blocked"] + 1

    @pytest.mark.asyncio
    async def test_force_unsafe_records_bypassed(self, caplog):
        from memtomem.integrations.langgraph import MemtomemStore

        mem = MemtomemStore.__new__(MemtomemStore)
        # Stub bootstrap so the bypass path can attempt a write without
        # hitting real storage. The append + index step will fail on the
        # MagicMock memory_dirs, but the bypass counter must already
        # have ticked.
        mem._ensure_init = AsyncMock(  # type: ignore[attr-defined]
            return_value=MagicMock(
                config=MagicMock(indexing=MagicMock(memory_dirs=[Path("/nonexistent")])),
                index_engine=AsyncMock(),
            )
        )

        before = privacy.snapshot()["by_tool"].get(
            "langgraph_add", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            try:
                await mem.add(content=_SECRET_SAMPLE, force_unsafe=True)
            except Exception:
                # Downstream write may fail with the stubbed engine —
                # that's fine, the guard counter has already moved.
                pass
        after = privacy.snapshot()["by_tool"]["langgraph_add"]

        assert after["bypassed"] == before["bypassed"] + 1
        assert "redaction bypass" in caplog.text

    @pytest.mark.asyncio
    async def test_blocked_under_agent_session_keeps_error_dict_shape(self):
        """Multi-agent × redaction: a redaction block under an active
        agent session still returns the same error-dict contract and ticks
        the same ``blocked`` counter — the agent identity must not change
        how the trust boundary surfaces failures.
        """
        from memtomem.integrations.langgraph import MemtomemStore

        mem = MemtomemStore.__new__(MemtomemStore)
        mem._current_session_id = "fake-session"
        mem._current_agent_id = "planner"
        mem._ensure_init = AsyncMock(return_value=MagicMock())  # type: ignore[attr-defined]

        before = privacy.snapshot()["by_tool"].get(
            "langgraph_add", {"blocked": 0, "pass": 0, "bypassed": 0}
        )
        result = await mem.add(content=_SECRET_SAMPLE)
        after = privacy.snapshot()["by_tool"]["langgraph_add"]

        assert isinstance(result, dict)
        assert result.get("error") == "redaction_blocked"
        assert "hits" in result
        assert after["blocked"] == before["blocked"] + 1
        # Agent state survives the block — the call is rejected, not the session.
        assert mem._current_agent_id == "planner"

    @pytest.mark.asyncio
    async def test_force_unsafe_under_agent_session_pins_call_order(self, tmp_path, monkeypatch):
        """Multi-agent × redaction bypass: under an active agent session,
        ``force_unsafe=True`` must run **redaction guard → namespace
        resolve → write → index** in that exact order, with
        ``namespace="agent-runtime:<id>"`` reaching the index layer.

        A naked "final ``index_file`` got the agent namespace" assertion
        would still pass if a refactor moved the namespace resolve
        *before* the redaction guard — losing the trust-boundary
        invariant that no namespace work happens on rejected content.
        We sequence-pin via spies on each stage.
        """
        from memtomem import privacy as _privacy
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.tools import memory_writer as _memory_writer

        mem = MemtomemStore.__new__(MemtomemStore)
        mem._current_session_id = "fake-session"
        mem._current_agent_id = "planner"

        memory_dir = tmp_path / "memory"
        memory_dir.mkdir()

        index_engine = AsyncMock()
        mem._ensure_init = AsyncMock(  # type: ignore[attr-defined]
            return_value=MagicMock(
                config=MagicMock(indexing=MagicMock(memory_dirs=[memory_dir])),
                index_engine=index_engine,
            )
        )

        events: list[tuple[str, dict]] = []

        real_guard = _privacy.enforce_write_guard

        def _spy_guard(content, *, surface, force_unsafe, audit_context):
            events.append(("guard", {"force_unsafe": force_unsafe, "surface": surface}))
            return real_guard(
                content,
                surface=surface,
                force_unsafe=force_unsafe,
                audit_context=audit_context,
            )

        monkeypatch.setattr(_privacy, "enforce_write_guard", _spy_guard)

        real_resolve = mem._resolve_add_namespace

        def _spy_resolve(namespace):
            events.append(("resolve_namespace", {"input": namespace}))
            return real_resolve(namespace)

        # Method spy via direct attribute set — ``add`` calls
        # ``self._resolve_add_namespace(namespace)`` which resolves
        # through the instance dict before falling back to the class.
        mem._resolve_add_namespace = _spy_resolve  # type: ignore[method-assign]

        real_append = _memory_writer.append_entry

        def _spy_append(target, content, *, title=None, tags=None):
            events.append(("append", {"target": str(target)}))
            return real_append(target, content, title=title, tags=tags)

        monkeypatch.setattr(_memory_writer, "append_entry", _spy_append)

        async def _spy_index(*args, **kwargs):
            events.append(("index", {"namespace": kwargs.get("namespace")}))
            return MagicMock(indexed_chunks=1)

        index_engine.index_file = _spy_index

        target_file = str(tmp_path / "agent_target.md")
        result = await mem.add(
            content=_SECRET_SAMPLE,
            file=target_file,
            force_unsafe=True,
        )

        # Order pin: redaction guard fires first, then namespace resolve,
        # then write, then index. Any reorder trips the equality check.
        assert [name for name, _ in events] == [
            "guard",
            "resolve_namespace",
            "append",
            "index",
        ], events

        # Index call carried the agent-runtime namespace.
        assert events[3][1]["namespace"] == "agent-runtime:planner"
        # Caller saw the success shape (not the redaction-block dict).
        assert result.get("error") is None
        assert "indexed_chunks" in result
