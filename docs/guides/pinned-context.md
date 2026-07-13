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
mm pinned compose "deployment checklist"
```

Agent-specific blocks use `--agent NAME`. For the same block id, agent-specific
beats general and `project_local` beats `project_shared`, which beats `user`.
One block is limited to 2,000 characters, all pinned blocks to 6,000, and a
default composed bundle to 12,000. A block is never cut in the middle; omitted
ids are returned explicitly.

`mem_context_compose` schema 2 accepts the same optional `namespace` and
`context_window` retrieval controls used by search. Pinned files remain
searchable through legacy `mem_search`, but the composed path excludes every
file under the active user and project `pinned/` roots from its retrieved leg,
including shadowed, agent-specific, and malformed blocks. This prevents pinned
content from appearing a second time as an ordinary search result.

## Review-first candidates

Candidate generation is explicit and never writes long-term memory by itself.

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

Install the optional adapter and pass it to a graph as its store:

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
