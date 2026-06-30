# Automation

Run memory lifecycle work on a schedule: declarative policies and cron-driven jobs.

[← memtomem Reference](../reference.md)

**On this page**

- [8. Memory Policies — mem_policy_add, mem_policy_list, mem_policy_run](#8-memory-policies--mem_policy_add-mem_policy_list-mem_policy_run)
- [9. Scheduled jobs — mm schedule, schedule_*](#9-scheduled-jobs--mm-schedule-schedule_)

---

## 8. Memory Policies — `mem_policy_add`, `mem_policy_list`, `mem_policy_run`

Policies automate memory lifecycle management. Instead of manually running
maintenance tasks, you define rules that archive old memories, expire unused
ones, consolidate related chunks, promote frequently accessed archives, or
auto-tag new content. Each policy runs on demand via `mem_policy_run` or
automatically in the background with the built-in scheduler.

### Policy types

| Type | What it does | Example config |
|------|-------------|----------------|
| `auto_archive` | Move old/unused chunks to an archive namespace | `{"max_age_days": 30}` |
| `auto_promote` | Move frequently accessed archived chunks back to active namespace | `{"min_access_count": 5}` |
| `auto_expire` | Delete very old chunks with zero access | `{"max_age_days": 90}` |
| `auto_consolidate` | Group chunks by source file into heuristic summaries | `{"min_group_size": 3}` |
| `auto_tag` | Auto-tag untagged chunks | `{"max_tags": 5}` |

### Creating a policy

**auto_archive** — move old memories to archive:

```
mem_policy_add(name="archive-stale", policy_type="auto_archive",
    config='{"max_age_days": 30, "archive_namespace": "archive"}')
→ Policy 'archive-stale' created
```

With categorized buckets and access/importance filters:

```
mem_policy_add(name="archive-categorized", policy_type="auto_archive",
    config='{"max_age_days": 90, "age_field": "last_accessed_at",
             "min_access_count": 3, "max_importance_score": 0.3,
             "archive_namespace_template": "archive:{first_tag}"}')
```

- `age_field`: `"created_at"` (default) or `"last_accessed_at"` (null-safe via COALESCE).
- `min_access_count`: only archive chunks with access_count at most this value.
- `max_importance_score`: only archive chunks with importance_score below this value.
- `archive_namespace_template`: per-chunk target using the `{first_tag}` placeholder (empty tags fall back to `"misc"`).

**auto_promote** — bring back frequently accessed archives:

```
mem_policy_add(name="promote-active", policy_type="auto_promote",
    config='{"min_access_count": 3, "target_namespace": "default"}')
```

With importance and recency filters:

```
mem_policy_add(name="promote-recent", policy_type="auto_promote",
    config='{"source_prefix": "archive", "target_namespace": "default",
             "min_access_count": 3, "min_importance_score": 0.5,
             "recency_days": 30}')
```

- `source_prefix`: namespace prefix to scan (default `"archive"` — matches both `archive` and `archive:*`).
- `target_namespace`: destination namespace (default `"default"`).
- `min_access_count`: minimum access count to qualify (default 3).
- `min_importance_score`: optional importance floor (AND with access count).
- `recency_days`: only promote if accessed within this many days. Note: this is the *opposite* of auto_archive's age cutoff — here, *recent* access qualifies a chunk.

> **Tip**: auto_promote resets `last_accessed_at` to the current time on promotion, preventing immediate re-archival by auto_archive (ping-pong prevention).

**auto_expire** — delete old unaccessed chunks:

```
mem_policy_add(name="expire-cold", policy_type="auto_expire",
    config='{"max_age_days": 90}')
```

Only chunks with `access_count = 0` are expired.

**auto_consolidate** — summarize related chunks:

```
mem_policy_add(name="consolidate-sources", policy_type="auto_consolidate",
    config='{"min_group_size": 3, "max_groups": 10, "keep_originals": true}')
```

- `min_group_size`: minimum chunks per source file to qualify (default 3).
- `max_groups`: cap on groups processed per run (default 10).
- `keep_originals`: if `false`, soft-decays originals (importance *= 0.5, floor 0.3) instead of deleting.
- `summary_namespace`: target namespace for summaries (default `"archive:summary"`) — excluded from default search.

**auto_tag** — tag untagged chunks:

```
mem_policy_add(name="tag-new", policy_type="auto_tag",
    config='{"max_tags": 5}')
```

### Listing and deleting

```
mem_policy_list()
→ Memory Policies (2):
  - archive-stale (auto_archive, enabled) (last run: 2026-04-12T10:00:00)
    Config: {"max_age_days": 30, "archive_namespace": "archive"}
  - promote-active (auto_promote, enabled)
    Config: {"min_access_count": 3, "target_namespace": "default"}

mem_policy_delete(name="archive-stale")
→ Policy 'archive-stale' deleted.
```

### Running policies

Always preview first with `dry_run=True` (the default), then apply:

```
mem_policy_run(name="archive-stale")
→ [DRY RUN] Would archive 12 chunks older than 30 days → 'archive'

mem_policy_run(name="archive-stale", dry_run=False)
→ Archived 12 chunks older than 30 days → 'archive'
```

Run all enabled policies at once:

```
mem_policy_run()
→ Policy run (dry run) results:
  - archive-stale (auto_archive): Would archive 12 chunks older than 30 days → 'archive'
  - promote-active (auto_promote): Would promote 3 chunks → 'default'
```

Use `namespace_filter` when creating a policy to restrict it to a specific namespace:

```
mem_policy_add(name="archive-work", policy_type="auto_archive",
    config='{"max_age_days": 60}', namespace_filter="work")
```

### Background scheduler

Set these environment variables (or use `mm init` to configure) to run policies
automatically in the background while the MCP server is active:

| Variable | Default | Description |
|----------|---------|-------------|
| `MEMTOMEM_POLICY__ENABLED` | `false` | Enable the background policy scheduler |
| `MEMTOMEM_POLICY__SCHEDULER_INTERVAL_MINUTES` | `60.0` | Minutes between policy runs |
| `MEMTOMEM_POLICY__MAX_ACTIONS_PER_RUN` | `100` | Cumulative action cap per scheduled run |

When enabled, the scheduler runs all enabled policies periodically. The
`max_actions` cap is checked between policies — individual handlers run
atomically. Policies can always be run on-demand via `mem_policy_run`
regardless of this setting.

> **`PolicyScheduler` requires the MCP server, not `mm web`.** Like the
> cron scheduler in §9, the policy scheduler is wired into the MCP server
> lifespan only — `mm web` logs a warning at startup if
> `policy.enabled=true` but does not actually run policies. Run
> `memtomem-server` (or connect a Claude Code / Claude Desktop MCP
> session) for periodic dispatch; `mem_policy_run` works regardless.

### Combining policies

A common pattern is pairing auto_archive with auto_promote:

```
mem_policy_add(name="archive-old", policy_type="auto_archive",
    config='{"max_age_days": 60, "age_field": "last_accessed_at",
             "archive_namespace_template": "archive:{first_tag}"}')

mem_policy_add(name="promote-hot", policy_type="auto_promote",
    config='{"min_access_count": 5, "recency_days": 14}')
```

This creates a lifecycle where old unused memories move to categorized
archive buckets, but any archived chunk that gets accessed 5+ times in the
last 14 days is automatically promoted back. The `last_accessed_at` reset
on promotion prevents the chunk from being immediately re-archived.

---

## 9. Scheduled jobs — `mm schedule`, `schedule_*`

Phase A of the cron scheduler ships **direct-cron** registration for the
maintenance jobs the watchdog already runs. Each schedule is a 5-field
cron expression interpreted in **UTC**, paired with one of the
whitelisted `JOB_KINDS`:

| `job_kind`                | Effect                                                      |
|---------------------------|-------------------------------------------------------------|
| `compaction`              | Delete chunks whose source files no longer exist on disk    |
| `importance_decay`        | Delete chunks older than `max_age_days` (TTL-based decay)   |
| `dead_chunk_link_cleanup` | Remove `chunk_links` rows whose source chunk is gone        |
| `dedup_scan`              | Surface duplicate-chunk candidates (no auto-merge)          |

Schedules are stored in SQLite and dispatched by the same watchdog tick
loop that powers `mem_watchdog` — set
`MEMTOMEM_SCHEDULER__ENABLED=true` on top of the watchdog config to
enable dispatch.

> **Dispatch requires the MCP server, not `mm web`.** The watchdog (and
> therefore the schedule dispatcher) is wired into the MCP server
> lifespan only — it is not started by `mm web`. Schedules registered
> through `mm schedule add` while only `mm web` is running will sit
> idle (`mm schedule list` shows `last=never (—)`) until an MCP server
> session
> (e.g. a connected Claude Code / Claude Desktop client) is also
> active. Use `mm schedule run-now <id>` for one-off out-of-band
> execution that does not depend on the watchdog tick.

```bash
mm schedule add --cron "0 3 * * 0" --job compaction
mm schedule list
mm schedule run-now <id>           # out-of-band run; same timeout as dispatcher
mm schedule delete <id>
```

The same actions are reachable through `mem_do`:

```
mem_do(action="schedule_register",
       params={"cron": "0 3 * * 0", "job_kind": "compaction"})
mem_do(action="schedule_list")
mem_do(action="schedule_run_now", params={"id": "<id>"})
mem_do(action="schedule_delete", params={"id": "<id>"})
```

> Phase A is direct-cron only. Natural-language schedules
> (`spec="every Sunday 3am"`) and `disable`/`enable` commands arrive in
> Phase B and Phase C respectively.

---
