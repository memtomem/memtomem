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
