"""The duplicate brief: ``mem_dedup_scan`` with ``maintenance.experimental_duplicate_brief`` on.

Three layers, each with its own class group:

* the pure pieces (``differing_fields``, the renderer) on hand-built members;
* the tool with a stubbed scanner, for the switch and for what it renders from;
* the tool on a real store, where something changes between the scan and the
  evidence read.

With the switch off the tool is the one ``test_dedup.py`` covers, so the tests
here pin only that the switch is what separates the two.
"""

from __future__ import annotations

import inspect
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from helpers import StubCtx
from memtomem import config as _cfg
from memtomem.config import MUTABLE_FIELDS, Mem2MemConfig, load_config_overrides
from memtomem.models import ORIGIN_CONSOLIDATION_POLICY, Chunk, ChunkMetadata, ChunkTopology
from memtomem.search.dedup import (
    DIFFERING_FIELDS,
    DedupCandidate,
    DedupCoverage,
    DedupScanner,
    differing_fields,
)
from memtomem.server.context import AppContext
from memtomem.server.tools import dedup_decay
from memtomem.server.tools.dedup_decay import (
    _brief_member,
    _brief_validity,
    _duplicate_brief,
    _quoted,
    mem_dedup_scan,
)
from memtomem.storage.source_holds import REDACTION_BLOCKED_HOLD

_FOOTER = "Scanned all 2 scannable chunks; near-duplicate search probed 2."
_COVERAGE = DedupCoverage(pool=2, eligible=2, near_search_enabled=True, probed=2, without_vector=0)
_OMITTED = (
    "omitted: a member is not stored now, its source is unavailable, or the bodies of an "
    "exact pair do not match."
)
_NO_DIFFERENCE = (
    "  differs: none (compared: heading, namespace, scope, project_root, tags, validity)"
)
_T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def _unix(year: int, month: int, day: int) -> int:
    return int(datetime(year, month, day, tzinfo=timezone.utc).timestamp())


def _member(content: str = "same body", *, source: str = "/notes/a.md", **meta) -> Chunk:
    return Chunk(
        content=content,
        metadata=ChunkMetadata(source_file=Path(source), **meta),
        created_at=_T0,
    )


def _at(chunk: Chunk) -> str:
    """``"<source path>":<start line>`` as a member's head line spells it."""
    return f"{_quoted(str(chunk.metadata.source_file))}:{chunk.metadata.start_line}"


def _topology(**moved) -> ChunkTopology:
    alone = {
        "only_in_source": False,
        "at_source_start": False,
        "span_shared": False,
        "consolidated_into": 0,
        "consolidates": 0,
        "other_relations": 0,
        "links_out": (),
        "links_in": (),
    }
    return ChunkTopology(**{**alone, **moved})


def _lines(chunk: Chunk, topology: ChunkTopology) -> dict[str, str]:
    """The three evidence lines of one member, keyed by their label."""
    rendered = _brief_member("A", chunk, topology).splitlines()[1:]
    return dict(line.strip().split(": ", 1) for line in rendered)


# --------------------------------------------------------------------------- #
# differing_fields
# --------------------------------------------------------------------------- #


class TestDifferingFields:
    _BASE = ChunkMetadata(
        source_file=Path("/notes/a.md"),
        heading_hierarchy=("Guide",),
        namespace="default",
        scope="user",
        project_root=None,
        tags=("a", "b"),
        valid_from_unix=10,
        valid_to_unix=20,
    )

    @pytest.mark.parametrize(
        ("changed", "name"),
        [
            ({"heading_hierarchy": ("Guide", "Setup")}, "heading"),
            ({"namespace": "work"}, "namespace"),
            ({"scope": "project_local"}, "scope"),
            ({"project_root": Path("/abs/project")}, "project_root"),
            ({"tags": ("a",)}, "tags"),
            ({"valid_from_unix": 11}, "validity"),
            ({"valid_to_unix": None}, "validity"),
        ],
    )
    def test_one_changed_field_is_the_one_named(self, changed, name):
        assert differing_fields(self._BASE, replace(self._BASE, **changed)) == (name,)

    def test_every_reported_name_has_a_case_above(self):
        named = {
            case[1] for case in self.test_one_changed_field_is_the_one_named.pytestmark[0].args[1]
        }
        assert named == set(DIFFERING_FIELDS)

    def test_the_same_record_differs_in_nothing(self):
        assert differing_fields(self._BASE, replace(self._BASE)) == ()

    def test_tag_order_is_not_a_difference(self):
        assert differing_fields(self._BASE, replace(self._BASE, tags=("b", "a"))) == ()

    def test_where_the_chunk_sits_is_not_a_record_difference(self):
        elsewhere = replace(self._BASE, source_file=Path("/notes/b.md"), start_line=40, end_line=44)
        assert differing_fields(self._BASE, elsewhere) == ()

    def test_names_come_in_the_declared_order(self):
        other = replace(self._BASE, tags=(), heading_hierarchy=(), namespace="work")
        assert differing_fields(self._BASE, other) == ("heading", "namespace", "tags")


# --------------------------------------------------------------------------- #
# DedupScanner.member_evidence
# --------------------------------------------------------------------------- #


class TestMemberEvidence:
    @pytest.mark.asyncio
    async def test_all_pairs_are_answered_by_one_read_of_the_distinct_ids(self):
        keep, dup_one, dup_two, other = (_member(f"body {i}") for i in range(4))
        storage = MagicMock()
        storage.get_chunks_with_topology = AsyncMock(return_value={keep.id: "sentinel"})
        pairs = [
            DedupCandidate(chunk_a=keep, chunk_b=dup_one, score=1.0, exact=True),
            DedupCandidate(chunk_a=keep, chunk_b=dup_two, score=1.0, exact=True),
            DedupCandidate(chunk_a=dup_two, chunk_b=other, score=0.95, exact=False),
        ]

        found = await DedupScanner(storage).member_evidence(pairs)

        assert found == {keep.id: "sentinel"}
        storage.get_chunks_with_topology.assert_awaited_once_with(
            [keep.id, dup_one.id, dup_two.id, other.id]
        )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestMemberLines:
    def test_the_head_line_names_the_member_where_it_is_stored(self):
        chunk = _member("first line\nsecond line", start_line=12)

        head = _brief_member("B", chunk, _topology()).splitlines()[0]

        assert _at(chunk).endswith(":12")
        assert head == f'  B ({chunk.id}): {_at(chunk)} — "first line second line"'

    def test_a_bare_record(self):
        assert _lines(_member(), _topology()) == {
            "record": 'heading=(none) | namespace="default" | scope="user" | tags=(none) | '
            "valid=always",
            "source": "writable (stored flag)",
            "links": "none",
        }

    def test_a_full_record(self):
        root = Path("/abs/project")
        chunk = _member(
            heading_hierarchy=("Guide", "Setup"),
            namespace="work",
            scope="project_shared",
            project_root=root,
            tags=("a", "b"),
        )

        assert _lines(chunk, _topology())["record"] == (
            f'heading="Guide" > "Setup" | namespace="work" | '
            f'scope="project_shared" ({_quoted(str(root))}) | tags="a", "b" | valid=always'
        )

    @pytest.mark.parametrize(
        ("window", "text"),
        [
            ((None, None), "always"),
            ((_unix(2026, 3, 1), None), "from 2026-03-01T00:00:00Z"),
            ((None, _unix(2027, 1, 1) - 1), "until 2026-12-31T23:59:59Z"),
            (
                (_unix(2026, 3, 1), _unix(2027, 1, 1)),
                "from 2026-03-01T00:00:00Z until 2027-01-01T00:00:00Z",
            ),
        ],
    )
    def test_each_bound_of_the_validity_window_is_named_on_its_own(self, window, text):
        chunk = _member(valid_from_unix=window[0], valid_to_unix=window[1])

        assert _lines(chunk, _topology())["record"].endswith(f"| valid={text}")

    @pytest.mark.parametrize(
        ("meta", "moved", "text"),
        [
            ({"source_read_only": True}, {}, "read-only (stored flag)"),
            ({"redaction_count": 2}, {}, "writable (stored flag) | masked projection"),
            (
                {},
                {"at_source_start": None, "span_shared": None},
                "writable (stored flag) | line span not recorded",
            ),
            (
                {},
                {"at_source_start": True},
                "writable (stored flag) | no sibling starts on an earlier line",
            ),
            (
                {"source_read_only": True},
                {"span_shared": True},
                "read-only (stored flag) | line span overlaps a sibling chunk",
            ),
            ({}, {"only_in_source": True}, "writable (stored flag) | only chunk of its source"),
            (
                {"source_read_only": True, "redaction_count": 1},
                {"at_source_start": True, "span_shared": True, "only_in_source": True},
                "read-only (stored flag) | masked projection | "
                "no sibling starts on an earlier line | line span overlaps a sibling chunk | "
                "only chunk of its source",
            ),
        ],
    )
    def test_the_source_line(self, meta, moved, text):
        assert _lines(_member(**meta), _topology(**moved))["source"] == text

    @pytest.mark.parametrize(
        ("meta", "moved", "text"),
        [
            ({"origin": ORIGIN_CONSOLIDATION_POLICY}, {}, "policy summary"),
            ({}, {"consolidated_into": 1}, "covered by 1 summary"),
            ({}, {"consolidated_into": 2}, "covered by 2 summaries"),
            ({}, {"consolidates": 1}, "summary of 1 chunk"),
            ({}, {"consolidates": 3}, "summary of 3 chunks"),
            ({}, {"other_relations": 1}, "1 other relation"),
            ({}, {"other_relations": 2}, "2 other relations"),
            (
                {},
                {"links_out": (("shared", 1), ("summarizes", 3))},
                'link source: "shared" x1, "summarizes" x3',
            ),
            (
                {},
                {"links_in": ("shared", "summarizes")},
                'link target: "shared", "summarizes"',
            ),
            (
                {"origin": ORIGIN_CONSOLIDATION_POLICY},
                {
                    "consolidated_into": 1,
                    "consolidates": 4,
                    "other_relations": 2,
                    "links_out": (("summarizes", 2),),
                    "links_in": ("shared",),
                },
                "policy summary | covered by 1 summary | summary of 4 chunks | "
                '2 other relations | link source: "summarizes" x2 | link target: "shared"',
            ),
        ],
    )
    def test_the_links_line(self, meta, moved, text):
        assert _lines(_member(**meta), _topology(**moved))["links"] == text


class TestQuoting:
    """A stored value is shown inside quotes it cannot close and on a line it cannot end."""

    @pytest.mark.parametrize(
        ("stored", "shown"),
        [
            ("plain", '"plain"'),
            ("", '""'),
            ("한글 tag", '"한글 tag"'),
            ('say "hi"', '"say \\"hi\\""'),
            ("back\\slash", '"back\\\\slash"'),
            # Unescaped, a final backslash would escape the closing quote.
            ("ends with \\", '"ends with \\\\"'),
            ("a\nb", '"a\\nb"'),
            ("a\r\nb", '"a\\r\\nb"'),
            ("a\tb", '"a\\tb"'),
            ("a" + chr(0x85) + "b", '"a\\x85b"'),
            ("a" + chr(0x2028) + "b", '"a\\u2028b"'),
            ("a" + chr(0x1B) + "[31mb", '"a\\x1b[31mb"'),
        ],
    )
    def test_a_value_is_quoted_on_one_line(self, stored, shown):
        assert _quoted(stored) == shown
        assert len(_quoted(stored).splitlines()) == 1

    def test_a_tag_cannot_add_an_evidence_line(self):
        forged = "x\n     source: writable (stored flag) | only chunk of its source"
        chunk = _member(tags=(forged,), source_read_only=True)

        rendered = _brief_member("A", chunk, _topology())

        assert len(rendered.splitlines()) == 4
        assert _lines(chunk, _topology())["source"] == "read-only (stored flag)"
        assert '| tags="x\\n     source: writable (stored flag) | only chunk of its source" |' in (
            rendered
        )

    def test_a_heading_cannot_pass_for_the_next_field(self):
        chunk = _member(heading_hierarchy=('Notes | namespace="work" | scope="user"',))

        record = _lines(chunk, _topology())["record"]

        # The real namespace field follows the closing quote of the heading;
        # the forged one sits inside it with its own quotes escaped.
        assert record.startswith(
            'heading="Notes | namespace=\\"work\\" | scope=\\"user\\"" | namespace="default" | '
        )

    def test_a_path_and_a_body_cannot_break_the_head_line(self):
        chunk = _member(
            'body" — forged' + chr(0x2028) + "  B (id): /other.md:1",
            source="/notes/a" + chr(10) + "  B (x).md",
            start_line=4,
        )

        rendered = _brief_member("A", chunk, _topology()).splitlines()

        assert len(rendered) == 4
        assert rendered[0] == (
            f"  A ({chunk.id}): {_quoted(str(chunk.metadata.source_file))}:4 — "
            '"body\\" — forged\\u2028  B (id): /other.md:1"'
        )
        assert "\\n" in _quoted(str(chunk.metadata.source_file))

    def test_a_namespace_scope_and_project_root_are_quoted_too(self):
        root = Path("/abs/pro ject")
        chunk = _member(namespace="a | b", scope="user\nx", project_root=root)

        assert _lines(chunk, _topology())["record"] == (
            f'heading=(none) | namespace="a | b" | scope="user\\nx" ({_quoted(str(root))}) | '
            "tags=(none) | valid=always"
        )

    def test_a_tag_named_like_the_empty_marker_stays_distinct(self):
        assert "tags=(none)" in _lines(_member(), _topology())["record"]
        assert 'tags="(none)"' in _lines(_member(tags=("(none)",)), _topology())["record"]


class TestValidityBounds:
    def test_bounds_inside_one_day_read_differently(self):
        assert _brief_validity(10, 20) == "from 1970-01-01T00:00:10Z until 1970-01-01T00:00:20Z"

    def test_a_bound_before_the_epoch(self):
        assert _brief_validity(-5, None) == "from 1969-12-31T23:59:55Z"

    @pytest.mark.parametrize("bound", [2**63 - 1, -(2**63), 253402300800])
    def test_a_bound_no_calendar_reaches_is_shown_as_stored(self, bound):
        assert _brief_validity(bound, None) == f"from unix {bound}"
        assert _brief_validity(None, bound) == f"until unix {bound}"

    def test_the_last_second_a_calendar_reaches(self):
        assert _brief_validity(None, 253402300799) == "until 9999-12-31T23:59:59Z"

    def test_a_member_with_such_a_bound_does_not_fail_the_brief(self):
        a = _member(valid_to_unix=2**63 - 1)
        b = _member(source="/notes/b.md")
        pair = DedupCandidate(chunk_a=a, chunk_b=b, score=1.0, exact=True)

        out = _duplicate_brief(
            [pair], {a.id: (a, _topology()), b.id: (b, _topology())}, 0.92, _FOOTER
        )

        assert f"valid=until unix {2**63 - 1}" in out
        assert "  differs: validity" in out


class TestBriefText:
    def _two_pairs(self):
        root = Path("/abs/project")
        a = _member(heading_hierarchy=("Guide", "Setup"), tags=("a", "b"), start_line=12)
        b = _member(
            source="/notes/b.md",
            heading_hierarchy=("Notes",),
            namespace="work",
            scope="project_shared",
            project_root=root,
            valid_to_unix=_unix(2027, 1, 1),
            source_read_only=True,
            start_line=40,
        )
        c = _member("near body one", source="/notes/c.md", start_line=3)
        d = _member("near body two", source="/notes/d.md", start_line=7)
        pairs = [
            DedupCandidate(chunk_a=a, chunk_b=b, score=1.0, exact=True),
            DedupCandidate(chunk_a=c, chunk_b=d, score=0.93125, exact=False),
        ]
        evidence = {
            a.id: (a, _topology(at_source_start=True, only_in_source=True)),
            b.id: (
                b,
                _topology(
                    span_shared=True,
                    consolidated_into=1,
                    other_relations=2,
                    links_out=(("shared", 1), ("summarizes", 3)),
                    links_in=("shared",),
                ),
            ),
            c.id: (c, _topology()),
            d.id: (d, _topology()),
        }
        return (a, b, c, d), pairs, evidence, root

    def test_the_whole_brief_for_an_exact_pair_and_a_near_pair(self):
        (a, b, c, d), pairs, evidence, root = self._two_pairs()

        out = _duplicate_brief(pairs, evidence, 0.92, _FOOTER)

        bare = 'heading=(none) | namespace="default" | scope="user" | tags=(none) | valid=always'
        assert out == "\n".join(
            [
                "Duplicate candidates: 2 pairs (threshold=0.92):",
                "",
                "[1] exact duplicate",
                f'  A ({a.id}): {_at(a)} — "same body"',
                '     record: heading="Guide" > "Setup" | namespace="default" | scope="user" | '
                'tags="a", "b" | valid=always',
                "     source: writable (stored flag) | no sibling starts on an earlier line | "
                "only chunk of its source",
                "     links: none",
                f'  B ({b.id}): {_at(b)} — "same body"',
                '     record: heading="Notes" | namespace="work" | '
                f'scope="project_shared" ({_quoted(str(root))}) | '
                "tags=(none) | valid=until 2027-01-01T00:00:00Z",
                "     source: read-only (stored flag) | line span overlaps a sibling chunk",
                "     links: covered by 1 summary | 2 other relations | "
                'link source: "shared" x1, "summarizes" x3 | link target: "shared"',
                "  differs: heading, namespace, scope, project_root, tags, validity",
                "[2] score=0.9313",
                f'  A ({c.id}): {_at(c)} — "near body one"',
                f"     record: {bare}",
                "     source: writable (stored flag)",
                "     links: none",
                f'  B ({d.id}): {_at(d)} — "near body two"',
                f"     record: {bare}",
                "     source: writable (stored flag)",
                "     links: none",
                _NO_DIFFERENCE,
                _FOOTER,
            ]
        )

    def test_a_pair_with_an_absent_member_is_left_out_and_counted(self):
        (a, b, c, d), pairs, evidence, _ = self._two_pairs()
        del evidence[d.id]

        out = _duplicate_brief(pairs, evidence, 0.92, _FOOTER).splitlines()

        assert out[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert out[-2:] == [f"1 pair {_OMITTED}", _FOOTER]
        assert not any(str(c.id) in line or "near body" in line for line in out)
        assert sum(line.startswith("[") for line in out) == 1

    @pytest.mark.parametrize("absent", ["a", "b"])
    def test_either_member_being_absent_leaves_the_pair_out(self, absent):
        (a, b, _, _), pairs, evidence, _ = self._two_pairs()
        del evidence[{"a": a, "b": b}[absent].id]

        out = _duplicate_brief(pairs, evidence, 0.92, _FOOTER)

        assert "exact duplicate" not in out
        assert f"1 pair {_OMITTED}" in out

    def test_an_exact_pair_whose_members_no_longer_hash_the_same_is_left_out(self):
        (a, b, _, _), pairs, evidence, _ = self._two_pairs()
        rewritten = replace(b, content="rewritten since the scan", content_hash="other")
        evidence[b.id] = (rewritten, evidence[b.id][1])

        out = _duplicate_brief(pairs, evidence, 0.92, _FOOTER)

        assert "exact duplicate" not in out and "rewritten since the scan" not in out
        assert f"1 pair {_OMITTED}" in out

    def test_a_near_pair_does_not_need_equal_hashes(self):
        (_, _, c, d), pairs, evidence, _ = self._two_pairs()
        assert c.content_hash != d.content_hash

        assert "[2] score=0.9313" in _duplicate_brief(pairs, evidence, 0.92, _FOOTER)

    def test_no_pair_left_reads_as_an_empty_scan_that_says_why(self):
        _, pairs, _, _ = self._two_pairs()

        assert _duplicate_brief(pairs, {}, 0.92, _FOOTER) == "\n".join(
            ["No duplicate chunks found (threshold=0.92).", f"2 pairs {_OMITTED}", _FOOTER]
        )


# --------------------------------------------------------------------------- #
# The tool, scanner stubbed
# --------------------------------------------------------------------------- #


def _stub_app(monkeypatch, candidates, *, brief, evidence=None):
    app = MagicMock()
    app.config.maintenance.experimental_duplicate_brief = brief
    app.dedup_scanner.scan_with_coverage = AsyncMock(return_value=(candidates, _COVERAGE))
    app.dedup_scanner.member_evidence = AsyncMock(return_value=evidence or {})

    async def _fake_app(_ctx):
        return app

    monkeypatch.setattr(dedup_decay, "_get_app_initialized", _fake_app)
    return app


class TestTheSwitch:
    @staticmethod
    def _pair():
        scanned_a = _member(start_line=1, heading_hierarchy=("As scanned",))
        scanned_b = _member(source="/notes/b.md", start_line=2)
        return DedupCandidate(chunk_a=scanned_a, chunk_b=scanned_b, score=1.0, exact=True)

    @pytest.mark.asyncio
    async def test_off_the_output_is_the_plain_scan_and_no_evidence_is_read(self, monkeypatch):
        pair = self._pair()
        app = _stub_app(monkeypatch, [pair], brief=False)

        out = await mem_dedup_scan(ctx=None)

        a, b = pair.chunk_a, pair.chunk_b
        assert out == (
            "Duplicate candidates: 1 pairs (threshold=0.92):\n\n"
            "[1] exact duplicate\n"
            f'  A ({a.id}): {a.metadata.source_file}:1 — "same body"\n'
            f'  B ({b.id}): {b.metadata.source_file}:2 — "same body"\n'
            f"{_FOOTER}"
        )
        app.dedup_scanner.member_evidence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_on_the_members_are_rendered_from_the_evidence_read(self, monkeypatch):
        pair = self._pair()
        # Same ids, as a re-index leaves them; another heading and line.
        now_a = replace(
            pair.chunk_a,
            metadata=replace(
                pair.chunk_a.metadata, heading_hierarchy=("As stored now",), start_line=31
            ),
        )
        evidence = {
            now_a.id: (now_a, _topology()),
            pair.chunk_b.id: (pair.chunk_b, _topology()),
        }
        app = _stub_app(monkeypatch, [pair], brief=True, evidence=evidence)

        out = await mem_dedup_scan(ctx=None)

        app.dedup_scanner.member_evidence.assert_awaited_once_with([pair])
        assert now_a.metadata.start_line == 31
        assert f"{_at(now_a)} — " in out
        assert 'heading="As stored now"' in out
        assert "As scanned" not in out and f"{_at(pair.chunk_a)} — " not in out
        assert out.splitlines()[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert out.splitlines()[-1] == _FOOTER

    @pytest.mark.asyncio
    @pytest.mark.parametrize("brief", [False, True])
    async def test_an_empty_scan_reads_the_same_either_way(self, monkeypatch, brief):
        app = _stub_app(monkeypatch, [], brief=brief)

        out = await mem_dedup_scan(ctx=None)

        assert out == f"No duplicate chunks found (threshold=0.92).\n{_FOOTER}"
        app.dedup_scanner.member_evidence.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_the_next_offset_counts_the_pairs_the_scan_selected(self, monkeypatch):
        # The evidence read drops the second pair; the page still held two of
        # the scan's pairs, so the next one starts after both, not after the
        # one that was rendered.
        kept, dropped = self._pair(), self._pair()
        evidence = {
            kept.chunk_a.id: (kept.chunk_a, _topology()),
            kept.chunk_b.id: (kept.chunk_b, _topology()),
        }
        app = _stub_app(monkeypatch, [kept, dropped], brief=True, evidence=evidence)
        app.dedup_scanner.scan_with_coverage.return_value = (
            [kept, dropped],
            replace(_COVERAGE, pairs_found=501),
        )

        out = await mem_dedup_scan(limit=2, offset=10, ctx=None)

        assert out.splitlines()[-2:] == [
            f"1 pair {_OMITTED}",
            "Scanned all 2 scannable chunks; near-duplicate search probed 2; offset=10 kept "
            "pairs 11 to 12 of 501: to see the next ones, scan again from the same position "
            "with offset=12.",
        ]


class TestTheToolSurfaceDidNotMove:
    """The brief is behind a setting, not a parameter: it adds nothing a client is told."""

    def test_the_action_s_parameters_and_help(self):
        from memtomem.server import mcp  # noqa: F401  (registers the tools)
        from memtomem.server.tool_registry import ACTIONS

        info = ACTIONS["dedup_scan"]

        assert info.category == "maintenance"
        assert info.description == "Scan for duplicate chunk candidates (dry-run, no mutations)."
        assert info.params == {
            "threshold": "float = 0.92",
            "limit": "int = 50",
            "max_scan": "int = 500",
            "after": "str | None = None",
            "offset": "int = 0",
        }
        assert info.param_docs == {
            "threshold": "Dense score 1/(1+L2) threshold (0-1, default 0.92)",
            "limit": "Maximum number of candidate pairs to return",
            "max_scan": "Maximum chunks to scan, taken in chunk-id order",
            "after": "Chunk id to scan after; the last scan's result names the next one",
            "offset": _OFFSET_DOC,
        }

    def test_the_function_s_signature_and_docstring(self):
        assert str(inspect.signature(mem_dedup_scan)) == (
            "(threshold: 'float' = 0.92, limit: 'int' = 50, max_scan: 'int' = 500, "
            "after: 'str | None' = None, offset: 'int' = 0, ctx: 'CtxType' = None) -> 'str'"
        )
        assert inspect.getdoc(mem_dedup_scan) == (
            "Scan for duplicate chunk candidates (dry-run, no mutations).\n\n"
            "Args:\n"
            "    threshold: Dense score 1/(1+L2) threshold (0-1, default 0.92)\n"
            "    limit: Maximum number of candidate pairs to return\n"
            "    max_scan: Maximum chunks to scan, taken in chunk-id order\n"
            "    after: Chunk id to scan after; the last scan's result names the next one\n"
            "    offset: Pairs of the scanned pool to skip; a result names the next offset when it\n"
            "        offers one. Pages line up only while every scan gives the same list"
        )


_OFFSET_DOC = (
    "Pairs of the scanned pool to skip; a result names the next offset when it offers one. "
    "Pages line up only while every scan gives the same list"
)


# --------------------------------------------------------------------------- #
# The setting
# --------------------------------------------------------------------------- #


class TestTheSetting:
    @pytest.fixture
    def override_path(self, tmp_path, monkeypatch):
        from helpers import isolate_memtomem_env

        isolate_memtomem_env(monkeypatch)
        path = tmp_path / "config.json"
        monkeypatch.setattr(_cfg, "_override_path", lambda: path)
        monkeypatch.setattr(_cfg, "_config_d_path", lambda: tmp_path / "config.d")
        monkeypatch.setattr(_cfg, "_canonical_provider_dirs", lambda: [])
        return path

    def test_it_is_off_unless_set(self, override_path):
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.maintenance.experimental_duplicate_brief is False

    def test_the_environment_variable_turns_it_on(self, override_path, monkeypatch):
        monkeypatch.setenv("MEMTOMEM_MAINTENANCE__EXPERIMENTAL_DUPLICATE_BRIEF", "true")

        assert Mem2MemConfig().maintenance.experimental_duplicate_brief is True

    def test_config_json_turns_it_on(self, override_path):
        override_path.write_text(
            json.dumps({"maintenance": {"experimental_duplicate_brief": True}}), "utf-8"
        )
        cfg = Mem2MemConfig()
        load_config_overrides(cfg)

        assert cfg.maintenance.experimental_duplicate_brief is True

    def test_it_is_not_a_field_the_runtime_setters_accept(self):
        assert "maintenance" not in MUTABLE_FIELDS


# --------------------------------------------------------------------------- #
# The tool on a real store, something changing between the two reads
# --------------------------------------------------------------------------- #

_BODY = "# Topic\n\nduplicateword shared body.\n"


async def _index(comp, path: Path, body: str) -> Chunk:
    path.write_text(body, encoding="utf-8")
    assert not (await comp.index_engine.index_file(path)).errors
    rows = await comp.storage.list_chunks_by_source(path.resolve(), limit=None)
    assert len(rows) == 1, rows
    return rows[0]


def _make_oldest(comp, chunk: Chunk) -> None:
    """Pin which member the scan keeps as ``A``: the oldest of an exact group."""
    db = comp.storage._get_db()
    db.execute("UPDATE chunks SET created_at='2020-01-01T00:00:00' WHERE id=?", (str(chunk.id),))
    db.commit()


def _between_the_reads(monkeypatch, app, change) -> list:
    """Run ``change`` after the scan has returned and before the evidence read."""
    real = app.dedup_scanner.member_evidence
    seen: list = []

    async def changed_then_read(candidates):
        seen.extend(candidates)
        await change()
        return await real(candidates)

    monkeypatch.setattr(app.dedup_scanner, "member_evidence", changed_then_read)
    return seen


class TestAgainstARealStore:
    pytestmark = pytest.mark.asyncio

    async def _three_copies(self, comp, mem_dir):
        keeper = await _index(comp, mem_dir / "keeper.md", _BODY)
        second = await _index(comp, mem_dir / "second.md", _BODY)
        third = await _index(comp, mem_dir / "third.md", _BODY)
        _make_oldest(comp, keeper)
        comp.config.maintenance.experimental_duplicate_brief = True
        return keeper, second, third

    async def test_an_unchanged_store_lists_every_pair_with_stored_evidence(
        self, bm25_only_components
    ):
        comp, mem_dir = bm25_only_components
        keeper, second, third = await self._three_copies(comp, mem_dir)

        out = await mem_dedup_scan(ctx=StubCtx(AppContext.from_components(comp)))

        assert out.splitlines()[0] == "Duplicate candidates: 2 pairs (threshold=0.92):"
        assert "omitted" not in out
        assert out.count(f"  A ({keeper.id}): {_at(keeper)} — ") == 2
        assert out.count('record: heading="# Topic" | namespace="default" | scope="user"') == 4
        assert (
            out.count(
                "source: writable (stored flag) | no sibling starts on an earlier line | "
                "only chunk of its source"
            )
            == 4
        )
        assert out.count(_NO_DIFFERENCE) == 2

    async def test_the_switch_off_prints_no_evidence_on_the_same_store(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        await self._three_copies(comp, mem_dir)
        comp.config.maintenance.experimental_duplicate_brief = False

        out = await mem_dedup_scan(ctx=StubCtx(AppContext.from_components(comp)))

        assert out.splitlines()[0] == "Duplicate candidates: 2 pairs (threshold=0.92):"
        assert "record:" not in out and "differs:" not in out

    @pytest.mark.parametrize("kind", ["scan_missing", REDACTION_BLOCKED_HOLD, "pending"])
    async def test_a_member_whose_source_is_held_after_the_scan_is_left_out(
        self, bm25_only_components, monkeypatch, kind
    ):
        comp, mem_dir = bm25_only_components
        keeper, second, third = await self._three_copies(comp, mem_dir)
        app = AppContext.from_components(comp)
        held = mem_dir / "second.md"

        async def hold() -> None:
            if kind == "pending":
                assert comp.storage.queue_source_check_sync(held)
            else:
                assert await comp.storage.hold_source(held, kind)

        scanned = _between_the_reads(monkeypatch, app, hold)

        out = await mem_dedup_scan(ctx=StubCtx(app))

        # The scan had both pairs; only the read after the hold can drop one.
        assert {pair.chunk_b.id for pair in scanned} == {second.id, third.id}
        assert out.splitlines()[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert out.splitlines()[-2] == f"1 pair {_OMITTED}"
        # The path as the brief spells it: quoted, a Windows path has its
        # backslashes doubled, and the raw spelling would be absent either way.
        assert str(second.id) not in out
        assert _quoted(str(second.metadata.source_file)) not in out
        assert _quoted(str(third.metadata.source_file)) in out
        assert str(third.id) in out

    async def test_a_re_index_that_keeps_the_id_is_rendered_as_stored_now(
        self, bm25_only_components, monkeypatch
    ):
        comp, mem_dir = bm25_only_components
        keeper, second, third = await self._three_copies(comp, mem_dir)
        app = AppContext.from_components(comp)
        path = mem_dir / "second.md"

        async def rename_and_shift() -> None:
            path.write_text("\n\n" + _BODY.replace("# Topic", "# Renamed"), encoding="utf-8")
            assert not (await comp.index_engine.index_file(path)).errors

        _between_the_reads(monkeypatch, app, rename_and_shift)

        out = await mem_dedup_scan(ctx=StubCtx(app))

        after = await comp.storage.list_chunks_by_source(path.resolve(), limit=None)
        # The id survived, so the pair is still listed: this cannot pass by the
        # pair having been dropped.
        assert [c.id for c in after] == [second.id]
        assert (after[0].metadata.heading_hierarchy, after[0].metadata.start_line) == (
            ("# Renamed",),
            3,
        )
        assert "omitted" not in out
        assert f"  B ({second.id}): {_at(after[0])} — " in out
        assert out.count('record: heading="# Renamed" | ') == 1
        assert f"{_at(second)} — " not in out
        assert out.count("  differs: heading") == 1

    async def test_an_exact_pair_rewritten_under_its_id_is_left_out(
        self, bm25_only_components, monkeypatch
    ):
        comp, mem_dir = bm25_only_components
        keeper, second, third = await self._three_copies(comp, mem_dir)
        app = AppContext.from_components(comp)

        async def rewrite() -> None:
            db = comp.storage._get_db()
            db.execute(
                "UPDATE chunks SET content='rewrittenword body.', content_hash=? WHERE id=?",
                (str(uuid4()), str(second.id)),
            )
            db.commit()

        _between_the_reads(monkeypatch, app, rewrite)

        out = await mem_dedup_scan(ctx=StubCtx(app))

        assert out.splitlines()[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert out.splitlines()[-2] == f"1 pair {_OMITTED}"
        assert "rewrittenword" not in out and str(second.id) not in out

    async def test_a_cut_scan_that_also_loses_a_pair_states_each_count(
        self, bm25_only_components, monkeypatch
    ):
        # Two counts that are not each other's: ``limit`` keeps 1 of the scan's
        # 2 pairs, and the brief then leaves that one out. The coverage line
        # speaks of the first, the omission line of the second.
        comp, mem_dir = bm25_only_components
        await self._three_copies(comp, mem_dir)
        app = AppContext.from_components(comp)
        scanned: list = []

        async def hold_the_returned_duplicate() -> None:
            assert await comp.storage.hold_source(
                scanned[0].chunk_b.metadata.source_file, "scan_missing"
            )

        scanned = _between_the_reads(monkeypatch, app, hold_the_returned_duplicate)

        out = await mem_dedup_scan(limit=1, ctx=StubCtx(app))

        assert len(scanned) == 1
        assert out.splitlines() == [
            "No duplicate chunks found (threshold=0.92).",
            f"1 pair {_OMITTED}",
            # This store keeps no vectors, so the exact phase found both pairs.
            "Scanned all 3 scannable chunks; near-duplicate search probed 0, 3 had no usable "
            "stored vector and were not probed; limit=1 kept 1 of 2 pairs: to see the rest, "
            "scan again from the same position with a higher limit.",
        ]


class TestAWalkOnARealStore:
    """``after`` taken from one call's last line selects the next chunks."""

    pytestmark = pytest.mark.asyncio

    async def test_each_call_names_the_cursor_of_the_next(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        stored = [
            await _index(comp, mem_dir / f"note-{i}.md", f"# Topic {i}\n\nbody number {i}.\n")
            for i in range(5)
        ]
        ids = sorted(str(chunk.id) for chunk in stored)
        ctx = StubCtx(AppContext.from_components(comp))

        def unprobed(n: int) -> str:
            # This store keeps no vectors.
            return (
                f"near-duplicate search probed 0, {n} had no usable stored vector "
                "and were not probed"
            )

        first = await mem_dedup_scan(max_scan=2, ctx=ctx)
        second = await mem_dedup_scan(max_scan=2, after=ids[1], ctx=ctx)
        # Typed in another form than it was printed in: it names the same id.
        third = await mem_dedup_scan(max_scan=2, after=f"{{{ids[3].upper()}}}", ctx=ctx)

        assert first.splitlines()[-1] == (
            "Scanned 2 of 5 scannable chunks (the first 2 in chunk-id order); "
            f'{unprobed(2)}; 3 remain: continue with after="{ids[1]}".'
        )
        assert second.splitlines()[-1] == (
            f"Scanned 2 of 5 scannable chunks (the 2 after {ids[1]} in chunk-id order); "
            f'{unprobed(2)}; 1 remains: continue with after="{ids[3]}".'
        )
        assert third.splitlines()[-1] == (
            f"Scanned 1 of 5 scannable chunks (the 1 after {ids[3]} in chunk-id order); "
            f"{unprobed(1)}; reached the end of the store."
        )

    async def test_a_pair_is_found_in_the_slice_its_members_are_in(self, bm25_only_components):
        comp, mem_dir = bm25_only_components
        others = [
            await _index(comp, mem_dir / f"note-{i}.md", f"# Topic {i}\n\nbody number {i}.\n")
            for i in range(4)
        ]
        low = min(others, key=lambda chunk: str(chunk.id))
        # Two copies whose ids sort above every other chunk's: found only by
        # the slice that starts after the lowest id, not by the first chunk.
        copies = [await _index(comp, mem_dir / f"copy-{i}.md", _BODY) for i in range(2)]
        db = comp.storage._get_db()
        for n, chunk in enumerate(copies):
            db.execute(
                "UPDATE chunks SET id=? WHERE id=?",
                (f"ffffffff-ffff-4fff-8fff-00000000000{n}", str(chunk.id)),
            )
        db.commit()
        ctx = StubCtx(AppContext.from_components(comp))

        first = await mem_dedup_scan(max_scan=1, ctx=ctx)
        rest = await mem_dedup_scan(max_scan=10, after=str(low.id), ctx=ctx)

        assert first.splitlines()[0] == "No duplicate chunks found (threshold=0.92)."
        assert f'continue with after="{low.id}"' in first.splitlines()[-1]
        assert rest.splitlines()[0] == "Duplicate candidates: 1 pairs (threshold=0.92):"
        assert rest.count("ffffffff-ffff-4fff-8fff-00000000000") == 2
        assert rest.splitlines()[-1].endswith("; reached the end of the store.")
