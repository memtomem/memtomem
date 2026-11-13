"""The index gate judges a file under one resolution of its path (#3290).

``IndexEngine._index_file`` took the stored key of the file it read, then
resolved the path again for the decision, and the scope and manifest lookups
resolved it once more each. A leaf replaced by a symlink for the length of one
of those lookups and then put back had its bytes judged under the link target's
scope and stored under its own key; ``_require_same_source`` compares the key
again later and does not see a swap that was undone.

The gate now takes the key and the decision path from one resolution and hands
that path to both lookups as given, and the chunker receives the gate's
projection instead of looking it up a second time.

Each test places the swap with a wrapper around the call it targets. That makes
the timing deterministic; none of this is a live race.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

import memtomem.indexing.engine as engine_mod
import memtomem.indexing.reviewed_projection as reviewed_mod
from memtomem import privacy
from memtomem.indexing.engine import PrivacyRejection
from memtomem.storage.sqlite_helpers import norm_path

# Hits only the label rule a ``documents-patterns`` declaration covers: the file
# is ``exempted`` in user scope and ``blocked_project_shared`` in a project.
_EXEMPT_DOC = (
    "---\nredaction: documents-patterns\n---\n\n## Patterns\n\nCharlieword notes\n"
    "\npassword: hunter2\n"
)
_CLEAN = "## B\n\nBravoword entry about reviews\n"
_MASKED = "## A\n\nAlphaword public text.\n\npassword: hunter2\n"
_MASKED_LINE = 5


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


def _register(comp, root: Path) -> Path:
    pdir = root / ".memtomem" / "memories"
    pdir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [pdir]
    return pdir


def _rows(comp, path: Path) -> list[tuple[str, str]]:
    rows = comp.storage._get_db().execute(
        "SELECT content, scope FROM chunks WHERE source_file=? ORDER BY id", (norm_path(path),)
    )
    return [(row[0], row[1]) for row in rows.fetchall()]


def _swap_during(monkeypatch, owner, name: str, leaf: Path, target: Path, aside: Path) -> list:
    """Make ``leaf`` a link to ``target`` for the first call of ``owner.name`` only.

    The file is moved to ``aside`` and put back when the call returns, so every
    later look at the path finds the original. Returns the list of swaps made,
    for the test to assert the wrapped call was reached.
    """
    real = getattr(owner, name)
    swaps: list[str] = []

    def wrapper(*args, **kwargs):
        if swaps:
            return real(*args, **kwargs)
        swaps.append(name)
        leaf.rename(aside)
        leaf.symlink_to(target)
        try:
            return real(*args, **kwargs)
        finally:
            leaf.unlink()
            aside.rename(leaf)

    monkeypatch.setattr(owner, name, wrapper)
    return swaps


def _mask(comp, tmp_path: Path, path: Path, text: str, line: int) -> None:
    """Write ``text`` to ``path`` and a reviewed manifest that masks its ``line``."""
    # Bytes, so the manifest's digest matches what the indexer reads on Windows too.
    path.write_bytes(text.encode())
    manifest = tmp_path / "mask.json"
    manifest.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    str(path.resolve()): {
                        "source_sha256": hashlib.sha256(text.encode()).hexdigest(),
                        "spans": [[line, line]],
                    }
                },
            }
        )
    )
    engine = comp.index_engine
    engine._config.index_masking_manifest_path = str(manifest)
    masked = engine._prepare_index_projection(path.resolve(), text, "user", resolved=True)
    assert masked.redaction_count == 1, (
        "the manifest entry must apply for the test to mean anything"
    )


def _masked_file_and_manifest(comp, mem_dir: Path, tmp_path: Path) -> tuple[Path, Path]:
    """``masked.md`` whose label line a reviewed manifest masks, and a clean ``other.md``."""
    masked = mem_dir / "masked.md"
    _mask(comp, tmp_path, masked, _MASKED, _MASKED_LINE)
    other = mem_dir / "other.md"
    other.write_text(_CLEAN, encoding="utf-8")
    return masked, other


@pytest.mark.requires_symlinks
async def test_a_leaf_swapped_for_the_scope_lookup_is_still_judged_as_its_own_scope(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    shared = _register(comp, tmp_path / "proj") / "patterns.md"
    shared.write_text(_EXEMPT_DOC, encoding="utf-8")
    target = mem_dir / "target.md"
    target.write_text(_CLEAN, encoding="utf-8")
    swaps = _swap_during(
        monkeypatch, engine_mod, "classify_scope", shared, target, tmp_path / "aside.md"
    )

    # Before #3290 this indexed one chunk under the file's own key with scope
    # ``user``: the lookup resolved the path again, through the link.
    with pytest.raises(PrivacyRejection) as refused:
        await comp.index_engine.index_file(shared)

    assert swaps == ["classify_scope"], "the scope lookup was never reached"
    assert refused.value.decision == "blocked_project_shared"
    assert _rows(comp, shared) == []
    assert not shared.is_symlink() and shared.read_text(encoding="utf-8") == _EXEMPT_DOC


@pytest.mark.requires_symlinks
async def test_a_leaf_swapped_for_the_manifest_lookup_keeps_its_own_entry(
    bm25_only_components, tmp_path, monkeypatch
):
    comp, mem_dir = bm25_only_components
    masked, other = _masked_file_and_manifest(comp, mem_dir, tmp_path)
    swaps = _swap_during(
        monkeypatch,
        reviewed_mod,
        "prepare_reviewed_projection",
        masked,
        other,
        tmp_path / "aside.md",
    )

    # Before #3290 the lookup resolved the path through the link, found no
    # entry for other.md, and the unmasked label line was refused.
    stats = await comp.index_engine.index_file(masked)

    assert swaps == ["prepare_reviewed_projection"], "the manifest lookup was never reached"
    assert stats.indexed_chunks == 1, stats
    rows = _rows(comp, masked)
    assert [scope for _, scope in rows] == ["user"]
    assert all("hunter2" not in content for content, _ in rows)


@pytest.mark.requires_symlinks
async def test_the_stored_text_is_the_gates_projection(bm25_only_components, tmp_path, monkeypatch):
    """A leaf swapped while the file is chunked does not get a second, unmasked lookup."""
    comp, mem_dir = bm25_only_components
    masked, other = _masked_file_and_manifest(comp, mem_dir, tmp_path)
    swaps = _swap_during(
        monkeypatch, comp.index_engine, "chunk_content", masked, other, tmp_path / "aside.md"
    )

    stats = await comp.index_engine.index_file(masked)

    assert swaps == ["chunk_content"], "the chunker was never reached"
    assert stats.indexed_chunks == 1, stats
    rows = _rows(comp, masked)
    assert [scope for _, scope in rows] == ["user"]
    # Before #3290 the chunker looked the manifest entry up again through the
    # link, found none, and the store held the line the gate had passed masked.
    assert all("hunter2" not in content for content, _ in rows)


async def test_a_declared_exemption_is_stored_verbatim_over_a_manifest_entry(
    bm25_only_components, tmp_path
):
    """The gate hands the chunker its projection, and the exemption still wins over it."""
    comp, mem_dir = bm25_only_components
    declared = mem_dir / "declared.md"
    _mask(comp, tmp_path, declared, _EXEMPT_DOC, _EXEMPT_DOC.count("\n"))

    stats = await comp.index_engine.index_file(declared)

    assert stats.indexed_chunks >= 1, stats
    assert any("hunter2" in content for content, _ in _rows(comp, declared))


async def test_a_forced_index_is_stored_verbatim_over_a_manifest_entry(
    bm25_only_components, tmp_path
):
    comp, mem_dir = bm25_only_components
    masked, _ = _masked_file_and_manifest(comp, mem_dir, tmp_path)

    stats = await comp.index_engine.index_file(masked, force_unsafe=True)

    assert stats.indexed_chunks >= 1, stats
    assert any("hunter2" in content for content, _ in _rows(comp, masked))


@pytest.mark.parametrize("where", ["project_shared", "user_with_manifest"])
async def test_the_path_is_resolved_once_between_the_read_and_the_guard(
    bm25_only_components, tmp_path, monkeypatch, where
):
    """The key and every lookup the gate makes come from one resolution of the path."""
    comp, mem_dir = bm25_only_components
    if where == "project_shared":
        leaf = _register(comp, tmp_path / "proj") / "clean.md"
        leaf.write_text(_CLEAN, encoding="utf-8")
    else:
        leaf, _ = _masked_file_and_manifest(comp, mem_dir, tmp_path)
    counting = {"on": False, "resolved": 0, "at_guard": None}
    real_read, real_resolve, real_guard = Path.read_text, Path.resolve, privacy.enforce_write_guard

    def read_text(self, *args, **kwargs):
        if self.name == leaf.name:
            counting.update(on=True, resolved=0)
        return real_read(self, *args, **kwargs)

    def resolve(self, *args, **kwargs):
        if counting["on"] and self.name == leaf.name:
            counting["resolved"] += 1
        return real_resolve(self, *args, **kwargs)

    def guard(*args, **kwargs):
        if counting["on"]:
            counting.update(on=False, at_guard=counting["resolved"])
        return real_guard(*args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(Path, "resolve", resolve)
    monkeypatch.setattr(privacy, "enforce_write_guard", guard)

    stats = await comp.index_engine.index_file(leaf)

    assert stats.indexed_chunks >= 1, stats
    assert counting["at_guard"] == 1
