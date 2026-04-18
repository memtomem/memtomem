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

### Scope narrowed to 6 topics (2026-04-18)

Corpus expansion paused at **n=6 topics** (caching, postgres,
cost_optimization, security, observability, k8s — 192 chunks total,
96 per language). Rationale: the chunk-level artifact candidate
hypothesis reached the k ≥ 4 confirmation threshold (§ 11.4 of
`b2-v2-phase1-validation.md`) at k8s with n=5 topic-strong cluster
and no falsifying cases. Further topic additions would be
confirmation-only at diminishing ROI. Corpus-building effort
exceeded the budget for this phase.

**Remaining 9 topics** (`ci_cd`, `auth`, `kafka`, `search`,
`networking`, `ml_ops`, `data_pipelines`, `incident_response`,
`api_design`) are **deferred, not cancelled**. They can be added
later as regression-test corpus expansion without any methodology
change — the 15-topic freeze, closed vocabulary, and Gemini prompt
template remain valid. Trigger conditions for resumption:

- Phase 5 calibration reveals a blind spot that new topics could
  resolve
- A new pipeline feature needs topic coverage that the 6 topics
  do not exercise
- Post-release regression gap points at an untested topic

**Phase 4-7 run on the 6-topic corpus**. Downstream consequences:
- Query portfolio (Phase 4): 100 queries still achievable across
  6 topics × 4 genres × 2 langs; topical diversity reduced from
  ~15 to 6.
- Calibration (Phase 5): per-topic thresholds computed from 6
  measurements instead of 15; statistical power lower, still
  publishable.
- H1/H2/H3 retirement / reformulation (Phase 5): decision uses
  existing evidence (observability 28.1% + k8s 40.6% both rejected
  all three bands); no additional topic required.
- Genre-pair confusion matrix (Phase 5): 4 × 4 × 2 = 32 cells
  from 6-topic corpus, same structure.
- CI wiring (Phase 6): unaffected.

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

## Subtopic vocabulary (frozen 2026-04-17)

Topic-level is frozen from the start. Subtopic-level started with 3-5
seeds per topic in "emergent + mid-way freeze" mode; freeze trigger
was after the first 80 chunks (two cells × 2 languages × 10 chunks on
average).

**Freeze declared 2026-04-17** at 96 chunks (caching 32 + postgres 32
+ cost_optimization 32). No new subtopics emerged during the first
three topics; closed vocabulary below proved sufficient.

**Post-freeze amendment protocol** (see
`b2-v2-phase2b-ledger.md` § "Subtopic freeze declaration"):
- If a chunk cannot be mapped to any existing subtopic, route to the
  nearest closest entry and log the mismatch in the ledger as
  "unresolved gap".
- When unresolved-gap entries accumulate ≥ 3 distinct cases (same
  conceptual gap across ≥ 3 topics), trigger deliberate vocabulary
  expansion review — documented amendment with rationale, not
  emergency addition.
- Silent vocabulary expansion is the failure mode to prevent; the
  protocol exists so "we had to add a subtopic" is always a
  conscious decision with its own ledger entry.

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
| **3a** | Claude curates: drift correction against closed vocabulary, evidence check against chunk body | user reviews per-batch — **mandatory human-in-the-loop step** |
| **3b** | Claude converts curated batches to markdown; later (≥ 5 topics) extract drift validator rules | script-verifiable |
| **4** | Query portfolio final draft (100 queries per language, includes genre-primary axis) | user reviews |
| **5** | Calibration (10-run determinism, threshold setting, genre confusion matrix) | — |
| **6** | Sensitivity check per query type (rrf_weights extremes) | — |
| **7** | CI wiring + PR | user reviews PR |

Phase 3 is **split into 3a (drift correction) and 3b (markdown conversion)** because
curation consistently finds ~30% drift rate across topics (Phase 1 caching ko,
Phase 2a caching en, Phase 2b postgres). Phase 3a is not optional — raw Gemini
output does not meet the closed-vocabulary contract. See
`b2-v2-phase2b-ledger.md` for the curation ledger.

Phases 1, 2a, and 2b are complete at the time this revision lands (see
`b2-v2-phase1-validation.md` § 8-10 for measurements).

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

**Divergence — formal definition** (Phase 2b-established, frozen):

```python
# For each genre-primary query q in language L against corpus C:
bm25_res, _  = await search(q, top_k=3, rrf_weights=[1.0, 0.0])
dense_res, _ = await search(q, top_k=3, rrf_weights=[0.0, 1.0])
diverge = 1 if [r.chunk.id for r in bm25_res] != [r.chunk.id for r in dense_res] else 0
```

Divergence rate = Σ diverge(q) / N_queries, reported as "X/N KO,
Y/N EN". All 14 topics use this exact definition. Post-hoc
redefinition (top-5, set-equality, top-1-only) prohibited. See
`b2-v2-phase2b-ledger.md` § "Formal definitions" for canonical
reference.

**Query-body overlap pre-measurement rule** (Phase 2c-established):

Before running divergence for any topic, measure genre-primary
queries against that topic's fixtures. Target overlap < 0.5 (ratio
of query topic-tokens also appearing in target-genre body). If
≥ 0.5, flag in ledger; the divergence reading for that genre
becomes "measurement-consistent but signal-confounded". See
`b2-v2-phase1-validation.md` § 11.5 for the cost_opt adr precedent.

### Chunk-size / corpus-scale contingency

If 200 chunks/lang still produces EN-wide collapse (no query type
discriminates rrf_weights), the v2 plan escalates to:

- Expand corpus to 300 chunks/lang (add more genres per topic or
  widen subtopics)
- Or accept "EN is pipeline-invariant under current model
  configuration" as a documented limit and ship as KO-primary +
  EN-monitoring regression.

### Postgres genre-primary pipeline invariance (B-2 finding, Phase 2b)

Measured at Phase 2b checkpoint (`b2-v2-phase1-validation.md` § 10 has
full data):

- Strengthened-query divergence on genre-primary queries: **0/4 KO,
  0/4 EN** under `rrf_weights=[1,0]` vs `[0,1]`
- Cross-topic bleed (caching+postgres 64 chunks): **1/16 (6.3%)**,
  below the pre-registered "medium+" threshold for redesigning genre
  rotation
- Top-1 genre accuracy: KO 3/4, EN 4/4 — accuracy healthy, **fusion
  weight sensitivity absent**
- Query IDF sum: KO 23.42 (+42% vs caching baseline), EN 18.67 (+19%
  vs caching baseline). Both **exceed the measurement-matched target**,
  ruling out "weak query" as the invariance cause — stronger queries
  still produce zero divergence

**Decision (B-2)**: demote postgres genre-primary floors to
measurement-only (retained in the portfolio but not asserted against
CI floors). Retain 10 genre-primary postgres queries for documentation
and cross-topic bleed tracking. The rest of the postgres query-type
floors (direct/paraphrase/underspecified/multi_topic/negation) remain
active — only the genre-primary axis is demoted on this topic.

### Topic-strong vs topic-weak — testable predictions

**Original hypothesis (Phase 2b)**: a topic's genre-primary
`rrf_weights` sensitivity is inversely related to the density of
topic-proper-noun vocabulary that saturates the dense embedding
regardless of genre framing.

**Status as of Phase 2c (2026-04-17): falsified by cost_optimization
counter-prediction**. Cost_opt subtopics (compute/storage/network/
database/observability) are generic cloud-engineering concepts, not
proper-noun-dense, yet cost_opt measured 0/8 divergence — same as
postgres (proper-noun-heavy). See `b2-v2-phase1-validation.md` § 11.3.

**Revised candidate hypothesis** (n=2, pending security
falsification): chunk-level artifact density dominates topic-level
vocabulary density in determining BM25/dense agreement. Every chunk
in both postgres and cost_opt contains 2+ distinctive technical
artifacts (commands, config keys, proper nouns) that anchor both
retrievers to the same result. Full falsification conditions in
`b2-v2-phase1-validation.md` § 11.4.

| Predicted profile (original) | Topics | Expected genre-primary divergence | Measured |
|---|---|---|---|
| **Topic-strong** (command/API vocabulary dominates) | postgres ✓, k8s, kafka | Low: 0-2/8 | postgres 0/8 ✓ |
| **Topic-weak** (conceptual narrative) | caching ✓, security, cost_optimization | High: 6-8/8 | **cost_opt 0/8 ✗ — counter-prediction** |
| **Middle** (mixed) | observability, ci_cd, auth, networking | 3-5/8 | not yet measured |

**Revised ordering rationale** (post-cost_opt): kafka's information
value as "boundary test" reduced since cost_opt (conceptual) already
shows topic-strong. If security also shows topic-strong, kafka
shifts to confirmation-only and alternative boundary candidates
(observability) may replace it. Decision deferred to post-security
per `b2-v2-phase2b-ledger.md` § "Kafka cadence contingency".

Checkmarks denote topics where divergence has been measured.

**Topic ordering for validation**: run topic-weak candidates first
(cost_optimization ✓ measured, security next) to establish the
divergence pattern, then introduce the boundary case (kafka —
proper-noun-heavy but concept-rich) before confirming the clean
topic-strong case (k8s). See `b2-v2-handoff.md` Phase 2c actions
for the full cadence.

### Drift validator — Phase 3b (implemented 2026-04-18)

Implemented at `tools/retrieval-eval/drift_validator.py` after the
n=5 topic-strong cluster satisfied the "≥ 5 topics ledgered"
trigger. Two tiers locked; greenlist deferred until a false-positive
case appears.

- **Forbidden** (auto-reject, CLI exit 1): closed-vocab violations
  (unknown topic, unknown subtopic, malformed tag) plus the
  `genre-postmortem-vs-ir-postmortem-subtopic` rule (k8s Pattern 2,
  3 events).
- **Manual-review** (warn, no block): three body-contextual rules
  — `kubectl logs` diagnostic vs `observability/logging` subtopic
  (k8s Pattern 1, 3 events); `security/access_control` primary with
  RBAC-specific body (security ledger, 2 events); `security/encryption`
  primary with transport-layer body (security ledger, 3 events;
  suppressed if secondary already names `networking/tls` or
  `auth/mtls`, matching borderline-preserved curation precedent).

Rule sources are documented inline in `drift_validator.py` with
ledger citations. Tests at `packages/memtomem/tests/test_drift_validator.py`
(23 tests). Current corpus passes with zero violations.

The validator is a drift-detection aid for the human curator, not a
replacement. Phase 3a remains mandatory human-in-the-loop. CI wiring
lands at Phase 6.

## What this plan is not

- Not a research-grade IR benchmark (no statistical significance
  testing, no model comparison sweep)
- Not a replacement for unit tests of individual pipeline stages
  (those live in `test_search_stages.py`)
- Not a latency benchmark (timings captured but not asserted)
- Not a multi-model regression test (fastembed `MiniLM-L12` only)
