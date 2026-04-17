# B.2 v2 — Phase 2b handoff for next session

**If you are a new Claude session picking this up**: read this file
first, then `b2-v2-design.md`, `b2-v2-phase1-validation.md`, and
`b2-v2-query-portfolio.md` in that order for full context. You'll
already have enough to continue Phase 2b without re-deriving
methodology.

## Branch state (as of handoff)

- **Branch**: `feat/multilingual-regression-v2` (branched from `main`,
  not pushed to remote)
- **Exploratory branch**: `feat/multilingual-regression-mvp`
  (preserved as reference for why broad-tag MVP failed; un-pushed)
- **Commits on v2 branch** (newest first):
  - `5b083c8` — Phase 2a: complete caching × en × 4 genres
    (16 chunks, EN anchor sensitivity validated 8/8 BM25 top-1)
  - `e495638` — Phase 1: methodology validation
    (16 caching × ko chunks + 4 planning docs + ir_metrics infra)

## Phase progress

| Phase | State | Deliverable |
|---|---|---|
| 1 | ✅ committed | caching × ko × 4 genres; dense/BM25 asymmetry + anchor mechanism validated |
| 2a | ✅ committed | caching × en × 4 genres; EN parity confirmed |
| **2b** | **🔄 in progress** | **postgres × 4 genres × 2 langs = 8 batches; 2 JSON received (below), 6 remaining** |
| 2c onwards | 📋 not started | 13 remaining topics × 8 batches each |
| 3-7 | 📋 not started | normalization, query portfolio, calibration, CI wiring, PR |

## Phase 2b completed batches — raw JSON + normalizations

These two Gemini batches were returned and reviewed but not yet
converted to markdown fixtures. Apply the normalizations below, then
convert to markdown files under
`packages/memtomem/tests/fixtures/corpus_v2/{en,ko}/postgres/` using
the same format as the caching files (H2 section per chunk,
`<!-- primary: ... -->` + `<!-- secondary: ... -->` tags, disclaimer
at top of file).

### Batch 1: runbook EN (postgres) — 4 chunks

Raw Gemini JSON:

```json
[
  {
    "primary_subtopic": "postgres/indexing",
    "secondary_subtopics": ["postgres/vacuum", "cost_optimization/compute"],
    "genre": "runbook",
    "language": "en",
    "content": "Connect to the target database using `psql`. Run `CREATE INDEX CONCURRENTLY` on the `users` table to add a new B-tree index without blocking concurrent writes. Monitor the `pg_stat_progress_create_index` view to track the build percentage and state. Once completed, execute `ANALYZE users` to ensure the query planner has updated distribution statistics."
  },
  {
    "primary_subtopic": "postgres/partitioning",
    "secondary_subtopics": ["postgres/indexing", "data_pipelines/transformation"],
    "genre": "runbook",
    "language": "en",
    "content": "Prepare the storage for the upcoming month by creating a child table with `CREATE TABLE events_2026_05 PARTITION OF events`. Define the range using the `FOR VALUES FROM ('2026-05-01') TO ('2026-06-01')` clause. Attach any existing standalone tables using the `ALTER TABLE ... ATTACH PARTITION` command. Verify the new hierarchy in the `pg_inherits` system catalog."
  },
  {
    "primary_subtopic": "postgres/connection_pool",
    "secondary_subtopics": ["networking/connection_pool", "observability/metrics"],
    "genre": "runbook",
    "language": "en",
    "content": "Open the `pgbouncer.ini` configuration file on the proxy node. Update the `max_client_conn` to 2000 and ensure the `pool_mode` is set to `transaction` for high-concurrency workloads. Execute a `RELOAD` command via the PGBouncer admin console to apply these settings without dropping active sessions. Monitor the `cl_active` metric to verify the new connection limits are respected."
  },
  {
    "primary_subtopic": "postgres/replication",
    "secondary_subtopics": ["postgres/vacuum", "incident_response/mitigation"],
    "genre": "runbook",
    "language": "en",
    "content": "Initiate a physical base backup of the primary node using the `pg_basebackup -D /var/lib/postgresql/data` command. Configure the `postgresql.auto.conf` file on the standby node with the required `primary_conninfo` string and `promote_trigger_file` path. Start the service and inspect the `pg_stat_wal_receiver` view to confirm the stream is active. Verify the replication lag using the `pg_last_wal_receive_lsn()` function."
  }
]
```

Normalizations to apply before markdown conversion:

| Chunk | Fix |
|---|---|
| 1 (indexing) | secondary `[postgres/vacuum, cost_optimization/compute]` → `[observability/metrics]` (ANALYZE is statistics/metrics; compute-cost not discussed) |
| 2 (partitioning) | secondary `[postgres/indexing, data_pipelines/transformation]` → `[data_pipelines/ingestion]` (monthly partitioning = time-series ingestion; transformation is ETL/dbt, wrong fit) |
| 3 (connection_pool) | keep as-is — both secondary fit |
| 4 (replication) | secondary `[postgres/vacuum, incident_response/mitigation]` → `[incident_response/mitigation]` (vacuum unrelated to replication setup) |

Proposed section headings (for the markdown file):
1. `Create indexes without blocking writes`
2. `Create monthly partition for events table`
3. `Scale PgBouncer for high-concurrency workloads`
4. `Set up streaming replication with pg_basebackup`

### Batch 2: troubleshooting KO (postgres) — 4 chunks

Raw Gemini JSON:

```json
[
  {
    "primary_subtopic": "postgres/vacuum",
    "secondary_subtopics": ["observability/metrics", "cost_optimization/storage"],
    "genre": "troubleshooting",
    "language": "ko",
    "content": "테이블 용량이 비정상적으로 비대해지면 `pg_stat_all_tables` 뷰에서 `n_dead_tup` 수치를 확인하세요. `autovacuum`이 작동하지 않아 bloat이 심화된 경우, `VACUUM (VERBOSE, ANALYZE)`를 실행하여 데드 튜플을 강제로 정리해야 합니다. 향후 재발 방지를 위해 `autovacuum_vacuum_scale_factor`를 0.05로 낮추어 더 자주 실행되도록 설정하십시오."
  },
  {
    "primary_subtopic": "postgres/connection_pool",
    "secondary_subtopics": ["networking/connection_pool", "observability/alerting"],
    "genre": "troubleshooting",
    "language": "ko",
    "content": "'Too many connections' 에러가 발생하면 우선 `pg_stat_activity`에서 유휴 세션의 상태를 점검하세요. 클라이언트 측에서 `PgBouncer`와 같은 풀러를 사용 중인지 확인하고, 서버의 `max_connections` 설정값이 하드웨어 리소스 대비 적절한지 검토하십시오. 임시 조치로 `idle_in_transaction_session_timeout`을 설정하여 좀비 커넥션을 자동으로 정리하세요."
  },
  {
    "primary_subtopic": "postgres/indexing",
    "secondary_subtopics": ["search/query", "observability/tracing"],
    "genre": "troubleshooting",
    "language": "ko",
    "content": "특정 API의 응답 시간이 급증했다면 `EXPLAIN (ANALYZE, BUFFERS)` 명령어로 쿼리 실행 계획을 추출하세요. 실행 계획에 `Seq Scan`이 포함되어 있다면 인덱스가 누락되었거나 통계 정보가 오래된 상태입니다. `CREATE INDEX CONCURRENTLY`를 통해 서비스 중단 없이 인덱스를 생성하고, `ANALYZE`를 실행하여 쿼리 플래너의 통계치를 갱신하십시오."
  },
  {
    "primary_subtopic": "postgres/replication",
    "secondary_subtopics": ["caching/replication", "incident_response/mitigation"],
    "genre": "troubleshooting",
    "language": "ko",
    "content": "복제본(Replica)의 데이터가 최신이 아니라면 `pg_current_wal_lsn()`과 `pg_last_wal_receive_lsn()`의 차이를 계산하여 복제 지연량을 확인하세요. 네트워크 대역폭 문제라면 `max_wal_senders`와 `wal_keep_size` 파라미터를 상향 조정해야 합니다. 지연이 복구되지 않을 경우 `pg_basebackup`을 사용하여 대기 서버를 재구축하는 방안을 고려하십시오."
  }
]
```

Normalizations to apply:

| Chunk | Fix |
|---|---|
| 1 (vacuum) | drop `cost_optimization/storage` — keep `[observability/metrics]` (bloat→storage cost is indirect, not explicit) |
| 2 (connection_pool) | `observability/alerting` → `observability/metrics` (alerting not discussed; pg_stat_activity monitoring is metrics-level) |
| 3 (indexing) | drop `[search/query, observability/tracing]` → `[observability/metrics]` (search/* is ES/OpenSearch vocab, not SQL; tracing = distributed tracing, not latency metrics) |
| 4 (replication) | keep as-is — `caching/replication` is intentional cross-topic link (Redis↔Postgres replication comparison) |

Proposed section headings:
1. `n_dead_tup 폭증 — autovacuum 조정`
2. `Too many connections — PgBouncer + idle session cleanup`
3. `API 응답 시간 급증 — EXPLAIN 으로 plan 분석`
4. `복제 지연 — WAL LSN 비교 및 재구축`

## Phase 2b remaining batches — 6 Gemini prompts to run

User runs these in Gemini, one at a time, and returns JSON to
Claude. Each prompt is self-contained.

**Prompt template structure** (same for all 6):

1. Intro paragraph ("You are generating...")
2. `## This batch` — topic=postgres, genre varies, language varies, count=4
3. `## Rules` — 6 rules (rule 5 is genre-specific, see below)
4. `## Closed subtopic vocabulary` (~70 subtopics, identical to Phase 1/2a)
5. `## Cross-cutting concerns` (identical)
6. `## Drift to avoid` (new addition — `search/*`, `observability/tracing`,
   `data_pipelines/transformation` common drift cases)
7. `## Output` — JSON array schema

The common sections (vocabulary, cross-cutting, drift) are
reproducible from `b2-v2-gemini-template.md`. New session: when
reproducing the 6 prompts, use `b2-v2-gemini-template.md` as base
and fill in:

- `TOPIC: postgres`
- `GENRE: <varies>` (runbook | postmortem | adr | troubleshooting)
- `LANGUAGE: <varies>` (ko | en)
- Rule 5 genre example with postgres-themed content

Topic-themed genre examples (already drafted — reuse verbatim):

### runbook KO example
> "Postgres 프라이머리에 접속한다. `ALTER SYSTEM SET shared_buffers = '4GB'`
> 를 실행하여 shared buffer 풀을 조정한다. `pg_ctl reload` 로 적용 후
> `SHOW shared_buffers` 로 반영 여부를 확인한다."

### postmortem KO example
> "2026-03-05 14:22 KST 부터 배치 쿼리 지연이 30분 이상 지속됐다. 원인은
> `autovacuum` 이 대형 테이블에서 취소되어 bloat 가 급증한 것이었다.
> `VACUUM ANALYZE` 수동 실행 후 실행 계획이 정상화됐다. 후속 조치로
> `autovacuum_vacuum_scale_factor` 를 0.1 로 조정했다."

### adr KO example
> "Native range partitioning 대신 pg_partman 기반 월 단위 declarative
> partitioning 을 채택했다. Native 는 attach/detach 가 빠르지만
> single-column 파티션 key 제약이 있다. Attach 속도 일부를 감수하고
> pg_partman 의 유연성을 선택했다. 파티션당 row 수가 10억을 초과하면
> 재검토한다."

### postmortem EN example
> "At 2026-03-05 14:22 UTC, batch query latency began exceeding 30
> minutes. Root cause: autovacuum had been cancelled on a large
> table, causing bloat to spike. We ran manual `VACUUM ANALYZE` and
> plans normalized. Follow-up: lowered
> `autovacuum_vacuum_scale_factor` to 0.1."

### adr EN example
> "We chose declarative monthly partitioning with pg_partman over
> native range partitioning. Native offers faster attach/detach but
> imposes single-column key constraints. We accepted slower attach
> in exchange for pg_partman's flexibility. Revisit if
> per-partition row count exceeds 1B."

### troubleshooting EN example
> "If query time exceeds 100× expected, run
> `EXPLAIN (ANALYZE, BUFFERS)` to inspect the plan. A
> `Seq Scan on large_table` indicates a likely missing index. As a
> workaround, `SET enable_seqscan = off`; longer-term, identify and
> create the missing index."

### Drift-avoidance block (new; add to all prompts)

```
## Drift to avoid

- `search/*` = search infrastructure (ElasticSearch/OpenSearch), NOT
  SQL queries. For SQL query diagnosis use postgres/* or
  observability/metrics.
- `observability/tracing` = distributed tracing (spans), NOT general
  latency monitoring (use observability/metrics).
- `data_pipelines/transformation` = ETL/dbt, NOT partitioning or
  generic data ops.
```

## Next-session first actions (in order)

1. **Read this handoff**, then `b2-v2-design.md` §§ 1-5,
   `b2-v2-phase1-validation.md` §§ 1-9, and
   `b2-v2-query-portfolio.md` § "REQUIRED (Phase 4)".

2. **Ask user whether the 2 completed batches (runbook EN +
   troubleshooting KO) are still wanted as-is**, or if they want to
   regenerate. If as-is: curate (apply normalizations above) and
   write to markdown. If regenerate: drop these batches and rerun
   Gemini with updated prompts that may have stricter drift
   guidance.

3. **Prepare the 6 remaining Gemini prompts** (runbook KO,
   postmortem KO, adr KO, postmortem EN, adr EN, troubleshooting
   EN) per the template in `b2-v2-gemini-template.md`, fill in
   topic-themed examples above, and present to user.

4. **User runs 6 in Gemini, returns JSON.**

5. **Curate all 6** — apply same normalization discipline. Expected
   drift rate ~30-40% secondary-subtopic remaps per batch based on
   Phase 1/2a history.

6. **Convert 8 batches (2 already-received + 6 new) to 8 markdown
   files** under `fixtures/corpus_v2/{en,ko}/postgres/{runbook,
   postmortem,adr,troubleshooting}.md`.

7. **Run anchor sensitivity spot-check** on postgres-only corpus
   (32 chunks). Expected: 6-8/8 genre-primary queries diverge
   between `rrf_weights=[1,0]` and `[0,1]`. Use the same script
   pattern as Phase 2a verification (`/tmp/phase2a_verify.py`
   was the recipe; reproduce from Phase 2a commit if needed).

8. **Run cross-topic sensitivity** on combined caching+postgres
   (64 chunks). Verify that queries referencing one topic don't
   bleed into the other topic's chunks excessively.

9. **Commit Phase 2b** as `test(memtomem): add postgres corpus
   for B.2 v2 (Phase 2b)` with findings summary.

10. **Prepare Phase 2c prompts** for the next topic (suggested:
    `k8s`, but any remaining topic works). Topic-unit cadence
    (8 batches) from here on.

## Key invariants (do not drift from these)

- Topic vocabulary is frozen at 15 topics (see `b2-v2-design.md`).
  No additions without explicit plan update.
- Subtopic vocabulary is in "emergent + mid-way freeze" mode; freeze
  target is after ~80 chunks total. At handoff, ~64 chunks exist
  (caching 32 + postgres 32 after 2b). Freeze trigger is imminent.
- Genre-primary queries are a **required** Phase 4 axis (not deferred
  memo). Portfolio count: 100 queries per language (was 80).
- KO is primary regression signal; EN is parity + best-effort.
- Do NOT introduce cross-cutting tags (`performance`,
  `data_consistency`, `high_availability`) — absorb into existing
  topic subtopics per `b2-v2-design.md` rules.
- Every fixture commit should include the
  `> Synthetic content for search regression testing — verify
  before adopting as runbook.` disclaimer at the top of the file.
- AI attribution opt-in: include `Co-Authored-By: Claude` in commits
  per user's prior explicit approval for v2 PR work.
