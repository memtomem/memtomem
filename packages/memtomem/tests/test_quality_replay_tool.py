"""MCP contract tests for ``mem_quality_replay`` (#1802, PR-5).

The tool is a thin surface over ``memtomem.quality.replay.replay_cases`` +
``serialize_report``: it returns the canonical report bytes verbatim, every
validation error arrives as an ``Error: ...`` string (never an exception), and
the raw ``mem_do`` dispatch path (which bypasses FastMCP's annotation checks)
is guarded against type-confused params.
"""

from __future__ import annotations

import json

from helpers import StubCtx, make_chunk as _make_chunk
from memtomem.quality.replay import replay_cases, serialize_report
from memtomem.server.context import AppContext
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.quality import mem_quality_replay
from memtomem.storage.mixins.eval_cases import (
    EVAL_CASE_SET_KIND,
    EVAL_CASE_SET_SCHEMA_VERSION,
)

_PINNED = 1_784_500_000


def _envelope(cases: list[dict]) -> dict:
    return {
        "schema_version": EVAL_CASE_SET_SCHEMA_VERSION,
        "kind": EVAL_CASE_SET_KIND,
        "cases": cases,
    }


def _case(
    case_id: str,
    query: str,
    labels: list[tuple[str, str]],
    *,
    name: str | None = None,
    status: str = "active",
) -> dict:
    return {
        "case_id": case_id,
        "name": name,
        "query_text": query,
        "top_k": 5,
        "version": 1,
        "status": status,
        "filters": {"namespace": None, "scope": None},
        "labels": [{"content_hash": h, "judgment": j} for h, j in labels],
    }


async def _seed(storage, texts: list[tuple[str, str]]) -> list[str]:
    chunks = [_make_chunk(body, source=src) for body, src in texts]
    await storage.upsert_chunks(chunks)
    return [c.content_hash for c in chunks]


async def _seed_one_case(components, *, name: str | None = None, status: str = "active"):
    hashes = await _seed(
        components.storage, [("alpha beta gamma", "a.md"), ("delta epsilon", "b.md")]
    )
    await components.storage.import_eval_cases(
        _envelope([_case("c-aaaa", "alpha", [(hashes[0], "relevant")], name=name, status=status)])
    )
    return hashes


async def test_happy_path_returns_replay_report_json(bm25_only_components):
    components, _ = bm25_only_components
    await _seed_one_case(components)
    ctx = StubCtx(AppContext.from_components(components))

    out = await mem_quality_replay(ctx=ctx)  # type: ignore[arg-type]
    report = json.loads(out)
    assert report["kind"] == "replay_report"
    assert len(report["cases"]) == 1
    assert report["cases"][0]["case_id"] == "c-aaaa"
    assert "metrics" in report["cases"][0]


async def test_deterministic_bytes_match_direct_engine_call(bm25_only_components):
    components, _ = bm25_only_components
    await _seed_one_case(components)
    ctx = StubCtx(AppContext.from_components(components))

    a = await mem_quality_replay(as_of_unix=_PINNED, ctx=ctx)  # type: ignore[arg-type]
    b = await mem_quality_replay(as_of_unix=_PINNED, ctx=ctx)  # type: ignore[arg-type]
    assert a == b

    direct = serialize_report(
        await replay_cases(
            components.storage,
            components.search_pipeline,
            components.config,
            as_of_unix=_PINNED,
        )
    )
    assert a == direct  # verbatim pass-through, trailing newline included


async def test_selection_by_name_id_empty_list_and_archived(bm25_only_components):
    components, _ = bm25_only_components
    await _seed_one_case(components, name="baseline")
    ctx = StubCtx(AppContext.from_components(components))

    by_name = json.loads(await mem_quality_replay(cases=["baseline"], ctx=ctx))  # type: ignore[arg-type]
    by_id = json.loads(await mem_quality_replay(cases=["c-aaaa"], ctx=ctx))  # type: ignore[arg-type]
    empty = json.loads(await mem_quality_replay(cases=[], ctx=ctx))  # type: ignore[arg-type]
    assert len(by_name["cases"]) == len(by_id["cases"]) == len(empty["cases"]) == 1


async def test_validation_errors_are_strings(bm25_only_components):
    components, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(components))

    neg = await mem_quality_replay(as_of_unix=-5, ctx=ctx)  # type: ignore[arg-type]
    assert neg.startswith("Error: as_of_unix")
    blank = await mem_quality_replay(cases=["  "], ctx=ctx)  # type: ignore[arg-type]
    assert blank.startswith("Error: cases")


async def test_raw_type_abuse_via_mem_do_never_raises(bm25_only_components):
    """mem_do dispatches to the raw fn with unvalidated params — type-confused
    inputs must return ``Error:`` strings, not TypeErrors."""
    components, _ = bm25_only_components
    await _seed_one_case(components)
    ctx = StubCtx(AppContext.from_components(components))

    for params in (
        {"cases": "baseline"},  # bare string would iterate char-by-char
        {"cases": [1]},  # non-string item
        {"as_of_unix": "0"},  # string would blow up on comparison
        {"as_of_unix": True},  # bool is an int subclass
    ):
        out = await mem_do("quality_replay", params=params, ctx=ctx)  # type: ignore[arg-type]
        assert out.startswith("Error:"), (params, out)


async def test_unknown_selector_surfaces_not_found(bm25_only_components):
    components, _ = bm25_only_components
    await _seed_one_case(components)
    ctx = StubCtx(AppContext.from_components(components))

    out = await mem_quality_replay(cases=["nope"], ctx=ctx)  # type: ignore[arg-type]
    assert out.startswith("Error:") and "not found" in out


async def test_empty_registry_returns_valid_report(bm25_only_components):
    components, _ = bm25_only_components
    ctx = StubCtx(AppContext.from_components(components))

    report = json.loads(await mem_quality_replay(ctx=ctx))  # type: ignore[arg-type]
    assert report["kind"] == "replay_report"
    assert report["counts"]["replayed"] == 0
