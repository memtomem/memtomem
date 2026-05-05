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
        # Soft ceiling — not a microbenchmark, just a quadratic-regression
        # guard. The 9 current patterns are short prefix-anchored regexes
        # and ``re.finditer`` over 1 MB completes in single-digit ms on
        # CI hardware; 200 ms gives ample headroom for jitter while
        # still catching an accidental O(N^2) rewrite.
        text = ("a" * 999_980) + _SECRET
        start = time.perf_counter()
        hits = privacy.scan(text)
        elapsed = time.perf_counter() - start
        assert hits, "Sanity check: the secret must still be detected"
        assert elapsed < 0.2, (
            f"1MB scan took {elapsed * 1000:.1f} ms — exceeds the 200 ms "
            "ceiling, suggesting a non-linear regression"
        )
