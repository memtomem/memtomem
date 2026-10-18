"""Source-hold reasons that change how a hold may be lifted.

Every other reason is an availability observation (missing, unavailable,
unresolved) that a later observation may replace or lift. This one is about
the file's content, so only a clean index pass of that content lifts it.
"""

# The redaction guard refused the file's current content (#2629).
REDACTION_BLOCKED_HOLD = "redaction_blocked"
