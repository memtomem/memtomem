"""``mem_update_coverage`` — the commits an ``update`` run has reviewed (#2918).

Coverage is a per-project set of SHAs, never a cursor. These tests run the
tool against a real BM25-only store from inside a registered project, from a
plugin directory bound with ``mem_project_bind`` (the Antigravity shape), and
through ``mem_do``, whose parameters arrive unvalidated.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from helpers import StubCtx
from memtomem.server import context as server_context
from memtomem.server.context import AppContext
from memtomem.server.tools.meta import mem_do
from memtomem.server.tools.project_bind import mem_project_bind
from memtomem.server.tools.update_coverage import mem_update_coverage

RUN = "0f3b5d2e-1a4c-4f8e-9b7d-2c6e8a1f3b5d"
SHAS = [f"{i:040x}" for i in range(1, 11)]


def _make_project(base: Path, name: str) -> tuple[Path, Path]:
    root = base / name
    (root / ".git").mkdir(parents=True)
    tier = root / ".memtomem" / "memories.local"
    tier.mkdir(parents=True)
    return root.resolve(), tier


@pytest.fixture
def project(bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """An app whose cwd is inside a registered project; yields (app, ctx, root)."""
    comp, _mem_dir = bm25_only_components
    root, tier = _make_project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    monkeypatch.chdir(root)
    app = AppContext.from_components(comp)
    return app, StubCtx(app), root


def _lines(out: str) -> dict[str, str]:
    pairs = (line.split(": ", 1) for line in out.splitlines() if ": " in line)
    return {k: v for k, v in pairs}


def _unreviewed(out: str) -> list[str]:
    if "unreviewed_shas:" not in out:
        return []
    return out.split("unreviewed_shas:\n", 1)[1].splitlines()


# ---------------------------------------------------------------------------
# The set
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_run_sees_everything_unreviewed_and_records_only_what_it_read(project) -> None:
    app, ctx, root = project
    out = await mem_update_coverage(shas=SHAS, ctx=ctx)
    assert _lines(out)["project_root"] == str(root)
    assert _lines(out)["reviewed"] == "0" and _lines(out)["unreviewed"] == "10"
    assert _unreviewed(out) == SHAS, "unreviewed SHAs come back in the order given"

    # The run read seven messages in full; it records seven, not the window.
    out = await mem_update_coverage(shas=SHAS[:7], record=True, run_id=RUN, ctx=ctx)
    assert _lines(out) == {"project_root": str(root), "recorded": "7", "already_recorded": "0"}

    out = await mem_update_coverage(shas=SHAS, ctx=ctx)
    assert _lines(out)["reviewed"] == "7" and _lines(out)["unreviewed"] == "3"
    assert _unreviewed(out) == SHAS[7:]


@pytest.mark.asyncio
async def test_record_is_idempotent_and_a_check_never_writes(project) -> None:
    _app, ctx, _root = project
    await mem_update_coverage(shas=SHAS[:3], record=True, run_id=RUN, ctx=ctx)
    again = await mem_update_coverage(shas=SHAS[:3], record=True, run_id=RUN, ctx=ctx)
    assert _lines(again)["recorded"] == "0" and _lines(again)["already_recorded"] == "3"
    # A check run between (a topic-mode or aborted run) leaves the set alone.
    for _ in range(2):
        out = await mem_update_coverage(shas=SHAS, ctx=ctx)
        assert _lines(out)["reviewed"] == "3"


@pytest.mark.asyncio
async def test_a_check_inside_the_callers_transaction_sees_its_own_record(project) -> None:
    """A pooled reader would serve the pre-transaction snapshot; the check must
    read through the writer while ``transaction()`` is open."""
    app, ctx, _root = project
    async with app.storage.transaction():
        out = await mem_update_coverage(shas=SHAS[:2], record=True, run_id=RUN, ctx=ctx)
        assert _lines(out)["recorded"] == "2"
        inside = await mem_update_coverage(shas=SHAS[:3], ctx=ctx)
        assert _lines(inside)["reviewed"] == "2", inside
        assert _unreviewed(inside) == SHAS[2:3]
    after = await mem_update_coverage(shas=SHAS[:3], ctx=ctx)
    assert _lines(after)["reviewed"] == "2"


@pytest.mark.asyncio
async def test_two_interleaved_runs_record_the_union_of_what_each_read(project) -> None:
    _app, ctx, _root = project
    run_a, run_b = str(uuid.uuid4()), str(uuid.uuid4())
    a = _unreviewed(await mem_update_coverage(shas=SHAS, ctx=ctx))
    b = _unreviewed(await mem_update_coverage(shas=SHAS, ctx=ctx))
    assert a == b == SHAS
    # A reads the first six, B the first eight; neither can record what it did not read.
    out_a = await mem_update_coverage(shas=a[:6], record=True, run_id=run_a, ctx=ctx)
    out_b = await mem_update_coverage(shas=b[:8], record=True, run_id=run_b, ctx=ctx)
    assert _lines(out_a)["recorded"] == "6"
    assert _lines(out_b)["recorded"] == "2" and _lines(out_b)["already_recorded"] == "6"
    assert _unreviewed(await mem_update_coverage(shas=SHAS, ctx=ctx)) == SHAS[8:]


@pytest.mark.asyncio
async def test_projects_do_not_share_coverage(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, _mem_dir = bm25_only_components
    root_a, tier_a = _make_project(tmp_path, "alpha")
    root_b, tier_b = _make_project(tmp_path, "beta")
    comp.config.indexing.project_memory_dirs = [tier_a, tier_b]
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    monkeypatch.chdir(root_a)
    await mem_update_coverage(shas=SHAS[:5], record=True, run_id=RUN, ctx=ctx)
    monkeypatch.chdir(root_b)
    out = await mem_update_coverage(shas=SHAS, ctx=ctx)
    assert _lines(out)["project_root"] == str(root_b)
    assert _lines(out)["reviewed"] == "0"


@pytest.mark.asyncio
async def test_an_expired_row_is_covered_again_after_a_re_record(project) -> None:
    """Prune runs before the insert: re-recording an expired SHA must leave it covered."""
    app, ctx, root = project
    await mem_update_coverage(shas=SHAS[:1], record=True, run_id=RUN, ctx=ctx)
    stale = (datetime.now(timezone.utc) - timedelta(days=100)).isoformat(timespec="seconds")
    db = app.storage._get_db()
    db.execute(
        "UPDATE update_review_coverage SET reviewed_at = ? WHERE project_root = ?",
        (stale, str(root)),
    )
    db.commit()

    out = await mem_update_coverage(shas=SHAS[:1], record=True, run_id=RUN, ctx=ctx)
    assert _lines(out)["recorded"] == "1", "the expired row was pruned, then re-inserted"
    assert _lines(await mem_update_coverage(shas=SHAS[:1], ctx=ctx))["reviewed"] == "1"
    rows = db.execute("SELECT COUNT(*) FROM update_review_coverage").fetchone()[0]
    assert rows == 1


# ---------------------------------------------------------------------------
# Project context
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_project_context_is_refused(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    comp, _mem_dir = bm25_only_components
    _root, tier = _make_project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    outside = tmp_path / "plugins" / "memtomem"
    outside.mkdir(parents=True)
    monkeypatch.chdir(outside)
    app = AppContext.from_components(comp)
    out = await mem_update_coverage(shas=SHAS, ctx=StubCtx(app))
    assert out.startswith("Error: no project context"), out


@pytest.mark.asyncio
async def test_a_bound_server_uses_the_bound_root(
    bm25_only_components, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Antigravity starts the server in the plugin directory and binds the project."""
    monkeypatch.setattr(server_context, "_server_transport", "stdio")
    comp, _mem_dir = bm25_only_components
    root, tier = _make_project(tmp_path, "proj")
    comp.config.indexing.project_memory_dirs = [tier]
    plugin_cwd = tmp_path / "plugins" / "memtomem"
    plugin_cwd.mkdir(parents=True)
    monkeypatch.chdir(plugin_cwd)
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    bound = await mem_project_bind(project_root=str(root), ctx=ctx)
    assert bound.startswith("Bound project root:"), bound
    out = await mem_update_coverage(shas=SHAS[:2], record=True, run_id=RUN, ctx=ctx)
    assert _lines(out)["project_root"] == str(root)
    assert _lines(out)["recorded"] == "2"


# ---------------------------------------------------------------------------
# Validation — through mem_do, whose params arrive unvalidated
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "fragment"),
    [
        ({"shas": SHAS[0]}, "must be a list"),
        ({"shas": []}, "must not be empty"),
        ({"shas": SHAS + ["f" * 40]}, "at most 10"),
        ({"shas": [SHAS[0][:39]]}, "not a full lowercase hexadecimal"),
        ({"shas": ["ab" * 20]}, None),  # control: letters in lowercase pass validation
        ({"shas": [("ab" * 20).upper()]}, "not a full lowercase hexadecimal"),
        ({"shas": [42]}, "not a full lowercase hexadecimal"),
        ({"shas": [SHAS[0], SHAS[0]]}, "duplicate"),
        ({"shas": SHAS[:1], "record": True}, "needs run_id"),
        ({"shas": SHAS[:1], "record": True, "run_id": "not-a-uuid"}, "canonical lowercase UUID"),
        ({"shas": SHAS[:1], "record": True, "run_id": RUN.upper()}, "canonical lowercase UUID"),
        ({"shas": SHAS[:1], "record": True, "run_id": 7}, "canonical lowercase UUID"),
        # A check never stores run_id, but a malformed one is still refused: the
        # record call of the same run would reject it anyway.
        ({"shas": SHAS[:1], "run_id": 7}, "canonical lowercase UUID"),
        ({"shas": SHAS[:1], "run_id": "not-a-uuid"}, "canonical lowercase UUID"),
        ({"shas": SHAS[:1], "run_id": RUN.upper()}, "canonical lowercase UUID"),
        ({"shas": SHAS[:1], "run_id": RUN}, None),  # control: a canonical run_id passes a check
    ],
)
async def test_mem_do_rejects_malformed_input_with_an_error_string(
    project, params, fragment
) -> None:
    _app, ctx, _root = project
    out = await mem_do(action="update_coverage", params=params, ctx=ctx)
    if fragment is None:
        # The control case proves the uppercase rejection below is about case,
        # not about letters: the same SHA in lowercase is accepted.
        assert out.startswith("project_root:"), out
        return
    assert out.startswith("Error:") and fragment in out, out
    # Nothing was written by a refused call.
    assert _lines(await mem_update_coverage(shas=SHAS, ctx=ctx))["reviewed"] == "0"


@pytest.mark.asyncio
async def test_mem_do_rejects_a_stringified_boolean(project) -> None:
    _app, ctx, _root = project
    out = await mem_do(
        action="update_coverage",
        params={"shas": SHAS[:1], "record": "true", "run_id": RUN},
        ctx=ctx,
    )
    assert out.startswith("error:") and "literal boolean" in out, out


@pytest.mark.asyncio
async def test_mem_do_reaches_the_tool(project) -> None:
    _app, ctx, root = project
    out = await mem_do(
        action="update_coverage",
        params={"shas": SHAS[:2], "record": True, "run_id": RUN},
        ctx=ctx,
    )
    assert _lines(out) == {"project_root": str(root), "recorded": "2", "already_recorded": "0"}


# The wire shape (annotations as advertised by ``tools/list``) is pinned in
# ``test_tool_mode_pruning.py``, which spawns the server: in the default core
# mode this tool is pruned from the in-process ``mcp`` and could not be read
# here without a skip that would hide a broken pin.
