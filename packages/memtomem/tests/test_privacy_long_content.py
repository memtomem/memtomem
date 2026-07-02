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

The perf pins are RELATIVE (small-input vs full-input ratio, #1545): a
fixed wall-clock ceiling flaked on shared macOS runners once the
forward-synced pattern set grew past the headroom (500.9 ms / 625.7 ms
vs the old 500 ms cap, on PRs that touched no privacy code). The pin's
job was never "scan 1 MB in under half a second" — it was "cost grows
linearly with input size" — and a ratio tests that directly, immune to
runner speed and to future pattern-set growth (each added pattern
scales both measurements equally).
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

# Linear scaling predicts the full/small time ratio ≈ the 10× size ratio;
# an O(N^2) regression predicts ≈ 100×. 20 sits 2× above linear (shared-runner
# jitter headroom; measured 9.7–10.1 locally across all fixtures) and 5× below
# quadratic, so it cannot flake on a slow runner yet still fails loudly on the
# #1488 backtracking class.
_LINEAR_RATIO_CEILING = 20


def _min_scan_time(text: str, repeats: int = 3) -> float:
    """Best-of-N wall time for one ``privacy.scan`` call.

    ``min`` is the standard low-noise timing estimator: a loaded runner can
    only ever ADD time to a run, so the minimum approaches the true cost, and
    taking it on BOTH sides of the ratio keeps the comparison stable.
    """
    best = float("inf")
    for _ in range(repeats):
        start = time.perf_counter()
        privacy.scan(text)
        best = min(best, time.perf_counter() - start)
    return best


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

    def test_one_megabyte_scan_stays_linear(self):
        # Relative pin (#1545) — not a microbenchmark, a non-linear-regression
        # guard. ``scan()`` runs one ``re.finditer`` pass per pattern, so cost
        # grows linearly with input size; a naive overlap-window rewrite that
        # re-reads large prefixes would scale quadratically instead. The old
        # absolute 500 ms ceiling flaked on shared macOS runners as the
        # forward-synced pattern set grew (memtomem-stm#553/#562/#561 → 19
        # patterns); the ratio is immune to both runner speed and pattern
        # count. Same-shape inputs at both sizes so per-position cost matches.
        small = ("a" * 99_980) + _SECRET
        full = ("a" * 999_980) + _SECRET
        assert privacy.scan(full), "Sanity check: the secret must still be detected"
        small_elapsed = _min_scan_time(small)
        full_elapsed = _min_scan_time(full)
        ratio = full_elapsed / max(small_elapsed, 0.001)
        assert ratio < _LINEAR_RATIO_CEILING, (
            f"1MB scan took {ratio:.1f}x the 100KB scan "
            f"({small_elapsed * 1000:.1f} ms → {full_elapsed * 1000:.1f} ms) — "
            f"a 10x input should stay near 10x (ceiling {_LINEAR_RATIO_CEILING}x); "
            "a quadratic rewrite would land near 100x"
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
        # quadratic scan past the trust boundary. Relative form (#1545): a
        # 1/10 slice of the SAME repetition keeps the adversarial shape, so
        # linear stays ≈10x while the unbounded form lands ≈100x.
        privacy.scan(blob[:1000])  # warm-up: pattern compile out of the timing
        small_elapsed = _min_scan_time(blob[: len(blob) // 10])
        full_elapsed = _min_scan_time(blob)
        ratio = full_elapsed / max(small_elapsed, 0.001)
        assert ratio < _LINEAR_RATIO_CEILING, (
            f"crafted {len(blob)}-char repetition scanned in {ratio:.1f}x its "
            f"{len(blob) // 10}-char slice "
            f"({small_elapsed * 1000:.1f} ms → {full_elapsed * 1000:.1f} ms) — "
            f"a 10x input should stay near 10x (ceiling {_LINEAR_RATIO_CEILING}x), "
            "indicating an O(N^2) backtracking regression in one of the #1488 "
            "bounded patterns"
        )
