"""Synthetic policy fixture: no network calls and no production endpoint."""

MAX_RETRIES = 5
BACKOFF_MS = 250
JITTER_ENABLED = True
LEGACY_CALLBACK = "/api/auth/legacy-callback"
ROLLOUT_FLAG = "AUTH_CALLBACK_V2_ENABLED"
