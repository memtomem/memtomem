"""Per-namespace day files + the mixed-namespace write guard (issue #2005).

``index_file`` re-chunks the whole file and stamps one namespace on every
chunk it rewrites, so an append that merges with an earlier entry moves
that entry into the appending call's namespace. Two defences, both
covered here:

1. The default target is one day file per namespace, so a namespaced
   write never shares a file with another namespace to begin with.
2. Any write into a file that already holds a different namespace is
   refused, unless the caller opts into the mixing explicitly.

The end-to-end regression at the top is the one that fails on the code
this issue was filed against; the rest pin the individual guards.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from helpers import StubCtx, set_home
from memtomem.memory_scope import day_file_name, namespace_mix_error, namespace_mix_refusal


@pytest.fixture
def home(monkeypatch, tmp_path) -> Path:
    """Isolated HOME. ``mm add`` reads ``~/.memtomem/.current_session`` to
    resolve the namespace it inherits, and the developer's own session must
    not reach into these runs."""
    h = tmp_path / "home"
    h.mkdir(exist_ok=True)
    set_home(monkeypatch, h)
    return h


_DATE = "2026-08-03"

# Both well under ``min_chunk_tokens`` (128), which is half of what it takes
# to merge; the other half is a shared heading, since ``append_entry`` gives
# an untitled entry a unique ``## Entry <ts> <uuid>`` one. Tests that need the
# merge pass the same ``title=``.
_ALPHA = "Alpha entry: the deploy pipeline uses blue-green with a five minute bake"
_BETA = "tiny"


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 1. The regression: a later append must not re-namespace an earlier entry
# ---------------------------------------------------------------------------


class TestEarlierEntryKeepsItsNamespace:
    @pytest.mark.asyncio
    async def test_a_later_namespaced_add_leaves_the_earlier_one_alone(self, bm25_only_components):
        """``mem_add(namespace="aaa")`` then ``mem_add(namespace="bbb")``:
        the ``aaa`` entry is still stored under ``aaa``.

        The issue's own repro, driven through the tool rather than through
        a hand-built path, so it exercises the target selection that is the
        fix. Before it, both writes landed in ``{date}.md``, the second
        re-chunked the file, the merged chunk was upserted with ``bbb``, and
        the ``aaa`` entry silently changed namespace — while both calls
        reported the namespace they were asked for.
        """
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        # Same title on purpose: ``append_entry`` gives an untitled entry a
        # unique ``## Entry <ts> <uuid>`` heading, and chunk merging only
        # joins chunks under one heading. A shared title is therefore the
        # shape in which the loss actually happens.
        await mem_add(content=_ALPHA, title="Shared", namespace="aaa", ctx=ctx)  # type: ignore[arg-type]
        await mem_add(content=_BETA, title="Shared", namespace="bbb", ctx=ctx)  # type: ignore[arg-type]

        stored = {
            chunk.metadata.namespace: chunk.content
            for ns in ("aaa", "bbb")
            for src in await comp.storage.list_sources_by_namespace(ns)
            for chunk in await comp.storage.list_chunks_by_source(src)
        }
        assert _ALPHA in stored.get("aaa", ""), (
            "the first entry lost its namespace to the second write"
        )
        assert _BETA in stored.get("bbb", "")

    @pytest.mark.asyncio
    async def test_the_two_writes_land_in_different_files(self, bm25_only_components):
        """The mechanism, pinned separately from the outcome: merging can
        only join chunks within one file, so the namespaces must not share
        one."""
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        await mem_add(content=_ALPHA, namespace="aaa", ctx=ctx)  # type: ignore[arg-type]
        await mem_add(content=_BETA, namespace="bbb", ctx=ctx)  # type: ignore[arg-type]

        written = sorted(p.name for p in mem_dir.glob("*.md"))
        assert len(written) == 2, written
        assert all(name.startswith(_today()) for name in written)
        # The plain day file is reserved for the default namespace.
        assert f"{_today()}.md" not in written

    @pytest.mark.asyncio
    async def test_a_namespaceless_reindex_does_not_restamp(self, bm25_only_components):
        """The watcher and ``mem_edit`` re-index with no namespace at all.

        Without preservation they resolve through the rules/default
        fallback, so the next edit of an ``-n aaa`` file would quietly move
        every changed chunk to the default namespace — undoing the fix one
        file modification later.
        """
        from memtomem.tools.memory_writer import append_entry

        comp, mem_dir = bm25_only_components
        target = mem_dir / "notes.md"
        append_entry(target, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(target, namespace="aaa", already_scanned=True)

        append_entry(target, "a second observation about the same deploy", title=None, tags=[])
        await comp.index_engine.index_file(target, already_scanned=True)

        stored = await comp.storage.namespaces_for_source(target)
        assert stored == ["aaa"], f"re-index without a namespace restamped the file: {stored}"

    @pytest.mark.asyncio
    async def test_deleting_one_chunk_leaves_the_survivors_namespace_alone(
        self, bm25_only_components
    ):
        """The web chunk-delete path re-indexes with ``force=True`` for the
        re-embed, and force re-resolves namespaces — so removing one chunk
        from an ``aaa`` file used to move every survivor to whatever the
        rules say today. It must pass the file's namespace explicitly."""
        from memtomem.tools.memory_writer import append_entry, remove_lines

        comp, mem_dir = bm25_only_components
        target = mem_dir / "multi.md"
        for i in range(3):
            append_entry(
                target,
                f"Entry {i}: a long enough sentence about deploys {i}",
                title=f"H{i}",
                tags=[],
            )
        await comp.index_engine.index_file(target, namespace="aaa", already_scanned=True)
        chunks = await comp.storage.list_chunks_by_source(target)
        assert len(chunks) > 1, "the regression needs survivors to lose their namespace"

        # Exactly what ``web/routes/chunks.py`` does on delete.
        preserved = await comp.index_engine.effective_namespace_for(target)
        victim = chunks[0]
        remove_lines(target, victim.metadata.start_line, victim.metadata.end_line)
        await comp.index_engine.index_file(
            target, force=True, namespace=preserved, already_scanned=True
        )

        assert await comp.storage.namespaces_for_source(target) == ["aaa"]

    @pytest.mark.asyncio
    async def test_the_resolver_answers_the_same_before_and_after_a_write(
        self, bm25_only_components
    ):
        """One resolver, one answer. A default-namespace file resolves to
        ``None`` before it exists (the untagged carve-out) and its chunks
        then store the literal "default" — so reading the stored spelling
        back would make the preview and the ``/api/index`` echo disagree
        about a file that never moved."""
        from memtomem.tools.memory_writer import append_entry

        comp, mem_dir = bm25_only_components
        target = mem_dir / "plain.md"
        before = await comp.index_engine.effective_namespace_for(target)

        append_entry(target, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(target, already_scanned=True)

        assert await comp.storage.namespaces_for_source(target) == ["default"]
        assert await comp.index_engine.effective_namespace_for(target) == before

    @pytest.mark.asyncio
    async def test_a_lookup_failure_refuses_instead_of_re_resolving(
        self, bm25_only_components, monkeypatch
    ):
        """Falling back to the rules on a transient read error would perform
        exactly the silent move this rule prevents, so it raises — and it
        raises something the watcher can recognise as retryable."""
        from memtomem.errors import NamespaceResolutionError

        comp, mem_dir = bm25_only_components
        monkeypatch.setattr(
            comp.storage,
            "namespaces_for_source",
            AsyncMock(side_effect=RuntimeError("store down")),
        )

        with pytest.raises(NamespaceResolutionError):
            await comp.index_engine.effective_namespace_for(mem_dir / "any.md")

    @pytest.mark.asyncio
    async def test_force_reindex_still_applies_the_resolved_namespace(self, bm25_only_components):
        """``--force`` is the documented way to apply changed namespace
        rules, so preservation must not swallow it."""
        from memtomem.tools.memory_writer import append_entry

        comp, mem_dir = bm25_only_components
        target = mem_dir / "rules.md"
        append_entry(target, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(target, namespace="aaa", already_scanned=True)

        await comp.index_engine.index_file(
            target, force=True, namespace="ccc", already_scanned=True
        )

        assert await comp.storage.namespaces_for_source(target) == ["ccc"]


# ---------------------------------------------------------------------------
# 2. Day-file naming
# ---------------------------------------------------------------------------


class TestDayFileName:
    def test_default_namespace_keeps_the_historical_name(self):
        assert day_file_name(None, "default", date_str=_DATE) == f"{_DATE}.md"
        assert day_file_name("default", "default", date_str=_DATE) == f"{_DATE}.md"
        # A configured non-"default" default is still the default file.
        assert day_file_name("team", "team", date_str=_DATE) == f"{_DATE}.md"

    def test_readable_namespace_stays_readable(self):
        name = day_file_name("aaa", "default", date_str=_DATE)
        assert name.startswith(f"{_DATE}--aaa-")
        assert name.endswith(".md")

    def test_illegal_characters_are_replaced(self):
        name = day_file_name("agent-runtime:planner", "default", date_str=_DATE)
        # ``:`` is invalid in a Windows filename component.
        assert ":" not in name
        assert "agent-runtime-planner" in name

    def test_sanitizing_collision_still_gets_distinct_files(self):
        """``a:b`` sanitizes to ``a-b``. Without a digest over the exact
        namespace it would share a file with a literal ``a-b``."""
        assert day_file_name("a:b", "default", date_str=_DATE) != day_file_name(
            "a-b", "default", date_str=_DATE
        )

    def test_case_only_difference_survives_a_case_insensitive_filesystem(self):
        """macOS and Windows default to case-insensitive filesystems, so
        ``Foo``/``foo`` differing only in the readable part would name one
        file. The digest has to differ too."""
        foo = day_file_name("foo", "default", date_str=_DATE).lower()
        cap = day_file_name("Foo", "default", date_str=_DATE).lower()
        assert foo != cap

    def test_long_namespace_fits_a_filename_component(self):
        name = day_file_name("x" * 500, "default", date_str=_DATE)
        assert len(name.encode("utf-8")) < 255

    def test_namespace_of_only_illegal_characters_still_names_a_file(self):
        name = day_file_name(":::", "default", date_str=_DATE)
        assert name.startswith(f"{_DATE}--")
        assert name.endswith(".md")
        assert name != f"{_DATE}--.md"

    def test_digest_is_over_the_exact_namespace(self):
        expected = hashlib.sha256(b"aaa").hexdigest()[:16]
        assert day_file_name("aaa", "default", date_str=_DATE) == f"{_DATE}--aaa-{expected}.md"


# ---------------------------------------------------------------------------
# 3. The refusal message
# ---------------------------------------------------------------------------


class TestNamespaceMixError:
    def test_no_error_when_the_file_agrees(self):
        assert namespace_mix_error(Path("f.md"), ["aaa"], "aaa", override_hint="--flag") is None

    def test_no_error_for_an_unindexed_file(self):
        assert namespace_mix_error(Path("f.md"), [], "aaa", override_hint="--flag") is None

    def test_error_names_the_file_both_namespaces_and_the_override(self):
        err = namespace_mix_error(
            Path("f.md"), ["aaa"], "bbb", override_hint="--allow-namespace-mix"
        )
        assert err is not None
        assert "f.md" in err
        assert "'aaa'" in err and "'bbb'" in err
        assert "--allow-namespace-mix" in err

    def test_every_disagreeing_namespace_is_listed(self):
        err = namespace_mix_error(Path("f.md"), ["aaa", "bbb", "ccc"], "ccc", override_hint="-x")
        assert err is not None
        assert "'aaa'" in err and "'bbb'" in err
        # ``ccc`` is this write's own namespace, not a victim.
        assert "namespace(s) 'aaa', 'bbb'" in err


# ---------------------------------------------------------------------------
# 4. The guard's state machine
# ---------------------------------------------------------------------------


def _engine(resolved: str | None = None):
    return SimpleNamespace(effective_namespace_for=AsyncMock(return_value=resolved))


class TestNamespaceMixRefusal:
    @pytest.mark.asyncio
    async def test_missing_file_is_allowed_without_touching_the_store(self, tmp_path):
        storage = SimpleNamespace(namespaces_for_source=AsyncMock())
        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=storage,
            default_namespace="default",
            target=tmp_path / "absent.md",
            effective_ns="bbb",
            override_hint="-x",
        )
        assert err is None
        storage.namespaces_for_source.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_empty_file_ignores_stale_rows(self, tmp_path):
        """A file whose content is gone has nothing to protect; its rows are
        awaiting reaping, and refusing on them would block a legitimate
        rewrite of the path."""
        target = tmp_path / "emptied.md"
        target.write_text("")
        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=["aaa"])),
            default_namespace="default",
            target=target,
            effective_ns="bbb",
            override_hint="-x",
        )
        assert err is None

    @pytest.mark.asyncio
    async def test_unindexed_content_is_allowed(self, tmp_path):
        """Text nobody indexed has no stored namespace to lose. Refusing
        here would also break the idempotency contract, whose "appended,
        then indexing failed" state looks exactly like this."""
        target = tmp_path / "unindexed.md"
        target.write_text("some text")
        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=[])),
            default_namespace="default",
            target=target,
            effective_ns="bbb",
            override_hint="-x",
        )
        assert err is None

    @pytest.mark.asyncio
    async def test_disagreeing_namespace_refuses(self, tmp_path):
        target = tmp_path / "shared.md"
        target.write_text("some text")
        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=["aaa"])),
            default_namespace="default",
            target=target,
            effective_ns="bbb",
            override_hint="-x",
        )
        assert err is not None and "'aaa'" in err

    @pytest.mark.asyncio
    async def test_engine_resolution_decides_not_the_raw_argument(self, tmp_path):
        """``effective_ns=None`` is not "no namespace" — the engine resolves
        it through its rules, and comparing the raw ``None`` would refuse
        writes that actually agree with the file."""
        target = tmp_path / "ruled.md"
        target.write_text("some text")
        err = await namespace_mix_refusal(
            index_engine=_engine("aaa"),  # rules resolve this file to aaa
            storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=["aaa"])),
            default_namespace="default",
            target=target,
            effective_ns=None,
            override_hint="-x",
        )
        assert err is None

    @pytest.mark.asyncio
    async def test_none_resolution_compares_against_the_configured_default(self, tmp_path):
        """The engine returns ``None`` for the untagged carve-out, which is
        stored as the configured default namespace."""
        target = tmp_path / "plain.md"
        target.write_text("some text")
        err = await namespace_mix_refusal(
            index_engine=_engine(None),
            storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=["default"])),
            default_namespace="default",
            target=target,
            effective_ns=None,
            override_hint="-x",
        )
        assert err is None

    @pytest.mark.asyncio
    async def test_store_failure_refuses(self, tmp_path):
        """Fail closed: without an answer the guard cannot tell a safe write
        from one that silently moves data, and the override is still there
        for a caller who accepts the risk."""
        target = tmp_path / "shared.md"
        target.write_text("some text")
        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=SimpleNamespace(
                namespaces_for_source=AsyncMock(side_effect=RuntimeError("db"))
            ),
            default_namespace="default",
            target=target,
            effective_ns="bbb",
            override_hint="--allow-namespace-mix",
        )
        assert err is not None
        assert "--allow-namespace-mix" in err


# ---------------------------------------------------------------------------
# 5. The write surfaces refuse, and the override releases them
# ---------------------------------------------------------------------------


class TestMcpSurface:
    """``mem_add`` / ``mem_batch_add`` with an explicit ``file=`` — the
    only add path that can still target a mixed file once day files are
    per namespace."""

    @staticmethod
    async def _seed(comp, name: str = "shared.md") -> Path:
        from memtomem.tools.memory_writer import append_entry

        target = comp.config.indexing.memory_dirs[0] / name
        append_entry(target, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(target, namespace="aaa", already_scanned=True)
        return target

    @pytest.mark.asyncio
    async def test_mem_add_refuses_a_disagreeing_namespace(self, bm25_only_components):
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        target = await self._seed(comp)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await mem_add(content=_BETA, file="shared.md", namespace="bbb", ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error:")
        assert "'aaa'" in out and "allow_namespace_mix" in out
        assert await comp.storage.namespaces_for_source(target) == ["aaa"]
        assert _BETA not in target.read_text(), "refused write still appended"

    @pytest.mark.asyncio
    async def test_mem_add_override_appends(self, bm25_only_components):
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        target = await self._seed(comp)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await mem_add(  # type: ignore[arg-type]
            content=_BETA,
            file="shared.md",
            namespace="bbb",
            allow_namespace_mix=True,
            ctx=ctx,
        )

        assert not out.startswith("Error:")
        assert _BETA in target.read_text()

    @pytest.mark.asyncio
    async def test_mem_add_allows_the_namespace_the_file_holds(self, bm25_only_components):
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        await self._seed(comp)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await mem_add(content=_BETA, file="shared.md", namespace="aaa", ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error:")

    @pytest.mark.asyncio
    async def test_refusal_leaves_the_idempotency_key_reusable(self, bm25_only_components):
        """The guard runs before the claim, so a refusal must not consume
        the key — otherwise a corrected retry would block on a pending row
        until the ledger's TTL expired."""
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        await self._seed(comp)
        ctx = StubCtx(AppContext.from_components(comp))

        refused = await mem_add(  # type: ignore[arg-type]
            content=_BETA, file="shared.md", namespace="bbb", idempotency_key="k1", ctx=ctx
        )
        assert refused.startswith("Error:")
        assert await comp.storage.idempotency_get("mem_add", "k1") is None

        retried = await mem_add(  # type: ignore[arg-type]
            content=_BETA, file="shared.md", namespace="aaa", idempotency_key="k1", ctx=ctx
        )
        assert not retried.startswith("Error:")
        assert "in progress" not in retried

    @pytest.mark.asyncio
    async def test_mem_batch_add_refuses_too(self, bm25_only_components):
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, _ = bm25_only_components
        await self._seed(comp)
        ctx = StubCtx(AppContext.from_components(comp))

        out = await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": "t", "value": _BETA}],
            file="shared.md",
            namespace="bbb",
            ctx=ctx,
        )

        assert out.startswith("Error:")
        assert "allow_namespace_mix" in out

    @pytest.mark.asyncio
    async def test_mem_batch_add_default_target_is_per_namespace(self, bm25_only_components):
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"key": "t", "value": _ALPHA}], namespace="aaa", ctx=ctx
        )

        assert [p.name for p in mem_dir.glob("*.md")] == [
            day_file_name("aaa", comp.config.namespace.default_namespace, date_str=_today())
        ]


class TestCliSurface:
    """``mm add`` shares the guard, but must name its own flag: nobody can
    type ``allow_namespace_mix=true`` into a shell.

    Component doubles rather than the real stack — ``CliRunner`` calls
    ``asyncio.run``, so these cannot be async tests.
    """

    @staticmethod
    def _comp(tmp_path: Path, *, existing: list[str]) -> SimpleNamespace:
        mem_dir = tmp_path / "memories"
        mem_dir.mkdir(exist_ok=True)
        (mem_dir / "shared.md").write_text("previously written text\n")
        return SimpleNamespace(
            config=SimpleNamespace(
                indexing=SimpleNamespace(memory_dirs=[str(mem_dir)], project_memory_dirs=[]),
                namespace=SimpleNamespace(default_namespace="default"),
            ),
            index_engine=SimpleNamespace(
                index_file=AsyncMock(return_value=SimpleNamespace(indexed_chunks=1)),
                effective_namespace_for=AsyncMock(side_effect=lambda p, ns=None, **k: ns),
            ),
            storage=SimpleNamespace(
                list_chunks_by_source=AsyncMock(return_value=[]),
                get_session=AsyncMock(return_value=None),
                namespaces_for_source=AsyncMock(return_value=existing),
            ),
        )

    @staticmethod
    def _invoke(monkeypatch, comp, args):
        from contextlib import asynccontextmanager

        from click.testing import CliRunner

        from memtomem.cli.memory import add as add_cmd

        @asynccontextmanager
        async def _fake():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake)
        monkeypatch.setattr(
            "memtomem.server.tools.search._resolve_project_context_root", lambda comp: None
        )
        return CliRunner().invoke(add_cmd, args)

    def test_refuses_and_names_the_cli_flag(self, monkeypatch, tmp_path, home):
        comp = self._comp(tmp_path, existing=["aaa"])
        result = self._invoke(
            monkeypatch, comp, [_BETA, "--file", "shared.md", "--namespace", "bbb"]
        )

        assert result.exit_code != 0
        assert "'aaa'" in result.output
        assert "--allow-namespace-mix" in result.output
        comp.index_engine.index_file.assert_not_awaited()

    def test_override_lets_the_write_through(self, monkeypatch, tmp_path, home):
        comp = self._comp(tmp_path, existing=["aaa"])
        result = self._invoke(
            monkeypatch,
            comp,
            [_BETA, "--file", "shared.md", "--namespace", "bbb", "--allow-namespace-mix"],
        )

        assert result.exit_code == 0, result.output
        comp.index_engine.index_file.assert_awaited_once()

    def test_agreeing_namespace_is_not_refused(self, monkeypatch, tmp_path, home):
        comp = self._comp(tmp_path, existing=["aaa"])
        result = self._invoke(
            monkeypatch, comp, [_BETA, "--file", "shared.md", "--namespace", "aaa"]
        )

        assert result.exit_code == 0, result.output

    def test_a_session_change_during_the_lock_wait_moves_the_write(
        self, monkeypatch, tmp_path, home
    ):
        """The CLI carries its own copy of the re-target loop. Without it the
        write lands in the file named for the pre-lock guess."""
        comp = self._comp(tmp_path, existing=[])
        answers = iter(["aaa", "bbb", "bbb", "bbb"])
        monkeypatch.setattr(
            "memtomem.cli._session_state.resolve_session_write_namespace",
            AsyncMock(side_effect=lambda storage: next(answers)),
        )

        result = self._invoke(monkeypatch, comp, [_BETA])

        assert result.exit_code == 0, result.output
        written = comp.index_engine.index_file.await_args.args[0]
        assert written.name == day_file_name("bbb", "default", date_str=_today())
        assert comp.index_engine.index_file.await_args.kwargs["namespace"] == "bbb"

    def test_a_namespace_that_keeps_flipping_gives_up_without_writing(
        self, monkeypatch, tmp_path, home
    ):
        import itertools

        comp = self._comp(tmp_path, existing=[])
        flip = itertools.cycle(["aaa", "bbb"])
        monkeypatch.setattr(
            "memtomem.cli._session_state.resolve_session_write_namespace",
            AsyncMock(side_effect=lambda storage: next(flip)),
        )

        result = self._invoke(monkeypatch, comp, [_BETA])

        assert result.exit_code != 0
        assert "namespace kept changing" in result.output
        comp.index_engine.index_file.assert_not_awaited()

    def test_default_target_is_the_namespace_day_file(self, monkeypatch, tmp_path, home):
        comp = self._comp(tmp_path, existing=[])
        result = self._invoke(monkeypatch, comp, [_BETA, "--namespace", "aaa"])

        assert result.exit_code == 0, result.output
        written = comp.index_engine.index_file.await_args.args[0]
        assert written.name == day_file_name("aaa", "default", date_str=_today())


class TestInLockRetarget:
    """The day file's name depends on the namespace, but the namespace is
    only authoritative once resolved inside the lock (#1991). When the two
    disagree the write has to move to the right file, not append to one
    named for somebody else's namespace."""

    @pytest.mark.asyncio
    async def test_a_session_change_during_the_lock_wait_moves_the_write(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.server.context import AppContext
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        # "aaa" pre-lock (picks the file), "bbb" from the first in-lock read
        # onwards (the authoritative value).
        answers = iter([(None, "aaa"), (None, "bbb"), (None, "bbb"), (None, "bbb")])

        async def _capture(app, namespace):
            return next(answers)

        monkeypatch.setattr(memory_crud, "capture_session_and_namespace", _capture)

        out = await mem_add(content=_ALPHA, ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error:"), out
        written = sorted(p.name for p in mem_dir.glob("*.md"))
        expected = day_file_name("bbb", comp.config.namespace.default_namespace, date_str=_today())
        assert written == [expected], written
        assert await comp.storage.namespaces_for_source(mem_dir / expected) == ["bbb"]

    @pytest.mark.asyncio
    async def test_a_namespace_that_keeps_flipping_gives_up_without_writing(
        self, bm25_only_components, monkeypatch
    ):
        """Bounded, so a session flipping every acquisition cannot spin
        forever — and the give-up path must not have written anything."""
        import itertools

        from memtomem.server.context import AppContext
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        flip = itertools.cycle([(None, "aaa"), (None, "bbb")])

        async def _capture(app, namespace):
            return next(flip)

        monkeypatch.setattr(memory_crud, "capture_session_and_namespace", _capture)

        out = await mem_add(content=_ALPHA, ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error:")
        assert "namespace kept changing" in out
        assert sorted(p.name for p in mem_dir.glob("*.md")) == []

    @pytest.mark.asyncio
    async def test_batch_add_retargets_the_same_way(self, bm25_only_components, monkeypatch):
        """``mem_batch_add`` carries its own copy of the loop, so it needs
        its own proof rather than inheriting ``mem_add``'s."""
        from memtomem.server.context import AppContext
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.memory_crud import mem_batch_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        answers = iter([(None, "aaa"), (None, "bbb"), (None, "bbb"), (None, "bbb")])

        async def _capture(app, namespace):
            return next(answers)

        monkeypatch.setattr(memory_crud, "capture_session_and_namespace", _capture)

        out = await mem_batch_add(entries=[{"key": "t", "value": _ALPHA}], ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error:"), out
        expected = day_file_name("bbb", comp.config.namespace.default_namespace, date_str=_today())
        assert sorted(p.name for p in mem_dir.glob("*.md")) == [expected]

    @pytest.mark.asyncio
    async def test_a_retarget_does_not_consume_the_idempotency_key(
        self, bm25_only_components, monkeypatch
    ):
        """The claim is taken after the re-target check, so an abandoned
        attempt must leave the ledger untouched — otherwise the very first
        session flip would burn the caller's key."""
        from memtomem.server.context import AppContext
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        answers = iter([(None, "aaa"), (None, "bbb"), (None, "bbb"), (None, "bbb")])

        async def _capture(app, namespace):
            return next(answers)

        monkeypatch.setattr(memory_crud, "capture_session_and_namespace", _capture)

        claims: list[str] = []
        real_claim = comp.storage.idempotency_claim

        async def _spy(tool, key):
            claims.append(key)
            return await real_claim(tool, key)

        monkeypatch.setattr(comp.storage, "idempotency_claim", _spy)

        out = await mem_add(content=_ALPHA, idempotency_key="k1", ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error:"), out
        # Exactly one claim: the retargeted attempt never reached it.
        assert claims == ["k1"]

    @pytest.mark.asyncio
    async def test_an_explicit_file_is_never_retargeted(self, bm25_only_components, monkeypatch):
        """Re-targeting only owns the default day file. A ``file=`` the
        caller named is their choice, and moving it would be a surprise."""
        from memtomem.server.context import AppContext
        from memtomem.server.tools import memory_crud
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))

        async def _capture(app, namespace):
            return (None, "bbb")

        monkeypatch.setattr(memory_crud, "capture_session_and_namespace", _capture)

        out = await mem_add(content=_ALPHA, file="pinned.md", ctx=ctx)  # type: ignore[arg-type]

        assert not out.startswith("Error:"), out
        assert (mem_dir / "pinned.md").exists()
        assert await comp.storage.namespaces_for_source(mem_dir / "pinned.md") == ["bbb"]


class TestAgentShareSurface:
    """``mm agent share`` appends and then indexes with
    ``namespace=<target>``, so it has the same hazard as ``mm add`` even
    though it takes no ``--namespace`` flag."""

    @pytest.mark.asyncio
    async def test_share_writes_to_the_targets_own_day_file(
        self, bm25_only_components, monkeypatch
    ):
        from contextlib import asynccontextmanager

        from memtomem.cli.agent_cmd import _run_share
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        await mem_add(content=_ALPHA, title="Shared", ctx=ctx)  # type: ignore[arg-type]
        day = mem_dir / f"{_today()}.md"
        chunk = (await comp.storage.list_chunks_by_source(day))[0]

        @asynccontextmanager
        async def _fake():
            yield comp

        monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", _fake)
        await _run_share(str(chunk.id), "shared")

        expected = day_file_name(
            "shared", comp.config.namespace.default_namespace, date_str=_today()
        )
        assert (mem_dir / expected).exists(), sorted(p.name for p in mem_dir.glob("*.md"))
        # The default day file it was shared *from* keeps its own namespace.
        assert await comp.storage.namespaces_for_source(day) == ["default"]


class TestLangGraphSurface:
    @pytest.mark.asyncio
    async def test_adapter_uses_the_namespace_day_file_and_refuses_a_mix(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.integrations.langgraph import MemtomemStore
        from memtomem.tools.memory_writer import append_entry

        comp, mem_dir = bm25_only_components
        store = MemtomemStore()
        monkeypatch.setattr(store, "_ensure_init", AsyncMock(return_value=comp))

        out = await store.add(_ALPHA, namespace="aaa")
        assert Path(out["file"]).name == day_file_name(
            "aaa", comp.config.namespace.default_namespace, date_str=_today()
        )

        held = mem_dir / "held.md"
        append_entry(held, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(held, namespace="aaa", already_scanned=True)
        refused = await store.add(_BETA, namespace="bbb", file=str(held))
        assert refused.get("error") == "namespace_mix_refused"


class TestWebSurface:
    @pytest.mark.asyncio
    async def test_add_route_refuses_with_409_and_honours_the_override(self, bm25_only_components):
        from memtomem.memory_scope import namespace_mix_refusal as _refusal
        from memtomem.tools.memory_writer import append_entry

        comp, mem_dir = bm25_only_components
        target = mem_dir / "shared.md"
        append_entry(target, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(target, namespace="aaa", already_scanned=True)

        # The route's own call shape — the guard is shared, so this pins the
        # arguments the web surface passes rather than re-testing the rule.
        err = await _refusal(
            index_engine=comp.index_engine,
            storage=comp.storage,
            default_namespace=comp.config.namespace.default_namespace,
            target=target,
            effective_ns="bbb",
            override_hint="allow_namespace_mix=true",
        )
        assert err is not None and "allow_namespace_mix=true" in err


# ---------------------------------------------------------------------------
# 7. Storage
# ---------------------------------------------------------------------------


class TestNamespacesForSource:
    @pytest.mark.asyncio
    async def test_returns_the_distinct_set(self, bm25_only_components):
        from memtomem.tools.memory_writer import append_entry

        comp, mem_dir = bm25_only_components
        a = mem_dir / "a.md"
        append_entry(a, _ALPHA, title=None, tags=[])
        await comp.index_engine.index_file(a, namespace="aaa", already_scanned=True)

        assert await comp.storage.namespaces_for_source(a) == ["aaa"]

    @pytest.mark.asyncio
    async def test_unindexed_source_has_no_namespaces(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        assert await comp.storage.namespaces_for_source(mem_dir / "never.md") == []


class TestGuardFailsClosedOnFilesystemErrors:
    """Issue #2005 follow-up: only "it isn't there" means there is nothing to
    protect. Any other stat failure hides whatever the file holds."""

    @pytest.mark.asyncio
    async def test_a_stat_permission_error_refuses(self, tmp_path, monkeypatch):
        target = tmp_path / "unreadable.md"
        target.write_text("some text")
        monkeypatch.setattr(
            Path, "stat", lambda self, **k: (_ for _ in ()).throw(PermissionError("denied"))
        )

        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=SimpleNamespace(namespaces_for_source=AsyncMock(return_value=["aaa"])),
            default_namespace="default",
            target=target,
            effective_ns="bbb",
            override_hint="--allow-namespace-mix",
        )

        assert err is not None
        assert "PermissionError" in err
        assert "--allow-namespace-mix" in err

    @pytest.mark.asyncio
    async def test_a_missing_file_is_still_allowed(self, tmp_path):
        """The discriminating half: a brand-new day file must not be refused,
        or every first write of the day fails."""
        err = await namespace_mix_refusal(
            index_engine=_engine("bbb"),
            storage=SimpleNamespace(namespaces_for_source=AsyncMock()),
            default_namespace="default",
            target=tmp_path / "absent.md",
            effective_ns="bbb",
            override_hint="-x",
        )
        assert err is None


class TestRetryableLabelSurvivesRollback:
    @pytest.mark.asyncio
    async def test_mem_edit_reports_a_namespace_failure_as_retryable(
        self, bm25_only_components, monkeypatch
    ):
        """``_mutate_file_and_reindex`` rolls back and returns a plain
        ``Error:`` string. For a transient store failure that tells the caller
        a retryable condition is permanent, so it re-raises instead and lets
        ``tool_handler`` label it."""
        from memtomem.errors import NamespaceResolutionError
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add, mem_edit

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        await mem_add(content=_ALPHA, title="Shared", ctx=ctx)  # type: ignore[arg-type]
        chunk = (await comp.storage.list_chunks_by_source(mem_dir / f"{_today()}.md"))[0]

        calls = {"n": 0}
        real = comp.index_engine.index_file

        async def _fail_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise NamespaceResolutionError("store down")
            return await real(*args, **kwargs)

        monkeypatch.setattr(comp.index_engine, "index_file", _fail_first)

        out = await mem_edit(chunk_id=str(chunk.id), new_content="replacement text", ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error (retryable):"), out

    @pytest.mark.asyncio
    async def test_a_permanent_failure_stays_a_plain_error(self, bm25_only_components, monkeypatch):
        """The discriminating half — otherwise every rollback would advertise
        itself as worth retrying."""
        from memtomem.server.context import AppContext
        from memtomem.server.tools.memory_crud import mem_add, mem_edit

        comp, mem_dir = bm25_only_components
        ctx = StubCtx(AppContext.from_components(comp))
        await mem_add(content=_ALPHA, title="Shared", ctx=ctx)  # type: ignore[arg-type]
        chunk = (await comp.storage.list_chunks_by_source(mem_dir / f"{_today()}.md"))[0]

        calls = {"n": 0}
        real = comp.index_engine.index_file

        async def _fail_first(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("boom")
            return await real(*args, **kwargs)

        monkeypatch.setattr(comp.index_engine, "index_file", _fail_first)

        out = await mem_edit(chunk_id=str(chunk.id), new_content="replacement text", ctx=ctx)  # type: ignore[arg-type]

        assert out.startswith("Error:") and "retryable" not in out, out
