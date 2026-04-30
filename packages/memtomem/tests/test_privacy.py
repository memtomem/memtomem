"""Privacy module — pattern surface, scan window, counter behavior.

These tests pin the parent-side trust boundary at the unit level. The
wire-in tests for ``mem_add`` / ``mem_batch_add`` live in
``test_memory_crud_redaction.py``.

Drift-prevention notes embedded as test pins:

- ``test_pattern_count_pinned_at_nine`` — the parent set is intentionally
  the secrets-only subset of STM's ``DEFAULT_PATTERNS`` (10 patterns), with
  the email/PII pattern excluded by design.
- ``test_clean_inputs_have_no_hit`` — direct contract pin: well-formed
  contact-note prose (emails, phone numbers, plain text) must pass
  through untouched. Future drift toward PII inclusion would break this
  immediately.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re

import pytest

from memtomem import privacy


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


class TestPatternSurface:
    def test_pattern_count_pinned_at_nine(self):
        assert len(privacy.DEFAULT_PATTERNS) == 9

    @pytest.mark.parametrize(
        "clean_input",
        [
            "user@example.com",
            "Email me at jane.doe+work@acme.io about the meeting.",
            "Call 555-123-4567 for the on-call rotation.",
            "Met with John today to discuss Q2 plans.",
            "The IPv4 address 192.168.1.1 is the gateway.",
            "Note: discussed deploys with team@acme.io and shipping@vendor.co",
        ],
    )
    def test_clean_inputs_have_no_hit(self, clean_input):
        hits = privacy.scan(clean_input)
        assert hits == [], f"Expected zero hits for clean input {clean_input!r}; got {hits!r}"


class TestScan:
    @pytest.mark.parametrize(
        "secret_sample",
        [
            "api_key: AKIAIOSFODNN7EXAMPL",
            "password = hunter2hunter2",
            "sk-" + "a" * 30,
            "github_pat_" + "x" * 30,
            "sk_live_" + "a" * 25,
            "npm_" + "a" * 30,
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIicm9vdA.SflKxwRJSMeK",
            "-----BEGIN RSA PRIVATE KEY-----",
        ],
    )
    def test_each_secret_pattern_hits(self, secret_sample):
        hits = privacy.scan(secret_sample)
        assert hits, f"Expected at least one hit for sample {secret_sample!r}"

    def test_clean_text_returns_empty_list(self):
        assert privacy.scan("Just a regular note about coffee.") == []

    def test_scan_window_capped_at_10k(self):
        # Secret beyond the scan window is invisible — parity with STM's
        # compression-side scanner (10K-char window).
        hidden = "a" * 10_001 + "AKIAIOSFODNN7EXAMPLE"
        assert privacy.scan(hidden) == []

    def test_secret_just_within_window_is_seen(self):
        visible = "a" * 9_900 + " AKIAIOSFODNN7EXAMPLE"
        assert privacy.scan(visible)

    def test_explicit_empty_pattern_set_returns_no_hit(self):
        assert privacy.scan("AKIAIOSFODNN7EXAMPLE", patterns=()) == []


class TestCounter:
    def test_record_increments_outcome_and_by_tool(self):
        privacy.record("blocked", "mem_add")
        privacy.record("blocked", "mem_add")
        privacy.record("pass", "mem_add")
        privacy.record("bypassed", "mem_batch_add")

        snap = privacy.snapshot()
        assert snap["outcomes"] == {"blocked": 2, "pass": 1, "bypassed": 1}
        assert snap["by_tool"]["mem_add"] == {"blocked": 2, "pass": 1, "bypassed": 0}
        assert snap["by_tool"]["mem_batch_add"] == {"blocked": 0, "pass": 0, "bypassed": 1}

    def test_record_unknown_outcome_is_dropped(self, caplog):
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            privacy.record("invalid_outcome", "mem_add")
        snap = privacy.snapshot()
        assert all(v == 0 for v in snap["outcomes"].values())
        assert "unknown outcome" in caplog.text

    def test_snapshot_is_deep_copy_safe(self):
        privacy.record("pass", "mem_add")
        snap = privacy.snapshot()
        snap["outcomes"]["pass"] = 999
        snap["by_tool"]["mem_add"]["pass"] = 999
        live = privacy.snapshot()
        assert live["outcomes"]["pass"] == 1
        assert live["by_tool"]["mem_add"]["pass"] == 1

    def test_reset_for_tests_clears_state(self):
        privacy.record("blocked", "mem_add")
        privacy.reset_for_tests()
        snap = privacy.snapshot()
        assert snap["outcomes"] == {"blocked": 0, "pass": 0, "bypassed": 0}
        assert snap["by_tool"] == {}


# ---------------------------------------------------------------------------
# JS-RegExp translator
#
# Background: the Web UI's compose-mode privacy warning needs to scan
# textarea content client-side using the same patterns the server
# enforces (#580). Python ``re`` and JS ``RegExp`` diverge on inline
# flag groups — ``new RegExp("(?i)foo")`` raises in JS — so the server
# translates patterns before serving them. These tests pin the
# translator's parity contract (Python re of translated body+flags must
# match the same fixtures as the original pattern) and lock the
# hard-reject set so future Python-only constructs can't slip through
# silently.
#
# Fixture-domain assumption: all positive/negative fixtures here are
# pure-ASCII. Word-boundary semantics (``\b``) align between Python and
# JS in the ASCII domain. A future pattern that depends on Unicode
# ``\b`` would need a different parity strategy than direct re vs JS
# comparison.
# ---------------------------------------------------------------------------


# Per-pattern positive + negative fixtures, paired by index with
# DEFAULT_PATTERNS. The positives are deliberately drawn from realistic
# secret shapes; the negatives are similar-looking strings that should
# NOT match (drift guard against a future translation accidentally
# broadening the pattern).
_PATTERN_FIXTURES: tuple[tuple[str, str], ...] = (
    # 0: api_key/secret_key/access_token (case-insensitive)
    ("API_KEY: abc123", "api keys are documented separately"),
    # 1: password/passwd/pwd
    ("Password = hunter2", "passport renewal next month"),
    # 2: sk-/ghp_/xox prefix
    ("token=sk-" + "a" * 30, "Sky color today is blue"),
    # 3: github_pat_
    ("github_pat_" + "x" * 30, "github_user joined the org"),
    # 4: stripe-style sk_live_, pk_test_, whsec_
    ("sk_live_" + "a" * 25, "sk_live_short"),
    # 5: npm_
    ("npm_" + "a" * 30, "npm install foo"),
    # 6: AWS access key id (AKIA/ASIA)
    ("AKIAIOSFODNN7EXAMPLE", "AKIA-no-good"),
    # 7: JWT
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIicm9vdA.SflKxwRJSMeK", "eyJ-not-a-jwt"),
    # 8: PEM private key header
    ("-----BEGIN RSA PRIVATE KEY-----", "RSA public key"),
)


class TestJsPatternTranslation:
    """Parity + hard-reject contract for ``privacy.to_js_pattern``.

    The parity test (§1.5) recovers the issue-580 test-plan item
    "client-side regex matches a known API-key fixture" without needing
    a JS runtime: feeding the translated body + lifted flags through
    Python ``re`` is equivalent to running the original pattern, so a
    successful Python match guarantees an identical JS match.
    """

    @pytest.mark.parametrize(
        "idx,positive,negative",
        [(i, pos, neg) for i, (pos, neg) in enumerate(_PATTERN_FIXTURES)],
    )
    def test_translated_pattern_matches_original(self, idx, positive, negative):
        original = privacy.DEFAULT_PATTERNS[idx]
        body, flags = privacy.to_js_pattern(original)

        original_re = re.compile(original)
        translated_re = re.compile(body, privacy.flags_str_to_re_flags(flags))

        # Positive fixture: same hits, same spans.
        orig_hits = [m.span() for m in original_re.finditer(positive)]
        trans_hits = [m.span() for m in translated_re.finditer(positive)]
        assert orig_hits == trans_hits, (
            f"Pattern {idx} hit-span parity broke on positive fixture {positive!r}: "
            f"original={orig_hits} translated={trans_hits}"
        )
        assert orig_hits, f"Pattern {idx} positive fixture {positive!r} did not hit"

        # Negative fixture: both reject identically.
        assert not original_re.search(negative), (
            f"Pattern {idx} positive-fixture mislabeled — negative {negative!r} actually hits"
        )
        assert not translated_re.search(negative)

    def test_module_constants_built_from_default_patterns(self):
        # JS_PATTERNS is computed once at import. Re-deriving it here
        # locks the pre-computation to the live translator.
        derived = tuple(
            {"pattern": body, "flags": flags}
            for body, flags in (privacy.to_js_pattern(p) for p in privacy.DEFAULT_PATTERNS)
        )
        assert privacy.JS_PATTERNS == derived

    def test_sha_locks_serialization_choice(self):
        # SHA must match canonical JSON serialization (sort_keys + tight
        # separators). Computed from the live JS_PATTERNS so adding a
        # 10th pattern only fails parity tests, not this one — this
        # test locks the *serialization*, not the pattern set.
        expected = hashlib.sha256(
            json.dumps(
                privacy.JS_PATTERNS,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        assert privacy.JS_PATTERNS_SHA == expected

    @pytest.mark.parametrize(
        "bad_pattern,construct",
        [
            # ``construct`` is a regex (pytest ``match=``); escape backslashes.
            (r"\Afoo", r"\\A or \\Z anchor"),
            (r"foo\Z", r"\\A or \\Z anchor"),
            ("foo(?i)bar", "mid-pattern inline flag group"),
            ("(?ix)foo", "verbose mode"),
            ("(?P<n>x)", "named group"),
            (r"(?#comment)x", r"inline comment \(\?#\.\.\.\)"),
            ("(?-i:x)", "inline flag negation"),
        ],
    )
    def test_hard_rejects_python_only_constructs(self, bad_pattern, construct):
        with pytest.raises(ValueError, match=construct):
            privacy.to_js_pattern(bad_pattern)

    def test_emitted_flags_are_jsregexp_compatible(self):
        # Each entry's flags is a (possibly empty) string of distinct
        # chars from the imsu subset (the only Python flags the
        # translator lifts; x is hard-rejected; g/y are JS-only and the
        # translator never emits them).
        allowed = set("imsu")
        for entry in privacy.JS_PATTERNS:
            flags = entry["flags"]
            assert len(flags) == len(set(flags)), (
                f"Duplicate flag chars in {flags!r} — JS rejects new RegExp(body, 'ii')"
            )
            assert set(flags) <= allowed, (
                f"Translator emitted unexpected flag in {flags!r}; allowed: {sorted(allowed)}"
            )
