"""The JWT credential rule finds what it found before, in linear time and memory.

The old rule tried a match after every ``-`` inside a run of base64url
characters, and each attempt scanned to the end of the run, so a text of
``eyJ-`` repeated held ``scan()`` for seconds (GHSA-rr87-p3f8-qhxj). The
rewrite must give the same hits — checked against the old rule, kept in
``privacy._JWT_RULE_OLD`` as the oracle — and must finish, without building up
regex state, on the inputs that used to stall. Those checks run in a child
process so a regression fails instead of hanging the suite.

``scan()`` reports the old rule's match starts too: the new rule may begin a
match at a later ``eyJ`` in the same run, and ``scan()`` moves it back, so
redaction and the audit commands see the same spans as before.
"""

from __future__ import annotations

import random
import re
import subprocess
import sys
import textwrap

import pytest

from memtomem import privacy

OLD_JWT_RULE = re.compile(privacy._JWT_RULE_OLD)
NEW_JWT_RULE = re.compile(privacy._JWT_RULE)
JWT_INDEX = privacy.DEFAULT_PATTERNS.index(privacy._JWT_RULE)

_ALPHABET = [*"ab9_-.", "eyJ", "-eyJ", "eyJAAAAAAAA", " ", "AAAAAAAA", "é", "\n", "가", "--"]

STRUCTURED = [
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.abcDEF_-12345",
    "Bearer eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0In0.SflKxwRJSMeKKF2QT4fwpMeJf36",
    "x-eyJAAAAAAAA.BBBBBBBB.CCCCCCCC",
    "eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD",
    "eyJAAAAAAAA-eyJBB.CCCCCCCC.DDDDDDDD",
    "eyJAAAAAAA.BBBBBBBB.CCCCCCCC",
    "eyJAAAAAAAA.BBBBBBBB.CCCCCCC",
    "eyJAAAAAAAA.BBBBBBBB.CCCCCCCC-",
    "_eyJAAAAAAAA.BBBBBBBB.CCCCCCCC",
    "éeyJAAAAAAAA.BBBBBBBB.CCCCCCCC",
    # ``é`` is not an ASCII word character, so the first ``eyJ`` is on a
    # boundary and the old rule begins there.
    "éeyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD",
    "ab-eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD",
    "eyJAAAAAAAA.BBBBBBBB.CCCCCCCC-eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD",
    "eyJ-" * 20,
    "eyJ-" * 20 + "AAAAAAAA.BBBBBBBB.CCCCCCCC",
    "hdr.eyJAAAAAAAA.BBBBBBBB.CCCCCCCC.extra",
]


def _strings() -> list[str]:
    rng = random.Random(21)
    fuzz = ["".join(rng.choice(_ALPHABET) for _ in range(rng.randint(0, 30))) for _ in range(20000)]
    return fuzz + STRUCTURED


def _jwt_spans(text: str) -> list[tuple[int, int]]:
    return [hit.span for hit in privacy.scan(text) if hit.pattern_index == JWT_INDEX]


def test_the_rule_alone_gives_the_same_answer_count_and_ends():
    for text in _strings():
        old = [m.span() for m in OLD_JWT_RULE.finditer(text)]
        new = [m.span() for m in NEW_JWT_RULE.finditer(text)]
        assert bool(NEW_JWT_RULE.search(text)) == bool(OLD_JWT_RULE.search(text)), repr(text)
        assert [end for _, end in new] == [end for _, end in old], repr(text)
        # A match may begin at a later ``eyJ`` in the same run, never earlier.
        assert all(n >= o for (o, _), (n, _) in zip(old, new, strict=True)), repr(text)


def test_scan_reports_the_old_rules_spans():
    for text in _strings():
        assert _jwt_spans(text) == [m.span() for m in OLD_JWT_RULE.finditer(text)], repr(text)


def test_a_later_start_in_the_same_run_is_moved_back():
    text = "eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD"
    assert [m.span() for m in NEW_JWT_RULE.finditer(text)] == [(12, len(text))]
    assert _jwt_spans(text) == [(0, len(text))]


_CHILD = """
import sys, time, tracemalloc
from memtomem.privacy import scan
text = {payload}
scan("warm up")
tracemalloc.start()
start = time.perf_counter()
scan(text)
elapsed = time.perf_counter() - start
peak = tracemalloc.get_traced_memory()[1]
print(f"{{elapsed:.3f}}s {{peak / 2**20:.1f}}MiB")
sys.exit(0 if elapsed < 2.0 and peak < 8 * 2**20 else 1)
"""

# Inputs that stalled the old rule (``eyJ-`` repeated: 7.75 s at 256 KB), the
# shapes that made a per-character rewrite hold a backtracking frame per
# character (146 MiB for one 1 MB segment), and runs where scan() moves many
# starts back: one long run ahead of a token, and many tokens chained by ``-``.
STALL_CASES = {
    "repeated-starts": "'eyJ-' * 64_000",
    "one-long-segment": "'eyJ' + 'A' * 1_024_000",
    "hyphen-dense-segment": "'eyJ' + 'a-' * 512_000",
    "viable-starts-without-tail": "'-eyJAAAAAAAA' * 80_000",
    "long-run-before-a-token": "'eyJ' + 'A-' * 256_000 + 'eyJBBBBBBBB.CCCCCCCC.DDDDDDDD'",
    "chained-tokens": "'eyJAAAAAAAA-eyJBBBBBBBB.CCCCCCCC.DDDDDDDD-' * 20_000",
}


@pytest.mark.parametrize("case", sorted(STALL_CASES))
def test_crafted_input_does_not_stall(case):
    code = textwrap.dedent(_CHILD).format(payload=STALL_CASES[case])
    try:
        done = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
    except subprocess.TimeoutExpired:
        pytest.fail(f"{case}: still scanning after 60 s", pytrace=False)
    assert done.returncode == 0, f"{case}: {done.stdout.strip()} {done.stderr.strip()[-500:]}"
