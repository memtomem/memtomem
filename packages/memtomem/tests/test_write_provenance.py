"""Write provenance: what a session actually wrote (issue #1876).

Split from ``test_sessions.py`` because this is a cross-cutting concern
of the write surfaces rather than of the session lifecycle.
"""

from __future__ import annotations

import pytest

from memtomem.server.context import AppContext
from memtomem.server.tools import _provenance
from memtomem.server.tools._provenance import (
    PROVENANCE_KIND,
    mark_provenance_incomplete,
    record_write_provenance,
    render_event_content,
)

# The seven chunk-creating write surfaces. ``mem_edit`` / ``mem_delete``
# are deliberately absent — see the module docstring of ``_provenance``.
EVENT_TYPES = (
    "add",
    "batch_add",
    "index",
    "fetch",
    "agent_share",
    "candidate_review",
    "consolidate_apply",
)


class _Stats:
    """Stand-in for ``IndexingStats``; only ``new_chunk_ids`` is read."""

    def __init__(self, ids):
        self.new_chunk_ids = tuple(ids)


class TestProvenanceEventContent:
    """The event's ``content`` is read by more than a human.

    ``formation.scan_session_candidates`` regex-classifies every session
    event's content into review candidates, copies it verbatim into the
    candidate body and its proposed diff, and the web API renders it.
    """

    @pytest.mark.parametrize("event_type", EVENT_TYPES)
    @pytest.mark.parametrize("truncated", [False, True])
    def test_content_matches_no_formation_kind_pattern(self, event_type, truncated):
        """A rename that slipped a classifier keyword into an event type
        would make every write of that kind silently manufacture a review
        candidate, which ``mem_formation_scan`` then offers to the user.

        Pinned by running the real classifier, not by reading the regexes.
        """
        from memtomem.formation import _classify

        content = render_event_content(event_type, 3, truncated=truncated)
        assert _classify(content) is None, f"{content!r} classifies as {_classify(content)}"

    @pytest.mark.parametrize("event_type", EVENT_TYPES)
    def test_content_is_a_fixed_descriptor(self, event_type):
        content = render_event_content(event_type, 12, truncated=False)
        assert content == f"{PROVENANCE_KIND} {event_type} chunks=12"
        assert "/" not in content


class TestRecordWriteProvenance:
    """The recorder itself, driven directly against real storage."""

    async def _app(self, components, session_id="s1"):
        app = AppContext.from_components(components)
        await app.storage.create_session(session_id, "planner", "default")
        app.current_session_id = session_id
        return app

    @pytest.mark.asyncio
    async def test_records_one_marked_event_with_stringified_ids(self, components):
        from uuid import uuid4

        app = await self._app(components)
        ids = [uuid4() for _ in range(3)]

        await record_write_provenance(app, session_id="s1", event_type="add", stats=_Stats(ids))

        events = await app.storage.get_session_events("s1")
        assert len(events) == 1
        assert events[0]["event_type"] == "add"
        assert events[0]["metadata"] == {"provenance": PROVENANCE_KIND, "chunk_count": 3}
        assert events[0]["chunk_ids"] == [str(i) for i in ids]
        # UUIDs would have raised inside json.dumps; assert the type
        # explicitly so a future change that round-trips them some other
        # way still has to keep the contract.
        assert all(isinstance(c, str) for c in events[0]["chunk_ids"])

    @pytest.mark.asyncio
    async def test_no_session_records_nothing(self, components):
        app = await self._app(components)
        from uuid import uuid4

        await record_write_provenance(
            app, session_id=None, event_type="add", stats=_Stats([uuid4()])
        )

        assert await app.storage.get_session_events("s1") == []

    @pytest.mark.asyncio
    async def test_no_stats_records_nothing(self, components):
        """The idempotency replays and every early-error path return
        ``stats is None``; none of them wrote a chunk."""
        app = await self._app(components)

        await record_write_provenance(app, session_id="s1", event_type="add", stats=None)

        assert await app.storage.get_session_events("s1") == []

    @pytest.mark.asyncio
    async def test_an_unchanged_reindex_records_nothing(self, components):
        app = await self._app(components)

        await record_write_provenance(app, session_id="s1", event_type="index", stats=_Stats([]))

        assert await app.storage.get_session_events("s1") == []

    @pytest.mark.asyncio
    async def test_one_event_per_call_regardless_of_id_count(self, components):
        """``event_counts`` must stay a count of logical writes.

        Sharding a large id list would turn one ``mem_index`` into twenty
        ``index`` events, and that number is rendered by
        ``mem_session_end``, stored in the session metadata, written into
        the archive frontmatter, and reported by ``mm session show``, the
        web totals and ``langgraph.end_agent_session``.
        """
        from uuid import uuid4

        app = await self._app(components)
        ids = [uuid4() for _ in range(300)]

        await record_write_provenance(app, session_id="s1", event_type="index", stats=_Stats(ids))

        events = await app.storage.get_session_events("s1")
        assert len(events) == 1
        assert len(events[0]["chunk_ids"]) == 300
        assert events[0]["metadata"]["chunk_count"] == 300
        assert "truncated" not in events[0]["metadata"]


class TestProvenanceTruncation:
    @pytest.mark.asyncio
    async def test_ids_past_the_cap_are_truncated_and_the_session_flagged(
        self, components, monkeypatch
    ):
        """Truncation is lossy, so it must be visible on the row a
        consumer reads — the response line alone is not enough."""
        from uuid import uuid4

        monkeypatch.setattr(_provenance, "MAX_IDS_PER_EVENT", 3)
        app = AppContext.from_components(components)
        await app.storage.create_session("s2", "planner", "default")
        app.current_session_id = "s2"
        ids = [uuid4() for _ in range(5)]

        await record_write_provenance(app, session_id="s2", event_type="index", stats=_Stats(ids))

        events = await app.storage.get_session_events("s2")
        assert len(events[0]["chunk_ids"]) == 3
        assert events[0]["metadata"]["truncated"] is True
        # The true count survives, so a consumer knows how much it lost.
        assert events[0]["metadata"]["chunk_count"] == 5

        row = await app.storage.get_session("s2")
        assert row["metadata"]["provenance_incomplete"] is True

    @pytest.mark.asyncio
    async def test_a_later_clean_write_does_not_clear_the_flag(self, components, monkeypatch):
        """The flag is one-way. A session that lost ids once has lost
        them for good; a subsequent tidy write does not restore them."""
        from uuid import uuid4

        monkeypatch.setattr(_provenance, "MAX_IDS_PER_EVENT", 3)
        app = AppContext.from_components(components)
        await app.storage.create_session("s3", "planner", "default")
        app.current_session_id = "s3"

        await record_write_provenance(
            app, session_id="s3", event_type="index", stats=_Stats([uuid4() for _ in range(5)])
        )
        await record_write_provenance(
            app, session_id="s3", event_type="add", stats=_Stats([uuid4()])
        )

        row = await app.storage.get_session("s3")
        assert row["metadata"]["provenance_incomplete"] is True


class TestProvenanceFailureIsolation:
    @pytest.mark.asyncio
    async def test_a_failed_event_write_is_downgraded_to_the_incomplete_flag(
        self, components, caplog
    ):
        """The recorder must not raise into the write path, and must not
        stay silent either: a consumer treats provenance as authoritative
        unless the row says otherwise."""
        from uuid import uuid4

        app = AppContext.from_components(components)
        await app.storage.create_session("s4", "planner", "default")
        app.current_session_id = "s4"

        calls = []

        async def boom(*args, **kwargs):
            calls.append(args)
            raise RuntimeError("database is locked")

        app.storage.add_session_event = boom  # type: ignore[method-assign]

        with caplog.at_level("WARNING"):
            await record_write_provenance(
                app, session_id="s4", event_type="add", stats=_Stats([uuid4()])
            )

        # The double actually fired — not just "the outcome looks right".
        assert len(calls) == 1
        assert "provenance_event_write_failed" in caplog.text
        row = await app.storage.get_session("s4")
        assert row["metadata"]["provenance_incomplete"] is True

    @pytest.mark.asyncio
    async def test_a_failed_flag_write_is_logged_at_error_and_swallowed(self, components, caplog):
        """Both writes gone is the one state where a consumer can be
        actively wrong — the session still claims to record provenance
        and nothing says the record is short. Alertable, not silent."""
        from uuid import uuid4

        app = AppContext.from_components(components)
        await app.storage.create_session("s5", "planner", "default")
        app.current_session_id = "s5"

        flag_calls = []

        async def boom_event(*args, **kwargs):
            raise RuntimeError("database is locked")

        async def boom_flag(*args, **kwargs):
            flag_calls.append(args)
            raise RuntimeError("database is locked")

        app.storage.add_session_event = boom_event  # type: ignore[method-assign]
        app.storage.update_session_metadata = boom_flag  # type: ignore[method-assign]

        with caplog.at_level("ERROR"):
            await record_write_provenance(
                app, session_id="s5", event_type="add", stats=_Stats([uuid4()])
            )

        assert len(flag_calls) == 1
        assert "provenance_flag_write_failed" in caplog.text

    @pytest.mark.asyncio
    async def test_marking_a_missing_session_does_not_raise(self, components):
        app = AppContext.from_components(components)
        await mark_provenance_incomplete(app, "no-such-session")
