# ADR: preserve the legacy callback during staged rollout

This is synthetic learning data, not a customer story.

Decision: keep /api/auth/legacy-callback until the staged rollout is reviewed.
Reason: older clients still use that path.
Rollback flag: AUTH_CALLBACK_V2_ENABLED.
Retry policy: retry at most 5 times with 250 ms backoff and jitter.
Verification: run python -m unittest discover -s . -p test_policy.py.

Search phrases used in this fixture are deliberately lexical:
"legacy callback", "Retry policy", and "AUTH_CALLBACK_V2_ENABLED".
