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

import logging

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
