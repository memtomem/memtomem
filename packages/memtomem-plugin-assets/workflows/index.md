<!-- scope:project -->
Require an explicit file or directory path before calling `mem_index`; never rely on its `.` default. Resolve ambiguity with the user before indexing a broad directory.
<!-- /scope -->
<!-- scope:user -->
Require an explicit absolute file or directory path (or one starting with `~`) before calling `mem_index`; never rely on its `.` default and never pass a relative path — this server's working directory is the plugin directory, not the user's. Resolve ambiguity with the user before indexing a broad directory.
<!-- /scope -->

Use `force=false` and `auto_tag=false` unless the user explicitly requests otherwise. Report scanned, indexed, skipped, deleted, and blocked counts. Explain redaction or embedding-mismatch failures without bypassing them automatically.
