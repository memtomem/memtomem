"""The decision record itself: validation, rendering and tags (#3620).

``mem_do`` checks only booleans and argument binding before it calls an
action, so the record's own validation is the type check. These tests drive
``memtomem.decision_record`` directly; the action is covered in
``test_decision_record_action.py``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from memtomem.chunking.markdown import MarkdownChunker
from memtomem.decision_record import (
    DecisionRecordError,
    build_decision_record,
    parse_body,
    project_slug,
)
from memtomem.tools.memory_writer import format_entry_block

DECISION_ID = "6f0c2a4e-5b1d-4c57-9a3e-2d8f1b7c4e90"
NOW = datetime(2026, 11, 26, 2, 41, 9, tzinfo=timezone.utc)
CAP = 100_000


def _args(**overrides):
    args = {
        "decision_id": DECISION_ID,
        "project": "memtomem",
        "recorded_by": "claude-code",
        "question": "Which axis to build the first sample on",
        "options": ["patterns (Recommended)", "interests", "not yet"],
        "chosen": "not yet",
        "max_body_chars": CAP,
        "now": NOW,
    }
    args.update(overrides)
    return args


def _refused(member: str, **overrides) -> DecisionRecordError:
    with pytest.raises(DecisionRecordError) as caught:
        build_decision_record(**_args(**overrides))
    assert caught.value.member == member, str(caught.value)
    return caught.value


class TestTypes:
    @pytest.mark.parametrize(
        ("member", "overrides"),
        [
            ("options", {"options": "ab"}),
            ("options[1]", {"options": ["a", 1]}),
            ("chosen", {"chosen": 1}),
            ("refs", {"refs": {}}),
            ("refs[0]", {"refs": [None]}),
            ("question", {"question": None}),
            ("recommended", {"recommended": 1}),
            ("user_words", {"user_words": True}),
            ("agent_reason", {"agent_reason": ["because"]}),
            ("decision_id", {"decision_id": 7}),
            ("project", {"project": ["memtomem"]}),
            ("recorded_by", {"recorded_by": False}),
            ("decided_at", {"decided_at": 1764124867}),
        ],
    )
    def test_a_value_of_the_wrong_type_is_refused(self, member, overrides):
        _refused(member, **overrides)

    @pytest.mark.parametrize(
        ("member", "overrides"),
        [
            ("decision_id", {"decision_id": "\ud800"}),
            ("project", {"project": "mem\udc00"}),
            ("recorded_by", {"recorded_by": "claude\ud800"}),
            ("question", {"question": "why \ud800"}),
            ("options[0]", {"options": ["a\ud800", "b"]}),
            ("chosen", {"chosen": "\udfff"}),
            ("recommended", {"recommended": "\ud800"}),
            ("recommended_by", {"recommended_by": "\ud800"}),
            ("agent_reason", {"agent_reason": "\ud800"}),
            ("user_words", {"user_words": "because \ud83d"}),
            ("refs[1]", {"refs": ["#1", "\ude00"]}),
            ("decided_at", {"decided_at": "2026-11-26T02:41:07\ud800"}),
        ],
    )
    def test_an_unpaired_surrogate_is_refused_by_member(self, member, overrides):
        error = _refused(member, **overrides)
        # The message is returned to the caller, so it must itself encode.
        str(error).encode("utf-8")


class TestSyntax:
    @pytest.mark.parametrize("ending", ["\n", "\r", "\u0085", " ", " ", "\x0b", "\x1c"])
    def test_a_question_with_a_line_break_is_refused(self, ending):
        _refused("question", question=f"first{ending}second")
        _refused("question", question=f"only{ending}")

    def test_question_length_boundary(self):
        assert build_decision_record(**_args(question="q" * 120)).members["question"] == "q" * 120
        _refused("question", question="q" * 121)

    @pytest.mark.parametrize("question", ["", "   "])
    def test_an_empty_question_is_refused(self, question):
        _refused("question", question=question)

    @pytest.mark.parametrize("chosen", ["", " \t"])
    def test_an_empty_choice_is_refused(self, chosen):
        _refused("chosen", chosen=chosen)

    @pytest.mark.parametrize(
        "label",
        ["claude-code", "codex-cli", "any", "kimi-code", "a1", "x" * 32],
    )
    def test_labels_accepted(self, label):
        record = build_decision_record(**_args(recorded_by=label))
        assert record.members["recorded_by"] == label

    @pytest.mark.parametrize(
        "label",
        [
            "",
            "Claude-Code",
            "claude code",
            "claude_code",
            "-claude",
            "claude-",
            "claude--code",
            "claude\u0085code",
            "claude code",
            "claude code",
            "claude\ncode",
            "클로드",
            "x" * 33,
        ],
    )
    def test_labels_refused(self, label):
        _refused("recorded_by", recorded_by=label)
        _refused("recommended_by", recommended="not yet", recommended_by=label)

    @pytest.mark.parametrize("project", ["memtomem", "My.Repo_2", "a", "repo-copy-", "..a"])
    def test_project_accepted(self, project):
        assert build_decision_record(**_args(project=project)).project == project

    @pytest.mark.parametrize(
        "project", ["", "a,b", "a*", "-a", ".", "..", "a:b", "a b", "a/b", "프로젝트", "a\x00"]
    )
    def test_project_refused(self, project):
        _refused("project", project=project)

    @pytest.mark.parametrize(
        "decision_id",
        [
            "",
            DECISION_ID.upper(),
            DECISION_ID.replace("-", ""),
            "{" + DECISION_ID + "}",
            DECISION_ID + "\n",
            DECISION_ID[:-1] + "g",
            "1",
        ],
    )
    def test_decision_id_must_be_a_lowercase_canonical_uuid(self, decision_id):
        _refused("decision_id", decision_id=decision_id)


class TestDecidedAt:
    def test_default_is_the_given_now(self):
        record = build_decision_record(**_args())
        assert record.members["decided_at"] == "2026-11-26T02:41:09+00:00"

    @pytest.mark.parametrize(
        "given",
        [
            "2026-11-26T02:41:07+00:00",
            "2026-11-26T02:41:07Z",
            "2026-11-26T11:41:07+09:00",
            "2026-11-25T21:41:07-05:00",
            "2026-11-26T02:41:07.987654+00:00",
        ],
    )
    def test_every_accepted_form_is_stored_as_the_same_utc_second(self, given):
        record = build_decision_record(**_args(decided_at=given))
        assert record.members["decided_at"] == "2026-11-26T02:41:07+00:00"

    @pytest.mark.parametrize(
        "given",
        ["2026-11-26", "2026-11-26T02:41:07", "yesterday", "", " 2026-11-26T02:41:07Z"],
    )
    def test_a_date_alone_a_naive_time_and_non_timestamps_are_refused(self, given):
        _refused("decided_at", decided_at=given)

    @pytest.mark.parametrize("given", ["0001-01-01T00:00:00+01:00", "9999-12-31T23:59:59-01:00"])
    def test_a_time_that_does_not_convert_to_utc_is_refused_by_name(self, given):
        _refused("decided_at", decided_at=given)


class TestRelations:
    def test_fewer_than_two_options(self):
        _refused("options", options=["only"])
        _refused("options", options=[])

    def test_equal_options(self):
        _refused("options", options=["a", "b", "a"])

    def test_recommended_must_be_an_option_exactly(self):
        _refused("recommended", recommended="patterns")
        _refused("recommended", recommended="patterns (Recommended) ")

    def test_reason_and_recommender_need_a_recommendation(self):
        _refused("agent_reason", agent_reason="it expires")
        _refused("recommended_by", recommended_by="claude-code")

    def test_recommended_by_defaults_to_the_recorder_only_with_a_recommendation(self):
        with_rec = build_decision_record(**_args(recommended="interests"))
        assert with_rec.members["recommended_by"] == "claude-code"
        without = build_decision_record(**_args())
        assert without.members["recommended_by"] is None

    def test_caps_on_the_agents_own_words(self):
        ok = build_decision_record(**_args(recommended="interests", agent_reason="r" * 240))
        assert len(ok.members["agent_reason"]) == 240
        _refused("agent_reason", recommended="interests", agent_reason="r" * 241)
        assert build_decision_record(**_args(refs=["#1"] * 5)).members["refs"] == ["#1"] * 5
        _refused("refs", refs=["#1"] * 6)

    def test_the_chooser_is_written(self):
        assert build_decision_record(**_args()).members["chosen_by"] == "user"

    def test_member_order_is_the_written_order(self):
        assert list(build_decision_record(**_args()).members) == [
            "decision_id",
            "decided_at",
            "project",
            "question",
            "options",
            "recommended",
            "recommended_by",
            "agent_reason",
            "chosen",
            "chosen_by",
            "user_words",
            "recorded_by",
            "refs",
        ]


#: Values a verbatim member must survive: each would break an entry written
#: as ``key: value`` lines.
HOSTILE = [
    "line one\nline two",
    "```\n## Not a heading\n```",
    "~~~~\ntilde fence",
    "## Decision 00000000-0000-0000-0000-000000000000",
    '> tags: ["forged"]',
    "see [[some note]] | none",
    'quote " and backslash \\ and \\n spelled out',
    "nel\u0085ls ps end",
    "\r\n\x0b\x0c\x1c\x1d\x1e tab\t",
    "한글 선택지 (Recommended) 🙂",
]


class TestRendering:
    @pytest.mark.parametrize("value", HOSTILE)
    def test_the_body_parses_back_to_the_members(self, value):
        record = build_decision_record(
            **_args(
                options=[value, "other"],
                recommended=value,
                chosen=value,
                user_words=value,
                refs=[value],
            )
        )
        assert parse_body(record.body) == record.members
        assert record.members["chosen"] == value

    @pytest.mark.parametrize("value", HOSTILE)
    def test_no_value_adds_a_line_or_starts_one(self, value):
        record = build_decision_record(
            **_args(options=[value, "other"], chosen=value, user_words=value, refs=[value])
        )
        lines = record.body.split("\n")
        # What the file's line counters see is what ``\n`` splits.
        assert record.body.splitlines() == lines
        assert lines[0] == "```json" and lines[-1] == "```"
        # 13 members, two list brackets each for options and refs, their items, two braces.
        assert len(lines) == 2 + 2 + 11 + (2 + 2) + (2 + 1)
        for line in lines[1:-1]:
            assert (
                line[0] in "{}"
                or line.startswith('  "')
                or line.startswith("  ]")
                or (line.startswith('    "'))
            ), line

    def test_the_three_separators_are_escaped_not_dropped(self):
        record = build_decision_record(**_args(user_words="a\u0085b c d"))
        assert "\\u0085" in record.body and "\\u2028" in record.body and "\\u2029" in record.body
        for raw in "\u0085  ":
            assert raw not in record.body

    def test_non_ascii_text_stays_readable(self):
        record = build_decision_record(**_args(chosen="표본은 아직"))
        assert "표본은 아직" in record.body

    @pytest.mark.parametrize("value", HOSTILE)
    def test_the_entry_keeps_its_heading_and_tags_between_neighbours(self, value):
        """Through the real entry renderer and markdown chunker."""
        record = build_decision_record(
            **_args(options=[value, "other"], chosen=value, user_words=value)
        )
        text = "".join(
            [
                format_entry_block("the entry before", "Before", ["tag-before"]),
                format_entry_block(record.body, record.title, record.tags),
                format_entry_block("the entry after", "After", ["tag-after"]),
            ]
        )
        chunks = MarkdownChunker().chunk_file(Path("2026-11-26--shared-fixture.md"), text)
        by_heading = {chunk.metadata.heading_hierarchy[-1]: chunk for chunk in chunks}
        assert set(by_heading) == {"## Before", f"## {record.title}", "## After"}, sorted(
            by_heading
        )
        assert set(by_heading[f"## {record.title}"].metadata.tags) == set(record.tags)
        assert set(by_heading["## Before"].metadata.tags) == {"tag-before"}
        assert set(by_heading["## After"].metadata.tags) == {"tag-after"}


class TestSize:
    def _record_of_body_length(self, target: int):
        base = build_decision_record(**_args(user_words=""))
        return _args(user_words="w" * (target - len(base.body)))

    def test_a_body_at_the_cap_is_accepted_whole(self):
        record = build_decision_record(**self._record_of_body_length(CAP))
        assert len(record.body) == CAP
        assert parse_body(record.body) == record.members

    def test_a_body_over_the_cap_is_refused_with_its_length(self):
        with pytest.raises(DecisionRecordError) as caught:
            build_decision_record(**self._record_of_body_length(CAP + 1))
        assert str(CAP + 1) in str(caught.value) and str(CAP) in str(caught.value)

    def test_escaping_counts_toward_the_cap(self):
        """The cap is on what is written, and a quote is written as two characters."""
        args = self._record_of_body_length(CAP)
        args["user_words"] = '"' + args["user_words"][1:]
        with pytest.raises(DecisionRecordError):
            build_decision_record(**args)


class TestTags:
    def test_taken(self):
        record = build_decision_record(**_args(recommended="not yet"))
        assert record.tags == ["decision-record", "from-claude-code", "decision-user-taken"]

    def test_declined(self):
        record = build_decision_record(**_args(recommended="interests"))
        assert record.tags == ["decision-record", "from-claude-code", "decision-user-declined"]

    def test_no_recommendation(self):
        record = build_decision_record(**_args(recorded_by="codex-cli"))
        assert record.tags == ["decision-record", "from-codex-cli", "decision-user-norec"]

    def test_the_users_own_answer_with_a_recommendation_is_a_decline(self):
        record = build_decision_record(
            **_args(recommended="interests", chosen="none of these, ask me next week")
        )
        assert record.tags[-1] == "decision-user-declined"

    def test_title(self):
        assert build_decision_record(**_args()).title == f"Decision {DECISION_ID}"


class TestProjectSlug:
    @pytest.mark.parametrize(
        ("name", "slug"),
        [
            ("memtomem", "memtomem"),
            ("My Repo", "My-Repo"),
            ("  spaced  out  ", "spaced-out-"),
            ("--lead", "lead"),
            ("a@@b", "a-b"),
            ("repo.v2_x", "repo.v2_x"),
            ("한글", None),
            ("", None),
            (".", None),
            ("..", None),
            ("-", None),
        ],
    )
    def test_the_handoff_rule(self, name, slug):
        assert project_slug(name) == slug

    @pytest.mark.parametrize("name", ["memtomem", "My Repo", "--lead", "a@@b", "x-"])
    def test_a_derived_slug_is_an_accepted_project(self, name):
        slug = project_slug(name)
        assert slug is not None
        assert build_decision_record(**_args(project=slug)).project == slug
