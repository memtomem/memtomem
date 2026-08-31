"""A file's path retrieves it only when nothing's prose matches (#2224).

``chunks_fts`` is ``fts5(content, source_file)`` and an unqualified ``MATCH``
searches both columns. ``source_file`` is stored raw, so unicode61 splits it
into components and every directory and filename word became a searchable term:
on a real 6,995-chunk store, 71 of the top 100 hits for ``plans`` were chunks
about something else, surfaced because they live under ``.claude/plans/``, and
for auto-generated plan-filename words the path noise was the *entire* result
set.

**Why weighting the column 0.0 is not the fix.** It was the obvious one — the
issue proposes it — and it is not enough: RRF fuses on a result's *ordinal
rank* (``search/fusion.py``: ``w / (k + rank)``), not on its score, so a
path-only row scored 0.0 still occupies a BM25 slot and still contributes
positive fused relevance to the hybrid pipeline every caller actually uses. The
row has to not be *retrieved*, which is why the MATCH is column-qualified
instead. ``TestHybridPipeline`` below is the test that distinguishes the two:
the leg-level ones pass under either design.

The fallback is deliberate. Typing a filename into search is an undocumented
but plausible habit, and it still works — the path phase runs only once the
content phase has found nothing, so a path can never outrank prose *in this
leg*.

That last qualifier is the honest bound on the contract. When BM25-content
finds nothing but the dense leg finds prose, the path fallback's rows and those
dense rows do meet in RRF. Nothing here pins that case, and the fixture cannot
reach it (see ``TestPipelineFusion``).
"""

from __future__ import annotations

import sqlite3

import pytest

from memtomem.storage import fts_tokenizer as _fts
from memtomem.storage.sqlite_backend import _column_match

from helpers import make_chunk as _make_chunk

#: Long filler so the content match has a *low* per-term density. fts5
#: normalises each column by its average length, so a short path carrying the
#: term outscores a long body mentioning it once — which is why the real
#: store's worst cases were long notes losing to plan filenames. A fixture with
#: short bodies ranks correctly even unfixed and proves nothing.
_FILLER = " ".join(f"word{i}" for i in range(200))


class TestBm25Leg:
    @pytest.mark.asyncio
    async def test_a_path_only_match_is_not_retrieved_when_content_matches(self, storage):
        content_hit = _make_chunk(
            f"{_FILLER} a note about octopus behaviour and habitats. {_FILLER}",
            source="notes/marine-biology.md",
        )
        path_only = [_make_chunk("short", source=f".claude/plans/octopus-{i}.md") for i in range(5)]
        await storage.upsert_chunks([*path_only, content_hit])

        results = await storage.bm25_search("octopus", top_k=10)

        assert [r.chunk.id for r in results] == [content_hit.id], (
            "a path-only chunk was retrieved alongside the chunk that says the word: "
            f"{[str(r.chunk.metadata.source_file) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_a_path_only_match_is_retrieved_when_nothing_else_matches(self, storage):
        """The fallback: filename lookup still works, it just cannot compete."""
        path_only = _make_chunk("body text with no marker word", source="reports/octopus.md")
        await storage.upsert_chunks([path_only])

        results = await storage.bm25_search("octopus", top_k=5)

        assert [r.chunk.id for r in results] == [path_only.id], (
            "the path stopped being searchable at all — the fallback is gone"
        )

    @pytest.mark.asyncio
    async def test_the_or_fallback_survives_inside_the_content_phase(self, storage):
        """A multi-term query with no AND hit still degrades to OR — on content."""
        content_hit = _make_chunk("a note about pangolin diets", source="notes/a.md")
        path_only = _make_chunk("short", source="plans/pangolin-and-quokka.md")
        await storage.upsert_chunks([path_only, content_hit])

        # "quokka" appears nowhere in content, so the AND pass finds nothing and
        # the OR pass answers — still without the path row.
        results = await storage.bm25_search("pangolin quokka", top_k=10)

        assert [r.chunk.id for r in results] == [content_hit.id], (
            f"OR fallback leaked the path row: "
            f"{[str(r.chunk.metadata.source_file) for r in results]}"
        )

    @pytest.mark.asyncio
    async def test_the_path_phase_has_its_own_or_fallback(self, storage):
        """Both phases run the same AND-then-OR ladder."""
        path_only = _make_chunk("nothing relevant here", source="plans/wombat-notes.md")
        await storage.upsert_chunks([path_only])

        results = await storage.bm25_search("wombat quokka", top_k=5)

        assert [r.chunk.id for r in results] == [path_only.id], (
            "the path phase did not fall back to OR for a multi-term query"
        )

    @pytest.mark.asyncio
    async def test_every_content_match_is_returned_before_any_path_phase_runs(self, storage):
        """One weak content match still suppresses the whole path phase."""
        weak = _make_chunk(f"{_FILLER} pangolin {_FILLER}", source="notes/long.md")
        paths = [_make_chunk("short", source=f"plans/pangolin-{i}.md") for i in range(3)]
        await storage.upsert_chunks([*paths, weak])

        results = await storage.bm25_search("pangolin", top_k=10)

        assert [r.chunk.id for r in results] == [weak.id]


class TestPipelineFusion:
    """Through fusion — the test that fails under 0.0-weighting.

    Zero-weighting demotes a path-only row inside the BM25 leg but still
    returns it, and RRF scores it by ordinal rank, so it reaches the fused
    results anyway. Only not retrieving it keeps it out.

    Scope, stated because the class name used to overclaim it: the shared
    ``components`` fixture leaves ``embedding.provider`` at its default
    ``"none"``, so this runs one leg through RRF and the post-fusion stages,
    not BM25 against a live dense leg. That is enough for what it pins — the
    ordinal-rank point needs no second leg — and it is *not* enough to pin the
    case where BM25-content finds nothing while dense finds prose, in which the
    path fallback and a dense hit do meet in fusion. See the contract note in
    the module docstring.
    """

    @pytest.mark.asyncio
    async def test_a_path_only_chunk_does_not_surface_in_fused_results(self, pipeline, storage):
        content_hit = _make_chunk(
            f"{_FILLER} the octopus section discusses octopus habitats. {_FILLER}",
            source="notes/marine-biology.md",
        )
        path_only = [_make_chunk("short", source=f".claude/plans/octopus-{i}.md") for i in range(5)]
        await storage.upsert_chunks([*path_only, content_hit])

        results, _stats = await pipeline.search("octopus", top_k=10, record=False)

        surfaced = {str(r.chunk.metadata.source_file) for r in results}
        assert not any(".claude/plans/octopus-" in s for s in surfaced), (
            f"a path-only chunk reached the fused results: {sorted(surfaced)}"
        )
        assert any("marine-biology" in s for s in surfaced), (
            "the genuine content match did not survive the pipeline"
        )


class TestTheColumnFilterCannotBeEscaped:
    """A query cannot spell its way out of the filter imposed on it.

    The filter is built by concatenating around ``tokenize_for_fts``'s output,
    which makes the escaping contract load-bearing in a way it was not before:
    a query that itself looks like fts5 column syntax must stay a *search term*.
    The braces matter here — a bare ``content : expr`` prefix would be re-parsed
    when ``expr`` starts with something column-shaped, while a braced column
    list is closed before the expression begins.
    """

    #: Queries that spell fts5 syntax, including the column filter itself.
    _ADVERSARIAL = (
        "source_file : secret",
        "{source_file} : secret",
        "} : (source_file",
        "content} OR {source_file",
        "a}b",
        "secret) OR {source_file} : (secret",
        '" OR "',
        "NEAR(a b)",
        "^anchor",
        "a AND b OR NOT c",
    )

    @staticmethod
    def _run(db, expr: str):
        """``(rows, error)`` — fts5 rejecting an expression is data, not a crash."""
        try:
            return (
                db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ?", (expr,)
                ).fetchall(),
                None,
            )
        except sqlite3.OperationalError as exc:
            return [], exc

    @staticmethod
    def _probe_db():
        db = sqlite3.connect(":memory:")
        db.execute(
            "CREATE VIRTUAL TABLE chunks_fts USING fts5(content, source_file, tokenize='unicode61')"
        )
        db.execute(
            "INSERT INTO chunks_fts VALUES (?, ?)",
            ("a body that says secret once", "notes/secret-plan.md"),
        )
        return db

    @pytest.mark.parametrize("query", _ADVERSARIAL)
    @pytest.mark.parametrize("use_or", [False, True])
    @pytest.mark.parametrize("column", ["content", "source_file"])
    def test_the_filter_adds_no_parse_failure_and_no_new_match(self, query, use_or, column):
        """Run the real expression, and compare it against the unfiltered one.

        Two things have to hold, and only executing shows either. The filter
        must not make fts5 reject an expression it would otherwise accept, and
        it must not let the query reach a column it did not name. Both are
        stated *relative to the bare expression* on purpose: some of these
        queries are un-parsable either way (a bare ``AND`` was a syntax error
        before this change too), and a test that forbade that outright would be
        asserting a fix nobody made.

        Asserting on the string was the weaker version this replaces — it
        passed while permitting an unquoted brace as long as a quote appeared
        anywhere else in the body.
        """
        bare = _fts.tokenize_for_fts(query, for_query=True, use_or=use_or)
        expr = _column_match(column, query, use_or=use_or)
        assert expr == "{" + column + "} : (" + bare + ")", (
            "the filter rewrote the tokenizer's output instead of wrapping it"
        )

        db = self._probe_db()
        try:
            bare_rows, bare_error = self._run(db, bare)
            filtered_rows, filtered_error = self._run(db, expr)
        finally:
            db.close()

        if bare_error is not None:
            assert filtered_error is not None, (
                f"the filter made {query!r} parse where it did not before: {expr!r}"
            )
            return
        assert filtered_error is None, (
            f"the filter broke a query fts5 accepted bare: {expr!r} ({filtered_error})"
        )
        # The filtered result is the bare one restricted to this column: it may
        # lose rows, never gain them. Gaining one would mean the query reached
        # a column the filter did not name.
        assert set(filtered_rows) <= set(bare_rows), (
            f"{query!r} matched more with the {column} filter than without it: "
            f"{filtered_rows} vs {bare_rows}"
        )

    def test_the_same_holds_under_the_morphological_tokenizer(self):
        """The escaping lives in ``tokenize_for_fts``, which has two backends."""
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")
        from memtomem.storage import fts_tokenizer

        previous = fts_tokenizer.get_tokenizer()
        fts_tokenizer.set_tokenizer("kiwipiepy")
        try:
            db = self._probe_db()
            try:
                for query in (*self._ADVERSARIAL, "비밀 } : (source_file"):
                    for use_or in (False, True):
                        bare = fts_tokenizer.tokenize_for_fts(query, for_query=True, use_or=use_or)
                        expr = _column_match("content", query, use_or=use_or)
                        assert expr == "{content} : (" + bare + ")", expr
                        bare_rows, bare_error = self._run(db, bare)
                        rows, error = self._run(db, expr)
                        if bare_error is not None:
                            assert error is not None, f"kiwi: filter fixed {query!r}"
                            continue
                        assert error is None, f"kiwi: filter broke {query!r}: {expr!r} ({error})"
                        assert set(rows) <= set(bare_rows), f"kiwi: {query!r} gained rows"
            finally:
                db.close()
        finally:
            fts_tokenizer.set_tokenizer(previous)

    @pytest.mark.asyncio
    async def test_naming_the_column_does_not_reopen_it(self, storage):
        """The end of the same argument, run against a real store.

        A query that spells ``source_file :`` gets no more access to the path
        column than any other query: while something's *content* matches, the
        path phase does not run, and the words it typed are searched as terms.
        (With nothing matching in content the fallback would return the path
        chunk — as it would for the bare word — which is the feature, not an
        escape, so this fixture keeps a content match present.)
        """
        content_hit = _make_chunk("a note that mentions secret handling", source="notes/a.md")
        path_only = _make_chunk("nothing to see", source="notes/secret-plan.md")
        await storage.upsert_chunks([path_only, content_hit])

        results = await storage.bm25_search("source_file : secret", top_k=5)

        assert [r.chunk.id for r in results] == [content_hit.id], (
            "a column-shaped query pulled a path-only chunk while content matched: "
            f"{[str(r.chunk.metadata.source_file) for r in results]}"
        )


class TestTheOrFallbackUnderAMorphologicalTokenizer:
    """Whitespace is not term multiplicity (#2224 review).

    The OR pass used to be gated on ``" " in query.strip()`` — inherited
    unchanged from before this issue. ``kiwipiepy`` expands one space-free
    Korean word into several FTS terms (``했습니다`` → ``하* 었* 습니다*``), so
    an AND query that misses had its OR fallback skipped for a query the raw
    string said was a single term. The gate is now whether the two tokenized
    expressions differ, which is the thing the pass actually depends on.
    """

    @pytest.fixture
    def kiwi(self):
        pytest.importorskip("kiwipiepy", reason="the korean extra is not installed")
        from memtomem.storage import fts_tokenizer

        previous = fts_tokenizer.get_tokenizer()
        fts_tokenizer.set_tokenizer("kiwipiepy")
        try:
            yield fts_tokenizer
        finally:
            fts_tokenizer.set_tokenizer(previous)

    def test_a_space_free_korean_word_still_has_an_or_pass(self, kiwi):
        """The premise: one word, two different expressions."""
        query = "했습니다"
        assert " " not in query, "the fixture query must be the space-free case"
        and_expr = _column_match("content", query, use_or=False)
        or_expr = _column_match("content", query, use_or=True)
        assert and_expr != or_expr, (
            "kiwipiepy did not expand this word — pick one that it does, or the test proves nothing"
        )
        assert " OR " in or_expr

    @pytest.mark.asyncio
    async def test_the_or_pass_actually_answers(self, kiwi, storage):
        """End of the argument: the AND form misses, the OR form finds it."""
        chunk = _make_chunk("회의를 진행합니다", source="notes/ko.md")
        await storage.upsert_chunks([chunk])

        results = await storage.bm25_search("했습니다", top_k=5)

        assert [r.chunk.id for r in results] == [chunk.id], (
            "the OR fallback was skipped for a space-free query whose tokenizer "
            "expands it into several terms"
        )
