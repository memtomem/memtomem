"""Privacy module — pattern surface, scan window, counter behavior.

These tests pin the parent-side trust boundary at the unit level. The
wire-in tests for ``mem_add`` / ``mem_batch_add`` live in
``test_memory_crud_redaction.py``.

Drift-prevention notes embedded as test pins:

- ``test_pattern_count_pinned`` — the parent set is the STM-synced
  secrets-only subset (email/PII excluded by design) plus the LTM-origin
  secret-class additions of #1488; the exact count is pinned so a silent
  add/drop fails loudly.
- ``test_fixtures_cover_every_pattern`` — every DEFAULT_PATTERNS entry has
  a paired positive/negative JS-translation fixture, so a new pattern
  cannot be added without a parity fixture.
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
    def test_pattern_count_pinned(self):
        # 12 STM-synced secret-class patterns (incl. the memtomem-stm#553
        # AWS-label, #562 quoted-label, and #561 x-amz-security-token
        # forward-syncs) + 7 LTM-origin additions (#1488). Bump
        # deliberately when adding a pattern so a silent add/drop surfaces
        # here.
        assert len(privacy.DEFAULT_PATTERNS) == 19

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
            # Quoted-JSON label (memtomem-stm#562 forward-sync) — the closing
            # quote blocks the unquoted rules' [:=].
            '"password": "hunter2"',
            # AWS wire label (memtomem-stm#561 forward-sync) — header line as
            # botocore DEBUG logs emit it (value is FAKE).
            "x-amz-security-token: FAKEFwoGZXIvYXdzFAKE",
            "sk-" + "a" * 30,
            "github_pat_" + "x" * 30,
            "sk_live_" + "a" * 25,
            "npm_" + "a" * 30,
            "AKIAIOSFODNN7EXAMPLE",
            "eyJhbGciOiJIUzI1NiJ9.eyJzdWIicm9vdA.SflKxwRJSMeK",
            "-----BEGIN RSA PRIVATE KEY-----",
            # --- #1488 LTM-origin additions (all examples are FAKE) -----
            # Modern OpenAI key: legacy ``sk-`` rule misses these (hyphen
            # after "proj" halts the run) — must hit via the T3BlbkFJ rule.
            "sk-proj-FAKE0aaaa1111bbbb2222cccc3333T3Blbk" + "FJEXAMPLE0dddd4444eeee5555ffff6666",
            # Anthropic key (FAKE) — exact 93-char body + AA terminal.
            "sk-ant-api03-" + ("FAKEfake0123456789" * 6)[:93] + "AA",
            # GitHub server-to-server token (gh*_ family, FAKE).
            "ghs_FAKEfake0123456789FAKEfake0123456789",
            # Google API key, AIza + 35 (FAKE).
            "AIzaSy0_-BcdSy0_-BcdSy0_-BcdSy0_-BcdSy0",
            # GitLab PAT (FAKE) — exact 20-char body.
            "glpat-" + ("FAKEfake0123456789" * 2)[:20],
            # Hugging Face token, hf_ + 34 (FAKE).
            "hf" + "_FAKEfake0123456789FAKEfake01234567",
            # PyPI upload token, macaroon header (FAKE).
            "pypi-AgEIcHlwaS5vcmcFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",
        ],
    )
    def test_each_secret_pattern_hits(self, secret_sample):
        hits = privacy.scan(secret_sample)
        assert hits, f"Expected at least one hit for sample {secret_sample!r}"

    def test_clean_text_returns_empty_list(self):
        assert privacy.scan("Just a regular note about coffee.") == []

    def test_secret_within_first_10k_is_seen(self):
        # Baseline: a secret near the old 10 K boundary still hits. Pin
        # both edges of the former cap so a future re-introduction of a
        # truncation can't hide a regression behind "the test only used
        # short inputs."
        within = "a" * 9_900 + "sk-" + "a" * 30
        assert privacy.scan(within)

    def test_secret_past_former_10k_window_is_seen(self):
        # Pin-and-invert (memory: pin_invert_symmetric_assertion): the
        # earlier revision truncated at 10K chars and asserted ``== []``
        # for a secret past that mark. ``scan()`` now covers the entire
        # input, so the same shape must hit. A regression that re-adds
        # the cap fails this assertion loudly.
        #
        # Uses the ``sk-`` prefix shape (the sk-/ghp_/xox rule)
        # rather than the AWS AKIA shape — the latter is anchored with
        # ``\b`` and would silently no-match against a leading run of
        # word chars even when the scan does cover the position, which
        # would conflate two distinct regression modes.
        past = "a" * 10_001 + "sk-" + "a" * 30
        assert privacy.scan(past), (
            "scan() must cover the entire input — secrets past the former "
            "10K window must not silently bypass the trust boundary"
        )

    def test_explicit_empty_pattern_set_returns_no_hit(self):
        assert privacy.scan("AKIAIOSFODNN7EXAMPLE", patterns=()) == []


class TestEnforceWriteGuard:
    """Helper unit tests. The wire-in tests for individual surfaces
    (``mem_add`` / ``mem_edit`` / Web routes / CLI / LangGraph) live in
    ``test_memory_crud_redaction.py`` and ``test_redaction_write_surfaces.py``.
    The unit-level pin here measures the helper's contract directly so a
    surface-level regression cannot mask a helper-level bug.
    """

    def test_clean_content_records_pass_and_returns_empty_hits(self):
        before = privacy.snapshot()["outcomes"]["pass"]
        result = privacy.enforce_write_guard(
            "Just a normal note about Q2 plans.", surface="unit_pass"
        )
        after = privacy.snapshot()["outcomes"]["pass"]

        assert result.decision == "pass"
        assert result.hits == []
        assert after == before + 1

    def test_secret_without_force_unsafe_records_blocked(self):
        before = privacy.snapshot()["outcomes"]["blocked"]
        result = privacy.enforce_write_guard("secret = sk-" + "a" * 30, surface="unit_block")
        after = privacy.snapshot()["outcomes"]["blocked"]

        assert result.decision == "blocked"
        assert result.hits, "blocked decision must surface hit count to caller"
        assert after == before + 1

    def test_secret_with_force_unsafe_records_bypassed_and_audit_logs(self, caplog):
        before = privacy.snapshot()["outcomes"]["bypassed"]
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            result = privacy.enforce_write_guard(
                "secret = sk-" + "a" * 30,
                surface="unit_bypass",
                force_unsafe=True,
                audit_context={"namespace": "default", "file": "notes.md"},
            )
        after = privacy.snapshot()["outcomes"]["bypassed"]

        assert result.decision == "bypassed"
        assert result.hits
        assert after == before + 1
        assert "redaction bypass" in caplog.text
        assert "surface=unit_bypass" in caplog.text
        # audit_context fields appear in the log line so operators can
        # correlate, but the secret bytes themselves never do.
        assert "namespace='default'" in caplog.text
        assert "file='notes.md'" in caplog.text
        assert "sk-" not in caplog.text, "Matched bytes must never reach the audit log"

    def test_clean_content_with_force_unsafe_records_pass_not_bypassed(self):
        """``bypassed`` only fires on a real hit. A clean write with the
        flag set is no different from a clean write without it — pin so
        the bypass label keeps measuring real escape-hatch usage rather
        than degrading into "kwarg was passed."
        """
        before = privacy.snapshot()["outcomes"]
        result = privacy.enforce_write_guard(
            "Plain prose, nothing sensitive.",
            surface="unit_clean_force",
            force_unsafe=True,
        )
        after = privacy.snapshot()["outcomes"]

        assert result.decision == "pass"
        assert after["pass"] == before["pass"] + 1
        assert after["bypassed"] == before["bypassed"]

    def test_secret_in_audit_context_value_is_redacted(self, caplog):
        """User-controllable audit-context fields (file path, upload
        filename, scratch key) can themselves embed the same secret
        that's being bypassed. The helper must scrub those values
        before they reach the audit log — otherwise the "matched bytes
        never reach logs" goal is undermined exactly when a bypass
        gets recorded.
        """
        secret = "sk-" + "a" * 30
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            privacy.enforce_write_guard(
                f"the body contains {secret}",
                surface="unit_ctx_redact",
                force_unsafe=True,
                audit_context={
                    "file": f"/tmp/notes-{secret}.md",
                    "namespace": "default",
                    "filename": f"{secret}.md",
                    "item_idx": 3,
                },
            )
        line = next(
            (r.getMessage() for r in caplog.records if "redaction bypass" in r.getMessage()),
            "",
        )
        assert secret not in line, f"Audit log leaked the matched bytes: {line!r}"
        # The redaction marker takes the value's place so operators see
        # the field was scrubbed rather than missing.
        assert "<redacted: secret-shape>" in line
        # Non-secret context fields and non-string values pass through.
        assert "namespace='default'" in line
        assert "item_idx=3" in line

    def test_long_audit_context_string_is_truncated(self, caplog):
        long_clean = "x" * 5000
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            privacy.enforce_write_guard(
                "secret = sk-" + "a" * 30,
                surface="unit_ctx_truncate",
                force_unsafe=True,
                audit_context={"path": long_clean},
            )
        line = next(
            (r.getMessage() for r in caplog.records if "redaction bypass" in r.getMessage()),
            "",
        )
        assert "...(truncated)" in line
        # Truncation cap keeps the line compact even with abusive context.
        assert len(line) < 1000

    def test_audit_log_without_context_still_records_shape_metadata(self, caplog):
        """An ``audit_context=None`` bypass still emits the structured
        ``surface=…, content_chars=…, hits=…`` triple. The
        ``audit_context`` block is optional sugar for downstream
        forensics; the core shape is always present.
        """
        with caplog.at_level(logging.WARNING, logger="memtomem.privacy"):
            privacy.enforce_write_guard(
                "secret = sk-" + "a" * 30,
                surface="unit_no_ctx",
                force_unsafe=True,
            )
        line = next(
            (r.getMessage() for r in caplog.records if "redaction bypass" in r.getMessage()),
            "",
        )
        assert "surface=unit_no_ctx" in line
        assert "content_chars=" in line
        assert "hits=" in line
        # No double-comma artefact from an empty context block.
        assert ", , " not in line
        # Matched bytes never leak into the log line.
        assert "sk-" not in line


class TestCounter:
    def test_record_increments_outcome_and_by_tool(self):
        privacy.record("blocked", "mem_add")
        privacy.record("blocked", "mem_add")
        privacy.record("pass", "mem_add")
        privacy.record("bypassed", "mem_batch_add")

        snap = privacy.snapshot()
        # ``blocked_project_shared`` (ADR-0011) is exposed as 0 here —
        # the outcome dict is keyed off ``_VALID_OUTCOMES`` and every
        # known label appears, even unused ones, so dashboards show a
        # stable schema.
        assert snap["outcomes"] == {
            "blocked": 2,
            "pass": 1,
            "bypassed": 1,
            "blocked_project_shared": 0,
        }
        assert snap["by_tool"]["mem_add"] == {
            "blocked": 2,
            "pass": 1,
            "bypassed": 0,
            "blocked_project_shared": 0,
        }
        assert snap["by_tool"]["mem_batch_add"] == {
            "blocked": 0,
            "pass": 0,
            "bypassed": 1,
            "blocked_project_shared": 0,
        }

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
        assert snap["outcomes"] == {
            "blocked": 0,
            "pass": 0,
            "bypassed": 0,
            "blocked_project_shared": 0,
        }
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
    # 2: general quoted-JSON label (memtomem-stm#562 forward-sync).
    #    Negative: the pwd exclusion — a working-directory field must not
    #    classify as credential-bearing.
    ('"password": "hunter2"', '"pwd": "/home/user"'),
    # 3: AWS secret-material label (memtomem-stm#553 forward-sync).
    #    Negative: an identifier that embeds the label — the unquoted
    #    branch's left boundary must reject it.
    ('"SessionToken": "FAKEFwoGZXIvYXdzFAKE"', "supports_session_token: true"),
    # 4: x-amz-security-token wire label (memtomem-stm#561 forward-sync).
    #    Negative: a kebab compound that embeds the label — the
    #    separator-only left boundary must reject it.
    ("x-amz-security-token: FAKEFwoGZXIvYXdzFAKE", "forward-x-amz-security-token: true"),
    # 5: sk-/ghp_/xox prefix
    ("token=sk-" + "a" * 30, "Sky color today is blue"),
    # 6: github_pat_
    ("github_pat_" + "x" * 30, "github_user joined the org"),
    # 7: stripe-style sk_live_, pk_test_, whsec_
    ("sk_live_" + "a" * 25, "sk_live_short"),
    # 8: npm_
    ("npm_" + "a" * 30, "npm install foo"),
    # 9: AWS access key id (AKIA/ASIA)
    ("AKIAIOSFODNN7EXAMPLE", "AKIA-no-good"),
    # 10: JWT
    ("eyJhbGciOiJIUzI1NiJ9.eyJzdWIicm9vdA.SflKxwRJSMeK", "eyJ-not-a-jwt"),
    # 11: PEM private key header
    ("-----BEGIN RSA PRIVATE KEY-----", "RSA public key"),
    # 12: OpenAI modern (sk-proj-/svcacct-/admin-, T3BlbkFJ-anchored).
    #     Negative: a kebab slug with the prefix but no marker.
    (
        "sk-proj-FAKE0aaaa1111bbbb2222cccc3333T3Blbk" + "FJEXAMPLE0dddd4444eeee5555ffff6666",
        "sk-project-management-tool",
    ),
    # 13: Anthropic (sk-ant-NN-, exact 93-char body + AA). Negative: a
    #     digit-bearing kebab slug carrying the infix (a loose body matched
    #     it; the exact length + AA terminal rejects it).
    (
        "sk-ant-api03-" + ("FAKEfake0123456789" * 6)[:93] + "AA",
        "sk-ant-api03-release-notes-2026-migration-guide",
    ),
    # 14: GitHub gh*_ family completion. Negative: an English word that
    #     starts "gho" but is not a gho_ token.
    ("ghs_FAKEfake0123456789FAKEfake0123456789", "ghost_writer_mode_enabled"),
    # 15: Google API key (AIza + exactly 35). Negative: too short.
    ("AIzaSy0_-BcdSy0_-BcdSy0_-BcdSy0_-BcdSy0", "AIzaShortKey"),
    # 16: GitLab PAT (glpat-, exact 20-char body). Negative: a digit-bearing
    #     "glpat-" kebab slug (a {20,} superset matched it; exact length
    #     rejects it).
    ("glpat-" + ("FAKEfake0123456789" * 2)[:20], "glpat-form-builder-component-name-2026"),
    # 17: Hugging Face (hf_ + exactly 34, digit-guarded). Negative: a
    #     34-char letter-only run (no digit) directly after hf_.
    ("hf" + "_FAKEfake0123456789FAKEfake01234567", "hf" + "_abcdefghijklmnopqrstuvwxyzabcdefgh"),
    # 18: PyPI / TestPyPI macaroon token. Negative: the prefix without the
    #     fixed base64 header.
    (
        "pypi-AgEIcHlwaS5vcmcFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",
        "pypi-package-name-here",
    ),
)


class TestJsPatternTranslation:
    """Parity + hard-reject contract for ``privacy.to_js_pattern``.

    The parity test (§1.5) recovers the issue-580 test-plan item
    "client-side regex matches a known API-key fixture" without needing
    a JS runtime: feeding the translated body + lifted flags through
    Python ``re`` is equivalent to running the original pattern, so a
    successful Python match guarantees an identical JS match.
    """

    def test_fixtures_cover_every_pattern(self):
        # Every DEFAULT_PATTERNS entry must have a paired positive/negative
        # fixture so the parity test below covers the whole set — a new
        # pattern cannot be added without its parity fixture (and a
        # contributor who appends a pattern but forgets the fixture fails
        # here rather than shipping an untranslated/untested entry).
        assert len(_PATTERN_FIXTURES) == len(privacy.DEFAULT_PATTERNS)

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
            # Odd-length backslash run before ``A``/``Z``: the leading ``\\``
            # is a literal-backslash pair, the final ``\`` actively escapes
            # the next char. Real anchor — must still raise. Symmetric pair.
            (r"\\\Afoo", r"\\A or \\Z anchor"),
            (r"foo\\\Z", r"\\A or \\Z anchor"),
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

    @pytest.mark.parametrize(
        "pat",
        [
            r"foo\\Abar",  # run of 2: literal ``\`` + literal ``A``, no anchor
            r"foo\\Zbar",  # run of 2: literal ``\`` + literal ``Z``, no anchor
            r"foo\\\\Abar",  # run of 4: two literal-``\`` pairs + literal ``A``
        ],
    )
    def test_accepts_escaped_anchor_literals(self, pat):
        # Detector must distinguish ``\A``/``\Z`` (Python anchor) from
        # ``\\A``/``\\Z`` (escaped backslash + literal char). Issue #594.
        body, flags = privacy.to_js_pattern(pat)
        assert body == pat
        assert flags == ""

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


# ---------------------------------------------------------------------------
# #1488 — LTM-origin secret-class additions: per-pattern positive coverage +
# near-miss false-positive guards. Every token example is FAKE (embedded
# FAKE/EXAMPLE markers, never a live credential) but structurally correct
# against the provider format. The near-misses lock the FP-defense
# mechanisms (the T3BlbkFJ OpenAI marker, the digit-in-body lookaheads, and
# the exact-length anchors) so a future loosening of any pattern fails loudly.
# ---------------------------------------------------------------------------

_NEW_PATTERN_POSITIVES = [
    # OpenAI modern (T3BlbkFJ-anchored): proj / svcacct / admin.
    "sk-proj-FAKE0aaaa1111bbbb2222cccc3333T3Blbk" + "FJEXAMPLE0dddd4444eeee5555ffff6666",
    "sk-svcacct-FAKE_svc-0123456789aaaaaaaaaaaaaaaaaT3Blbk"
    + "FJEXAMPLE_only-9876543210bbbbbbbbbbbbbbbbb",
    "sk-admin-FAKE0not0real0123456789aaaaaaaaaaaaaaaaaaaaT3Blbk"
    + "FJEXAMPLE0only0987654321bbbbbbbbbbbbbbbbbbbb",
    # Anthropic: api03 / admin01 (exact 93-char body + AA terminal). oat01
    # is intentionally NOT covered (no canonical shape — see privacy.py).
    "sk-ant-api03-" + ("FAKEfake0123456789" * 6)[:93] + "AA",
    "sk-ant-admin01-" + ("FAKEfake0123456789" * 6)[:93] + "AA",
    # GitHub gh*_ family (gho / ghu / ghs / ghr), each 36-char base62 body.
    "gho_FAKEfake0123456789FAKEfake0123456789",
    "ghu_FAKEfake0123456789FAKEfake0123456789",
    "ghs_FAKEfake0123456789FAKEfake0123456789",
    "ghr_FAKEfake0123456789FAKEfake0123456789",
    # Google API key: AIza + exactly 35.
    "AIzaSy0_-BcdSy0_-BcdSy0_-BcdSy0_-BcdSy0",
    # GitLab PAT (exact 20-char body).
    "glpat-" + ("FAKEfake0123456789" * 2)[:20],
    # Hugging Face: hf_ + exactly 34.
    "hf" + "_FAKEfake0123456789FAKEfake01234567",
    # PyPI (prod) + TestPyPI (test) macaroon tokens.
    "pypi-AgEIcHlwaS5vcmcFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",
    "pypi-AgENdGVzdC5weXBpLm9yZwFAKEfake0123456789FAKEfake0123456789FAKEfake0123456789",
]

_NEW_PATTERN_NEAR_MISSES = [
    # OpenAI: the sk-<class>- prefix but no T3BlbkFJ marker (kebab slug).
    "sk-proj-some-feature-branch-name-without-any-marker-here",
    "sk-admin-dashboard-component-wrapper-container-element",
    # OpenAI: the marker present but no sk-<class>- prefix.
    "a sentence that happens to mention T3BlbkFJ as base64 of OpenAI here",
    # OpenAI: marker present but the post-marker segment runs past the {200}
    # cap into more token chars — the trailing terminal guard rejects the
    # over-long run (a bare capped greedy matched its first 200 chars).
    "sk-proj-" + "a" * 20 + "T3BlbkFJ" + "b" * 200 + "-followup-doc",
    # Anthropic: infix shape but not the exact 93-char + AA token body
    # (kebab slugs, with and without digits — the digit-bearing one is the
    # case a digit-guard alone would have let through).
    "sk-ant-colony-simulation-readme-design-notes",
    "sk-ant-api03-readme-design-notes-only-letters-here",
    "sk-ant-api03-release-notes-2026-migration-guide",
    # Anthropic: a full 93-char body ending in the "AA" marker but followed
    # by a token char (-). A bare \b terminal would accept "AA-"; the
    # (?![A-Za-z0-9_-]) terminal rejects it.
    "sk-ant-api03-" + "x" * 93 + "AA-followup-doc",
    # Anthropic: 'api' not followed by two digits.
    "sk-ant-api-gateway-architecture-doc",
    # GitHub: a word starting with the prefix letters, not a token.
    "ghost_writer_mode_enabled_for_the_editor",
    "the ghs_ prefix is mentioned but not a token",
    "gho_tooShort",
    # Google: too short, and prefix inside an identifier (no word boundary).
    "AIzaShort",
    "MosaicAIzaPrefixedInsideAnIdentifierWordBoundaryFails",
    # GitLab: 'glpat-' kebab slugs (with and without digits) and 'glpattern'
    # (no hyphen) — none is the exact 20-char token body. The digit-bearing
    # slugs are the cases a digit-guard alone would have let through.
    "glpat-form-builder-component-name-no-digits",
    "glpat-form-builder-component-name-2026",
    "glpat-release-2026-secret-scanner-doc",
    "glpattern-matching-utility-helper-module",
    # Hugging Face: identifier (underscore breaks the run) + a 34-letter
    # no-digit run directly after hf_.
    "hf_hidden_states_from_the_transformer",
    "hf" + "_abcdefghijklmnopqrstuvwxyzabcdefgh",
    # PyPI: the prefix without the fixed macaroon header.
    "pypi-package-upload-instructions-doc",
    "pypi-AgSomethingElseEntirelyNotTheRealHeader1234567890abcdefghij",
]


class TestNewSecretPatterns1488:
    @pytest.mark.parametrize("sample", _NEW_PATTERN_POSITIVES)
    def test_new_pattern_positive_hits(self, sample):
        assert privacy.scan(sample), f"FAKE token shape must be redacted: {sample[:40]!r}"

    @pytest.mark.parametrize("text", _NEW_PATTERN_NEAR_MISSES)
    def test_new_pattern_near_miss_does_not_hit(self, text):
        # The contract is "no false positive from the #1488 additions"
        # (indices >= 12 since the memtomem-stm#553/#562/#561 forward-syncs
        # inserted at indices 2-4). A near-miss may legitimately stay clean
        # of the earlier 0-11 rules too, but those are pinned elsewhere;
        # here we isolate the new set so a future loosening surfaces
        # precisely.
        offending = [h.pattern_index for h in privacy.scan(text) if h.pattern_index >= 12]
        assert not offending, f"#1488 pattern(s) {offending} false-positived on {text[:50]!r}"

    def test_ghp_still_covered_by_legacy_not_duplicated(self):
        # ghp_ stays the legacy sk-/ghp_/xox rule's job (index 5 since the
        # memtomem-stm#553/#562/#561 forward-syncs inserted at indices 2-4);
        # the new gh[ousr]_ pattern (index 14) deliberately excludes 'p' to
        # avoid a duplicate hit. Locks the dedup intent + guards against the
        # change accidentally dropping ghp_ coverage.
        ghp = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"  # ghp_ + 36 base62
        idxs = {h.pattern_index for h in privacy.scan(ghp)}
        assert 5 in idxs, "ghp_ must still be caught by the legacy sk-/ghp_/xox rule"
        assert 14 not in idxs, "gh[ousr]_ (index 14) must exclude 'p' — no duplication"
