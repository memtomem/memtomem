"""Privacy gate A — scope-aware refusal of force_unsafe (ADR-0011 §5).

The four-decision matrix:

    | scope            | force_unsafe | hits | decision               |
    |------------------|--------------|------|------------------------|
    | user             | False        | no   | pass                   |
    | user             | False        | yes  | blocked                |
    | user             | True         | yes  | bypassed               |
    | project_local    | True         | yes  | bypassed               |
    | project_shared   | False        | yes  | blocked                |
    | project_shared   | True         | yes  | blocked_project_shared |
    | project_shared   | True         | no   | pass                   |

The ``project_shared + force_unsafe + hits`` cell is the load-bearing
new behavior — it MUST hard-refuse rather than fall through to the
``bypassed`` branch. A mutate-validate pin at the bottom of the file
confirms the gate cannot regress to the pre-ADR-0011 behavior without
the test going red.
"""

from __future__ import annotations

import pytest

from memtomem import privacy

# A string the secrets-only ``DEFAULT_PATTERNS`` reliably hits.
SECRET = "api_key=AKIA1234567890ABCDEF"
CLEAN = "this is just regular prose, no secrets here"


@pytest.fixture(autouse=True)
def _reset_counters():
    privacy.reset_for_tests()
    yield
    privacy.reset_for_tests()


class TestUserScopeUnchanged:
    """Default scope ('user') must preserve pre-ADR-0011 behavior."""

    def test_clean_pass(self):
        r = privacy.enforce_write_guard(CLEAN, surface="t", scope="user")
        assert r.decision == "pass"

    def test_hit_no_force_blocked(self):
        r = privacy.enforce_write_guard(SECRET, surface="t", scope="user")
        assert r.decision == "blocked"

    def test_hit_force_bypassed(self):
        r = privacy.enforce_write_guard(SECRET, surface="t", scope="user", force_unsafe=True)
        assert r.decision == "bypassed"

    def test_default_scope_is_user(self):
        # The new ``scope`` kwarg defaults to "user" so existing callers
        # keep working without code changes — verify pin.
        r = privacy.enforce_write_guard(SECRET, surface="t", force_unsafe=True)
        assert r.decision == "bypassed"


class TestProjectLocalAllowsBypass:
    """project_local is gitignored — same bypass rules as user."""

    def test_hit_force_bypassed(self):
        r = privacy.enforce_write_guard(
            SECRET, surface="t", scope="project_local", force_unsafe=True
        )
        assert r.decision == "bypassed"

    def test_hit_no_force_blocked(self):
        r = privacy.enforce_write_guard(SECRET, surface="t", scope="project_local")
        assert r.decision == "blocked"


class TestProjectSharedHardRefusal:
    """project_shared MUST refuse force_unsafe — git history is forever."""

    def test_hit_force_blocked_project_shared(self):
        r = privacy.enforce_write_guard(
            SECRET, surface="t", scope="project_shared", force_unsafe=True
        )
        assert r.decision == "blocked_project_shared"

    def test_hit_no_force_blocked(self):
        # Without force_unsafe, the regular ``blocked`` outcome fires —
        # the project_shared refusal only kicks in when bypass was
        # *attempted*. Counter on the no-force path stays attributed to
        # ``blocked`` for monitoring continuity.
        r = privacy.enforce_write_guard(SECRET, surface="t", scope="project_shared")
        assert r.decision == "blocked"

    def test_clean_pass(self):
        # Scope doesn't matter when there's nothing to redact.
        r = privacy.enforce_write_guard(CLEAN, surface="t", scope="project_shared")
        assert r.decision == "pass"

    def test_counter_records_blocked_project_shared(self):
        privacy.enforce_write_guard(SECRET, surface="t1", scope="project_shared", force_unsafe=True)
        snap = privacy.snapshot()
        assert snap["outcomes"]["blocked_project_shared"] == 1
        # Must NOT increment ``bypassed`` — that would mask the refusal
        # in operational dashboards.
        assert snap["outcomes"]["bypassed"] == 0


class TestAuditLogMarker:
    """Bypass audit log line carries a ``blocked_scope`` marker."""

    def test_audit_log_contains_blocked_scope(self, caplog):
        with caplog.at_level("WARNING", logger="memtomem.privacy"):
            privacy.enforce_write_guard(
                SECRET,
                surface="t",
                scope="project_shared",
                force_unsafe=True,
            )
        msgs = [r.message for r in caplog.records]
        assert any("blocked_scope='project_shared'" in m for m in msgs), msgs


class TestValidOutcomes:
    """The new outcome must be in the registered set."""

    def test_blocked_project_shared_in_valid_outcomes(self):
        assert "blocked_project_shared" in privacy._VALID_OUTCOMES

    def test_record_accepts_new_outcome(self):
        # ``record()`` rejects unknown outcomes with a logger warning;
        # this verifies the new label is plumbed through.
        privacy.record("blocked_project_shared", "tool_x")
        snap = privacy.snapshot()
        assert snap["by_tool"]["tool_x"]["blocked_project_shared"] == 1


class TestMutateValidatePin:
    """Mutate-validate-pin: simulate the gate flipped off → test must red.

    Reasoning per ``feedback_pin_test_mutation_validation.md``: a pin
    test that cannot detect production code reverting to the broken
    state silently gives a false-PASS. We can't actually mutate the
    helper here, but we CAN assert that the load-bearing branch is
    distinguishable from the regular ``bypassed`` branch — if a future
    refactor drops the project_shared check and falls through to
    ``bypassed`` again, this distinguishability check fails.
    """

    def test_project_shared_force_decision_is_distinct(self):
        # The gate's output decision must differ from the pre-ADR-0011
        # ``bypassed`` value. If they collapse, the gate is gone.
        r_shared = privacy.enforce_write_guard(
            SECRET, surface="t", scope="project_shared", force_unsafe=True
        )
        r_user = privacy.enforce_write_guard(SECRET, surface="t", scope="user", force_unsafe=True)
        assert r_shared.decision != r_user.decision
        assert r_shared.decision == "blocked_project_shared"
        assert r_user.decision == "bypassed"
