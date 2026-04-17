# B.2 Multilingual large-corpus regression — v2 design

Design doc for the expanded multilingual regression suite. Replaces the
exploratory MVP on `feat/multilingual-regression-mvp` (preserved, not
published) which demonstrated that 6-topic broad tags saturate MRR and
collapse rrf_weights sensitivity on EN — particularly that small-scale
with broad tagging cannot distinguish genuine pipeline regressions from
noise.

This v2 scales to ~400 chunks with hierarchical tagging, cross-genre
content, and a typed query portfolio so that per-stage regressions
become observable.

## Phase 1 validation status

Phase 1 (16 sample chunks, caching × ko, 4 genres) validated the
methodology with important caveats — see `b2-v2-phase1-validation.md`
for full measurements. Summary:

- Dense embeddings do NOT separate genres (topic dominant) — expected
- BM25 raw Jaccard does NOT separate genres at set level (subtopic
  diversity overrides genre marker vocabulary)
- BUT high-IDF genre marker tokens persist 4/4 in their home genre
  (`후속/조치/KST/원인` for postmortem; `채택/결정/대신/trade-off` for
  adr; `SET/CONFIG/절차` for runbook; `증상/의심/만약` for
  troubleshooting)

Consequence: v2 `rrf_weights` sensitivity depends on **genre-primary
queries that use anchor vocabulary**, not on chunk-side homogeneity.
Plan updated:

- Query portfolio grows from 80 → 100 per language (adds 10
  genre-primary queries), see `b2-v2-query-portfolio.md` § "REQUIRED
  (Phase 4)"
- Phase 2b gates the remaining 14 topics on a sensitivity check:
  `rrf_weights=[1,0]` vs `[0,1]` on genre-primary queries must
  produce different top-K. Fail-early mechanism to avoid wasted
  generation.

## Intent

A CI-runnable regression gate that detects quality regressions in
memtomem's search pipeline across:

- EN and KO, with KO as the primary signal (per user audience)
- Different content genres (runbook / postmortem / ADR / troubleshooting)
- Different query archetypes (direct / paraphrase / underspecified /
  multi-topic / negation)
- Graded relevance (primary + secondary topic tagging → nDCG)

Scope is still CI-sized (target < 3 min on test-golden-path job).
Research-grade benchmarking is out of scope.

## Methodology: Claude-designed, Gemini-drafted, Claude-curated

Independent LLMs provide genuine distribution diversity that
single-author corpus cannot:

- **Claude** (this session): designs matrix + subtopic vocabulary +
  query portfolio, writes Gemini prompt templates, curates and
  normalizes Gemini output, converts to fixture format, runs
  calibration.
- **Gemini** (offline, user-driven): generates chunk content per cell
  using closed-set subtopic constraints and genre style constraints.
- **User**: executes Gemini prompts, passes JSON output back for
  curation.

Neither Claude alone nor Gemini alone produces the corpus — the
division is deliberate. Claude as sole author produces
embedding-homogeneous content (same training distribution); Gemini as
sole author drifts on subtopic taxonomy without closed-set
enforcement.

## Topic × genre matrix

15 topics × 4 genres = 60 cells. Target 3-4 chunks per cell per
language → 180-240 chunks per language, 360-480 total.

### Topics (frozen upfront)

`caching`, `postgres`, `k8s`, `observability`, `ci_cd`, `auth`,
`kafka`, `search`, `networking`, `security`, `ml_ops`,
`data_pipelines`, `cost_optimization`, `incident_response`,
`api_design`.

Topic-level vocabulary is closed. New topics are not added during
corpus generation.

### Genres (frozen upfront)

| Genre | Style constraint |
|---|---|
| `runbook` | Imperative present tense, ordered steps, command-heavy. "Run X. Check Y. If Z, then W." |
| `postmortem` | Narrative past tense, timeline-driven, root cause + remediation. "At 03:40 UTC, X failed. Root cause: Y. Action: Z." |
| `adr` | Decision-framed, trade-off discussion. "Chose X over Y because A, accepting B." |
| `troubleshooting` | Symptom → diagnosis command → root cause → workaround. Similar to runbook but diagnostic rather than operational. |

Each genre must produce text with measurably distinct vocabulary and
structure — this is what creates variance in BM25 and dense
embeddings for the same topic.

### Matrix constraints

- Every (topic, genre) cell has ≥ 1 chunk per language
- Each topic row sums to 12-15 chunks (across 4 genres)
- Each genre column sums to 25% ± 5% of total (balanced across genres)
- Each cell mixes ≥ 2 different primary subtopics (avoids
  subtopic-monoculture within a cell)

## Subtopic vocabulary (seed, with emergence policy)

Topic-level is frozen; subtopic-level starts with 3-5 seeds per topic
and allows emergent additions. Freeze trigger: after the first 80
chunks (two cells × 2 languages × 10 chunks on average), no new
subtopics admitted — only re-use.

### Seed subtopics

```
caching/          redis, eviction, invalidation, stampede, replication
postgres/         indexing, replication, vacuum, connection_pool, partitioning
k8s/              scheduling, networking, storage, scaling, rollout
observability/    metrics, logging, tracing, alerting, synthetic
ci_cd/            pipeline, caching, deployment, testing, release
auth/             oauth, jwt, mtls, rbac, session, webauthn
kafka/            producer, consumer, topic, connect, streams
search/           indexing, query, relevance, cluster, ingestion
networking/       dns, load_balancing, tls, service_mesh, connection_pool
security/         vulnerability, secrets, encryption, access_control, incident
ml_ops/           training, serving, monitoring, feature_store, versioning
data_pipelines/   ingestion, transformation, orchestration, quality, warehouse
cost_optimization/ compute, storage, network, database, observability
incident_response/ detection, mitigation, communication, postmortem, oncall
api_design/       rest, grpc, rate_limiting, pagination, idempotency
```

~70 subtopics total. Gemini prompts reference this closed set.

### Cross-cutting concerns

Cross-cutting concepts (`performance`, `data_consistency`, `high_availability`)
are **absorbed into topic subtopics** rather than admitted as separate
axis. Rationale: nDCG relevance lives in a single tag space; adding
facets complicates graded-relevance rules. Examples of absorption:

- `performance/latency` in a caching chunk → `caching/stampede` or
  re-scope the chunk primary to `observability/metrics`
- `data_consistency/eventual` in a caching chunk → `caching/invalidation`
- `high_availability/failover` in a Redis chunk →
  `caching/replication`; in a Postgres chunk →
  `postgres/replication`; for cluster-level failover →
  `incident_response/mitigation`

## Relevance model

Each chunk declares:

```markdown
<!-- primary: topic/subtopic -->
<!-- secondary: topic/subtopic, topic/subtopic, topic/subtopic -->
```

Secondary is 0-3 tags (not always 2-3 — some chunks are tightly
single-focus).

### Relevance grading

For a query with target tag set `Q`:

| Condition | Relevance score |
|---|---|
| chunk's primary ∈ Q | 1.0 |
| chunk's primary ∉ Q but any secondary ∈ Q | 0.5 |
| no overlap | 0.0 |

Multi-topic queries (target `Q` = {tag_A, tag_B}): chunks whose primary
matches *either* target get 1.0; chunks whose primary matches *both*
(rare but possible via secondary) get 1.0 + 0.5 capped at 1.0 for
binary metrics.

### Metric usage

- **recall@10**: primary-relevant only (binary). Catches catastrophic
  misses.
- **MRR@10**: primary-relevant only (binary). Catches top-1 position
  regressions.
- **nDCG@10**: graded (1.0 primary, 0.5 secondary, 0.0 none). Catches
  subtle ordering regressions where a secondary-relevant chunk
  outranks a primary-relevant one.

`ndcg_at_k` (already implemented in `tests/ir_metrics.py` in v1) now
has an actual consumer.

## Query portfolio

40 queries per language, 5 types each serving a different regression
signal.

### EN distribution

| Type | Count | Relevant size (primary) | Detects |
|---|---|---|---|
| `direct` | 10 | 2-3 | Catastrophic ranker failure |
| `paraphrase` | 10 | 2-3 | Dense embedding degradation |
| `underspecified` | 8 | 5-8 | Reranker / MMR diversity |
| `multi_topic` | 7 | 4-6 (union across 2 topics) | Fusion weight miscalibration |
| `negation` | 5 | 1-2 | Dense semantic understanding |

### KO distribution

| Type | Count | Relevant size (primary) | Detects |
|---|---|---|---|
| `direct` | 10 | 2-3 | Same as EN |
| `paraphrase` | 10 | 2-3 | Same as EN |
| `underspecified` | 10 | 5-8 | Same as EN (+2 slots from negation) |
| `multi_topic` | 7 | 4-6 | Same as EN |
| `negation` | 3 | 1-2 | Reduced — KO negation often stilted |

### Examples (one per type, EN)

- **direct**: "Redis maxmemory-policy allkeys-lru eviction"
- **paraphrase**: "preventing Redis from losing hot data under memory
  pressure"
- **underspecified**: "cache invalidation"
- **multi_topic**: "monitoring Postgres replication lag"
- **negation**: "why eventual consistency is unsuitable for inventory"

### Thresholds per type

Floors are per-type (not per-language-aggregate) so `direct`-query
regressions don't hide behind `underspecified` noise:

- `EN_FLOOR_direct_recall10`, `EN_FLOOR_direct_mrr10`,
  `EN_FLOOR_direct_ndcg10`
- `EN_FLOOR_paraphrase_recall10`, ...
- etc. — 5 query types × 3 metrics × 2 languages = 30 assertion
  constants.

## Gemini prompt template

Template prompts Gemini to generate N chunks per call, constrained by
closed-set subtopic list. User runs this per (topic, genre, language)
batch. See `docs/testing/b2-gemini-prompt-template.md`.

Key prompt constraints:

1. Closed set of allowed subtopics pasted in-prompt
2. "Do not invent new tags — choose closest if no fit"
3. Mix ≥ 2 primary subtopics across the batch
4. Specified genre style with exemplar sentence
5. JSON output schema

## Implementation phases

| Phase | Deliverable | Human gate |
|---|---|---|
| **0** | v2 branch + clean infrastructure (done) | — |
| **1** | This design doc + Gemini prompt template + first cell sample (4 chunks) | user reviews sample for style/vocab quality |
| **2** | Gemini batch drafts per cell (60 cells × 2 langs = 120 batches) | user generates, shares JSON |
| **3** | Claude curates, normalizes subtopics, converts to markdown | user reviews per-batch |
| **4** | Query portfolio final draft (80 queries) | user reviews |
| **5** | Calibration (10-run determinism, threshold setting) | — |
| **6** | Sensitivity check per query type (rrf_weights extremes) | — |
| **7** | CI wiring + PR | user reviews PR |

Phase 1 is the current cycle's scope.

## Verification strategy

### Calibration

10-run determinism with `PYTHONHASHSEED=0 OMP_NUM_THREADS=1`. Variance
across runs must be ≤ 0.02 per metric before setting floors.

### Sensitivity

Three `rrf_weights` configurations — `[1,1]` balanced, `[1,0]`
BM25-only, `[0,1]` dense-only. Must produce meaningfully different
scores on **at least one** query type per language. If a query type
stays pipeline-invariant on BOTH languages under all three configs,
that type is demoted from floor assertion (measured but not
enforced) — document as residual limit.

### Chunk-size / corpus-scale contingency

If 200 chunks/lang still produces EN-wide collapse (no query type
discriminates rrf_weights), the v2 plan escalates to:

- Expand corpus to 300 chunks/lang (add more genres per topic or
  widen subtopics)
- Or accept "EN is pipeline-invariant under current model
  configuration" as a documented limit and ship as KO-primary +
  EN-monitoring regression.

## What this plan is not

- Not a research-grade IR benchmark (no statistical significance
  testing, no model comparison sweep)
- Not a replacement for unit tests of individual pipeline stages
  (those live in `test_search_stages.py`)
- Not a latency benchmark (timings captured but not asserted)
- Not a multi-model regression test (fastembed `MiniLM-L12` only)
