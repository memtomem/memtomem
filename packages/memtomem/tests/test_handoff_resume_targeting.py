"""Regression: handoff resume must stay target-aware past the result cap.

The handoff workflow's resume step originally called ``mem_recall`` with
``limit=10`` and only then filtered by recipient, so newer handoffs
addressed to other runtimes made a valid older record for the current
runtime look nonexistent.

Two later attempts were also unsound, and each is pinned below:

* ``mem_search`` tag filters — resume still must not use them, but the
  reason changed with #2191. The pipeline used to apply the tag filter
  *after* the ranked candidate pool was capped, so higher-ranked
  wrong-target records evicted the valid one outright; the filter now runs
  at retrieval, and the pin below asserts that. What remains disqualifying
  is the ordering: search returns the most *relevant* eligible record, not
  the newest, and its dense leg only sees tags inside its KNN pool.
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
import os
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
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


async def test_search_applies_tag_filter_before_the_candidate_cap() -> None:
    """#2191: the tag reaches retrieval, so the cap can no longer evict it.

    Driven by mocked retrieval so the outcome depends on the pipeline's
    stage order rather than on which retrieval legs the ambient config
    happens to wire up — an earlier version of this test asserted absence
    against real retrieval and was green file-locally while red in the full
    suite. The BM25 mock applies ``tags_any`` the way the SQL does; without
    that it would return the wrong-target rows too and the eligible row
    would fall outside ``top_k`` for the wrong reason, leaving this
    regression green whatever the pipeline did.
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

    bm25_calls: list[object] = []

    async def _bm25(*_args, **kwargs):
        # Stand in for ``_metadata_filter_sql``: the tag predicate selects
        # candidates, it does not trim them afterwards.
        metadata_filter = kwargs.get("metadata_filter")
        bm25_calls.append(metadata_filter)
        wanted = set(getattr(metadata_filter, "tags_any", ()) or ())
        if not wanted:
            return ranked
        return [r for r in ranked if wanted & set(r.chunk.metadata.tags)]

    storage = AsyncMock()
    storage.bm25_search = _bm25
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

    assert bm25_calls and bm25_calls[0] is not None, "tag filter never reached retrieval"
    assert bm25_calls[0].tags_any == (
        "handoff-to-any",
        "handoff-to-claude-code",
    )
    assert eligible.id in {r.chunk.id for r in results}, (
        "the tag filter stopped selecting before the candidate cap — resume "
        "still uses mem_recall for newest-first ordering, but re-read "
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
    """The fence keeps the character-based markdown pass from splitting a record.

    ``indexing.max_chunk_tokens`` is user-settable down to 64, well under a
    full-size record. A fenced block is atomic to the markdown chunker, so the
    record survives that pass whole; unfenced it splits and a lookup can land
    on a fragment (pinned by the sibling test below). This config has no exact
    token budget; the E5 token pass, which does split a fenced record, is
    pinned by the ``e5_budget`` tests at the end of this file.
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
    """The Save-side fix for the case above, on the character-based pass.

    Fenced, the long value survives the markdown chunker. Under an exact token
    budget a single line over that budget is torn even when fenced; see
    ``test_normalization_expanding_line_is_torn_and_detectable``.
    """
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


# --- The E5 token pass (#2814) ---------------------------------------------
#
# The tests above use a config with no exact token budget, so only the
# character-based markdown pass runs and the fence keeps a record whole. The
# E5 profile adds ``bound_chunks``, which re-splits any chunk over
# ``hard_max_chunk_tokens`` (384) and does not exempt a fence. These tests pin
# what the workflow says about that pass, against the pinned tokenizer.

_FIELDS = (
    "handoff_id",
    "from_runtime",
    "to_runtime",
    "project_root",
    "objective",
    "completed",
    "changed_files",
    "git_head",
    "worktree_state",
    "validation",
    "blockers",
    "next_action",
)
_FASTEMBED_CACHE_ENV = ("MEMTOMEM_FASTEMBED_CACHE", "FASTEMBED_CACHE_PATH")


def _verified_cached_e5_tokenizer() -> str | None:
    """A copy of the pinned tokenizer already in a fastembed cache, read only.

    Looks where the product would (the non-empty overrides, then the default
    ``~/.memtomem/cache/fastembed``) without creating or writing anything, and
    returns a file only when its checksum is the pinned one.
    """
    import hashlib
    from pathlib import Path

    from memtomem.embedding.profiles import E5_MODEL, E5_REVISION, E5_TOKENIZER_SHA256

    roots = [os.environ[name] for name in _FASTEMBED_CACHE_ENV if os.environ.get(name)]
    roots.append(str(Path.home() / ".memtomem" / "cache" / "fastembed"))
    snapshot = Path("models--" + E5_MODEL.replace("/", "--"), "snapshots", E5_REVISION)
    for root in roots:
        candidate = Path(root).expanduser() / snapshot / "tokenizer.json"
        if not candidate.is_file():
            continue
        with candidate.open("rb") as stream:
            if hashlib.file_digest(stream, "sha256").hexdigest() == E5_TOKENIZER_SHA256:
                return str(candidate)
    return None


@pytest.fixture(scope="session")
def _e5_tokenizer_file(tmp_path_factory) -> str:
    """The pinned E5 ``tokenizer.json``, checksum-verified, as a plain path.

    Reuses a verified copy from an existing cache without writing to it;
    otherwise downloads into a temporary cache, never the developer's. Skips
    only when the file cannot be fetched. A checksum mismatch on the download
    (``ValueError``) is a real failure and propagates.
    """
    pytest.importorskip("tokenizers")
    pytest.importorskip("huggingface_hub")
    httpx = pytest.importorskip("httpx")
    from huggingface_hub.errors import HfHubHTTPError, LocalEntryNotFoundError

    from memtomem.embedding.profiles import E5_TOKENIZER, resolve_tokenizer

    cached = _verified_cached_e5_tokenizer()
    if cached is not None:
        return cached
    with pytest.MonkeyPatch.context() as mp:
        # ``MEMTOMEM_FASTEMBED_CACHE`` outranks ``FASTEMBED_CACHE_PATH``, so
        # this alone keeps the download out of any cache the developer uses.
        mp.setenv("MEMTOMEM_FASTEMBED_CACHE", str(tmp_path_factory.mktemp("fastembed")))
        try:
            path = resolve_tokenizer(E5_TOKENIZER)
        except (HfHubHTTPError, LocalEntryNotFoundError, OSError, httpx.TransportError) as exc:
            pytest.skip(f"pinned E5 tokenizer unavailable: {type(exc).__name__}: {exc}")
    return str(path)


@pytest.fixture
def e5_budget(_e5_tokenizer_file):
    """The E5 profile's indexing budget and a ``TokenBudget`` over it.

    Built from the profile rather than copied numbers, then pointed at the
    already verified file so no later step downloads again.
    """
    from memtomem.chunking.bounded import TokenBudget
    from memtomem.config import EmbeddingConfig, Mem2MemConfig
    from memtomem.embedding.profiles import E5_TOKENIZER

    indexing = Mem2MemConfig(
        embedding=EmbeddingConfig(provider="onnx", model="multilingual-e5-small")
    ).indexing
    assert indexing.chunk_tokenizer_path == E5_TOKENIZER
    assert indexing.hard_max_chunk_tokens == 384
    config = indexing.model_copy(update={"chunk_tokenizer_path": _e5_tokenizer_file})
    return config, TokenBudget(config)


def _korean(length: int) -> str:
    """Korean prose of exactly *length* characters, the densest field text measured."""
    sentence = "마이그레이션을 마치고 게이트웨이 경로를 검증한 뒤 리뷰를 올린다 "
    return (sentence * (length // len(sentence) + 1))[:length]


def _fenced_record(**values: str) -> str:
    fields = {
        "handoff_id": _TARGET_ID,
        "from_runtime": "claude-code",
        "to_runtime": "codex-cli",
        "project_root": "/Users/x/work/project",
        "git_head": "a1b2c3d4" * 5,
        **values,
    }
    body = "".join(f"{key}: {fields.get(key, '')}\n" for key in _FIELDS)
    return "```text\n" + body + "```\n"


def _capped_korean_record() -> str:
    """Every capped field filled to its cap with Korean text."""
    return _fenced_record(
        objective=_korean(100),
        completed=_korean(240),
        changed_files=", ".join(f"src/pkg/모듈_{i}.py" for i in range(10)),
        worktree_state=_korean(80),
        validation=_korean(120),
        blockers=_korean(80),
        next_action=_korean(120),
    )


def _e5_rows(tmp_path, record: str, config):
    """The rows the engine stores: markdown pass, then the exact token pass.

    The engine's ``_merge_short_chunks`` and overlap stages sit between these
    two and only join chunks, so they cannot add a split.
    """
    from memtomem.chunking.bounded import bound_chunks
    from memtomem.chunking.markdown import MarkdownChunker
    from memtomem.tools.memory_writer import append_entry

    target = tmp_path / "handoff.md"
    append_entry(
        target,
        record,
        title=f"Handoff {_TARGET_ID}",
        tags=["handoff", "handoff-to-codex-cli", f"handoff-id-{_TARGET_ID}"],
    )
    chunks = MarkdownChunker(config).chunk_file(target, target.read_text(encoding="utf-8"))
    return bound_chunks(chunks, config)


_FENCE_LINES = ("```text", "```")


def _record_is_whole(contents: list[str]) -> bool:
    """Resume step 4: count the union's non-blank lines, in any row order.

    Save writes every field exactly once, so a whole record has one line per
    field key and either both fence lines once or neither. A torn value's
    continuation begins a row with whatever text came next: a line that is no
    field line, a second line for some field, or a second fence line. Each of
    those breaks a count, so no row needs to be judged by how it starts.
    """
    lines = [line for content in contents for line in content.splitlines() if line.strip()]
    fences = (lines.count("```text"), lines.count("```"))
    keys = []
    for line in lines:
        if line not in _FENCE_LINES:
            key, colon, _ = line.partition(":")
            # A line with no colon is no field line, even when it is a bare key.
            keys.append(key if colon else line + " (no colon)")
    return fences in ((1, 1), (0, 0)) and sorted(keys) == sorted(_FIELDS)


def _parse_fields(contents: list[str]) -> dict[str, str]:
    """Resume step 3: read fields per line from the union of rows, in any order."""
    assert _record_is_whole(contents), "torn record"
    fields: dict[str, str] = {}
    for content in contents:
        for line in content.splitlines():
            if line.strip() and line not in _FENCE_LINES:
                key, _, value = line.partition(":")
                fields[key] = value.removeprefix(" ")
    return fields


def test_e5_token_budget_splits_a_capped_record_between_field_lines(tmp_path, e5_budget):
    """A fenced record within every cap is more than one row on E5.

    The workflow used to say the fence kept it one chunk. It now says the
    token pass re-splits it at line boundaries and resume reads the union.
    """
    config, budget = e5_budget
    record = _capped_korean_record()
    assert len(record) <= 1200, "fixture left the workflow's 1,200-character ceiling"

    rows = _e5_rows(tmp_path, record, config)

    assert len(rows) > 1, "a capped Korean record no longer splits; re-check the workflow text"
    for row in rows:
        assert budget.count(row.content) <= config.hard_max_chunk_tokens
        assert f"handoff-id-{_TARGET_ID}" in row.metadata.tags
    contents = [row.content for row in rows]
    expected = _parse_fields([record])
    assert len(expected) == len(_FIELDS)
    assert _parse_fields(contents) == expected
    # mem_recall returns rows newest first, not in file order.
    assert _parse_fields(contents[::-1]) == expected


def test_a_last_row_can_hold_only_the_closing_fence(tmp_path, e5_budget):
    """A record just over the budget leaves the closing fence alone in a row.

    That row is legitimate, so the count must accept it.
    """
    config, budget = e5_budget
    record = _fenced_record(objective=_korean(100), completed=_korean(240), validation=_korean(103))

    rows = _e5_rows(tmp_path, record, config)

    assert [row.content for row in rows][-1:] == ["```"], "re-pick the padding"
    assert budget.count(rows[0].content) == config.hard_max_chunk_tokens
    assert _parse_fields([row.content for row in rows][::-1]) == _parse_fields([record])


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(_korean(240), id="korean-prose"),
        pytest.param(("/깊은디렉터리이름" * 30)[:240], id="korean-path"),
        pytest.param(("\U0001f469\u200d\U0001f469\u200d\U0001f467 " * 60)[:240], id="zwj-emoji"),
        pytest.param(("a1b2c3d4e5f6" * 20)[:240], id="hex"),
        pytest.param(("!@#$%^&*()_+-=[]{}|;':\",./<>?" * 9)[:240], id="punctuation"),
    ],
)
def test_capped_ordinary_lines_fit_the_e5_line_budget(value, e5_budget):
    """Why an ordinary token split falls between lines, never inside one.

    The densest 240-character values measured cost at most 243 tokens with
    their key, against a 384-token budget, so ``TokenBudget.spans`` can back
    off to the preceding newline.
    """
    config, budget = e5_budget
    assert len(value) == 240
    assert budget.count(f"completed: {value}") < config.hard_max_chunk_tokens


def test_normalization_expanding_line_is_torn_and_detectable(tmp_path, e5_budget):
    """The case that makes resume's torn-record check apply to every record.

    U+FDFA normalizes to 18 characters before tokenizing, so 240 of them cost
    far more than the budget although the record is within every cap. The
    line is torn mid-value. Nothing is lost, but the union now holds lines
    that are no field line, which is what resume's count catches.
    """
    config, budget = e5_budget
    value = "\ufdfa" * 240
    record = _fenced_record(completed=value)
    assert len(record) <= 1200
    assert budget.count(f"completed: {value}") > config.hard_max_chunk_tokens

    rows = _e5_rows(tmp_path, record, config)

    assert len(rows) > 2
    contents = [row.content for row in rows]
    assert not _record_is_whole(contents)
    assert not _record_is_whole(contents[::-1])
    assert "".join(contents) == record.rstrip("\n")


@pytest.mark.parametrize(
    ("tail", "padding"),
    [
        pytest.param("```tail", 62, id="backticks"),
        pytest.param("```", 62, id="closing-fence"),
        pytest.param("```text", 62, id="opening-fence"),
        pytest.param("blockers: none", 68, id="field-key"),
    ],
)
def test_a_torn_continuation_that_looks_like_a_record_line_is_caught(
    tail, padding, tmp_path, e5_budget
):
    """A torn row can start with text that looks like a whole record line.

    The budget here falls exactly before *tail* inside ``completed``, so a row
    starts with it. Judging rows by how they start accepted each of these in
    turn; counting catches every one: ``tail`` is either no record line, a
    second fence line, or a second line for an existing field.
    """
    config, _budget = e5_budget
    record = _fenced_record(completed="\ufdfa" * 92 + "a" * padding + tail)

    rows = _e5_rows(tmp_path, record, config)

    contents = [row.content for row in rows]
    # Past the first row, which starts with the record's real opening fence.
    continuations = contents[1:]
    assert any(c.startswith(tail + "\n") for c in continuations), "split moved; re-pick padding"
    assert not _record_is_whole(contents)
    assert not _record_is_whole(contents[::-1])
    with pytest.raises(AssertionError, match="torn record"):
        _parse_fields(contents)


def test_fields_no_cap_shortens_fill_most_of_the_e5_budget(e5_budget):
    """Why lowering the value caps cannot keep every record in one chunk.

    With every capped value empty and a 240-character Korean ``project_root``,
    the remaining fields, keys and fences already cost more than 340 of the
    384 tokens.
    """
    config, budget = e5_budget
    root = ("/Users/사용자/" + "/".join(f"깊은디렉터리이름{i}" for i in range(30)))[:240]
    assert len(root) == 240

    cost = budget.count(_fenced_record(project_root=root))

    assert 340 < cost <= config.hard_max_chunk_tokens


def test_a_bare_field_name_without_a_colon_is_not_a_field_line() -> None:
    record = _fenced_record()
    assert _record_is_whole([record])

    assert not _record_is_whole([record.replace("blockers: \n", "blockers\n")])


def test_the_count_cannot_vouch_for_a_record_saved_without_a_field(tmp_path, e5_budget):
    """The blind spot resume step 4 states, pinned so the text stays honest.

    Save writes every field, and the count leans on that. A record saved
    without ``blockers`` whose torn ``completed`` continues with that key
    passes every count, and the parse invents the field.
    """
    config, _budget = e5_budget
    expanding = chr(0xFDFA)
    record = _fenced_record(completed=expanding * 92 + "a" * 69 + "blockers: none")
    record = record.replace("blockers: \n", "")

    rows = _e5_rows(tmp_path, record, config)

    contents = [row.content for row in rows]
    assert any(c.startswith("blockers: none\n") for c in contents[1:]), "re-pick the padding"
    assert _record_is_whole(contents)
    assert _parse_fields(contents[::-1])["blockers"] == "none"
