"""``mm add --tags`` must tag only the entry it appended.

``index_file`` re-chunks the whole target file, and ``mm add`` used to merge
``--tags`` into every chunk ``list_chunks_by_source`` returned for it — so a
second add into an existing day file stamped its tags onto every earlier
entry there. These drive the real ``_add`` against real BM25-only storage and
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
    import memtomem.server.tools.search  # noqa: F401 — the patch target below

    comp, mem_dir = bm25_only_components
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    set_home(monkeypatch, home)

    @asynccontextmanager
    async def fake():
        yield comp

    monkeypatch.setattr("memtomem.cli._bootstrap.cli_components", fake)
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda comp: None
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
        """Pins the state right after the add only. The sub-heading chunks
        carry the tags because ``_add`` merged them in after indexing, and
        the next re-index of the file drops them again (#2747)."""
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
        not. Counting the earlier entry that way placed the boundary 40 lines
        late, past every chunk of the next entry, so none of them got tagged."""
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
