"""ADR-0011 PR-D — write surface gate tests.

Three load-bearing pins for the memory write surface:

1. **Gate B explicit-flag-and-confirm.** ``mem_add(scope='project_shared',
   ...)`` without ``confirm_project_shared=True`` rejects with a clear
   error. Mirrored on ``mem_batch_add``.
2. **Gate A unbypassable on project_shared.** ``force_unsafe=True``
   plus ``scope='project_shared'`` plus a hit returns
   ``blocked_project_shared`` regardless of the surface (single
   ``mem_add``, batch ``mem_batch_add``).
3. **Inferred scope on edit.** ``mem_edit`` reads the loaded chunk's
   ``metadata.scope`` and feeds it to *both* gates — a client cannot
   bypass Gate A, or skip Gate B's confirmation, by omitting an explicit
   scope param (#2317).

The mocks in this file pre-stage the canonical pieces ``_mem_add_core``
calls: the embedding mismatch check, the AppContext, the index_engine
file-index, and the storage chunk lookup.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from helpers import StubCtx, consent_lines
from memtomem import privacy
from memtomem.models import Chunk, ChunkMetadata
from memtomem.server.context import AppContext
from memtomem.server.tools import memory_crud

_SECRET = "api_key=AKIA1234567890ABCDEF"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


# ---------------------------------------------------------------------------
# Gate B: explicit-flag-and-confirm
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_project_shared_without_confirm_rejects(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="harmless team rule",
        scope="project_shared",
        ctx=ctx,
    )
    assert "confirm_project_shared=True" in out
    assert "Error" in out


@pytest.mark.asyncio
async def test_mem_batch_add_project_shared_without_confirm_rejects(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k", "value": "harmless team rule"}],
        scope="project_shared",
        ctx=ctx,
    )
    assert "confirm_project_shared=True" in out
    assert "Error" in out


# ---------------------------------------------------------------------------
# Gate A: project_shared force_unsafe is hard-refused on every surface
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_project_shared_force_unsafe_blocked(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content=_SECRET,
        scope="project_shared",
        confirm_project_shared=True,
        force_unsafe=True,
        ctx=ctx,
    )
    assert "force_unsafe=True is not permitted" in out
    assert "git history is forever" in out


@pytest.mark.asyncio
async def test_mem_batch_add_project_shared_force_unsafe_blocked(bm25_only_components):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[
            {"key": "clean", "value": "harmless"},
            {"key": "secret", "value": _SECRET},
        ],
        scope="project_shared",
        confirm_project_shared=True,
        force_unsafe=True,
        ctx=ctx,
    )
    assert "force_unsafe=True is not permitted" in out
    assert "Whole batch rejected" in out
    # The blocked_project_shared counter records once per hit item.
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_batch_add"]["blocked_project_shared"] == 1
    # Critically: clean entries do NOT register a pass on a rejected
    # batch (transactional reject preserved).
    assert snap["by_tool"]["mem_batch_add"]["pass"] == 0


# ---------------------------------------------------------------------------
# Default scope behavior preserved (user)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_default_user_scope_force_unsafe_still_works(bm25_only_components):
    """Existing user-scope force_unsafe path is unchanged by ADR-0011."""
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content=_SECRET,
        force_unsafe=True,  # user scope by default
        ctx=ctx,
    )
    # No project_shared error; the old bypass path proceeds.
    assert "force_unsafe=True is not permitted" not in out
    assert "Memory added to" in out
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_add"]["bypassed"] == 1


# ---------------------------------------------------------------------------
# mem_edit inferred scope — gate sees chunk.metadata.scope
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_edit_inferred_scope_blocks_project_shared_force_unsafe(
    bm25_only_components, monkeypatch, tmp_path, caplog
):
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    # Anchor under tmp_path so Windows CI's
    # ``$HOME=C:\Users\runneradmin\AppData\Local\Temp\...`` does not
    # rewrite the path through tilde-expansion downstream
    # (``feedback_windows_tmp_path_under_userprofile.md``).
    proj = tmp_path / "proj_x"
    # The directory has to exist: since #2346 a span whose source directory is
    # gone holds no cross-process lock and refuses to rewrite the file, so
    # ``mem_edit`` would answer that instead of the Gate-A refusal this test
    # measures. Before #2346 the lock acquire created this tree itself — which
    # was the bug — so the fixture had been leaning on it.
    (proj / ".memtomem" / "memories").mkdir(parents=True)
    chunk_id = uuid4()
    fake_chunk = Chunk(
        content="original",
        metadata=ChunkMetadata(
            source_file=proj / ".memtomem" / "memories" / "x.md",
            scope="project_shared",
            project_root=proj,
        ),
        embedding=[0.1] * 1024,
    )
    monkeypatch.setattr(comp.storage, "get_chunk", AsyncMock(return_value=fake_chunk))
    # Gate A is only reachable for a chunk the caller may address: since
    # ADR-0036 ``mem_edit`` resolves ids inside the ADR-0011 boundary, so a
    # project_shared chunk answers "not found" unless the server runs in that
    # project. Pin the context so this test still measures the gate.
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root", lambda _app: proj
    )

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_edit(
            chunk_id=str(chunk_id),
            new_content=_SECRET,
            force_unsafe=True,
            # Gate B is satisfied (#2317) so the call reaches Gate A, which is
            # what this test measures. Passing the consent also makes the
            # assertion the stronger one: even a caller who has explicitly
            # authorised the git-tracked write cannot carry a secret through it.
            confirm_project_shared=True,
            ctx=ctx,
        )
    # The edit surface inferred scope=project_shared from the loaded
    # chunk's metadata; force_unsafe=True is hard-refused.
    assert "force_unsafe=True is not permitted" in out
    assert "git history is forever" in out
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_edit"]["blocked_project_shared"] == 1
    # Consent recorded ≠ write landed. Gate A refused afterwards, and the
    # line stays: someone did authorise a repository-tracked write, which
    # is the fact with no other record. Also the ordering pin — moving the
    # emit behind Gate A would drop this line and leave the rest green.
    # Mirrors ``test_gate_a_refusal_after_consent_still_records_the_consent``
    # on the add path.
    assert len(consent_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_mem_edit_inferred_user_scope_force_unsafe_proceeds(
    bm25_only_components, monkeypatch, tmp_path
):
    """A user-scope chunk's edit surface still allows force_unsafe (no regression)."""
    comp, _mem_dir = bm25_only_components
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    src = tmp_path / "u.md"
    src.write_text("## hi\n\noriginal\n")
    chunk_id = uuid4()
    fake_chunk = Chunk(
        content="original",
        metadata=ChunkMetadata(
            source_file=src,
            scope="user",
            project_root=None,
            start_line=1,
            end_line=3,
        ),
        embedding=[0.1] * 1024,
    )
    monkeypatch.setattr(comp.storage, "get_chunk", AsyncMock(return_value=fake_chunk))

    # Stub the file mutation + reindex so the test stays at the gate
    # boundary (file IO happens in real bm25 storage, but with a
    # synthetic chunk the line-replace + reindex pipeline isn't useful).
    async def fake_index_file(*args, **kwargs):
        from memtomem.models import IndexingStats

        return IndexingStats(0, 0, 0, 0, 0, 0.0)

    monkeypatch.setattr(comp.index_engine, "index_file", fake_index_file)

    out = await memory_crud.mem_edit(
        chunk_id=str(chunk_id),
        new_content=_SECRET,
        force_unsafe=True,
        ctx=ctx,
    )
    # No project_shared error — user-scope chunks still allow bypass.
    assert "force_unsafe=True is not permitted" not in out
    snap = privacy.snapshot()
    assert snap["by_tool"]["mem_edit"]["bypassed"] == 1


# ---------------------------------------------------------------------------
# mem_edit Gate B — the consent half, added in #2317
#
# Gate A was on this path from PR-D; Gate B was not, which made ``mem_edit``
# the one repository-tracked write nobody was asked to confirm while
# ``mem_delete`` asked before removing the very same chunk.
# ---------------------------------------------------------------------------


#: Which ``storage.get_chunk`` call inside ``mem_edit`` is the one the gates
#: read. ``_locked_chunk`` fetches twice: an unlocked probe to learn the
#: source file (the lock key), then a re-fetch under L1+L2. The gates must
#: consume the second. Pinned as a number so a change to that sequence makes
#: the re-scope tests below fail loudly rather than silently start measuring
#: the probe again.
_MEM_EDIT_FRESH_FETCH = 2


def _stage_edit_chunk(
    comp,
    monkeypatch,
    tmp_path,
    *,
    scope="project_shared",
    fresh_scope=None,
    body="original body",
):
    """A chunk backed by a real file, addressable by id, with a re-scope hook.

    The file is real because a refusal must be distinguishable from a
    write that failed for an unrelated reason: the "unchanged on disk"
    assertion is vacuous against a path that never existed. The project
    context is pinned because ADR-0036 makes a project_shared id resolve
    only from inside its own project — without it the gate is never
    reached and a refusal assertion would pass for the wrong reason.

    ``fresh_scope`` stages the race the gate exists to survive: the
    unlocked probe answers with ``scope`` and the re-fetch under the lock
    answers with ``fresh_scope``. The **source file stays the same** on
    both, so ``_locked_chunk`` does not take its "moved" re-key branch and
    the test isolates one variable — which fetch the gate reads.

    Which real writer produces that state, stated precisely because an
    earlier draft of this docstring named the wrong one. **Not**
    ``memory-migrate``: its update sets ``source_file`` and ``scope`` in a
    single statement (``storage/sqlite_backend.py``), so a migrated chunk
    always arrives with a new path and takes the re-key branch instead —
    a different pin, and one this shape deliberately excludes. The writer
    that does produce same-path-new-scope is an **incremental re-index**:
    ``IndexEngine.index_file`` re-derives scope from the path on every
    pass (``_resolve_scope``), and chunk UUIDs are stable across re-index
    (ADR-0005, #1788). So a re-index that lands after
    ``project_memory_dirs`` gains a directory — a config edit, a
    ``config.d`` reload, a newly discovered ``.memtomem/`` — flips a
    chunk's scope at a fixed id and a fixed path, which is exactly this.

    Returns ``(chunk, source, calls)``; ``calls["n"]`` is the number of
    ``get_chunk`` calls, so a test can refuse to pass vacuously if the
    fetch it is aiming at never happened.
    """
    project_root = tmp_path / "proj_edit"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True, exist_ok=True)
    source = proj_dir / "rule.md"
    source.write_text(f"## team rule\n\n{body}\n", encoding="utf-8")
    comp.config.indexing.project_memory_dirs = [proj_dir]

    def _chunk(chunk_scope):
        return Chunk(
            content=body,
            metadata=ChunkMetadata(
                source_file=source,
                scope=chunk_scope,
                project_root=None if chunk_scope == "user" else project_root,
                start_line=1,
                end_line=3,
            ),
            embedding=[0.1] * 1024,
        )

    probe = _chunk(scope)
    fresh = probe if fresh_scope is None else _chunk(fresh_scope)
    # Both share one id: this is the same row being re-read, not two rows.
    fresh = Chunk(
        content=fresh.content,
        metadata=fresh.metadata,
        embedding=fresh.embedding,
        id=probe.id,
    )
    calls = {"n": 0}

    async def _get_chunk(_uid):
        calls["n"] += 1
        return probe if calls["n"] < _MEM_EDIT_FRESH_FETCH else fresh

    monkeypatch.setattr(comp.storage, "get_chunk", AsyncMock(side_effect=_get_chunk))
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda _app: project_root,
    )

    async def fake_index_file(*args, **kwargs):
        from memtomem.models import IndexingStats

        return IndexingStats(0, 0, 0, 0, 0, 0.0)

    monkeypatch.setattr(comp.index_engine, "index_file", fake_index_file)
    return probe, source, calls


@pytest.mark.asyncio
async def test_mem_edit_project_shared_without_confirm_rejects(
    bm25_only_components, monkeypatch, tmp_path, caplog
):
    """Gate B on the edit path: no consent, no write, no consent record."""
    comp, _mem_dir = bm25_only_components
    chunk, source, _calls = _stage_edit_chunk(comp, monkeypatch, tmp_path)
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_edit(
            chunk_id=str(chunk.id),
            new_content="rewritten body",
            ctx=ctx,
        )
    assert "confirm_project_shared=True" in out
    # Refused before the mutation, not after it.
    assert "original body" in source.read_text(encoding="utf-8")
    assert "rewritten body" not in source.read_text(encoding="utf-8")
    # A refusal is not a consent.
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_mem_edit_project_shared_with_confirm_records_one_consent(
    bm25_only_components, monkeypatch, tmp_path, caplog
):
    comp, _mem_dir = bm25_only_components
    chunk, source, _calls = _stage_edit_chunk(comp, monkeypatch, tmp_path)
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_edit(
            chunk_id=str(chunk.id),
            new_content="rewritten body",
            confirm_project_shared=True,
            ctx=ctx,
        )
    assert "Memory updated in" in out, out
    assert "rewritten body" in source.read_text(encoding="utf-8")
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "project_shared.confirmed_via=mem_edit" in lines[0]
    assert "mechanism=param" in lines[0]
    # ``action`` distinguishes this record from the delete of the same
    # chunk, which is the other consent an operator would be reading for.
    assert "action=edit" in lines[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("other_scope", ["user", "project_local"])
@pytest.mark.parametrize("confirmed", [False, True])
async def test_mem_edit_other_tiers_record_no_consent(
    bm25_only_components, monkeypatch, tmp_path, caplog, other_scope, confirmed
):
    """The emit must mirror the gate's predicate, which the AST guard
    cannot check.

    Two axes, both of which a plausible regression widens. ``project_local``
    is the tier a ``scope != "user"`` predicate would sweep in — it lives
    inside a project and is not committed, so it is exactly the case a
    reader mistakes for the shared one. And passing the flag on a tier that
    did not ask for it is not a consent either: the record says a
    repository-tracked write was authorised, and a flag on a
    ``project_local`` edit authorised no such thing.
    """
    comp, _mem_dir = bm25_only_components
    chunk, source, _calls = _stage_edit_chunk(comp, monkeypatch, tmp_path, scope=other_scope)
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_edit(
            chunk_id=str(chunk.id),
            new_content="rewritten body",
            confirm_project_shared=confirmed,
            ctx=ctx,
        )
    # The edit landed without a confirmation being required — the flag is
    # not a blanket requirement, only a project_shared one.
    assert "Memory updated in" in out, out
    assert "rewritten body" in source.read_text(encoding="utf-8")
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_mem_edit_gate_b_reads_the_chunk_re_fetched_under_the_lock(
    bm25_only_components, monkeypatch, tmp_path, caplog
):
    """A re-scope landing while we wait for the lock must not get its edit
    in on the probe's tier.

    The unlocked probe says ``user``; the re-fetch under L1+L2 says
    ``project_shared``. A gate reading the probe would let this through
    and write into the repository-tracked tier with no consent at all —
    the whole reason ``_locked_chunk`` re-fetches. See
    ``_stage_edit_chunk`` for which writer produces this state and which
    one does not.
    """
    comp, _mem_dir = bm25_only_components
    chunk, source, calls = _stage_edit_chunk(
        comp, monkeypatch, tmp_path, scope="user", fresh_scope="project_shared"
    )
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_edit(
            chunk_id=str(chunk.id),
            new_content="rewritten body",
            ctx=ctx,
        )
    # The re-fetch has to have happened, or this passes for the wrong reason.
    assert calls["n"] >= _MEM_EDIT_FRESH_FETCH, calls
    assert "confirm_project_shared=True" in out
    assert "rewritten body" not in source.read_text(encoding="utf-8")
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_mem_edit_gate_b_does_not_refuse_on_a_stale_project_shared_probe(
    bm25_only_components, monkeypatch, tmp_path, caplog
):
    """The inverse, which the refusal test alone leaves open.

    A gate that read the probe would *also* refuse an edit the fresh chunk
    says needs no consent — same bug, opposite sign, and a test that only
    checks the refusal direction cannot tell "reads the fresh chunk" from
    "refuses whenever either fetch says project_shared".
    """
    comp, _mem_dir = bm25_only_components
    chunk, source, calls = _stage_edit_chunk(
        comp, monkeypatch, tmp_path, scope="project_shared", fresh_scope="user"
    )
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_edit(
            chunk_id=str(chunk.id),
            new_content="rewritten body",
            ctx=ctx,
        )
    assert calls["n"] >= _MEM_EDIT_FRESH_FETCH, calls
    assert "Memory updated in" in out, out
    assert "rewritten body" in source.read_text(encoding="utf-8")
    # No consent was asked for, so none is recorded.
    assert consent_lines(caplog) == []


# ---------------------------------------------------------------------------
# Scope-aware write target — MCP mem_add lands in the right tier directory
# (PR-D review #9: gate alone is not enough; metadata must persist scope.)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mem_add_relative_file_under_project_shared_scope_lands_in_project_tier(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D round 8 P2 pin: a relative ``file=`` path with
    explicit ``scope='project_shared'`` must resolve under the project
    tier (``<project>/.memtomem/memories/``), not under
    ``memory_dirs[0]``.

    The pre-fix shape: ``_validate_path("team.md", mdirs, pmdirs)``
    resolved the relative path against ``user_bases[0]`` regardless
    of caller-supplied ``scope=``. ``classify_scope`` then saw a
    user-tier path and silently flipped the effective scope back to
    ``user`` — so the explicit project_shared kwarg was ignored, the
    write landed in user memory, and Gate B's confirm requirement
    never fired (user-tier writes do not trigger it).
    """
    comp, _user_mem_dir = bm25_only_components
    project_root = tmp_path / "proj_relative"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]

    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="harmless team rule",
        file="team.md",  # RELATIVE — the bug-shape this pins.
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    # Successful write to the PROJECT tier.
    assert "Memory added to" in out, out
    expected = proj_dir / "team.md"
    assert str(expected) in out, f"summary did not land in project tier: {out}"
    # Negative: must not have written to user tier.
    assert str(_user_mem_dir / "team.md") not in out
    # Metadata pin — chunks persist project_shared scope.
    chunks = await comp.storage.list_chunks_by_source(expected)
    assert chunks, "expected at least one chunk indexed under project_shared"
    for c in chunks:
        assert c.metadata.scope == "project_shared", c.metadata.scope
        assert c.metadata.project_root == project_root


@pytest.mark.asyncio
async def test_mem_add_relative_file_project_scope_without_context_errors(
    bm25_only_components, monkeypatch
):
    """Companion negative pin: explicit project scope on a relative file
    with NO registered project context errors clearly instead of
    silently falling back to user tier."""
    comp, _ = bm25_only_components
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: None,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="rule",
        file="team.md",
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Error" in out
    assert "registered project context" in out


@pytest.mark.asyncio
async def test_mem_add_project_shared_writes_to_project_dir_and_persists_metadata(
    bm25_only_components, monkeypatch, tmp_path
):
    """``mem_add(scope='project_shared', confirm=True)`` must:

    1. Land the file under ``<project>/.memtomem/memories/`` (not the
       user-tier ``memory_dirs[0]``).
    2. Persist ``metadata.scope == 'project_shared'`` and
       ``metadata.project_root == <project_root>`` on every indexed
       chunk so the read surface (PR-C ``ScopeFilter``) sees the
       correct tier.
    """
    comp, _user_mem_dir = bm25_only_components
    project_root = tmp_path / "proj_a"
    (project_root / ".memtomem" / "memories").mkdir(parents=True)
    # Register the project tier with the indexer so the scope classifier
    # tags chunks with project_shared on re-index.
    comp.config.indexing.project_memory_dirs = [project_root / ".memtomem" / "memories"]

    # Pin project_root resolution to this test's fixture (real
    # ``_resolve_project_context_root`` walks cwd; tmp_path may not
    # cover it portably).
    from memtomem.server.tools import memory_crud as _mc

    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await _mc.mem_add(
        content="harmless team rule",
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Memory added to" in out
    # File location pin.
    assert str(project_root / ".memtomem" / "memories") in out
    # Metadata pin: indexed chunks carry scope+project_root.
    chunks = await comp.storage.list_chunks_by_source(
        next((project_root / ".memtomem" / "memories").glob("*.md"))
    )
    assert chunks, "expected at least one chunk indexed under project_shared"
    for c in chunks:
        assert c.metadata.scope == "project_shared", c.metadata.scope
        assert c.metadata.project_root == project_root, c.metadata.project_root


@pytest.mark.asyncio
async def test_mem_add_project_shared_without_project_context_errors(
    bm25_only_components, monkeypatch
):
    """No registered project tier → ``scope='project_shared'`` errors clearly."""
    comp, _ = bm25_only_components
    # project_memory_dirs is empty by default in the fixture.
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: None,
    )
    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="rule",
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Error" in out
    assert "registered project context" in out


@pytest.mark.asyncio
async def test_mem_add_unregistered_project_local_target_refuses(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review (round 6) pin: refuse a project-tier write
    whose resolved target directory is not in
    ``IndexingConfig.project_memory_dirs``.

    The bypass: only the sibling tier (``.memtomem/memories``) is
    registered, but a caller asks for ``scope='project_local'``. The
    helper resolves ``base`` to ``.memtomem/memories.local`` — a
    *different* directory the indexer does not know about. The
    subsequent ``index_file()`` would classify the new file as
    ``scope='user'`` (registration mismatch in
    ``classify_scope``), so the row is visible across project
    boundaries and the watcher does not track it.
    """
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_unreg"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_local = project_root / ".memtomem" / "memories.local"
    proj_shared.mkdir(parents=True)
    proj_local.mkdir(parents=True)
    # Only the sibling tier is registered.
    comp.config.indexing.project_memory_dirs = [proj_shared]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_add(
        content="rule",
        scope="project_local",
        ctx=ctx,
    )
    assert "Error" in out
    assert "not registered" in out
    # No file landed in the unregistered tier.
    assert not any(proj_local.glob("*.md"))


@pytest.mark.asyncio
async def test_mem_batch_add_unregistered_project_local_target_refuses(
    bm25_only_components, monkeypatch, tmp_path
):
    """Batch path mirrors the single-add registration guard."""
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_unreg_batch"
    proj_shared = project_root / ".memtomem" / "memories"
    proj_local = project_root / ".memtomem" / "memories.local"
    proj_shared.mkdir(parents=True)
    proj_local.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_shared]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k", "value": "v"}],
        scope="project_local",
        ctx=ctx,
    )
    assert "Error" in out
    assert "not registered" in out
    assert not any(proj_local.glob("*.md"))


@pytest.mark.asyncio
async def test_mem_add_file_path_in_project_dir_promotes_scope_and_blocks_force_unsafe(
    bm25_only_components, tmp_path, monkeypatch
):
    """Caller leaves ``scope='user'`` but points ``file=`` at the project tier.

    PR-D security pin: the gates must see the *target tier*, not the
    caller's parameter. A user-scope caller with a project_shared
    file path:

    - Must be rejected without ``confirm_project_shared=True`` (Gate B).
    - Must hard-refuse ``force_unsafe=True`` on a secret hit (Gate A).
    """
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_bypass"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    target_path = proj_dir / "rule.md"

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    # 1) Gate B: no scope, no confirm — explicit project_shared file
    # path alone must trigger the confirm requirement.
    out = await memory_crud.mem_add(
        content="harmless team rule",
        file=str(target_path),
        ctx=ctx,
    )
    assert "confirm_project_shared=True" in out
    assert "scope inferred from file= path" in out

    # 2) Gate A: force_unsafe=True with a secret pattern must be
    # hard-refused even though the caller never named project_shared.
    out = await memory_crud.mem_add(
        content=_SECRET,
        file=str(target_path),
        confirm_project_shared=True,  # bypass Gate B; Gate A still active
        force_unsafe=True,
        ctx=ctx,
    )
    assert "force_unsafe=True is not permitted" in out
    assert "git history is forever" in out


@pytest.mark.asyncio
async def test_mem_delete_by_namespace_with_project_shared_chunk_rejects_without_confirm(
    bm25_only_components, tmp_path
):
    """PR-D review (round 3) pin: ``mem_delete(namespace=...)`` must
    apply the same Gate B probe that ``source_file`` deletes do.

    project_shared memories can sit in the same default namespace as
    user memories, so passing ``namespace="default"`` could otherwise
    wipe project_shared rows without ``confirm_project_shared=True``.
    """
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_ns_delete"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)

    # Stage one project_shared chunk and one user chunk, both in the
    # default namespace.
    proj_chunk = Chunk(
        content="team rule body",
        metadata=ChunkMetadata(
            source_file=proj_dir / "rule.md",
            scope="project_shared",
            project_root=project_root,
            namespace="default",
        ),
        embedding=[0.1] * 1024,
    )
    user_chunk = Chunk(
        content="personal note body",
        metadata=ChunkMetadata(
            source_file=tmp_path / "u.md",
            scope="user",
            project_root=None,
            namespace="default",
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([proj_chunk, user_chunk])

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    out = await memory_crud.mem_delete(namespace="default", ctx=ctx)
    assert "scope='project_shared'" in out
    assert "confirm_project_shared=True" in out
    # Both chunks must still be present — no partial mutation on a
    # rejected bulk delete.
    assert await comp.storage.get_chunk(proj_chunk.id) is not None
    assert await comp.storage.get_chunk(user_chunk.id) is not None

    # With explicit confirm, the bulk delete proceeds.
    out_confirmed = await memory_crud.mem_delete(
        namespace="default",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Removed" in out_confirmed
    assert await comp.storage.get_chunk(proj_chunk.id) is None
    assert await comp.storage.get_chunk(user_chunk.id) is None


@pytest.mark.asyncio
async def test_mem_batch_add_file_path_in_project_dir_promotes_scope(
    bm25_only_components, tmp_path
):
    """Same bypass surface as ``mem_add``; same fix.

    A batch caller can route hits through the project tier by pointing
    ``file=`` at a project_shared path while leaving ``scope='user'``;
    the gates must still fire on the inferred tier.
    """
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_bypass_batch"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    target_path = proj_dir / "batch.md"

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)

    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k", "value": "harmless team rule"}],
        file=str(target_path),
        ctx=ctx,
    )
    assert "confirm_project_shared=True" in out
    assert "scope inferred from file= path" in out


@pytest.mark.asyncio
async def test_mem_batch_add_project_shared_writes_to_project_dir(
    bm25_only_components, monkeypatch, tmp_path
):
    """Batch path mirrors single-add scope-aware target dir."""
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_b"
    (project_root / ".memtomem" / "memories").mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [project_root / ".memtomem" / "memories"]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: project_root,
    )

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await memory_crud.mem_batch_add(
        entries=[{"key": "k1", "value": "v1"}, {"key": "k2", "value": "v2"}],
        scope="project_shared",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Batch add complete" in out
    assert str(project_root / ".memtomem" / "memories") in out


# ---------------------------------------------------------------------------
# mem_consolidate_apply cross-scope rejection
# (PR-D review #4: chunk_ids is the truth source, not group["source"]
#  — protects against re-index / source rename between consolidate and apply.
#  PR-D review #5: skip is user-visible in the MCP return string,
#  not just a logger.warning.)
# ---------------------------------------------------------------------------


async def _stage_consolidate_group(
    comp,
    chunks: list[Chunk],
    group_id: int = 0,
    namespace: str | None = None,
    source_path: Path | None = None,
):
    """Insert ``chunks`` into storage and pre-stage a scratch group entry.

    Used by the consolidation tests below — bypasses the
    ``mem_consolidate`` discovery path so we can pin specific
    scope mixes per group.
    """
    import json
    from datetime import datetime, timedelta, timezone

    await comp.storage.upsert_chunks(chunks)
    group = {
        "group_id": group_id,
        "source": str(source_path or chunks[0].metadata.source_file),
        "chunk_count": len(chunks),
        "total_tokens": sum(len(c.content.split()) for c in chunks),
        "namespace": namespace,
        "previews": [],
        "chunk_ids": [str(c.id) for c in chunks],
    }
    expires = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    await comp.storage.scratch_set(
        "consolidation_groups",
        json.dumps([group], default=str),
        expires_at=expires,
    )


def _fake_chunk(scope: str, source_file: Path, content: str = "x") -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=source_file,
            scope=scope,
            project_root=source_file.parent.parent.parent if scope != "user" else None,
            start_line=1,
            end_line=2,
        ),
        embedding=[0.1] * 1024,
    )


@pytest.mark.asyncio
async def test_mem_consolidate_apply_mixed_scope_returns_skip_message(
    bm25_only_components, tmp_path
):
    """Mixed user + project_shared chunks → user-visible skip with reason."""
    from memtomem.server.tools import consolidation

    comp, mem_dir = bm25_only_components
    user_md = mem_dir / "user.md"
    user_md.write_text("## hi\n\nuser content\n")
    proj_root = tmp_path / "proj_mixed"
    proj_dir = proj_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    proj_md = proj_dir / "proj.md"
    proj_md.write_text("## hi\n\nproject content\n")

    chunks = [
        _fake_chunk("user", user_md, "user one"),
        _fake_chunk("project_shared", proj_md, "shared one"),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined",
        ctx=ctx,
    )
    assert "skipped group 0" in out
    assert "mixed memory scopes" in out
    # Both scope names present so the caller knows which tiers conflict.
    assert "user" in out
    assert "project_shared" in out


@pytest.mark.asyncio
async def test_mem_consolidate_apply_project_shared_requires_confirm(
    bm25_only_components, tmp_path
):
    """All-project_shared group still rejects without explicit confirm."""
    from memtomem.server.tools import consolidation

    comp, _ = bm25_only_components
    proj_root = tmp_path / "proj_share"
    proj_dir = proj_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    src = proj_dir / "p.md"
    src.write_text("## hi\n\nproject content\n")

    chunks = [
        _fake_chunk("project_shared", src, "shared one"),
        _fake_chunk("project_shared", src, "shared two"),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined",
        ctx=ctx,
    )
    assert "scope='project_shared'" in out
    assert "confirm_project_shared=True" in out


@pytest.mark.asyncio
async def test_mem_consolidate_apply_pins_summary_to_source_project(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review round 7 pin: cross-project leak fix.

    Source chunks live in ``proj_a`` but the MCP server's cwd is in
    ``proj_b`` (simulated via ``_resolve_project_context_root`` patch).
    Without the override the summary would land in
    ``proj_b/.memtomem/memories`` and ``link_consolidation_relations``
    would tie the original ``proj_a`` chunks to a summary in a foreign
    project. The override pins the write target to the source chunks'
    persisted ``metadata.project_root`` so the summary lands beside
    the originals.
    """
    from memtomem.server.tools import consolidation

    comp, _ = bm25_only_components
    proj_a = tmp_path / "proj_a"
    proj_a_dir = proj_a / ".memtomem" / "memories"
    proj_a_dir.mkdir(parents=True)
    proj_b = tmp_path / "proj_b"
    proj_b_dir = proj_b / ".memtomem" / "memories"
    proj_b_dir.mkdir(parents=True)
    src = proj_a_dir / "rules.md"
    src.write_text("## hi\n\nproject content\n")

    # Both project tiers registered so the destination registration
    # check inside ``_mem_add_core`` accepts proj_a's directory.
    comp.config.indexing.project_memory_dirs = [proj_a_dir, proj_b_dir]

    # Server cwd resolves to proj_b — without the override this is the
    # project the summary would have been written into.
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: proj_b,
    )

    chunks = [
        _fake_chunk("project_shared", src, "shared one"),
        _fake_chunk("project_shared", src, "shared two"),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined team rule",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "Consolidation applied" in out or "Memory added to" in out, out
    # Summary file path mentions proj_a's directory, not proj_b's.
    assert str(proj_a_dir) in out, f"summary did not land in proj_a tier: {out}"
    assert str(proj_b_dir) not in out, f"summary leaked into proj_b tier (cross-project): {out}"


@pytest.mark.asyncio
async def test_mem_consolidate_apply_mixed_project_roots_refuses(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review round 7 pin: refuse a project-tier group whose
    source chunks span multiple ``project_root`` values.

    A mixed-project group cannot pick a single destination tier without
    discarding the others, so consolidation is refused before the write
    step. Mirrors the mixed-scope rejection a few lines above.
    """
    from memtomem.server.tools import consolidation

    comp, _ = bm25_only_components
    proj_a = tmp_path / "proj_a"
    proj_a_dir = proj_a / ".memtomem" / "memories"
    proj_a_dir.mkdir(parents=True)
    proj_b = tmp_path / "proj_b"
    proj_b_dir = proj_b / ".memtomem" / "memories"
    proj_b_dir.mkdir(parents=True)
    a_src = proj_a_dir / "a.md"
    a_src.write_text("## hi\n\nA team rule\n")
    b_src = proj_b_dir / "b.md"
    b_src.write_text("## hi\n\nB team rule\n")

    comp.config.indexing.project_memory_dirs = [proj_a_dir, proj_b_dir]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: proj_a,
    )

    chunks = [
        _fake_chunk("project_shared", a_src, "from A"),
        _fake_chunk("project_shared", b_src, "from B"),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "skipped group 0" in out
    assert "multiple projects" in out
    # Both project paths cited so the caller can act on the conflict.
    assert str(proj_a) in out
    assert str(proj_b) in out


@pytest.mark.asyncio
async def test_mem_consolidate_apply_null_project_root_project_tier_refuses(
    bm25_only_components, monkeypatch, tmp_path
):
    """ADR-0011 PR-D review round 10 (B1) pin: refuse a project-tier
    consolidate group when EVERY source chunk has ``project_root=None``.

    Pre-fix shape: ``source_project_roots`` is empty (set comprehension
    skips ``None`` values), the ``len > 1`` mixed-project guard does
    NOT fire, and ``project_root_override = next(iter({}), None) =
    None``. ``_mem_add_core`` then resolves the write target via
    ``_resolve_project_context_root(app)`` (server cwd), so a
    project_shared summary lands in whatever project the server cwd
    happens to cover — silent cross-project leak for legacy rows
    that pre-date the PR-B project_root backfill.

    Post-fix: explicit refusal naming the missing project_root.
    """
    from memtomem.server.tools import consolidation

    comp, mem_dir = bm25_only_components
    # Server cwd happens to cover this project — so a leak would land
    # the summary HERE if the guard fired. The refusal must beat the
    # cwd fallback to the punch.
    server_proj = tmp_path / "server_cwd_proj"
    server_proj_dir = server_proj / ".memtomem" / "memories"
    server_proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [server_proj_dir]
    monkeypatch.setattr(
        "memtomem.server.tools.search._resolve_project_context_root",
        lambda app: server_proj,
    )

    # Two project_shared chunks — but with project_root=None on the
    # metadata. Models.py default is ``project_root=None`` so this
    # exact shape can result from any decode path that doesn't set
    # the column (legacy rows pre-migration backfill).
    user_md = mem_dir / "legacy.md"
    user_md.write_text("## hi\n\nlegacy content\n")
    chunks = [
        Chunk(
            content="legacy one",
            metadata=ChunkMetadata(
                source_file=user_md,
                scope="project_shared",
                project_root=None,  # the bug shape
                start_line=1,
                end_line=2,
            ),
            embedding=[0.1] * 1024,
        ),
        Chunk(
            content="legacy two",
            metadata=ChunkMetadata(
                source_file=user_md,
                scope="project_shared",
                project_root=None,
                start_line=3,
                end_line=4,
            ),
            embedding=[0.1] * 1024,
        ),
    ]
    await _stage_consolidate_group(comp, chunks, group_id=0)

    app = AppContext.from_components(comp)
    ctx = StubCtx(app)
    out = await consolidation.mem_consolidate_apply(
        group_id=0,
        summary="combined",
        confirm_project_shared=True,
        ctx=ctx,
    )
    assert "skipped group 0" in out
    assert "no source chunk carries a persisted project_root" in out
    # Negative pin — message must NOT mention the server-cwd project,
    # because the leak shape would have surfaced it.
    assert str(server_proj) not in out
    # No file landed in server_cwd_proj's tier (no leak).
    assert not list(server_proj_dir.glob("*.md")), (
        "summary must NOT have leaked into server-cwd project on NULL project_root"
    )


def test_validate_path_accepts_project_memory_dirs(tmp_path):
    """``_validate_path`` rejects absolute paths outside both base lists,
    accepts paths under either ``memory_dirs`` or ``project_memory_dirs``.
    """
    user_dir = tmp_path / "user_mem"
    user_dir.mkdir()
    project_dir = tmp_path / "proj" / ".memtomem" / "memories"
    project_dir.mkdir(parents=True)

    # Path under project_memory_dirs is accepted only when the helper
    # is told about that base.
    project_file = project_dir / "x.md"
    out, err = memory_crud._validate_path(str(project_file), [user_dir], None)
    assert err is not None  # rejected without project base
    out, err = memory_crud._validate_path(str(project_file), [user_dir], [project_dir])
    assert err is None
    assert out == project_file.resolve()

    # Outside-of-everything stays rejected.
    bogus = tmp_path / "elsewhere" / "y.md"
    bogus.parent.mkdir()
    bogus.write_text("x")
    out, err = memory_crud._validate_path(str(bogus), [user_dir], [project_dir])
    assert err is not None


def test_validate_path_empty_memory_dirs_no_cwd_fallback(tmp_path):
    """#1768 — no silent cwd fallback: a relative user-tier path with empty
    ``memory_dirs`` errors naming the config field; project-tier absolute
    paths keep validating against ``project_memory_dirs`` alone.
    """
    out, err = memory_crud._validate_path("note.md", [], None)
    assert out is None
    assert "indexing.memory_dirs is empty" in err

    project_dir = tmp_path / "proj" / ".memtomem" / "memories"
    project_dir.mkdir(parents=True)
    team_file = project_dir / "team.md"
    out, err = memory_crud._validate_path(str(team_file), [], [project_dir])
    assert err is None
    assert out == team_file.resolve()


# ── ADR-0011 §5 Gate B consent line (#2306) ──────────────────────────────
#
# The AST guard in ``test_project_shared_confirmation_audit_guard.py``
# proves every gate is *followed* by an emit; it cannot prove the emit is
# reached on the right runs. These carry that half for the MCP write
# surface: a confirmed project_shared call records exactly one line, and a
# call that needed no consent records none. The negative direction is the
# load-bearing one — the first draft of ``mem_delete``'s three emits was
# unconditional, so every ordinary user-tier delete logged a consent that
# was never asked for.


async def _seed_two_scope_chunks(comp, tmp_path, namespace="default"):
    """One project_shared chunk and one user chunk, same namespace.

    Both source files are written to disk: a chunk delete rewrites its
    source, so a row pointing at a missing file fails before it reaches
    any gate — which would let a "no consent recorded" assertion pass for
    the wrong reason.
    """
    project_root = tmp_path / "proj_consent"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True, exist_ok=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    comp.config.indexing.memory_dirs = [tmp_path]
    (proj_dir / "rule.md").write_text("## team rule\n\nteam rule body\n", encoding="utf-8")
    (tmp_path / "personal.md").write_text("## personal\n\npersonal note body\n", encoding="utf-8")
    shared = Chunk(
        content="team rule body",
        metadata=ChunkMetadata(
            source_file=proj_dir / "rule.md",
            scope="project_shared",
            project_root=project_root,
            namespace=namespace,
        ),
        embedding=[0.1] * 1024,
    )
    personal = Chunk(
        content="personal note body",
        metadata=ChunkMetadata(
            source_file=tmp_path / "personal.md",
            scope="user",
            project_root=None,
            namespace=namespace,
            start_line=1,
            end_line=3,
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([shared, personal])
    return shared, personal


@pytest.mark.asyncio
async def test_confirmed_project_shared_add_records_one_consent_line(
    bm25_only_components, tmp_path, caplog
):
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_add_consent"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        await memory_crud.mem_add(
            content="team decision body",
            file=str(proj_dir / "team.md"),
            scope="project_shared",
            confirm_project_shared=True,
            ctx=ctx,
        )
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "project_shared.confirmed_via=mem_add" in lines[0]
    assert "mechanism=param" in lines[0]


@pytest.mark.asyncio
async def test_user_tier_add_records_no_consent(bm25_only_components, tmp_path, caplog):
    """The tier that never asked for consent must not claim to have had it."""
    comp, _ = bm25_only_components
    user_dir = tmp_path / "user_mem"
    user_dir.mkdir()
    comp.config.indexing.memory_dirs = [user_dir]
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_add(content="personal note", scope="user", ctx=ctx)
    # Assert the write happened first: "no consent line" is also true of a
    # call that errored out before reaching any gate, which would make this
    # pass for the wrong reason.
    assert "Memory added to" in out
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_refused_project_shared_add_records_no_consent(
    bm25_only_components, tmp_path, caplog
):
    """A refusal is not a consent — the gate returns before the line."""
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_refused"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_add(
            content="team decision body",
            file=str(proj_dir / "team.md"),
            scope="project_shared",
            ctx=ctx,
        )
    assert "confirm_project_shared=True" in out
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_gate_a_refusal_after_consent_still_records_the_consent(
    bm25_only_components, tmp_path, caplog
):
    """Consent recorded ≠ write landed.

    Gate B passes, then Gate A hard-refuses the secret. The line stays:
    someone did authorise a git-tracked write, and that is the fact with
    no other record.
    """
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_gate_a"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_add(
            content=_SECRET,
            file=str(proj_dir / "team.md"),
            scope="project_shared",
            confirm_project_shared=True,
            force_unsafe=True,
            ctx=ctx,
        )
    assert "force_unsafe=True is not permitted" in out
    assert len(consent_lines(caplog)) == 1


@pytest.mark.asyncio
async def test_user_tier_chunk_delete_records_no_consent(bm25_only_components, tmp_path, caplog):
    """Regression: the emit must mirror the gate's scope predicate.

    ``mem_delete`` reaches its post-gate code on two different paths —
    consent given, and no consent needed — and only the first is a
    consent.
    """
    comp, _ = bm25_only_components
    _, personal = await _seed_two_scope_chunks(comp, tmp_path)
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_delete(chunk_id=str(personal.id), ctx=ctx)
    assert "Error" not in out
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_user_only_namespace_delete_records_no_consent(
    bm25_only_components, tmp_path, caplog
):
    comp, _ = bm25_only_components
    await _seed_two_scope_chunks(comp, tmp_path, namespace="solo")
    # Re-stage: a namespace holding only user chunks.
    user_only = Chunk(
        content="only mine",
        metadata=ChunkMetadata(
            source_file=tmp_path / "solo.md",
            scope="user",
            project_root=None,
            namespace="useronly",
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([user_only])
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_delete(namespace="useronly", ctx=ctx)
    assert "Removed 0 " not in out, out  # a no-op delete would make the next line vacuous
    assert "Removed" in out
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_confirmed_namespace_delete_records_one_consent(
    bm25_only_components, tmp_path, caplog
):
    comp, _ = bm25_only_components
    await _seed_two_scope_chunks(comp, tmp_path, namespace="mixed")
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_delete(namespace="mixed", confirm_project_shared=True, ctx=ctx)
    assert "Removed 0 " not in out, out  # a no-op delete would make the next line vacuous
    assert "Removed" in out
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "project_shared.confirmed_via=mem_delete" in lines[0]
    assert "action=delete" in lines[0]


@pytest.mark.asyncio
async def test_user_only_source_delete_records_no_consent(bm25_only_components, tmp_path, caplog):
    """The third ``mem_delete`` branch, which the first two tests left open.

    Chunk and namespace deletes are covered above; the source_file branch
    has its own gate and its own emit, so it needs its own witness — the
    AST guard cannot tell whether that emit carries the gate's predicate.
    """
    comp, _ = bm25_only_components
    user_dir = tmp_path / "user_mem_src"
    user_dir.mkdir()
    comp.config.indexing.memory_dirs = [user_dir]
    user_source = user_dir / "mine.md"
    user_source.write_text("personal body\n", encoding="utf-8")
    user_chunk = Chunk(
        content="personal body",
        metadata=ChunkMetadata(
            source_file=user_source,
            scope="user",
            project_root=None,
            namespace="default",
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([user_chunk])
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_delete(source_file=str(user_source), ctx=ctx)
    assert "Removed 0 " not in out, out  # a no-op delete would make the next line vacuous
    assert "Removed" in out
    assert consent_lines(caplog) == []


@pytest.mark.asyncio
async def test_confirmed_source_delete_records_one_consent(bm25_only_components, tmp_path, caplog):
    comp, _ = bm25_only_components
    project_root = tmp_path / "proj_src_consent"
    proj_dir = project_root / ".memtomem" / "memories"
    proj_dir.mkdir(parents=True)
    comp.config.indexing.project_memory_dirs = [proj_dir]
    shared_source = proj_dir / "rule.md"
    shared_source.write_text("team rule body\n", encoding="utf-8")
    shared_chunk = Chunk(
        content="team rule body",
        metadata=ChunkMetadata(
            source_file=shared_source,
            scope="project_shared",
            project_root=project_root,
            namespace="default",
        ),
        embedding=[0.1] * 1024,
    )
    await comp.storage.upsert_chunks([shared_chunk])
    ctx = StubCtx(AppContext.from_components(comp))

    with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
        out = await memory_crud.mem_delete(
            source_file=str(shared_source), confirm_project_shared=True, ctx=ctx
        )
    assert "Removed 0 " not in out, out  # a no-op delete would make the next line vacuous
    assert "Removed" in out
    lines = consent_lines(caplog)
    assert len(lines) == 1
    assert "project_shared.confirmed_via=mem_delete" in lines[0]
    assert "action=delete" in lines[0]
