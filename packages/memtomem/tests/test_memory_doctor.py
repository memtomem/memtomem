"""Tests for ``mm memory doctor`` (Tier 1 report-only + Tier 2 ``--fix``).

Layers:

* ``TestParser`` / ``TestClassifyLink`` / ``TestBudget`` — pure functions, no
  DB or disk-config side effects.
* ``TestAnalysis`` — drives ``_gather_reports`` against a **real**
  ``SqliteBackend`` (a tmp DB) + a real on-disk ``claude-memory`` dir, so the
  disk↔DB drift detection exercises the actual SQL aggregate and the engine's
  own discovery (no ``AsyncMock`` masking — memory
  ``feedback_mocked_storage_hides_sql_bugs``).
* ``TestCli`` — Click ``CliRunner`` end-to-end with the read-only config
  loader stubbed, pinning the exit code and the ``--json`` payload shape.
* ``TestDocsParity`` — pins the contract documented in
  ``docs/guides/reference.md`` (check/severity table, error-severity set,
  budget caps, ``--json`` status rule, help text) against the implementation
  so the two can't drift.
* ``TestSpliceRoundTrip`` / ``TestMissingTargetGuard`` / ``TestApplyFix`` /
  ``TestFixCli`` — Tier 2 ``--fix`` (ADR-0020): byte-exact line splicing across
  LF/CRLF ± trailing newline, the missing_target-only subtractive guard, the
  locked re-validate/atomic-write apply path against a real on-disk index file,
  and the CLI dry-run/apply/exit-code surface.
* ``TestAllLinksDeadRule`` / ``TestStrictGrammarRule`` — ADR-0020 §1's per-line
  eligibility (amended #1764): which candidate lines ``--fix`` may delete whole,
  and that the rest are *skipped and reported* rather than blocking the run.
"""

from __future__ import annotations

import json
from collections import Counter
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest
from click.testing import CliRunner

from memtomem.cli import cli
from memtomem.cli.memory_doctor_cmd import (
    _apply_fix,
    _gather_reports,
    _missing_target_entries,
    _partition_candidates,
    _splice_lines,
    classify_link,
    measure_budget,
    parse_memory_index,
)
from memtomem.config import Mem2MemConfig
from memtomem.storage.sqlite_backend import SqliteBackend
from memtomem.storage.sqlite_helpers import norm_path


# ── Pure: parser ────────────────────────────────────────────────────


class TestParser:
    def test_extracts_pointer_entries(self):
        text = "- [Alpha](alpha.md) — first\n* [Beta](sub/beta.md) — second\n"
        parsed = parse_memory_index(text)
        assert [(e.title, e.target) for e in parsed.entries] == [
            ("Alpha", "alpha.md"),
            ("Beta", "sub/beta.md"),
        ]
        assert parsed.entries[0].line_no == 1
        assert parsed.entries[1].line_no == 2

    def test_preserves_non_pointer_lines_with_numbers(self):
        text = "# Header\n\n- [A](a.md) — x\nplain prose line\n<!-- comment -->\n"
        parsed = parse_memory_index(text)
        assert len(parsed.entries) == 1
        # Header(1), blank(2), prose(4), comment(5) preserved in order.
        assert [n for n, _ in parsed.other_lines] == [1, 2, 4, 5]

    def test_title_with_brackets_in_hook_not_swallowed(self):
        # Non-greedy title stops at the first ``]``; brackets in the trailing
        # hook prose must not be pulled into the title or target.
        parsed = parse_memory_index("- [Title](t.md) — see [note] and (paren)\n")
        assert parsed.entries[0].title == "Title"
        assert parsed.entries[0].target == "t.md"

    def test_non_ascii_filename_target(self):
        parsed = parse_memory_index("- [한글](한글노트.md) — 메모\n")
        assert parsed.entries[0].target == "한글노트.md"

    def test_crlf_line_numbers_stay_aligned(self):
        # Entry line numbers come from markdown-it's source maps, but --fix
        # splices by `splitlines(keepends=True)` index. The two counters agree
        # on CRLF only because both treat \r\n as one break — if they ever
        # diverged, every splice on a Windows-authored index would cut the
        # wrong line, and TestSpliceRoundTrip can't see it (it's handed line
        # numbers rather than deriving them).
        parsed = parse_memory_index(
            "- [A](a.md) — x\r\n- [Dead](gone.md) — y\r\n- [B](b.md) — z\r\n"
        )
        assert [(e.line_no, e.target) for e in parsed.entries] == [
            (1, "a.md"),
            (2, "gone.md"),
            (3, "b.md"),
        ]
        assert parsed.entries[1].raw == "- [Dead](gone.md) — y"  # terminator-stripped

    def test_every_link_on_a_line_is_an_entry(self):
        # #1757 defect 1/2: a line-anchored, match-once parser saw only ``a``,
        # so ``b``'s live target never suppressed its orphan and a dead ``b``
        # was never link-classified at all.
        parsed = parse_memory_index("- [a](x.md) · [b](y.md)\n")
        assert [(e.title, e.target) for e in parsed.entries] == [("a", "x.md"), ("b", "y.md")]
        assert [e.line_no for e in parsed.entries] == [1, 1]
        # ``raw`` stays the whole line for both — it is the splice unit, not the
        # link's slice of the line.
        assert {e.raw for e in parsed.entries} == {"- [a](x.md) · [b](y.md)"}

    def test_prose_prefixed_bullet_is_parsed(self):
        # The old ``^\s*[-*]\s*\[`` anchor matched nothing here, so the line
        # silently became "other" and its pointer went unchecked.
        parsed = parse_memory_index("- NS: [a](x.md) — hook\n")
        assert [(e.title, e.target) for e in parsed.entries] == [("a", "x.md")]

    def test_nested_hook_link_is_an_entry(self):
        # Real-world shape: the hook's parenthetical carries a second pointer at
        # a real file. Counting it is what keeps that file out of index_orphan.
        parsed = parse_memory_index("- [A](topic.md) — P2=NO-GO([why](other.md))\n")
        assert [e.target for e in parsed.entries] == ["topic.md", "other.md"]
        assert 1 not in parsed.ambiguous_lines  # balanced parens read cleanly

    def test_non_bullet_line_with_link_is_not_an_entry(self):
        parsed = parse_memory_index("See [docs](https://example.com) for more.\n")
        assert parsed.entries == ()
        assert [n for n, _ in parsed.other_lines] == [1]

    def test_bullet_without_link_is_preserved(self):
        parsed = parse_memory_index("- just a prose bullet\n")
        assert parsed.entries == ()
        assert [n for n, _ in parsed.other_lines] == [1]


class TestDeceivingLines:
    """Lines whose literal text is not the link the file declares.

    Every line here was a live counterexample from an adversarial review pass —
    each was read wrong by the hand-rolled grammar that preceded the CommonMark
    parser, and each was found only after the previous one had been fixed. That
    history is the point: it is the record of an enumeration that did not
    converge, and the reason the links are parsed rather than pattern-matched
    now. Deleting a case needs a reason; adding one is always welcome.

    A mis-read here is not cosmetic. ``--fix`` deletes a line on the strength of
    its target classifying ``missing_target``, so reading a live pointer wrong
    destroys memory.
    """

    RESOLVED = [
        ("- [Live](notes_(v2).md)", "notes_(v2).md", "paren inside the destination"),
        (r"- [Live](notes_\(v2.md)", "notes_(v2.md", "backslash escape in the destination"),
        ("- [Live](notes_&amp;v2.md)", "notes_&v2.md", "character reference"),
        ("- [A [nested]](gone.md)", "gone.md", "nested brackets in the label"),
        (r"- [A \] title](gone.md)", "gone.md", "escaped bracket in the label"),
        ("- [한글](한글노트.md) — 메모", "한글노트.md", "non-ascii filename stays un-encoded"),
    ]

    @pytest.mark.parametrize("line,target,why", RESOLVED, ids=[w for *_, w in RESOLVED])
    def test_destination_is_resolved_not_guessed(self, line, target, why):
        """The parser reports the link the file declares — so these stay usable."""
        parsed = parse_memory_index(line + "\n")
        assert [e.target for e in parsed.entries] == [target], why
        assert parsed.ambiguous_lines == frozenset(), why

    NOT_A_POINTER = [
        ("- [A](a.md) — run `echo [x](y)`", ["a.md"], "link quoted in a code span"),
        ("- ``[literal](gone.md)``", [], "code span delimited by a backtick run"),
    ]

    @pytest.mark.parametrize("line,targets,why", NOT_A_POINTER, ids=[w for *_, w in NOT_A_POINTER])
    def test_quoted_link_is_not_an_entry(self, line, targets, why):
        """Code spans quote link syntax; quoting is not pointing."""
        parsed = parse_memory_index(line + "\n")
        assert [e.target for e in parsed.entries] == targets, why
        assert parsed.ambiguous_lines == frozenset(), why

    WONT_GUESS = [
        ("- [A](<x y.md>)", "angle-bracket form, destination holds a space"),
        ("- [Live](live.md?view=1)", "query string may not be part of the filename"),
        ("- [Live](live%2Emd)", "percent-escape may not be part of the filename"),
        ("- [Live](urn:live.md)", "a scheme that is neither a url nor a path"),
    ]

    @pytest.mark.parametrize("line,why", WONT_GUESS, ids=[w for _, w in WONT_GUESS])
    def test_uri_machinery_is_not_resolved_on_a_guess(self, line, why):
        """Parsed fine; still not called a filename. Reported, never deleted."""
        parsed = parse_memory_index(line + "\n")
        assert parsed.entries[0].unreadable is True, why
        assert parsed.ambiguous_lines == frozenset({1}), why

    def test_doubt_about_one_target_does_not_cover_for_its_neighbour(self):
        """Readability is per entry; only the refusal to delete is per line.

        Letting one odd destination silence the pointer beside it would put back
        the blind spot this parser exists to close — a dead link going unreported
        because of the company it keeps.
        """
        parsed = parse_memory_index("- [Odd](live%2Emd) · [Dead](gone.md)\n")
        assert [(e.target, e.unreadable) for e in parsed.entries] == [
            ("live%2Emd", True),
            ("gone.md", False),  # still checkable, and still checked
        ]
        assert parsed.ambiguous_lines == frozenset({1})  # the *line* stays unfixable


class TestWikilinks:
    """memtomem has wikilinks; CommonMark doesn't. The doctor must know its own.

    `[[other-memo]]` is memtomem's link syntax (`chunking/markdown.py`, and the
    agent memory convention writes it constantly). CommonMark reads
    `[[note]](미커밋)` as an ordinary link — label `[note]`, destination `미커밋`
    — so a parenthetical *after a wikilink* became a pointer at a file that
    never existed, reported at error severity. Found by post-merge smoke against
    a real index; the line-at-a-time parser it replaced never saw past the first
    link, which is why it went unnoticed until the reader widened.
    """

    NOT_POINTERS = [
        ("- [Real](real.md) — done → [[memo-b]](미커밋)", "wikilink + parenthetical note"),
        ("- [Real](real.md) — see [[memo-b|alias]](wip)", "aliased wikilink + note"),
        ("- [[memo-a]](x.md) — leading wikilink", "wikilink whose note looks like a file"),
    ]

    @pytest.mark.parametrize("line,why", NOT_POINTERS, ids=[w for _, w in NOT_POINTERS])
    def test_wikilink_parenthetical_is_not_a_pointer(self, line, why):
        targets = [e.target for e in parse_memory_index(line + "\n").entries]
        assert "미커밋" not in targets and "wip" not in targets and "x.md" not in targets, why

    SPACED_NOTES = [
        ("- [Real](real.md) — built → [[memo-b]](PR#42 merged)", "spaced note"),
        ("- [Real](real.md) — [[a]](PR#1 merged)·[[b]](not yet)", "two of them"),
    ]

    @pytest.mark.parametrize("line,why", SPACED_NOTES, ids=[w for _, w in SPACED_NOTES])
    def test_wikilink_with_a_spaced_note_is_not_unresolved_syntax(self, line, why):
        # A space means CommonMark won't read `(...)` as a destination, so the
        # whole `[[memo]](note)` stays literal text — and its `]](` then looks
        # like a pointer someone failed to close. Same collision as above, down
        # the text path rather than the link path.
        parsed = parse_memory_index(line + "\n")
        assert parsed.unresolved_syntax_lines == frozenset(), why
        assert [e.target for e in parsed.entries] == ["real.md"], why

    STILL_POINTERS = [
        ("- [[draft] Title](file.md)", "bracketed prefix in the title"),
        ("- [Title [note]](file.md)", "bracketed suffix in the title"),
        ("- [Plain](file.md)", "ordinary title"),
    ]

    @pytest.mark.parametrize("line,why", STILL_POINTERS, ids=[w for _, w in STILL_POINTERS])
    def test_bracketed_titles_are_still_pointers(self, line, why):
        # The tell is a title CommonMark reports as *wholly* bracketed — only
        # `[[x]]` in the source produces that. A title merely containing
        # brackets is an ordinary pointer and must stay checked.
        assert [e.target for e in parse_memory_index(line + "\n").entries] == ["file.md"], why

    def test_bare_wikilink_is_not_an_entry(self):
        # No parenthetical, so CommonMark sees no link at all. Pinned so that
        # "wikilinks are never pointer entries" stays a state on record: #1762
        # decided they are link-checked separately (`dangling_wikilink`,
        # info-severity, index lines only) precisely because they are not
        # entries — never `broken_link`, never counted as listed, never a
        # `--fix` candidate.
        parsed = parse_memory_index("- [[memo-a]] and [[memo-b]] — related\n")
        assert parsed.entries == ()
        assert parsed.unresolved_syntax_lines == frozenset()
        assert [t for _, t in parsed.wikilinks] == ["memo-a", "memo-b"]

    COLLECTED = [
        ("- [Topic](topic.md) — supersedes [[deleted-memo]]", ["deleted-memo"], "beside a pointer"),
        ("- [[memo|alias]] — aliased", ["memo"], "an alias never names the file"),
        ("- [[memo]](미커밋) — label shape", ["memo"], "recovered from the dropped-link shape"),
        ("- [Real](real.md) — built → [[memo-b]](PR#42 merged)", ["memo-b"], "spaced-note shape"),
        ("prose [[not-a-bullet]]", [], "non-list lines are not index surface"),
    ]

    @pytest.mark.parametrize("line,targets,why", COLLECTED, ids=[w for *_, w in COLLECTED])
    def test_wikilinks_are_collected(self, line, targets, why):
        # Every shape #1761 taught the parser to *not* read as a pointer is
        # still a wikilink at a memory file, and #1762 wants it collected —
        # from the same token stream, so quoting stays quoting (below).
        assert [t for _, t in parse_memory_index(line + "\n").wikilinks] == targets, why

    def test_quoted_wikilink_is_not_collected(self):
        # A code span quotes link syntax; an indented block is an *example* of
        # an index line. Neither points at a memory file.
        assert parse_memory_index("- `[[quoted]]` in a code span\n").wikilinks == ()
        assert parse_memory_index("Example:\n\n    - [x](y.md) [[fenced]]\n").wikilinks == ()

    ESCAPED_LABELS = [
        (r"- [\[memo\]](gone.md) — escaped", "backslash-escaped brackets"),
        ("- [&#91;memo&#93;](gone.md) — entity", "entity-encoded brackets"),
        (r"- [\[memo\]](gone.md) and `[[memo]]`", "escaped label beside a same-name code span"),
    ]

    @pytest.mark.parametrize("line,why", ESCAPED_LABELS, ids=[w for _, w in ESCAPED_LABELS])
    def test_escaped_bracket_label_is_a_pointer_not_a_wikilink(self, line, why):
        # The label is read from the *rendered* title, which decoding can forge:
        # both of these render `[memo]` from source that wrote no wikilink. Left
        # unconfirmed they'd demote an ordinary pointer — its dead target then
        # reported by nothing and offered to `--fix` by nothing. Escaping the
        # brackets is how an author says "not a wikilink"; the raw source is the
        # only place that survives.
        parsed = parse_memory_index(line + "\n")
        assert [e.target for e in parsed.entries] == ["gone.md"], why
        assert parsed.wikilinks == (), why

    ESCAPED_PROSE = [
        (r"- \[\[future]] — escaped prose", "backslash-escaped"),
        ("- &#91;[future]] — entity-encoded", "entity-encoded"),
    ]

    @pytest.mark.parametrize("line,why", ESCAPED_PROSE, ids=[w for _, w in ESCAPED_PROSE])
    def test_escaped_prose_is_not_a_wikilink(self, line, why):
        # Same decoding, the other direction: text that *renders* `[[future]]`
        # was never a wikilink in the file, so it must not raise a dangling
        # finding against a memo nobody linked.
        assert parse_memory_index(line + "\n").wikilinks == (), why

    ACCEPTED_IMPRECISION = [
        (
            r"- \[\[future]] and `[[future]]`",
            ["future"],
            "escaped prose vouched for by a code span",
        ),
        ("- [[memo&amp;x]] — entity in the target", [], "entity target is not collected"),
    ]

    @pytest.mark.parametrize(
        "line,wikilinks,why", ACCEPTED_IMPRECISION, ids=[w for *_, w in ACCEPTED_IMPRECISION]
    )
    def test_raw_confirmation_is_inline_wide(self, line, wikilinks, why):
        # The raw check asks whether the *inline* writes `[[x]]`, not whether
        # this occurrence does, so two contrived shapes stay imprecise. Pinned
        # as accepted, not overlooked: both are advisory-only (an info finding
        # raised or missed, never an error, never a `--fix` candidate), and
        # closing them means hand-rolling CommonMark's code-span and escape
        # rules over the raw source — the parsing-by-pattern this module
        # exists to avoid. The label route, where a demotion would cost a
        # pointer its link-check, is tightened instead (see above).
        assert [t for _, t in parse_memory_index(line + "\n").wikilinks] == wikilinks, why

    def test_escaped_label_pointer_stays_a_fix_candidate(self, tmp_path):
        # The end of the same thread: a demoted pointer silently drops out of
        # `--fix` too. Pinned end-to-end because the parser assertion above
        # can't see that consequence.
        text = r"- [\[memo\]](gone.md) — escaped label" + "\n"
        parsed = parse_memory_index(text)
        candidates = _missing_target_entries(text, root=tmp_path, parsed=parsed)
        eligible, skipped = _partition_candidates(candidates, parsed=parsed, root=tmp_path)
        assert [line_no for line_no, _ in eligible] == [1]
        assert not skipped

    def test_wikilink_does_not_shield_a_dead_line_from_fix(self, tmp_path):
        # #1762 considered-and-left: a wikilink is not an entry, so a line
        # whose only pointer is provably dead stays an eligible --fix
        # candidate even though deleting the line takes the wikilink with it.
        text = "- [Dead](gone.md) — supersedes [[forward-ref]]\n"
        parsed = parse_memory_index(text)
        candidates = _missing_target_entries(text, root=tmp_path, parsed=parsed)
        eligible, skipped = _partition_candidates(candidates, parsed=parsed, root=tmp_path)
        assert [line_no for line_no, _ in eligible] == [1]
        assert not skipped


class TestContestedWikilinkLabels:
    """#1774: a demotion must be paid for by the raw source, occurrence for occurrence.

    The label route's old confirmation was inline-wide, so a genuine
    `[[memo]](note)` vouched for an escaped `[\\[memo\\]](README.md)` beside it:
    both demoted, the live pointer gone from the entries, and its line — now
    reading as all-dead — offered whole to `--fix --apply`. The census that
    replaced it counts raw `[[name]](` occurrences per label name, subtracts
    the ones a delimiter-isolated claim source accounts for (a code span, an
    html attribute, a link destination or title), and refuses to demote when
    anything is left unattributable — an unknown token type, or bracket
    characters stranded in decoded text (the trace a needle split across token
    boundaries always leaves). The refusal is `contested wikilink label`:
    every same-named label stays an entry, warned as `ambiguous_index_line`,
    excluded from link-checking and from `--fix`.
    """

    CONTESTED = [
        (
            r"- [[memo]](note) and [\[memo\]](README.md)",
            [("note", True), ("README.md", True)],
            [],
            "genuine + backslash-escaped same name",
        ),
        (
            "- [[memo]](note) and [&#91;memo&#93;](README.md)",
            [("note", True), ("README.md", True)],
            [],
            "genuine + entity-encoded same name",
        ),
        (
            r"- [\[memo\]](README.md) and `[[memo]](x)`",
            [("README.md", True)],
            [],
            "a code span supplies the raw needle",
        ),
        (
            r"- [\[memo\]](README.md) and [[memo]](not yet)",
            [("README.md", True)],
            ["memo"],
            "a spaced parenthetical supplies the needle from text",
        ),
        (
            r"- [\[memo\]](README.md) and ![[memo]](x.png)",
            [("README.md", True)],
            [],
            "an image consumes the needle in syntax the census can't read",
        ),
        (
            r"- [\[memo\]](README.md) and [`[[memo]](x)`](dest.md)",
            [("README.md", True), ("dest.md", False)],
            [],
            "a code span inside another link's label",
        ),
        (
            r'- [\[memo\]](README.md) and <span data-x="[[memo]](x)">',
            [("README.md", True)],
            [],
            "an html attribute carries the needle verbatim",
        ),
        (
            r"- [\[memo\]](README.md) and [other](foo[[memo]](bar))",
            [("README.md", True), ("foo[[memo]](bar)", False)],
            [],
            "a link destination carries the needle",
        ),
        (
            r'- [\[memo\]](README.md) and [other](dest.md "[[memo]](x)")',
            [("README.md", True), ("dest.md", False)],
            [],
            "a link title carries the needle",
        ),
    ]

    @pytest.mark.parametrize(
        "line,entries,wikilinks,why", CONTESTED, ids=[w for *_, w in CONTESTED]
    )
    def test_unattributable_needle_never_demotes(self, line, entries, wikilinks, why):
        # Every row writes one raw `[[memo]](` the completed links can't be
        # proven to own. The escaped pointer must stay an entry — flagged, so
        # the *line* is protected — and the label route must collect no
        # wikilink on the guess.
        parsed = parse_memory_index(line + "\n")
        assert [(e.target, e.unreadable) for e in parsed.entries] == entries, why
        assert [t for _, t in parsed.wikilinks] == wikilinks, why
        assert parsed.ambiguous_lines == frozenset({1}), why

    def test_reference_link_cannot_smuggle_the_needle(self):
        # A reference link consumes the needle's middle, splitting it across
        # tokens so no single claim source sees it — but the split strands
        # `[other][` and `](x)` in text, and stranded brackets are exactly
        # what bracket accounting refuses to demote over.
        text = r"- [\[memo\]](README.md) and [other][[memo]](x)" + "\n\n[memo]: missing.md\n"
        parsed = parse_memory_index(text)
        assert [(e.target, e.unreadable) for e in parsed.entries] == [
            ("README.md", True),
            ("missing.md", False),
        ]
        assert parsed.wikilinks == ()
        assert 1 in parsed.ambiguous_lines

    STILL_DEMOTED = [
        ("- [[memo]](a) · [[memo]](b)", [], ["memo", "memo"], "same name twice, both real"),
        (
            "- [[memo]](note) and `[[memo]](x)`",
            [],
            ["memo"],
            "a code-span claim cancels its own needle exactly",
        ),
        (
            r"- [[memo|alias]](x) and [\[memo\]](README.md)",
            [("README.md", False)],
            ["memo"],
            "an alias groups apart from the bare name",
        ),
        (
            "- [[memo]](note) and [[memo]](PR#42 merged)",
            [],
            ["memo", "memo"],
            "a spaced same-name parenthetical claims its own needle",
        ),
        (
            "- [[b]](note) then [[a]]",
            [],
            ["b", "a"],
            "a bare wikilink is scrubbed, not stranded — and order is source order",
        ),
        (
            "- [Topic](topic.md) — built → [[memo-b]](PR#42 머지)·[[memo-c]](미커밋)",
            [("topic.md", False)],
            ["memo-b", "memo-c"],
            "the real index line that must stay quiet (#1761's own shape)",
        ),
    ]

    @pytest.mark.parametrize(
        "line,entries,wikilinks,why", STILL_DEMOTED, ids=[w for *_, w in STILL_DEMOTED]
    )
    def test_attributable_labels_still_demote(self, line, entries, wikilinks, why):
        # The census must not buy safety with blanket refusal: when the raw
        # occurrences cover every same-named label and nothing else could
        # have written them, the demotion is exactly #1761's.
        parsed = parse_memory_index(line + "\n")
        assert [(e.target, e.unreadable) for e in parsed.entries] == entries, why
        assert [t for _, t in parsed.wikilinks] == wikilinks, why
        assert parsed.ambiguous_lines == frozenset(), why

    FAIL_CLOSED_NOISE = [
        (
            "- [[memo]](note) and ![icon](x.png)",
            [("note", True)],
            [],
            "an unrelated opaque token contests a genuine wikilink",
        ),
        (
            "- [[memo]](note) and [Title [draft]](live.md)",
            [("note", True), ("live.md", False)],
            [],
            "brackets in another link's label contest the census",
        ),
        (
            "- [[memo]](note) — [WIP]",
            [("note", True)],
            [],
            "bracketed prose is a strand the scrub can't account for",
        ),
    ]

    @pytest.mark.parametrize(
        "line,entries,wikilinks,why", FAIL_CLOSED_NOISE, ids=[w for *_, w in FAIL_CLOSED_NOISE]
    )
    def test_fail_closed_noise_is_deliberate(self, line, entries, wikilinks, why):
        # These lines demoted cleanly before #1774 and now warn instead: the
        # bracket-bearing company means the raw needle is no longer provably
        # the link's own. Pinned as accepted — warn-only, and any future
        # "noise reduction" that flips one of these back to a demotion is
        # reopening the deletion path, not polishing.
        parsed = parse_memory_index(line + "\n")
        assert [(e.target, e.unreadable) for e in parsed.entries] == entries, why
        assert [t for _, t in parsed.wikilinks] == wikilinks, why
        assert parsed.ambiguous_lines == frozenset({1}), why

    def test_the_reason_names_the_doubt(self):
        # Two different doubts share the unreadable flag; the report must not
        # call a perfectly readable path "unreadable target".
        line = r"- [[memo]](note) and [\[memo\]](README.md) · [Q](x.md?v=1)"
        parsed = parse_memory_index(line + "\n")
        assert [e.unreadable_reason for e in parsed.entries] == [
            "contested wikilink label",
            "contested wikilink label",
            "unreadable target",
        ]

    def test_contested_line_is_skipped_not_eligible(self, tmp_path):
        # The issue's repro, at the unit seam `--fix` runs on: the contested
        # pointer is no candidate, and its line — dead sibling and all — is
        # skipped with a reported reason, not spliced.
        (tmp_path / "README.md").write_text("x", encoding="utf-8")
        text = r"- [[memo]](note) and [\[memo\]](README.md) and [Dead](missing.md)" + "\n"
        parsed = parse_memory_index(text)
        candidates = _missing_target_entries(text, root=tmp_path, parsed=parsed)
        assert [e.target for e in candidates] == ["missing.md"]
        eligible, skipped = _partition_candidates(candidates, parsed=parsed, root=tmp_path)
        assert eligible == []
        assert [(line_no, why) for line_no, _, why in skipped] == [
            (1, "holds a link this command will not resolve on a guess")
        ]


class TestListMarkers:
    """The parser reads every list marker CommonMark does — `--fix` still doesn't.

    The old grammar was anchored at `^\\s*[-*]\\s`, so an ordered or `+`-marked
    entry was invisible: unchecked, and counted as an orphan. Reading them is
    the fix. *Deleting* them is a separate question — ADR-0020 §1's strict
    grammar names only the `-`/`*` bullet the `MEMORY.md` contract specifies, so
    the others are skipped and reported for a human.
    """

    MARKERS = [
        ("1. [a](x.md) — ordered", "ordered list"),
        ("+ [a](x.md) — plus", "plus bullet"),
        ("* [a](x.md) — star", "star bullet"),
        ("- [a](x.md) — dash", "dash bullet"),
    ]

    @pytest.mark.parametrize("line,why", MARKERS, ids=[w for _, w in MARKERS])
    def test_every_marker_is_read(self, line, why):
        parsed = parse_memory_index(line + "\n")
        assert [e.target for e in parsed.entries] == ["x.md"], why

    @pytest.mark.parametrize(
        "body,fixable",
        [("- [Dead](gone.md) — dash\n", True), ("1. [Dead](gone.md) — ordered\n", False)],
        ids=["dash is fixable", "ordered is skipped"],
    )
    def test_fix_scope_only_covers_the_bullet_shape(self, body, fixable, tmp_path, monkeypatch):
        import memtomem.cli.memory_doctor_cmd as mod

        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        if fixable:
            assert result.exit_code == 0
            assert (mem_dir / "MEMORY.md").read_bytes() != before
        else:
            assert result.exit_code == 1
            assert "bullet entry" in result.output
            assert (mem_dir / "MEMORY.md").read_bytes() == before


class TestBlockContext:
    """A bullet is only a pointer where the *document* says it is.

    An index explains itself: it holds fenced examples of the very shape it is
    made of. Read line by line, an example is indistinguishable from the real
    thing — and its target is usually a placeholder that doesn't exist, which is
    exactly the verdict ``--fix`` deletes on. Reading blocks, not lines, is what
    tells them apart.
    """

    def test_fenced_example_is_not_a_pointer(self):
        text = (
            "- [Real](real.md) — genuine\n"
            "\n"
            "```markdown\n"
            "- [Example](gone.md) — how to write an entry\n"
            "```\n"
        )
        parsed = parse_memory_index(text)
        assert [e.target for e in parsed.entries] == ["real.md"]
        # The fence's lines are preserved as-is, so the budget still counts them.
        assert [n for n, _ in parsed.other_lines] == [2, 3, 4, 5]

    def test_indented_code_block_is_not_a_pointer(self):
        parsed = parse_memory_index("Example:\n\n    - [Example](gone.md) — indented\n")
        assert parsed.entries == ()

    # An item outgrows its line in more ways than a wrapped paragraph. Each of
    # these leaves the pointer's own paragraph exactly one line long, so only
    # the item's *structure* gives it away — and deleting the pointer's line
    # would reparent what follows as top-level markdown.
    OUTGROWS_ITS_LINE = [
        ("- [A](a.md) — hook\n  continues here\n", "lazy continuation"),
        ("- [A](a.md) — hook\n\n  second paragraph\n", "second paragraph"),
        ("- [A](a.md) — hook\n\n  ```\n  code\n  ```\n", "child fence"),
        ("- [A](a.md) — hook\n  - [B](b.md) — child\n", "nested list"),
    ]

    @pytest.mark.parametrize("text,why", OUTGROWS_ITS_LINE, ids=[w for _, w in OUTGROWS_ITS_LINE])
    def test_item_bigger_than_its_line_is_read_but_not_fixable(self, text, why):
        parsed = parse_memory_index(text)
        # The pointer is still read and checked — only --fix stands down.
        assert parsed.entries[0].target == "a.md", why
        assert parsed.entries[0].line_no == 1, why
        assert 1 in parsed.multiline_lines, why

    def test_pointer_in_a_later_paragraph_is_still_read(self):
        # The item's first paragraph is prose; the pointer is in its second.
        # Reading only the item's first inline would drop it from the report
        # entirely — a real pointer, unchecked.
        parsed = parse_memory_index("- introductory note\n\n  [Dead](gone.md) — second para\n")
        assert [(e.line_no, e.target) for e in parsed.entries] == [(3, "gone.md")]
        assert parsed.multiline_lines == frozenset({3})  # read, checked, not fixable

    def test_nested_child_is_fixable_on_its_own_line(self):
        # The parent is unfixable, but the child item *is* its line.
        parsed = parse_memory_index("- [A](a.md) — hook\n  - [B](b.md) — child\n")
        assert [(e.line_no, e.target) for e in parsed.entries] == [(1, "a.md"), (2, "b.md")]
        assert parsed.multiline_lines == frozenset({1})

    CHILDLESS_PARENTS = [
        ("-\n  - [A](a.md) — child\n", 2, "parent with no text of its own"),
        ("- ```\n  code\n  ```\n  - [A](a.md) — child\n", 4, "parent opening with a fence"),
    ]

    @pytest.mark.parametrize(
        "text,line,why", CHILDLESS_PARENTS, ids=[w for *_, w in CHILDLESS_PARENTS]
    )
    def test_parent_does_not_adopt_its_childs_pointer(self, text, line, why):
        """One pointer, recorded once, against the item it actually belongs to.

        A parent with no inline of its own must not pick up its child's: that
        counts the pointer twice, reports the link twice, and makes the child's
        line look like it carries two entries — refusing a fix that is safe.
        """
        parsed = parse_memory_index(text)
        assert [(e.line_no, e.target) for e in parsed.entries] == [(line, "a.md")], why
        assert parsed.multiline_lines == frozenset(), why

    def test_entry_before_a_blank_line_stays_fixable(self):
        # A loose list's item map swallows the blank line after it, so measuring
        # the map instead of the structure would call this a multi-line item —
        # and every entry before a paragraph break would stop being fixable.
        parsed = parse_memory_index("- [A](a.md) — hook\n\nprose after the list\n")
        assert parsed.multiline_lines == frozenset()


class TestReferenceStyleLinks:
    """A pointer whose destination is defined on another line is still a pointer.

    Lines are read one at a time so each entry keeps the line number ``--fix``
    splices by. Reference links are the construct that breaks that isolation:
    read alone, ``- [Live][live]`` has no destination and so looks like no link
    at all. The definitions are harvested from the whole document to close it.
    """

    def test_full_reference_resolves(self):
        parsed = parse_memory_index("- [Live][live] — hook\n\n[live]: live.md\n")
        assert [(e.title, e.target) for e in parsed.entries] == [("Live", "live.md")]
        assert parsed.entries[0].line_no == 1  # the pointer's line, not the definition's

    def test_collapsed_and_shortcut_references_resolve(self):
        parsed = parse_memory_index(
            "- [Coll][] — x\n- [Short] — y\n\n[coll]: c.md\n[short]: s.md\n"
        )
        assert [e.target for e in parsed.entries] == ["c.md", "s.md"]

    def test_undefined_reference_is_left_as_prose(self):
        # A shortcut reference with no definition is, to CommonMark, literal
        # text — and it is indistinguishable from ordinary bracketed prose
        # ("see [note] below"), so guessing that it meant a pointer would flag
        # hooks all over a healthy index. Pinned as intended, not overlooked.
        parsed = parse_memory_index("- [Live][nope] — no such definition\n")
        assert parsed.entries == ()
        assert parsed.unresolved_syntax_lines == frozenset()

    def test_reference_sibling_is_seen_beside_a_dead_inline_link(self):
        # The line that made this matter: read line-by-line, the live reference
        # link is invisible, so the line reads single-entry and --fix splices it
        # away — dead pointer and live sibling together.
        parsed = parse_memory_index("- [Dead](gone.md) and [Live][live]\n\n[live]: live.md\n")
        assert [e.target for e in parsed.entries] == ["gone.md", "live.md"]
        assert [e.line_no for e in parsed.entries] == [1, 1]


class TestAmbiguousLines:
    def test_unclosed_link_on_bullet_yields_no_entry_but_is_flagged(self):
        # The line has no complete link, so it produces no entry and would
        # otherwise be filed as prose — an unread pointer reported as nothing.
        parsed = parse_memory_index("- [B](b.md\n")
        assert parsed.entries == ()
        assert [n for n, _ in parsed.other_lines] == [1]
        assert parsed.ambiguous_lines == frozenset({1})

    def test_unresolved_link_syntax_beside_a_good_link_is_flagged(self):
        parsed = parse_memory_index("- [A](a.md) — and [B](b.md\n")
        # Recorded on its own terms, not inferred from "a line with no entry":
        # this line has one. Folding both doubts into a single set is what let
        # the report lose it (see test_unread_pointer_is_reported_beside_a_good_link).
        assert parsed.unresolved_syntax_lines == frozenset({1})
        assert [e.target for e in parsed.entries] == ["a.md"]
        assert parsed.ambiguous_lines == frozenset({1})  # still unfixable

    def test_the_two_doubts_are_tracked_apart(self):
        # An unreadable *target* is an entry the report can point at; unresolved
        # *syntax* has no entry at all. Only their union is --fix's business.
        parsed = parse_memory_index("- [Odd](live%2Emd) — target\n- [B](b.md\n")
        assert parsed.unresolved_syntax_lines == frozenset({2})
        assert [(e.line_no, e.unreadable) for e in parsed.entries] == [(1, True)]
        assert parsed.ambiguous_lines == frozenset({1, 2})

    def test_url_and_anchor_targets_are_not_flagged(self):
        # They carry a ``:`` / lead with ``#``, but are never resolved against
        # the filesystem, so the plain-relative rule does not apply to them.
        parsed = parse_memory_index("- [Web](https://example.com) — x\n- [Top](#section) — y\n")
        assert parsed.ambiguous_lines == frozenset()

    def test_prose_punctuation_is_not_flagged(self):
        # Over-flagging would bury a real finding under warnings about hooks:
        # an index is prose, and prose is full of backticks, brackets, parens.
        clean = (
            "- [A](a.md) — see (the note) here\n"
            "- [B](b.md) — see [note] later\n"
            "- [C](c.md) — run `mm index --force`\n"
            "- [D](d.md) — idle=`var(--muted)`\n"
            "- [E](e.md) — P2=NO-GO([why](f.md))\n"
            "- [a](x.md) · [b](y.md)\n"
            "- NS: [c](z.md) — hook\n"
            "- [A](sub/b.md#anchor) — anchored path\n"
        )
        assert parse_memory_index(clean).ambiguous_lines == frozenset()

    def test_real_world_index_is_not_flagged(self):
        # Regression pin for the noise budget: an early version of this check
        # flagged 34 of the 189 lines in a real maintainer index (every hook
        # holding a backtick), which would have made the finding worthless.
        real_shapes = (
            "- [UI polish prefs](user_ui_polish_prefs.md) — text>icon, idle=`var(--muted)`\n"
            "- [MCP 설정 위치](reference_mcp.md) — 3건: CC `.claude.json`·Codex `config.toml`\n"
            "- [STM no core import](feedback_stm.md) `from memtomem.*` 금지\n"
            "- [진행중: ADR-0026](project_adr0026.md) #1353 P0/P1 shipped·P2=NO-GO([probe 금지](f.md))\n"
        )
        assert parse_memory_index(real_shapes).ambiguous_lines == frozenset()


# ── Pure: link classification ───────────────────────────────────────


class TestClassifyLink:
    def test_existing_file_ok(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        assert classify_link("a.md", root=tmp_path, source_dir=tmp_path) == "ok"

    def test_missing_file(self, tmp_path):
        assert classify_link("gone.md", root=tmp_path, source_dir=tmp_path) == "missing_target"

    def test_dotdot_escape_is_outside_root(self, tmp_path):
        inner = tmp_path / "memory"
        inner.mkdir()
        assert classify_link("../../etc/passwd", root=inner, source_dir=inner) == "outside_root"

    def test_absolute_path_outside_root(self, tmp_path):
        inner = tmp_path / "memory"
        inner.mkdir()
        assert classify_link("/etc/hosts", root=inner, source_dir=inner) == "outside_root"

    def test_url_not_a_file(self, tmp_path):
        assert classify_link("https://example.com/x", root=tmp_path, source_dir=tmp_path) == "url"
        assert classify_link("mailto:a@b.com", root=tmp_path, source_dir=tmp_path) == "url"

    def test_anchor_only(self, tmp_path):
        assert classify_link("#section", root=tmp_path, source_dir=tmp_path) == "anchor"
        assert classify_link("", root=tmp_path, source_dir=tmp_path) == "anchor"

    def test_file_with_anchor_suffix_uses_file_part(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        assert classify_link("a.md#heading", root=tmp_path, source_dir=tmp_path) == "ok"

    def test_whitespace_target_trimmed(self, tmp_path):
        (tmp_path / "a.md").write_text("x", encoding="utf-8")
        assert classify_link("  a.md  ", root=tmp_path, source_dir=tmp_path) == "ok"


# ── Pure: budget ────────────────────────────────────────────────────


class TestBudget:
    def test_small_file_under_budget(self):
        m = measure_budget("- [A](a.md) — x\n")
        assert not m.over_budget
        assert m.line_count == 1

    def test_line_count_over_cap(self):
        m = measure_budget("\n".join(["x"] * 250))
        assert m.over_budget
        assert m.line_count == 250

    def test_byte_count_over_cap(self):
        m = measure_budget("x" * 25_000)
        assert m.over_budget
        assert m.byte_len == 25_000

    def test_overlong_line_measured_in_chars_not_bytes(self):
        # 150 CJK chars = 450 UTF-8 bytes but only 150 characters, so it must
        # NOT trip the 200-char per-line cap (char-based, not byte-based).
        m = measure_budget("가" * 150)
        assert m.overlong_lines == ()
        assert not m.over_budget
        # 201 chars does trip it.
        m2 = measure_budget("a" * 201)
        assert m2.overlong_lines == (1,)
        assert m2.over_budget


# ── Integration: real DB + real disk ────────────────────────────────


_FIXTURE_INDEXED_AT = "2026-06-01T00:00:00"


def _insert_chunk(
    backend: SqliteBackend,
    *,
    chunk_id: str,
    source_file: Path,
    access_count: int = 0,
    last_accessed_at: str | None = None,
    importance_score: float = 0.0,
    updated_at: str | None = _FIXTURE_INDEXED_AT,
    content: str | None = None,
    content_hash: str | None = None,
    heading_hierarchy: tuple[str, ...] | None = None,
    start_line: int = 0,
    end_line: int = 0,
    tags: tuple[str, ...] = (),
    valid_from_unix: int | None = None,
    valid_to_unix: int | None = None,
) -> None:
    """Insert one ``chunks`` row (read-only doctor never touches FTS).

    ``content_hash`` / ``heading_hierarchy`` / ``tags`` default to synthetic or
    empty values, which is fine for every check that only counts rows. The
    staleness checks diff real hashes *and retrieval metadata*, so those tests
    pass the values a real index run would write (see
    :func:`_insert_real_chunks`).
    """
    db = backend._get_db()
    db.execute(
        "INSERT INTO chunks (id, content, content_hash, source_file, heading_hierarchy, "
        "start_line, end_line, created_at, updated_at, access_count, last_accessed_at, "
        "importance_score, tags, valid_from_unix, valid_to_unix) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            chunk_id,
            content if content is not None else f"content of {chunk_id}",
            content_hash if content_hash is not None else f"hash-{chunk_id}",
            norm_path(source_file),
            json.dumps(list(heading_hierarchy or ())),
            start_line,
            end_line,
            "2026-06-01T00:00:00",
            updated_at,
            access_count,
            last_accessed_at,
            importance_score,
            json.dumps(list(tags)),
            valid_from_unix,
            valid_to_unix,
        ),
    )
    db.commit()


@pytest.fixture
def doctor_env(tmp_path, monkeypatch):
    """A real ``claude-memory`` dir + a tmp DB wired into a ``Mem2MemConfig``.

    Layout (disk):
      MEMORY.md, README.md   — meta/index (engine-excluded)
      alpha.md               — indexed, listed, accessed       → clean
      beta.md                — indexed, listed, never accessed → cold_candidate
      gamma.md               — NOT indexed, listed             → db_coverage
      delta.md               — indexed, NOT listed             → index_orphan

    DB also has chunks for ghost.md (no disk file → stale_source) and MEMORY.md
    (meta indexed as content → convention_violation). Returns ``(config, dir)``.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)

    # Path must end with ``/.claude/projects/<slug>/memory`` to classify as
    # ``claude-memory`` (so the index_file/exclude convention applies).
    mem_dir = tmp_path / ".claude" / "projects" / "-test-proj" / "memory"
    mem_dir.mkdir(parents=True)
    for name in ("alpha.md", "beta.md", "gamma.md", "delta.md", "README.md"):
        (mem_dir / name).write_text(f"# {name}\n\nbody\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        "- [Alpha](alpha.md) — a\n"
        "- [Beta](beta.md) — b\n"
        "- [Gamma](gamma.md) — c\n"
        "- [Missing](nonexistent.md) — broken\n"
        "- [Escape](../../../../../../etc/passwd) — escapes root\n"
        "- [Web](https://example.com) — external\n"
        "- [Anchor](#top) — in-page\n",
        encoding="utf-8",
    )

    db_path = tmp_path / "doctor.db"
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]
    return config, mem_dir


def _findings_by_check(report) -> dict[str, object]:
    return {f.check: f for f in report.findings}


@pytest.mark.asyncio
async def test_analysis_detects_all_drift_classes(doctor_env):
    config, mem_dir = doctor_env

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(
            backend,
            chunk_id="a1",
            source_file=mem_dir / "alpha.md",
            access_count=3,
            last_accessed_at="2026-06-01T12:00:00",
            importance_score=0.5,
        )
        # beta: two chunks, never accessed → cold_candidate
        _insert_chunk(backend, chunk_id="b1", source_file=mem_dir / "beta.md")
        _insert_chunk(backend, chunk_id="b2", source_file=mem_dir / "beta.md")
        # delta: indexed + accessed (not cold), not in TOC → index_orphan
        _insert_chunk(backend, chunk_id="d1", source_file=mem_dir / "delta.md", access_count=1)
        # ghost: chunk with no disk file → stale_source
        _insert_chunk(backend, chunk_id="g1", source_file=mem_dir / "ghost.md")
        # MEMORY.md indexed as content → convention_violation
        _insert_chunk(backend, chunk_id="m1", source_file=mem_dir / "MEMORY.md")
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    dir_reports = [r for r in reports if r.path != "(unowned)"]
    assert len(dir_reports) == 1
    report = dir_reports[0]

    assert report.category == "claude-memory"
    assert report.index_file == "MEMORY.md"
    # disk indexable = alpha, beta, gamma, delta (MEMORY.md/README.md excluded)
    assert report.disk_indexable == 4
    # covered = alpha, beta, delta (gamma has no chunk)
    assert report.db_covered == 3

    by = _findings_by_check(report)

    assert by["db_coverage"].items == ["gamma.md"]
    assert by["stale_source"].severity == "error"
    assert by["stale_source"].items == [norm_path(mem_dir / "ghost.md")]
    # #1928: an error-severity finding must name its remediation (the CLI's
    # own vocabulary — `mm gc orphan-sources`, not the MCP tool name).
    assert "run `mm gc orphan-sources --apply`" in by["stale_source"].summary
    assert by["convention_violation"].severity == "error"
    assert by["convention_violation"].items == [norm_path(mem_dir / "MEMORY.md")]
    assert by["cold_candidate"].severity == "info"
    assert by["cold_candidate"].count == 1
    assert by["cold_candidate"].items == ["beta.md (2 chunks)"]
    # broken links: missing_target + outside_root; url + anchor NOT reported.
    broken = by["broken_link"]
    assert broken.severity == "error"
    assert broken.count == 2
    assert any("missing_target" in i for i in broken.items)
    assert any("outside_root" in i for i in broken.items)
    assert not any("example.com" in i for i in broken.items)
    assert by["index_orphan"].items == ["delta.md"]
    assert "budget" not in by  # small index file is under budget
    # Baseline: every file is older than its chunks, so nothing is stale.
    assert "stale_index" not in by
    assert "stale_index_blocked" not in by


@pytest.mark.asyncio
async def test_multi_entry_index_reports_accurately(tmp_path, monkeypatch):
    """#1757: an index that packs entries onto a line is read in full.

    The pre-fix parser saw one entry per line and nothing at all on a
    prose-prefixed line, which produced (1) an ``index_orphan`` for every
    correctly-indexed file whose pointer wasn't first on its line, and (2) a
    ``broken_link`` blind spot — a dead pointer at position ≥2 reported clean,
    which is the worse half: a safety check that passes when it shouldn't.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-crammed" / "memory"
    mem_dir.mkdir(parents=True)
    for name in ("first.md", "second.md", "nested.md", "prose.md"):
        (mem_dir / name).write_text(f"# {name}\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        # Two entries on one line — ``second.md`` is only reachable at position 2.
        "- [First](first.md) · [Second](second.md)\n"
        # A hook parenthetical carrying a real pointer (the real-world shape).
        "- [Topic](prose.md) — P2=NO-GO([why](nested.md))\n"
        # Prose prefix: invisible to the old anchor, dead target at position 2.
        "- NS: [Live](first.md) · [Dead](gone.md)\n",
        encoding="utf-8",
    )

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "crammed.db"
    config.indexing.memory_dirs = [mem_dir]

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        for i, name in enumerate(("first.md", "second.md", "nested.md", "prose.md")):
            _insert_chunk(
                backend,
                chunk_id=f"c{i}",
                source_file=mem_dir / name,
                access_count=1,
                last_accessed_at="2026-06-01T00:00:00",
            )
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    by = _findings_by_check([r for r in reports if r.path != "(unowned)"][0])

    # Defect 1: every listed file is seen as listed — no false orphans. Before
    # the fix, second.md and nested.md (position ≥2) and prose.md (prose-prefixed
    # line) were all reported as orphans despite being correctly indexed.
    assert "index_orphan" not in by

    # Defect 2: the dead pointer at position 2 of a prose-prefixed line is
    # caught. Before the fix this line yielded no entries at all → silent pass.
    broken = by["broken_link"]
    assert broken.severity == "error"
    assert broken.count == 1
    assert "gone.md" in broken.items[0]
    assert broken.items[0].startswith("L3 ")


@pytest.mark.asyncio
async def test_paren_filename_resolves_instead_of_being_flagged(tmp_path, monkeypatch):
    """A filename with parens is a filename, and reads as one.

    ``[Live](notes_(v2).md)`` defeated the hand-rolled grammar, which sliced the
    target at the inner ``)`` and called the live pointer dead — an error-level
    finding on a healthy index, and a deletion candidate. Read as Markdown it is
    simply correct, so the right outcome is *no finding at all*.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-paren" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "notes_(v2).md").write_text("# live\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [Live](notes_(v2).md) — real file\n", encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "paren.db"
    config.indexing.memory_dirs = [mem_dir]

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(
            backend,
            chunk_id="a1",
            source_file=mem_dir / "notes_(v2).md",
            access_count=1,
            last_accessed_at="2026-06-01T00:00:00",
        )
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    report = [r for r in reports if r.path != "(unowned)"][0]

    # Resolved, listed, indexed — a clean line in every respect.
    assert report.findings == []


@pytest.mark.asyncio
async def test_dead_link_is_reported_beside_an_unreadable_one(tmp_path, monkeypatch):
    """A dead link must not hide behind the company it keeps.

    ``live%2Emd`` is a destination the doctor won't resolve, but the pointer
    beside it is an ordinary dead one. Suppressing its verdict because a
    neighbour is doubtful is the same blind spot this issue is about — a broken
    link that reports clean — just reached by a different route.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-mixed" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "live.md").write_text("# real\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [Odd](live%2Emd) · [Dead](gone.md)\n", encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "mixed.db"
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    by = _findings_by_check([r for r in reports if r.path != "(unowned)"][0])

    assert by["broken_link"].severity == "error"
    assert by["broken_link"].items == ["L1 [missing_target] gone.md"]
    assert "live%2Emd" in by["ambiguous_index_line"].items[0]


@pytest.mark.asyncio
async def test_contested_label_is_warn_not_broken_nor_dangling(tmp_path, monkeypatch):
    """#1774 at the report surface: the census's refusal is a warn that names itself.

    The contested pair is excluded from ``broken_link`` (neither ``note`` nor
    ``live.md`` is judged), excluded from ``dangling_wikilink`` (no name was
    attributed), and surfaced as ``ambiguous_index_line`` items that say
    *contested wikilink label* — not *unreadable target*, which would send the
    reader off to fix a perfectly readable path. The dead sibling still
    reports: doubt about the pair says nothing about the pointer beside it.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-contested" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "live.md").write_text("# real\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        r"- [[memo]](note) and [\[memo\]](live.md) and [Dead](gone.md)" + "\n",
        encoding="utf-8",
    )

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "contested.db"
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    by = _findings_by_check([r for r in reports if r.path != "(unowned)"][0])

    assert by["broken_link"].items == ["L1 [missing_target] gone.md"]
    assert by["ambiguous_index_line"].severity == "warn"
    assert by["ambiguous_index_line"].items == [
        "L1 [contested wikilink label] note",
        "L1 [contested wikilink label] live.md",
    ]
    assert by["ambiguous_index_line"].summary.startswith("1 line(s)")  # lines, not items
    assert "dangling_wikilink" not in by


def test_dangling_wikilink_is_info_never_error(tmp_path, monkeypatch):
    """#1762: a ``[[name]]`` with no ``name.md`` is advisory, not a failure.

    The doctor cannot tell a forward reference — which the agent memory
    convention allows (a name worth writing later) — from a stale link to a
    deleted memo, so the finding informs, never flips the exit code, and a
    wikilink that resolves is no finding at all.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-wiki" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "topic.md").write_text("# t\n", encoding="utf-8")
    (mem_dir / "linked.md").write_text("# l\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        "- [Topic](topic.md) — supersedes [[deleted-memo]], see [[linked]]\n"
        "- [Linked](linked.md) — see [[nested/other|alias]]\n",
        encoding="utf-8",
    )

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "wiki.db"
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    report = [r for r in reports if r.path != "(unowned)"][0]
    by = _findings_by_check(report)

    dangling = by["dangling_wikilink"]
    assert dangling.severity == "info"
    assert dangling.items == [
        "L1 [missing_target] [[deleted-memo]] → deleted-memo.md",
        "L2 [missing_target] [[nested/other]] → nested/other.md",
    ]
    # ``[[linked]]`` resolves → not reported; wikilinks never feed broken_link.
    assert "broken_link" not in by
    assert not any(f.severity == "error" for f in report.findings)

    # And end-to-end: an index whose only link problem is a dangling wikilink
    # exits 0 — info findings are advisory by decision (#1762).
    import memtomem.cli.memory_doctor_cmd as mod

    monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)
    result = CliRunner().invoke(cli, ["memory", "doctor"])
    assert result.exit_code == 0
    assert "wikilink" in result.output


def test_wikilink_resolution_edges(tmp_path, monkeypatch):
    """The doctor's resolution rule, where it parts from the importers'.

    Documented as "close to the import convention, deliberately more lenient",
    which only means something if the divergence is pinned: the importer
    appends `.md` unconditionally (`[[name.md]]` → `name.md.md`), while an
    author who writes the suffix means the file. An `outside_root` name is
    reported with its own class rather than as a missing file — it may well
    exist where it points.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-edges" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "suffixed.md").write_text("# s\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(
        "- [S](suffixed.md) — [[suffixed.md]] resolves, not suffixed.md.md\n"
        "- [S2](suffixed.md) — [[../../../etc/passwd]] escapes the root\n",
        encoding="utf-8",
    )

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "edges.db"
    config.indexing.memory_dirs = [mem_dir]

    report = [
        r for r in _gather_reports(config=config, inspect_dirs=[mem_dir]) if r.path != "(unowned)"
    ][0]
    by = _findings_by_check(report)

    # ``[[suffixed.md]]`` names a real file → no finding for L1; only the
    # escape is reported, and it says *why* rather than "no memory file".
    assert by["dangling_wikilink"].items == [
        "L2 [outside_root] [[../../../etc/passwd]] → ../../../etc/passwd.md"
    ]
    assert by["dangling_wikilink"].severity == "info"
    assert not any(f.severity == "error" for f in report.findings)


@pytest.mark.asyncio
async def test_unread_pointer_is_reported_beside_a_good_link(tmp_path, monkeypatch):
    """An unclosed pointer must not hide behind a readable one on its line.

    This shipped once: the report asked for "ambiguous lines that produced no
    entry" as `ambiguous_lines - entry_lines`, which excludes any line carrying
    an entry — so `[B](b.md` next to a live `[A](a.md)` was reported by neither
    path, and `--fix` never surfaces it either (the line yields no candidate
    while a.md is live). The parser-level pin passed throughout; only a pin on
    the *report* could catch it.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-unread" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [A](a.md) — and [B](b.md\n", encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "unread.db"
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    by = _findings_by_check([r for r in reports if r.path != "(unowned)"][0])

    assert by["ambiguous_index_line"].severity == "warn"
    assert "[B](b.md" in by["ambiguous_index_line"].items[0]


@pytest.mark.asyncio
async def test_unresolvable_target_feeds_neither_conclusion(tmp_path, monkeypatch):
    """A destination we won't resolve is reported, and used for nothing else.

    ``live.md?view=1`` parses fine, but whether the query is part of the
    filename is not ours to guess. Guessing either way writes a wrong finding:
    call it dead and a healthy index fails CI (and the line becomes deletable);
    call it ``live.md`` and a file is silently marked listed on an assumption.
    So it is stated as what it is, and feeds neither the broken-link nor the
    listed set — the file it may mean is left to surface as an orphan.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-query" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "live.md").write_text("# real\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [Live](live.md?view=1) — query\n", encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "query.db"
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    report = [r for r in reports if r.path != "(unowned)"][0]
    by = _findings_by_check(report)

    assert by["ambiguous_index_line"].severity == "warn"
    assert "live.md?view=1" in by["ambiguous_index_line"].items[0]
    assert "broken_link" not in by  # never guessed dead
    assert by["index_orphan"].items == ["live.md"]  # nor guessed listed
    assert not any(f.severity == "error" for f in report.findings)


@pytest.mark.asyncio
async def test_clean_dir_has_no_findings(tmp_path, monkeypatch):
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-clean" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "alpha.md").write_text("# alpha\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [Alpha](alpha.md) — a\n", encoding="utf-8")

    db_path = tmp_path / "clean.db"
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(
            backend,
            chunk_id="a1",
            source_file=mem_dir / "alpha.md",
            access_count=2,
            last_accessed_at="2026-06-01T00:00:00",
        )
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    dir_reports = [r for r in reports if r.path != "(unowned)"]
    assert len(dir_reports) == 1
    assert dir_reports[0].findings == []


def test_missing_db_is_not_created(tmp_path, monkeypatch):
    """Read-only contract: a missing DB is never created just by diagnosing.

    Pins the report-only guarantee — running the doctor against a config whose
    ``sqlite_path`` (and its parent) does not exist must leave the filesystem
    untouched and degrade to disk/index-only checks.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-absent" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")

    db_path = tmp_path / "nope" / "absent.db"  # parent dir also absent
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])

    assert not db_path.exists()
    assert not db_path.parent.exists()  # doctor must not mkdir the parent either
    note = next(r for r in reports if r.path == "(database)")
    assert note.findings[0].check == "db_unavailable"
    # With no DB, every disk file shows as uncovered.
    dir_report = next(r for r in reports if not r.path.startswith("("))
    cov = next(f for f in dir_report.findings if f.check == "db_coverage")
    assert "a.md" in cov.items


def test_old_schema_db_degrades_gracefully(tmp_path, monkeypatch):
    """A DB whose schema predates the aggregate's columns is reported, not
    crashed on, and is not migrated by the doctor."""
    import sqlite3

    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-old" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")

    db_path = tmp_path / "old.db"
    conn = sqlite3.connect(db_path)
    # chunks table missing access_count / last_accessed_at / importance_score.
    conn.execute("CREATE TABLE chunks(id TEXT, source_file TEXT)")
    conn.commit()
    conn.close()

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    note = next(r for r in reports if r.path == "(database)")
    assert note.findings[0].check == "db_unavailable"
    # The doctor must not have added the missing columns (no migration).
    conn = sqlite3.connect(db_path)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(chunks)")}
    conn.close()
    assert cols == {"id", "source_file"}


def test_corrupt_db_degrades_gracefully(tmp_path, monkeypatch):
    """A corrupt / non-SQLite file at sqlite_path must not crash the doctor.

    ``mode=ro`` opens the file, but reading it raises
    ``sqlite3.DatabaseError: file is not a database`` — the reader degrades to
    the db_unavailable note instead of propagating the error."""
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-corrupt" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "a.md").write_text("# a\n", encoding="utf-8")

    db_path = tmp_path / "corrupt.db"
    db_path.write_bytes(b"this is definitely not a sqlite database\n" * 8)

    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [mem_dir]

    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    note = next(r for r in reports if r.path == "(database)")
    assert note.findings[0].check == "db_unavailable"


@pytest.mark.asyncio
async def test_nested_roots_no_false_uncovered(tmp_path, monkeypatch):
    """Nested configured roots: a child's indexed file is the child's, not a
    false ``db_coverage`` gap under the parent.

    Disk discovery for the parent is recursive (it sees the child's files),
    but DB rows for the child are bucketed to the child by longest-prefix
    ownership. The parent report must attribute disk files the same way, so it
    reports only its own files — otherwise the child's already-indexed file
    shows as uncovered under the parent.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    parent = tmp_path / ".codex" / "memories"
    child = parent / "project-docs"
    child.mkdir(parents=True)
    (parent / "p.md").write_text("# p\n", encoding="utf-8")
    (child / "c.md").write_text("# c\n", encoding="utf-8")

    db_path = tmp_path / "nested.db"
    config = Mem2MemConfig()
    config.storage.sqlite_path = db_path
    config.indexing.memory_dirs = [parent, child]

    backend = SqliteBackend(
        config.storage, dimension=0, embedding_provider="none", embedding_model=""
    )
    await backend.initialize()
    try:
        _insert_chunk(backend, chunk_id="p1", source_file=parent / "p.md")
        _insert_chunk(backend, chunk_id="c1", source_file=child / "c.md")
    finally:
        await backend.close()

    reports = _gather_reports(config=config, inspect_dirs=[parent, child])
    parent_report = next(r for r in reports if Path(r.path) == parent.resolve())
    child_report = next(r for r in reports if Path(r.path) == child.resolve())

    # Parent owns only p.md; child owns c.md — no double counting.
    assert parent_report.disk_indexable == 1
    assert child_report.disk_indexable == 1
    # Both files are indexed, so neither dir has a coverage gap.
    assert not any(f.check == "db_coverage" for f in parent_report.findings)
    assert not any(f.check == "db_coverage" for f in child_report.findings)


# ── CLI ─────────────────────────────────────────────────────────────


# ── stale_index / stale_index_blocked (#2078) ───────────────────────


def _doctor_engine(config):
    """The same storage-less engine the doctor builds, for chunk parity."""
    from memtomem.cli.memory_doctor_cmd import _build_discovery_engine

    return _build_discovery_engine(config)


def _insert_real_chunks(backend, config, path: Path, *, updated_at=_FIXTURE_INDEXED_AT) -> int:
    """Index ``path`` the way a real run would, as far as the doctor can see.

    Writes one row per chunk the indexer's own pipeline produces, carrying the
    real ``content_hash``, ``heading_hierarchy`` and line range — the columns
    the staleness check diffs. Synthetic values would make every file look
    changed, which is exactly the false-PASS these tests exist to rule out.
    """
    chunks = _doctor_engine(config).chunk_content(path, path.read_text(encoding="utf-8"))
    for i, c in enumerate(chunks):
        _insert_chunk(
            backend,
            chunk_id=str(c.id),
            source_file=path,
            updated_at=updated_at,
            content=c.content,
            content_hash=c.content_hash,
            heading_hierarchy=tuple(c.metadata.heading_hierarchy),
            start_line=c.metadata.start_line,
            end_line=c.metadata.end_line,
            tags=tuple(c.metadata.tags),
            valid_from_unix=c.metadata.valid_from_unix,
            valid_to_unix=c.metadata.valid_to_unix,
        )
    return len(chunks)


@pytest.fixture
def stale_env(tmp_path, monkeypatch):
    """A one-file memory dir whose chunks are real and current.

    Returns ``(config, mem_dir, note, reindex)`` where ``note`` is the indexed
    file and ``reindex()`` re-reads it into the DB. The file is backdated, so
    the baseline is clean and each test creates its own drift.
    """
    import asyncio

    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-stale" / "memory"
    mem_dir.mkdir(parents=True)
    note = mem_dir / "note.md"
    note.write_text("# note\n\noriginal body zqx1\n", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text("- [Note](note.md) — n\n", encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "stale.db"
    config.indexing.memory_dirs = [mem_dir]

    def reindex(updated_at=_FIXTURE_INDEXED_AT):
        async def _go():
            backend = SqliteBackend(
                config.storage, dimension=0, embedding_provider="none", embedding_model=""
            )
            await backend.initialize()
            try:
                backend._get_db().execute("DELETE FROM chunks")
                return _insert_real_chunks(backend, config, note, updated_at=updated_at)
            finally:
                await backend.close()

        return asyncio.run(_go())

    reindex()
    return config, mem_dir, note, reindex


def _stale_findings(config, mem_dir) -> dict:
    reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
    # Drop the store-wide note reports ("(unowned)", "(database)").
    dir_reports = [r for r in reports if not r.path.startswith("(")]
    assert len(dir_reports) == 1
    return _findings_by_check(dir_reports[0])


def _set_mtime(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


class TestStaleIndex:
    def test_baseline_is_clean(self, stale_env):
        config, mem_dir, _note, _reindex = stale_env
        by = _stale_findings(config, mem_dir)
        assert "stale_index" not in by
        assert "stale_index_blocked" not in by

    def test_appended_content_is_reported(self, stale_env):
        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        by = _stale_findings(config, mem_dir)
        finding = by["stale_index"]
        assert finding.severity == "warn"
        assert finding.items == ["note.md"]
        # The remediation must name the command that actually clears it.
        assert "run `mm index <file>`" in finding.summary
        assert "stale_index_blocked" not in by

    def test_reindexing_clears_the_finding(self, stale_env):
        config, mem_dir, note, reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")
        assert "stale_index" in _stale_findings(config, mem_dir)

        # The finding clears because the chunks now match the file, which is
        # the only thing the check looks at.
        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_touch_only_is_not_reported(self, stale_env):
        """Half of why the verdict is a diff, not a timestamp (#2078).

        A ``touch`` moves the mtime while the chunks stay correct, and a no-op
        re-index writes nothing — so a mtime-based check would flag the file
        forever and ``mm index`` could never clear it. A finding whose
        remediation cannot work is worse than no finding.
        """
        config, mem_dir, note, _reindex = stale_env
        _set_mtime(note, datetime(2026, 7, 1, tzinfo=timezone.utc))
        by = _stale_findings(config, mem_dir)
        assert "stale_index" not in by
        assert "stale_index_blocked" not in by

    def test_identical_rewrite_is_not_reported(self, stale_env):
        config, mem_dir, note, _reindex = stale_env
        note.write_text(note.read_text(encoding="utf-8"), encoding="utf-8")
        _set_mtime(note, datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_metadata_only_write_cannot_hide_drift(self, stale_env):
        """The other half (#2078 review): ``updated_at`` is not a content mark.

        A tag rename bumps ``updated_at`` on the chunk without touching indexed
        content, so a mtime-vs-``MAX(updated_at)`` check would go quiet on a
        genuinely stale file the moment any tag edit landed. Content is the
        only honest signal, so this must still report.
        """
        import asyncio

        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        async def _bump():
            backend = SqliteBackend(
                config.storage, dimension=0, embedding_provider="none", embedding_model=""
            )
            await backend.initialize()
            try:
                db = backend._get_db()
                db.execute("UPDATE chunks SET updated_at = ?", ("2099-01-01T00:00:00+00:00",))
                db.commit()
            finally:
                await backend.close()

        asyncio.run(_bump())
        # Chunks now claim to be newer than any possible file mtime.
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    def test_edit_immediately_after_indexing_is_reported(self, stale_env):
        """No grace window: an edit seconds after a run is still drift.

        An earlier revision skipped files whose mtime sat within a couple of
        seconds of the index run, to absorb coarse filesystem timestamps. Since
        the verdict is a content diff, that slack bought nothing and hid real
        edits made right after indexing.
        """
        config, mem_dir, note, reindex = stale_env
        reindex()
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended one second later zqx2\n")
        _set_mtime(note, datetime(2026, 6, 1, 0, 0, 1, tzinfo=timezone.utc))

        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    def test_backdated_edit_is_reported(self, stale_env):
        """A change that keeps an older mtime (`cp -p`, `rsync -a`) is drift too."""
        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")
        _set_mtime(note, datetime(2020, 1, 1, tzinfo=timezone.utc))

        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    def test_tag_blockquote_edit_is_reported(self, stale_env):
        """A section's ``> tags: [...]`` edit is drift the doctor must see (#2124).

        The blockquote is stripped from the chunk text, so nothing the content
        hash covers moves — but the row's tags are what
        ``mem_search(tag_filter=...)`` reads, and a re-index now rewrites them.
        """
        config, mem_dir, note, reindex = stale_env
        body = "tag drift body zqx4. " * 20 + "\n"
        note.write_text(f"# t\n\n## s\n\n> tags: [alpha]\n\n{body}", encoding="utf-8")
        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

        note.write_text(f"# t\n\n## s\n\n> tags: [bravo]\n\n{body}", encoding="utf-8")
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_stale_validity_window_on_matching_text_is_reported(self, stale_env):
        """Rows whose validity window no longer matches the file are drift (#2140).

        The window comes from file-level frontmatter and is stamped on every
        chunk, so rows can disagree with the file while their text matches it
        byte for byte — exactly what a pre-#2140 index run left behind. The
        check must see that, and a re-index must clear it. Editing the
        frontmatter itself would not prove this: that moves the text of the
        chunk carrying it, which the hash comparison already catches.
        """
        import asyncio

        config, mem_dir, note, reindex = stale_env
        body = "validity body zqx6. " * 40 + "\n"
        note.write_text(
            f"---\nvalid_to: 2030-01-01\n---\n\n# t\n\n## s\n\n{body}", encoding="utf-8"
        )
        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

        async def _skew():
            backend = SqliteBackend(
                config.storage, dimension=0, embedding_provider="none", embedding_model=""
            )
            await backend.initialize()
            try:
                db = backend._get_db()
                db.execute("UPDATE chunks SET valid_to_unix = ?", (123456789,))
                db.commit()
            finally:
                await backend.close()

        asyncio.run(_skew())
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_unreadable_validity_bound_is_not_reported_as_drift(self, stale_env):
        """Junk in a numeric column is "can't tell", not evidence of an edit."""
        import asyncio

        config, mem_dir, note, reindex = stale_env
        body = "validity junk zqx7. " * 40 + "\n"
        note.write_text(f"# t\n\n## s\n\n{body}", encoding="utf-8")
        reindex()

        async def _corrupt():
            backend = SqliteBackend(
                config.storage, dimension=0, embedding_provider="none", embedding_model=""
            )
            await backend.initialize()
            try:
                db = backend._get_db()
                db.execute("UPDATE chunks SET valid_to_unix = ?", ("not-a-number",))
                db.commit()
            finally:
                await backend.close()

        asyncio.run(_corrupt())
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_tag_order_alone_is_not_reported(self, stale_env):
        """Set membership, not order — a reordered stored tuple is not drift."""
        config, mem_dir, note, reindex = stale_env
        body = "tag order body zqx5. " * 20 + "\n"
        note.write_text(f"# t\n\n## s\n\n> tags: [alpha, bravo]\n\n{body}", encoding="utf-8")
        reindex()

        import asyncio

        async def _shuffle():
            backend = SqliteBackend(
                config.storage, dimension=0, embedding_provider="none", embedding_model=""
            )
            await backend.initialize()
            try:
                db = backend._get_db()
                db.execute("UPDATE chunks SET tags = ?", ('["bravo", "alpha"]',))
                db.commit()
            finally:
                await backend.close()

        asyncio.run(_shuffle())
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_collapsing_identical_sections_is_reported(self, stale_env):
        """Dropping one of two byte-identical sections is drift (#2123).

        This was a documented blind spot: the differ keyed deletion on whether
        the hash still appeared anywhere, so a plain re-index wrote nothing and
        the check — which asks exactly that question — stayed silent while the
        orphan rows kept answering searches under a heading the file had lost.
        """
        config, mem_dir, note, reindex = stale_env
        body = "identical body sentence zqx3. " * 100 + "\n"
        note.write_text(f"# t\n\n## one\n\n{body}\n## two\n\n{body}", encoding="utf-8")
        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

        note.write_text(f"# t\n\n## one\n\n{body}", encoding="utf-8")
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

        reindex()
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_garbage_chunk_timestamps_change_nothing(self, stale_env):
        """Timestamps are not an input: a corrupt one neither hides nor invents.

        Legacy and corrupt ``updated_at`` text exists in the wild (the column
        has carried two formats). Whatever it says, the verdict must come from
        content — clean when the file matches, reported when it doesn't.
        """
        config, mem_dir, note, reindex = stale_env

        reindex(updated_at="not-a-timestamp")
        assert "stale_index" not in _stale_findings(config, mem_dir)

        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    @pytest.mark.asyncio
    async def test_reordered_sections_are_reported(self, tmp_path, monkeypatch):
        """Same bytes, same hashes, different answer (review #2122).

        Swapping two sections leaves every content hash intact, so the differ
        calls them all unchanged — but a re-index rewrites their line ranges,
        and retrieval reads those: `mem_search` with a context window orders a
        source's chunks by ``start_line``, so an un-refreshed index hands back
        the wrong neighbours. Position is part of what "indexed" means.
        """
        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        mem_dir = tmp_path / "mem"
        mem_dir.mkdir()
        note = mem_dir / "note.md"
        section_a = "## alpha\n\n" + ("alpha body sentence here. " * 150) + "\n"
        section_b = "## beta\n\n" + ("beta body sentence here. " * 150) + "\n"
        note.write_text(f"# title\n\n{section_a}\n{section_b}", encoding="utf-8")

        config = Mem2MemConfig()
        config.storage.sqlite_path = tmp_path / "reorder.db"
        config.indexing.memory_dirs = [mem_dir]

        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        await backend.initialize()
        try:
            assert _insert_real_chunks(backend, config, note) > 1
        finally:
            await backend.close()

        assert "stale_index" not in _stale_findings(config, mem_dir)

        note.write_text(f"# title\n\n{section_b}\n{section_a}", encoding="utf-8")
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    def test_concurrent_reindex_does_not_produce_a_finding(self, stale_env, monkeypatch):
        """A background re-index landing mid-run must not be reported (review #2122).

        The per-dir chunk state is snapshotted before files are read, and the
        fs watcher indexes while the doctor runs. Simulate the race: the
        snapshot is stale, the live DB is already current. Reporting drift that
        a running server has fixed is the one false positive a report-only tool
        cannot explain away.
        """
        import memtomem.cli.memory_doctor_cmd as mod

        config, mem_dir, note, reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        real_read = mod._read_chunk_states

        def _snapshot_then_reindex(db_path, dir_key):
            snapshot = real_read(db_path, dir_key)  # pre-edit state
            reindex()  # the watcher catches up before we look at the file
            return snapshot

        monkeypatch.setattr(mod, "_read_chunk_states", _snapshot_then_reindex)
        by = _stale_findings(config, mem_dir)
        assert "stale_index" not in by
        assert "stale_index_blocked" not in by

    def test_failed_recheck_keeps_the_confirmed_finding(self, stale_env, monkeypatch):
        """A blip on the second look is not evidence of freshness (review #2122).

        The re-confirm exists to drop findings a concurrent re-index already
        fixed. It must not also drop findings because the re-read itself failed
        — that turns a transient lock into a silent false negative.
        """
        import memtomem.cli.memory_doctor_cmd as mod

        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        monkeypatch.setattr(mod, "_read_source_state", lambda *a, **kw: None)
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    def test_concurrently_emptied_source_defers_to_coverage(self, stale_env, monkeypatch):
        """Losing every chunk mid-run is an uncovered file, not a stale one."""
        import memtomem.cli.memory_doctor_cmd as mod

        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        monkeypatch.setattr(mod, "_read_source_state", lambda *a, **kw: {})
        reports = _gather_reports(config=config, inspect_dirs=[mem_dir])
        report = next(r for r in reports if not r.path.startswith("("))
        by = _findings_by_check(report)
        assert "stale_index" not in by
        # ...and the run must not go silent: the file is now uncovered, and
        # the coverage count has to agree with the finding it just emitted.
        assert by["db_coverage"].items == ["note.md"]
        assert report.db_covered == 0

    @pytest.mark.asyncio
    async def test_items_name_the_file_its_remediation_needs(self, tmp_path, monkeypatch):
        """Two same-named files under one root must be told apart."""
        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        mem_dir = tmp_path / "mem"
        (mem_dir / "a").mkdir(parents=True)
        (mem_dir / "b").mkdir(parents=True)
        first, second = mem_dir / "a" / "note.md", mem_dir / "b" / "note.md"
        for f in (first, second):
            f.write_text("# note\n\noriginal body zqx1\n", encoding="utf-8")

        config = Mem2MemConfig()
        config.storage.sqlite_path = tmp_path / "names.db"
        config.indexing.memory_dirs = [mem_dir]

        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        await backend.initialize()
        try:
            for f in (first, second):
                _insert_real_chunks(backend, config, f)
        finally:
            await backend.close()

        with second.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        items = _stale_findings(config, mem_dir)["stale_index"].items
        assert items == [str(Path("b") / "note.md")]

    def test_malformed_line_range_degrades_instead_of_crashing(self, stale_env):
        """Junk in a numeric column is a read failure, not a finding."""
        import asyncio

        config, mem_dir, note, _reindex = stale_env

        async def _corrupt():
            backend = SqliteBackend(
                config.storage, dimension=0, embedding_provider="none", embedding_model=""
            )
            await backend.initialize()
            try:
                db = backend._get_db()
                db.execute("UPDATE chunks SET start_line = ?", ("not-a-number",))
                db.commit()
            finally:
                await backend.close()

        asyncio.run(_corrupt())
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        by = _stale_findings(config, mem_dir)  # must not raise
        assert "stale_index" not in by

    def test_emptied_file_is_reported(self, stale_env):
        """Chunks with nothing left to match are drift too (a pure to_delete)."""
        config, mem_dir, note, _reindex = stale_env
        note.write_text("", encoding="utf-8")
        _set_mtime(note, datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert _stale_findings(config, mem_dir)["stale_index"].items == ["note.md"]

    def test_binary_and_oversized_files_are_skipped(self, stale_env, monkeypatch):
        """A file the indexer would decline is one the doctor declines too."""
        import memtomem.indexing.engine as engine_mod

        config, mem_dir, note, _reindex = stale_env
        note.write_bytes(b"# note\n\n\x00\x00 binary now\n")
        _set_mtime(note, datetime(2026, 7, 1, tzinfo=timezone.utc))
        assert "stale_index" not in _stale_findings(config, mem_dir)

        note.write_text("# note\n\nplain but huge zqx2\n", encoding="utf-8")
        _set_mtime(note, datetime(2026, 7, 1, tzinfo=timezone.utc))
        monkeypatch.setattr(engine_mod, "_MAX_INDEX_FILE_BYTES", 4)
        assert "stale_index" not in _stale_findings(config, mem_dir)

    def test_cli_reports_stale_index_without_failing_the_exit(self, stale_env, monkeypatch):
        """Warn-severity: visible in both surfaces, but CI stays green."""
        import memtomem.cli.memory_doctor_cmd as mod

        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

        result = CliRunner().invoke(cli, ["memory", "doctor"])
        assert result.exit_code == 0
        assert "note.md" in result.output
        assert "mm index <file>" in result.output

        result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])
        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["status"] == "issues"
        finding = next(
            f for d in payload["dirs"] for f in d["findings"] if f["check"] == "stale_index"
        )
        assert finding == {
            "check": "stale_index",
            "severity": "warn",
            "count": 1,
            "summary": finding["summary"],
            "items": ["note.md"],
        }

    def test_unreadable_db_yields_no_staleness_findings(self, stale_env):
        """Degrade like every other DB-backed check rather than crashing."""
        config, mem_dir, note, _reindex = stale_env
        with note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")
        Path(config.storage.sqlite_path).write_bytes(b"not a database")

        by = _stale_findings(config, mem_dir)
        assert "stale_index" not in by
        assert "stale_index_blocked" not in by


class TestStaleIndexBlocked:
    SECRET_LINE = "api_key = " + "z" * 24

    def _make_blocked(self, note: Path) -> None:
        with note.open("a", encoding="utf-8") as fh:
            fh.write(f"\n{self.SECRET_LINE}\n")

    def test_redaction_hit_routes_to_its_own_check(self, stale_env):
        """`mm index` skips these, so they must not be told to run it."""
        config, mem_dir, note, _reindex = stale_env
        self._make_blocked(note)

        by = _stale_findings(config, mem_dir)
        assert "stale_index" not in by
        blocked = by["stale_index_blocked"]
        assert blocked.severity == "warn"
        assert blocked.items == ["note.md"]
        assert "skips them" in blocked.summary
        # Both real ways out are named: `mm index` alone is not one of them,
        # but the audited bypass is (and is refused for project_shared).
        assert "--force-unsafe" in blocked.summary
        assert "project_shared" in blocked.summary

    def test_matched_text_never_reaches_the_report(self, stale_env):
        """#2076's discipline: counts, never the matched bytes."""
        config, mem_dir, note, _reindex = stale_env
        self._make_blocked(note)

        blocked = _stale_findings(config, mem_dir)["stale_index_blocked"]
        rendered = blocked.summary + " " + " ".join(blocked.items) + json.dumps(blocked.to_json())
        assert "z" * 24 not in rendered
        assert self.SECRET_LINE not in rendered

    @pytest.mark.asyncio
    async def test_matched_bytes_never_reach_the_logs(self, tmp_path, monkeypatch, caplog):
        """A chunker must never see un-adjudicated bytes it can echo (review #2122).

        The indexer gets this for free — it refuses a blocked file before
        chunking. The doctor has to chunk to diff, and the structured chunker
        logs parse failures with ``exc_info=True``; PyYAML quotes the offending
        source line, so a secret on a malformed line lands in the DEBUG log.
        Reproduced before the fix: 1 leaking record.
        """
        import logging

        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        mem_dir = tmp_path / "mem"
        mem_dir.mkdir()
        note = mem_dir / "conf.yaml"
        secret = "sk_live_" + "q" * 25
        note.write_text("ok: 1\n", encoding="utf-8")

        config = Mem2MemConfig()
        config.storage.sqlite_path = tmp_path / "leak.db"
        config.indexing.memory_dirs = [mem_dir]

        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        await backend.initialize()
        try:
            _insert_real_chunks(backend, config, note)
        finally:
            await backend.close()

        # Drift + a redaction hit sitting on the line YAML will choke on.
        note.write_text(f"ok: 1\nbroken: api_key: {secret}\n", encoding="utf-8")

        with caplog.at_level(logging.DEBUG):
            by = _stale_findings(config, mem_dir)

        assert by["stale_index_blocked"].items == ["conf.yaml"]
        assert secret not in caplog.text
        assert "sk_live_" not in caplog.text

    def test_markdown_items_get_the_frontmatter_remedy(self, stale_env):
        """#2076's third way out, named only where it can be typed."""
        config, mem_dir, note, _reindex = stale_env
        assert note.suffix == ".md"
        self._make_blocked(note)

        blocked = _stale_findings(config, mem_dir)["stale_index_blocked"]
        assert "redaction: documents-patterns" in blocked.summary
        assert "frontmatter" in blocked.summary

    @pytest.mark.asyncio
    async def test_non_markdown_items_do_not_get_the_frontmatter_remedy(
        self, tmp_path, monkeypatch
    ):
        """A ``.yaml`` source has no frontmatter block to declare it in, so
        naming the declaration there would be advice that cannot work."""
        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        mem_dir = tmp_path / "mem"
        mem_dir.mkdir()
        note = mem_dir / "conf.yaml"
        note.write_text("ok: 1\n", encoding="utf-8")

        config = Mem2MemConfig()
        config.storage.sqlite_path = tmp_path / "yaml.db"
        config.indexing.memory_dirs = [mem_dir]

        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        await backend.initialize()
        try:
            _insert_real_chunks(backend, config, note)
        finally:
            await backend.close()

        note.write_text(f"ok: 1\nsecret_key: {'y' * 24}\n", encoding="utf-8")

        blocked = _stale_findings(config, mem_dir)["stale_index_blocked"]
        assert blocked.items == ["conf.yaml"]
        assert "redaction: documents-patterns" not in blocked.summary
        assert "--force-unsafe" in blocked.summary

    def test_declared_exemption_routes_to_plain_stale_index(self, stale_env):
        """#2076: a file the guard would *honour* is not blocked at all.

        `mm index <file>` clears it, so it belongs in ``stale_index`` — routing
        it to ``stale_index_blocked`` would hand the reader a bypass they do
        not need. This is why the routing asks the engine's judgement instead
        of re-implementing it with a bare ``privacy.scan``.
        """
        config, mem_dir, note, _reindex = stale_env
        note.write_text(
            "---\nredaction: documents-patterns\n---\n\n"
            "The guard matches `api_key=` on the keyword alone.\n",
            encoding="utf-8",
        )

        by = _stale_findings(config, mem_dir)
        assert "stale_index_blocked" not in by
        assert by["stale_index"].items == ["note.md"]
        assert "mm index <file>" in by["stale_index"].summary

    def test_a_crlf_declaration_is_honoured_on_the_indexing_path(self, stale_env):
        """CRLF frontmatter declares here, because the indexer reads text (#2310).

        `Path.read_text` folds CRLF before the declaration reader or the chunker
        ever sees the file, so a Windows-authored note is not a special case on
        this path — a fact `docs/guides/reference/core-memory-tools.md` states
        and the bundle transport had to re-establish for verbatim bytes.

        Fails if the indexer ever switches to a byte-verbatim read without
        routing it through ``indexer_text``.
        """
        config, mem_dir, note, _reindex = stale_env
        note.write_bytes(
            b"---\r\nredaction: documents-patterns\r\n---\r\n\r\n"
            b"The guard matches `api_key=` on the keyword alone.\r\n"
        )

        by = _stale_findings(config, mem_dir)
        assert "stale_index_blocked" not in by
        assert by["stale_index"].items == ["note.md"]

    def test_declared_exemption_with_a_real_token_stays_blocked(self, stale_env):
        """The declaration waives label rules only; a token re-blocks."""
        config, mem_dir, note, _reindex = stale_env
        note.write_text(
            "---\nredaction: documents-patterns\n---\n\n"
            "The guard matches `api_key=` on the keyword alone.\n"
            f"token: sk-{'a' * 24}\n",
            encoding="utf-8",
        )

        by = _stale_findings(config, mem_dir)
        assert "stale_index" not in by
        assert by["stale_index_blocked"].items == ["note.md"]

    def test_blocked_file_that_did_not_change_is_not_reported(self, stale_env):
        """Only *drift* is a finding — a blocked file matching its chunks isn't."""
        config, mem_dir, note, reindex = stale_env
        self._make_blocked(note)
        reindex()  # chunks now match the blocked content
        _set_mtime(note, datetime(2026, 7, 1, tzinfo=timezone.utc))

        by = _stale_findings(config, mem_dir)
        assert "stale_index_blocked" not in by
        assert "stale_index" not in by


class TestStaleIndexScoping:
    """The per-dir chunk-state query must select this dir's rows and no others."""

    @pytest.mark.asyncio
    async def test_nested_roots_attribute_drift_to_the_owning_dir(self, tmp_path, monkeypatch):
        """A nested memory dir owns its own subtree, for staleness as for coverage.

        Both roots hold a real, current index; only the child's file drifts.
        An over-broad prefix (or one normalized differently from the persisted
        ``source_file``) shows up here as the parent reporting the child's
        file, or as neither report seeing it at all.
        """
        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        parent = tmp_path / "root"
        child = parent / "nested"
        sibling = tmp_path / "root-adjacent"  # shares the parent's prefix sans separator
        for d in (parent, child, sibling):
            d.mkdir(parents=True, exist_ok=True)
        p_note = parent / "p.md"
        c_note = child / "c.md"
        s_note = sibling / "s.md"
        for f in (p_note, c_note, s_note):
            f.write_text(f"# {f.stem}\n\noriginal body zqx1\n", encoding="utf-8")

        config = Mem2MemConfig()
        config.storage.sqlite_path = tmp_path / "nested.db"
        config.indexing.memory_dirs = [parent, child, sibling]

        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        await backend.initialize()
        try:
            for f in (p_note, c_note, s_note):
                _insert_real_chunks(backend, config, f)
        finally:
            await backend.close()

        # Only the child's file drifts.
        with c_note.open("a", encoding="utf-8") as fh:
            fh.write("\nappended later zqx2\n")

        reports = {
            r.path: _findings_by_check(r)
            for r in _gather_reports(config=config, inspect_dirs=[parent, child, sibling])
            if not r.path.startswith("(")
        }
        assert reports[str(child.resolve())]["stale_index"].items == ["c.md"]
        assert "stale_index" not in reports[str(parent.resolve())]
        assert "stale_index" not in reports[str(sibling.resolve())]


class TestChunkContentParity:
    """``IndexEngine.chunk_content`` is the doctor's whole basis for a verdict."""

    @pytest.mark.asyncio
    async def test_doctor_chunks_match_what_indexing_persists(self, tmp_path, monkeypatch):
        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        mem_dir = tmp_path / ".claude" / "projects" / "-parity" / "memory"
        mem_dir.mkdir(parents=True)
        note = mem_dir / "note.md"
        # Carries real retrieval metadata — frontmatter validity plus per-section
        # tags — so the parity assertion below compares something other than the
        # defaults. A fixture without them would "pass" comparing None to None.
        note.write_text(
            "---\nvalid_from: 2020-01-01\nvalid_to: 2030-01-01\n---\n\n"
            "# title\n\nintro paragraph with enough words to stand alone as prose.\n\n"
            "## section one\n\n> tags: [alpha]\n\n"
            + ("body sentence for section one. " * 40)
            + "\n\n## section two\n\n> tags: [bravo]\n\n"
            + ("body sentence for section two. " * 40)
            + "\n",
            encoding="utf-8",
        )

        config = Mem2MemConfig()
        config.storage.sqlite_path = tmp_path / "parity.db"
        config.indexing.memory_dirs = [mem_dir]

        from memtomem.embedding.noop import NoopEmbedder
        from memtomem.indexing.engine import IndexEngine

        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        await backend.initialize()
        try:
            real = IndexEngine(
                storage=backend,
                embedder=NoopEmbedder(),
                config=config.indexing,
                namespace_config=config.namespace,
            )
            await real.index_file(note)
            persisted = await backend.get_chunk_index_state(note)
        finally:
            await backend.close()

        doctor = _doctor_engine(config).chunk_content(note, note.read_text(encoding="utf-8"))
        assert len(doctor) > 1, "fixture should produce several chunks"
        assert any(c.metadata.tags for c in doctor), "fixture must exercise real tags"
        assert any(c.metadata.valid_to_unix for c in doctor), "...and a real validity window"

        # Whole state tuples, as a multiset: comparing field projections
        # separately would pass even if the fields belonged to different chunks,
        # and would not notice a sixth field the doctor never learned to read.
        # This is what keeps the two chunkers' idea of a chunk identical (#2078),
        # field for field, as the state grows (#2124, #2140).
        def _state_of(chunk):
            return (
                chunk.content_hash,
                tuple(chunk.metadata.heading_hierarchy),
                tuple(chunk.metadata.tags),
                chunk.metadata.valid_from_unix,
                chunk.metadata.valid_to_unix,
            )

        assert Counter(_state_of(c) for c in doctor) == Counter(persisted.values())


class TestCli:
    def _patch_loader(self, monkeypatch, config):
        import memtomem.cli.memory_doctor_cmd as mod

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

    def test_exit_1_on_error_finding(self, doctor_env, monkeypatch):
        config, mem_dir = doctor_env
        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        import asyncio

        async def _setup():
            await backend.initialize()
            _insert_chunk(backend, chunk_id="g1", source_file=mem_dir / "ghost.md")
            await backend.close()

        asyncio.run(_setup())
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor"])
        assert result.exit_code == 1  # stale_source is error-severity
        assert "no longer exist on disk" in result.output
        # #1928: the rendered finding carries its own remediation command.
        assert "mm gc orphan-sources --apply" in result.output

    def test_json_payload_shape(self, doctor_env, monkeypatch):
        config, mem_dir = doctor_env
        backend = SqliteBackend(
            config.storage, dimension=0, embedding_provider="none", embedding_model=""
        )
        import asyncio

        async def _setup():
            await backend.initialize()
            _insert_chunk(backend, chunk_id="a1", source_file=mem_dir / "alpha.md")
            await backend.close()

        asyncio.run(_setup())
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] in ("ok", "issues")
        assert "summary" in payload and set(payload["summary"]) == {"error", "warn", "info"}
        dir_entry = next(d for d in payload["dirs"] if d["path"] != "(unowned)")
        assert dir_entry["category"] == "claude-memory"
        assert dir_entry["index_file"] == "MEMORY.md"
        for f in dir_entry["findings"]:
            assert set(f) == {"check", "severity", "count", "summary", "items"}

    def test_unconfigured_path_errors(self, doctor_env, monkeypatch, tmp_path):
        config, _ = doctor_env
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", str(tmp_path / "nope")])
        assert result.exit_code != 0
        assert "not a configured memory_dir" in result.output

    def _index_missing_finding(self, payload):
        dir_entry = next(d for d in payload["dirs"] if d["category"] == "claude-memory")
        return next(f for f in dir_entry["findings"] if f["check"] == "index_missing")

    def test_undecodable_index_is_index_missing_warn_not_traceback(self, doctor_env, monkeypatch):
        """#1769: ``UnicodeDecodeError`` is a ``ValueError``, not an ``OSError``.

        An index the doctor cannot decode must land in the existing
        ``index_missing`` warn ("could not read"), not take the command — and
        its ``--json`` payload — down with a traceback.
        """
        config, mem_dir = doctor_env
        self._patch_loader(monkeypatch, config)
        (mem_dir / "MEMORY.md").write_bytes(b"- [Dead](gone.md) \xff\xfe broken bytes\n")

        result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])

        assert result.exit_code == 0  # warn is advisory, same as a missing index
        payload = json.loads(result.output)  # a payload, not a traceback
        finding = self._index_missing_finding(payload)
        assert finding["severity"] == "warn"
        assert "could not read MEMORY.md" in finding["summary"]

    def test_oserror_index_read_is_index_missing_warn(self, doctor_env, monkeypatch):
        # The OSError arm predates #1769 but was untested; pin it while the
        # except structure is being touched.
        config, mem_dir = doctor_env
        self._patch_loader(monkeypatch, config)
        index = (mem_dir / "MEMORY.md").resolve()

        real_read_text = Path.read_text

        def deny_index(self, *args, **kwargs):
            if self.resolve() == index:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_index)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])

        assert result.exit_code == 0
        finding = self._index_missing_finding(json.loads(result.output))
        assert finding["severity"] == "warn"
        assert "could not read MEMORY.md" in finding["summary"]

    def test_blank_oserror_message_names_the_exception_class(self, doctor_env, monkeypatch):
        """``str(OSError())`` is empty — the summary must not trail off (#1771).

        Tier 1's finding never depended on the message being truthy (unlike
        Tier 2's per-file error), so this is about what the reader sees: the
        shared helper falls back to the exception class name rather than
        leaving "could not read MEMORY.md: ".
        """
        config, mem_dir = doctor_env
        self._patch_loader(monkeypatch, config)
        index = (mem_dir / "MEMORY.md").resolve()

        real_read_text = Path.read_text

        def deny_index(self, *args, **kwargs):
            if self.resolve() == index:
                raise OSError()
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", deny_index)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--json"])

        assert result.exit_code == 0
        finding = self._index_missing_finding(json.loads(result.output))
        assert finding["summary"] == "could not read MEMORY.md: OSError"


# ── Docs-as-tests parity ────────────────────────────────────────────
#
# These guards bind the public contract published in
# ``docs/guides/reference.md`` (§5 "Memory hygiene — `mm memory doctor`")
# directly to the implementation, so neither can drift unnoticed: the
# check/severity table is **parsed out of the markdown** and compared to the
# check/severity pairs **extracted from the command's own ``Finding(...)`` call
# sites via AST**. Adding, renaming, or re-classifying a check fails here until
# the reference table is edited to match — and vice versa. The budget caps
# quoted in the table, the ``--json`` status rule, and the help text are pinned
# too (memory ``feedback_docs_as_tests`` / ``feedback_docs_parity_canonical_fixture``).


def _docs_check_severities() -> dict[str, str]:
    """Parse the published check→severity table from ``reference/organization-maintenance.md``.

    Matches table rows whose first cell is a backticked identifier and whose
    second cell is a (optionally bold) severity word, so only the checks table
    rows are picked up — not the Glossary or other tables.
    """
    ref = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "guides"
        / "reference"
        / "organization-maintenance.md"
    )
    assert ref.is_file(), f"reference guide not found at {ref}"
    row = re.compile(r"^\|\s*`(\w+)`\s*\|\s*\*{0,2}(warn|error|info)\*{0,2}\s*\|")
    table: dict[str, str] = {}
    for line in ref.read_text(encoding="utf-8").splitlines():
        m = row.match(line)
        if m:
            table[m.group(1)] = m.group(2)
    return table


def _source_check_severities() -> dict[str, str]:
    """Extract the check→severity map from every ``Finding(...)`` call site in
    the command module via AST.

    Fails loudly if any ``Finding`` call omits a string-literal ``check`` /
    ``severity`` (e.g. a future check moved to a named constant) so a new check
    cannot slip past the parity guard by being non-literal.
    """
    import ast

    import memtomem.cli.memory_doctor_cmd as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    calls = 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Finding":
            continue
        calls += 1
        kw = {k.arg: k.value for k in node.keywords}
        check_node, sev_node = kw.get("check"), kw.get("severity")
        assert isinstance(check_node, ast.Constant) and isinstance(check_node.value, str), (
            f"Finding at line {node.lineno} has a non-literal/missing check= — "
            "the docs-parity guard requires string literals"
        )
        assert isinstance(sev_node, ast.Constant) and isinstance(sev_node.value, str), (
            f"Finding at line {node.lineno} has a non-literal/missing severity="
        )
        check, sev = check_node.value, sev_node.value
        # Same check at two call sites (index_missing) must agree on severity.
        assert found.get(check, sev) == sev, f"{check} has conflicting severities"
        found[check] = sev
    assert calls > 0, "no Finding(...) call sites found — was the symbol renamed?"
    return found


def _summary_tail(check: str) -> str:
    """Constant parts of ``check``'s summary f-string, straight from the AST."""
    import ast

    import memtomem.cli.memory_doctor_cmd as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Finding":
            continue
        kw = {k.arg: k.value for k in node.keywords}
        check_node = kw.get("check")
        if not (isinstance(check_node, ast.Constant) and check_node.value == check):
            continue
        summary = kw.get("summary")
        assert isinstance(summary, ast.JoinedStr), (
            f"{check} summary is no longer a single f-string — update _summary_tail"
        )
        return "".join(p.value for p in summary.values if isinstance(p, ast.Constant))
    raise AssertionError(f"no {check} Finding(...) call site found")


def _stale_source_summary_tail() -> str:
    """Extract the constant suffix of the ``stale_source`` summary from the AST.

    The summary is an f-string whose only dynamic part is the leading count;
    joining the constant parts yields the exact prose the guide duplicates in
    its example output and JSON example, so the docs assertions below track
    source edits automatically instead of pinning a second copy of the string.
    """
    import ast

    import memtomem.cli.memory_doctor_cmd as mod

    tree = ast.parse(Path(mod.__file__).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != "Finding":
            continue
        kw = {k.arg: k.value for k in node.keywords}
        check = kw.get("check")
        if not (isinstance(check, ast.Constant) and check.value == "stale_source"):
            continue
        summary = kw.get("summary")
        assert isinstance(summary, ast.JoinedStr), (
            "stale_source summary is no longer a single f-string — update "
            "_stale_source_summary_tail to match"
        )
        return "".join(p.value for p in summary.values if isinstance(p, ast.Constant))
    raise AssertionError("no stale_source Finding(...) call site found")


class TestDocsParity:
    def test_docs_table_matches_source(self):
        docs = _docs_check_severities()
        assert docs, "no check rows parsed from reference.md — did the table format change?"
        # The published markdown table must equal the implementation's checks.
        assert docs == _source_check_severities()

    def test_error_severity_set_documented(self):
        # The reference guide names exactly these three as the exit-1 drivers
        # (independent anchor: catches docs+source drifting together).
        errors = {c for c, s in _docs_check_severities().items() if s == "error"}
        assert errors == {"stale_source", "convention_violation", "broken_link"}

    def test_stale_source_remediation_pinned_across_doc_surfaces(self):
        # #1928: the remediation hint lives on four surfaces — the source
        # string, the guide's example output, its JSON example, and the
        # remediation table row. Bind the three doc surfaces to the summary
        # extracted from source so none can drift independently.
        tail = _stale_source_summary_tail()
        assert "(run `mm gc orphan-sources --apply`)" in tail
        ref = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "guides"
            / "reference"
            / "organization-maintenance.md"
        ).read_text(encoding="utf-8")
        # Example output + JSON example quote the summary verbatim (count-prefixed).
        assert ref.count(tail) == 2, (
            "guide example output / JSON example no longer quote the stale_source summary verbatim"
        )
        # The remediation table row leads with the same command.
        row = next(line for line in ref.splitlines() if line.startswith("| `stale_source` |"))
        assert "`mm gc orphan-sources --apply`" in row

    def test_stale_index_remediation_pinned_across_doc_surfaces(self):
        """#2078: the two staleness checks must not drift from their guide rows.

        ``stale_index`` is only useful if it names the command that clears it,
        and ``stale_index_blocked`` is only honest if it says that command
        *won't* — the whole reason the two are separate checks.
        """
        ref = (
            Path(__file__).resolve().parents[3]
            / "docs"
            / "guides"
            / "reference"
            / "organization-maintenance.md"
        ).read_text(encoding="utf-8")

        assert "run `mm index <file>`" in _summary_tail("stale_index")
        row = next(line for line in ref.splitlines() if line.startswith("| `stale_index` |"))
        assert "`mm index <file>`" in row
        # The guide must say the touch case is excluded — that exclusion is
        # what makes the remediation work, not a footnote.
        assert "touch" in row

        blocked_tail = _summary_tail("stale_index_blocked")
        assert "skips them" in blocked_tail
        assert "`mm index <file>`" not in blocked_tail
        blocked_row = next(
            line for line in ref.splitlines() if line.startswith("| `stale_index_blocked` |")
        )
        assert "skips" in blocked_row

    def test_staleness_checks_are_advisory(self):
        """A partially-stale store is a legitimate steady state; never exit 1."""
        source = _source_check_severities()
        assert source["stale_index"] == "warn"
        assert source["stale_index_blocked"] == "warn"

    def test_budget_caps_match_documented_numbers(self):
        import memtomem.cli.memory_doctor_cmd as mod

        # Quoted in the checks table: "24,400 bytes / 200 lines / 200 chars".
        assert mod._INDEX_MAX_BYTES == 24_400
        assert mod._INDEX_MAX_LINES == 200
        assert mod._INDEX_MAX_LINE_CHARS == 200

    def test_help_documents_usage_flags_and_exit_codes(self):
        result = CliRunner().invoke(cli, ["memory", "doctor", "--help"])
        assert result.exit_code == 0
        # Click hard-wraps help to the terminal width and breaks on hyphens, so
        # collapse whitespace and assert on non-hyphenated prose tokens.
        out = " ".join(result.output.split())
        for token in (
            "PATH",  # optional dir argument
            "--json",  # structured-output flag
            "Exit codes",  # exit-code documentation
            "stale DB sources",  # the three documented error-severity checks…
            "convention violations",
            "broken index links",
        ):
            assert token in out, f"help text missing {token!r}"

    def test_memory_group_lists_doctor(self):
        result = CliRunner().invoke(cli, ["memory", "--help"])
        assert result.exit_code == 0
        assert "doctor" in result.output
        assert "hygiene" in result.output

    def test_help_documents_fix_flags(self):
        # The Tier 2 write path (ADR-0020) must be discoverable from --help.
        # Click hard-wraps and breaks on hyphens, so assert only on the option
        # names and the underscore-joined (break-safe) scope token.
        result = CliRunner().invoke(cli, ["memory", "doctor", "--help"])
        assert result.exit_code == 0
        out = " ".join(result.output.split())
        assert "--fix" in out
        assert "--apply" in out
        assert "missing_target" in out  # the subtractive scope is documented

    _REF_DOC = (
        Path(__file__).resolve().parents[3]
        / "docs"
        / "guides"
        / "reference"
        / "organization-maintenance.md"
    )

    def test_reference_documents_fix(self):
        # reference/organization-maintenance.md §5 carries the --fix surface
        # (ADR-0020 consequence). Pin the usage examples + the subtractive-scope
        # wording so docs can't silently drift from the shipped flags.
        text = self._REF_DOC.read_text(encoding="utf-8")
        assert "mm memory doctor --fix --apply" in text
        assert "Fixing broken links" in text  # the subsection heading
        assert "subtractive" in text.lower()
        assert "0020-memory-index-write-contract" in text  # ADR link

    def test_reference_documents_the_fix_status_values(self):
        # The doc names the --json statuses and the skip exit code; drift here
        # would have a script trust "clean" for a partially-fixed index.
        text = self._REF_DOC.read_text(encoding="utf-8")
        for status in ("clean", "would-fix", "fixed", "would-partial", "partial", "error"):
            assert f"`{status}`" in text, f"§5 does not document --fix --json status {status!r}"

    def test_reference_documents_per_line_skip_not_whole_run_refusal(self):
        """The pre-#1764 contract refused the whole run; the shipped one skips per line.

        This is the wording the amendment (ADR-0020 §1) explicitly deferred to
        the implementing PR, so pin it: the doc must not drift back to promising
        a refusal the code no longer makes.
        """
        text = self._REF_DOC.read_text(encoding="utf-8")
        assert "aborts the whole `--fix` run" not in text
        assert "One entry per line, or it refuses" not in text
        assert "skipped" in text
        # §5's multiplicity guard counts lines, not links (the amended rule).
        assert "physical line occurrences" in text
        assert "which identical copy survives is unspecified" not in text

    def test_json_status_rule_matches_summary(self, capsys):
        # Documented rule: status is "issues" when any error/warn finding
        # exists; an info-only report stays "ok".
        from memtomem.cli.memory_doctor_cmd import DirReport, Finding, _emit_json

        report = DirReport(
            path="/d",
            category="user",
            index_file=None,
            exists=True,
            disk_indexable=1,
            db_covered=1,
        )
        report.findings.append(Finding(check="cold_candidate", severity="info", summary="x"))
        _emit_json([report])
        assert json.loads(capsys.readouterr().out)["status"] == "ok"

        report.findings.append(Finding(check="db_coverage", severity="warn", summary="y"))
        _emit_json([report])
        assert json.loads(capsys.readouterr().out)["status"] == "issues"


# ── Tier 2: --fix line splice (ADR-0020 §2 — byte-exact preservation) ─
#
# The splice is the load-bearing primitive of the write contract: it must keep
# every surviving line's exact terminator (LF vs CRLF) and the file's
# trailing-newline state, and a no-removal splice must be a byte-for-byte
# identity. These cases are the round-trip proof ADR-0020 §2 requires.


class TestSpliceRoundTrip:
    # Each fixture varies the EOL style and trailing-newline state; the identity
    # case (no removal) must return the input unchanged byte-for-byte.
    @pytest.mark.parametrize(
        "text",
        [
            "- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n",  # LF, trailing newline
            "- [A](a.md)\n- [B](b.md)\n- [C](c.md)",  # LF, no trailing newline
            "- [A](a.md)\r\n- [B](b.md)\r\n- [C](c.md)\r\n",  # CRLF, trailing
            "- [A](a.md)\r\n- [B](b.md)\r\n- [C](c.md)",  # CRLF, no trailing
            "",  # empty file
            "\n\n",  # blank lines only
        ],
    )
    def test_no_removal_is_byte_identity(self, text):
        assert _splice_lines(text, set()) == text

    def test_removes_only_targeted_line_lf(self):
        text = "- [A](a.md)\n- [B](b.md)\n- [C](c.md)\n"
        # Drop line 2 (B); A and C survive with their LF terminators.
        assert _splice_lines(text, {2}) == "- [A](a.md)\n- [C](c.md)\n"

    def test_removes_only_targeted_line_crlf_preserved(self):
        text = "- [A](a.md)\r\n- [B](b.md)\r\n- [C](c.md)\r\n"
        # Survivors keep CRLF — the splice never normalizes to LF.
        out = _splice_lines(text, {2})
        assert out == "- [A](a.md)\r\n- [C](c.md)\r\n"
        assert "\r\n" in out and out.count("\n") == 2

    def test_remove_last_line_without_trailing_newline(self):
        # Removing the final (un-terminated) line leaves the prior line's
        # terminator intact; no spurious newline is added or removed.
        text = "- [A](a.md)\n- [B](b.md)"
        assert _splice_lines(text, {2}) == "- [A](a.md)\n"

    def test_remove_first_of_no_trailing(self):
        text = "- [A](a.md)\n- [B](b.md)"
        assert _splice_lines(text, {1}) == "- [B](b.md)"


# ── Tier 2: missing_target-only subtractive guard (ADR-0020 §1) ───────


class TestMissingTargetGuard:
    def _index(self, tmp_path):
        """A claude-style index with one of every link-class + an existing file."""
        (tmp_path / "exists.md").write_text("x", encoding="utf-8")
        text = (
            "- [Ok](exists.md) — present\n"
            "- [Dead](gone.md) — missing target\n"
            "- [Escape](../../../etc/passwd) — outside root\n"
            "- [Web](https://example.com) — url\n"
            "- [Anchor](#section) — anchor\n"
        )
        return text

    def test_selects_only_missing_target(self, tmp_path):
        text = self._index(tmp_path)
        entries = _missing_target_entries(text, root=tmp_path)
        # ONLY the missing-target line — outside_root/url/anchor/ok excluded.
        assert [e.target for e in entries] == ["gone.md"]
        assert entries[0].line_no == 2

    def test_reappeared_target_not_selected(self, tmp_path):
        text = "- [Dead](gone.md) — x\n"
        assert _missing_target_entries(text, root=tmp_path)  # gone.md absent → selected
        (tmp_path / "gone.md").write_text("back", encoding="utf-8")
        assert _missing_target_entries(text, root=tmp_path) == []  # now present → spared

    def test_unreadable_target_is_never_a_candidate(self, tmp_path):
        """A literal miss on a target we won't resolve is not proof of death.

        ``live.md?view=1`` names no file *literally*, so it classifies
        missing_target — but the query may belong to the filename, which is the
        guess ``--fix`` must not make. Tier 1 already reports it as
        ``ambiguous_index_line``; letting it through here would make a line
        ``--fix`` has no business touching look like a candidate it declined.
        """
        (tmp_path / "live.md").write_text("x", encoding="utf-8")
        text = "- [Q](live.md?view=1) — query\n- [Dead](gone.md) — really dead\n"
        assert [e.target for e in _missing_target_entries(text, root=tmp_path)] == ["gone.md"]


# ── Tier 2: locked apply path (ADR-0020 §5) ──────────────────────────


def _analysis(text, root):
    """The T1 snapshot ``_run_fix`` hands ``_apply_fix`` for *text*.

    Built the same way the command builds it, so these tests exercise the real
    handoff rather than a hand-rolled stand-in.
    """
    from memtomem.cli.memory_doctor_cmd import _AnalysisSnapshot

    parsed = parse_memory_index(text)
    candidates = _missing_target_entries(text, root=root, parsed=parsed)
    eligible, _ = _partition_candidates(candidates, parsed=parsed, root=root)
    return _AnalysisSnapshot.of(parsed, candidates=candidates, eligible=eligible)


class TestApplyFix:
    def _setup(self, tmp_path):
        root = tmp_path / "mem"
        root.mkdir()
        (root / "exists.md").write_text("x", encoding="utf-8")
        return root

    def test_removes_missing_keeps_other_classes(self, tmp_path):
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = (
            "# TOC\n"
            "- [Ok](exists.md) — keep\n"
            "- [Dead](gone.md) — drop\n"
            "- [Escape](../../etc/passwd) — keep (outside_root, ambiguous)\n"
            "- [Web](https://x.com) — keep\n"
        )
        index.write_text(text, encoding="utf-8")
        removed, skipped = _apply_fix(index, root, _analysis(text, root))
        assert [r[1] for r in removed] == ["- [Dead](gone.md) — drop"]
        assert skipped == []
        out = index.read_text(encoding="utf-8")
        assert "gone.md" not in out
        # Every non-missing_target line — including outside_root — survives.
        for keep in ("# TOC", "exists.md", "etc/passwd", "https://x.com"):
            assert keep in out

    def test_crlf_survivors_preserved(self, tmp_path):
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Ok](exists.md) — keep\r\n- [Dead](gone.md) — drop\r\n"
        index.write_bytes(text.encode("utf-8"))  # write CRLF bytes verbatim
        _apply_fix(index, root, _analysis(text, root))
        raw = index.read_bytes()
        # Survivor keeps CRLF; the dropped line is gone; no LF normalization.
        assert raw == b"- [Ok](exists.md) \xe2\x80\x94 keep\r\n"

    def test_no_trailing_newline_preserved(self, tmp_path):
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Ok](exists.md) — a\n- [Dead](gone.md) — b"  # no EOF newline
        index.write_text(text, encoding="utf-8")
        _apply_fix(index, root, _analysis(text, root))
        # Dropping the un-terminated last line leaves the survivor's LF intact.
        assert index.read_text(encoding="utf-8") == "- [Ok](exists.md) — a\n"

    @pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX mode bits; NTFS ignores them (atomic_write preserves access via ACL inheritance)",
    )
    def test_file_mode_preserved_not_downgraded(self, tmp_path):
        import os
        import stat

        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n- [Ok](exists.md) — y\n"
        index.write_text(text, encoding="utf-8")
        os.chmod(index, 0o644)  # a typical TOC mode, NOT atomic_write's 0o600
        _apply_fix(index, root, _analysis(text, root))
        assert stat.S_IMODE(index.stat().st_mode) == 0o644

    def test_revalidate_target_reappeared_is_spared(self, tmp_path):
        # Candidate collected while gone.md is absent; the target reappears
        # before apply (the locked re-classify sees it as ok → spares the line).
        # The drop is reported, not silently absent (ADR-0020 §1).
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n- [Ok](exists.md) — y\n"
        index.write_text(text, encoding="utf-8")
        snapshot = _analysis(text, root)
        (root / "gone.md").write_text("resurrected", encoding="utf-8")
        removed, skipped = _apply_fix(index, root, snapshot)
        assert removed == []
        # The line is no longer a dead pointer at all, so there is nothing left
        # in the file to report — and the file is untouched.
        assert skipped == []
        assert index.read_text(encoding="utf-8") == text

    def test_revalidate_line_that_left_fix_scope_is_spared(self, tmp_path):
        """A guard is not a one-time admission check — the file is live.

        The candidate line is untouched between analysis and apply, so its raw
        still matches and its target is still dead. What changed is elsewhere:
        the agent defined the reference the line already cited, which turns it
        from an all-dead line into one with a live sibling on it. Re-classifying
        the candidate's own target can't see that; §1's test has to be re-asked
        of the fresh read.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) and [Live][live]\n"
        index.write_text(analysis_text, encoding="utf-8")
        snapshot = _analysis(analysis_text, root)
        assert snapshot.eligible  # eligible at analysis time: the reference is undefined

        # The agent defines the reference before the fix takes the lock.
        index.write_text(analysis_text + "\n[live]: exists.md\n", encoding="utf-8")

        removed, skipped = _apply_fix(index, root, snapshot)

        assert removed == []
        # The line is still a candidate (gone.md is still dead), so the fresh
        # partition speaks for it and names §1's reason.
        assert [(n, why) for n, _, why in skipped] == [
            (1, "carries a link that is not provably dead")
        ]
        assert "[Live][live]" in index.read_text(encoding="utf-8")

    def test_revalidate_agent_edited_candidate_line_is_spared(self, tmp_path):
        """The agent rewrote the candidate line's hook since analysis.

        Its raw no longer occurs in the fresh read, so the count that bounded the
        removal is gone and the line is spared. The report speaks of the file on
        disk, not the snapshot: it names the *rewritten* line (a dead pointer
        that is really there, at the line number it is really at), never the old
        text at a coordinate that may now belong to something else.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — old hook\n"
        snapshot = _analysis(analysis_text, root)
        index.write_text("- [Dead](gone.md) — NEW hook\n", encoding="utf-8")
        removed, skipped = _apply_fix(index, root, snapshot)
        assert removed == []
        assert [(n, raw) for n, raw, _ in skipped] == [(1, "- [Dead](gone.md) — NEW hook")]
        assert "added since analysis" in skipped[0][2]
        assert index.read_text(encoding="utf-8") == "- [Dead](gone.md) — NEW hook\n"

    def test_agent_additions_carried_through_new_dead_spared(self, tmp_path):
        # Between analysis and apply the agent appended two lines: a real pointer
        # and a *new* dead pointer that was never a candidate. The fix removes
        # only the original candidate; both additions survive (the new dead one
        # is spared because it isn't in the candidate set — it may precede a file
        # the agent is about to create). It is still *reported*: it is a dead
        # pointer this run left in the file, and a run that hid it would hand
        # back a "fixed" file with a dead pointer in it.
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — drop\n"
        snapshot = _analysis(analysis_text, root)
        fresh = (
            "- [Dead](gone.md) — drop\n"
            "- [Real](exists.md) — agent added\n"
            "- [Fresh](alsogone.md) — agent added, not yet on disk\n"
        )
        index.write_text(fresh, encoding="utf-8")
        removed, skipped = _apply_fix(index, root, snapshot)
        assert [r[1] for r in removed] == ["- [Dead](gone.md) — drop"]
        assert [(n, why) for n, _, why in skipped] == [
            (3, "added since analysis — re-run --fix to remove it")
        ]
        out = index.read_text(encoding="utf-8")
        assert "- [Dead](gone.md) — drop" not in out
        assert "- [Real](exists.md) — agent added" in out
        assert "- [Fresh](alsogone.md) — agent added, not yet on disk" in out

    def test_ineligible_twin_does_not_look_like_a_changed_count(self, tmp_path):
        """Both sides of the guard must count the same population, or it lies.

        Two byte-identical dead lines, one of them a multiline item: analysis
        finds one *eligible* copy (L1's continuation rules it out) and one
        skipped. Counting eligible copies at analysis against *all* copies in
        the fresh read makes 1 != 2 — and the fix reports a line "changed in
        number since analysis" on a file nobody touched. Nothing changed here,
        so L4 goes and only L4.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — hook\n  continues here\n\n- [Dead](gone.md) — hook\n"
        index.write_text(text, encoding="utf-8")
        snapshot = _analysis(text, root)
        assert [n for n, _ in snapshot.eligible] == [4]  # L1 is skipped at T1 (multiline)

        removed, skipped = _apply_fix(index, root, snapshot)

        assert [n for n, _ in removed] == [4]
        # L1 is still a dead pointer --fix won't take, so the fresh partition
        # reports it — with §1's reason, not a bogus "it changed" one.
        assert [(n, why) for n, _, why in skipped] == [
            (1, "is a list item that continues past this line")
        ]
        assert index.read_text(encoding="utf-8") == (
            "- [Dead](gone.md) — hook\n  continues here\n\n"
        )

    def test_duplicate_appeared_since_analysis_removes_no_copy(self, tmp_path):
        # Multiplicity guard (ADR-0020 §5): analysis (T1) saw ONE dead line; the
        # agent added a byte-identical dead pointer before apply (fresh has two).
        # The count says one copy should go but nothing about *which* — and
        # identical lines can sit in different sections, so removing either could
        # take the copy the agent meant to keep. It fails closed on both and
        # reports, rather than guessing. (Also the regression pin for the
        # frozenset-membership bug that removed every identical match, emptying
        # the file.)
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        snapshot = _analysis("- [Dead](gone.md) — drop\n", root)
        assert len(snapshot.eligible) == 1
        both = "- [Dead](gone.md) — drop\n- [Dead](gone.md) — drop\n"
        index.write_text(both, encoding="utf-8")
        removed, skipped = _apply_fix(index, root, snapshot)
        assert removed == []
        # Both copies are dead pointers left behind, so both are named (§1) —
        # one record per raw string would undercount them.
        assert [n for n, _, _ in skipped] == [1, 2]
        assert "will not guess which copy to remove" in skipped[0][2]
        assert index.read_text(encoding="utf-8") == both  # neither copy touched

    def test_mismatch_names_every_line_it_left(self, tmp_path):
        # Analysis saw two identical dead copies; the agent added a third. All
        # three stay, so all three must be in the report — a per-raw record would
        # say "1 skipped" for three dead pointers the user still has to repair.
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n- [Dead](gone.md) — x\n"
        index.write_text(text, encoding="utf-8")
        snapshot = _analysis(text, root)
        assert len(snapshot.eligible) == 2
        index.write_text(text + "- [Dead](gone.md) — x\n", encoding="utf-8")

        removed, skipped = _apply_fix(index, root, snapshot)

        assert removed == []
        assert [n for n, _, _ in skipped] == [1, 2, 3]
        assert index.read_text(encoding="utf-8") == text + "- [Dead](gone.md) — x\n"

    def test_rewritten_candidate_is_reported_at_its_fresh_line_not_its_old_one(self, tmp_path):
        """A rewritten candidate is reported as what it is now, not what it was.

        The agent both rewrites the hook *and* drops the continuation, so the
        line analysis skipped is gone and a different, eligible dead line stands
        in its place. Matching is on raw text (§5), so nothing bounds a removal —
        but the dead pointer is really there, and the report says so instead of
        calling the file clean.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — hook\n  continues here\n"
        index.write_text(analysis_text, encoding="utf-8")
        snapshot = _analysis(analysis_text, root)
        assert snapshot.eligible == ()

        index.write_text("- [Dead](gone.md) — NEW hook\n", encoding="utf-8")
        removed, skipped = _apply_fix(index, root, snapshot)

        assert removed == []
        assert [(n, raw) for n, raw, _ in skipped] == [(1, "- [Dead](gone.md) — NEW hook")]

    def test_vanished_candidate_is_not_reported_at_a_stale_coordinate(self, tmp_path):
        """A candidate the agent deleted is not "left behind" — it is gone.

        Analysis saw dead A at L1 and dead B at L2. The agent deletes A, so B
        moves up to L1 and apply removes it there. Reporting A from the snapshot
        would print L1 — the line number this very run just removed B from — for
        a line that is not in the file at all.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [A](goneA.md) — a\n- [B](goneB.md) — b\n"
        index.write_text(text, encoding="utf-8")
        snapshot = _analysis(text, root)
        assert [n for n, _ in snapshot.eligible] == [1, 2]

        index.write_text("- [B](goneB.md) — b\n", encoding="utf-8")  # the agent removed A
        removed, skipped = _apply_fix(index, root, snapshot)

        assert [(n, raw) for n, raw in removed] == [(1, "- [B](goneB.md) — b")]
        assert skipped == []
        assert index.read_text(encoding="utf-8") == ""

    def test_skipped_candidate_that_became_eligible_is_still_reported(self, tmp_path):
        """A candidate the report promised to leave behind must not vanish from it.

        Analysis skips the only candidate (a multiline item); the agent then
        deletes the continuation, making that same line eligible. Analysis
        cleared no copy of this raw, so there is no count to bound a removal by
        and the line is spared — but it is a dead pointer the dry-run named, so
        an apply that drops it from the report claims a clean file over a dead
        pointer that is still sitting in it.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — hook\n  continues here\n"
        index.write_text(analysis_text, encoding="utf-8")
        snapshot = _analysis(analysis_text, root)
        assert snapshot.eligible == ()  # analysis cleared nothing

        index.write_text("- [Dead](gone.md) — hook\n", encoding="utf-8")
        removed, skipped = _apply_fix(index, root, snapshot)

        assert removed == []
        assert [(n, why) for n, _, why in skipped] == [
            (1, "changed in eligibility since analysis — will not guess which copy to remove")
        ]
        assert index.read_text(encoding="utf-8") == "- [Dead](gone.md) — hook\n"

    def test_apply_report_never_calls_one_line_both_removed_and_skipped(self, tmp_path):
        """The apply report comes from the fresh read, never the stale snapshot.

        The agent moves the continuation from the first identical copy to the
        second, so eligibility *swaps* between two byte-identical lines: counts
        never move, and L1 — the copy analysis skipped — is now the eligible one.
        Removing it is right (the fresh file says so, and §5 preserves how many
        copies survive, not which). But a report that keeps analysis's verdicts
        would carry "L1 skipped: continues past this line" alongside this run's
        own "L1 removed", and name L3's surviving dead pointer nowhere.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — hook\n  continues here\n\n- [Dead](gone.md) — hook\n"
        index.write_text(analysis_text, encoding="utf-8")
        snapshot = _analysis(analysis_text, root)
        assert [n for n, _ in snapshot.eligible] == [4]  # analysis cleared the *second* copy

        index.write_text(
            "- [Dead](gone.md) — hook\n\n- [Dead](gone.md) — hook\n  continues here\n",
            encoding="utf-8",
        )
        removed, skipped = _apply_fix(index, root, snapshot)

        assert [n for n, _ in removed] == [1]
        assert [(n, why) for n, _, why in skipped] == [
            (3, "is a list item that continues past this line")
        ]
        assert not {n for n, _ in removed} & {n for n, _, _ in skipped}
        # One copy survives, and it is the one that still has a continuation.
        assert index.read_text(encoding="utf-8") == "\n- [Dead](gone.md) — hook\n  continues here\n"

    def test_eligibility_growing_since_analysis_is_skipped_not_a_crash(self, tmp_path):
        """The copies can stay put while *more* of them become eligible.

        Analysis: L1 is an ineligible twin (multiline), L4 is the eligible copy.
        The agent then deletes L1's continuation, so both copies are eligible —
        the raw's occurrence count never moved, but the eligible count grew from
        1 to 2. No copy has a skip reason to report, so a reader that assumes an
        ineligible one exists crashes; and since analysis only ever cleared one
        copy, removing either would be the guess the guard exists to refuse.
        """
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        analysis_text = "- [Dead](gone.md) — hook\n  continues here\n\n- [Dead](gone.md) — hook\n"
        index.write_text(analysis_text, encoding="utf-8")
        snapshot = _analysis(analysis_text, root)
        assert [n for n, _ in snapshot.eligible] == [4]

        fresh = "- [Dead](gone.md) — hook\n\n- [Dead](gone.md) — hook\n"
        index.write_text(fresh, encoding="utf-8")

        removed, skipped = _apply_fix(index, root, snapshot)

        assert removed == []
        assert [n for n, _, _ in skipped] == [1, 3]
        assert "changed in eligibility since analysis" in skipped[0][2]
        assert index.read_text(encoding="utf-8") == fresh  # untouched

    def test_duplicate_dead_lines_are_removed_when_the_count_matches(self, tmp_path):
        # The mismatch guard must not disarm the fix on a file that legitimately
        # holds the same dead line twice: analysis saw two occurrences, apply
        # sees two, so the count agrees and both go.
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — drop\n- [Ok](exists.md) — keep\n- [Dead](gone.md) — drop\n"
        index.write_text(text, encoding="utf-8")
        snapshot = _analysis(text, root)
        assert [n for n, _ in snapshot.eligible] == [1, 3]
        removed, skipped = _apply_fix(index, root, snapshot)
        assert [n for n, _ in removed] == [1, 3]
        assert skipped == []
        assert index.read_text(encoding="utf-8") == "- [Ok](exists.md) — keep\n"

    def test_apply_does_not_create_lock_artifacts_in_tree(self, tmp_path):
        # The sidecar lock lives next to the index file; it must be the only
        # extra artifact (no stray .tmp left behind after a successful replace).
        root = self._setup(tmp_path)
        index = root / "MEMORY.md"
        text = "- [Dead](gone.md) — x\n"
        index.write_text(text, encoding="utf-8")
        _apply_fix(index, root, _analysis(text, root))
        leftovers = {p.name for p in root.iterdir()} - {"MEMORY.md", "exists.md"}
        # Only the sidecar lockfile may remain; no .tmp residue from mkstemp.
        assert not any(name.endswith(".tmp") for name in leftovers)


# ── Tier 2: --fix CLI surface ────────────────────────────────────────


def _fix_env(tmp_path, monkeypatch, *, body):
    """A claude-memory dir with an existing file + a MEMORY.md *body*.

    Returns ``(config, mem_dir)``. The path classifies as ``claude-memory`` so
    ``--fix`` resolves the MEMORY.md index convention.
    """
    from helpers import isolate_memtomem_env

    isolate_memtomem_env(monkeypatch)
    mem_dir = tmp_path / ".claude" / "projects" / "-fix-proj" / "memory"
    mem_dir.mkdir(parents=True)
    (mem_dir / "exists.md").write_text("x", encoding="utf-8")
    (mem_dir / "MEMORY.md").write_text(body, encoding="utf-8")

    config = Mem2MemConfig()
    config.storage.sqlite_path = tmp_path / "fix.db"
    config.indexing.memory_dirs = [mem_dir]
    return config, mem_dir


class TestFixCli:
    def _patch_loader(self, monkeypatch, config):
        import memtomem.cli.memory_doctor_cmd as mod

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

    _BODY = "- [Ok](exists.md) — keep\n- [Dead](gone.md) — drop\n"

    def test_dry_run_previews_without_writing(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])
        assert result.exit_code == 0
        assert "Would remove" in result.output
        assert "gone.md" in result.output
        assert "--apply" in result.output
        # Dry-run must not touch the file.
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_apply_writes_and_removes_only_missing(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])
        assert result.exit_code == 0
        assert "Removed" in result.output
        out = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert out == "- [Ok](exists.md) — keep\n"

    def test_apply_without_fix_errors(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--apply"])
        assert result.exit_code != 0
        assert "--apply only applies with --fix" in result.output

    def test_clean_index_reports_nothing_to_remove(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body="- [Ok](exists.md) — keep\n")
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])
        assert result.exit_code == 0
        assert "No missing_target links to remove" in result.output

    def test_fix_json_shape(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] == "would-fix"
        assert payload["applied"] is False
        assert payload["summary"] == {"files": 1, "lines": 1, "skipped": 0, "errors": 0}
        f = payload["files"][0]
        assert f["index_file"] == "MEMORY.md"
        assert f["removed"] == [{"line": 2, "text": "- [Dead](gone.md) — drop"}]
        assert f["skipped"] == []
        assert f["error"] is None

    def test_fix_json_apply_status(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] == "fixed"
        assert payload["applied"] is True
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == "- [Ok](exists.md) — keep\n"

    # One dead line --fix can take, one it must not (the live sibling would go
    # with it). The three outcomes ADR-0020 §1 requires stay apart in --json:
    # "clean" is above, "fixed"/"would-fix" is above, and a run that left a dead
    # pointer behind is neither.
    _MIXED = "- [Dead](gone.md) — drop\n- [Dead2](gone2.md) — keep · [Live](exists.md) — keep\n"

    def test_partial_json_status_dry_run(self, tmp_path, monkeypatch):
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._MIXED)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] == "would-partial"  # NOT would-fix, NOT clean
        assert payload["summary"] == {"files": 1, "lines": 1, "skipped": 1, "errors": 0}
        f = payload["files"][0]
        assert f["removed"] == [{"line": 1, "text": "- [Dead](gone.md) — drop"}]
        assert f["skipped"] == [
            {
                "line": 2,
                "text": "- [Dead2](gone2.md) — keep · [Live](exists.md) — keep",
                "reason": "carries a link that is not provably dead",
            }
        ]
        assert result.exit_code == 1  # a dead pointer remains — not success

    def test_partial_json_status_apply(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._MIXED)
        self._patch_loader(monkeypatch, config)
        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])
        payload = json.loads(result.output)
        assert payload["status"] == "partial"
        assert payload["applied"] is True
        assert result.exit_code == 1
        # The eligible line went; the skipped one survives byte-exact.
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == (
            "- [Dead2](gone2.md) — keep · [Live](exists.md) — keep\n"
        )

    def test_apply_json_removed_and_skipped_are_disjoint(self, tmp_path, monkeypatch):
        """End to end: the --apply report describes the file the fix actually wrote.

        The agent edits the index between the analysis read and the locked apply
        read — the race ADR-0020 §5 exists for. Here it moves a continuation
        between two byte-identical dead copies, which swaps which copy is
        eligible. An --apply report that kept the analysis snapshot's verdicts
        would list the same line as both removed and skipped.
        """
        body = "- [Dead](gone.md) — hook\n  continues here\n\n- [Dead](gone.md) — hook\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        index = mem_dir / "MEMORY.md"

        # Stand in for the agent: rewrite the file after the analysis read, while
        # --fix is between T1 and taking the lock.
        import memtomem.cli.memory_doctor_cmd as mod

        real_apply = mod._apply_fix

        def edit_then_apply(*args, **kwargs):
            index.write_text(
                "- [Dead](gone.md) — hook\n\n- [Dead](gone.md) — hook\n  continues here\n",
                encoding="utf-8",
            )
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(mod, "_apply_fix", edit_then_apply)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        f = payload["files"][0]
        removed_lines = {r["line"] for r in f["removed"]}
        skipped_lines = {s["line"] for s in f["skipped"]}
        assert removed_lines and skipped_lines
        assert not removed_lines & skipped_lines, "a line was reported removed AND skipped"
        assert payload["summary"]["skipped"] == len(f["skipped"])
        # The reported coordinates come from the fresh apply-time read — L3 is
        # the surviving line's number *after* the agent's edit, not before it.
        assert skipped_lines == {3}
        assert index.read_text(encoding="utf-8") == "\n- [Dead](gone.md) — hook\n  continues here\n"

    def test_apply_never_reports_clean_over_a_surviving_dead_pointer(self, tmp_path, monkeypatch):
        """ "Clean" must mean the file has no dead pointers, not that we lost track.

        The dry-run reports this candidate as skipped. If the agent then makes
        the line eligible before the lock, the apply still cannot remove it
        (analysis cleared no copy to bound the removal) — so the one thing it
        must not do is report the file clean while the dead pointer is still in
        it.
        """
        body = "- [Dead](gone.md) — hook\n  continues here\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        index = mem_dir / "MEMORY.md"

        dry = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])
        assert json.loads(dry.output)["status"] == "would-partial"  # promised: left behind

        import memtomem.cli.memory_doctor_cmd as mod

        real_apply = mod._apply_fix

        def edit_then_apply(*args, **kwargs):
            index.write_text("- [Dead](gone.md) — hook\n", encoding="utf-8")
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(mod, "_apply_fix", edit_then_apply)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        assert "gone.md" in index.read_text(encoding="utf-8")  # still there…
        assert payload["status"] == "partial"  # …so the report says so
        assert payload["summary"]["skipped"] == 1
        assert result.exit_code == 1

    def test_apply_on_a_clean_index_still_reads_under_the_lock(self, tmp_path, monkeypatch):
        """ "Nothing to do" at analysis time is not a licence to skip the fresh read.

        The index is clean when --fix analyses it, and the agent adds a dead
        pointer before the lock. Short-circuiting on the analysis verdict would
        report clean about a file this run never re-opened — and the dead pointer
        would be in it.
        """
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body="- [Ok](exists.md) — keep\n")
        self._patch_loader(monkeypatch, config)
        index = mem_dir / "MEMORY.md"

        import memtomem.cli.memory_doctor_cmd as mod

        real_apply = mod._apply_fix

        def edit_then_apply(*args, **kwargs):
            index.write_text(
                "- [Ok](exists.md) — keep\n- [Dead](gone.md) — agent added\n", encoding="utf-8"
            )
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(mod, "_apply_fix", edit_then_apply)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "partial"
        assert payload["files"][0]["skipped"] == [
            {
                "line": 2,
                "text": "- [Dead](gone.md) — agent added",
                "reason": "added since analysis — re-run --fix to remove it",
            }
        ]
        assert result.exit_code == 1
        # Spared, not removed: no analysis-time count bounds it. A re-run clears it.
        assert "gone.md" in index.read_text(encoding="utf-8")

    def test_all_candidates_skipped_is_not_clean(self, tmp_path, monkeypatch):
        # Nothing was removed — but "no dead pointers" and "dead pointers this
        # tool won't touch" must not read the same to a script.
        body = "- [Dead](gone.md) · [Live](exists.md)\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "partial"
        assert payload["summary"] == {"files": 1, "lines": 0, "skipped": 1, "errors": 0}
        assert result.exit_code == 1
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    # ── #1769: an index --fix cannot read is an error, never "clean" ──

    _RAW_BAD = b"- [Dead](gone.md) \xff\xfe broken bytes\n"

    def test_fix_json_unreadable_index_is_error_not_clean(self, tmp_path, monkeypatch):
        """The issue's repro: --fix used to drop the file and report clean/0."""
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        (mem_dir / "MEMORY.md").write_bytes(self._RAW_BAD)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert result.exit_code == 1
        f = payload["files"][0]
        assert "utf-8" in f["error"]
        assert f["removed"] == [] and f["skipped"] == []
        assert payload["summary"] == {"files": 1, "lines": 0, "skipped": 0, "errors": 1}

    def test_fix_apply_unreadable_index_is_error_and_writes_nothing(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        (mem_dir / "MEMORY.md").write_bytes(self._RAW_BAD)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert result.exit_code == 1
        assert (mem_dir / "MEMORY.md").read_bytes() == self._RAW_BAD

    def test_unreadable_index_does_not_suppress_healthy_dir(self, tmp_path, monkeypatch):
        """One bad index must not swallow the others' report — and ``error``
        outranks ``fixed`` in the headline (the account is incomplete)."""
        config, bad_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        (bad_dir / "MEMORY.md").write_bytes(self._RAW_BAD)
        good_dir = tmp_path / ".claude" / "projects" / "-fix-proj-b" / "memory"
        good_dir.mkdir(parents=True)
        (good_dir / "exists.md").write_text("x", encoding="utf-8")
        (good_dir / "MEMORY.md").write_text(self._BODY, encoding="utf-8")
        config.indexing.memory_dirs = [bad_dir, good_dir]
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "error"  # precedence: error > fixed
        assert result.exit_code == 1
        assert payload["summary"] == {"files": 2, "lines": 1, "skipped": 0, "errors": 1}
        good = next(f for f in payload["files"] if f["error"] is None)
        bad = next(f for f in payload["files"] if f["error"] is not None)
        assert good["removed"] == [{"line": 2, "text": "- [Dead](gone.md) — drop"}]
        assert bad["removed"] == [] and bad["skipped"] == []
        # The healthy index really was fixed on disk; the bad one untouched.
        assert (good_dir / "MEMORY.md").read_text(encoding="utf-8") == "- [Ok](exists.md) — keep\n"
        assert (bad_dir / "MEMORY.md").read_bytes() == self._RAW_BAD

    def test_fix_human_output_names_unreadable_index(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body="- [Ok](exists.md) — keep\n")
        self._patch_loader(monkeypatch, config)
        (mem_dir / "MEMORY.md").write_bytes(self._RAW_BAD)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])

        assert result.exit_code == 1
        assert "Could not read MEMORY.md" in result.output
        assert "index file(s) could not be read" in result.output
        # An all-error run has verified nothing — it must not claim otherwise.
        assert "No missing_target links to remove" not in result.output

    def test_apply_time_unreadable_index_is_error_not_a_crash(self, tmp_path, monkeypatch):
        """The index decodes at T1 and turns non-UTF-8 before the lock (#1769).

        The locked fresh read inside ``_apply_fix`` must surface as a per-file
        error inside valid JSON, not as a traceback through the payload. Only
        the read is converted: nothing was written, so ``removed=[]`` is true.
        """
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        index = mem_dir / "MEMORY.md"

        import memtomem.cli.memory_doctor_cmd as mod

        real_apply = mod._apply_fix

        def corrupt_then_apply(*args, **kwargs):
            index.write_bytes(self._RAW_BAD)
            return real_apply(*args, **kwargs)

        monkeypatch.setattr(mod, "_apply_fix", corrupt_then_apply)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert result.exit_code == 1
        f = payload["files"][0]
        assert "utf-8" in f["error"]
        assert f["removed"] == []
        assert index.read_bytes() == self._RAW_BAD  # nothing was written

    def test_fix_oserror_on_read_is_error(self, tmp_path, monkeypatch):
        # Decode failures aren't the only unreadable shape; an I/O failure on
        # the same read must get the same accounting (cross-platform stand-in
        # for chmod-000).
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        index = (mem_dir / "MEMORY.md").resolve()

        real_read_bytes = Path.read_bytes

        def deny_index(self):
            if self.resolve() == index:
                raise PermissionError(13, "Permission denied")
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", deny_index)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert result.exit_code == 1
        assert "Permission denied" in payload["files"][0]["error"]

    def test_fix_blank_oserror_message_is_still_an_error(self, tmp_path, monkeypatch):
        """``str(OSError())`` is ``""`` — presence must be ``is not None``.

        A blank message under a truthiness check would drop the file from the
        report and resurrect the original false-``clean`` (#1769, review
        finding). The message falls back to the exception class name.
        """
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)
        index = (mem_dir / "MEMORY.md").resolve()

        real_read_bytes = Path.read_bytes

        def deny_index(self):
            if self.resolve() == index:
                raise OSError()
            return real_read_bytes(self)

        monkeypatch.setattr(Path, "read_bytes", deny_index)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--json"])

        payload = json.loads(result.output)
        assert payload["status"] == "error"
        assert result.exit_code == 1
        assert payload["files"][0]["error"] == "OSError"
        assert payload["summary"]["errors"] == 1

    def test_write_failure_during_apply_propagates_not_error(self, tmp_path, monkeypatch):
        """Only the *read* converts to a per-file error; write failures escape.

        An exception surfacing from the atomic replace may postdate the commit,
        so converting it to ``error`` + ``removed=[]`` could hide a write that
        happened — the audit guarantee (ADR-0020 §5) requires it to propagate
        instead (#1769).
        """
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)

        import memtomem.context._atomic as atomic_mod

        def boom(*args, **kwargs):
            raise OSError(28, "No space left on device")

        monkeypatch.setattr(atomic_mod, "atomic_write_text", boom)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        assert isinstance(result.exception, OSError)
        assert '"status"' not in result.output  # no payload claimed anything

    def test_lock_failure_during_apply_propagates_not_error(self, tmp_path, monkeypatch):
        # Same boundary from the other side of the read: failing to take the
        # lock is not "could not read the index".
        config, _ = _fix_env(tmp_path, monkeypatch, body=self._BODY)
        self._patch_loader(monkeypatch, config)

        import memtomem.context._atomic as atomic_mod

        def boom(*args, **kwargs):
            raise OSError(13, "lock dir denied")

        monkeypatch.setattr(atomic_mod, "_file_lock", boom)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        assert isinstance(result.exception, OSError)
        assert '"status"' not in result.output


class TestAllLinksDeadRule:
    """``--fix`` splices whole lines, so every link on the line must be dead.

    A line is the unit of deletion, so the question is not "how many entries?"
    but "is *every* one of them provably dead?" — one live sibling would go with
    the line. The parser sees those siblings (#1757), which is what lets a
    multi-link line be judged at all: all-dead is fixable, mixed is skipped and
    reported (ADR-0020 §1, amended #1764).
    """

    def _patch_loader(self, monkeypatch, config):
        import memtomem.cli.memory_doctor_cmd as mod

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

    # One line, three entries: the first is dead, the other two are live.
    _CRAMMED = "- [Dead](gone.md) — drop · [Live](exists.md) — keep · [Live2](exists2.md) — keep\n"

    def test_apply_skips_and_leaves_the_line_untouched(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._CRAMMED)
        (mem_dir / "exists2.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1  # a dead pointer was left behind
        assert "Skipped" in result.output
        # The live siblings must still be on disk — this is the data loss the
        # all-links-dead rule exists to prevent.
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_dry_run_skips_too(self, tmp_path, monkeypatch):
        """The preview must not advertise a removal --apply would not make."""
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._CRAMMED)
        (mem_dir / "exists2.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])

        assert result.exit_code == 1
        assert "Skipped" in result.output
        assert "Would remove" not in result.output

    def test_skip_names_the_offending_line(self, tmp_path, monkeypatch):
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=self._CRAMMED)
        (mem_dir / "exists2.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])

        assert "L1:" in result.output
        assert "not provably dead" in result.output

    def test_all_dead_multi_entry_line_is_removed(self, tmp_path, monkeypatch):
        """The rule is all-links-*dead*, not one-link-per-line.

        Every pointer on this line is gone, so no correct version of the TOC
        keeps it — the line goes whole. Under the pre-#1764 contract the mere
        entry count refused it.
        """
        body = "- [D1](gone.md) — drop · [D2](gone2.md) — drop\n- [Ok](exists.md) — keep\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert "Skipped" not in result.output
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == "- [Ok](exists.md) — keep\n"

    def test_partition_fixes_the_rest_of_the_file(self, tmp_path, monkeypatch):
        """One non-conforming line no longer blocks fixing the rest (#1758 → #1764).

        This is the whole point of the per-line partition: the mixed line
        survives byte-exact *and* the eligible dead lines around it still go.
        """
        body = (
            "# TOC\n"
            "- [Dead](gone.md) — drop\n"
            "- [Mixed](gone2.md) — keep · [Live](exists.md) — keep\n"
            "- [Dead3](gone3.md) — drop\n"
        )
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1  # the mixed line's dead pointer remains
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == (
            "# TOC\n- [Mixed](gone2.md) — keep · [Live](exists.md) — keep\n"
        )

    def test_single_entry_lines_still_fix(self, tmp_path, monkeypatch):
        """The rule is scoped to mixed lines — it must not disarm --fix."""
        body = "- [Ok](exists.md) — keep\n- [Dead](gone.md) — drop\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == "- [Ok](exists.md) — keep\n"

    def test_live_multi_entry_line_is_not_a_candidate(self, tmp_path, monkeypatch):
        """A crammed line with no dead pointer was never a candidate — not "skipped"."""
        body = "- [Live](exists.md) — keep · [Live2](exists2.md) — keep\n- [Dead](gone.md) — drop\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        (mem_dir / "exists2.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert "Skipped" not in result.output
        out = (mem_dir / "MEMORY.md").read_text(encoding="utf-8")
        assert out == "- [Live](exists.md) — keep · [Live2](exists2.md) — keep\n"

    def test_live_hook_internal_link_spares_the_line(self, tmp_path, monkeypatch):
        """A dead entry whose hook cites a *live* memo must not splice that cite away."""
        body = "- [Dead](gone.md) — see also ([why](exists.md))\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1
        assert "not provably dead" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_dead_hook_internal_link_does_not_save_the_line(self, tmp_path, monkeypatch):
        """Whether the nested link is a sibling or hook prose never has to be answered.

        Both destinations are gone, so the line-unit deletion is right either
        way — which is exactly why ADR-0020 rejected span surgery.
        """
        body = "- [Dead](gone.md) — see also ([why](gone2.md))\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == ""

    def test_outside_root_sibling_spares_the_line(self, tmp_path, monkeypatch):
        """Not-dead is broader than live: an escaping link isn't provably dead either.

        ``--fix`` never removes an ``outside_root`` link on its own line, so it
        must not remove one riding along on a candidate's line.
        """
        body = "- [Dead](gone.md) — drop · [Out](../../etc/passwd) — keep\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1
        assert "not provably dead" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before


class TestStrictGrammarRule:
    """Reading a line is not the same as being able to delete it (ADR-0020 §1).

    The parse must account for the *whole* line before a splice may take it:
    a single-line ``-``/``*`` bullet of links plus inert prose. What the widened
    parser newly reads right (a prose prefix, a balanced paren) is now fixable;
    what it reads with a doubt (unresolved syntax, a destination we won't guess
    at) never is, and a line that outgrows itself never is.
    """

    def _patch_loader(self, monkeypatch, config):
        import memtomem.cli.memory_doctor_cmd as mod

        monkeypatch.setattr(mod, "_load_config_read_only", lambda: config)

    def test_prose_prefixed_dead_line_is_removed(self, tmp_path, monkeypatch):
        """The old grammar couldn't see this pointer; the parse accounts for it now.

        ``- NS: [Dead](gone.md)`` is a bullet whose links are all dead and whose
        prefix is inert prose — §1's test passes, so the frozen legacy-shape
        refusal is gone with the amendment.
        """
        body = "- NS: [Dead](gone.md) — drop\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == ""

    def test_unresolvable_target_line_is_not_even_a_candidate(self, tmp_path, monkeypatch):
        """A destination we won't resolve is never deleted on the guess — nor reported here.

        ``live.md?view=1``'s literal text names no file, so a literal lookup
        calls it missing_target — but the query may belong to the filename, and
        that guess is the one --fix must not make with a delete. Tier 1 reports
        it as ambiguous_index_line; Tier 2 has no business with the line at all,
        so it is not a candidate and not a *skip* either (a skip would claim
        --fix found a dead pointer here, which it did not).
        """
        body = "- [Live](live.md?view=1) — query\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        (mem_dir / "live.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply", "--json"])

        assert json.loads(result.output)["status"] == "clean"
        assert result.exit_code == 0
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_unreadable_sibling_of_a_dead_link_skips_the_line(self, tmp_path, monkeypatch):
        """The doubt is per line once a real dead pointer puts the line in scope.

        Unlike the case above there *is* a candidate here, so the line is --fix
        business — and the splice would take a pointer whose destination nobody
        resolved. Skipped and reported.
        """
        body = "- [Dead](gone.md) — drop · [Q](live.md?view=1) — query\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        (mem_dir / "live.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1
        assert "will not resolve on a guess" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_unresolved_syntax_beside_a_dead_link_skips_the_line(self, tmp_path, monkeypatch):
        """An unclosed link is a pointer the grammar could not read — the line stays."""
        body = "- [Dead](gone.md) — drop and [B](b.md\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1
        assert "resolved to no link" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_contested_wikilink_label_line_survives_apply(self, tmp_path, monkeypatch):
        """#1774's promised harm, pinned through the real write path.

        A genuine wikilink and an escaped same-name pointer share the line with
        a dead sibling; the old inline-wide confirmation demoted the live
        ``live.md`` pointer, read the line as all-dead, and ``--apply`` deleted
        it whole. The census contests the pair instead, so the line — live
        pointer and all — must survive byte-for-byte, in the dry run and under
        ``--apply``, with the skip reported for a human.
        """
        body = (
            "- [Ok](exists.md) — keep\n"
            + r"- [[memo]](note) and [\[memo\]](live.md) and [Dead](gone.md)"
            + "\n"
        )
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        (mem_dir / "live.md").write_text("x", encoding="utf-8")  # provably live
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        dry = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])
        assert "will not resolve on a guess" in dry.output
        assert "Would remove" not in dry.output  # no advertised removal either
        assert (mem_dir / "MEMORY.md").read_bytes() == before

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])
        assert result.exit_code == 1
        assert "will not resolve on a guess" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_escaped_target_line_survives_because_it_is_read_right(self, tmp_path, monkeypatch):
        """The escape case, end to end: read correctly, so never a candidate.

        Markdown resolves ``notes_\\(v2.md`` to the live ``notes_(v2.md``. A
        literal path lookup misses it and calls it dead — which is how --apply
        came to delete a live pointer. Nothing on the line hints at the
        difference, so no amount of inspecting the *text* saves it; only reading
        the link does.
        """
        body = "- [Live](notes_\\(v2.md) — escaped paren\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        (mem_dir / "notes_(v2.md").write_text("x", encoding="utf-8")
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert "No missing_target links to remove" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_fenced_example_is_never_edited(self, tmp_path, monkeypatch):
        """End to end: --apply must not touch a code block.

        The example's target doesn't exist — that's what makes it an example —
        so a line-wise reader offers it up as a dead pointer and edits the
        fence.
        """
        body = "- [Dead](gone.md) — real\n\n```markdown\n- [Example](nowhere.md) — example\n```\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        # The real dead pointer goes; the fence survives byte-for-byte.
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == (
            "\n```markdown\n- [Example](nowhere.md) — example\n```\n"
        )
        assert "nowhere.md" not in result.output

    def test_multiline_item_is_skipped(self, tmp_path, monkeypatch):
        body = "- [Dead](gone.md) — hook\n  continues here\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1
        assert "continues past this line" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_reference_sibling_blocks_the_fix(self, tmp_path, monkeypatch):
        """End to end: the live reference link must stop the splice.

        The dead inline pointer alone would make this line fixable, and the
        live sibling's destination lives on another line — so a reader that
        can't see across lines deletes a live pointer and reports one removal.
        """
        body = "- [Dead](gone.md) and [Live][live]\n\n[live]: exists.md\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)
        before = (mem_dir / "MEMORY.md").read_bytes()

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 1
        assert "not provably dead" in result.output
        assert (mem_dir / "MEMORY.md").read_bytes() == before

    def test_quoted_link_is_not_a_sibling_so_the_line_stays_fixable(self, tmp_path, monkeypatch):
        """A code span is not an entry, so it neither blocks nor survives a fix.

        The line's only pointer is dead, so the line goes — quoted text and all.
        Treating the quote as a live sibling would have frozen --fix instead.
        """
        body = "- [Dead](gone.md) — quoting `[x](y)`\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == ""

    def test_skip_report_names_the_reason_per_line(self, tmp_path, monkeypatch):
        body = "- [Dead](gone.md) · [Live](exists.md)\n1. [Dead2](gone2.md)\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix"])

        assert "L1:" in result.output
        assert "not provably dead" in result.output
        assert "L2:" in result.output
        assert "bullet entry" in result.output

    def test_balanced_hook_parens_stay_fixable(self, tmp_path, monkeypatch):
        """Ambiguity must be narrow: ordinary parenthetical prose still fixes."""
        body = "- [Dead](gone.md) — drop (for good reasons)\n"
        config, mem_dir = _fix_env(tmp_path, monkeypatch, body=body)
        self._patch_loader(monkeypatch, config)

        result = CliRunner().invoke(cli, ["memory", "doctor", "--fix", "--apply"])

        assert result.exit_code == 0
        assert (mem_dir / "MEMORY.md").read_text(encoding="utf-8") == ""
