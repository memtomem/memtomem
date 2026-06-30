"""Long-content scan coverage — pin against the silent-truncation bypass.

The earlier revision of ``privacy.scan()`` truncated at the first 10 K
chars to mirror STM's compression-side scanner. At the LTM trust
boundary that cap is a silent bypass: any secret pasted past the mark
wrote through unredacted. These tests pin the post-fix contract — the
entire input is scanned — at three sizes spanning the former cap, and
each assertion has a paired negative (clean prose of identical shape
must produce zero hits).

Pin-and-invert + mutation-validation rationale:

- Each positive case asserts ``hits`` is non-empty AND that the recorded
  span lies inside the embedded-secret slice. A future regression that
  re-introduces a truncation would produce ``hits == []`` and fail
  loudly. A regression that broadens the patterns would mismatch the
  span and also fail.
- Each negative case asserts ``hits == []`` so a future false-positive
  drift on benign long prose surfaces immediately.

The perf pin is a soft ceiling — generous enough that platform-jitter
won't flake but tight enough to catch a quadratic regression (e.g. a
naive overlap-window rewrite that re-scans large prefixes).
"""

from __future__ import annotations

import time

import pytest

from memtomem import privacy


# OpenAI-style ``sk-`` prefix — matches DEFAULT_PATTERNS index 2.
# Picked over the AWS ``AKIA…`` shape because that pattern is
# word-boundary-anchored (``\b…\b``), and a leading run of word chars
# (e.g. ``"a" * N + "AKIA…"``) would silently fail to hit on \b
# rather than on a truncation regression — the test would conflate
# "scan ran past N" with "boundary found." Using the prefix-only
# ``sk-`` shape removes that ambiguity: the only way to miss this
# secret is to not scan its position.
_SECRET = "sk-" + "a" * 30


# Sizes straddle the former 10K cap. 12K is just past it; 100K and 1MB
# stress the linear-scan claim in the docstring.
@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


class TestLongContentScan:
    @pytest.mark.parametrize(
        "size",
        [12_000, 100_000, 1_000_000],
        ids=["12K", "100K", "1MB"],
    )
    def test_secret_at_end_of_long_input_is_caught(self, size: int):
        prefix = "a" * (size - len(_SECRET))
        text = prefix + _SECRET
        hits = privacy.scan(text)
        assert hits, (
            f"Secret at the end of a {size}-char input must hit. "
            f"A silent truncation regression would return []."
        )
        # At least one hit's span lands on the trailing secret slice.
        secret_start = size - len(_SECRET)
        assert any(h.span[0] >= secret_start for h in hits), (
            f"No hit landed on the trailing secret slice ({secret_start}..{size}); "
            f"got spans {[h.span for h in hits]}"
        )

    @pytest.mark.parametrize(
        "size",
        [12_000, 100_000, 1_000_000],
        ids=["12K", "100K", "1MB"],
    )
    def test_long_clean_prose_has_no_hit(self, size: int):
        # Repeated benign sentence. Picked from the existing clean-input
        # fixtures in ``test_privacy.py`` so a future PII-pattern drift
        # would fail both surfaces consistently.
        sentence = "Met with John today to discuss Q2 plans. "
        text = (sentence * (size // len(sentence) + 1))[:size]
        assert len(text) == size
        assert privacy.scan(text) == [], (
            f"{size}-char benign prose must produce zero hits; "
            "a non-empty result here means the pattern set is overreaching"
        )

    def test_secret_in_middle_of_1mb_is_caught(self):
        # Trailing-position pin alone could be satisfied by a "scan only
        # the last N chars" rewrite. This anchors the contract at an
        # arbitrary interior position so a truncation in either
        # direction fails.
        size = 1_000_000
        mid = size // 2
        prefix = "a" * mid
        suffix = "a" * (size - mid - len(_SECRET))
        text = prefix + _SECRET + suffix
        hits = privacy.scan(text)
        assert hits
        assert any(mid <= h.span[0] < mid + len(_SECRET) for h in hits), (
            f"No hit landed on the mid-buffer secret at offset {mid}; "
            f"got spans {[h.span for h in hits]}"
        )

    def test_one_megabyte_scan_under_perf_ceiling(self):
        # Soft ceiling — not a microbenchmark, just a non-linear-regression
        # guard. ``scan()`` runs one ``re.finditer`` pass per pattern, so cost
        # grows linearly with the pattern count: the 16 short prefix-anchored
        # ``DEFAULT_PATTERNS`` take ~100 ms locally and ~200 ms on a loaded CI
        # runner for a 1 MB input (#1488 grew the set 9 -> 16, each added
        # secret class a linear pass — no single hot spot). The 500 ms ceiling
        # — matching ``test_crafted_repetition_scans_linearly`` below — keeps
        # ~2x headroom over that worst case while still catching a genuine
        # non-linear rewrite (e.g. a naive overlap-window scan that re-reads
        # large prefixes), which would run in seconds at 1 MB, not hundreds of
        # ms. The dedicated O(N^2) backtracking guard is that sibling test.
        text = ("a" * 999_980) + _SECRET
        start = time.perf_counter()
        hits = privacy.scan(text)
        elapsed = time.perf_counter() - start
        assert hits, "Sanity check: the secret must still be detected"
        assert elapsed < 0.5, (
            f"1MB scan took {elapsed * 1000:.1f} ms — exceeds the 500 ms "
            "ceiling, suggesting a non-linear regression"
        )

    @pytest.mark.parametrize(
        "blob",
        [
            "glpat-" * 60_000,  # digit-lookahead pattern, class incl. '-'
            "sk-proj-" * 50_000,  # greedy segment + required T3BlbkFJ marker
            "sk-ant-api00-" * 30_000,  # digit-lookahead after a literal prefix
            "glpat-sk-proj-AIzahf_gho_" * 40_000,  # ~1MB mixed prefixes
        ],
        ids=["glpat-rep", "sk-proj-rep", "sk-ant-rep", "mixed-1mb"],
    )
    def test_crafted_repetition_scans_linearly(self, blob: str):
        # #1488 adds patterns with bounded greedy segments ({20,200}) and
        # bounded digit-lookaheads ({0,128}/{0,33}). A naive UNbounded form
        # (e.g. ``glpat-(?=[0-9A-Za-z_-]*[0-9])…`` whose class contains '-')
        # is O(N^2) on a crafted prefix repetition: each of ~N/6 prefix
        # positions scans the rest of the buffer for a digit that never
        # comes. This pins linear-time behavior so a future maintainer who
        # drops the bound regresses here loudly rather than shipping a
        # quadratic scan past the trust boundary.
        start = time.perf_counter()
        privacy.scan(blob)
        elapsed = time.perf_counter() - start
        assert elapsed < 0.5, (
            f"crafted {len(blob)}-char repetition scanned in {elapsed * 1000:.1f} ms — "
            "exceeds the 500 ms ceiling, indicating an O(N^2) backtracking regression "
            "in one of the #1488 bounded patterns"
        )
