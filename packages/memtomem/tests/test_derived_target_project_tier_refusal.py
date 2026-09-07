"""#2322 — the surfaces that derive their destination refuse a project tier.

Companion to ``test_require_user_base_tier.py``, which pins the helper. These
pin that the refusal actually *reaches* each surface in its own vocabulary: an
``Error:`` string for MCP tools, a declined archive for the automatic
session-end summary, a 409 for the web route.

The overlap is staged for real — ``memory_dirs[0]`` and ``project_memory_dirs``
both pointing at ``<project>/.memtomem/memories`` — rather than by
monkeypatching the scope resolver, because the misconfiguration *is* the
reachability argument in the issue.
"""

from __future__ import annotations

import pytest

from helpers import StubCtx
from memtomem.server.context import AppContext
from memtomem.server.tools import importers as importer_tools
from memtomem.server.tools import url_index as url_tools


def _overlap(comp, tmp_path):
    """Point ``memory_dirs[0]`` at a registered ``project_shared`` tier."""
    tier = tmp_path / "proj" / ".memtomem" / "memories"
    tier.mkdir(parents=True, exist_ok=True)
    comp.config.indexing.memory_dirs = [tier]
    comp.config.indexing.project_memory_dirs = [tier]
    return tier


def _assert_names_both_fields(out: str) -> None:
    assert out.startswith("Error:"), out
    assert "indexing.memory_dirs[0]" in out
    assert "indexing.project_memory_dirs" in out
    assert "project_shared" in out


@pytest.mark.asyncio
async def test_mem_fetch_refuses_before_it_fetches(bm25_only_components, tmp_path):
    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    ctx = StubCtx(AppContext.from_components(comp))

    out = await url_tools.mem_fetch("https://example.com/page", ctx=ctx)

    _assert_names_both_fields(out)
    assert not (tier / "_fetched").exists()


@pytest.mark.asyncio
async def test_mem_import_notion_refuses(bm25_only_components, tmp_path):
    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    export = tmp_path / "notion-export"
    export.mkdir()
    (export / "note.md").write_text("# A note\n\nharmless\n", encoding="utf-8")
    ctx = StubCtx(AppContext.from_components(comp))

    out = await importer_tools.mem_import_notion(str(export), ctx=ctx)

    _assert_names_both_fields(out)
    assert not (tier / "_imported").exists()


@pytest.mark.asyncio
async def test_mem_import_obsidian_refuses(bm25_only_components, tmp_path):
    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    vault = tmp_path / "vault"
    vault.mkdir()
    (vault / "note.md").write_text("# A note\n\nharmless\n", encoding="utf-8")
    ctx = StubCtx(AppContext.from_components(comp))

    out = await importer_tools.mem_import_obsidian(str(vault), ctx=ctx)

    _assert_names_both_fields(out)
    assert not (tier / "_imported").exists()


@pytest.mark.asyncio
async def test_mem_add_default_target_refuses(bm25_only_components, tmp_path):
    """``mem_add`` with no ``file=`` derives the same base. It *does* have a
    Gate B, but only on an explicit ``scope='project_shared'`` — a default
    ``scope='user'`` call would otherwise land in the tier having asked
    nothing."""
    from memtomem.server.tools import memory_crud

    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    ctx = StubCtx(AppContext.from_components(comp))

    out = await memory_crud.mem_add("a harmless note", ctx=ctx)

    _assert_names_both_fields(out)
    # An implementation that wrote and *then* returned the error would pass
    # on the message alone, having already put the note in the repository.
    assert not list(tier.rglob("*.md"))
    assert (await comp.storage.get_stats())["total_chunks"] == 0


def test_pinned_set_user_scope_refuses_a_project_tier_base(bm25_only_components, tmp_path):
    """Found by widening the derivation guard, not by the issue's list.

    ``PinnedContextStore.set`` gates on the *declared* scope, so a
    ``scope="user"`` block passed both gates and still landed under the
    project tier's ``pinned/`` directory.
    """
    from memtomem.errors import ConfigError
    from memtomem.pinned import PinnedContextStore

    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    store = PinnedContextStore(comp.config)

    with pytest.raises(ConfigError) as exc:
        store.set("block", "harmless content")

    assert "indexing.memory_dirs[0]" in str(exc.value)
    assert not list(tier.rglob("*.md"))


def test_pinned_delete_user_scope_refuses_a_project_tier_base(bm25_only_components, tmp_path):
    """Removing shared bytes is a mutation of the shared tier exactly as
    adding them is — the symmetry ``mem_delete`` already observes.

    The block must survive: a refusal that unlinks first is not a refusal.
    """
    from memtomem.errors import ConfigError
    from memtomem.pinned import PinnedContextStore

    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    victim = tier / "pinned" / "general" / "block.md"
    victim.parent.mkdir(parents=True)
    victim.write_text("---\nid: block\n---\ncommitted content\n", encoding="utf-8")

    store = PinnedContextStore(comp.config)
    with pytest.raises(ConfigError) as exc:
        store.delete("block")

    assert "indexing.memory_dirs[0]" in str(exc.value)
    assert victim.exists(), "the git-tracked block must still be there"
    assert "committed content" in victim.read_text(encoding="utf-8")


def test_pinned_store_still_constructs_and_reads_on_a_project_tier_base(
    bm25_only_components, tmp_path
):
    """Reads stay total (#1768's shape): only the mutations refuse.

    Without this, "refuse the write" and "refuse the store" look identical
    from the test above.

    The tier is prepopulated on purpose. Asserting ``list() == []`` against
    an empty directory proves nothing — an implementation that suppressed
    every user-scope read would satisfy it just as well. The reads have to
    come back with the content.
    """
    from memtomem.pinned import PinnedContextStore

    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    block = tier / "pinned" / "general" / "existing.md"
    block.parent.mkdir(parents=True)
    block.write_text(
        "---\nid: existing\ndescription: prior block\npriority: 3\n---\nremembered text\n",
        encoding="utf-8",
    )

    store = PinnedContextStore(comp.config)

    assert store.user_base == tier.resolve()

    got = store.get("existing")
    assert got is not None
    assert got.content == "remembered text"
    assert got.description == "prior block"

    listed = store.list()
    assert [b.block_id for b in listed] == ["existing"]
    assert listed[0].content == "remembered text"

    # The user tier is still named as an exclusion root — search must keep
    # skipping it even though writes to it are refused.
    assert any(
        tier.resolve() in root.parents or root == tier.resolve() / "pinned"
        for root in store.search_exclusion_roots()
    )


def test_pinned_set_project_shared_with_consent_is_unaffected(bm25_only_components, tmp_path):
    """The refusal is about a *derived* destination, not about the tier: an
    explicit, confirmed ``project_shared`` write still works."""
    from memtomem.pinned import PinnedContextStore

    comp, _ = bm25_only_components
    proj = tmp_path / "proj"
    tier = proj / ".memtomem" / "memories"
    tier.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [tier]

    store = PinnedContextStore(comp.config, project_root=proj)
    block = store.set(
        "block", "harmless content", scope="project_shared", confirm_project_shared=True
    )

    assert block.source_path.is_relative_to(tier)


@pytest.mark.asyncio
async def test_session_end_declines_the_archive_but_keeps_the_summary_on_the_row(
    bm25_only_components, tmp_path, caplog
):
    """A session end has no human to confirm and no flag to grow, so it
    declines the shared archive — and the summary still reaches the row.

    Driven through the real ``mem_session_end`` and asserted against the
    persisted row, not against an internal helper's return value: "the
    summary survives" is a claim about what a later reader can find, and
    ``blocked=False`` from the guard does not establish that anything was
    stored.
    """
    import logging

    from memtomem.server.tools.session import mem_session_end, mem_session_start

    comp, _ = bm25_only_components
    tier = _overlap(comp, tmp_path)
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    await mem_session_start(agent_id="tester", ctx=ctx)
    session_id = app.current_session_id

    with caplog.at_level(logging.WARNING, logger="memtomem.server.tools.session"):
        out = await mem_session_end(summary="an ordinary summary with no secrets", ctx=ctx)

    assert "Session ended" in out
    row = await comp.storage.get_session(session_id)
    assert row["summary"] == "an ordinary summary with no secrets"
    # …and nothing was archived into the git-tracked tier.
    assert not list(tier.rglob("*.md"))
    assert any("session_summary_archive_declined" in r.message for r in caplog.records)


def test_session_summary_guard_declines_the_archive_without_blocking(
    bm25_only_components, tmp_path
):
    """Unit-level companion: the guard returns "not blocked, no payload".

    Kept alongside the end-to-end pin because it distinguishes *declining
    the archive* from *failing the summary closed*, which the row assertion
    above cannot tell apart on its own.
    """
    from memtomem.server.tools.session import _guard_and_prepare_summary

    comp, _ = bm25_only_components
    _overlap(comp, tmp_path)
    app = AppContext.from_components(comp)

    blocked, prepared = _guard_and_prepare_summary(
        app,
        session_id="projecttier",
        agent_id="tester",
        summary="an ordinary summary with no secrets",
        event_counts={"note": 1},
        force_unsafe=False,
    )

    assert blocked is False, "the summary itself is clean; only the archive is declined"
    assert prepared is None, "no archive payload — the destination is the git-tracked tier"


def test_session_summary_still_archives_on_a_user_tier_base(bm25_only_components):
    """The other direction: an ordinary config still prepares the archive, so
    the pin above cannot pass by declining everything."""
    from memtomem.server.tools.session import _guard_and_prepare_summary

    comp, _ = bm25_only_components
    app = AppContext.from_components(comp)

    blocked, prepared = _guard_and_prepare_summary(
        app,
        session_id="usertier",
        agent_id="tester",
        summary="an ordinary summary with no secrets",
        event_counts={"note": 1},
        force_unsafe=False,
    )

    assert blocked is False
    assert prepared is not None
