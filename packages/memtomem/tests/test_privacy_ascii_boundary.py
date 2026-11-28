"""A token written against non-ASCII text is still found (#3298).

Python's ``\\b`` counts every Unicode letter and digit as a word character, so a
credential with a Korean particle attached, or sitting inside Chinese or
Japanese prose, had no boundary on that side and ten of the token rules missed
it. The rules now spell their boundaries as ASCII lookarounds.

What that must and must not change is checked against two references built
from the patterns themselves: the set as both repos spelled it before (the
same strings with ``\\b``), and that spelling with the boundary made ASCII by a
scoped inline flag, which is the behaviour the lookarounds are meant to have.
"""

from __future__ import annotations

import hashlib
import re
import re._parser as re_parser
from re._constants import AT, AT_BOUNDARY, AT_NON_BOUNDARY

import pytest

from helpers import StubCtx
from memtomem import privacy
from memtomem.server.context import AppContext
from memtomem.server.tools.memory_crud import mem_add
from test_privacy import _PATTERN_FIXTURES

_LEADING = "(?<![A-Za-z0-9_])"
_TRAILING = "(?![A-Za-z0-9_])"
_TRAILING_AFTER_HYPHEN_CLASS = "(?<=[A-Za-z0-9_])(?![A-Za-z0-9_])"

# What this repo pinned before #3298, and memtomem-stm until its mirror
# (memtomem-stm#1378).
_SET_BEFORE_3298_SHA256 = "631d79e538a2000424dd3493737bb60580f7516a20bdacddd12df9432caa99d8"
# The browser's JWT rule as it was sent before #3298. It is not in
# ``DEFAULT_PATTERNS``, so the hash above does not vouch for it.
_BROWSER_JWT_RULE_WITH_WORD_BOUNDARIES = (
    r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"
)

_NON_ASCII = ("가", "を", "密", "é")
_NEIGHBOURS = ("", " ", "x", "7", "_", "-", ".", "/", *_NON_ASCII, "٢")
_ASCII_NEIGHBOURS = tuple(n for n in _NEIGHBOURS if n.isascii())

_JWT_HEAD = "eyJAAAAAAAA.BBBBBBBB."
_JWT_TAILS = ("abcdefgh", "abcdefgh-", "abcdefg-", "--------", "a---------", "abcdefg----")


def _with_word_boundaries(pattern: str) -> str:
    """``pattern`` with its ASCII lookarounds spelled ``\\b`` again."""
    return (
        pattern.replace(_TRAILING_AFTER_HYPHEN_CLASS, r"\b")
        .replace(_LEADING, r"\b")
        .replace(_TRAILING, r"\b")
    )


def _ascii_boundary(pattern: str) -> re.Pattern[str]:
    """The ``\\b`` spelling with ``\\b`` meaning the ASCII boundary, nothing else changed."""
    return re.compile(_with_word_boundaries(pattern).replace(r"\b", r"(?a:\b)"))


#: The rules #3298 rewrote were among the first 20; later additions (GHSA-h6g8
#: and on) were written with ASCII boundaries from the start.
_RULES_AT_3298 = privacy.DEFAULT_PATTERNS[:20]

_REWRITTEN = tuple(i for i, p in enumerate(_RULES_AT_3298) if _with_word_boundaries(p) != p)


def _rule_with(fragment: str) -> int:
    (index,) = [i for i, p in enumerate(privacy.DEFAULT_PATTERNS) if fragment in p]
    return index


# Rules whose last class holds ASCII word characters only, so an underscore
# behind the token is not consumed and has to block the match. Named here, not
# read off the patterns: a rule that lost its guard would drop out of a list
# derived from them.
_TRAILING_GUARDED = tuple(
    _rule_with(fragment) for fragment in ("npm_", "(?:AKIA|ASIA)", "gh[ousr]_", "hf_(")
)
_JWT_INDEX = privacy.DEFAULT_PATTERNS.index(privacy._JWT_RULE)


def _bodies(index: int) -> list[str]:
    bodies = list(_PATTERN_FIXTURES[index])
    if index == _JWT_INDEX:
        bodies += [_JWT_HEAD + tail for tail in _JWT_TAILS]
    return bodies


def _texts(index: int, neighbours: tuple[str, ...]) -> list[str]:
    return [
        f"{before}{body}{after}"
        for body in _bodies(index)
        for before in neighbours
        for after in neighbours
    ]


def _spans(compiled: re.Pattern[str], text: str) -> list[tuple[int, int]]:
    return [m.span() for m in compiled.finditer(text)]


def _hit(text: str, index: int) -> bool:
    return any(hit.pattern_index == index for hit in privacy.scan(text))


def _boundary_assertions(pattern: str) -> int:
    """How many ``\\b`` / ``\\B`` assertions ``re`` parses out of ``pattern``."""
    found = 0
    stack: list[object] = [re_parser.parse(pattern)]
    while stack:
        node = stack.pop()
        if isinstance(node, re_parser.SubPattern):
            for op, av in node:
                if op is AT and av in (AT_BOUNDARY, AT_NON_BOUNDARY):
                    found += 1
                else:
                    stack.append(av)
        elif isinstance(node, (list, tuple)):
            stack.extend(node)
    return found


def test_the_set_differs_from_the_one_before_3298_only_in_how_boundaries_are_spelled():
    as_before = [_with_word_boundaries(p) for p in _RULES_AT_3298]
    joined = "\n".join(as_before).encode()
    assert hashlib.sha256(joined).hexdigest() == _SET_BEFORE_3298_SHA256
    assert len(_REWRITTEN) == 10


def test_the_browsers_jwt_rule_differs_from_the_one_sent_before_only_in_its_boundaries():
    assert _with_word_boundaries(privacy._JWT_RULE_OLD) == _BROWSER_JWT_RULE_WITH_WORD_BOUNDARIES


@pytest.mark.parametrize("index", _REWRITTEN)
def test_a_rewritten_rule_matches_what_an_ascii_word_boundary_matches(index):
    rule = re.compile(privacy.DEFAULT_PATTERNS[index])
    reference = _ascii_boundary(privacy.DEFAULT_PATTERNS[index])
    for text in _texts(index, _NEIGHBOURS):
        assert _spans(rule, text) == _spans(reference, text), repr(text)


def test_the_browsers_jwt_rule_matches_what_an_ascii_word_boundary_matches():
    rule = re.compile(privacy._JWT_RULE_OLD)
    reference = re.compile(_BROWSER_JWT_RULE_WITH_WORD_BOUNDARIES.replace(r"\b", r"(?a:\b)"))
    for text in _texts(_JWT_INDEX, _NEIGHBOURS):
        assert _spans(rule, text) == _spans(reference, text), repr(text)


@pytest.mark.parametrize("index", _REWRITTEN)
def test_nothing_changes_between_ascii_neighbours(index):
    rule = re.compile(privacy.DEFAULT_PATTERNS[index])
    before_3298 = re.compile(_with_word_boundaries(privacy.DEFAULT_PATTERNS[index]))
    for text in _texts(index, _ASCII_NEIGHBOURS):
        assert _spans(rule, text) == _spans(before_3298, text), repr(text)


@pytest.mark.parametrize("neighbour", _NON_ASCII)
@pytest.mark.parametrize("index", range(len(privacy.DEFAULT_PATTERNS)))
def test_every_rule_finds_its_token_against_non_ascii_text(index, neighbour):
    token = _PATTERN_FIXTURES[index][0]
    assert _hit(f"{neighbour}{token}", index)
    assert _hit(f"{token}{neighbour}", index)
    assert _hit(f"{neighbour}{token}{neighbour}", index)


@pytest.mark.parametrize("index", _REWRITTEN)
@pytest.mark.parametrize("neighbour", ["x", "7", "_"])
def test_an_ascii_word_character_in_front_is_still_not_a_boundary(index, neighbour):
    assert not _hit(f"{neighbour}{_PATTERN_FIXTURES[index][0]}", index)


@pytest.mark.parametrize("index", _TRAILING_GUARDED)
def test_an_underscore_behind_is_still_not_a_boundary(index):
    assert not _hit(f"{_PATTERN_FIXTURES[index][0]}_", index)


def test_no_rule_uses_a_word_boundary_escape():
    # A rule synced back with ``\b`` reintroduces the miss; write the boundary
    # as the ASCII lookarounds described above ``DEFAULT_PATTERNS``.
    rules = [*privacy.DEFAULT_PATTERNS, *privacy._JS_PATTERN_OVERRIDES.values()]
    assert {rule: _boundary_assertions(rule) for rule in rules if _boundary_assertions(rule)} == {}


def test_the_boundary_check_sees_what_it_is_looking_for():
    assert _boundary_assertions(r"\bnpm_[a-z]+\b") == 2
    assert _boundary_assertions(r"(?:a|(?=\Bx))(?<=\by)") == 2
    assert _boundary_assertions(r"[\b]\\b") == 0


@pytest.mark.parametrize("tail", ["--------가", "a---------가", "abcdefg----٢", "abcdefg-가"])
def test_a_jwt_tail_of_hyphens_before_a_non_ascii_word_character_is_not_matched(tail):
    # Accepted with #3298: the Unicode boundary between the last ``-`` and that
    # character used to end the match, and the ASCII boundary has none there.
    assert not _hit(_JWT_HEAD + tail, _JWT_INDEX)


def test_a_jwt_match_ends_at_its_last_word_character():
    spans = [h.span for h in privacy.scan(_JWT_HEAD + "abcdefgh-가")]
    assert spans == [(0, len(_JWT_HEAD) + 8)]
    assert _hit(_JWT_HEAD + "abcdefgh가", _JWT_INDEX)


@pytest.mark.parametrize("neighbour", ["가", "é", "٢"])
def test_a_jwt_start_is_moved_back_across_a_non_ascii_neighbour(neighbour):
    text = f"{neighbour}eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD"
    raw = [m.span() for m in re.finditer(privacy._JWT_RULE, text)]
    assert raw == [(13, len(text))]
    assert [h.span for h in privacy.scan(text)] == [(1, len(text))]
    assert [m.span() for m in re.finditer(privacy._JWT_RULE_OLD, text)] == [(1, len(text))]


def test_a_label_still_matches_across_an_ideographic_space():
    # Why the patterns are not simply compiled with ``re.ASCII``.
    password_rule = privacy.DEFAULT_PATTERNS.index(r"(?i)(password|passwd|pwd)\s*[:=]")
    assert _hit("pwd\u3000= x", password_rule)


@pytest.mark.asyncio
async def test_mem_add_refuses_a_key_id_with_a_particle_attached(bm25_only_components):
    comp, mem_dir = bm25_only_components
    target = mem_dir / "notes.md"
    key_id = _PATTERN_FIXTURES[9][0]

    result = await mem_add(  # type: ignore[arg-type]
        content=f"배포 키 {key_id}를 사용한다",
        file=str(target),
        ctx=StubCtx(AppContext.from_components(comp)),
    )

    assert "privacy pattern" in result, result
    assert not target.exists()
