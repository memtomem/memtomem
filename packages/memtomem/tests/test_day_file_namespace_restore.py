"""A rebuilt index restores a day file's namespace from its name (ADR-0043, #3051).

``day_file_name`` writes ``{date}--{slug}-{sha256(ns)[:16]}.md``. The slug is
lossy, but for the namespaces memtomem produces the only lost character is the
``:`` between segments, so the slug plus the digest give the namespace back.
These tests pin the decoder against the writer, the engine's precedence for a
source with no stored rows, and the mix guard that keeps names truthful.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from memtomem.memory_scope import (
    day_file_declaration,
    day_file_name,
    decode_day_file_namespace,
    namespace_mix_refusal,
)

DATE = "2026-11-04"


def _name(namespace: str) -> str:
    return day_file_name(namespace, "default", date_str=DATE)


class TestDecoder:
    @pytest.mark.parametrize(
        "namespace",
        [
            "shared:repo-demo",
            # The handoff workflow trims only leading ``-`` from its slug.
            "shared:repo-copy-",
            "agent-runtime:planner",
            "agent-runtime:planner-",
            # A rule-derived Claude project namespace: many literal hyphens.
            "claude:-Users-dev-Work-agent-harness-memtomem",
            "team:alpha:beta",
            "a:b",
            "a-b",
        ],
    )
    def test_round_trips_what_the_writer_names(self, namespace: str) -> None:
        assert decode_day_file_namespace(_name(namespace)) == namespace

    def test_a_shared_slug_still_decodes_each_namespace_to_itself(self) -> None:
        # ``a:b`` and ``a-b`` share the slug ``a-b``; the digest separates them.
        assert _name("a:b") != _name("a-b")
        assert decode_day_file_namespace(_name("a:b")) == "a:b"
        assert decode_day_file_namespace(_name("a-b")) == "a-b"

    @pytest.mark.parametrize(
        "name",
        [
            f"{DATE}.md",  # the default namespace's day file
            "notes.md",
            f"{DATE}--0123456789abcdef.md",  # digest-only: no candidate
            f"{DATE}--shared-repo-demo-0123456789abcdef.md",  # digest matches nothing
            f"{DATE}--shared-repo-demo-9E5D158E8745CA45.md",  # not the writer's hex
        ],
    )
    def test_names_the_writer_did_not_produce_declare_nothing(self, name: str) -> None:
        assert decode_day_file_namespace(name) is None

    def test_a_truncated_slug_declares_nothing(self) -> None:
        namespace = "shared:" + "x" * 80
        assert decode_day_file_namespace(_name(namespace)) is None

    def test_a_namespace_with_other_unsafe_characters_declares_nothing(self) -> None:
        assert decode_day_file_namespace(_name("shared:my project")) is None

    def test_the_search_gives_up_at_its_budget(self) -> None:
        # Nineteen hyphens, ten of which were colons: past 2^16 candidates.
        namespace = "a-b:c-d:e-f:g-h:i-j:k-l:m-n:o-p:q-r:s:t"
        assert decode_day_file_namespace(_name(namespace)) is None


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link)
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


class TestDeclarationReadsTheResolvedName:
    """The indexer decides on the resolved path while discovery keeps link
    names, so the declaration must come from where the link points."""

    def test_a_day_file_shaped_link_to_a_plain_file_declares_nothing(self, tmp_path) -> None:
        target = tmp_path / "notes.md"
        target.write_text("x", encoding="utf-8")
        link = tmp_path / _name("shared:repo-demo")
        _symlink_or_skip(link, target)

        assert day_file_declaration(link) == (None, False)

    def test_a_plain_link_to_a_day_file_declares_its_namespace(self, tmp_path) -> None:
        target = tmp_path / _name("shared:repo-demo")
        target.write_text("x", encoding="utf-8")
        link = tmp_path / "notes.md"
        _symlink_or_skip(link, target)

        assert day_file_declaration(link) == ("shared:repo-demo", True)

    def test_an_undecodable_day_file_shaped_name_is_reported_as_such(self, tmp_path) -> None:
        fp = tmp_path / f"{DATE}--shared-repo-demo-0123456789abcdef.md"
        assert day_file_declaration(fp) == (None, True)


def _write_day_file(memory_dir: Path, namespace: str, body: str = "entry") -> Path:
    fp = memory_dir / _name(namespace)
    fp.write_text(f"## Note\n\n> created: {DATE}T10:00:00+00:00\n\n{body}\n", encoding="utf-8")
    return fp


class TestEngineRestoresFromTheName:
    async def test_a_rebuild_puts_a_day_file_back_in_its_namespace(
        self, components, memory_dir
    ) -> None:
        """The #3051 headline: no stored rows, stock configuration."""
        fp = _write_day_file(memory_dir, "shared:repo-demo")

        stats = await components.index_engine.index_path(memory_dir, recursive=True)

        assert await components.storage.namespaces_for_source(fp) == ["shared:repo-demo"]
        assert stats.namespaces_restored_from_name == 1
        assert stats.day_file_names_undecoded == 0

    async def test_the_name_outranks_a_session_binding(self, components, memory_dir) -> None:
        """``mem_index`` in a bound MCP session passes the binding as
        ``new_source_namespace``; a rebuild under it must not claim the tree."""
        fp = _write_day_file(memory_dir, "shared:repo-demo")
        fresh = memory_dir / "fresh.md"
        fresh.write_text("# F\n\nwritten this session", encoding="utf-8")

        await components.index_engine.index_path(
            memory_dir, recursive=True, new_source_namespace="agent-runtime:planner"
        )

        assert await components.storage.namespaces_for_source(fp) == ["shared:repo-demo"]
        assert await components.storage.namespaces_for_source(fresh) == ["agent-runtime:planner"]

    async def test_stored_rows_outrank_the_name(self, components, memory_dir) -> None:
        """A file whose rows were moved keeps the move while the rows exist."""
        engine = components.index_engine
        fp = _write_day_file(memory_dir, "shared:repo-demo")
        await engine.index_file(fp, namespace="shared:moved", ingress_guarded=True)

        stats = await engine.index_path(memory_dir, recursive=True, force=True)

        assert await components.storage.namespaces_for_source(fp) == ["shared:moved"]
        assert stats.namespaces_restored_from_name == 0

    async def test_an_explicit_namespace_outranks_the_name(self, components, memory_dir) -> None:
        fp = _write_day_file(memory_dir, "shared:repo-demo")

        await components.index_engine.index_path(memory_dir, recursive=True, namespace="pinned")

        assert await components.storage.namespaces_for_source(fp) == ["pinned"]

    async def test_reassignment_still_means_the_rules(self, components, memory_dir) -> None:
        fp = _write_day_file(memory_dir, "shared:repo-demo")

        await components.index_engine.index_path(
            memory_dir, recursive=True, reassign_namespaces=True
        )

        assert await components.storage.namespaces_for_source(fp) == ["default"]

    async def test_an_undecodable_name_falls_back_and_is_counted(
        self, components, memory_dir
    ) -> None:
        fp = memory_dir / f"{DATE}--shared-repo-demo-0123456789abcdef.md"
        fp.write_text("## Note\n\nentry\n", encoding="utf-8")

        stats = await components.index_engine.index_path(
            memory_dir, recursive=True, new_source_namespace="agent-runtime:planner"
        )

        assert await components.storage.namespaces_for_source(fp) == ["agent-runtime:planner"]
        assert stats.day_file_names_undecoded == 1
        assert stats.namespaces_restored_from_name == 0

    async def test_the_preview_answers_what_the_write_stamps(self, components, memory_dir) -> None:
        """Preview and prepass read the same decision as the in-lock write."""
        fp = _write_day_file(memory_dir, "shared:repo-demo")
        engine = components.index_engine

        preview = await engine.resolve_namespaces_for([fp])
        await engine.index_path(memory_dir, recursive=True)

        assert preview == ["shared:repo-demo"]
        assert await components.storage.namespaces_for_source(fp) == ["shared:repo-demo"]

    async def test_a_single_file_index_reports_the_restore(self, components, memory_dir) -> None:
        fp = _write_day_file(memory_dir, "shared:repo-demo")

        stats = await components.index_engine.index_file(fp)

        assert await components.storage.namespaces_for_source(fp) == ["shared:repo-demo"]
        assert stats.namespaces_restored_from_name == 1

    async def test_the_stream_reports_the_restore(self, components, memory_dir) -> None:
        _write_day_file(memory_dir, "shared:repo-demo")
        undecoded = memory_dir / f"{DATE}--shared-x-0123456789abcdef.md"
        undecoded.write_text("## Note\n\nentry\n", encoding="utf-8")

        complete = None
        async for event in components.index_engine.index_path_stream(memory_dir, recursive=True):
            if event.get("type") == "complete":
                complete = event
        assert complete is not None
        assert complete["namespaces_restored_from_name"] == 1
        assert complete["day_file_names_undecoded"] == 1


class TestMixGuardKeepsNamesTruthful:
    """ADR-0043 §3: a name declares its namespace, so a write under another
    one would move after a rebuild. Refused like any other mix."""

    async def test_a_write_under_another_namespace_is_refused_even_into_an_empty_file(
        self, components, memory_dir
    ) -> None:
        target = memory_dir / _name("shared:repo-demo")

        err = await namespace_mix_refusal(
            index_engine=components.index_engine,
            storage=components.storage,
            target=target,
            effective_ns="shared:other",
            override_hint="allow_namespace_mix=true",
        )

        assert err is not None
        assert "'shared:repo-demo'" in err and "'shared:other'" in err
        assert "allow_namespace_mix=true" in err

    async def test_a_write_under_the_declared_namespace_passes(
        self, components, memory_dir
    ) -> None:
        target = _write_day_file(memory_dir, "shared:repo-demo")

        err = await namespace_mix_refusal(
            index_engine=components.index_engine,
            storage=components.storage,
            target=target,
            effective_ns="shared:repo-demo",
            override_hint="allow_namespace_mix=true",
        )

        assert err is None

    async def test_an_unbound_write_resolves_to_the_name_and_passes(
        self, components, memory_dir
    ) -> None:
        target = _write_day_file(memory_dir, "shared:repo-demo")

        err = await namespace_mix_refusal(
            index_engine=components.index_engine,
            storage=components.storage,
            target=target,
            effective_ns=None,
            override_hint="allow_namespace_mix=true",
        )

        assert err is None


class TestMemAddIntoADayFileShapedFile:
    """The write contract end to end through ``mem_add(file=...)``: session
    resolution, the refusal before the append, the override, and the index."""

    @staticmethod
    def _ctx(comp):
        from helpers import StubCtx
        from memtomem.server.context import AppContext

        return StubCtx(AppContext.from_components(comp))

    async def test_an_unbound_write_lands_in_the_declared_namespace(
        self, bm25_only_components
    ) -> None:
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        name = _name("shared:repo-demo")

        out = await mem_add(content="handoff body", file=name, ctx=self._ctx(comp))  # type: ignore[arg-type]

        assert not out.startswith("Error:"), out
        assert await comp.storage.namespaces_for_source(mem_dir / name) == ["shared:repo-demo"]

    async def test_a_write_under_another_namespace_is_refused_and_writes_nothing(
        self, bm25_only_components
    ) -> None:
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        name = _name("shared:repo-demo")

        out = await mem_add(  # type: ignore[arg-type]
            content="misrouted body", file=name, namespace="shared:other", ctx=self._ctx(comp)
        )

        assert out.startswith("Error:"), out
        assert "'shared:repo-demo'" in out and "allow_namespace_mix=true" in out
        assert not (mem_dir / name).exists()

    async def test_the_override_writes_under_the_named_namespace(
        self, bm25_only_components
    ) -> None:
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        name = _name("shared:repo-demo")

        out = await mem_add(  # type: ignore[arg-type]
            content="deliberate body",
            file=name,
            namespace="shared:other",
            allow_namespace_mix=True,
            ctx=self._ctx(comp),
        )

        assert not out.startswith("Error:"), out
        assert await comp.storage.namespaces_for_source(mem_dir / name) == ["shared:other"]

    async def test_a_default_target_add_round_trips_through_a_rebuild(
        self, bm25_only_components
    ) -> None:
        """The #3051 repro through the tool: add, lose the rows, re-index."""
        from memtomem.server.tools.memory_crud import mem_add

        comp, mem_dir = bm25_only_components
        out = await mem_add(  # type: ignore[arg-type]
            content="handoff body", namespace="shared:repo-demo", ctx=self._ctx(comp)
        )
        assert not out.startswith("Error:"), out
        (written,) = list(mem_dir.glob("*.md"))
        await comp.storage.delete_by_source(written)
        assert await comp.storage.namespaces_for_source(written) == []

        stats = await comp.index_engine.index_path(mem_dir, recursive=True)

        assert await comp.storage.namespaces_for_source(written) == ["shared:repo-demo"]
        assert stats.namespaces_restored_from_name == 1


class TestTallyCountsCommittedWritesOnly:
    """Both counters say what was indexed, so a file whose write did not commit
    (an embedding failure before the upsert) counts in neither."""

    @pytest.mark.parametrize(
        ("decision_kwargs", "field"),
        [
            ({"target": "shared:x", "reason": "declared_by_name"}, "restored_from_name"),
            ({"target": None, "reason": "resolved", "undecoded_name": True}, "undecoded_names"),
        ],
    )
    @pytest.mark.parametrize("written", [False, True])
    def test_only_a_written_file_is_counted(self, decision_kwargs, field, written) -> None:
        from memtomem.indexing.namespace_resolver import NamespaceDecision, _NamespaceTally

        tally = _NamespaceTally()
        tally.record(
            NamespaceDecision(**decision_kwargs),
            written=written,
            canonical=lambda ns: ns or "default",
        )

        assert getattr(tally, field) == (1 if written else 0)
