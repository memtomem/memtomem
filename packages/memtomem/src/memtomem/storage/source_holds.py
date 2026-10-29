"""Source-hold reasons that change how a hold may be lifted.

Most reasons are availability observations (missing, unavailable, unresolved)
that a later observation may replace or lift. Redaction is about the file's
content, and a relink about which file the path names, so only a clean index
pass of the path lifts either. The absence reasons are the ones a retained dir
lifts without any index pass.
"""

# The redaction guard refused the file's current content (#2629).
REDACTION_BLOCKED_HOLD = "redaction_blocked"

# The path is still there but is now a symlink, so it no longer names the file
# whose content is stored under its key (#2878).
RELINKED_HOLD = "source_relinked"

# Holds that a present file does not lift: what the path presents is the
# reason it is held. ``release_source_hold(validated=False)`` keeps them.
PRESENCE_KEPT_HOLD_REASONS = frozenset({REDACTION_BLOCKED_HOLD, RELINKED_HOLD})

# Holds that record only that the file is gone. Under ``retain_missing_dirs``
# these are lifted, because the chunks are the archive the user asked to keep
# (#2833). ``watch_reindex_unresolved`` is not one: the watcher also writes it
# for excluded, unsupported and failed reindexes.
ABSENCE_HOLD_REASONS = frozenset({"source_missing", "scan_missing"})
