"""``mm add --tags`` must tag only the entry it appended.

``index_file`` re-chunks the whole target file, and ``mm add`` used to merge
``--tags`` into every chunk ``list_chunks_by_source`` returned for it — so a
second add into an existing day file stamped its tags onto every earlier
entry there. Tags now come only from the entry's ``> tags:`` header, read by
the chunker, so what an add stores is what a re-index of the file stores
(#2762). These drive the real ``_add`` against real BM25-only storage and
read the stored tags back per entry.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from helpers import set_home
from memtomem import privacy
from memtomem.cli.memory import _add

_ALPHA = "Alpha entry: the deploy pipeline uses blue-green with a five minute bake"
_BETA = "Beta entry: the on-call rotation hands over every Monday at 09:00 UTC"


@pytest.fixture(autouse=True)
def _reset_privacy_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


@pytest.fixture
def cli_comp(bm25_only_components, monkeypatch, tmp_path):
    """Route ``mm add`` at the real BM25-only stack, outside any project."""
    comp, mem_dir = bm25_only_components
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    set_home(monkeypatch, home)

    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    monkeypatch.setattr(
        "memtomem.runtime.project_context._resolve_project_context_root", lambda comp: None
    )
    return comp, mem_dir


async def _tags_by_entry(comp, target: Path, *needles: str) -> dict[str, set[str]]:
    chunks = await comp.storage.list_chunks_by_source(target)
    found: dict[str, set[str]] = {}
    for needle in needles:
        holders = [c for c in chunks if needle in c.content]
        assert holders, f"no stored chunk holds {needle!r}"
        found[needle] = set().union(*(set(c.metadata.tags) for c in holders))
    return found


async def _only_source(comp) -> Path:
    sources = await comp.storage.get_all_source_files()
    assert len(sources) == 1, sources
    return Path(next(iter(sources)))


class TestCliAddTagIsolation:
    @pytest.mark.asyncio
    async def test_a_later_add_does_not_tag_the_earlier_entry(self, cli_comp):
        comp, _ = cli_comp
        await _add(_ALPHA, "Alpha", ["alpha"], "shared.md")
        await _add(_BETA, "Beta", ["beta"], "shared.md")

        target = await _only_source(comp)
        tags = await _tags_by_entry(comp, target, _ALPHA, _BETA)
        assert tags == {_ALPHA: {"alpha"}, _BETA: {"beta"}}

    @pytest.mark.asyncio
    async def test_an_untagged_add_stays_untagged(self, cli_comp):
        comp, _ = cli_comp
        await _add(_ALPHA, "Alpha", ["alpha"], "shared.md")
        await _add(_BETA, "Beta", [], "shared.md")

        target = await _only_source(comp)
        tags = await _tags_by_entry(comp, target, _ALPHA, _BETA)
        assert tags == {_ALPHA: {"alpha"}, _BETA: set()}

    @pytest.mark.asyncio
    async def test_hand_written_content_keeps_its_tags(self, cli_comp):
        """An entry the user wrote into the file directly is not an ``mm add``
        entry, and a tagged add into that file must leave it untagged."""
        comp, mem_dir = cli_comp
        (mem_dir / "shared.md").write_text(f"## Handwritten\n\n{_ALPHA}\n", encoding="utf-8")
        await _add(_BETA, "Beta", ["beta"], "shared.md")

        target = await _only_source(comp)
        tags = await _tags_by_entry(comp, target, _ALPHA, _BETA)
        assert tags == {_ALPHA: set(), _BETA: {"beta"}}

    @pytest.mark.asyncio
    async def test_every_chunk_of_a_multi_chunk_entry_is_tagged(self, cli_comp):
        """Pins the state right after the add. The survives-a-later-write
        cases are ``test_sub_heading_tags_survive_*`` below."""
        comp, _ = cli_comp
        await _add(_ALPHA, "Alpha", ["alpha"], "shared.md")
        sections = "\n\n".join(
            f"### Part {i}\n\n" + " ".join(f"part{i}-word{j}" for j in range(400)) for i in range(3)
        )
        await _add(sections, "Beta", ["beta"], "shared.md")

        target = await _only_source(comp)
        chunks = await comp.storage.list_chunks_by_source(target)
        parts = [c for c in chunks if "part" in c.content and "-word" in c.content]
        assert len(parts) > 1, "the long entry did not split; the test proves nothing"
        assert all(set(c.metadata.tags) == {"beta"} for c in parts), [
            (c.metadata.start_line, c.metadata.tags) for c in parts
        ]
        tags = await _tags_by_entry(comp, target, _ALPHA)
        assert tags == {_ALPHA: {"alpha"}}

    @pytest.mark.asyncio
    async def test_line_breaks_only_splitlines_sees_do_not_shift_the_boundary(self, cli_comp):
        """``str.splitlines`` also breaks at ``\\x0c``; chunk line numbers do
        not. A tag boundary counted that way once fell 40 lines late, past
        every chunk of the next entry, so none of them got tagged."""
        comp, _ = cli_comp
        await _add(_ALPHA + "\x0c" * 40 + " end", "Alpha", ["alpha"], "shared.md")
        sections = "\n\n".join(
            f"### Part {i}\n\n" + " ".join(f"part{i}-word{j}" for j in range(400)) for i in range(3)
        )
        await _add(sections, "Beta", ["beta"], "shared.md")

        target = await _only_source(comp)
        chunks = await comp.storage.list_chunks_by_source(target)
        parts = [c for c in chunks if "-word" in c.content]
        assert parts
        assert all(set(c.metadata.tags) == {"beta"} for c in parts), [
            (c.metadata.start_line, c.metadata.tags) for c in parts
        ]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("later_tags", [["gamma"], []])
    async def test_sub_heading_tags_survive_a_later_add(self, cli_comp, later_tags):
        """The tags used to live only in the DB patch ``_add`` applied after
        indexing, so the next add to the file re-chunked them away (#2747)."""
        comp, _ = cli_comp
        await _add(_PARTED, "Beta", ["beta"], "shared.md")
        await _add(_ALPHA, "Alpha", later_tags, "shared.md")

        target = await _only_source(comp)
        assert await _part_tags(comp, target) == {"beta"}
        tags = await _tags_by_entry(comp, target, _ALPHA)
        assert tags == {_ALPHA: set(later_tags)}

    @pytest.mark.asyncio
    async def test_sub_heading_tags_survive_a_forced_reindex(self, cli_comp):
        comp, _ = cli_comp
        await _add(_PARTED, "Beta", ["beta"], "shared.md")

        target = await _only_source(comp)
        assert not (await comp.index_engine.index_file(target, force=True)).errors
        assert await _part_tags(comp, target) == {"beta"}


_PARTED = "\n\n".join(
    f"### Part {i}\n\n" + " ".join(f"part{i}-word{j}" for j in range(400)) for i in range(3)
)


async def _part_tags(comp, target: Path) -> set[str]:
    """The one tag set every ``### Part`` chunk carries."""
    chunks = await comp.storage.list_chunks_by_source(target)
    parts = [c for c in chunks if "-word" in c.content]
    assert len(parts) > 1, "the long entry did not split; the test proves nothing"
    tag_sets = {tuple(c.metadata.tags) for c in parts}
    assert len(tag_sets) == 1, [(c.metadata.start_line, c.metadata.tags) for c in parts]
    return set(next(iter(tag_sets)))


_TEMPLATE = f"## Mine\n\n{_BETA}"


class TestCliAddTemplateEntry:
    """Content that opens with its own ``## `` heading (#2762)."""

    @pytest.mark.asyncio
    async def test_is_tagged_and_does_not_leak_into_the_previous_entry(self, cli_comp):
        comp, _ = cli_comp
        await _add(_ALPHA, "Alpha", ["alpha"], "shared.md")
        await _add(_TEMPLATE, None, ["beta"], "shared.md")

        target = await _only_source(comp)
        assert await _tags_by_entry(comp, target, _ALPHA, _BETA) == {
            _ALPHA: {"alpha"},
            _BETA: {"beta"},
        }
        (alpha,) = [
            c for c in await comp.storage.list_chunks_by_source(target) if _ALPHA in c.content
        ]
        assert alpha.content == _ALPHA

    @pytest.mark.asyncio
    async def test_tags_survive_a_forced_reindex(self, cli_comp):
        """The add used to look right only because a post-index DB patch
        tagged the entry; re-indexing the file dropped the tags."""
        comp, _ = cli_comp
        await _add(_ALPHA, "Alpha", ["alpha"], "shared.md")
        await _add(_TEMPLATE, None, ["beta"], "shared.md")

        target = await _only_source(comp)
        assert not (await comp.index_engine.index_file(target, force=True)).errors
        assert await _tags_by_entry(comp, target, _ALPHA, _BETA) == {
            _ALPHA: {"alpha"},
            _BETA: {"beta"},
        }

    @pytest.mark.asyncio
    async def test_a_second_top_level_section_is_not_the_entrys(self, cli_comp):
        """A further ``## `` heading inside one add's content closes the entry,
        so its section carries no tags: the same right after the add as after a
        re-index, and the same as MCP ``mem_add``."""
        comp, _ = cli_comp
        other = "Other section: the staging cluster drains at 18:00 UTC"
        await _add(f"{_TEMPLATE}\n\n## Other\n\n{other}", None, ["beta"], "shared.md")

        target = await _only_source(comp)
        assert await _tags_by_entry(comp, target, _BETA, other) == {_BETA: {"beta"}, other: set()}


@pytest.mark.asyncio
async def test_a_non_markdown_target_is_not_tagged(cli_comp):
    """Only the markdown chunker reads the ``> tags:`` header, so an add into a
    ``.rst`` file stores no tags — as MCP ``mem_add`` and a re-index do."""
    comp, _ = cli_comp
    await _add(_BETA, "Beta", ["beta"], "notes.rst")

    target = await _only_source(comp)
    assert target.suffix == ".rst"
    assert await _tags_by_entry(comp, target, _BETA) == {_BETA: set()}
