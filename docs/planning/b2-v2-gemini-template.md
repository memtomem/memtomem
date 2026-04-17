# B.2 Gemini prompt template

Prompt Gemini runs per (topic × genre × language) batch. Generates 4-5
chunks for one matrix cell. User executes this offline; Claude curates
the JSON output.

See `b2-multilingual-regression-v2.md` for the design rationale (why
Gemini, why closed-set vocabulary, how the curator reviews).

## Template

Copy this verbatim and fill in the four `{{...}}` placeholders per
batch. Do not modify the vocabulary list between batches.

---

```
You are generating technical documentation chunks for a multilingual
search regression test corpus. The corpus is loaded into a BM25 + dense
hybrid search pipeline; chunks with high vocabulary diversity and
genre-typical style produce the ranking variance this test needs.

## This batch
- Topic: {{TOPIC}}
- Genre: {{GENRE}}
- Language: {{LANGUAGE}}   (ko | en)
- Chunk count: 4

## Rules

1. Each chunk has one `primary_subtopic` chosen from the closed list
   below. The 4 chunks in this batch must cover at least 2 different
   primary subtopics — do NOT put all 4 on the same subtopic.

2. Each chunk has 1-3 `secondary_subtopics` from the closed list. They
   should reflect adjacent concerns the chunk actually discusses —
   not "related topics in general". If a chunk is tightly focused,
   0 secondary is fine.

3. Do NOT invent new subtopic names. If a concept doesn't fit the
   list, pick the closest match. The closed list is exhaustive for
   this project.

4. Content: 150-400 characters (after any leading whitespace), 3-5
   sentences, written in the specified genre style. For Korean,
   write in natural Korean technical prose (Claude-equivalent native
   fluency); do not translate from English — write fresh in Korean.

5. Genre styles are not cosmetic — they must be measurably different
   in vocabulary, tense, and structure (this is what creates BM25/dense
   variance for the same topic):

   - `runbook`: imperative present tense, ordered steps, command-heavy.
     Example tone: "Run `X`. Verify `Y`. If `Z` is not N, then tune W."
   - `postmortem`: narrative past tense, timeline-driven, root cause +
     remediation. Example tone: "At HH:MM UTC on YYYY-MM-DD, A began
     failing. Investigation revealed B. We mitigated by C and have
     since added D."
   - `adr`: decision-framed, trade-off discussion. Example tone: "We
     chose X over Y because A outweighs B. Accepted trade-off: C."
   - `troubleshooting`: symptom → diagnosis command → root cause →
     workaround. Example tone: "If symptom S, run `cmd` to check C.
     Likely root cause: R. Workaround: W."

6. Include ≥ 2 specific technical artifacts per chunk: a command,
   config key, metric name, version number, or similar. "Redis"
   alone is not specific; "Redis `maxmemory-policy allkeys-lru`" is.

## Closed subtopic vocabulary

Use exact strings (with the `/` separator). Copy verbatim — no renames.

caching/redis, caching/eviction, caching/invalidation,
caching/stampede, caching/replication

postgres/indexing, postgres/replication, postgres/vacuum,
postgres/connection_pool, postgres/partitioning

k8s/scheduling, k8s/networking, k8s/storage, k8s/scaling, k8s/rollout

observability/metrics, observability/logging, observability/tracing,
observability/alerting, observability/synthetic

ci_cd/pipeline, ci_cd/caching, ci_cd/deployment, ci_cd/testing,
ci_cd/release

auth/oauth, auth/jwt, auth/mtls, auth/rbac, auth/session, auth/webauthn

kafka/producer, kafka/consumer, kafka/topic, kafka/connect,
kafka/streams

search/indexing, search/query, search/relevance, search/cluster,
search/ingestion

networking/dns, networking/load_balancing, networking/tls,
networking/service_mesh, networking/connection_pool

security/vulnerability, security/secrets, security/encryption,
security/access_control, security/incident

ml_ops/training, ml_ops/serving, ml_ops/monitoring,
ml_ops/feature_store, ml_ops/versioning

data_pipelines/ingestion, data_pipelines/transformation,
data_pipelines/orchestration, data_pipelines/quality,
data_pipelines/warehouse

cost_optimization/compute, cost_optimization/storage,
cost_optimization/network, cost_optimization/database,
cost_optimization/observability

incident_response/detection, incident_response/mitigation,
incident_response/communication, incident_response/postmortem,
incident_response/oncall

api_design/rest, api_design/grpc, api_design/rate_limiting,
api_design/pagination, api_design/idempotency

## Cross-cutting concerns

`performance`, `data_consistency`, `high_availability` are NOT axes in
this vocabulary. They are absorbed:

- Performance/latency → map to the monitoring aspect
  (`observability/metrics`) or the load aspect
  (`caching/stampede`, `postgres/indexing`)
- Data consistency → map to the mutation aspect
  (`caching/invalidation`, `postgres/replication`,
  `data_pipelines/quality`)
- High availability → map to the replication or failover aspect
  (`caching/replication`, `postgres/replication`, `k8s/rollout`,
  `incident_response/mitigation`)

## Output format

JSON array of exactly 4 objects. No prose around the array — raw JSON.

```json
[
  {
    "primary_subtopic": "{{TOPIC}}/<subtopic>",
    "secondary_subtopics": ["<topic/subtopic>", "<topic/subtopic>"],
    "genre": "{{GENRE}}",
    "language": "{{LANGUAGE}}",
    "content": "..."
  },
  ...
]
```

## Example (for reference, do not copy)

For batch (topic=caching, genre=troubleshooting, language=ko), 4 chunks
might cover: 2× `caching/redis`, 1× `caching/eviction`,
1× `caching/invalidation`. See attached samples in
`fixtures/corpus_v2/ko/caching/troubleshooting.md` if provided.
```

---

## Curator checklist (Claude, post-batch)

When user returns Gemini JSON, run these checks before converting to
markdown:

1. **Subtopic conformance**: every `primary_subtopic` and
   `secondary_subtopic` ∈ closed list. Flag any drift; ask user
   whether to remap or reject the chunk.
2. **Primary diversity**: batch covers ≥ 2 different primary subtopics.
   If not, ask user to regenerate or pick 2 from the batch.
3. **Genre conformance**: skim each `content` for tense / imperative /
   narrative / decision / troubleshooting style match. Obvious mismatches
   → reject.
4. **Technical specificity**: each chunk has ≥ 2 specific artifacts
   (command/config/metric). Generic "use Redis caching" without
   specifics → reject.
5. **Length**: 150-400 chars. Truncate verbose or ask for expansion.
6. **Language authenticity** (KO): no translation-style phrasing.
   Spot-check sentence structure.
7. **Deduplication**: if two chunks in a batch are near-duplicates
   (same scenario, different wording), keep only one.
8. **Synthetic disclaimer**: top of each genre file gets
   `> Synthetic content for search regression testing — verify before adopting as runbook.`

Surviving chunks are written to
`packages/memtomem/tests/fixtures/corpus_v2/{language}/{topic}/{genre}.md`
as H2 sections with topic comments (see existing sample for format).
