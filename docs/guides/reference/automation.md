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
| `auto_archive` | Move old/unused source files to an archive namespace | `{"max_age_days": 30}` |
| `auto_promote` | Move frequently accessed archived source files back to active namespace | `{"min_access_count": 5}` |
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
- `min_access_count`: a chunk matches only with access_count at most this value.
- `max_importance_score`: a chunk matches only with importance_score below this value.
- `archive_namespace_template`: target from each chunk's first tag via the `{first_tag}` placeholder (empty tags fall back to `"misc"`).

A source file is archived whole, and only once every one of its chunks matches:
archiving part of a file would leave it in two namespaces, which a forced
re-index refuses. A file whose chunks' tags route to different template targets
is skipped and reported as `split_targets` in the run's outcome. With a
`namespace_filter`, every chunk of the file must be in that namespace or
already in the target.

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

One qualifying chunk brings its whole source file back, so the file never ends
up split across namespaces. Every chunk of the file must be in a source
namespace or already in the target; a file with chunks elsewhere stays put.

> **Tip**: auto_promote resets `last_accessed_at` to the current time on every chunk it moves, preventing immediate re-archival by auto_archive (ping-pong prevention).

Neither policy merges or deletes to make room. A file whose move would put two
chunks with the same content at the same line into one namespace is left where
it is and listed under `conflicts` in the run's outcome, and the run reports an
error.

**auto_expire** — delete old unaccessed chunks:

```
mem_policy_add(name="expire-cold", policy_type="auto_expire",
    config='{"max_age_days": 90}')
```

Only chunks with `access_count = 0` are expired.

All three policies above skip held sources (missing, unreadable or
redaction-blocked files kept for recovery) and sources with a pending check, as
do `mem_decay_scan` / `mem_decay_expire`. Purge a source you have confirmed gone
with `mm gc orphan-sources --apply`.

**auto_consolidate** — summarize related chunks:

```
mem_policy_add(name="consolidate-sources", policy_type="auto_consolidate",
    config='{"min_group_size": 3, "max_groups": 10, "keep_originals": true}')
```

- `min_group_size`: minimum chunks per source file to qualify (default 3).
- `max_groups`: cap on groups processed per run (default 10).
- `max_bullets`: how many of a source's chunks the summary reads (default 20). Only those are linked to the summary and, with `keep_originals: false`, decayed; the rest stay uncovered, so agent consolidation can still group them.
- `keep_originals`: if `false`, soft-decays originals (importance *= 0.5, floor 0.3) instead of deleting. An original that is itself a summary (the target of a `consolidated_into` edge) keeps its importance.
- `summary_namespace`: target namespace for summaries (default `"archive:summary"`) — excluded from default search.

A summary takes the scope and project root of the chunks it summarises, so a
project's notes are not summarised into a user-wide chunk; a group whose
originals span more than one scope boundary is refused.

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
→ Archived 12 chunks older than 30 days → 'archive' (run #42)
```

Run all enabled policies at once:

```
mem_policy_run()
→ Policy run (dry run) results:
  - archive-stale (auto_archive): Would archive 12 chunks older than 30 days → 'archive'
  - promote-active (auto_promote): Would promote 3 chunks → 'default'
```

### Run history

Every *applied* (non-dry-run) run is recorded, so "what did the last
maintenance run change?" is answerable after the fact — which matters most for
`auto_expire`, whose deletion is real and irreversible. Dry runs record nothing.

Each record holds the run's status (`ok`, `error`, or `running` — either still
in flight, or interrupted before it could finish, which a missing completion
time distinguishes), where it came from (`scheduler` for the
background scheduler, `mcp` for a tool call), the affected count, the namespaces
touched, and a per-kind summary of what changed — including the chunk ids for
the destructive kinds (`auto_expire` deletions and the stale summaries
`auto_consolidate` replaces) and the ids moved by `auto_archive` /
`auto_promote`.

Pass `runs=N` to `mem_policy_list` to see the last N runs per policy:

```
mem_policy_list(runs=2)
→ Memory Policies (1):
  - expire-old (auto_expire, enabled) (last run: 2026-04-12T10:00:00)
    Config: {"max_age_days": 90}
    Runs:
    - #42 ok 2026-04-12T10:00:00+00:00 (scheduler) affected=8 ns=default,work
      deleted_ids: 8 — a1b2c3d4, e5f6a7b8, … (+3 more)
    - #38 error 2026-04-11T10:00:00+00:00 (mcp) — ValueError: disk full
```

`mem_consolidate_apply` records its runs too; because they belong to no policy,
they are listed under a trailing `Other maintenance runs:` block in the same
output. Records are kept for 90 days and are wiped by `mm reset`.

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
| `compaction`              | Hold unavailable source chunks outside search for recovery  |
| `importance_decay`        | Delete chunks older than `max_age_days` (TTL-based decay)   |
| `dead_chunk_link_cleanup` | Remove `chunk_links` rows whose source chunk is gone        |
| `dedup_scan`              | Surface duplicate-chunk candidates (no auto-merge)          |

Schedules are stored in SQLite and dispatched by the same watchdog tick
loop that powers `mem_watchdog` — set
`MEMTOMEM_SCHEDULER__ENABLED=true` on top of the watchdog config to
enable dispatch.

Dispatch is **at-most-once per cron slot**: each due schedule is claimed
with an atomic compare-and-set before it runs, so two MCP servers sharing
one database (e.g. Claude Code and Claude Desktop connected at the same
time) cannot both fire the same slot, and a run interrupted by a crash is
not re-fired on restart — it resumes at the next slot.

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

### Reading a run record

Each schedule keeps the outcome of its latest run, and the position of a job
that continues between runs:

| Field             | Meaning                                                                 |
|-------------------|-------------------------------------------------------------------------|
| `last_run_status` | `ok`, `skipped`, `error`, `timeout`, or `running` while a run is in flight |
| `last_run_error`  | Error text for `error` / `timeout`                                      |
| `last_run_result` | The summary the job returned (`null` for `error` / `timeout`)           |
| `state`           | Where the next run starts, for a job that continues (`dedup_scan`); `null` for the other jobs |
| `params_error`    | Why the stored `params` cannot be read, or `null` when they can (see below) |
| `cron_error`      | Why the dispatcher cannot schedule the row, or `null` when it can (see below) |

`skipped` means the job returned normally but did no work, and its
`skipped_reason` says why. For example, `dedup_scan` has no scanner
(`dedup_scanner_not_initialized`). `mm schedule list` prints the reason next
to the status. `mm schedule list --json` and `schedule_list` return the whole
record.

`cron_error` is checked each time the schedule is read, the same way the
dispatcher finds the next slot: from `last_run_at`, or `created_at` before the
first run. It does not depend on how the last run ended. It has four kinds of
value:
- `null`: the expression can be scheduled.
- A croniter parse error, such as `CroniterBadCronError: …` or its subclass
  `CroniterNotAlphaError: …`: croniter cannot read the expression, so the
  dispatcher skips the schedule on every tick and it never runs on its own.
  An expression stored with non-ASCII digits or spaces that croniter 6
  accepted is refused by croniter 7 this way. `mm schedule list` marks such a
  row `automatic runs blocked`; `mm schedule run-now <id>` still runs it.
  Under croniter 6 only, a random `R` field whose range leaves the field's
  bounds (`R(0-60)` for minutes) fails this way only on its out-of-range
  draws, so such a row can be reported blocked and still run; croniter 7
  refuses that expression every time.
- `unparsable last_run_at: …` or `unparsable created_at: …`: the stored
  timestamp the next slot is counted from is not an ISO date (an empty
  `last_run_at` included), so the dispatcher skips the schedule on every tick.
  `mm schedule list` marks such a row
  `automatic runs blocked: stored timestamp unreadable`.
- `unchecked: …`: the check did not reach a verdict. That covers
  `CroniterBadDateError` ("failed to find
  next date"), which croniter raises both for a date that never occurs
  (`0 0 30 2 *`) and for one draw of its random `R` field that the next draw
  may not repeat (`0 0 R(28-30) 2 *` fails only on the draw that lands on the
  30th). `mm schedule list` marks such a row `cron check failed`. The message
  is the same for both causes, so look at the row's `cron_expr`: one with no
  random field and a date that never occurs is skipped on every tick like a
  blocked row; replace it the same way.

  `mm schedule add` and `schedule_register` look for a date only when the
  expression has five fields separated by ASCII whitespace and is written
  with nothing but digits, `* , - / ? # L W` and three-letter month and day
  names, and they refuse it when croniter finds none, such as `0 0 30 2 *`.
  Any other expression the installed croniter parses is stored without that
  lookup: one with a random `R` field, six or seven fields, or other
  characters. (A hashed `H` field does not parse here: neither command
  passes the hash id croniter requires for it.) A row added that way, or
  added before the check, can still show `cron check failed` in the listing.

To replace a blocked schedule, add the corrected one first, written with ASCII
digits and spaces and for a date that exists, with the same `--job` and the
`params` that `mm schedule list --json` shows for the old row, passed as JSON
to `--params`. If `mm schedule add` refuses a key as unsupported, leave it out:
the job ignores keys it does not define. Then remove the old schedule with
`mm schedule delete <id>`. The new schedule starts with no run history, and a
`dedup_scan` walk starts again from the first pool.

A stored value that cannot be read affects its own schedule only; the others
keep running and listing. That holds for text that is valid UTF-8, which is
what every memtomem writer stores; bytes that are not, written to the database
by other means, still fail the whole listing. `state` and `last_run_result`
that are not a JSON object, or that nest deeper than 100 levels, read as
`null` with a warning in the server log. `params` read that way become `null` and
`params_error` gives the reason. They are never replaced by the job's
defaults: every run of that schedule, automatic or `run-now`, is recorded as
`error` with `invalid params: <reason>` before the job starts. The plain
`mm schedule list` shows only `(error)`; the reason is in
`mm schedule list --json`. Such a row has no params to copy, so replace it
with the params it was meant to have. Releases before `mm schedule add`
refused unknown params keys stored them as given; one nested deeper than 100
levels makes its row's params unreadable now, though the job ignored it.

A `dedup_scan` result also reports what the scan covered: `pool` (chunks
selected, at most `max_scan`), `remaining` (scannable chunks after the pool),
`probed` (pool chunks searched with their stored vector), `without_vector`
(pool chunks with no usable stored vector, not probed),
`same_source_read_only_pairs` (pairs left out because both members are
read-only chunks of the same file; neither can be removed) and `pairs_found`
(pairs before `limit`; when it is above `candidates`, the list was cut). The
result holds these counts, not the pairs themselves. To see pairs for a run's
pool, scan it again: run `mem_dedup_scan` with the run's `after` and
`threshold` and the schedule's `max_scan` (another `max_scan` or `threshold` is
another scan, which can report other pairs), and with `offset` when the pool holds more
pairs than one call lists. That scans the store as it is now, so the pairs can
differ from the ones the run counted.
- **Zero candidates with a large `without_vector` count** means part of the
  pool was never checked, not that it is duplicate-free.
- **`near_search_enabled: false`** (a BM25-only store) means only exact
  duplicates were looked for. `without_vector` is then `0` and says nothing
  about vector coverage.
- **The counts describe the selected pool**, not the whole store.

A `dedup_scan` schedule walks the store, one pool per run:

- **Each run scans the pool after the one the last finished run scanned.**
  The first run scans the first pool (the first `max_scan` scannable chunks
  in chunk-id order). A run that reaches the end of the store (`remaining`
  is `0`) makes the next run start from the first pool again, which is also
  how a chunk added before the position during a walk gets scanned. In a
  result, `after` is the chunk id the run started after and `next_after` the
  one the next run starts after; each is `null` for the first pool.
- **`state` holds the same position** as `{"after": "<chunk id>"}`, or
  `null` when the next run starts from the first pool. It is the stored
  position, not a description of a run in flight, and it is where to read
  the position after a run that ended in `error` or `timeout`, which has no
  result.
- **Only a scan that finished moves the position.** A run that ends in
  `error` or `timeout`, or is `skipped`, leaves it, so the next run scans
  the same pool. A pool that times out on every run keeps the schedule
  there; delete the schedule and register one with a smaller `max_scan`.
- **Each schedule has its own position.** `mm schedule run-now` and
  `schedule_run_now` move it as a dispatched run does. Deleting a schedule
  deletes its position, and a new schedule starts from the first pool.
- **The counts of a walk's runs do not add up to a count for the store.** A
  walk in steps is not one scan of the whole store: a near pair whose members
  are in two pools can be counted in both runs, and copies with one content
  hash in different pools are left to the near search (see
  [Deduplication](organization-maintenance.md#deduplication)).
- **Runs of one schedule that overlap** (a `run-now` while a dispatched run
  is in flight, or a run still going at its next slot) start from the same
  position and scan the same pool, and the position is the one the last of
  them to finish writes. A run that finishes after others have moved on
  puts the position back at the end of its own pool: the pools after it are
  scanned again, and a chunk added in the meantime before that position
  waits for the next walk from the start. If that happens on every walk,
  such a chunk can stay unscanned.

> Phase A is direct-cron only. Natural-language schedules
> (`spec="every Sunday 3am"`) and `disable`/`enable` commands arrive in
> Phase B and Phase C respectively.

---
