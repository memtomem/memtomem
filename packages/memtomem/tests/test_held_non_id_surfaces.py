"""Held-source content stays out of the surfaces that reach it without an id (#2781).

A held source is hidden from ranked search, entity search and maintenance
discovery (#2547). #2777 closed the id-addressed surfaces for redaction holds;
these tests pin the rest: analytics reports, tag counts, the bulk tag tools,
auto-tagging, the web source listings, and the stored-path hold checks. Every
hold kind counts here — a missing source, a pending watcher check and a
redaction refusal — so each storage test runs once per kind.

The predicate lives in the statement that reads the row. Where a race is the
point, the hold is committed between a caller's own check and the read, and
released again before the caller's second check, so only the read itself can
keep the held rows out.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from memtomem.models import ORIGIN_CONSOLIDATION_POLICY
from memtomem.services import tag_management as tag_svc
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD
from memtomem.storage.sqlite_helpers import norm_path

pytestmark = pytest.mark.asyncio

_HOLD_KINDS = ("scan_missing", REDACTION_BLOCKED_HOLD, "pending")


async def _index(comp, path: Path, body: str):
    path.write_text(body, encoding="utf-8")
    assert not (await comp.index_engine.index_file(path)).errors
    rows = await comp.storage.list_chunks_by_source(path.resolve())
    assert len(rows) == 1, rows
    return rows[0]


async def _hide(comp, path: Path, kind: str) -> None:
    if kind == "pending":
        assert comp.storage.queue_source_check_sync(path)
    else:
        assert await comp.storage.hold_source(path, kind)


def _release(comp, path: Path) -> None:
    db = comp.storage._get_db()
    db.execute("DELETE FROM held_sources WHERE source_file=?", (norm_path(path),))
    db.execute("DELETE FROM pending_source_checks WHERE source_file=?", (norm_path(path),))
    db.commit()


def _tags_of(comp, chunk_id) -> list[str]:
    row = (
        comp.storage._get_db()
        .execute("SELECT tags FROM chunks WHERE id=?", (str(chunk_id),))
        .fetchone()
    )
    return json.loads(row[0])


async def _pair(comp, mem_dir: Path, *, held_body: str, visible_body: str):
    held_path = mem_dir / "held.md"
    held = await _index(comp, held_path, held_body)
    visible = await _index(comp, mem_dir / "visible.md", visible_body)
    return held_path, held, visible


class TestAnalytics:
    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_reports_leave_a_held_chunk_out(self, bm25_only_components, kind):
        comp, mem_dir = bm25_only_components
        held_path, held, visible = await _pair(
            comp,
            mem_dir,
            held_body="# Held heading\n\nheldword fact.\n",
            visible_body="# Visible heading\n\nvisibleword fact.\n",
        )
        await comp.storage.increment_access([held.id, held.id, visible.id])
        await _hide(comp, held_path, kind)

        report = await comp.storage.get_health_report()
        assert [r["id"] for r in report["top_accessed"]] == [str(visible.id)]
        assert report["total_chunks"] == 1
        assert report["total_sources"] == 1

        frequent = await comp.storage.get_frequently_accessed()
        assert [f["source_file"] for f in frequent] == [str(visible.metadata.source_file)]

        factors = await comp.storage.get_chunk_factors()
        assert [f["id"] for f in factors] == [str(visible.id)]

    async def test_a_policy_summary_of_a_held_parent_is_left_out(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        parent = mem_dir / "parent.md"
        await _index(comp, parent, "# P\n\nparentword fact.\n")
        virtual = mem_dir / "parent.md.consolidated.md"
        summary = await _index(comp, virtual, "# S\n\nsummaryword fact.\n")
        db = comp.storage._get_db()
        db.execute(
            "UPDATE chunks SET origin=? WHERE source_file=?",
            (ORIGIN_CONSOLIDATION_POLICY, str(virtual.resolve())),
        )
        db.commit()
        await comp.storage.increment_access([summary.id])
        await comp.storage.hold_source(parent, "scan_missing")

        report = await comp.storage.get_health_report()
        assert report["top_accessed"] == []
        assert report["total_chunks"] == 0

    async def test_connectivity_counts_neither_a_held_hub_nor_a_held_neighbour(
        self, bm25_only_components
    ):
        comp, mem_dir = bm25_only_components
        held_path, held, visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        other = await _index(comp, mem_dir / "other.md", "# O\n\notherword.\n")
        await comp.storage.add_relation(visible.id, held.id)
        await comp.storage.add_relation(held.id, other.id)
        await comp.storage.add_relation(visible.id, other.id)
        before = {r["chunk_id"]: r["link_count"] for r in await comp.storage.get_most_connected()}
        assert before[str(held.id)] == 2

        await comp.storage.hold_source(held_path, "scan_missing")

        after = {r["chunk_id"]: r["link_count"] for r in await comp.storage.get_most_connected()}
        assert after == {str(visible.id): 1, str(other.id): 1}


class TestTagCounts:
    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_a_tag_only_held_chunks_carry_is_not_listed(self, bm25_only_components, kind):
        comp, mem_dir = bm25_only_components
        held_path, held, visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        db = comp.storage._get_db()
        db.execute("UPDATE chunks SET tags=? WHERE id=?", ('["heldonly","shared"]', str(held.id)))
        db.execute("UPDATE chunks SET tags=? WHERE id=?", ('["shared"]', str(visible.id)))
        db.commit()
        await _hide(comp, held_path, kind)

        assert await comp.storage.get_tag_counts() == [("shared", 1)]


class TestBulkTagTools:
    async def _tagged(self, comp, mem_dir, kind):
        held_path, held, visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        db = comp.storage._get_db()
        for chunk in (held, visible):
            db.execute("UPDATE chunks SET tags=? WHERE id=?", ('["a","b"]', str(chunk.id)))
        db.commit()
        await _hide(comp, held_path, kind)
        return held, visible

    @pytest.mark.parametrize("kind", _HOLD_KINDS)
    async def test_preview_and_apply_skip_held_rows(self, bm25_only_components, kind):
        comp, mem_dir = bm25_only_components
        held, visible = await self._tagged(comp, mem_dir, kind)

        preview = await tag_svc.rename_tag(comp.storage, "a", "z", dry_run=True)
        assert preview.affected_chunks == 1
        assert [s.chunk_id for s in preview.samples] == [visible.id]
        merge_preview = await tag_svc.merge_tags(comp.storage, ["a", "b"], "m", dry_run=True)
        assert merge_preview.affected_chunks == 1
        assert [s.chunk_id for s in merge_preview.samples] == [visible.id]

        assert (await tag_svc.rename_tag(comp.storage, "a", "z")).affected_chunks == 1
        assert _tags_of(comp, visible.id) == ["b", "z"]
        assert (await tag_svc.delete_tag(comp.storage, "b")).affected_chunks == 1
        assert _tags_of(comp, visible.id) == ["z"]
        assert _tags_of(comp, held.id) == ["a", "b"]

    @pytest.mark.parametrize("op", ["rename", "delete", "merge"])
    async def test_a_held_rows_tags_are_never_decoded(self, bm25_only_components, op):
        """A malformed tag list on a held row must not stop the visible rows' rewrite."""
        comp, mem_dir = bm25_only_components
        held, visible = await self._tagged(comp, mem_dir, "scan_missing")
        db = comp.storage._get_db()
        # Both source tags, so merge's OR-joined candidate clauses match it in
        # either order and only the parenthesised hold screen keeps it out.
        db.execute("UPDATE chunks SET tags=? WHERE id=?", ('["a", "b", broken', str(held.id)))
        db.commit()

        if op == "rename":
            result = await tag_svc.rename_tag(comp.storage, "a", "z")
        elif op == "delete":
            result = await tag_svc.delete_tag(comp.storage, "a")
        else:
            result = await tag_svc.merge_tags(comp.storage, ["a", "b"], "m")

        assert result.affected_chunks == 1
        assert "a" not in _tags_of(comp, visible.id)

    async def test_merge_leaves_a_held_row_carrying_every_source_tag(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        held, visible = await self._tagged(comp, mem_dir, "scan_missing")

        assert (await tag_svc.merge_tags(comp.storage, ["a", "b"], "m")).affected_chunks == 1
        assert _tags_of(comp, visible.id) == ["m"]
        assert _tags_of(comp, held.id) == ["a", "b"]

    async def test_a_hold_committed_after_the_candidate_read_refuses_the_write(
        self, bm25_only_components
    ):
        comp, mem_dir = bm25_only_components
        held_path, held, _visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        db = comp.storage._get_db()
        rowid = db.execute("SELECT rowid FROM chunks WHERE id=?", (str(held.id),)).fetchone()[0]
        # The candidate read saw the row visible; the hold lands before the write.
        await comp.storage.hold_source(held_path, "scan_missing")

        written = comp.storage._write_tag_batch(db, [('["late"]', "2026-01-01T00:00:00Z", rowid)])

        assert written == 0
        assert _tags_of(comp, held.id) == []


class TestAutoTag:
    async def test_held_content_is_never_extracted_previewed_or_written(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.tools import auto_tag

        comp, mem_dir = bm25_only_components
        held_path, held, visible = await _pair(
            comp,
            mem_dir,
            held_body="# H\n\nheldword kubernetes deployment.\n",
            visible_body="# V\n\nvisibleword kubernetes deployment.\n",
        )
        await comp.storage.hold_source(held_path, REDACTION_BLOCKED_HOLD)
        seen: list[str] = []
        real = auto_tag._extract_tags_with_fallback

        async def spy(text, *args, **kwargs):
            seen.append(text)
            return await real(text, *args, **kwargs)

        monkeypatch.setattr(auto_tag, "_extract_tags_with_fallback", spy)

        stats = await auto_tag.auto_tag_storage(comp.storage, sample_limit=10)

        assert not any("heldword" in text for text in seen)
        assert [s.chunk_id for s in stats.samples] == [str(visible.id)]
        assert _tags_of(comp, held.id) == []
        assert _tags_of(comp, visible.id)

    async def test_a_hold_committed_during_extraction_skips_the_write(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.tools import auto_tag

        comp, mem_dir = bm25_only_components
        path = mem_dir / "late.md"
        chunk = await _index(comp, path, "# L\n\nlateword kubernetes deployment.\n")

        async def hold_then_tag(text, *args, **kwargs):
            await comp.storage.hold_source(path, "scan_missing")
            return ["kubernetes"]

        monkeypatch.setattr(auto_tag, "_extract_tags_with_fallback", hold_then_tag)

        stats = await auto_tag.auto_tag_storage(comp.storage)

        assert _tags_of(comp, chunk.id) == []
        assert (stats.total_chunks, stats.tagged_chunks, stats.skipped_chunks) == (0, 0, 1)

    async def test_set_chunk_tags_writes_a_visible_row_and_refuses_a_held_one(
        self, bm25_only_components
    ):
        comp, mem_dir = bm25_only_components
        held_path, held, visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        await comp.storage.hold_source(held_path, "scan_missing")

        assert await comp.storage.set_chunk_tags(visible.id, ["t"]) is True
        assert await comp.storage.set_chunk_tags(held.id, ["t"]) is False
        assert _tags_of(comp, visible.id) == ["t"]
        assert _tags_of(comp, held.id) == []

    async def test_the_policy_estimate_counts_what_auto_tag_can_see(self, bm25_only_components):
        from memtomem.tools.policy_engine import execute_auto_tag

        comp, mem_dir = bm25_only_components
        held_path, _held, _visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        await comp.storage.hold_source(held_path, "scan_missing")

        result = await execute_auto_tag(comp.storage, {}, None, dry_run=True)

        assert result.affected_count == 1


def _web_app(comp):
    from memtomem.web.app import create_app

    app = create_app(lifespan=None, mode="dev")
    app.state.csrf_enforce = False
    app.state.storage = comp.storage
    app.state.search_pipeline = comp.search_pipeline
    app.state.index_engine = comp.index_engine
    app.state.config = comp.config
    return app


async def _get(app, url: str, **params):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        return await client.get(url, params=params)


class TestWebChunkListing:
    async def test_rows_read_while_held_are_not_returned(self, bm25_only_components, monkeypatch):
        """Hold after the first check, release before the second: only the read can tell."""
        from memtomem.web.routes import chunks as chunks_route

        comp, mem_dir = bm25_only_components
        path = mem_dir / "raced.md"
        await _index(comp, path, "# R\n\nracedword fact.\n")
        real = chunks_route.source_hidden
        calls = 0

        async def racing(storage, source):
            nonlocal calls
            calls += 1
            answer = await real(storage, source)
            if calls == 1:
                await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
            else:
                _release(comp, path)
                answer = await real(storage, source)
            return answer

        monkeypatch.setattr(chunks_route, "source_hidden", racing)

        resp = await _get(_web_app(comp), "/api/chunks", source=str(path.resolve()))

        assert resp.status_code == 200, resp.text
        assert "racedword" not in resp.text
        assert resp.json() == {"chunks": [], "total": 0}

    async def test_a_hold_between_list_and_count_answers_404(
        self, bm25_only_components, monkeypatch
    ):
        comp, mem_dir = bm25_only_components
        path = mem_dir / "late.md"
        await _index(comp, path, "# L\n\nlateword fact.\n")
        real = comp.storage.count_chunks_by_source

        async def hold_then_count(*args, **kwargs):
            await comp.storage.hold_source(path, "scan_missing")
            return await real(*args, **kwargs)

        monkeypatch.setattr(comp.storage, "count_chunks_by_source", hold_then_count)

        resp = await _get(_web_app(comp), "/api/chunks", source=str(path.resolve()))

        assert resp.status_code == 404
        assert "lateword" not in resp.text


class TestWebSourceSummaries:
    async def test_storage_reads_drop_hidden_sources(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        held_path, _held, _visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        for p in (held_path, mem_dir / "visible.md"):
            await comp.storage.set_ai_summary(p, f"{p.stem} prose", "sig", "en")
        await comp.storage.hold_source(held_path, REDACTION_BLOCKED_HOLD)

        visible_key = norm_path(mem_dir / "visible.md")
        assert set(await comp.storage.get_source_summaries()) == {visible_key}
        assert set(await comp.storage.get_all_ai_summaries(exclude_hidden=True)) == {visible_key}
        assert len(await comp.storage.get_all_ai_summaries()) == 2

    async def test_a_hold_after_the_visibility_read_yields_no_excerpt(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.web.routes import sources as sources_route

        comp, mem_dir = bm25_only_components
        path = mem_dir / "raced.md"
        await _index(comp, path, "# R\n\nracedword excerpt.\n")
        await comp.storage.set_ai_summary(path, "racedword prose", "sig", "en")
        real = sources_route.load_source_visibility

        async def then_hold(storage, config):
            visibility = await real(storage, config)
            await comp.storage.hold_source(path, REDACTION_BLOCKED_HOLD)
            return visibility

        monkeypatch.setattr(sources_route, "load_source_visibility", then_hold)
        app = _web_app(comp)

        listing = await _get(app, "/api/sources")
        assert listing.status_code == 200, listing.text
        assert "racedword" not in listing.text

        _release(comp, path)
        matches = await _get(app, "/api/sources/content-matches", q="prose")
        assert matches.status_code == 200, matches.text
        assert matches.json()["paths"] == []


def _symlink_or_skip(link: Path, target: Path) -> None:
    try:
        os.symlink(target, link, target_is_directory=target.is_dir())
    except (OSError, NotImplementedError) as exc:  # Windows without the privilege
        pytest.skip(f"symlinks unavailable: {exc}")


class TestStoredPathHoldChecks:
    async def test_a_symlink_at_a_held_path_does_not_hide_the_hold(self, bm25_only_components):
        from memtomem.search.dedup import DedupScanner

        comp, mem_dir = bm25_only_components
        held_path, held, visible = await _pair(
            comp, mem_dir, held_body="# H\n\nheldword.\n", visible_body="# V\n\nvisibleword.\n"
        )
        stored = Path(norm_path(held_path))
        await comp.storage.hold_source(held_path, "scan_missing")
        held_path.unlink()
        _symlink_or_skip(held_path, mem_dir / "visible.md")

        assert await comp.storage.is_source_held(stored)
        assert await comp.storage.is_source_hidden(stored)
        chunks = await DedupScanner(comp.storage)._get_all_chunks(100)
        assert [c.id for c in chunks] == [visible.id]

    @pytest.mark.skipif(sys.platform == "win32", reason="directory symlinks need privilege")
    async def test_raw_read_rechecks_the_file_a_retargeted_parent_reaches(
        self, bm25_only_components, monkeypatch
    ):
        from memtomem.web.routes import sources as sources_route

        comp, mem_dir = bm25_only_components
        clean_dir, held_dir = mem_dir / "clean", mem_dir / "held"
        clean_dir.mkdir()
        held_dir.mkdir()
        await _index(comp, clean_dir / "f.md", "# C\n\ncleanword.\n")
        await _index(comp, held_dir / "f.md", "# H\n\nheldrawword.\n")
        await comp.storage.hold_source(held_dir / "f.md", REDACTION_BLOCKED_HOLD)
        real_gate = sources_route.require_indexed_source

        def gate_then_retarget(user_path, indexed):
            key = real_gate(user_path, indexed)
            clean_dir.rename(mem_dir / "clean-moved")
            _symlink_or_skip(clean_dir, held_dir)
            return key

        monkeypatch.setattr(sources_route, "require_indexed_source", gate_then_retarget)

        resp = await _get(
            _web_app(comp), "/api/sources/content", path=str((clean_dir / "f.md").resolve())
        )

        assert resp.status_code == 404
        assert "heldrawword" not in resp.text


class TestCostStaysLinear:
    """The hold screens must not turn a read quadratic in the chunk count.

    Two shapes did, both measured at 50,000 chunks: a ``visible`` CTE the
    planner inlined into a nested scan of both relation endpoints (over 15 s,
    from under 0.1 s), and a path screen that scanned the whole file once per
    row it read (over 30 s), which ``idx_chunks_policy_source`` answers. That
    scan ran in either order of the screen's terms for a file whose parent is
    held, hence the ``held_parent`` cells. Wall-clock limits would be flaky, so this counts
    SQLite VM steps through the progress handler and compares two sizes:
    quadrupling the rows must cost well under the sixteenfold a quadratic
    plan does.
    """

    _SMALL, _LARGE = 300, 1200

    @staticmethod
    def _fill(comp, n: int, source: str) -> None:
        import uuid

        db = comp.storage._get_db()
        db.execute("DELETE FROM chunks")
        db.executemany(
            "INSERT INTO chunks (id, content, source_file, tags, access_count, start_line, "
            "content_hash, created_at, updated_at, namespace, end_line, heading_hierarchy, "
            "scope) VALUES (?, 'c', ?, ?, 1, ?, 'h', "
            "'2026-01-01T00:00:00Z', '2026-01-01T00:00:00Z', 'default', 2, '[]', 'user')",
            # Half tagged for the tag counts, half untagged for the policy estimate.
            [(str(uuid.uuid4()), source, '["t"]' if i % 2 else "[]", i) for i in range(n)],
        )
        db.commit()

    @staticmethod
    async def _steps(comp, call) -> int:
        conns = [comp.storage._get_db(), *comp.storage._read_pool]
        ticks = 0

        def tick() -> int:
            nonlocal ticks
            ticks += 1
            return 0

        for conn in conns:
            conn.set_progress_handler(tick, 100)
        try:
            await call()
        finally:
            for conn in conns:
                conn.set_progress_handler(None, 0)
        return ticks

    @pytest.mark.parametrize(
        "name",
        [
            "health_report",
            "chunk_factors",
            "most_connected",
            "tag_counts",
            "source_summaries",
            "list_hidden",
            "count_hidden",
            "policy_estimate",
        ],
    )
    @pytest.mark.parametrize("held_parent", [False, True])
    async def test_quadrupling_the_rows_does_not_square_the_cost(
        self, bm25_only_components, name, held_parent
    ):
        from memtomem.tools.policy_engine import execute_auto_tag

        comp, _ = bm25_only_components
        st = comp.storage
        # Ordinary chunks (no policy origin) under a ``.consolidated.md`` path
        # whose parent is held: the screen must look for a policy row there.
        source = Path("/big/root.md.consolidated.md" if held_parent else "/big/one.md")
        if held_parent:
            db = st._get_db()
            db.execute(
                "INSERT INTO held_sources(source_file, reason, first_seen_at, last_checked_at) "
                "VALUES ('/big/root.md', 'scan_missing', '2026-01-01', '2026-01-01')"
            )
            db.commit()
        calls = {
            "health_report": lambda: st.get_health_report(),
            "chunk_factors": lambda: st.get_chunk_factors(),
            "most_connected": lambda: st.get_most_connected(),
            "tag_counts": lambda: st.get_tag_counts(),
            "source_summaries": lambda: st.get_source_summaries(),
            "list_hidden": lambda: st.list_chunks_by_source(source, None, exclude_hidden=True),
            "count_hidden": lambda: st.count_chunks_by_source(source, exclude_hidden=True),
            "policy_estimate": lambda: execute_auto_tag(st, {}, None, dry_run=True),
        }
        costs = []
        for n in (self._SMALL, self._LARGE):
            self._fill(comp, n, str(source))
            costs.append(await self._steps(comp, calls[name]))
        assert costs[1] < 8 * max(costs[0], 1), costs
