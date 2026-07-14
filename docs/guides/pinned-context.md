# Pinned Context and review-first memory

Pinned Context is for small facts or instructions that must be included before
ordinary search results. Blocks remain Markdown files and use the same user,
project-shared, project-local, privacy, and confirmation rules as other
memtomem writes.

```bash
mm pinned set response-style \
  --content "Prefer concise Korean answers with runnable commands." \
  --description "Default response style" --priority 10

mm pinned list
mm pinned get response-style --scope user
mm pinned compose "deployment checklist"
mm pinned delete response-style --scope user
```

Choose `--scope user|project_local|project_shared` on exact get/set/delete
operations. Writes to the Git-tracked shared tier require explicit consent:

```bash
mm pinned set team-policy --scope project_shared \
  --confirm-project-shared --content "Run the release checklist before tagging."
```

Agent-specific blocks use `--agent NAME`. For the same block id, agent-specific
beats general and `project_local` beats `project_shared`, which beats `user`.
One block is limited to 2,000 characters. The 6,000-character value is the
pinned portion of one compose operation, not a global storage limit; the
default complete composed bundle is limited to 12,000 characters. A block is
never cut in the middle and omitted ids are returned explicitly.

`mem_context_compose` schema 2 accepts the same optional `namespace` and
`context_window` retrieval controls used by search. Schema 3 additionally
returns adjacent chunks under each retrieved item's optional `context` object.
Matched hits retain schema 2 budget priority; de-duplicated adjacent chunks use
only the remaining composed bundle character budget, selected globally by
distance with hit rank and before/after as deterministic tie-breakers. Pinned
files remain searchable through legacy `mem_search`, but the composed path
excludes every file under the active user and project `pinned/` roots from its retrieved leg,
including shadowed, agent-specific, and malformed blocks. This prevents pinned
content from appearing a second time as an ordinary search result.

Schema 3 is a new capability instead of an in-place schema 2 change because
schema 2 shipped in memtomem 0.3.9 without adjacent chunks in its response.
Keeping that released contract immutable lets clients distinguish scoped
composition from composition that also preserves visible context windows.

## Review-first candidates

Candidate generation is explicit and never writes long-term memory by itself.

An external client can propose one candidate through
`mem_candidate_propose(content, source, source_ref, idempotency_key)`. Content
must be non-empty and at most 2,000 characters; source, source reference, and
idempotency key are limited to 128, 512, and 256 characters. The content and
source reference are privacy-scanned. Reusing a key with identical content
returns the original pending candidate; using it for different content fails.

```bash
mm review scan SESSION_ID
mm review list
mm review show CANDIDATE_ID
mm review approve CANDIDATE_ID --reviewer "$USER"
# or: mm review reject CANDIDATE_ID --reason "temporary detail"
```

Candidates use only events belonging to the selected session and retain event
and chunk evidence. Secret-bearing events are skipped. Pending candidates
expire after 30 days; only approval invokes the normal durable file write.

Approval first claims a candidate as `writing`. Normal write failures and
cancellation return it to `pending`. If the process is forcibly terminated,
inspect and recover claims older than the conservative 15-minute threshold:

```bash
mm review list --status writing
mm review recover --stale-after-minutes 15 --actor alice
```

The equivalent MCP action is `mem_candidate_recover`. Recovery is atomic with
approval finalization, does not touch fresh claims, and records a persistent
`writing → pending` transition with the operator and reason.

If a recovered claim's original process later reports that its durable write
already completed, memtomem moves the candidate to `write_uncertain` and
reports the destination. Do not approve it again: inspect the Markdown or
Pinned Context block first, then close the quarantine without another write:

```bash
mm review reject CANDIDATE_ID \
  --reviewer alice \
  --reason "confirmed the durable entry already exists"
```

The same `mem_candidate_review` MCP action accepts `decision="reject"` with a
non-empty reviewer and reason. The atomic `write_uncertain → rejected`
transition is audit-recorded; direct re-approval remains blocked.

## LangGraph

Install the optional adapters. `MemtomemBaseStore` implements LangGraph's
tuple-namespace JSON `BaseStore` contract:

```bash
uv add 'memtomem[langgraph]'
```

```python
from memtomem.integrations.langgraph import MemtomemBaseStore

store = MemtomemBaseStore()
store.put(("users", "alice"), "preferences", {"style": "concise"})
item = store.get(("users", "alice"), "preferences")
```

Canonical records are inspectable JSON files under the configured memory root;
semantic projections are derived. TTL is deliberately unsupported and raises
an explicit error.

`MemtomemStore` is the higher-level async adapter for memtomem search, writes,
sessions, and working memory. Its `config_overrides` are applied after
`config.d/` and `~/.memtomem/config.json`, so constructor values win over the
ambient process configuration. Use that precedence to isolate tests or graphs:

```python
from memtomem.integrations.langgraph import MemtomemStore

store = MemtomemStore(
    config_overrides={
        "storage": {"sqlite_path": "/tmp/my-graph/memtomem.db"},
        "indexing": {"memory_dirs": ["/tmp/my-graph/memories"]},
    }
)
```

Unknown override sections or keys raise `ValueError` on the first async call
instead of silently falling back to the user's default database.
