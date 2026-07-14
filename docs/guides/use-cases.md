# memtomem 0.3.11 use cases

These runnable scenarios show where the current release is useful. Start with
`mm init` and `mm status`, and use personal or synthetic data for evaluation.

## Multi-tool developers

### 1. Carry a decision from Claude Code to Codex

Save a durable decision in one tool and retrieve it from another:

```bash
mm add "Use blue-green deployment and run a smoke test before cutover" \
  --tags deployment,decision
mm search "deployment cutover strategy"
```

Configure both clients to use the same memtomem MCP server. Success means both
return the same source-backed decision without copying chat transcripts.

### 2. Synchronize one skill across runtimes

Keep a canonical skill in the Context Gateway, then preview and apply runtime
projections:

```bash
mm context diff --include skills
mm context sync --include skills
```

This is useful when Claude Code, Codex, and other supported runtimes need the
same maintained instructions without separate manual edits.

### 3. Add proactive surfacing only when needed

Use memtomem alone for explicit durable search. Add the separate
`memtomem-stm` proxy only when a workflow needs proactive surfacing,
compression, or caching. This keeps the canonical store independent from the
optional short-term-memory layer.

## Individual knowledge bases

### 4. Search Markdown notes and source code together

Register a directory containing Markdown, Python, JavaScript, JSON, YAML, or
TOML, then index and search it:

```bash
mm index ~/knowledge
mm search "where is retry backoff configured"
```

Results retain source paths and chunk boundaries, so an answer can be checked
against the original file.

### 5. Combine Korean exact terms with semantic intent

Test a query that contains a Korean product term and a paraphrased intent:

```bash
mm search "배포 전 확인해야 하는 복구 절차"
```

Hybrid BM25, dense retrieval, and reciprocal-rank fusion are most visible when
the query needs both an exact identifier and a meaning-level match.

### 6. Re-index only what changed

Index a directory, edit one paragraph, and index it again. Chunk hashes allow
unchanged content to retain its embeddings while only changed chunks are
updated. `mm status` provides the before-and-after operational check.

## Teams and projects

### 7. Review shared project memory in Git

Place durable shared memory in the project-shared scope. Teammates can review
normal Markdown diffs, while project-local and user scopes remain outside the
shared history. Writes to the shared scope require explicit confirmation and
cannot bypass privacy findings.

### 8. Keep local and shared context separate

Use the three scope tiers deliberately:

- `user` for cross-project personal context;
- `project_local` for machine- or workspace-specific context;
- `project_shared` for reviewed, Git-tracked team context.

Pinned Context applies the same scope hierarchy and can add agent-specific
blocks without changing the general project instructions.

### 9. Propose memories before writing them

Scan one exact session and review candidates before any durable write:

```bash
mm review scan SESSION_ID
mm review list
mm review approve CANDIDATE_ID --reviewer alice
```

Decisions, preferences, procedures, action items, and narrowly signalled facts
enter a review queue with evidence. Rejection writes no long-term memory;
approval rechecks privacy and atomically claims the candidate before writing.
After an abnormal process termination, operators can inspect
`mm review list --status writing` and run `mm review recover`; fresh claims are
left untouched and every recovery is audit-recorded.

## Public evaluation corpus

The original regression portfolio uses synthetic data and fixes its contract
at 48 files, 192 chunks, and 100 Korean/English queries. The v2 holdout adds
120 bilingual queries with separate English, Korean, and cross-language tracks,
portable qrels, and pinned corpus/query hashes. Run both gates:

```bash
uv run python tools/retrieval-eval/audit_public_corpus.py
uv run python tools/retrieval-eval/check_baseline.py
uv run python tools/retrieval-eval/check_baseline_v2.py
```

The audit verifies corpus integrity and checks for secret-like values,
disallowed email addresses, and public IP literals before publication. The v2
model comparison and staged k-sweep are maintainer experiments; the one-run
k-sweep retains `top_k=10`, candidates `50/50`, `rrf_k=60`, and reranking off
until repeated validation supports a change.
