"""Search scope-context tests for ADR-0011 PR-C.

Three load-bearing pins:

1. **Default semantics — project context vs no-project context.** With
   no explicit ``scope=``, an in-project search returns
   ``user`` rows + the current project's project-tier rows; an
   out-of-project search returns ``user`` only. Cross-project leak
   prevention.
2. **Tie-break ranking.** Same-relevance results order
   ``project_local > project_shared > user`` so freshest-context-first
   surfaces under equal score.
3. **Orthogonality with namespace prefix exclusion.** A chunk in
   ``namespace=archive:foo`` and ``scope=project_shared`` is hidden by
   the default-search archive prefix exclusion regardless of scope —
   the two filters compose via ``AND``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from memtomem.models import Chunk, ChunkMetadata, ScopeFilter
from memtomem.storage.sqlite_scope import (
    scope_context_sql,
    scope_sort_priority_case,
)


# ---------------------------------------------------------------------------
# Pure SQL-helper tests (no storage fixture required)
# ---------------------------------------------------------------------------


class TestScopeContextSqlNoFilter:
    def test_no_project_context_pins_user_only(self):
        frag, params = scope_context_sql(None, None)
        assert frag == "scope = 'user'"
        assert params == []

    def test_with_project_context_unions_user_and_project(self):
        frag, params = scope_context_sql(None, Path("/proj/a"))
        assert frag == "(scope = 'user' OR project_root = ?)"
        assert params == ["/proj/a"]


class TestScopeContextSqlExplicitFilter:
    def test_exact_filter_in_project_pins_to_project(self):
        f = ScopeFilter.parse("project_shared")
        frag, params = scope_context_sql(f, Path("/proj/a"))
        # Explicit narrowing is intersected with the project context
        # boundary so cross-project leak is impossible.
        assert "scope IN (?)" in frag
        assert "project_root = ?" in frag
        assert params == ["project_shared", "/proj/a"]

    def test_exact_filter_no_project_unions_cross_project(self):
        f = ScopeFilter.parse("project_shared")
        frag, params = scope_context_sql(f, None)
        # Out-of-project + explicit project_shared → cross-project union.
        assert frag == "scope IN (?)"
        assert params == ["project_shared"]

    def test_glob_filter_translates_to_like(self):
        f = ScopeFilter.parse("project_*")
        frag, params = scope_context_sql(f, None)
        assert "scope LIKE ?" in frag
        # Underscore is escaped (literal char) so user's ``project_*``
        # matches ``project_shared`` / ``project_local`` only — not e.g.
        # ``projectXfoo``. The ``%`` is the actual wildcard.
        assert params == ["project\\_%"]

    def test_list_filter_emits_in_clause(self):
        f = ScopeFilter.parse("user,project_local")
        frag, params = scope_context_sql(f, None)
        assert "scope IN (?,?)" in frag
        assert params == ["user", "project_local"]


class TestScopeContextSqlAlias:
    def test_alias_is_prepended_to_columns(self):
        frag, _ = scope_context_sql(None, Path("/p"), column_alias="c.")
        assert "c.scope" in frag
        assert "c.project_root" in frag


class TestTieBreakCase:
    def test_priority_order(self):
        case = scope_sort_priority_case()
        # Smaller integer = higher priority. project_local must come
        # first, project_shared second, user (else) last.
        assert "WHEN 'project_local' THEN 0" in case
        assert "WHEN 'project_shared' THEN 1" in case
        assert "ELSE 2" in case

    def test_alias_passthrough(self):
        case = scope_sort_priority_case("c.")
        assert "CASE c.scope" in case


# ---------------------------------------------------------------------------
# Storage-level integration tests (use the ``storage`` fixture)
# ---------------------------------------------------------------------------


def _make_chunk_at_scope(
    *,
    content: str,
    source_file: Path,
    scope: str,
    project_root: Path | None,
    namespace: str = "default",
) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(
            source_file=source_file,
            scope=scope,
            project_root=project_root,
            namespace=namespace,
        ),
        embedding=[0.1] * 1024,
    )


@pytest.mark.asyncio
async def test_recall_no_filter_no_project_returns_user_only(storage, tmp_path):
    """Default recall outside any project returns ``user`` rows only."""
    proj_a = tmp_path / "proj_a"
    proj_a.mkdir()
    user_chunk = _make_chunk_at_scope(
        content="user level note",
        source_file=tmp_path / "u.md",
        scope="user",
        project_root=None,
    )
    proj_chunk = _make_chunk_at_scope(
        content="proj A team rule",
        source_file=proj_a / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_a,
    )
    await storage.upsert_chunks([user_chunk, proj_chunk])
    rows = await storage.recall_chunks(limit=10)
    contents = {r.content for r in rows}
    assert "user level note" in contents
    assert "proj A team rule" not in contents


@pytest.mark.asyncio
async def test_recall_in_project_context_returns_user_plus_project(storage, tmp_path):
    """Default recall pinned to project_a returns user + project_a's chunks only."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    user_chunk = _make_chunk_at_scope(
        content="user level note",
        source_file=tmp_path / "u.md",
        scope="user",
        project_root=None,
    )
    a_shared = _make_chunk_at_scope(
        content="proj A team rule",
        source_file=proj_a / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_a,
    )
    b_shared = _make_chunk_at_scope(
        content="proj B team rule",
        source_file=proj_b / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_b,
    )
    await storage.upsert_chunks([user_chunk, a_shared, b_shared])

    rows = await storage.recall_chunks(limit=10, project_context_root=proj_a)
    contents = {r.content for r in rows}
    assert "user level note" in contents
    assert "proj A team rule" in contents
    # Cross-project leak prevention: B's project_shared MUST NOT surface.
    assert "proj B team rule" not in contents


@pytest.mark.asyncio
async def test_recall_explicit_project_shared_no_context_unions_all_projects(storage, tmp_path):
    """Cross-project search: explicit scope=project_shared from no-project context unions every project."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    a_shared = _make_chunk_at_scope(
        content="proj A team rule",
        source_file=proj_a / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_a,
    )
    b_shared = _make_chunk_at_scope(
        content="proj B team rule",
        source_file=proj_b / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_b,
    )
    user_chunk = _make_chunk_at_scope(
        content="user level note",
        source_file=tmp_path / "u.md",
        scope="user",
        project_root=None,
    )
    await storage.upsert_chunks([a_shared, b_shared, user_chunk])

    f = ScopeFilter.parse("project_shared")
    rows = await storage.recall_chunks(limit=10, scope_filter=f)
    contents = {r.content for r in rows}
    # Both project_shared rows surface. user-only chunk excluded by filter.
    assert "proj A team rule" in contents
    assert "proj B team rule" in contents
    assert "user level note" not in contents


@pytest.mark.asyncio
async def test_bm25_in_project_context_excludes_other_projects(storage, tmp_path):
    """BM25 search in project_a context does not return project_b's project_shared row."""
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()
    common_term = "deployment-checklist-2026-05-09"
    a_shared = _make_chunk_at_scope(
        content=f"{common_term} project A team rule",
        source_file=proj_a / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_a,
    )
    b_shared = _make_chunk_at_scope(
        content=f"{common_term} project B team rule",
        source_file=proj_b / ".memtomem" / "memories" / "rule.md",
        scope="project_shared",
        project_root=proj_b,
    )
    user_chunk = _make_chunk_at_scope(
        content=f"{common_term} user note",
        source_file=tmp_path / "u.md",
        scope="user",
        project_root=None,
    )
    await storage.upsert_chunks([a_shared, b_shared, user_chunk])

    results = await storage.bm25_search(common_term, top_k=20, project_context_root=proj_a)
    contents = {r.chunk.content for r in results}
    # User-tier and proj_a's shared visible.
    assert any("project A team rule" in c for c in contents)
    assert any("user note" in c for c in contents)
    # Cross-project leak prevented.
    assert not any("project B team rule" in c for c in contents)


@pytest.mark.asyncio
async def test_recall_orthogonal_to_namespace_archive_prefix(storage, tmp_path):
    """archive:* prefix exclusion AND scope filter compose — both fire."""
    proj = tmp_path / "p"
    proj.mkdir()
    archive_proj = _make_chunk_at_scope(
        content="archived team rule",
        source_file=proj / ".memtomem" / "memories" / "old.md",
        scope="project_shared",
        project_root=proj,
        namespace="archive:summary",
    )
    fresh_proj = _make_chunk_at_scope(
        content="fresh team rule",
        source_file=proj / ".memtomem" / "memories" / "new.md",
        scope="project_shared",
        project_root=proj,
        namespace="default",
    )
    await storage.upsert_chunks([archive_proj, fresh_proj])

    # Default recall + project context: archive:* should be excluded by
    # the system_namespace_prefixes filter (when callers parse with that
    # default), but here we emulate the "no exclusion passed" path —
    # both rows surface because no namespace filter is applied. The
    # orthogonality pin: when an archive prefix exclusion IS passed, it
    # must AND with scope, not replace it.
    from memtomem.models import NamespaceFilter

    ns_filter = NamespaceFilter.parse(None, system_prefixes=("archive:",))
    rows = await storage.recall_chunks(
        limit=10,
        namespace_filter=ns_filter,
        project_context_root=proj,
    )
    contents = {r.content for r in rows}
    assert "fresh team rule" in contents
    # archive:* excluded by namespace filter even though scope=project_shared
    # would otherwise let it through.
    assert "archived team rule" not in contents


@pytest.mark.asyncio
async def test_recall_tie_break_orders_local_shared_user(storage, tmp_path):
    """Same created_at across scopes orders project_local > project_shared > user."""
    import datetime
    from datetime import timezone

    proj = tmp_path / "p"
    proj.mkdir()
    common_ts = datetime.datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    user_chunk = Chunk(
        content="user X",
        metadata=ChunkMetadata(source_file=tmp_path / "u.md", scope="user"),
        embedding=[0.1] * 1024,
        created_at=common_ts,
        updated_at=common_ts,
    )
    shared_chunk = Chunk(
        content="shared X",
        metadata=ChunkMetadata(
            source_file=proj / ".memtomem" / "memories" / "s.md",
            scope="project_shared",
            project_root=proj,
        ),
        embedding=[0.1] * 1024,
        created_at=common_ts,
        updated_at=common_ts,
    )
    local_chunk = Chunk(
        content="local X",
        metadata=ChunkMetadata(
            source_file=proj / ".memtomem" / "memories.local" / "l.md",
            scope="project_local",
            project_root=proj,
        ),
        embedding=[0.1] * 1024,
        created_at=common_ts,
        updated_at=common_ts,
    )
    await storage.upsert_chunks([user_chunk, shared_chunk, local_chunk])

    rows = await storage.recall_chunks(limit=10, project_context_root=proj)
    contents = [r.content for r in rows]
    # Tie-break order: local first, then shared, then user.
    assert contents.index("local X") < contents.index("shared X") < contents.index("user X")


@pytest.mark.asyncio
async def test_cache_key_distinct_per_project_context(storage, tmp_path):
    """Search pipeline cache key includes project_context_root.

    Two callers from different projects MUST NOT share a cache slot —
    the always-on context-boundary fragment differs, so the cached
    result set differs.
    """
    from memtomem.config import SearchConfig
    from memtomem.search.pipeline import SearchPipeline

    # Use a minimal pipeline instance just to exercise _cache_key.
    cfg = SearchConfig()
    pipeline = SearchPipeline.__new__(SearchPipeline)
    pipeline._config = cfg
    pipeline._reranker = None
    pipeline._rerank_config = None
    pipeline._decay_config = type("D", (), {"enabled": False, "half_life_days": 30.0})()
    pipeline._mmr_config = type("M", (), {"enabled": False, "lambda_param": 0.7})()
    pipeline._context_window_config = None

    key_a = pipeline._cache_key(
        "query", 10, None, None, None, None, scope=None, project_context_root=Path("/proj/a")
    )
    key_b = pipeline._cache_key(
        "query", 10, None, None, None, None, scope=None, project_context_root=Path("/proj/b")
    )
    key_none = pipeline._cache_key(
        "query", 10, None, None, None, None, scope=None, project_context_root=None
    )
    assert key_a != key_b
    assert key_a != key_none
    assert key_b != key_none


@pytest.mark.asyncio
async def test_bm25_search_filters_inside_candidate_selection(storage, tmp_path):
    """PR-D review #2 pin: scope/namespace filter must run inside the
    FTS candidate selection, not after a post-LIMIT join.

    Stages many high-rank "other-project" hits so the global top-k
    would have been entirely cross-project chunks under the pre-fix
    shape (filter applied after LIMIT). With the fix, the inner
    filter pushes scope into MATCH-time candidate iteration so
    the current project's chunk still surfaces at top_k=2.
    """
    proj_a = tmp_path / "proj_a"
    proj_b = tmp_path / "proj_b"
    proj_a.mkdir()
    proj_b.mkdir()

    # 10 chunks in proj_b that all match the query — under the old
    # shape these would saturate the inner LIMIT before the scope
    # filter could see proj_a's chunk.
    b_chunks = [
        _make_chunk_at_scope(
            content="alpha bravo charlie noise " + str(i),
            source_file=proj_b / ".memtomem" / "memories" / f"b{i}.md",
            scope="project_shared",
            project_root=proj_b,
        )
        for i in range(10)
    ]
    a_chunk = _make_chunk_at_scope(
        content="alpha bravo charlie team rule",
        source_file=proj_a / ".memtomem" / "memories" / "a.md",
        scope="project_shared",
        project_root=proj_a,
    )
    await storage.upsert_chunks(b_chunks + [a_chunk])

    # Pin to proj_a's context — proj_b's chunks must be filtered out
    # AND proj_a's chunk must still surface even at small top_k.
    results = await storage.bm25_search(
        "alpha bravo charlie",
        top_k=2,
        project_context_root=proj_a,
    )
    contents = {r.chunk.content for r in results}
    # Cross-project hits stay filtered.
    assert not any(c.startswith("alpha bravo charlie noise") for c in contents)
    # proj_a's chunk surfaces despite proj_b dominating the global rank.
    assert "alpha bravo charlie team rule" in contents
