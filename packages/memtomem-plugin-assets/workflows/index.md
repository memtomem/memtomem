Require an explicit file or directory path before calling `mem_index`; never rely on its `.` default. Resolve ambiguity with the user before indexing a broad directory.

Use `force=false` and `auto_tag=false` unless the user explicitly requests otherwise. Report scanned, indexed, skipped, deleted, and blocked counts. Explain redaction or embedding-mismatch failures without bypassing them automatically.
