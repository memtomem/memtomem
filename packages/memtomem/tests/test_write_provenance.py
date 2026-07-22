"""Write provenance: what a session actually wrote (issue #1876).

Split from ``test_sessions.py`` because this is a cross-cutting concern
of the write surfaces rather than of the session lifecycle.
"""

from __future__ import annotations

from types import SimpleNamespace

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


class _StubCtx:
    """Minimal stand-in for the MCP ``Context`` object."""

    def __init__(self, app):
        self.request_context = SimpleNamespace(lifespan_context=app)


async def _provenance_events(app, session_id):
    """Only the events this module wrote — a session log may hold others."""
    return [
        e
        for e in await app.storage.get_session_events(session_id)
        if (e.get("metadata") or {}).get("provenance") == PROVENANCE_KIND
    ]


class TestEveryChunkCreatingSurfaceIsAccountedFor:
    """No tool may create chunks and say nothing about it.

    This guard exists because the first version of this feature was
    scoped from a hand-written list of seven write surfaces and then
    tested against that same list — so the three importers and the two
    bulk delete branches, which also change a session's chunk set, were
    invisible to both the design and its tests. A list checked against
    itself certifies nothing.

    So the source is the authority instead: any function that reaches the
    indexing engine or deletes chunks must either record provenance or
    flag the session. A new surface that does neither fails here rather
    than silently shipping a session that claims a complete record.
    """

    # Calls that create or destroy chunks. Reaching any of them obliges a
    # function to account for what it did. ``delete_chunks`` is the
    # low-level one every deletion helper bottoms out in — leaving it out
    # was what let ``mem_dedup_merge`` and ``mem_decay_expire`` through,
    # since they delete via a scanner/service rather than calling storage
    # directly.
    _CHUNK_CALLS = frozenset(
        {
            "index_file",
            "index_path",
            "import_chunks",
            "assign_namespace",
            "delete_by_source",
            "delete_by_namespace",
            "delete_chunks",
            "expire_chunks",
            "merge",
        }
    )
    # The ways to account for it.
    _ACCOUNTING_CALLS = frozenset(
        {
            "record_write_provenance",
            "flag_untracked_write",
            "mark_provenance_incomplete",
            "_flag_imprecise_write",
        }
    )
    # Functions that touch chunks but are provably not session inputs.
    # Each needs a reason, not just an entry.
    _EXEMPT = {
        # The session's own summary is an output of teardown, not one of
        # the inputs it summarizes. Instrumenting it would make every
        # session record a write it performed on itself.
        ("session.py", "_write_summary_archive"),
    }

    def test_no_chunk_creating_tool_is_silent(self):
        """Every public tool that changes the chunk set says so.

        Resolved one level through same-module helpers: a tool that
        delegates its mutation to a private helper is judged on the
        helper's behavior plus its own, so moving a delete into a helper
        does not shake the guard off.
        """
        import ast
        from pathlib import Path

        import memtomem

        tools = Path(memtomem.__file__).parent / "server" / "tools"

        def called_names(node, *, skip=frozenset()):
            """Names called inside ``node``.

            A mutator invoked with a literal ``dry_run=True`` is not a
            mutation and is not counted — checked rather than exempted by
            hand, so flipping that argument to False re-arms the guard
            instead of leaving a stale entry in a list.
            """
            names = set()
            for c in ast.walk(node):
                if not isinstance(c, ast.Call) or not isinstance(c.func, (ast.Attribute, ast.Name)):
                    continue
                fname = c.func.attr if isinstance(c.func, ast.Attribute) else c.func.id
                if fname in skip:
                    continue
                dry = next((k for k in c.keywords if k.arg == "dry_run"), None)
                if (
                    fname in self._CHUNK_CALLS
                    and dry is not None
                    and isinstance(dry.value, ast.Constant)
                    and dry.value.value is True
                ):
                    continue
                names.add(fname)
            return names

        offenders: list[str] = []
        for path in sorted(tools.glob("*.py")):
            if path.name == "__init__.py":
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            funcs = {
                n.name: n
                for n in ast.walk(tree)
                if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
            # An exempt helper does not contaminate its callers: the reason
            # it is exempt (a session's own summary is not one of its
            # inputs) holds just as well one frame up.
            exempt_here = {n for (f, n) in self._EXEMPT if f == path.name}
            for name, node in funcs.items():
                if name in exempt_here:
                    continue
                called = called_names(node, skip=exempt_here)
                # Inherit from same-module helpers this function calls, so
                # a mutation hidden one frame down still counts — and so
                # does the accounting done down there.
                for helper in called & set(funcs) - {name}:
                    called |= called_names(funcs[helper], skip=exempt_here)
                if called & self._CHUNK_CALLS and not (called & self._ACCOUNTING_CALLS):
                    offenders.append(f"{path.name}::{name}")

        assert not offenders, (
            "These functions create or delete chunks without recording provenance "
            "or flagging the session, so a session containing them would report a "
            "complete record of an incomplete one:\n  - " + "\n  - ".join(sorted(offenders))
        )


class TestMemAddCoreCallSiteLabels:
    """Every ``_mem_add_core`` caller names itself, and names itself
    *differently*.

    The required ``event_type`` keyword already makes a new caller fail
    loudly rather than skip provenance. What it cannot catch is a
    copy-pasted label: a fifth surface arriving as ``"add"`` would report
    its writes under another tool's name in ``event_counts``, and no
    runtime test would notice. Read from the source so the guard covers
    call sites no test happens to drive.
    """

    def test_each_caller_passes_a_distinct_event_type_literal(self):
        import ast
        from pathlib import Path

        import memtomem

        tools = Path(memtomem.__file__).parent / "server" / "tools"
        labels: dict[str, str] = {}
        for path in sorted(tools.glob("*.py")):
            # Pin UTF-8: py312 resolves ``read_text``'s encoding from the
            # locale, so on a cp1252 Windows runner this raises
            # UnicodeDecodeError on any non-ASCII byte in a tool module.
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                fn = node.func
                if not (isinstance(fn, ast.Name) and fn.id == "_mem_add_core"):
                    continue
                kw = next((k for k in node.keywords if k.arg == "event_type"), None)
                assert kw is not None, f"{path.name}:{node.lineno} omits event_type"
                assert isinstance(kw.value, ast.Constant), (
                    f"{path.name}:{node.lineno} passes a non-literal event_type; "
                    "the label must be readable from the call site"
                )
                where = f"{path.name}:{node.lineno}"
                assert kw.value.value not in labels, (
                    f"{where} reuses event_type={kw.value.value!r}, already used by "
                    f"{labels[kw.value.value]} — one of the two would be misreported"
                )
                labels[kw.value.value] = where

        assert set(labels) == {"add", "agent_share", "candidate_review", "consolidate_apply"}


class TestMemAddCoreSurfaces:
    """The four public tools that write through ``_mem_add_core``.

    Instrumented inside the shared helper rather than at each tool, so
    every one of them must still come out labelled with its *own* name —
    a mislabel here is what the required ``event_type`` parameter exists
    to prevent.
    """

    @pytest.mark.asyncio
    async def test_mem_add_records_an_add_event(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="auth flow: JWT in a cookie", ctx=ctx)  # type: ignore[arg-type]

        events = await _provenance_events(app, session_id)
        assert [e["event_type"] for e in events] == ["add"]
        assert len(events[0]["chunk_ids"]) >= 1
        assert all(isinstance(c, str) for c in events[0]["chunk_ids"])

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mem_add_records_nothing_without_a_session(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_add

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        await app.storage.create_session("bystander", "planner", "default")

        await mem_add(content="written outside any session", ctx=ctx)  # type: ignore[arg-type]

        assert await app.storage.get_session_events("bystander") == []

    @pytest.mark.asyncio
    async def test_an_idempotent_replay_records_nothing(self, bm25_only_components):
        """A replay returns ``stats is None`` because no second write
        happened — recording it would double-count one logical write."""
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="written once", idempotency_key="k1", ctx=ctx)  # type: ignore[arg-type]
        replay = await mem_add(content="written once", idempotency_key="k1", ctx=ctx)  # type: ignore[arg-type]

        assert "idempotent replay" in replay
        assert len(await _provenance_events(app, session_id)) == 1

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mem_agent_share_records_an_agent_share_event(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.multi_agent import mem_agent_register, mem_agent_share
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_agent_register(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact worth sharing with the team", ctx=ctx)  # type: ignore[arg-type]

        # Share the very chunk the first provenance event named — the id
        # round-trips, which is itself part of the contract.
        added = (await _provenance_events(app, session_id))[0]
        await mem_agent_share(chunk_id=added["chunk_ids"][0], target="shared", ctx=ctx)  # type: ignore[arg-type]

        events = await _provenance_events(app, session_id)
        assert [e["event_type"] for e in events] == ["add", "agent_share"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]


class TestProvenanceSessionAttribution:
    """A write is attributed to the session live when it *wrote*.

    Waiting on the file lock is a suspension point, so the active session
    can change between a tool being invoked and its content reaching the
    disk. The namespace resolution has always been done after that wait
    for exactly this reason; the session id has to be read in the same
    breath or the chunks and their provenance describe two different
    sessions.
    """

    @pytest.mark.asyncio
    async def test_the_session_id_is_read_under_the_same_lock_as_the_namespace(
        self, bm25_only_components, monkeypatch
    ):
        """Two separate ``_session_lock`` acquisitions would leave a
        window between them wide enough for a whole transition.

        Asserted structurally rather than by trying to hit the window:
        the race needs a transition to interleave between two reads that
        are microseconds apart, which no timing-based test can make
        reliable.
        """
        from memtomem.server.tools import _provenance as provenance_mod
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        observed: list[bool] = []
        real_resolve = provenance_mod._resolve_agent_namespace

        def watched(app_arg, agent_id):
            observed.append(app_arg._session_lock.locked())
            return real_resolve(app_arg, agent_id)

        monkeypatch.setattr(provenance_mod, "_resolve_agent_namespace", watched)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        observed.clear()
        await mem_add(content="written under a held session lock", ctx=ctx)  # type: ignore[arg-type]

        assert observed, "the namespace resolution hook never ran"
        assert all(observed), (
            "the namespace was resolved without _session_lock held — the session "
            "id and the namespace are being read in separate acquisitions"
        )

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_write_parked_on_the_file_lock_is_attributed_to_the_session_it_lands_in(
        self, bm25_only_components, monkeypatch
    ):
        """The whole point of resolving after the wait, applied to the
        provenance: a write whose lock wait straddles a session swap
        belongs to the session that was live when it wrote.

        Capturing before the wait would file these chunks under the old
        session while the namespace resolved for the new one — the same
        chunks-and-record-disagree failure #1876 is about.
        """
        import asyncio

        from memtomem.server.tools import session as session_mod
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        monkeypatch.setattr(session_mod, "_WRITE_DRAIN_TIMEOUT_S", 0.05)

        await mem_session_start(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        first_id = app.current_session_id

        # Park a writer on the target file's lock, then swap the session
        # underneath it before letting it proceed.
        from memtomem.server.tools.memory_crud import _validate_path

        target, err = _validate_path("notes.md", app.config.indexing.memory_dirs)
        assert err is None and target is not None

        released = asyncio.Event()

        async def hold_the_file_lock():
            async with app.get_memory_file_lock(target):
                await released.wait()

        holder = asyncio.create_task(hold_the_file_lock())
        await asyncio.sleep(0.05)

        writer = asyncio.create_task(
            mem_add(content="written across a session swap", file="notes.md", ctx=ctx)  # type: ignore[arg-type]
        )
        await asyncio.sleep(0.05)

        await mem_session_start(agent_id="beta", ctx=ctx)  # type: ignore[arg-type]
        second_id = app.current_session_id
        assert second_id != first_id

        released.set()
        await holder
        await writer

        assert await _provenance_events(app, first_id) == []
        landed = await _provenance_events(app, second_id)
        assert [e["event_type"] for e in landed] == ["add"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]


class TestProvenanceDrain:
    """Session teardown waits for the provenance *event*, not merely for
    the indexing that preceded it."""

    @pytest.mark.asyncio
    async def test_end_waits_for_the_provenance_event_not_just_the_indexing(
        self, bm25_only_components
    ):
        """A gauge that closed after ``index_file`` but before the event
        write would let teardown observe idle, snapshot an empty event
        list, and end the session reporting no writes — even though a
        chunk had already landed on disk. A consumer would inherit that
        as "this session wrote nothing".
        """
        import asyncio

        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id

        in_event = asyncio.Event()
        release = asyncio.Event()
        snapshot_taken = asyncio.Event()
        real_add_event = app.storage.add_session_event
        real_get_events = app.storage.get_session_events

        async def gated_add_event(*args, **kwargs):
            in_event.set()
            await release.wait()
            return await real_add_event(*args, **kwargs)

        async def watched_get_events(sid):
            snapshot_taken.set()
            return await real_get_events(sid)

        app.storage.add_session_event = gated_add_event  # type: ignore[method-assign]
        app.storage.get_session_events = watched_get_events  # type: ignore[method-assign]

        writer = asyncio.create_task(mem_add(content="a fact still being logged", ctx=ctx))  # type: ignore[arg-type]
        await in_event.wait()

        ender = asyncio.create_task(mem_session_end(ctx=ctx))  # type: ignore[arg-type]
        await asyncio.sleep(0.05)

        assert not snapshot_taken.is_set(), (
            "teardown snapshotted the event list while a provenance event was still being written"
        )

        release.set()
        await writer
        out = await ender

        assert "add:1" in out
        assert "writes still in flight" not in out
        assert len(await _provenance_events(app, session_id)) == 1


class TestProvenanceCompletenessSealing:
    """A marked session must never claim completeness it does not have.

    New captures are excluded after the transition claim. A write that
    captured the session before that seal can still outlast the bounded drain,
    though, so its eventual event must mark the already-ended row incomplete.
    Once the row says "this session records provenance", a silent short list
    is a false claim instead of a missing summary.
    """

    @pytest.mark.asyncio
    async def test_a_write_that_outran_teardown_marks_the_session_incomplete(
        self, bm25_only_components
    ):
        """The event still gets recorded — the write is real — but the
        session stops presenting its input set as whole."""
        from uuid import uuid4

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        await app.storage.create_session("late", "planner", "default", {"provenance": "write-v1"})
        # The session has been claimed for teardown; the write below is
        # the one that arrives too late to be in the snapshot.
        app.current_session_id = "late"
        app._ending_session_ids.add("late")

        await record_write_provenance(
            app, session_id="late", event_type="add", stats=_Stats([uuid4()])
        )

        assert len(await _provenance_events(app, "late")) == 1
        row = await app.storage.get_session("late")
        assert row["metadata"]["provenance_incomplete"] is True

    @pytest.mark.asyncio
    async def test_a_write_landing_after_the_session_was_replaced_marks_it_incomplete(
        self, bm25_only_components
    ):
        """Teardown may have finished and released its claim before the
        straggler lands, so the claim set alone is not enough."""
        from uuid import uuid4

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        await app.storage.create_session("gone", "planner", "default", {"provenance": "write-v1"})
        app.current_session_id = "successor"

        await record_write_provenance(
            app, session_id="gone", event_type="add", stats=_Stats([uuid4()])
        )

        row = await app.storage.get_session("gone")
        assert row["metadata"]["provenance_incomplete"] is True

    @pytest.mark.asyncio
    async def test_an_ordinary_write_leaves_the_session_complete(self, bm25_only_components):
        """The seal must not fire on the common path, or the flag means
        nothing and every session falls back."""
        from uuid import uuid4

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        await app.storage.create_session("live", "planner", "default", {"provenance": "write-v1"})
        app.current_session_id = "live"

        await record_write_provenance(
            app, session_id="live", event_type="add", stats=_Stats([uuid4()])
        )

        row = await app.storage.get_session("live")
        assert "provenance_incomplete" not in row["metadata"]

    @pytest.mark.asyncio
    async def test_a_drain_timeout_is_recorded_on_the_row_not_just_the_response(
        self, bm25_only_components, monkeypatch
    ):
        """A consumer of the stored row never sees the response line."""
        import asyncio

        from memtomem.server.tools import session as session_mod
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        monkeypatch.setattr(session_mod, "_WRITE_DRAIN_TIMEOUT_S", 0.05)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id

        in_write = asyncio.Event()
        release = asyncio.Event()

        async def parked_write():
            async with app.write_in_flight():
                in_write.set()
                await release.wait()

        writer = asyncio.create_task(parked_write())
        await in_write.wait()

        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
        release.set()
        await writer

        assert "writes still in flight" in out
        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

    @pytest.mark.asyncio
    async def test_a_superseded_session_records_its_own_drain_timeout(
        self, bm25_only_components, monkeypatch
    ):
        """Supersession runs the same protocol, and its row is read by
        the same consumer."""
        import asyncio

        from memtomem.server.tools import session as session_mod
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        monkeypatch.setattr(session_mod, "_WRITE_DRAIN_TIMEOUT_S", 0.05)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        superseded_id = app.current_session_id

        in_write = asyncio.Event()
        release = asyncio.Event()

        async def parked_write():
            async with app.write_in_flight():
                in_write.set()
                await release.wait()

        writer = asyncio.create_task(parked_write())
        await in_write.wait()

        await mem_session_start(agent_id="builder", ctx=ctx)  # type: ignore[arg-type]
        release.set()
        await writer

        row = await app.storage.get_session(superseded_id)
        assert row["metadata"]["auto_ended"] is True
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_clean_session_end_leaves_no_incomplete_flag(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="an ordinary written fact", ctx=ctx)  # type: ignore[arg-type]
        out = await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        assert "writes still in flight" not in out
        row = await app.storage.get_session(session_id)
        assert "provenance_incomplete" not in row["metadata"]


class TestImpreciseWritesFlagTheSession:
    """An append re-indexes the whole file, so its ``new_chunk_ids`` do
    not always mean "exactly what this call contributed"."""

    @pytest.mark.asyncio
    async def test_an_append_that_rechunks_earlier_content_marks_the_session(
        self, bm25_only_components
    ):
        """Appending under a heading a previous session already wrote to
        merges the two into one chunk with a new id.

        That id lands in this session's provenance, so its record names
        content it did not author. Attributing precisely would mean
        tracking the appended span through chunking; until then the
        session says its record is not exact.
        """
        from uuid import UUID

        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        await mem_add(content="ALPHAMARKER", title="Shared", file="shared.md", ctx=ctx)  # type: ignore[arg-type]
        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        await mem_session_start(agent_id="beta", ctx=ctx)  # type: ignore[arg-type]
        beta_id = app.current_session_id
        await mem_add(content="BETAMARKER", title="Shared", file="shared.md", ctx=ctx)  # type: ignore[arg-type]

        # The leak this guards against is real, so assert it is present —
        # if the chunker stops merging, this test should be revisited
        # rather than silently passing for the wrong reason.
        recorded = (await _provenance_events(app, beta_id))[0]["chunk_ids"]
        bodies = [(await app.storage.get_chunk(UUID(c))).content for c in recorded]
        assert any("ALPHAMARKER" in b for b in bodies), (
            "expected the re-chunk to absorb the earlier session's text"
        )

        row = await app.storage.get_session(beta_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_append_to_fresh_material_stays_complete(self, bm25_only_components):
        """The signal has to discriminate, or every session falls back
        and the flag stops meaning anything."""
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="ONE", title="First Heading", file="a.md", ctx=ctx)  # type: ignore[arg-type]
        await mem_add(content="TWO", title="Second Heading", file="b.md", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert "provenance_incomplete" not in row["metadata"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_indexing_error_reported_in_stats_marks_the_session(
        self, bm25_only_components
    ):
        """Indexing reports some failures by returning them rather than
        raising. The append is already durable by then, so the content
        exists, belongs to no event, and gets picked up by the watcher
        later — outside the session."""
        from memtomem.models import IndexingStats
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id

        async def soft_failure(*args, **kwargs):
            return IndexingStats(
                total_files=1,
                total_chunks=0,
                indexed_chunks=0,
                skipped_chunks=0,
                deleted_chunks=0,
                duration_ms=1.0,
                errors=("embedding provider unavailable",),
            )

        app.index_engine.index_file = soft_failure  # type: ignore[method-assign]

        await mem_add(content="content that never got embedded", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_indexing_exception_marks_the_session_before_propagating(
        self, bm25_only_components
    ):
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id

        async def boom(*args, **kwargs):
            raise RuntimeError("indexer exploded")

        app.index_engine.index_file = boom  # type: ignore[method-assign]

        # ``@tool_handler`` turns the raise into an error string; the
        # append already landed either way.
        await mem_add(content="content whose indexing blew up", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]


class TestMaintenanceToolsFlagTheSession:
    @pytest.mark.asyncio
    async def test_a_decay_expire_marks_the_session_incomplete(self, bm25_only_components):
        """Found by the AST guard once it learned to follow helpers —
        ``mem_decay_expire`` deletes through a service, not storage."""
        from memtomem.server.tools.dedup_decay import mem_decay_expire
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact old enough to expire", ctx=ctx)  # type: ignore[arg-type]

        # max_age_days below any real age ⇒ everything expires.
        await mem_decay_expire(max_age_days=0.0000001, dry_run=False, ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_dry_run_expiry_flags_nothing(self, bm25_only_components):
        from memtomem.server.tools.dedup_decay import mem_decay_expire
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact that survives the preview", ctx=ctx)  # type: ignore[arg-type]

        await mem_decay_expire(max_age_days=0.0000001, dry_run=True, ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert "provenance_incomplete" not in row["metadata"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]


class TestMutationSurfacesFlagTheSession:
    """``mem_edit`` / ``mem_delete`` record no event but must not be
    invisible — see ``_flag_mutation_on_active_session``."""

    @pytest.mark.asyncio
    async def test_mem_edit_marks_the_session_incomplete_without_an_event(
        self, bm25_only_components
    ):
        from memtomem.server.tools.memory_crud import mem_add, mem_edit
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="the original wording of the note", ctx=ctx)  # type: ignore[arg-type]
        chunk_id = (await _provenance_events(app, session_id))[0]["chunk_ids"][0]

        await mem_edit(chunk_id=chunk_id, new_content="the revised wording", ctx=ctx)  # type: ignore[arg-type]

        # No second event: an edit's chunk ids are re-chunk artifacts.
        assert [e["event_type"] for e in await _provenance_events(app, session_id)] == ["add"]
        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_bulk_namespace_delete_marks_the_session_incomplete(self, bm25_only_components):
        """``mem_delete(namespace=...)`` bypasses the chunk branch
        entirely, so it needs its own marker — it removes chunks an
        earlier provenance event still names."""
        from memtomem.server.tools.memory_crud import mem_add, mem_delete
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact that will be bulk-deleted", ctx=ctx)  # type: ignore[arg-type]
        row = await app.storage.get_session(session_id)
        assert "provenance_incomplete" not in row["metadata"]

        await mem_delete(namespace="agent-runtime:planner", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_bulk_delete_that_removed_nothing_flags_nothing(self, bm25_only_components):
        """The flag has to mean something, so it fires on an actual
        change rather than on the attempt."""
        from memtomem.server.tools.memory_crud import mem_add, mem_delete
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact that survives", ctx=ctx)  # type: ignore[arg-type]

        await mem_delete(namespace="some-empty-namespace", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert "provenance_incomplete" not in row["metadata"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_namespace_tool_delete_marks_the_session_incomplete(self, bm25_only_components):
        """``mem_ns_delete`` is a separate tool from ``mem_delete`` and
        was missed by the original seven-surface list — the AST guard
        found it."""
        from memtomem.server.tools.memory_crud import mem_add
        from memtomem.server.tools.namespace import mem_ns_delete
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact in the planner namespace", ctx=ctx)  # type: ignore[arg-type]

        await mem_ns_delete(namespace="agent-runtime:planner", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_source_file_delete_marks_the_session_incomplete(self, bm25_only_components):
        """The ``source_file`` branch is a third path through
        ``mem_delete``, separate from both the chunk branch and the
        namespace one, and it was flagging nothing."""
        from memtomem.server.tools.memory_crud import mem_add, mem_delete
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="a fact in its own file", file="doomed.md", ctx=ctx)  # type: ignore[arg-type]

        await mem_delete(source_file=str(mem_dir / "doomed.md"), ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance_incomplete"] is True

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_mutation_is_attributed_to_the_session_it_started_in(
        self, bm25_only_components, monkeypatch
    ):
        """The flag must name the session live when the edit was issued.

        Reading the handle after the re-index instead would let a session
        that ended meanwhile lose the flag entirely and one that started
        meanwhile inherit a mutation from its predecessor — the same
        attribution mistake this whole issue exists to fix.
        """
        from memtomem.server.tools.memory_crud import mem_add, mem_edit
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="alpha", ctx=ctx)  # type: ignore[arg-type]
        first_id = app.current_session_id
        await mem_add(content="the original wording", ctx=ctx)  # type: ignore[arg-type]
        chunk_id = (await _provenance_events(app, first_id))[0]["chunk_ids"][0]

        # Swap the session out from under the edit, mid-re-index.
        real_index_file = app.index_engine.index_file
        swapped = {"done": False}

        async def swapping_index_file(*args, **kwargs):
            if not swapped["done"]:
                swapped["done"] = True
                await app.storage.create_session("successor", "beta", "default")
                async with app._session_lock:
                    app.current_session_id = "successor"
            return await real_index_file(*args, **kwargs)

        monkeypatch.setattr(app.index_engine, "index_file", swapping_index_file)

        await mem_edit(chunk_id=chunk_id, new_content="the revised wording", ctx=ctx)  # type: ignore[arg-type]

        assert (await app.storage.get_session(first_id))["metadata"][
            "provenance_incomplete"
        ] is True
        successor = await app.storage.get_session("successor")
        assert "provenance_incomplete" not in successor["metadata"], (
            "the successor session inherited a mutation that happened in its predecessor"
        )

        async with app._session_lock:
            app.current_session_id = first_id
        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_a_mutation_outside_any_session_flags_nothing(self, bm25_only_components):
        from memtomem.server.tools.memory_crud import mem_add, mem_edit
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_add(content="the original wording of the note", ctx=ctx)  # type: ignore[arg-type]
        chunk_id = (await _provenance_events(app, session_id))[0]["chunk_ids"][0]
        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        await mem_edit(chunk_id=chunk_id, new_content="edited with no session", ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert "provenance_incomplete" not in row["metadata"]


class TestSessionRowMarker:
    """``mem_session_start`` marks its session as provenance-recording.

    The marker is what lets a consumer distinguish a session whose real
    inputs it can read from a legacy one it must infer. Sessions created
    by the CLI or the LangGraph adapter carry no marker and keep the old
    behavior.
    """

    @pytest.mark.asyncio
    async def test_a_started_session_carries_the_marker(self, bm25_only_components):
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        row = await app.storage.get_session(app.current_session_id)

        assert row["metadata"]["provenance"] == PROVENANCE_KIND
        # The marker asserts a mechanism, not completeness — a fresh
        # session has lost nothing and must not look like it has.
        assert "provenance_incomplete" not in row["metadata"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_marker_does_not_displace_the_title(self, bm25_only_components):
        """``mem_session_list`` reads ``title`` out of the same document."""
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", title="Sprint 7", ctx=ctx)  # type: ignore[arg-type]
        row = await app.storage.get_session(app.current_session_id)

        assert row["metadata"] == {"provenance": PROVENANCE_KIND, "title": "Sprint 7"}

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_the_marker_survives_session_end(self, bm25_only_components):
        """``end_session`` merges rather than replaces, so the marker is
        still there when a consumer reads the closed row."""
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

        row = await app.storage.get_session(session_id)
        assert row["metadata"]["provenance"] == PROVENANCE_KIND
        assert "event_counts" in row["metadata"]

    @pytest.mark.asyncio
    async def test_a_cli_created_session_carries_no_marker(self, components):
        """Producers other than ``mem_session_start`` stay unmarked, so a
        consumer keeps inferring their inputs the old way rather than
        trusting provenance that was never recorded."""
        app = AppContext.from_components(components)
        await app.storage.create_session("cli-session", "planner", "default", {"title": "x"})

        row = await app.storage.get_session("cli-session")
        assert "provenance" not in row["metadata"]


class TestDirectEngineSurfaces:
    """The three surfaces that call the indexing engine directly rather
    than through ``_mem_add_core``."""

    @pytest.mark.asyncio
    async def test_mem_batch_add_records_one_event_for_the_whole_batch(self, bm25_only_components):
        """One event per *call*, not per entry — ``event_counts`` is a
        count of logical writes and six surfaces render it."""
        from memtomem.server.tools.memory_crud import mem_batch_add
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, _ = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_batch_add(  # type: ignore[arg-type]
            entries=[{"content": f"batched fact number {i}"} for i in range(5)],
            ctx=ctx,
        )

        events = await _provenance_events(app, session_id)
        assert [e["event_type"] for e in events] == ["batch_add"]
        assert len(events[0]["chunk_ids"]) >= 1

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mem_index_records_an_index_event(self, bm25_only_components):
        from memtomem.server.tools.indexing import mem_index
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        (mem_dir / "indexed.md").write_text("# Heading\n\nSomething worth indexing.\n")

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_index(path=str(mem_dir), ctx=ctx)  # type: ignore[arg-type]

        events = await _provenance_events(app, session_id)
        assert [e["event_type"] for e in events] == ["index"]

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_an_unchanged_reindex_records_no_second_event(self, bm25_only_components):
        """``new_chunk_ids`` is only the genuinely-new upserts, so a
        re-index that changed nothing has nothing to record."""
        from memtomem.server.tools.indexing import mem_index
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        (mem_dir / "indexed.md").write_text("# Heading\n\nSomething worth indexing.\n")

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_index(path=str(mem_dir), ctx=ctx)  # type: ignore[arg-type]
        await mem_index(path=str(mem_dir), ctx=ctx)  # type: ignore[arg-type]

        assert len(await _provenance_events(app, session_id)) == 1

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_mem_index_event_names_no_path(self, bm25_only_components):
        """``scan_session_candidates`` copies event content verbatim into
        a review candidate and the web API renders it, so a resolved path
        here would leak through both."""
        from memtomem.server.tools.indexing import mem_index
        from memtomem.server.tools.session import mem_session_end, mem_session_start

        comp, mem_dir = bm25_only_components
        app = AppContext.from_components(comp)
        ctx = _StubCtx(app)
        (mem_dir / "secret-looking-dir-name.md").write_text("# H\n\nBody text here.\n")

        await mem_session_start(agent_id="planner", ctx=ctx)  # type: ignore[arg-type]
        session_id = app.current_session_id
        await mem_index(path=str(mem_dir), ctx=ctx)  # type: ignore[arg-type]

        content = (await _provenance_events(app, session_id))[0]["content"]
        assert str(mem_dir) not in content
        assert "secret-looking-dir-name" not in content
        assert "/" not in content

        await mem_session_end(ctx=ctx)  # type: ignore[arg-type]
