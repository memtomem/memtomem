Call `mem_status` once. Report the storage backend and database path, embedding state, source and chunk counts, dense-vector coverage, and warnings that the tool actually returns.

Treat `provider=none` and BM25-only coverage as a supported default, not a failed setup. Do not claim namespace totals or other fields absent from the response.
