"""Web scratch promote with an explicit ``file`` is gated for the tier it lands in.

Promotion writes to the user tier and runs the write guard for it. A
``project_shared`` tier registered inside a memory directory is another tier:
its files are tracked by git, the guard refuses ``force_unsafe`` for it, and its
writes ask for a confirmation. A file there is refused, as ``/api/add`` and
``mm add --file`` refuse it (#2821).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.context._atomic import memory_lock_path
from test_excluded_target_writes import _rows, _web_client

pytestmark = pytest.mark.asyncio

# Shaped like a credential so the guard flags it; ``force_unsafe`` lets it past
# the guard, which is the case a project_shared tier must not take.
SECRET_LIKE = "api_key=" + "sk-" + "a" * 32


def _tier(comp, root: Path, leaf: str = "memories") -> Path:
    tier = root / ".memtomem" / leaf
    tier.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [
        *comp.config.indexing.project_memory_dirs,
        tier,
    ]
    return tier


async def _promote(comp, key: str, value: str, **body):
    await comp.storage.scratch_set(key, value)
    async with _web_client(comp) as client:
        return await client.post(f"/api/scratch/{key}/promote", json=body)


@pytest.mark.parametrize(
    ("value", "force_unsafe"),
    [("plain note", False), (SECRET_LIKE, True)],
    ids=["clean", "forced-past-the-guard"],
)
async def test_a_file_in_a_nested_shared_tier_is_refused(bm25_only_components, value, force_unsafe):
    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj")
    target = tier / "notes.md"
    rows_before = _rows(comp)

    resp = await _promote(comp, "k", value, file=str(target), force_unsafe=force_unsafe)

    assert resp.status_code == 422, resp.text
    detail = resp.json()["detail"]
    assert "project_shared memory tier, not the user one" in detail
    assert "nothing was written" in detail
    assert not target.exists()
    assert not memory_lock_path(target).exists()
    assert _rows(comp) == rows_before
    assert not (await comp.storage.scratch_get("k"))["promoted"]


async def test_a_file_in_a_nested_local_tier_is_refused(bm25_only_components):
    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj", "memories.local")

    resp = await _promote(comp, "k", "plain note", file=str(tier / "notes.md"))

    assert resp.status_code == 422, resp.text
    assert "project_local memory tier, not the user one" in resp.json()["detail"]
    assert not (tier / "notes.md").exists()


async def test_the_refusal_names_a_route_that_takes_the_tier(bm25_only_components):
    comp, mem_dir = bm25_only_components
    tier = _tier(comp, mem_dir / "proj")

    resp = await _promote(comp, "k", "plain note", file=str(tier / "notes.md"))

    assert (
        "POST /api/add and scope='project_shared' and confirm_project_shared=true"
        in resp.json()["detail"]
    ), resp.text


async def test_a_file_elsewhere_in_the_user_base_still_promotes(bm25_only_components):
    comp, mem_dir = bm25_only_components
    _tier(comp, mem_dir / "proj")
    target = mem_dir / "notes" / "x.md"
    target.parent.mkdir(parents=True)

    resp = await _promote(comp, "k", "plain note", file=str(target))

    assert resp.status_code == 200, resp.text
    assert "plain note" in target.read_text(encoding="utf-8")
    assert (await comp.storage.scratch_get("k"))["promoted"]
    scopes = {c.metadata.scope for c in await comp.storage.list_chunks_by_source(target)}
    assert scopes == {"user"}
