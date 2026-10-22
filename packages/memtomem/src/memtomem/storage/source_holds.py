"""Source-hold reasons that change how a hold may be lifted.

Most reasons are availability observations (missing, unavailable, unresolved)
that a later observation may replace or lift. Redaction is about the file's
content, so only a clean index pass of that content lifts it. The absence
reasons are the ones a retained dir lifts without any index pass.
"""

# The redaction guard refused the file's current content (#2629).
REDACTION_BLOCKED_HOLD = "redaction_blocked"

# Holds that record only that the file is gone. Under ``retain_missing_dirs``
# these are lifted, because the chunks are the archive the user asked to keep
# (#2833). ``watch_reindex_unresolved`` is not one: the watcher also writes it
# for excluded, unsupported and failed reindexes.
ABSENCE_HOLD_REASONS = frozenset({"source_missing", "scan_missing"})
