"""Regression: handoff resume must stay target-aware past the result cap.

The handoff workflow's resume step originally called ``mem_recall`` with
``limit=10`` and only then filtered by recipient, so newer handoffs
addressed to other runtimes made a valid older record for the current
runtime look nonexistent.

Two later attempts were also unsound, and each is pinned below:

* ``mem_search`` tag filters — the pipeline applies the tag filter *after*
  the ranked candidate pool is capped (CLAUDE.md stage order), so enough
  higher-ranked wrong-target records still evict the valid one.
* Separate ``handoff`` / ``to-<runtime>`` tags — ``tag_filter`` matches ANY
  of the tags it is given, so ``to-<runtime>,to-any`` also admits
  non-handoff memories that happen to live in this shared namespace.

Resume therefore uses ``mem_recall`` with *composite* tags that carry both
facts in one string: ``handoff-to-<runtime>`` for the newest-for-me query
and ``handoff-id-<id>`` for an exact record. ``mem_recall`` filters in SQL
before ``limit`` and orders newest-first with a total tie-break, so the
selection is decidable from one bounded page.

``test_plugin_assets`` pins the workflow text that instructs all of this.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID

from helpers import make_chunk

_NAMESPACE = "shared:proj"
_TARGET_ID = "9b1de644-0f3a-4c52-8abc-6f2f6f6a7d01"

# More wrong-target records than any cap the workflow uses (limit=10, and
# the earlier mem_search attempt's top_k=20).
_DECOY_COUNT = 25
# Exceeds the largest page the earlier "just raise the limit" fix suggested,
# so an id lookup that pages instead of filtering would report a false miss.
_MANY_ELIGIBLE = 60

# Fixed timestamps — no wall-clock boundaries. The target is the OLDEST row.
_TARGET_AT = datetime(2026, 1, 1, tzinfo=timezone.utc)
_DECOY_BASE = datetime(2026, 2, 1, tzinfo=timezone.utc)


def _handoff_chunk(handoff_id: str, to_runtime: str, created_at: datetime):
    """A handoff record tagged the way the Save step tags one."""
    chunk = make_chunk(
        content=(
            f"handoff_id: {handoff_id}\n"
            "from_runtime: codex-cli\n"
            f"to_runtime: {to_runtime}\n"
            "objective: continue the handoff migration work\n"
        ),
        tags=(
            "handoff",
            "from-codex-cli",
            f"to-{to_runtime}",
            f"handoff-to-{to_runtime}",
            f"handoff-id-{handoff_id}",
        ),
        namespace=_NAMESPACE,
        source=f"handoff-{handoff_id[:8]}.md",
    )
    chunk.created_at = created_at
    chunk.updated_at = created_at
    return chunk


def _recipient_tagged_non_handoff(name: str, to_runtime: str, created_at: datetime):
    """A NON-handoff memory that happens to carry the bare recipient tag.

    The shared namespace is general-purpose, so this is a realistic
    neighbor — and it is exactly what a bare ``to-<runtime>`` filter would
    wrongly admit.
    """
    chunk = make_chunk(
        content=f"note: {name}\n",
        tags=("note", f"to-{to_runtime}"),
        namespace=_NAMESPACE,
        source=f"note-{name}.md",
    )
    chunk.created_at = created_at
    chunk.updated_at = created_at
    return chunk


async def _seed_mixed_targets(storage):
    """One old claude-code handoff buried under newer codex-cli handoffs."""
    target = _handoff_chunk(_TARGET_ID, "claude-code", _TARGET_AT)
    decoys = [
        _handoff_chunk(
            f"00000000-0000-4000-8000-0000000000{i:02d}",
            "codex-cli",
            _DECOY_BASE + timedelta(minutes=i),
        )
        for i in range(_DECOY_COUNT)
    ]
    await storage.upsert_chunks([target, *decoys])
    return target


async def test_untagged_recall_page_buries_the_target(components) -> None:
    """The original failure shape: newest-first page holds only decoys."""
    target = await _seed_mixed_targets(components.storage)

    recalled = await components.storage.recall_chunks(limit=10)

    assert target.id not in {c.id for c in recalled}


async def test_composite_tag_survives_more_decoys_than_the_limit(components) -> None:
    """SQL-level tag filter runs before ``limit``, so the target survives."""
    target = await _seed_mixed_targets(components.storage)

    recalled = await components.storage.recall_chunks(
        limit=10, tag_filter="handoff-to-claude-code,handoff-to-any"
    )

    assert target.id in {c.id for c in recalled}
    for chunk in recalled:
        assert {"handoff-to-claude-code", "handoff-to-any"} & set(chunk.metadata.tags)


async def test_composite_tag_excludes_recipient_tagged_non_handoffs(components) -> None:
    """``handoff-to-x`` carries both facts; a bare ``to-x`` would not.

    Pinned as a contrast so a future "simplify the tags" change has to
    confront why the composite exists.
    """
    target = await _seed_mixed_targets(components.storage)
    notes = [
        _recipient_tagged_non_handoff(f"n{i}", "claude-code", _DECOY_BASE + timedelta(hours=i))
        for i in range(15)
    ]
    await components.storage.upsert_chunks(notes)
    note_ids = {n.id for n in notes}

    composite = await components.storage.recall_chunks(
        limit=10, tag_filter="handoff-to-claude-code,handoff-to-any"
    )
    assert target.id in {c.id for c in composite}
    assert not note_ids & {c.id for c in composite}

    # The rejected alternative: bare recipient tags fill the page with notes
    # and bury the handoff again.
    bare = await components.storage.recall_chunks(limit=10, tag_filter="to-claude-code,to-any")
    assert target.id not in {c.id for c in bare}


async def test_exact_id_tag_reaches_a_record_older_than_any_page(components) -> None:
    """``handoff-id-<id>`` is pre-limit, so age cannot hide a record."""
    target = await _seed_mixed_targets(components.storage)
    extra = [
        _handoff_chunk(
            f"11111111-0000-4000-8000-0000000000{i:02d}",
            "claude-code",
            _DECOY_BASE + timedelta(days=1, minutes=i),
        )
        for i in range(_MANY_ELIGIBLE)
    ]
    await components.storage.upsert_chunks(extra)

    found = await components.storage.recall_chunks(limit=1, tag_filter=f"handoff-id-{_TARGET_ID}")

    assert [c.id for c in found] == [target.id]


async def test_equal_timestamp_rows_order_deterministically(components) -> None:
    """Same-second rows must not flip across calls (#516).

    ``created_at`` has second precision, so a page boundary over tied rows
    was previously decided by whatever order SQLite returned.
    """
    tied = [
        _handoff_chunk(f"22222222-0000-4000-8000-0000000000{i:02d}", "claude-code", _TARGET_AT)
        for i in range(20)
    ]
    await components.storage.upsert_chunks(tied)

    pages = [
        [c.id for c in await components.storage.recall_chunks(limit=5, tag_filter="handoff")]
        for _ in range(3)
    ]

    assert pages[0] == pages[1] == pages[2]
    # The documented rule is "largest id first" among tied rows.
    assert pages[0] == sorted((c.id for c in tied), key=lambda u: str(u), reverse=True)[:5]


async def test_microsecond_precision_beats_uuid_ordering(components) -> None:
    """A same-second re-save must win on recency, not on UUID luck.

    ``created_at`` is stored at microsecond precision, so the corrected
    record written microseconds after the stale one sorts first even when
    the stale record's UUID is lexicographically larger — before this,
    same-second rows fell through to ``id DESC`` and the stale record won
    deterministically for ~half of all UUID pairs.
    """
    # Stale record: larger UUID ("ff..."), earlier microsecond.
    stale = _handoff_chunk(
        "ffffffff-0000-4000-8000-000000000001",
        "claude-code",
        _TARGET_AT.replace(microsecond=100),
    )
    # Corrected re-save: smaller UUID ("00..."), later microsecond.
    corrected = _handoff_chunk(
        "00000000-0000-4000-8000-0000000000ff",
        "claude-code",
        _TARGET_AT.replace(microsecond=200),
    )
    await components.storage.upsert_chunks([stale, corrected])

    rows = await components.storage.recall_chunks(limit=5, tag_filter="handoff-to-claude-code")

    assert rows[0].id == corrected.id, "newest-by-microsecond must beat larger-UUID stale row"


async def test_legacy_second_precision_rows_sort_older_than_fractional(components) -> None:
    """Rows written by pre-microsecond releases coexist with new rows.

    A legacy row carries no fractional part (``...T00:00:00+00:00``); a new
    row in the same second carries one (``...T00:00:00.000001+00:00``).
    Lexicographic DESC must rank the fractional (newer) row first — the
    fraction only extends the prefix, and ``.`` sorts after ``+``.
    """
    import sqlite3

    legacy = _handoff_chunk("aaaaaaaa-0000-4000-8000-000000000001", "claude-code", _TARGET_AT)
    newer = _handoff_chunk(
        "00000000-0000-4000-8000-000000000002",
        "claude-code",
        _TARGET_AT.replace(microsecond=1),
    )
    await components.storage.upsert_chunks([legacy, newer])

    # Rewrite the legacy row exactly as an old release stored it:
    # second precision, no fractional part.
    conn = sqlite3.connect(str(components.config.storage.sqlite_path))
    try:
        conn.execute(
            "UPDATE chunks SET created_at = ? WHERE id = ?",
            (_TARGET_AT.isoformat(timespec="seconds"), str(legacy.id)),
        )
        conn.commit()
    finally:
        conn.close()

    rows = await components.storage.recall_chunks(limit=5, tag_filter="handoff-to-claude-code")

    assert [r.id for r in rows[:2]] == [newer.id, legacy.id], (
        "fractional-precision row in the same second must sort newer than a legacy row"
    )


async def test_mem_recall_structured_exposes_created_at_and_orders_newest_first(
    components, monkeypatch
) -> None:
    """The public tool payload carries what the workflow selects on."""
    from memtomem.server.tools import recall as recall_tool

    target = await _seed_mixed_targets(components.storage)
    # A SECOND eligible row, newer than the target, so the ordering
    # assertion is not vacuous on a single-row result.
    newer = _handoff_chunk(
        "33333333-0000-4000-8000-000000000001",
        "claude-code",
        _DECOY_BASE + timedelta(days=2),
    )
    await components.storage.upsert_chunks([newer])

    async def _fake_app(_ctx):
        return components

    monkeypatch.setattr(recall_tool, "_get_app_initialized", _fake_app)
    payload = json.loads(
        await recall_tool.mem_recall(
            tag_filter="handoff-to-claude-code,handoff-to-any",
            namespace=_NAMESPACE,
            limit=10,
            output_format="structured",
        )
    )

    rows = payload["results"]
    ids = [row["chunk_id"] for row in rows]
    assert {str(target.id), str(newer.id)} <= set(ids)
    assert len(ids) >= 2, "ordering assertion needs at least two eligible rows"
    for row in rows:
        assert row["created_at"]
    timestamps = [row["created_at"] for row in rows]
    assert timestamps == sorted(timestamps, reverse=True)
    # "Select the first row" must land on the newest eligible record.
    assert ids[0] == str(newer.id)


async def test_search_applies_tag_filter_after_the_candidate_cap() -> None:
    """Pin WHY resume does not use ``mem_search``: its tag filter is post-cap.

    Driven by mocked retrieval so the outcome depends on the pipeline's
    stage order rather than on which retrieval legs the ambient config
    happens to wire up — an earlier version of this test asserted absence
    against real retrieval and was green file-locally while red in the full
    suite. Here BM25 returns wrong-target rows ahead of the eligible one;
    if the tag filter ever moves before the cap, the eligible row survives
    and this test fails loudly.
    """
    from memtomem.config import SearchConfig
    from memtomem.models import SearchResult
    from memtomem.search.pipeline import SearchPipeline

    eligible = _handoff_chunk(_TARGET_ID, "claude-code", _TARGET_AT)
    wrong = [
        _handoff_chunk(f"44444444-0000-4000-8000-0000000000{i:02d}", "codex-cli", _DECOY_BASE)
        for i in range(5)
    ]
    ranked = [
        SearchResult(chunk=chunk, score=1.0 / (rank + 1), rank=rank + 1, source="bm25")
        for rank, chunk in enumerate([*wrong, eligible])
    ]

    storage = AsyncMock()
    storage.bm25_search = AsyncMock(return_value=ranked)
    storage.dense_search = AsyncMock(return_value=[])
    storage.increment_access = AsyncMock()
    storage.save_query_history = AsyncMock()
    storage.get_access_counts = AsyncMock(return_value={})
    storage.get_embeddings_for_chunks = AsyncMock(return_value={})
    storage.get_importance_scores = AsyncMock(return_value={})
    storage.count_chunks_by_ns_prefix = AsyncMock(return_value=0)
    storage.recall_chunks = AsyncMock(return_value=[])
    pipeline = SearchPipeline(
        storage=storage,
        embedder=None,
        config=SearchConfig(enable_bm25=True, enable_dense=False),
        reranker=None,
    )

    results, _ = await pipeline.search(
        query="handoff",
        top_k=3,
        tag_filter="handoff-to-claude-code,handoff-to-any",
    )

    assert eligible.id not in {r.chunk.id for r in results}, (
        "mem_search filtered before the candidate cap — re-read "
        "packages/memtomem-plugin-assets/workflows/handoff.md before "
        "changing this test"
    )


def test_handoff_id_tag_round_trips_through_the_writer(tmp_path) -> None:
    """UUID-bearing tags must survive the ``> tags:`` blockquote writer."""
    from memtomem.chunking.markdown import MarkdownChunker
    from memtomem.tools.memory_writer import append_entry

    target = tmp_path / "handoff.md"
    tag = f"handoff-id-{_TARGET_ID}"
    append_entry(target, "objective: x\n", title="Handoff", tags=["handoff", tag])

    chunks = MarkdownChunker().chunk_file(target, target.read_text(encoding="utf-8"))

    assert any(tag in chunk.metadata.tags for chunk in chunks), (
        "hyphenated UUID tag did not survive the writer/chunker round trip"
    )
    # Sanity: the tag is a plain UUID string, not a coincidence of parsing.
    UUID(_TARGET_ID)


def _max_size_record(*, fenced: bool) -> str:
    """A record at the workflow's 1,200-character ceiling."""
    body = (
        f"handoff_id: {_TARGET_ID}\n"
        "from_runtime: claude-code\n"
        "to_runtime: codex-cli\n"
        "project_root: /Users/x/work/project\n"
        "objective: " + "finish the migration and verify gateway paths " * 2 + "\n"
        "completed: " + "moved fan-out to the new home, added guards, " * 4 + "\n"
        "changed_files: " + ", ".join(f"src/mod_{i}.py" for i in range(10)) + "\n"
        f"git_head: {'a1b2c3d4' * 5}\n"
        "worktree_state: 3 dirty: M=2 ??=1; first=src/mod_0.py\n"
        "validation: full suite green; ruff clean\n"
        "blockers: none\n"
        "next_action: open the PR after a final re-gate\n"
    )
    if not fenced:
        return body
    padding = 1200 - len(body) - len("```text\n```\n") - len("notes: \n")
    return "```text\n" + body + "notes: " + "x" * padding + "\n```\n"


def _chunk_at_min_size(tmp_path, record: str):
    from memtomem.chunking.markdown import MarkdownChunker
    from memtomem.config import IndexingConfig
    from memtomem.tools.memory_writer import append_entry

    target = tmp_path / "handoff.md"
    append_entry(
        target,
        record,
        title=f"Handoff {_TARGET_ID}",
        tags=["handoff", "handoff-to-codex-cli", f"handoff-id-{_TARGET_ID}"],
    )
    # The minimum ``mm config set indexing.max_chunk_tokens`` accepts.
    config = IndexingConfig(max_chunk_tokens=64, min_chunk_tokens=64, target_chunk_tokens=64)
    chunks = MarkdownChunker(config).chunk_file(target, target.read_text(encoding="utf-8"))
    return [c for c in chunks if f"handoff-id-{_TARGET_ID}" in c.metadata.tags]


def test_fenced_record_stays_one_chunk_at_the_minimum_chunk_size(tmp_path) -> None:
    """The fence is what makes ``limit``-bounded resume sound.

    ``indexing.max_chunk_tokens`` is user-settable down to 64, well under a
    full-size record. A fenced block is atomic to the chunker, so the record
    survives whole; unfenced it splits and a lookup can land on a fragment
    (pinned by the sibling test below).
    """
    tagged = _chunk_at_min_size(tmp_path, _max_size_record(fenced=True))

    assert len(tagged) == 1
    assert "handoff_id:" in tagged[0].content
    assert "next_action:" in tagged[0].content


def test_unfenced_record_splits_and_union_recovers_short_field_lines(tmp_path) -> None:
    """Why resume parses the UNION of rows rather than one row.

    Records written before the fence rule are still out there. They split at
    the minimum chunk size and every fragment inherits the tags, so a
    single-row read misses most fields. When every field line is shorter
    than the chunk budget the split lands on line boundaries and the union
    is a complete recovery — but that is a property of *these* values, not a
    guarantee; see the torn-line test below for the case it does not cover.
    """
    tagged = _chunk_at_min_size(tmp_path, _max_size_record(fenced=False))

    assert len(tagged) > 1, "expected the unfenced record to split at 64 tokens"
    assert sum("handoff_id:" in c.content for c in tagged) == 1, (
        "a single-row read would miss the id on most fragments"
    )
    union = "\n".join(c.content for c in tagged)
    for field in (
        "handoff_id:",
        "to_runtime:",
        "project_root:",
        "git_head:",
        "worktree_state:",
        "next_action:",
    ):
        assert field in union, f"{field} lost — union parsing cannot recover the record"


def test_legacy_record_with_a_long_value_tears_a_field_line(tmp_path) -> None:
    """The case union-parsing CANNOT recover, which resume must detect.

    ``project_root`` carries a real absolute path and has no length cap, so
    on a legacy (unfenced) record with a small ``max_chunk_tokens`` the
    chunker splits mid-value: a row begins in the middle of the path instead
    of at a ``<field>:`` key. Resume is required to report such a record as
    torn — reconstructing it would mean guessing where the halves join.
    """
    long_root = "/Users/x/" + "/".join(f"deeply_nested_dir_{i}" for i in range(15))
    record = (
        f"handoff_id: {_TARGET_ID}\n"
        "from_runtime: claude-code\n"
        "to_runtime: codex-cli\n"
        f"project_root: {long_root}\n"
        "objective: finish it\n"
        "completed: did things\n"
        "changed_files: a.py\n"
        f"git_head: {'a1b2c3d4' * 5}\n"
        "worktree_state: clean\n"
        "validation: green\n"
        "blockers: none\n"
        "next_action: open the PR\n"
    )

    tagged = _chunk_at_min_size(tmp_path, record)

    assert len(tagged) > 1
    union = "\n".join(c.content for c in tagged)
    assert f"project_root: {long_root}" not in union, (
        "long value no longer tears — re-check whether resume still needs the torn-field rule"
    )
    # The detectable signature resume keys off: a row that starts mid-value
    # rather than at a known field key.
    known_keys = ("handoff_id:", "from_runtime:", "to_runtime:", "project_root:", "objective:")
    assert any(not row.content.lstrip().startswith(known_keys) for row in tagged)


def test_fencing_prevents_the_torn_field_line(tmp_path) -> None:
    """The Save-side fix for the case above: fenced, the long value survives."""
    long_root = "/Users/x/" + "/".join(f"deeply_nested_dir_{i}" for i in range(15))
    record = (
        "```text\n"
        f"handoff_id: {_TARGET_ID}\n"
        "from_runtime: claude-code\n"
        "to_runtime: codex-cli\n"
        f"project_root: {long_root}\n"
        "next_action: open the PR\n"
        "```\n"
    )

    tagged = _chunk_at_min_size(tmp_path, record)

    assert len(tagged) == 1
    assert f"project_root: {long_root}" in tagged[0].content
