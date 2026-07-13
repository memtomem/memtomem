1. Call `mem_status` and treat the default `provider=none` BM25-only configuration as healthy.
2. Obtain an explicit notes or memory directory from the request; ask for one when absent.
3. Call `mem_index` on that path with `force=false` and `auto_tag=false`.
4. Choose a representative phrase from the indexed material and call `mem_search` to verify retrieval.
5. Report the first-success path and mention embeddings only as an optional relevance enhancement.

Do not install Ollama, enable automation hooks, or edit host instruction files unless the user separately requests those actions.
