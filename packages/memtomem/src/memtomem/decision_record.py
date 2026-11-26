"""Decision records: what ``mem_decision_record`` validates and writes.

A decision record is one decision point, written when it happens: the options
an agent offered, the one it recommended, and the one the user picked. The
recommendation is the agent's judgement and the pick is the user's, and the
record keeps the two in separate members.

The record is an ordinary memory entry whose body is one JSON object in a
closed ``json`` fence. This module is the part that needs no server: it checks
the caller's values, builds the object, renders the body and derives the tags.
The MCP dispatcher checks only booleans and argument binding on the ``mem_do``
path, so every type is checked here.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

MAX_QUESTION_CHARS = 120
MAX_AGENT_REASON_CHARS = 240
MAX_REFS = 5
MAX_LABEL_CHARS = 32

#: The only chooser this record kind has. Written anyway, so a reader does not
#: infer the chooser from a record's age once another value exists.
CHOSEN_BY_USER = "user"

RECORD_TAG = "decision-record"
OUTCOME_TAKEN = "decision-user-taken"
OUTCOME_DECLINED = "decision-user-declined"
OUTCOME_NO_RECOMMENDATION = "decision-user-norec"

# A runtime label reaches the entry's ``> tags:`` line, which the writer
# serializes without escaping U+0085, U+2028 or U+2029, and a file holding one
# of those is refused for source rewrites as a whole. So the syntax is closed:
# which labels exist is not decided here, what a label may contain is.
_LABEL_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_PROJECT_RE = re.compile(r"[A-Za-z0-9._-]+")
_SLUG_UNSAFE_RUN_RE = re.compile(r"[^A-Za-z0-9._-]+")
_UUID_RE = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")

# ``json.dumps`` escapes the control characters below U+0020 and nothing else,
# so these three line separators would reach the file raw.
_LINE_SEPARATOR_ESCAPES = {
    0x0085: "\\u0085",
    0x2028: "\\u2028",
    0x2029: "\\u2029",
}

_FENCE_OPEN = "```json"
_FENCE_CLOSE = "```"


class DecisionRecordError(ValueError):
    """A value the record cannot hold. The message names the member, never its value."""

    def __init__(self, member: str, reason: str) -> None:
        super().__init__(f"{member} {reason}")
        self.member = member


@dataclass(frozen=True)
class DecisionRecord:
    """A validated record: its members in written order, and the rendered body."""

    members: dict[str, Any]
    body: str

    @property
    def decision_id(self) -> str:
        return self.members["decision_id"]

    @property
    def project(self) -> str:
        return self.members["project"]

    @property
    def title(self) -> str:
        return f"Decision {self.decision_id}"

    @property
    def tags(self) -> list[str]:
        """The entry's tags, all derived from the record."""
        recommended = self.members["recommended"]
        if recommended is None:
            outcome = OUTCOME_NO_RECOMMENDATION
        elif self.members["chosen"] == recommended:
            outcome = OUTCOME_TAKEN
        else:
            outcome = OUTCOME_DECLINED
        return [RECORD_TAG, f"from-{self.members['recorded_by']}", outcome]


def project_slug(directory_name: str) -> str | None:
    """The project slug for a project root's directory name, or ``None``.

    The rule the handoff workflow states for ``shared:<project-slug>``: keep
    case, turn each run of characters outside ``[A-Za-z0-9._-]`` into ``-``,
    and trim leading ``-``. ``None`` when nothing usable is left.
    """
    slug = _SLUG_UNSAFE_RUN_RE.sub("-", directory_name).lstrip("-")
    return None if slug in ("", ".", "..") else slug


def _string(member: str, value: object) -> str:
    if not isinstance(value, str):
        raise DecisionRecordError(member, f"must be a string, got {type(value).__name__}")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise DecisionRecordError(
            member, "holds an unpaired surrogate, which cannot be stored as UTF-8"
        ) from None
    return value


def _optional_string(member: str, value: object) -> str | None:
    return None if value is None else _string(member, value)


def _string_list(member: str, value: object) -> list[str]:
    if not isinstance(value, list):
        raise DecisionRecordError(member, f"must be a list of strings, got {type(value).__name__}")
    return [_string(f"{member}[{index}]", item) for index, item in enumerate(value)]


def _label(member: str, value: str) -> str:
    if len(value) > MAX_LABEL_CHARS or _LABEL_RE.fullmatch(value) is None:
        raise DecisionRecordError(
            member,
            f"must be a runtime label: lowercase letters and digits in groups joined by "
            f"single hyphens, at most {MAX_LABEL_CHARS} characters (e.g. claude-code)",
        )
    return value


def _decided_at(value: str | None, now: datetime) -> str:
    if value is None:
        moment = now
    else:
        try:
            moment = datetime.fromisoformat(value)
        except ValueError:
            raise DecisionRecordError(
                "decided_at", "must be an ISO 8601 timestamp with a UTC offset"
            ) from None
        if moment.utcoffset() is None:
            raise DecisionRecordError(
                "decided_at",
                "must carry a time and a UTC offset (e.g. 2026-11-26T02:41:07+00:00 or ...Z)",
            )
    try:
        utc = moment.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        raise DecisionRecordError(
            "decided_at", "is outside the range of timestamps that convert to UTC"
        ) from None
    return utc.replace(microsecond=0).isoformat()


def render_body(members: dict[str, Any]) -> str:
    """The entry body for ``members``: one JSON object in a closed ``json`` fence.

    One member, or one list item, per line. Every line the encoder writes
    starts with a brace, a bracket or indentation before a quote, and a string
    value never contains a raw line break, so no value can close the fence,
    start a heading, or change how the file's lines are counted.
    """
    text = json.dumps(members, ensure_ascii=False, indent=2)
    return f"{_FENCE_OPEN}\n{text.translate(_LINE_SEPARATOR_ESCAPES)}\n{_FENCE_CLOSE}"


def parse_body(body: str) -> dict[str, Any]:
    """The object a body rendered by :func:`render_body` holds."""
    lines = body.split("\n")
    if len(lines) < 3 or lines[0] != _FENCE_OPEN or lines[-1] != _FENCE_CLOSE:
        raise ValueError("not a decision record body")
    return json.loads("\n".join(lines[1:-1]))


def build_decision_record(
    *,
    decision_id: object,
    project: object,
    recorded_by: object,
    question: object,
    options: object,
    chosen: object,
    recommended: object = None,
    recommended_by: object = None,
    agent_reason: object = None,
    user_words: object = None,
    refs: object = None,
    decided_at: object = None,
    max_body_chars: int,
    now: datetime | None = None,
) -> DecisionRecord:
    """Validate the caller's values and return the record, or raise.

    Order: types, then each member's syntax, then the relations between
    members, then the size of the rendered body. ``options``, ``recommended``,
    ``chosen`` and ``user_words`` are kept exactly as given: nothing trims or
    cuts them, and a record too large to write is refused whole.

    Raises:
        DecisionRecordError: naming the first member that does not hold.
    """
    decision_id_s = _string("decision_id", decision_id)
    project_s = _string("project", project)
    recorded_by_s = _string("recorded_by", recorded_by)
    question_s = _string("question", question)
    options_l = _string_list("options", options)
    chosen_s = _string("chosen", chosen)
    recommended_s = _optional_string("recommended", recommended)
    recommended_by_s = _optional_string("recommended_by", recommended_by)
    agent_reason_s = _optional_string("agent_reason", agent_reason)
    user_words_s = _optional_string("user_words", user_words)
    refs_l = [] if refs is None else _string_list("refs", refs)
    decided_at_s = _optional_string("decided_at", decided_at)

    if _UUID_RE.fullmatch(decision_id_s) is None:
        raise DecisionRecordError(
            "decision_id", "must be a lowercase UUID (8-4-4-4-12 hexadecimal digits)"
        )
    if (
        _PROJECT_RE.fullmatch(project_s) is None
        or project_s.startswith("-")
        or project_s in (".", "..")
    ):
        raise DecisionRecordError(
            "project",
            "must be a project slug: letters, digits, '.', '_' and '-', not starting "
            "with '-', and not '.' or '..'",
        )
    _label("recorded_by", recorded_by_s)
    decided_at_utc = _decided_at(decided_at_s, now or datetime.now(timezone.utc))
    if not question_s.strip():
        raise DecisionRecordError("question", "must not be empty")
    if question_s.splitlines() != [question_s]:
        raise DecisionRecordError("question", "must be a single line")
    if len(question_s) > MAX_QUESTION_CHARS:
        raise DecisionRecordError(
            "question",
            f"is {len(question_s)} characters; write it in at most {MAX_QUESTION_CHARS}",
        )
    if not chosen_s.strip():
        raise DecisionRecordError("chosen", "must not be empty")

    if len(options_l) < 2:
        raise DecisionRecordError("options", "must hold at least two options")
    if len(set(options_l)) != len(options_l):
        raise DecisionRecordError("options", "must not hold the same option twice")
    if recommended_s is None:
        if agent_reason_s is not None:
            raise DecisionRecordError("agent_reason", "needs recommended")
        if recommended_by_s is not None:
            raise DecisionRecordError("recommended_by", "needs recommended")
    else:
        if recommended_s not in options_l:
            raise DecisionRecordError("recommended", "must be one of options, exactly")
        if recommended_by_s is None:
            recommended_by_s = recorded_by_s
        _label("recommended_by", recommended_by_s)
    if agent_reason_s is not None and len(agent_reason_s) > MAX_AGENT_REASON_CHARS:
        raise DecisionRecordError(
            "agent_reason",
            f"is {len(agent_reason_s)} characters; write it in at most {MAX_AGENT_REASON_CHARS}",
        )
    if len(refs_l) > MAX_REFS:
        raise DecisionRecordError("refs", f"holds {len(refs_l)} items; at most {MAX_REFS}")

    members: dict[str, Any] = {
        "decision_id": decision_id_s,
        "decided_at": decided_at_utc,
        "project": project_s,
        "question": question_s,
        "options": options_l,
        "recommended": recommended_s,
        "recommended_by": recommended_by_s,
        "agent_reason": agent_reason_s,
        "chosen": chosen_s,
        "chosen_by": CHOSEN_BY_USER,
        "user_words": user_words_s,
        "recorded_by": recorded_by_s,
        "refs": refs_l,
    }
    body = render_body(members)
    if len(body) > max_body_chars:
        raise DecisionRecordError(
            "the record",
            f"renders to {len(body)} characters, over the {max_body_chars} a memory entry "
            "may hold, and a record is never cut",
        )
    return DecisionRecord(members=members, body=body)
