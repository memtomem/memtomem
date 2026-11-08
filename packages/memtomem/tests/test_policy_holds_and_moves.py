"""Lifecycle policies keep held sources and whole files intact (#3110).

Four defects, each pinned here against the handlers that had them:

- ``auto_archive`` / ``auto_promote`` wrote with raw ``executemany`` on the
  writer outside ``transaction()``: a move onto a row the target already held
  raised ``IntegrityError`` and left the shared connection inside a
  transaction. They now plan and write under one write lock and refuse a file
  whose move would collide.
- ``auto_expire``, ``auto_archive``, ``auto_promote`` and decay's
  ``expire_chunks`` read held and pending sources as ordinary candidates, so
  expiry deleted rows a hold retains for recovery.
- ``auto_archive`` / ``auto_promote`` moved single chunks, leaving a file in
  two namespaces, which a forced re-index then refuses. A file now moves whole.
- ``auto_consolidate`` read 20 rows per source, so the mixed-namespace check
  and the source hash never saw the rest of a larger file.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from memtomem.models import CONSOLIDATED_SUFFIX, ORIGIN_CONSOLIDATION_POLICY, Chunk, ChunkMetadata
from memtomem.search.decay import expire_chunks
from memtomem.storage.sqlite_helpers import norm_path
from memtomem.tools import policy_engine
from memtomem.tools.policy_engine import (
    execute_auto_archive,
    execute_auto_consolidate,
    execute_auto_expire,
    execute_auto_promote,
)
from test_hold_write_gates import _hold_from_another_connection

pytestmark = pytest.mark.asyncio


def _chunk(
    text: str,
    source: Path,
    line: int,
    *,
    namespace: str = "default",
    tags: tuple[str, ...] = (),
    origin: str | None = None,
) -> Chunk:
    return Chunk(
        content=text,
        metadata=ChunkMetadata(
            source_file=source,
            start_line=line,
            end_line=line,
            namespace=namespace,
            tags=tags,
            origin=origin,
        ),
        embedding=[0.1] * 1024,
    )


def _age(storage, chunks, *, days: int = 200, access_count: int | None = None) -> None:
    stamp = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    db = storage._get_db()
    for c in chunks:
        db.execute(
            "UPDATE chunks SET created_at=?, updated_at=? WHERE id=?", (stamp, stamp, str(c.id))
        )
        if access_count is not None:
            db.execute("UPDATE chunks SET access_count=? WHERE id=?", (access_count, str(c.id)))
    db.commit()


def _namespaces(storage, source: Path) -> list[str]:
    rows = storage._get_db().execute(
        "SELECT DISTINCT namespace FROM chunks WHERE source_file=? ORDER BY namespace",
        (norm_path(source),),
    )
    return [r[0] for r in rows.fetchall()]


def _count(storage, source: Path) -> int:
    row = storage._get_db().execute(
        "SELECT COUNT(*) FROM chunks WHERE source_file=?", (norm_path(source),)
    )
    return int(row.fetchone()[0])


def _pend(storage, source: Path) -> None:
    db = storage._get_db()
    db.execute(
        "INSERT INTO pending_source_checks(source_file, queued_at) VALUES (?, ?)",
        (norm_path(source), datetime.now(timezone.utc).isoformat(timespec="seconds")),
    )
    db.commit()


# ---------------------------------------------------------------------------
# Collisions: transactional, refused per file, nothing left open
# ---------------------------------------------------------------------------


class TestMoveCollisions:
    async def test_archive_onto_a_duplicate_refuses_that_file_only(self, storage, tmp_path):
        dup = tmp_path / "dup.md"
        clean = tmp_path / "clean.md"
        live = _chunk("same text", dup, 1)
        archived = _chunk("same text", dup, 1, namespace="archive")
        other = _chunk("other text", clean, 1)
        await storage.upsert_chunks([live])
        await storage.upsert_chunks([archived])
        await storage.upsert_chunks([other])
        _age(storage, [live, archived, other])

        result = await execute_auto_archive(storage, {"max_age_days": 30}, None, dry_run=False)

        assert not storage._get_db().in_transaction
        assert result.outcome["conflicts"] == [
            {"source": norm_path(dup), "target": "archive", "count": 1}
        ]
        assert result.error is not None and "collide" in result.error
        assert _namespaces(storage, dup) == ["archive", "default"]  # untouched
        assert _namespaces(storage, clean) == ["archive"]
        assert result.affected_count == 1

    async def test_dry_run_reports_the_same_conflict(self, storage, tmp_path):
        dup = tmp_path / "dup.md"
        live = _chunk("same text", dup, 1)
        archived = _chunk("same text", dup, 1, namespace="archive")
        await storage.upsert_chunks([live])
        await storage.upsert_chunks([archived])
        _age(storage, [live, archived])

        result = await execute_auto_archive(storage, {"max_age_days": 30}, None, dry_run=True)

        assert result.affected_count == 0
        assert [c["source"] for c in result.outcome["conflicts"]] == [norm_path(dup)]

    async def test_promote_refuses_two_moving_rows_that_collide(self, storage, tmp_path):
        # Neither row is in the target yet; they collide with each other.
        dup = tmp_path / "dup.md"
        clean = tmp_path / "clean.md"
        a = _chunk("same text", dup, 1, namespace="archive:a")
        b = _chunk("same text", dup, 1, namespace="archive:b")
        other = _chunk("other text", clean, 1, namespace="archive:a")
        await storage.upsert_chunks([a])
        await storage.upsert_chunks([b])
        await storage.upsert_chunks([other])
        _age(storage, [a, b, other], access_count=5)

        result = await execute_auto_promote(storage, {}, None, dry_run=False)

        assert not storage._get_db().in_transaction
        assert [c["source"] for c in result.outcome["conflicts"]] == [norm_path(dup)]
        assert result.error is not None
        assert _namespaces(storage, dup) == ["archive:a", "archive:b"]
        assert _namespaces(storage, clean) == ["default"]
        assert result.outcome["promoted_ids"] == [str(other.id)]


# ---------------------------------------------------------------------------
# Holds: never a candidate
# ---------------------------------------------------------------------------


@pytest.fixture(params=["missing", "redaction_blocked", "pending"])
def hide(request):
    async def apply(storage, source: Path) -> None:
        if request.param == "pending":
            _pend(storage, source)
        else:
            await storage.hold_source(source, request.param)

    return apply


class TestHeldSourcesAreNotCandidates:
    async def test_auto_expire(self, storage, tmp_path, hide):
        held = tmp_path / "held.md"
        seen = tmp_path / "seen.md"
        h, s = _chunk("held", held, 1), _chunk("seen", seen, 1)
        await storage.upsert_chunks([h, s])
        _age(storage, [h, s], access_count=0)
        await hide(storage, held)

        result = await execute_auto_expire(storage, {"max_age_days": 30}, None, dry_run=False)

        assert result.outcome["deleted_ids"] == [str(s.id)]
        assert _count(storage, held) == 1
        assert _count(storage, seen) == 0

    async def test_decay_expire_chunks(self, storage, tmp_path, hide):
        held = tmp_path / "held.md"
        seen = tmp_path / "seen.md"
        hs = [_chunk(f"held {i}", held, i + 1) for i in range(3)]
        s = _chunk("seen", seen, 1)
        await storage.upsert_chunks([*hs, s])
        _age(storage, [*hs, s])
        await hide(storage, held)

        preview = await expire_chunks(storage, max_age_days=30, dry_run=True)
        stats = await expire_chunks(storage, max_age_days=30, dry_run=False)

        assert (preview.total_chunks, preview.expired_chunks) == (1, 1)
        assert (stats.expired_chunks, stats.deleted_chunks) == (1, 1)
        assert _count(storage, held) == 3
        assert _count(storage, seen) == 0

    async def test_policy_summary_of_a_held_parent_is_kept(self, storage, tmp_path):
        parent = tmp_path / "parent.md"
        summary_path = Path(f"{parent}{CONSOLIDATED_SUFFIX}")
        p = _chunk("parent", parent, 1)
        summary = _chunk("summary", summary_path, 1, origin=ORIGIN_CONSOLIDATION_POLICY)
        await storage.upsert_chunks([p, summary])
        _age(storage, [p, summary], access_count=0)
        await storage.hold_source(parent, "missing")

        await execute_auto_expire(storage, {"max_age_days": 30}, None, dry_run=False)
        await expire_chunks(storage, max_age_days=30, dry_run=False)

        assert _count(storage, summary_path) == 1
        assert _count(storage, parent) == 1

    async def test_auto_archive(self, storage, tmp_path, hide):
        held = tmp_path / "held.md"
        h = _chunk("held", held, 1)
        await storage.upsert_chunks([h])
        _age(storage, [h])
        await hide(storage, held)

        result = await execute_auto_archive(storage, {"max_age_days": 30}, None, dry_run=False)

        assert result.affected_count == 0
        assert _namespaces(storage, held) == ["default"]

    async def test_auto_promote(self, storage, tmp_path, hide):
        held = tmp_path / "held.md"
        h = _chunk("held", held, 1, namespace="archive")
        await storage.upsert_chunks([h])
        _age(storage, [h], access_count=9)
        await hide(storage, held)

        result = await execute_auto_promote(storage, {}, None, dry_run=False)

        assert result.affected_count == 0
        assert _namespaces(storage, held) == ["archive"]


class TestHoldCommittedDuringTheRun:
    """A hold from another connection cannot land between the scan and the write."""

    async def test_auto_expire(self, components, tmp_path, monkeypatch):
        storage = components.storage
        src = tmp_path / "late.md"
        c = _chunk("late", src, 1)
        await storage.upsert_chunks([c])
        _age(storage, [c], access_count=0)
        outcomes: list[str | None] = []
        real = storage.delete_chunks

        async def delete_after_hold(*args, **kwargs):
            outcomes.append(
                _hold_from_another_connection(components.config.storage.sqlite_path, src)
            )
            return await real(*args, **kwargs)

        monkeypatch.setattr(storage, "delete_chunks", delete_after_hold)

        await execute_auto_expire(storage, {"max_age_days": 30}, None, dry_run=False)

        assert len(outcomes) == 1, "the hold was never attempted"
        assert outcomes[0] is not None and "locked" in outcomes[0]

    async def test_decay_expire_chunks(self, components, tmp_path, monkeypatch):
        storage = components.storage
        src = tmp_path / "late.md"
        c = _chunk("late", src, 1)
        await storage.upsert_chunks([c])
        _age(storage, [c])
        outcomes: list[str | None] = []
        real = storage.delete_chunks

        async def delete_after_hold(*args, **kwargs):
            outcomes.append(
                _hold_from_another_connection(components.config.storage.sqlite_path, src)
            )
            return await real(*args, **kwargs)

        monkeypatch.setattr(storage, "delete_chunks", delete_after_hold)

        await expire_chunks(storage, max_age_days=30, dry_run=False)

        assert len(outcomes) == 1, "the hold was never attempted"
        assert outcomes[0] is not None and "locked" in outcomes[0]

    async def test_auto_archive(self, components, tmp_path, monkeypatch):
        storage = components.storage
        src = tmp_path / "late.md"
        c = _chunk("late", src, 1)
        await storage.upsert_chunks([c])
        _age(storage, [c])
        outcomes: list[str | None] = []
        real = policy_engine._colliding_rows

        def plan_then_hold(rows):
            outcomes.append(
                _hold_from_another_connection(components.config.storage.sqlite_path, src)
            )
            return real(rows)

        monkeypatch.setattr(policy_engine, "_colliding_rows", plan_then_hold)

        await execute_auto_archive(storage, {"max_age_days": 30}, None, dry_run=False)

        assert len(outcomes) == 1, "the hold was never attempted"
        assert outcomes[0] is not None and "locked" in outcomes[0]


# ---------------------------------------------------------------------------
# Whole-file moves
# ---------------------------------------------------------------------------


class TestWholeFileMoves:
    async def test_archive_waits_until_every_chunk_is_old(self, storage, tmp_path):
        src = tmp_path / "part.md"
        cs = [_chunk(f"part {i}", src, i + 1) for i in range(3)]
        await storage.upsert_chunks(cs)
        _age(storage, cs[:1])

        partial = await execute_auto_archive(
            storage, {"max_age_days": 30, "archive_namespace": "cold"}, None, dry_run=False
        )
        assert partial.affected_count == 0
        assert _namespaces(storage, src) == ["default"]

        _age(storage, cs)
        whole = await execute_auto_archive(
            storage, {"max_age_days": 30, "archive_namespace": "cold"}, None, dry_run=False
        )
        assert whole.affected_count == 3
        assert _namespaces(storage, src) == ["cold"]

    async def test_archive_heals_a_file_an_earlier_run_split(self, storage, tmp_path):
        src = tmp_path / "split.md"
        moved = _chunk("moved", src, 1, namespace="cold")
        left = _chunk("left", src, 2)
        await storage.upsert_chunks([moved, left])
        _age(storage, [moved, left])

        result = await execute_auto_archive(
            storage, {"max_age_days": 30, "archive_namespace": "cold"}, None, dry_run=False
        )

        assert result.outcome["moved"] == [{"target": "cold", "ids": [str(left.id)], "omitted": 0}]
        assert _namespaces(storage, src) == ["cold"]

    async def test_archive_namespace_filter_needs_the_whole_file(self, storage, tmp_path):
        src = tmp_path / "two.md"
        w = _chunk("work", src, 1, namespace="work")
        p = _chunk("personal", src, 2, namespace="personal")
        await storage.upsert_chunks([w, p])
        _age(storage, [w, p])

        result = await execute_auto_archive(storage, {"max_age_days": 30}, "work", dry_run=False)

        assert result.affected_count == 0
        assert _namespaces(storage, src) == ["personal", "work"]

    async def test_template_split_targets_are_reported_not_moved(self, storage, tmp_path):
        src = tmp_path / "tags.md"
        a = _chunk("a", src, 1, tags=("decisions",))
        b = _chunk("b", src, 2, tags=("tech",))
        await storage.upsert_chunks([a, b])
        _age(storage, [a, b])

        result = await execute_auto_archive(
            storage,
            {"max_age_days": 30, "archive_namespace_template": "archive:{first_tag}"},
            None,
            dry_run=False,
        )

        assert result.affected_count == 0
        assert result.error is None
        assert result.outcome["skipped"] == [{"source": norm_path(src), "reason": "split_targets"}]
        assert _namespaces(storage, src) == ["default"]

    async def test_promote_brings_the_whole_file_back_on_one_hot_chunk(self, storage, tmp_path):
        src = tmp_path / "back.md"
        hot = _chunk("hot", src, 1, namespace="archive")
        cold = _chunk("cold", src, 2, namespace="archive")
        await storage.upsert_chunks([hot, cold])
        _age(storage, [hot], access_count=5)
        _age(storage, [cold], access_count=0)

        result = await execute_auto_promote(storage, {}, None, dry_run=False)

        assert sorted(result.outcome["promoted_ids"]) == sorted([str(hot.id), str(cold.id)])
        assert _namespaces(storage, src) == ["default"]
        rows = storage._get_db().execute(
            "SELECT last_accessed_at FROM chunks WHERE source_file=?", (norm_path(src),)
        )
        assert all(r[0] is not None for r in rows.fetchall())

    async def test_promote_leaves_a_file_with_rows_outside_its_sources(self, storage, tmp_path):
        src = tmp_path / "mixed.md"
        hot = _chunk("hot", src, 1, namespace="archive")
        other = _chunk("other", src, 2, namespace="notes")
        await storage.upsert_chunks([hot, other])
        _age(storage, [hot], access_count=5)

        result = await execute_auto_promote(storage, {}, None, dry_run=False)

        assert result.affected_count == 0
        assert _namespaces(storage, src) == ["archive", "notes"]


# ---------------------------------------------------------------------------
# Consolidation over sources longer than the old 20-row read
# ---------------------------------------------------------------------------


async def _big_source(storage, tmp_path, n: int = 25) -> tuple[Path, list[Chunk]]:
    src = tmp_path / "big.md"
    chunks = [_chunk(f"chunk {i} unique fact {i}", src, i * 10 + 1) for i in range(n)]
    await storage.upsert_chunks(chunks)
    return src, chunks


def _importance(storage, chunks) -> dict[str, float]:
    rows = storage._get_db().execute("SELECT id, importance_score FROM chunks")
    wanted = {str(c.id) for c in chunks}
    return {r[0]: r[1] for r in rows.fetchall() if r[0] in wanted}


def _linked(storage, ids) -> set[str]:
    rows = storage._get_db().execute(
        "SELECT source_id FROM chunk_relations WHERE relation_type='consolidated_into'"
    )
    wanted = {str(i) for i in ids}
    return {r[0] for r in rows.fetchall()} & wanted


class TestConsolidationCoverage:
    async def test_links_only_what_the_summary_read(self, storage, tmp_path):
        _, chunks = await _big_source(storage, tmp_path)
        await storage.update_importance_scores({str(c.id): 1.0 for c in chunks})
        before = _importance(storage, chunks)

        result = await execute_auto_consolidate(
            storage, {"keep_originals": False}, None, dry_run=False, extract_entities=False
        )

        assert result.affected_count == 1
        assert _linked(storage, [c.id for c in chunks]) == {str(c.id) for c in chunks[:20]}
        after = _importance(storage, chunks)
        assert [after[str(c.id)] for c in chunks[20:]] == [before[str(c.id)] for c in chunks[20:]]
        # The read ones were demoted, so the comparison above is not vacuous.
        assert all(after[str(c.id)] < before[str(c.id)] for c in chunks[:20])

    async def test_max_bullets_widens_the_coverage(self, storage, tmp_path):
        _, chunks = await _big_source(storage, tmp_path)

        await execute_auto_consolidate(
            storage, {"max_bullets": 25}, None, dry_run=False, extract_entities=False
        )

        assert _linked(storage, [c.id for c in chunks]) == {str(c.id) for c in chunks}

    async def test_a_change_past_the_old_cap_regenerates(self, storage, tmp_path):
        src, chunks = await _big_source(storage, tmp_path)
        await execute_auto_consolidate(storage, {}, None, dry_run=False, extract_entities=False)

        again = await execute_auto_consolidate(
            storage, {}, None, dry_run=False, extract_entities=False
        )
        assert again.affected_count == 0  # idempotent

        await storage.upsert_chunks([_chunk("chunk 26 new fact", src, 999)])
        regen = await execute_auto_consolidate(
            storage, {}, None, dry_run=False, extract_entities=False
        )
        assert regen.affected_count == 1
        assert regen.outcome["groups"][0]["regenerated"] is True

    async def test_mixed_namespace_past_the_old_cap_is_skipped(self, storage, tmp_path):
        src, _ = await _big_source(storage, tmp_path)
        await storage.upsert_chunks([_chunk("late other ns", src, 9999, namespace="work")])

        result = await execute_auto_consolidate(
            storage, {}, None, dry_run=False, extract_entities=False
        )

        assert result.affected_count == 0
        assert result.outcome["skipped"] == ["big.md (mixed ns)"]

    async def test_a_summary_linked_past_max_bullets_is_regenerated(self, storage, tmp_path):
        # The state a summary written while the read stopped at 20 rows is in:
        # linked to more chunks than ``max_bullets`` let it read.
        src = tmp_path / "ten.md"
        chunks = [_chunk(f"fact {i}", src, i + 1) for i in range(10)]
        await storage.upsert_chunks(chunks)
        await execute_auto_consolidate(
            storage, {"max_bullets": 10}, None, dry_run=False, extract_entities=False
        )
        assert len(_linked(storage, [c.id for c in chunks])) == 10

        result = await execute_auto_consolidate(
            storage, {"max_bullets": 5}, None, dry_run=False, extract_entities=False
        )

        assert result.affected_count == 1
        assert _linked(storage, [c.id for c in chunks]) == {str(c.id) for c in chunks[:5]}
        settled = await execute_auto_consolidate(
            storage, {"max_bullets": 5}, None, dry_run=False, extract_entities=False
        )
        assert settled.affected_count == 0

    async def test_an_agent_link_past_max_bullets_mid_run_still_defers(
        self, storage, tmp_path, monkeypatch
    ):
        # The pre-check reads every chunk; the recheck under the write lock must
        # too, though only the first ``max_bullets`` are linked.
        src = tmp_path / "three.md"
        chunks = [_chunk(f"fact {i}", src, i + 1) for i in range(3)]
        agent = _chunk("agent summary", tmp_path / "agent.md", 1)
        await storage.upsert_chunks([*chunks, agent])
        await storage.update_importance_scores({str(c.id): 0.8 for c in chunks})
        linked: list[bool] = []
        real = storage.is_source_held

        async def link_then_check(path, *args, **kwargs):
            if not linked:
                linked.append(True)
                await storage.add_relation(chunks[2].id, agent.id, "consolidated_into")
            return await real(path, *args, **kwargs)

        monkeypatch.setattr(storage, "is_source_held", link_then_check)

        result = await execute_auto_consolidate(
            storage,
            {"max_bullets": 2, "keep_originals": False},
            None,
            dry_run=False,
            extract_entities=False,
        )

        assert linked == [True], "the agent link was never made"
        assert result.affected_count == 0
        assert result.outcome["skipped"] == ["three.md (agent-consolidated)"]
        assert _linked(storage, [c.id for c in chunks[:2]]) == set()
        assert [_importance(storage, chunks)[str(c.id)] for c in chunks] == [0.8, 0.8, 0.8]

    @pytest.mark.parametrize("bad", [0, -1, True, "20", 2.5])
    async def test_max_bullets_must_be_a_positive_integer(self, storage, bad):
        result = await execute_auto_consolidate(storage, {"max_bullets": bad}, None, dry_run=True)
        assert result.error == "max_bullets must be a positive integer"
